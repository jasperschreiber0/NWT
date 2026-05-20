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
    resp.raise_for_status()
    return resp.json()


def get_current_price(symbol: str) -> float:
    """Fetch latest trade price from Alpaca data API."""
    url = f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/trades/latest"
    resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return float(data["trade"]["p"])


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
    Return TRADE_REQUEST tickets with no decision from EXECUTION_ENGINE yet.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT t.*
            FROM nwt_tickets t
            WHERE t.to_agent = 'EXECUTION_ENGINE'
              AND t.type = 'TRADE_REQUEST'
              AND t.from_agent IN (
                  'EU_EXECUTOR', 'ASX_EXECUTOR', 'CHINA_EXECUTOR', 'NWT_EXECUTION_AGENT'
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
        "order_class": "simple",
    }
    logger.info("Placing options order: %s %s x%d", side, option_symbol, qty)
    return alpaca_post("/orders", order_body)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_ticket(conn, ticket: dict, directives: dict) -> None:
    ticket_id = str(ticket["ticket_id"])
    payload = ticket.get("payload") or {}

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

    # 3. Check global kill switch
    if directives.get("global_kill_switch", False):
        reason = "Global kill switch active — no new positions"
        logger.warning("Ticket %s rejected: %s", ticket_id, reason)
        insert_decision(conn, ticket_id, "REJECTED", reason)
        log_system_event(conn, "WARNING", "execution_engine", reason, {"ticket_id": ticket_id})
        return

    # 4. Place order
    asset_type = payload["asset_type"]
    symbol = payload["symbol"]
    expected_price = None

    try:
        if asset_type == "equity":
            order = place_equity_order(payload)
            expected_price = get_current_price(symbol)
        elif asset_type == "option":
            order = place_options_order(payload)
            # For options, expected price isn't easily fetchable pre-fill
            expected_price = None
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
        "asset": payload.get("option_symbol", symbol) if asset_type == "option" else symbol,
        "asset_type": asset_type,
        "direction": payload["direction"],
        "delta_exposure": delta_exposure,
        "notional_risk": float(payload["sized_notional"]),
        "entry_price": fill_price,
        "entry_time": datetime.now(timezone.utc),
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
