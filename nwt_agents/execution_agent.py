"""
nwt_agents/execution_agent.py
Fires every 5 minutes 13:00-21:00 UTC.
Picks up RISK_AGENT-APPROVED tickets and submits to Execution Engine.

For each approved proposal:
  1. Resolve the specific option contract via Alpaca options chain
  2. Build final execution payload
  3. INSERT into nwt_tickets (to_agent=EXECUTION_ENGINE, type=TRADE_REQUEST)
  4. Mark original ticket as SUBMITTED
"""

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import (
    check_no_trade_mode,
    get_db,
    insert_decision,
    insert_ticket,
    load_master_directives,
    log_system_event,
    pre_trade_veto,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("execution_agent")

ALPACA_BASE_URL = os.environ.get("NWT_ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
ALPACA_DATA_URL = os.environ.get("NWT_ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("NWT_ALPACA_KEY_ID", ""),
    "APCA-API-SECRET-KEY": os.environ.get("NWT_ALPACA_SECRET_KEY", ""),
}

ACCOUNT_SIZE = 97_000.0
ET_TZ = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_approved_proposals(conn) -> list:
    """
    Return TRADE_PROPOSAL tickets approved by RISK_AGENT that have NOT yet been
    submitted by NWT_EXECUTION_AGENT.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT t.*
            FROM nwt_tickets t
            INNER JOIN nwt_ticket_decisions d ON d.ticket_id = t.ticket_id
            WHERE t.to_agent = 'RISK_AGENT'
              AND t.type = 'TRADE_PROPOSAL'
              AND d.decision = 'APPROVED'
              AND d.decided_by = 'RISK_AGENT'
              AND NOT EXISTS (
                  SELECT 1 FROM nwt_ticket_decisions d2
                  WHERE d2.ticket_id = t.ticket_id
                    AND d2.decided_by = 'NWT_EXECUTION_AGENT'
              )
            ORDER BY t.created_at ASC
            """
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def resolve_option_contract(
    symbol: str,
    direction: str,
    strategy_type: str,
    dte_min: int,
    dte_max: int,
    strike_preference: str,
) -> dict | None:
    """
    Query Alpaca options chain to find the best contract.
    Returns dict with option_symbol, strike_price, expiration_date, type, or None.
    """
    today = date.today()
    exp_min = (today + timedelta(days=dte_min)).isoformat()
    exp_max = (today + timedelta(days=dte_max)).isoformat()

    # Determine option type based on strategy and direction
    if strategy_type in ("long_call", "bull_call_spread"):
        option_type = "call"
    elif strategy_type in ("long_put", "bear_put_spread"):
        option_type = "put"
    elif strategy_type == "iron_condor":
        # For iron condor, we start with the call side; full spread handled separately
        option_type = "call"
    elif strategy_type == "vix_calls":
        option_type = "call"
        symbol = "VIXY"  # Use VIXY as VIX proxy on Alpaca
    else:
        # Default: match direction to option type
        option_type = "call" if direction == "long" else "put"

    url = f"{ALPACA_BASE_URL}/v2/options/contracts"
    params = {
        "underlying_symbols": symbol,
        "expiration_date_gte": exp_min,
        "expiration_date_lte": exp_max,
        "type": option_type,
        "limit": 100,
    }

    try:
        resp = requests.get(url, headers=ALPACA_HEADERS, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        contracts = data.get("option_contracts", []) if isinstance(data, dict) else data

        if not contracts:
            logger.warning("No option contracts found for %s %s DTE=%d-%d", symbol, option_type, dte_min, dte_max)
            return None

        # Get current stock price to find ATM/OTM strike
        stock_url = f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/trades/latest"
        try:
            price_resp = requests.get(stock_url, headers=ALPACA_HEADERS, timeout=15)
            price_resp.raise_for_status()
            current_price = float(price_resp.json()["trade"]["p"])
        except Exception:
            # Fallback: use first contract's strike
            current_price = float(contracts[0].get("strike_price", 0))

        # Find ATM or 1-OTM contract
        if strike_preference == "ATM":
            # Closest strike to current price
            best = min(
                contracts,
                key=lambda c: abs(float(c.get("strike_price", 0)) - current_price),
            )
        else:  # 1_OTM
            if option_type == "call":
                # 1 OTM call = first strike ABOVE current price
                otm_contracts = [c for c in contracts if float(c.get("strike_price", 0)) > current_price]
                if otm_contracts:
                    best = min(otm_contracts, key=lambda c: float(c.get("strike_price", 0)))
                else:
                    best = min(contracts, key=lambda c: abs(float(c.get("strike_price", 0)) - current_price))
            else:
                # 1 OTM put = first strike BELOW current price
                otm_contracts = [c for c in contracts if float(c.get("strike_price", 0)) < current_price]
                if otm_contracts:
                    best = max(otm_contracts, key=lambda c: float(c.get("strike_price", 0)))
                else:
                    best = min(contracts, key=lambda c: abs(float(c.get("strike_price", 0)) - current_price))

        return {
            "option_symbol": best.get("symbol") or best.get("id"),
            "strike_price": float(best.get("strike_price", 0)),
            "expiration_date": best.get("expiration_date"),
            "option_type": option_type,
        }

    except Exception as exc:
        logger.error("Failed to resolve option contract for %s: %s", symbol, exc)
        return None


def compute_qty_from_notional(sized_notional: float, option_price: float) -> int:
    """Compute number of option contracts from notional. Each contract = 100 shares."""
    if option_price <= 0:
        return 1
    contract_cost = option_price * 100
    qty = max(int(sized_notional / contract_cost), 1)
    return qty


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _hard_close_utc() -> datetime:
    """15:45 ET in UTC, DST-aware."""
    et_today = datetime.now(ET_TZ).date()
    hc = datetime(et_today.year, et_today.month, et_today.day, 15, 45, tzinfo=ET_TZ)
    return hc.astimezone(timezone.utc)


def _get_option_price(option_symbol: str) -> float | None:
    """Fetch mark price for an options contract from Alpaca."""
    try:
        url = f"{ALPACA_BASE_URL}/v2/options/contracts/{option_symbol}"
        resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # Try mark_price, then last_price
        price = data.get("mark_price") or data.get("last_price") or data.get("close_price")
        return float(price) if price else None
    except Exception:
        return None


def monitor_options_positions(conn) -> None:
    """
    Check all open options positions:
    - 50% profit target → submit CLOSE_REQUEST
    - 50% stop loss → submit CLOSE_REQUEST
    - Past 15:45 ET hard close → submit CLOSE_REQUEST
    Deduplicates: skips positions that already have a pending CLOSE_REQUEST.
    """
    now_utc = datetime.now(timezone.utc)
    hard_close = _hard_close_utc()
    past_hard_close = now_utc >= hard_close

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM nwt_portfolio_ledger WHERE status='open' AND asset_type='option'"
        )
        positions = [dict(r) for r in cur.fetchall()]

    if not positions:
        return

    for pos in positions:
        position_id = str(pos["position_id"])
        symbol = pos.get("asset", "")
        entry_price = float(pos.get("entry_price") or 0)
        notional = float(pos.get("notional_risk") or 0)
        qty = max(int(round(notional / (entry_price * 100))) if entry_price > 0 else 1, 1)

        # Check if a close ticket already exists for this position
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM nwt_tickets
                WHERE type IN ('CLOSE_REQUEST', 'FORCE_CLOSE')
                  AND payload->>'position_id' = %s
                  AND created_at > NOW() - INTERVAL '2 hours'
                """,
                (position_id,),
            )
            if cur.fetchone()[0] > 0:
                continue  # Already have a pending close

        exit_reason = None

        if past_hard_close:
            exit_reason = "hard_close"
        elif entry_price > 0:
            current_price = _get_option_price(symbol)
            if current_price is not None:
                pnl_pct = (current_price - entry_price) / entry_price
                if pos.get("direction") == "short":
                    pnl_pct = -pnl_pct
                if pnl_pct >= 0.50:
                    exit_reason = "target"
                elif pnl_pct <= -0.50:
                    exit_reason = "stop"

        if exit_reason:
            try:
                insert_ticket(
                    conn,
                    from_agent="NWT_EXECUTION_AGENT",
                    to_agent="EXECUTION_ENGINE",
                    type_="CLOSE_REQUEST",
                    payload={
                        "approved": True,
                        "bot_source": pos.get("bot_source", "NWT_EXECUTION_AGENT"),
                        "symbol": symbol,
                        "option_symbol": symbol,
                        "direction": "close",
                        "strategy_id": pos.get("bot_source", "CLOSE"),
                        "sized_notional": notional,
                        "qty": qty,
                        "asset_type": "option",
                        "time_in_force": "day",
                        "exit_reason": exit_reason,
                        "position_id": position_id,
                    },
                )
                logger.info("Close request for %s position_id=%s reason=%s",
                            symbol, position_id, exit_reason)
            except Exception as exc:
                logger.error("Failed to insert close request for %s: %s", position_id, exc)


def main() -> None:
    conn = get_db()

    try:
        # no_trade_mode check
        halted, halt_reason = check_no_trade_mode(conn)
        if halted:
            logger.warning("no_trade_mode SET — execution agent exiting: %s", halt_reason)
            log_system_event(conn, "WARNING", "execution_agent",
                             f"no_trade_mode halted execution agent: {halt_reason}")
            return

        try:
            directives = load_master_directives()
        except FileNotFoundError:
            logger.error("master-directives.json not found — exiting")
            sys.exit(1)

        if directives.get("global_kill_switch", False):
            logger.warning("Global kill switch active — execution agent exiting")
            log_system_event(conn, "WARNING", "execution_agent", "Kill switch active — no submissions")
            return

        # Options position monitor (50% profit/stop/hard close)
        try:
            monitor_options_positions(conn)
        except Exception as exc:
            logger.error("Options monitor error: %s", exc)
            log_system_event(conn, "ERROR", "execution_agent",
                             f"Options monitor failed: {exc}")

        approved_proposals = fetch_approved_proposals(conn)
        logger.info("Found %d approved proposals to submit", len(approved_proposals))

        if not approved_proposals:
            return

        submitted_count = 0
        failed_count = 0

        for ticket in approved_proposals:
            ticket_id = str(ticket["ticket_id"])
            payload = ticket.get("payload") or {}

            symbol = payload.get("symbol", "")
            strategy_type = payload.get("strategy_type", "long_call")
            direction = payload.get("direction", "long")
            strategy_id = payload.get("strategy_id", "")
            sized_notional = float(payload.get("sized_notional", 0))
            dte_min = int(payload.get("dte_min", 7))
            dte_max = int(payload.get("dte_max", 21))
            strike_preference = payload.get("strike_preference", "ATM")
            stop_pct = -abs(float(payload.get("stop_loss_pct", 0.50)))
            target_pct = float(payload.get("profit_target_pct", 0.50))
            regime = payload.get("regime_at_decision", {})

            # Determine bot_source from track
            from_track = payload.get("from_track", "")
            bot_source_map = {"C": "NWT_TRACK_C", "D": "NWT_TRACK_D", "E": "NWT_TRACK_E"}
            bot_source = bot_source_map.get(from_track, f"NWT_TRACK_{from_track}")

            # Synchronous risk gate — the risk agent's APPROVED decision may be
            # minutes old; re-check kill switch / cooling-off / entry cutoff NOW,
            # before this proposal becomes an order.
            vetoed, veto_reason = pre_trade_veto(conn, from_track)
            if vetoed:
                logger.warning("Ticket %s vetoed at submission: %s", ticket_id, veto_reason)
                insert_decision(conn, ticket_id, "VETOED", veto_reason, "NWT_EXECUTION_AGENT")
                log_system_event(conn, "WARNING", "execution_agent", veto_reason, {"ticket_id": ticket_id})
                failed_count += 1
                continue

            # Resolve specific option contract
            contract = resolve_option_contract(
                symbol=symbol,
                direction=direction,
                strategy_type=strategy_type,
                dte_min=dte_min,
                dte_max=dte_max,
                strike_preference=strike_preference,
            )

            if contract is None:
                reason = f"Could not resolve option contract for {symbol} {strategy_type}"
                logger.error("Ticket %s: %s", ticket_id, reason)
                insert_decision(conn, ticket_id, "FAILED", reason, "NWT_EXECUTION_AGENT")
                log_system_event(conn, "ERROR", "execution_agent", reason, {"ticket_id": ticket_id})
                failed_count += 1
                continue

            option_symbol = contract["option_symbol"]
            option_price = _get_option_price(option_symbol)
            if option_price and option_price > 0:
                qty = compute_qty_from_notional(sized_notional, option_price)
            else:
                logger.warning(
                    "Ticket %s: could not fetch live price for %s — falling back to "
                    "conservative $200/contract estimate", ticket_id, option_symbol,
                )
                qty = max(int(sized_notional / 200), 1)

            # Build execution payload for the Execution Engine
            execution_payload = {
                "approved": True,
                "bot_source": bot_source,
                "symbol": symbol,
                "option_symbol": option_symbol,
                "direction": direction,
                "strategy_id": strategy_id,
                "archetype": payload.get("archetype", ""),
                "sized_notional": sized_notional,
                "qty": qty,
                "asset_type": "option",
                "time_in_force": "day",
                "stop_pct": stop_pct,
                "target_pct": target_pct,
                "strike_price": contract["strike_price"],
                "expiration_date": contract["expiration_date"],
                "option_type": contract["option_type"],
                "strategy_type": strategy_type,
                "regime_at_decision": regime,
                "source_proposal_ticket_id": ticket_id,
            }

            try:
                exec_ticket_id = insert_ticket(
                    conn,
                    from_agent="NWT_EXECUTION_AGENT",
                    to_agent="EXECUTION_ENGINE",
                    type_="TRADE_REQUEST",
                    payload=execution_payload,
                )
                logger.info(
                    "Submitted TRADE_REQUEST %s for %s (%s %s) — source proposal %s",
                    exec_ticket_id, symbol, option_symbol, strategy_type, ticket_id,
                )

                # Mark original proposal ticket as SUBMITTED
                insert_decision(
                    conn,
                    ticket_id,
                    "SUBMITTED",
                    f"Execution ticket {exec_ticket_id} submitted for {option_symbol}",
                    "NWT_EXECUTION_AGENT",
                )
                submitted_count += 1

            except Exception as exc:
                reason = f"Failed to submit execution ticket: {exc}"
                logger.error("Ticket %s: %s", ticket_id, reason)
                insert_decision(conn, ticket_id, "FAILED", reason, "NWT_EXECUTION_AGENT")
                log_system_event(conn, "ERROR", "execution_agent", reason, {"ticket_id": ticket_id})
                failed_count += 1

        log_system_event(
            conn,
            "INFO",
            "execution_agent",
            f"Execution agent run: {submitted_count} submitted, {failed_count} failed",
            {"submitted": submitted_count, "failed": failed_count},
        )
        logger.info("Execution agent done — %d submitted, %d failed", submitted_count, failed_count)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
