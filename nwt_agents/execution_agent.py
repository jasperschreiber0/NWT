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
    clean_alpaca_base_url,
    get_db,
    insert_decision,
    insert_ticket,
    load_master_directives,
    log_system_event,
    option_dte,
    pre_trade_veto,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("execution_agent")

ALPACA_BASE_URL = clean_alpaca_base_url(os.environ.get("NWT_ALPACA_BASE_URL", "https://paper-api.alpaca.markets"))
ALPACA_DATA_URL = clean_alpaca_base_url(os.environ.get("NWT_ALPACA_DATA_URL", "https://data.alpaca.markets"))
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
            SELECT t.*, d.sizing_multiplier
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
# Multi-leg (defined-risk) structures
#
# conviction_engine.py's LLM prompt constrains strategy_type to exactly:
# long_call, long_put, bull_call_spread, bear_put_spread, iron_condor,
# vix_calls — the same six matching CLAUDE.md's Options Strategy Rules
# table. There is no "short premium" strategy_type; the three spread types
# below are the only ones with a short leg, and each short leg is always
# paired with a long leg that bounds the risk.
# ---------------------------------------------------------------------------

SPREAD_STRATEGIES = {"bull_call_spread", "bear_put_spread", "iron_condor"}


def _fetch_chain(symbol: str, option_type: str, dte_min: int, dte_max: int) -> list:
    today = date.today()
    url = f"{ALPACA_BASE_URL}/v2/options/contracts"
    params = {
        "underlying_symbols": symbol,
        "expiration_date_gte": (today + timedelta(days=dte_min)).isoformat(),
        "expiration_date_lte": (today + timedelta(days=dte_max)).isoformat(),
        "type": option_type,
        "limit": 200,
    }
    resp = requests.get(url, headers=ALPACA_HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("option_contracts", []) if isinstance(data, dict) else data


def _get_spot(symbol: str) -> float | None:
    try:
        url = f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/trades/latest"
        resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
        resp.raise_for_status()
        return float(resp.json()["trade"]["p"])
    except Exception:
        return None


def _leg(contract: dict, side: str, option_type: str) -> dict:
    return {
        "option_symbol": contract.get("symbol") or contract.get("id"),
        "side": side,
        "option_type": option_type,
        "strike_price": float(contract.get("strike_price", 0)),
        "expiration_date": contract.get("expiration_date"),
    }


def resolve_spread_legs(symbol: str, strategy_type: str,
                        dte_min: int, dte_max: int) -> list | None:
    """
    Resolve the legs of a defined-risk structure. All legs share one expiry.
    Verticals: buy ATM, sell the next strike further OTM.
    Iron condor: sell first-OTM call+put, buy the next strike beyond each.
    Returns a list of leg dicts, or None if the structure cannot be built.
    """
    spot = _get_spot(symbol)
    if not spot or spot <= 0:
        logger.warning("No spot price for %s — cannot build %s", symbol, strategy_type)
        return None

    try:
        if strategy_type in ("bull_call_spread", "bear_put_spread"):
            option_type = "call" if strategy_type == "bull_call_spread" else "put"
            contracts = _fetch_chain(symbol, option_type, dte_min, dte_max)
            if not contracts:
                return None
            atm = min(contracts, key=lambda c: abs(float(c.get("strike_price", 0)) - spot))
            expiry = atm.get("expiration_date")
            same_exp = sorted(
                (c for c in contracts if c.get("expiration_date") == expiry),
                key=lambda c: float(c.get("strike_price", 0)),
            )
            atm_strike = float(atm.get("strike_price", 0))
            if option_type == "call":
                further = [c for c in same_exp if float(c["strike_price"]) > atm_strike]
                short = further[0] if further else None
            else:
                further = [c for c in same_exp if float(c["strike_price"]) < atm_strike]
                short = further[-1] if further else None
            if short is None:
                return None
            return [_leg(atm, "buy", option_type), _leg(short, "sell", option_type)]

        if strategy_type == "iron_condor":
            calls = _fetch_chain(symbol, "call", dte_min, dte_max)
            puts = _fetch_chain(symbol, "put", dte_min, dte_max)
            if not calls or not puts:
                return None
            atm_call = min(calls, key=lambda c: abs(float(c.get("strike_price", 0)) - spot))
            expiry = atm_call.get("expiration_date")
            exp_calls = sorted((c for c in calls if c.get("expiration_date") == expiry),
                               key=lambda c: float(c["strike_price"]))
            exp_puts = sorted((c for c in puts if c.get("expiration_date") == expiry),
                              key=lambda c: float(c["strike_price"]))
            calls_above = [c for c in exp_calls if float(c["strike_price"]) > spot]
            puts_below = [c for c in exp_puts if float(c["strike_price"]) < spot]
            if len(calls_above) < 2 or len(puts_below) < 2:
                return None
            short_call, long_call = calls_above[0], calls_above[1]
            short_put, long_put = puts_below[-1], puts_below[-2]
            return [
                _leg(short_call, "sell", "call"),
                _leg(long_call, "buy", "call"),
                _leg(short_put, "sell", "put"),
                _leg(long_put, "buy", "put"),
            ]

    except Exception as exc:
        logger.error("Failed to resolve %s legs for %s: %s", strategy_type, symbol, exc)
        return None

    return None


def size_spread_qty(legs: list, sized_notional: float) -> int:
    """
    Number of spreads from the net debit. Credit structures (net <= 0, e.g.
    iron condor) and unpriceable legs get 1 spread — never guess a multiple.
    """
    net = 0.0
    for leg in legs:
        price = _get_option_price(leg["option_symbol"])
        if not price or price <= 0:
            return 1
        net += price if leg["side"] == "buy" else -price
    if net <= 0:
        return 1
    return max(int(sized_notional / (net * 100)), 1)


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


def _position_qty(pos: dict) -> int:
    """
    The position's REAL contract count — the ledger qty column, which is the
    actual Alpaca fill. The notional/entry_price fallback exists only for
    legacy rows written before the qty column: deriving qty from notional on
    a row that has real qty under-counted the AAPL260717C00312500 close
    (sold 3 of 6 held contracts, the rest rode into expiry).
    """
    ledger_qty = pos.get("qty")
    if ledger_qty:
        return max(int(float(ledger_qty)), 1)
    entry_price = float(pos.get("entry_price") or 0)
    notional = float(pos.get("notional_risk") or 0)
    return max(int(round(notional / (entry_price * 100))) if entry_price > 0 else 1, 1)


def _has_pending_close(conn, position_id: str) -> bool:
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
        return cur.fetchone()[0] > 0


def _emit_close_request(conn, pos: dict, exit_reason: str) -> None:
    """
    direction is the position's OWN ledger direction (long/short) — the
    engine's process_close_ticket looks this up again from the ledger before
    choosing buy-vs-sell to close, but carrying it here too keeps the ticket
    payload self-describing.
    """
    position_id = str(pos["position_id"])
    symbol = pos.get("asset", "")
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
                "direction": pos.get("direction", "long"),
                "strategy_id": pos.get("strategy_id") or pos.get("bot_source", "CLOSE"),
                "sized_notional": float(pos.get("notional_risk") or 0),
                "qty": _position_qty(pos),
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


def _spread_exit_reason(legs: list, past_hard_close: bool) -> str | None:
    """
    Value the structure as a unit: V = sum(sign x price), long +, short -.
    PnL per spread = V_now - V_entry, scaled by |V_entry| (premium at risk) —
    target/stop at +/-50%, works for both debit and credit structures.

    DTE<=1 gates hard close exactly like single-leg positions: all legs
    share one expiry (resolve_spread_legs), so any leg's DTE represents the
    whole group. If DTE>1, hard-close is skipped and the price check below
    still applies — hard close only forces closed what expires imminently.
    Missing leg prices return None (no exit) rather than a guess.
    """
    dte = option_dte(legs[0].get("asset", "")) if legs else None
    if past_hard_close and dte is not None and dte <= 1:
        return "hard_close"

    v_entry = 0.0
    v_now = 0.0
    for leg in legs:
        entry_price = float(leg.get("entry_price") or 0)
        if entry_price <= 0:
            return None
        price = _get_option_price(leg.get("asset", ""))
        if price is None:
            return None
        sign = 1.0 if leg.get("direction") == "long" else -1.0
        v_entry += sign * entry_price
        v_now += sign * price

    premium_at_risk = abs(v_entry)
    if premium_at_risk <= 0:
        return None

    pnl_frac = (v_now - v_entry) / premium_at_risk
    if pnl_frac >= 0.50:
        return "target"
    if pnl_frac <= -0.50:
        return "stop"
    return None


def monitor_options_positions(conn) -> None:
    """
    Check all open options positions:
    - 50% profit target → submit CLOSE_REQUEST
    - 50% stop loss → submit CLOSE_REQUEST
    - Past 15:45 ET hard close AND DTE<=1 → submit CLOSE_REQUEST
    Legs sharing a spread_group_id are valued and closed as ONE unit (short
    legs first, so a partial failure never leaves a naked short outstanding).
    Deduplicates: skips positions that already have a pending CLOSE_REQUEST.

    Hard close only force-closes positions expiring today/tomorrow. Without
    the DTE<=1 guard, a 7-21 DTE spread opened this morning gets force-closed
    at 15:45 ET the same day, guaranteeing a loss regardless of direction —
    it never gets the multi-day move it was sized for. Positions with DTE>1
    survive overnight and are managed by stop/target only. Symbols that
    can't be parsed for DTE fall through to the price-based check rather
    than being guessed at.
    """
    now_utc = datetime.now(timezone.utc)
    past_hard_close = now_utc >= _hard_close_utc()

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM nwt_portfolio_ledger WHERE status='open' AND asset_type='option'"
        )
        positions = [dict(r) for r in cur.fetchall()]

    if not positions:
        return

    singles = []
    groups: dict[str, list] = {}
    for pos in positions:
        gid = pos.get("spread_group_id")
        if gid:
            groups.setdefault(str(gid), []).append(pos)
        else:
            singles.append(pos)

    for pos in singles:
        position_id = str(pos["position_id"])
        symbol = pos.get("asset", "")
        entry_price = float(pos.get("entry_price") or 0)

        if _has_pending_close(conn, position_id):
            continue

        exit_reason = None
        dte = option_dte(symbol)

        if past_hard_close and dte is not None and dte <= 1:
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
            _emit_close_request(conn, pos, exit_reason)

    for gid, legs in groups.items():
        if any(_has_pending_close(conn, str(leg["position_id"])) for leg in legs):
            continue

        exit_reason = _spread_exit_reason(legs, past_hard_close)
        if not exit_reason:
            continue

        # Short legs first: covering the short before selling the long means
        # there is never a naked-short interval if a later close fails
        for leg in sorted(legs, key=lambda l: 0 if l.get("direction") == "short" else 1):
            _emit_close_request(conn, leg, exit_reason)
        logger.info("Spread close requested: group=%s legs=%d reason=%s",
                    gid, len(legs), exit_reason)


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

            # Apply RISK_AGENT's sizing_multiplier (Rules 3/7 — slippage
            # expansion, regime confidence < 0.4) if it set one. This is the
            # actual sizing reduction those rules are supposed to cause; the
            # rules used to only log a WARNING with no downstream effect.
            sizing_multiplier = ticket.get("sizing_multiplier")
            if sizing_multiplier is not None and float(sizing_multiplier) < 1.0:
                sizing_multiplier = float(sizing_multiplier)
                original_notional = sized_notional
                sized_notional = round(sized_notional * sizing_multiplier, 2)
                logger.info(
                    "Ticket %s: sizing_multiplier=%.2f applied — sized_notional %.2f -> %.2f",
                    ticket_id, sizing_multiplier, original_notional, sized_notional,
                )

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

            # Base execution payload; leg/contract fields filled in per branch below.
            # direction is the market thesis, carried through for attribution —
            # the engine derives actual order sides itself (single-leg always
            # buys; only a spread's short leg can sell, always paired with a
            # long leg that bounds the risk).
            execution_payload = {
                "approved": True,
                "bot_source": bot_source,
                "symbol": symbol,
                "direction": direction,
                "strategy_id": strategy_id,
                "archetype": payload.get("archetype", ""),
                "sized_notional": sized_notional,
                "asset_type": "option",
                "time_in_force": "day",
                "stop_pct": stop_pct,
                "target_pct": target_pct,
                "strategy_type": strategy_type,
                "regime_at_decision": regime,
                "source_proposal_ticket_id": ticket_id,
            }

            if strategy_type in SPREAD_STRATEGIES:
                # Defined-risk structure — resolve all legs, submit as one mleg order
                legs = resolve_spread_legs(symbol, strategy_type, dte_min, dte_max)
                if not legs:
                    reason = f"Could not resolve {strategy_type} legs for {symbol}"
                    logger.error("Ticket %s: %s", ticket_id, reason)
                    insert_decision(conn, ticket_id, "FAILED", reason, "NWT_EXECUTION_AGENT")
                    log_system_event(conn, "ERROR", "execution_agent", reason, {"ticket_id": ticket_id})
                    failed_count += 1
                    continue
                execution_payload["legs"] = legs
                execution_payload["qty"] = size_spread_qty(legs, sized_notional)
                option_symbol = "/".join(l["option_symbol"] for l in legs)
            else:
                # Single-leg — always long premium (engine buys to open)
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

                execution_payload.update({
                    "option_symbol": option_symbol,
                    "qty": qty,
                    "strike_price": contract["strike_price"],
                    "expiration_date": contract["expiration_date"],
                    "option_type": contract["option_type"],
                })

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
