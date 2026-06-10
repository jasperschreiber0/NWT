"""
execution/engine.py
Execution Engine — lifecycle service.
Zero opinion on whether to trade. Executes only what the Portfolio Brain has approved.

Reads approved TRADE_REQUEST tickets from nwt_tickets and processes them:
  - Validates payload
  - Checks master kill switch
  - Places orders via Alpaca REST API
  - Polls for fill
  - Writes to nwt_portfolio_ledger
  - Records decision in nwt_ticket_decisions

Run continuously or on a short polling interval (e.g. every 30s via cron).
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import requests
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

from ledger import close_position, get_open_positions, insert_position, log_system_event

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

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
    "approved",
    "bot_source",
    "symbol",
    "direction",
    "strategy_id",
    "sized_notional",
    "asset_type",
    "time_in_force",
}

POLL_INTERVAL = 3   # seconds between order status polls
POLL_MAX = 10       # max polls before giving up

# Synchronous risk backstop — mirrors risk_agent rules. The risk agent's
# 5-minute sweep is authoritative, but its APPROVED decision can be minutes
# stale by the time an order is placed; these flags are re-checked here, in
# the order path, so no order reaches Alpaca after the state has turned.
NEW_ENTRY_CUTOFF_UTC_HOUR = 19    # no new entries after 19:30 UTC (15:30 EDT)
NEW_ENTRY_CUTOFF_UTC_MINUTE = 30
TRACK_COOLOFF_HOURS = 24


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_db() -> psycopg2.extensions.connection:
    return psycopg2.connect(NWT_DB_DSN)


def load_master_directives() -> dict:
    path = SHARED_DIR / "master-directives.json"
    with open(path) as f:
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


def alpaca_delete(path: str) -> dict:
    url = f"{ALPACA_BASE_URL}/v2{path}"
    resp = requests.delete(url, headers=ALPACA_HEADERS, timeout=15)
    if not resp.ok:
        logger.error("Alpaca DELETE %s → %d: %s", path, resp.status_code, resp.text[:500])
    resp.raise_for_status()
    return resp.json()


def get_current_price(symbol: str) -> float:
    """Fetch latest trade price from Alpaca data API."""
    url = f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/trades/latest"
    resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return float(data["trade"]["p"])


def get_latest_quote(symbol: str, asset_type: str) -> tuple:
    """
    Latest NBBO (bid, ask) for an option or stock symbol.
    Returns (None, None) on failure — quote capture must never block execution;
    the learning agent falls back to a conservative default spread.
    """
    try:
        if asset_type == "option":
            url = f"{ALPACA_DATA_URL}/v1beta1/options/quotes/latest"
            resp = requests.get(url, headers=ALPACA_HEADERS, params={"symbols": symbol}, timeout=15)
            resp.raise_for_status()
            q = (resp.json().get("quotes") or {}).get(symbol) or {}
        else:
            url = f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/quotes/latest"
            resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
            resp.raise_for_status()
            q = resp.json().get("quote") or {}
        bid = float(q.get("bp") or 0)
        ask = float(q.get("ap") or 0)
        return (bid if bid > 0 else None, ask if ask > 0 else None)
    except Exception as exc:
        logger.warning("Quote fetch failed for %s (%s): %s", symbol, asset_type, exc)
        return None, None


def get_disabled_tracks(conn) -> set:
    """Tracks placed in cooling-off by the risk agent within the last 24h."""
    disabled = set()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT payload FROM nwt_system_log
            WHERE component = 'risk_agent'
              AND message LIKE 'TRACK_DISABLED%'
              AND created_at > NOW() - INTERVAL '{TRACK_COOLOFF_HOURS} hours'
            """
        )
        rows = cur.fetchall()
    for (payload,) in rows:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                continue
        if isinstance(payload, dict) and payload.get("track"):
            disabled.add(payload["track"])
    return disabled


def synchronous_risk_veto(conn, payload: dict) -> tuple:
    """
    Final gate before an order for a NEW position reaches Alpaca.
    Re-reads master-directives.json fresh and re-checks cooling-off and the
    new-entry cutoff. Returns (vetoed: bool, reason: str).
    Closes (FORCE_CLOSE) bypass this — liquidation is always allowed.
    """
    try:
        directives = load_master_directives()
    except Exception:
        return True, "Synchronous veto: master-directives.json unreadable — NO-TRADE MODE"

    if directives.get("global_kill_switch", False):
        return True, "Synchronous veto: global kill switch active"

    bot_source = payload.get("bot_source", "")
    if bot_source.startswith("NWT_TRACK_"):
        track = bot_source.replace("NWT_TRACK_", "")
        if track in get_disabled_tracks(conn):
            return True, f"Synchronous veto: track {track} in cooling-off period"

    now_utc = datetime.now(timezone.utc)
    if payload.get("asset_type") == "option" and (
        now_utc.hour > NEW_ENTRY_CUTOFF_UTC_HOUR
        or (now_utc.hour == NEW_ENTRY_CUTOFF_UTC_HOUR and now_utc.minute >= NEW_ENTRY_CUTOFF_UTC_MINUTE)
    ):
        return True, (
            f"Synchronous veto: past new-entry cutoff "
            f"{NEW_ENTRY_CUTOFF_UTC_HOUR}:{NEW_ENTRY_CUTOFF_UTC_MINUTE:02d} UTC"
        )

    return False, ""


def poll_order_until_filled(order_id: str) -> dict:
    """
    Poll GET /v2/orders/{order_id} until status is 'filled' or we exhaust attempts.
    Returns the final order object.
    """
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
    # Return whatever we have
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
    """
    Return TRADE_REQUEST and FORCE_CLOSE tickets with no decision from
    EXECUTION_ENGINE yet. FORCE_CLOSE comes only from the RISK_AGENT.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT t.*
            FROM nwt_tickets t
            WHERE t.to_agent = 'EXECUTION_ENGINE'
              AND (
                  (t.type = 'TRADE_REQUEST' AND t.from_agent IN (
                      'EU_EXECUTOR', 'ASX_EXECUTOR', 'CHINA_EXECUTOR', 'NWT_EXECUTION_AGENT'
                  ))
                  OR (t.type = 'FORCE_CLOSE' AND t.from_agent = 'RISK_AGENT')
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


def get_ledger_position(conn, position_id: str):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM nwt_portfolio_ledger WHERE position_id = %s",
            (position_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Order placement helpers
# ---------------------------------------------------------------------------

def compute_qty_from_notional(sized_notional: float, price: float) -> int:
    """Compute whole-share quantity from notional."""
    qty = int(sized_notional / price)
    return max(qty, 1)


def place_equity_order(payload: dict) -> dict:
    """Place a market equity order. Returns Alpaca order object."""
    symbol = payload["symbol"]
    sized_notional = float(payload["sized_notional"])
    time_in_force = payload["time_in_force"]
    direction = payload["direction"]  # 'long' or 'short'

    # Get current price to compute qty
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
    """Place a simple market options order. Returns Alpaca order object."""
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
    }
    logger.info("Placing options order: %s %s x%d", side, option_symbol, qty)
    return alpaca_post("/orders", order_body)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_ticket(conn, ticket: dict, directives: dict) -> None:
    ticket_id = str(ticket["ticket_id"])
    payload = ticket.get("payload") or {}

    # FORCE_CLOSE tickets follow the liquidation path — never gated by
    # new-entry vetoes (the risk agent must always be able to flatten).
    if ticket.get("type") == "FORCE_CLOSE":
        process_force_close(conn, ticket)
        return

    # 1. Validate required fields
    missing = REQUIRED_FIELDS - set(payload.keys())
    if missing:
        reason = f"Missing required fields: {sorted(missing)}"
        logger.warning("Ticket %s rejected: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "REJECTED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    # 2. Check approved flag
    if not payload.get("approved", False):
        reason = payload.get("reasoning", "approved=False in payload")
        logger.info("Ticket %s rejected (not approved): %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "REJECTED", reason)
        return

    # 3. Synchronous risk gate — fresh kill switch / cooling-off / cutoff
    # check at order time (the directives loaded at run start may be stale)
    vetoed, veto_reason = synchronous_risk_veto(conn, payload)
    if vetoed:
        logger.warning("Ticket %s rejected: %s", ticket_id, veto_reason)
        insert_decision(conn, ticket_id, "REJECTED", veto_reason)
        log_system_event(conn, "WARNING", "execution_engine", veto_reason, {"ticket_id": ticket_id})
        return

    # 4. Place order
    asset_type = payload["asset_type"]
    symbol = payload["symbol"]
    expected_price = None
    entry_bid = entry_ask = None

    try:
        if asset_type == "equity":
            entry_bid, entry_ask = get_latest_quote(symbol, "equity")
            order = place_equity_order(payload)
            expected_price = get_current_price(symbol)
        elif asset_type == "option":
            option_symbol = payload.get("option_symbol", symbol)
            # NBBO before the order: mid = expected price, spread feeds pnl_adjusted
            entry_bid, entry_ask = get_latest_quote(option_symbol, "option")
            if entry_bid and entry_ask:
                expected_price = (entry_bid + entry_ask) / 2.0
            order = place_options_order(payload)
        else:
            reason = f"Unknown asset_type: {asset_type}"
            logger.error("Ticket %s: %s", ticket_id, reason)
            insert_decision(conn, ticket_id, "FAILED", reason)
            return
    except Exception as exc:
        reason = f"Order placement failed: {exc}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    alpaca_order_id = order["id"]
    logger.info("Ticket %s: order placed, alpaca_order_id=%s", ticket_id, alpaca_order_id)

    # 5. Poll for fill
    try:
        filled_order = poll_order_until_filled(alpaca_order_id)
    except Exception as exc:
        reason = f"Order poll failed: {exc}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    fill_status = filled_order.get("status", "")
    fill_price_str = filled_order.get("filled_avg_price")
    fill_price = float(fill_price_str) if fill_price_str else None

    if fill_status != "filled" or fill_price is None:
        reason = f"Order did not fill — final status={fill_status}"
        logger.warning("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason, {
            "ticket_id": ticket_id,
            "alpaca_order_id": alpaca_order_id,
            "order_status": fill_status,
        })
        return

    # 6. Compute slippage
    if expected_price and expected_price > 0:
        slippage = abs(fill_price - expected_price) / expected_price
    else:
        slippage = 0.0

    # 7. Write to ledger
    delta_exposure = 1.0 if payload.get("direction") == "long" else -1.0
    if asset_type == "option":
        # Options have delta < 1; default to 0.5 approximation if not provided
        delta_exposure = payload.get("delta_exposure", 0.5 * delta_exposure)

    ledger_data = {
        "bot_source": payload["bot_source"],
        "strategy_id": payload.get("strategy_id"),
        "asset": payload.get("option_symbol", symbol) if asset_type == "option" else symbol,
        "asset_type": asset_type,
        "direction": payload["direction"],
        "delta_exposure": delta_exposure,
        "notional_risk": float(payload["sized_notional"]),
        "entry_price": fill_price,
        "entry_time": datetime.now(timezone.utc),
        "entry_bid": entry_bid,
        "entry_ask": entry_ask,
        "alpaca_order_id": alpaca_order_id,
    }

    try:
        position_id = insert_position(conn, ledger_data)
    except Exception as exc:
        reason = f"Ledger insert failed: {exc}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    # 8. Mark decision EXECUTED
    reasoning = (
        f"Filled {fill_status} at {fill_price:.4f}, slippage={slippage:.4f}, "
        f"position_id={position_id}, alpaca_order_id={alpaca_order_id}"
    )
    insert_decision(conn, ticket_id, "EXECUTED", reasoning)
    log_system_event(
        conn,
        "INFO",
        "execution_engine",
        f"Executed {symbol} ({asset_type}) — {payload['direction']}",
        {
            "ticket_id": ticket_id,
            "position_id": position_id,
            "alpaca_order_id": alpaca_order_id,
            "fill_price": fill_price,
            "slippage": slippage,
            "strategy_id": payload.get("strategy_id"),
        },
    )
    logger.info(
        "Ticket %s EXECUTED: position_id=%s fill=%.4f slippage=%.4f",
        ticket_id, position_id, fill_price, slippage,
    )


def process_force_close(conn, ticket: dict) -> None:
    """
    Liquidate a ledger position on RISK_AGENT instruction.
    Uses Alpaca's close-position endpoint (whole position), polls the fill,
    and closes the ledger row with exit price + exit NBBO.
    """
    ticket_id = str(ticket["ticket_id"])
    payload = ticket.get("payload") or {}
    position_id = payload.get("position_id", "")
    asset = payload.get("symbol") or payload.get("option_symbol", "")

    position = get_ledger_position(conn, position_id) if position_id else None
    if position is None:
        reason = f"FORCE_CLOSE: ledger position {position_id} not found"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    if position.get("status") == "closed":
        reason = f"FORCE_CLOSE: position {position_id} already closed"
        logger.info("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "SKIPPED", reason)
        return

    asset = position.get("asset") or asset
    asset_type = position.get("asset_type", "option")

    # NBBO before liquidation: mid = expected price, spread feeds pnl_adjusted
    exit_bid, exit_ask = get_latest_quote(asset, asset_type)
    expected_price = (exit_bid + exit_ask) / 2.0 if (exit_bid and exit_ask) else None

    try:
        order = alpaca_delete(f"/positions/{asset}")
    except Exception as exc:
        reason = f"FORCE_CLOSE: liquidation order failed for {asset}: {exc}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    alpaca_order_id = order.get("id", "")
    try:
        filled_order = poll_order_until_filled(alpaca_order_id) if alpaca_order_id else order
    except Exception as exc:
        reason = f"FORCE_CLOSE: order poll failed: {exc}"
        logger.error("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "ERROR", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    fill_price_str = filled_order.get("filled_avg_price")
    fill_price = float(fill_price_str) if fill_price_str else None
    if fill_price is None:
        reason = f"FORCE_CLOSE: no fill price — status={filled_order.get('status')}"
        logger.warning("Ticket %s: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "FAILED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason, {
            "ticket_id": ticket_id, "alpaca_order_id": alpaca_order_id,
        })
        return

    if expected_price and expected_price > 0:
        slippage = abs(fill_price - expected_price) / expected_price
    else:
        slippage = 0.0

    close_position(conn, position_id, fill_price, slippage, exit_bid=exit_bid, exit_ask=exit_ask)
    reasoning = (
        f"FORCE_CLOSE filled at {fill_price:.4f}, slippage={slippage:.4f}, "
        f"position_id={position_id}, alpaca_order_id={alpaca_order_id}"
    )
    insert_decision(conn, ticket_id, "EXECUTED", reasoning)
    log_system_event(
        conn,
        "INFO",
        "execution_engine",
        f"Force-closed {asset} ({asset_type})",
        {
            "ticket_id": ticket_id,
            "position_id": position_id,
            "alpaca_order_id": alpaca_order_id,
            "fill_price": fill_price,
            "slippage": slippage,
        },
    )
    logger.info("Ticket %s FORCE_CLOSE EXECUTED: position=%s fill=%.4f", ticket_id, position_id, fill_price)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Execution Engine starting")

    try:
        directives = load_master_directives()
    except Exception as exc:
        logger.error("Cannot load master-directives.json: %s", exc)
        sys.exit(1)

    conn = get_db()
    try:
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
                logger.error("Unhandled error processing ticket %s: %s", ticket_id, exc)
                try:
                    log_system_event(
                        conn, "ERROR", "execution_engine",
                        f"Unhandled error on ticket {ticket_id}: {exc}",
                        {"ticket_id": ticket_id},
                    )
                except Exception:
                    pass

    finally:
        conn.close()

    logger.info("Execution Engine run complete")


if __name__ == "__main__":
    main()
