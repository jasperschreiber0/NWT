"""
execution/engine.py
Execution Engine — lifecycle service.
Zero opinion on whether to trade. Executes only what has been approved.

Each run (every 5 min via cron):
  1. Check no_trade_mode flag — exit if set.
  2. Upsert heartbeat.
  3. Run position monitor — close equity positions at stop/target/max-hold.
  4. Process pending TRADE_REQUEST tickets (place new orders).
  5. Process FORCE_CLOSE tickets (options hard close from Risk Agent).
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import requests
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

from ledger import close_position, get_open_positions, insert_position, log_system_event

_here = Path(__file__).parent
load_dotenv(_here / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("execution_engine")

ALPACA_BASE_URL = os.environ["ALPACA_BASE_URL"].rstrip("/")
ALPACA_DATA_URL = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
ALPACA_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]
NWT_DB_DSN = os.environ["NWT_DB_DSN"]
SHARED_DIR = Path(os.environ.get("SHARED_DIR", _here.parent / "shared"))

ALPACA_HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json",
}

REQUIRED_FIELDS = {
    "approved", "bot_source", "symbol", "direction",
    "strategy_id", "sized_notional", "asset_type", "time_in_force",
}

POLL_INTERVAL = 3
POLL_MAX = 10
ET_TZ = ZoneInfo("America/New_York")
DIRECTIONAL_CAP_PCT = 0.60   # 60% of account equity per direction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_db() -> psycopg2.extensions.connection:
    return psycopg2.connect(NWT_DB_DSN)


def load_master_directives() -> dict:
    with open(SHARED_DIR / "master-directives.json") as f:
        return json.load(f)


def alpaca_get(path: str) -> dict:
    url = f"{ALPACA_BASE_URL}/v2{path}"
    resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def alpaca_post(path: str, body: dict) -> dict:
    url = f"{ALPACA_BASE_URL}/v2{path}"
    resp = requests.post(url, headers=ALPACA_HEADERS, json=body, timeout=15)
    if not resp.ok:
        logger.error("Alpaca POST %s → %d: %s", path, resp.status_code, resp.text[:500])
    resp.raise_for_status()
    return resp.json()


def get_current_price(symbol: str) -> float:
    url = f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/trades/latest"
    resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
    resp.raise_for_status()
    return float(resp.json()["trade"]["p"])


def get_alpaca_account_equity() -> float:
    account = alpaca_get("/account")
    return float(account.get("equity", 97_000))


def poll_order_until_filled(order_id: str) -> dict:
    for attempt in range(POLL_MAX):
        order = alpaca_get(f"/orders/{order_id}")
        status = order.get("status", "")
        if status == "filled":
            return order
        if status in ("canceled", "expired", "rejected", "done_for_day"):
            logger.warning("Order %s terminal status: %s", order_id, status)
            return order
        logger.info("Order %s status=%s (attempt %d/%d)", order_id, status, attempt + 1, POLL_MAX)
        time.sleep(POLL_INTERVAL)
    return alpaca_get(f"/orders/{order_id}")


def insert_decision(conn, ticket_id: str, decision: str, reasoning: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_ticket_decisions (ticket_id, decision, reasoning, decided_by)
            VALUES (%s, %s, %s, 'EXECUTION_ENGINE')
            """,
            (ticket_id, decision, reasoning),
        )
    conn.commit()


def fetch_pending_tickets(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT t.*
            FROM nwt_tickets t
            WHERE t.to_agent = 'EXECUTION_ENGINE'
              AND t.type = 'TRADE_REQUEST'
              AND t.from_agent IN (
                  'EU_EXECUTOR', 'AUS_EXECUTOR', 'CHINA_EXECUTOR', 'NWT_EXECUTION_AGENT'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM nwt_ticket_decisions d
                  WHERE d.ticket_id = t.ticket_id
                    AND d.decided_by = 'EXECUTION_ENGINE'
              )
            ORDER BY t.created_at ASC
            """,
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def fetch_force_close_tickets(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT t.*
            FROM nwt_tickets t
            WHERE t.to_agent = 'EXECUTION_ENGINE'
              AND t.type IN ('FORCE_CLOSE', 'CLOSE_REQUEST')
              AND NOT EXISTS (
                  SELECT 1 FROM nwt_ticket_decisions d
                  WHERE d.ticket_id = t.ticket_id
                    AND d.decided_by = 'EXECUTION_ENGINE'
              )
            ORDER BY t.created_at ASC
            """
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# no_trade_mode check
# ---------------------------------------------------------------------------

def check_no_trade_mode(conn) -> tuple[bool, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT value, reason FROM nwt_system_flags WHERE flag = 'no_trade_mode'")
        row = cur.fetchone()
    if row and row[0]:
        return True, row[1] or "no_trade_mode flag is set"
    return False, ""


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def upsert_heartbeat(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_heartbeat (service, last_beat, status)
            VALUES ('execution_engine', NOW(), 'ok')
            ON CONFLICT (service) DO UPDATE SET last_beat=NOW(), status='ok'
            """
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Directional cap
# ---------------------------------------------------------------------------

def check_directional_cap(conn, direction: str, incoming_notional: float) -> tuple[bool, float, float]:
    """
    Returns (exceeded, total_exposure, cap).
    """
    try:
        equity = get_alpaca_account_equity()
    except Exception:
        equity = 97_000.0

    cap = equity * DIRECTIONAL_CAP_PCT
    ledger_direction = "long" if direction in ("long", "buy") else "short"

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(notional_risk), 0)
            FROM nwt_portfolio_ledger
            WHERE status = 'open' AND direction = %s
            """,
            (ledger_direction,),
        )
        existing = float(cur.fetchone()[0])

    total = existing + incoming_notional
    return total > cap, total, cap


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def compute_qty_from_notional(sized_notional: float, price: float) -> int:
    return max(int(sized_notional / price), 1)


def place_equity_order(payload: dict) -> dict:
    symbol = payload["symbol"]
    sized_notional = float(payload["sized_notional"])
    time_in_force = payload["time_in_force"]
    direction = payload["direction"]

    price = get_current_price(symbol)
    qty = compute_qty_from_notional(sized_notional, price)
    side = "buy" if direction == "long" else "sell"

    order_body = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": time_in_force,
    }
    logger.info("Placing equity order: %s %s x%d (notional=%.2f)", side, symbol, qty, sized_notional)
    return alpaca_post("/orders", order_body)


def place_options_order(payload: dict) -> dict:
    option_symbol = payload["option_symbol"]
    qty = int(payload.get("qty", 1))
    time_in_force = payload["time_in_force"]
    direction = payload["direction"]
    side = "buy" if direction in ("long", "buy") else "sell"

    order_body = {
        "symbol": option_symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": time_in_force,
        "order_class": "simple",
    }
    logger.info("Placing options order: %s %s x%d", side, option_symbol, qty)
    return alpaca_post("/orders", order_body)


def place_close_order(symbol: str, qty: int, asset_type: str) -> dict:
    """Place a market sell/close order."""
    order_body = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
    }
    if asset_type == "option":
        order_body["order_class"] = "simple"
    logger.info("Placing close order: %s x%d", symbol, qty)
    return alpaca_post("/orders", order_body)


# ---------------------------------------------------------------------------
# pnl_adjusted computation
# ---------------------------------------------------------------------------

def compute_pnl_adjusted(asset_type: str, pnl: float, entry_price: float,
                          exit_price: float, qty: int = 1,
                          bid_ask_spread: float = 0.0) -> tuple[float, str]:
    """
    Returns (pnl_adjusted, slippage_model).
    Options: haircut 0.5 × spread × 100 × qty per side (entry + exit).
    Equity: haircut 1bp per side.
    """
    if asset_type == "option":
        spread = bid_ask_spread if bid_ask_spread > 0 else max(entry_price * 0.02, 0.05)
        haircut = 0.5 * spread * 100 * qty * 2  # entry + exit
        return pnl - haircut, "half_spread_v1"
    else:
        notional = entry_price * qty
        haircut = notional * 0.0001 * 2  # 1bp per side × 2 sides
        return pnl - haircut, "equity_1bp_v1"


def write_trade_outcome(conn, position: dict, fill_price: float,
                        exit_reason: str, strategy_id: str = None) -> None:
    """Write a completed nwt_trade_outcomes row. Called on options position close."""
    entry_price = float(position.get("entry_price") or 0)
    notional = float(position.get("notional_risk") or 0)
    qty = max(int(round(notional / (entry_price * 100))) if entry_price > 0 else 1, 1)

    pnl = (fill_price - entry_price) * qty * 100
    if position.get("direction") == "short":
        pnl = -pnl

    pnl_adj, slippage_model = compute_pnl_adjusted(
        "option", pnl, entry_price, fill_price, qty
    )

    entry_time = position.get("entry_time")
    entry_dt = entry_time if isinstance(entry_time, datetime) else datetime.now(timezone.utc)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_trade_outcomes (
              strategy_id, symbol, direction,
              entry_price, entry_time, exit_price, exit_time,
              pnl, pnl_pct, pnl_adjusted, slippage_model,
              position_id, closed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, NOW())
            ON CONFLICT DO NOTHING
            """,
            (
                strategy_id or position.get("bot_source", "UNKNOWN"),
                position.get("asset", ""),
                position.get("direction", ""),
                entry_price, entry_dt, fill_price,
                round(pnl, 4),
                round(pnl / notional, 6) if notional > 0 else 0,
                round(pnl_adj, 4),
                slippage_model,
                str(position.get("position_id", "")),
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Position monitor (equity multi-day exits)
# ---------------------------------------------------------------------------

def run_equity_position_monitor(conn) -> None:
    """
    For each open equity position: fetch price, check stop/target/max_hold.
    Closes via market order if hit. Writes exit to ledger.
    Options are handled by execution_agent.py.
    """
    positions = get_open_positions(conn)
    equity_positions = [p for p in positions if p.get("asset_type") == "equity"
                        and p.get("bot_source") != "UNATTRIBUTED"]

    if not equity_positions:
        return

    for pos in equity_positions:
        symbol = pos.get("asset", "")
        position_id = str(pos.get("position_id", ""))
        entry_price = float(pos.get("entry_price") or 0)
        notional = float(pos.get("notional_risk") or 0)
        direction = pos.get("direction", "long")
        strategy_id = pos.get("bot_source", "")

        if entry_price <= 0 or not symbol:
            continue

        try:
            current_price = get_current_price(symbol)
        except Exception as exc:
            logger.warning("Position monitor: cannot fetch price for %s: %s", symbol, exc)
            continue

        pnl_pct = (current_price - entry_price) / entry_price
        if direction == "short":
            pnl_pct = -pnl_pct

        # Load genome for stop/target/max_hold — skip if genome unavailable
        stop_pct = -0.015   # default 1.5% stop
        target_pct = 0.025  # default 2.5% target
        max_hold_days = 20
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM nwt_strategy_genome WHERE strategy_id = %s AND active = TRUE",
                    (strategy_id,),
                )
                genome = cur.fetchone()
            if genome:
                stop_pct = -abs(float(genome["stop_loss_pct"]))
                target_pct = float(genome["profit_target_pct"])
        except Exception:
            pass

        # Check max hold
        entry_time = pos.get("entry_time")
        if entry_time:
            age_days = (datetime.now(timezone.utc) - entry_time.replace(tzinfo=timezone.utc)).days
            if age_days >= max_hold_days:
                _close_equity_position(conn, pos, current_price, position_id, symbol,
                                       notional, entry_price, "max_hold")
                continue

        # Check stop/target
        exit_reason = None
        if pnl_pct <= stop_pct:
            exit_reason = "stop"
        elif pnl_pct >= target_pct:
            exit_reason = "target"

        if exit_reason:
            _close_equity_position(conn, pos, current_price, position_id, symbol,
                                   notional, entry_price, exit_reason)


def _close_equity_position(conn, pos, current_price, position_id, symbol,
                            notional, entry_price, exit_reason) -> None:
    try:
        qty = compute_qty_from_notional(notional, entry_price)
        order = place_close_order(symbol, qty, "equity")
        filled = poll_order_until_filled(order["id"])
        fill_price = float(filled.get("filled_avg_price") or current_price)
        slippage = abs(fill_price - current_price) / current_price if current_price > 0 else 0.0
        close_position(conn, position_id, fill_price, slippage, exit_reason)
        log_system_event(conn, "INFO", "execution_engine",
                         f"Closed equity {symbol} reason={exit_reason} fill={fill_price:.4f}",
                         {"position_id": position_id, "exit_reason": exit_reason,
                          "fill_price": fill_price})
        logger.info("Closed equity %s at %.4f reason=%s", symbol, fill_price, exit_reason)
    except Exception as exc:
        logger.error("Failed to close equity position %s: %s", position_id, exc)
        log_system_event(conn, "ERROR", "execution_engine",
                         f"Equity close failed for {symbol}: {exc}",
                         {"position_id": position_id})


# ---------------------------------------------------------------------------
# FORCE_CLOSE / CLOSE_REQUEST processing
# ---------------------------------------------------------------------------

def process_close_ticket(conn, ticket: dict) -> None:
    ticket_id = str(ticket["ticket_id"])
    payload = ticket.get("payload") or {}
    symbol = payload.get("option_symbol") or payload.get("symbol", "")
    position_id = payload.get("position_id")
    exit_reason = payload.get("exit_reason", "hard_close")
    asset_type = payload.get("asset_type", "option")
    qty = int(payload.get("qty", 1))

    try:
        order = place_close_order(symbol, qty, asset_type)
        filled = poll_order_until_filled(order["id"])
        fill_price = float(filled.get("filled_avg_price") or 0)
        fill_status = filled.get("status", "")

        if fill_status != "filled" or fill_price <= 0:
            insert_decision(conn, ticket_id, "FAILED",
                            f"Close order not filled — status={fill_status}")
            return

        slippage = 0.0
        if position_id:
            # Fetch position for pnl computation
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE position_id = %s",
                            (position_id,))
                pos = cur.fetchone()
            if pos:
                pos = dict(pos)
                close_position(conn, position_id, fill_price, slippage, exit_reason)
                if asset_type == "option":
                    write_trade_outcome(conn, pos, fill_price, exit_reason,
                                        payload.get("strategy_id"))

        insert_decision(conn, ticket_id, "EXECUTED",
                        f"Closed {symbol} at {fill_price:.4f} reason={exit_reason}")
        log_system_event(conn, "INFO", "execution_engine",
                         f"Force close executed: {symbol} at {fill_price:.4f}",
                         {"ticket_id": ticket_id, "exit_reason": exit_reason,
                          "position_id": position_id})
    except Exception as exc:
        reason = f"Force close failed: {exc}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})


# ---------------------------------------------------------------------------
# Trade ticket processing
# ---------------------------------------------------------------------------

def process_ticket(conn, ticket: dict, directives: dict) -> None:
    ticket_id = str(ticket["ticket_id"])
    payload = ticket.get("payload") or {}

    missing = REQUIRED_FIELDS - set(payload.keys())
    if missing:
        reason = f"Missing required fields: {sorted(missing)}"
        logger.warning("Ticket %s rejected: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "REJECTED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    if not payload.get("approved", False):
        reason = payload.get("reasoning", "approved=False in payload")
        insert_decision(conn, ticket_id, "REJECTED", reason)
        return

    if directives.get("global_kill_switch", False):
        reason = "Global kill switch active — no new positions"
        insert_decision(conn, ticket_id, "REJECTED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    # Directional cap check
    direction = payload.get("direction", "long")
    sized_notional = float(payload.get("sized_notional", 0))
    cap_exceeded, total_exposure, cap = check_directional_cap(conn, direction, sized_notional)
    if cap_exceeded:
        reason = (f"Directional cap exceeded: {direction} exposure {total_exposure:.0f} "
                  f"> {cap:.0f} (60% of equity)")
        logger.warning("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "REJECTED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason,
                         {"ticket_id": ticket_id, "type": "directional_cap_reject",
                          "total_exposure": total_exposure, "cap": cap})
        return

    asset_type = payload["asset_type"]
    symbol = payload["symbol"]
    expected_price = None

    try:
        if asset_type == "equity":
            order = place_equity_order(payload)
            expected_price = get_current_price(symbol)
        elif asset_type == "option":
            order = place_options_order(payload)
        else:
            reason = f"Unknown asset_type: {asset_type}"
            insert_decision(conn, ticket_id, "FAILED", reason)
            return
    except Exception as exc:
        reason = f"Order placement failed: {exc}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    alpaca_order_id = order["id"]

    try:
        filled_order = poll_order_until_filled(alpaca_order_id)
    except Exception as exc:
        reason = f"Order poll failed: {exc}"
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    fill_status = filled_order.get("status", "")
    fill_price_str = filled_order.get("filled_avg_price")
    fill_price = float(fill_price_str) if fill_price_str else None

    if fill_status != "filled" or fill_price is None:
        reason = f"Order did not fill — final status={fill_status}"
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason,
                         {"ticket_id": ticket_id, "alpaca_order_id": alpaca_order_id})
        return

    slippage = (abs(fill_price - expected_price) / expected_price
                if expected_price and expected_price > 0 else 0.0)

    delta_exposure = 1.0 if direction == "long" else -1.0
    if asset_type == "option":
        delta_exposure = payload.get("delta_exposure", 0.5 * delta_exposure)

    ledger_data = {
        "bot_source": payload["bot_source"],
        "asset": payload.get("option_symbol", symbol) if asset_type == "option" else symbol,
        "asset_type": asset_type,
        "direction": direction,
        "delta_exposure": delta_exposure,
        "notional_risk": sized_notional,
        "entry_price": fill_price,
        "entry_time": datetime.now(timezone.utc),
        "alpaca_order_id": alpaca_order_id,
    }

    try:
        position_id = insert_position(conn, ledger_data)
    except Exception as exc:
        reason = f"Ledger insert failed: {exc}"
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    reasoning = (f"Filled at {fill_price:.4f}, slippage={slippage:.4f}, "
                 f"position_id={position_id}, alpaca_order_id={alpaca_order_id}")
    insert_decision(conn, ticket_id, "EXECUTED", reasoning)
    log_system_event(conn, "INFO", "execution_engine",
                     f"Executed {symbol} ({asset_type}) — {direction}",
                     {"ticket_id": ticket_id, "position_id": position_id,
                      "fill_price": fill_price, "slippage": slippage,
                      "strategy_id": payload.get("strategy_id")})
    logger.info("Ticket %s EXECUTED: position_id=%s fill=%.4f slippage=%.4f",
                ticket_id, position_id, fill_price, slippage)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Execution Engine starting")

    conn = get_db()
    try:
        # 1. Heartbeat — always upsert, even if halted
        upsert_heartbeat(conn)

        # 2. no_trade_mode check
        halted, halt_reason = check_no_trade_mode(conn)
        if halted:
            logger.warning("no_trade_mode is SET: %s — engine exiting without trading", halt_reason)
            log_system_event(conn, "WARNING", "execution_engine",
                             f"no_trade_mode halted engine: {halt_reason}")
            return

        try:
            directives = load_master_directives()
        except Exception as exc:
            logger.error("Cannot load master-directives.json: %s", exc)
            sys.exit(1)

        # 3. Position monitor (equity multi-day exits)
        try:
            run_equity_position_monitor(conn)
        except Exception as exc:
            logger.error("Position monitor error: %s", exc)
            log_system_event(conn, "ERROR", "execution_engine",
                             f"Position monitor failed: {exc}")

        # 4. Process FORCE_CLOSE / CLOSE_REQUEST tickets
        close_tickets = fetch_force_close_tickets(conn)
        if close_tickets:
            logger.info("Found %d force-close tickets", len(close_tickets))
            for ticket in close_tickets:
                try:
                    process_close_ticket(conn, ticket)
                except Exception as exc:
                    logger.error("Unhandled error in close ticket %s: %s",
                                 ticket.get("ticket_id"), exc)

        # 5. Process new TRADE_REQUEST tickets
        pending = fetch_pending_tickets(conn)
        logger.info("Found %d pending TRADE_REQUEST tickets", len(pending))

        if not pending:
            logger.info("No pending tickets — exiting")
            return

        for ticket in pending:
            try:
                process_ticket(conn, ticket, directives)
            except Exception as exc:
                ticket_id = str(ticket.get("ticket_id", "unknown"))
                logger.error("Unhandled error on ticket %s: %s", ticket_id, exc)
                try:
                    log_system_event(conn, "ERROR", "execution_engine",
                                     f"Unhandled error on ticket {ticket_id}: {exc}",
                                     {"ticket_id": ticket_id})
                except Exception:
                    pass

    finally:
        conn.close()

    logger.info("Execution Engine run complete")


if __name__ == "__main__":
    main()
