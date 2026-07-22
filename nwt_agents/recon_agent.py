"""
nwt_agents/recon_agent.py
Reconciliation Agent — ledger vs Alpaca. An autonomous resolver, not just a
detector: deterministic mismatches (expired options, broker-side closes that
match a real order) are resolved automatically; only genuinely ambiguous
cases fall back to bounded-retry RECON_PENDING and eventual human escalation
(UNKNOWN). Nothing recon flags is ever a silent, permanent dead end.

Modes:
  --gate              Startup check. exit 0 = clean. exit 1 = critical mismatch.
  --nightly           Always writes a ticket (recon_ok or recon_mismatch).
  --cold-start-import Import live Alpaca positions into ledger as UNATTRIBUTED.
  --clear-if-clean    Human-invoked only: clear no_trade_mode if recon is clean.
  --adopt-untracked   Human-invoked only: import in_alpaca_not_ledger positions
                       into a non-empty ledger (cold-start only handles an
                       empty one). Recon's own automatic reconstruction
                       (below) already handles the qty-exact single-match
                       case; this is the unconditional override for what it
                       couldn't resolve on its own.

Logic:
  1. Pull Alpaca /v2/positions.
  2. Pull nwt_portfolio_ledger WHERE status='open' (includes lifecycle_state
     RECON_PENDING/RECONCILING/UNKNOWN — see LEGACY_STATUS_FOR_LIFECYCLE in
     shared_context.py — so a position recon already flagged is reconsidered
     every run, not dropped from view).
  3. Match on symbol + side.
  4. Classify and resolve:
     - in_alpaca_not_ledger → attempt automatic reconstruction from matching
       Alpaca order history. Resolved: ledger row created, not critical.
       Unresolved: CRITICAL, no_trade_mode set, tracked in
       nwt_unknown_broker_positions for human review with full context
       already gathered.
     - qty_mismatch         → CRITICAL: set no_trade_mode. Compares SIGNED
       net exposure (long positive, short negative) per symbol, not raw
       summed qty — two legitimate opposite-direction strategies in the
       same symbol (Alpaca nets them into one broker position) must never
       false-positive. Applies to every asset_type, not just options. No
       auto-resolution (adjusting a quantity automatically risks masking a
       real problem rather than fixing one).
     - in_ledger_not_alpaca → attempt automatic resolution: a matching
       closing order at the broker → CLOSED (real exit price); an option
       past expiry with no closing order → EXPIRED (worthless). Neither
       possible → bounded-retry RECON_PENDING, escalating to UNKNOWN (with a
       ticket + CRITICAL log) after RECON_MAX_ATTEMPTS. Never critical by
       itself — never sets no_trade_mode.

Clean recon writes type='recon_ok'. Absence of recon is itself detectable.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import (
    clean_alpaca_base_url,
    clear_no_trade_mode,
    get_db,
    insert_ticket,
    log_system_event,
    option_dte,
    release_advisory_lock,
    set_no_trade_mode,
    transition_position_state,
    try_advisory_lock,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("recon_agent")

ALPACA_BASE_URL = clean_alpaca_base_url(os.environ.get("NWT_ALPACA_BASE_URL", "https://paper-api.alpaca.markets"))
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("NWT_ALPACA_KEY_ID", ""),
    "APCA-API-SECRET-KEY": os.environ.get("NWT_ALPACA_SECRET_KEY", ""),
}

# After this many inconclusive recon passes (broker confirms gone, but it's
# neither a matched closing order nor an expired option — e.g. an equity
# mismatch, or a corporate action), stop retrying silently and escalate to a
# human via UNKNOWN + a ticket. Matches nwt_force_close_state's proven
# bounded-retry-then-escalate pattern.
RECON_MAX_ATTEMPTS = 5

LOCK_NAME = "recon_agent"


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def fetch_alpaca_positions() -> list:
    url = f"{ALPACA_BASE_URL}/v2/positions"
    resp = requests.get(url, headers=ALPACA_HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_ledger_open(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE status = 'open'")
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def fetch_symbol_orders(symbol: str, limit: int = 50) -> list:
    """Filled/closed order history for one symbol, most recent first."""
    url = f"{ALPACA_BASE_URL}/v2/orders"
    params = {"symbols": symbol, "status": "closed", "direction": "desc", "limit": limit}
    resp = requests.get(url, headers=ALPACA_HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def find_closing_order(symbol: str, closing_side: str, after) -> dict | None:
    """
    Most recent FILLED order on `symbol` with the given side, filled after
    `after` (the ledger row's entry_time) if given. Returns None if no such
    order exists. Raises on a broker/network failure — callers must NOT
    treat an exception the same as "no order found"; those are different
    facts (inconclusive vs. confirmed absent).
    """
    orders = fetch_symbol_orders(symbol)
    for o in orders:
        if o.get("side") != closing_side or o.get("status") != "filled":
            continue
        filled_at = o.get("filled_at")
        if after and filled_at and filled_at <= after.isoformat():
            continue
        return o
    return None


def find_opening_order(symbol: str, opening_side: str, qty: float) -> dict | None:
    """
    A single filled order on `symbol`, matching side, whose filled_qty
    equals the broker's live qty within tolerance — the deterministic case
    for reconstructing an untracked broker position. Ambiguous cases
    (multiple candidates, no exact match) intentionally return None rather
    than guess.
    """
    orders = fetch_symbol_orders(symbol)
    candidates = [o for o in orders if o.get("side") == opening_side and o.get("status") == "filled"]
    for o in candidates:
        if abs(float(o.get("filled_qty") or 0) - qty) < 0.5:
            return o
    return None


# ---------------------------------------------------------------------------
# Learning integrity — positions resolved outside the normal close path
# still produce a trade_outcomes row, so they aren't invisible to the
# Learning Agent.
# ---------------------------------------------------------------------------

def _write_reconciled_trade_outcome(conn, row: dict, exit_price: float, exit_time, resolution: str) -> None:
    entry_price = float(row.get("entry_price") or 0)
    qty = float(row.get("qty") or 1)
    direction = row.get("direction") or "long"
    sign = 1.0 if direction != "short" else -1.0
    multiplier = 100 if row.get("asset_type") == "option" else 1
    pnl = sign * (exit_price - entry_price) * qty * multiplier
    pnl_pct = ((exit_price - entry_price) / entry_price * sign) if entry_price > 0 else None

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_trade_outcomes
              (strategy_id, symbol, direction, entry_price, entry_time,
               exit_price, exit_time, pnl, pnl_pct, pnl_adjusted, slippage_model,
               closed_at, position_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                row.get("strategy_id") or row.get("bot_source", "UNKNOWN"),
                row.get("asset"), direction, entry_price, row.get("entry_time"),
                exit_price, exit_time, pnl, pnl_pct, pnl, f"recon_{resolution.lower()}",
                exit_time, row["position_id"],
            ),
        )
    conn.commit()


def _apply_ledger_close(conn, position_id: str, exit_price: float, exit_time, exit_reason: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE nwt_portfolio_ledger
            SET exit_price = %s, exit_time = %s, exit_reason = %s, realized_slippage = 0
            WHERE position_id = %s
            """,
            (exit_price, exit_time, exit_reason, position_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Case: ledger OPEN, broker missing — autonomous resolution
# ---------------------------------------------------------------------------

def resolve_ledger_not_alpaca(conn, row: dict) -> str:
    """Returns the outcome: 'CLOSED' | 'EXPIRED' | 'RECON_PENDING' | 'UNKNOWN'."""
    position_id = str(row["position_id"])
    symbol = row["asset"]
    asset_type = row.get("asset_type", "option")
    direction = row.get("direction") or "long"
    entry_time = row.get("entry_time")
    closing_side = "sell" if direction != "short" else "buy"

    lookup_ok = True
    matched = None
    try:
        matched = find_closing_order(symbol, closing_side, entry_time)
    except Exception as exc:
        lookup_ok = False
        logger.warning("recon: order-history lookup failed for %s: %s", symbol, exc)

    if matched:
        exit_price = float(matched.get("filled_avg_price") or 0)
        exit_time = matched.get("filled_at") or datetime.now(timezone.utc).isoformat()
        _apply_ledger_close(conn, position_id, exit_price, exit_time, "reconciled_closed_at_broker")
        transition_position_state(
            conn, position_id, "CLOSED",
            f"recon: matched closing order {matched.get('id')} at broker (filled {exit_time})",
            "recon_agent",
        )
        _write_reconciled_trade_outcome(conn, row, exit_price, exit_time, "CLOSED_RECONCILED")
        logger.info("recon: %s (position_id=%s) auto-resolved CLOSED via matched order", symbol, position_id)
        return "CLOSED"

    if asset_type == "option" and lookup_ok:
        dte = option_dte(symbol)
        if dte is not None and dte < 0:
            exit_time = datetime.now(timezone.utc)
            _apply_ledger_close(conn, position_id, 0.0, exit_time, "expired_worthless")
            transition_position_state(
                conn, position_id, "EXPIRED",
                "recon: option past expiry, no closing order found at broker — expired worthless",
                "recon_agent",
            )
            _write_reconciled_trade_outcome(conn, row, 0.0, exit_time, "EXPIRED")
            logger.info("recon: %s (position_id=%s) auto-resolved EXPIRED", symbol, position_id)
            return "EXPIRED"

    # Ambiguous: not a matched close, not a confirmed expiry (or the lookup
    # itself failed). Bounded retry, then escalate — never a silent dead end.
    attempts = int(row.get("recon_attempts") or 0) + 1
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE nwt_portfolio_ledger SET recon_attempts = %s, last_recon_attempt_at = NOW() "
            "WHERE position_id = %s",
            (attempts, position_id),
        )
    conn.commit()

    if attempts >= RECON_MAX_ATTEMPTS:
        transition_position_state(
            conn, position_id, "UNKNOWN",
            f"recon: unresolved after {attempts} attempts — broker shows no position, no "
            f"matching closing order, not a confirmed option expiry. Needs human review.",
            "recon_agent",
        )
        insert_ticket(conn, "RECON_AGENT", "SYSTEM", "recon_escalation_required", {
            "position_id": position_id, "symbol": symbol, "attempts": attempts,
        })
        log_system_event(
            conn, "CRITICAL", "recon_agent",
            f"Position {position_id} ({symbol}) escalated to UNKNOWN after {attempts} recon attempts",
            {"position_id": position_id, "symbol": symbol},
        )
        logger.critical("recon: %s (position_id=%s) escalated to UNKNOWN", symbol, position_id)
        return "UNKNOWN"

    transition_position_state(
        conn, position_id, "RECON_PENDING",
        f"recon: broker shows no position, resolution ambiguous — attempt {attempts}/{RECON_MAX_ATTEMPTS}",
        "recon_agent",
    )
    return "RECON_PENDING"


# ---------------------------------------------------------------------------
# Case: broker position exists, ledger missing — autonomous reconstruction
# ---------------------------------------------------------------------------

def _mark_unknown_broker_position_resolved(conn, symbol: str, apos: dict, resolution: str,
                                            position_id: str) -> None:
    """
    Mark the current unresolved nwt_unknown_broker_positions row for this
    symbol as resolved — or insert one already-resolved if none was being
    tracked yet (e.g. resolved on the very first recon pass, before any
    unresolved row ever existed).

    NOT an INSERT ... ON CONFLICT (symbol) WHERE NOT resolved DO UPDATE:
    that looked correct but silently inserted a duplicate row instead of
    updating the existing one — the new row's own resolved=TRUE value falls
    outside the partial unique index's domain (WHERE NOT resolved), so
    Postgres never detects a conflict against the existing FALSE row at
    all. UPDATE-then-insert-if-nothing-matched is the correct pattern here.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE nwt_unknown_broker_positions
            SET resolved = TRUE, resolution = %s, resolved_at = NOW(),
                reconstructed_position_id = %s
            WHERE symbol = %s AND NOT resolved
            """,
            (resolution, position_id, symbol),
        )
        updated = cur.rowcount > 0
        if not updated:
            cur.execute(
                """
                INSERT INTO nwt_unknown_broker_positions
                  (symbol, qty, side, avg_price, resolved, resolution, resolved_at, reconstructed_position_id)
                VALUES (%s, %s, %s, %s, TRUE, %s, NOW(), %s)
                """,
                (symbol, apos.get("qty", 0), apos.get("side", "long"), apos.get("avg_entry", 0),
                 resolution, position_id),
            )
    conn.commit()


def resolve_alpaca_not_ledger(conn, symbol: str, apos: dict) -> str | None:
    """
    Returns the new position_id (str) if the position was reconstructed
    automatically, or None if it remains unresolved (tracked in
    nwt_unknown_broker_positions, still CRITICAL, no_trade_mode stays set).
    """
    opening_side = "buy" if apos["side"] == "long" else "sell"
    order_history = []
    match = None
    try:
        orders = fetch_symbol_orders(symbol)
        order_history = orders
        match = find_opening_order(symbol, opening_side, apos["qty"])
    except Exception as exc:
        logger.warning("recon: order-history lookup failed for %s: %s", symbol, exc)

    if match:
        asset_type = "option" if apos.get("asset_class") == "us_option" else "equity"
        # Options are quoted per-share but represent 100 shares/contract —
        # the same multiplier bug _import_alpaca_position() below already
        # had to fix for cold_start_import/adopt_untracked (an omitted
        # multiplier here would under-report notional_risk by 100x for any
        # reconstructed option position and desync the qty_mismatch check
        # on the very next recon pass).
        multiplier = 100 if asset_type == "option" else 1
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO nwt_portfolio_ledger
                  (bot_source, asset, asset_type, direction, notional_risk, qty,
                   entry_price, entry_time, alpaca_order_id, status, lifecycle_state)
                VALUES ('RECON_RECOVERED', %s, %s, %s, %s, %s, %s, %s, %s, 'open', 'OPEN')
                RETURNING position_id
                """,
                (
                    symbol,
                    asset_type,
                    apos["side"],
                    apos["qty"] * apos["avg_entry"] * multiplier,
                    apos["qty"],
                    apos["avg_entry"],
                    match.get("filled_at") or datetime.now(timezone.utc).isoformat(),
                    match.get("id"),
                ),
            )
            position_id = str(cur.fetchone()[0])
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO position_state_history (position_id, previous_state, new_state, reason, source) "
                "VALUES (%s, NULL, 'OPEN', %s, 'recon_agent')",
                (position_id, f"recon: auto-reconstructed from matched Alpaca order {match.get('id')}"),
            )
        conn.commit()

        _mark_unknown_broker_position_resolved(
            conn, symbol, apos, "auto_reconstructed", position_id,
        )
        logger.info("recon: %s auto-reconstructed as position_id=%s from matched order", symbol, position_id)
        return position_id

    # Unresolved — persist/refresh tracking so first_seen_at survives across
    # recon runs, with everything a human needs already gathered.
    import json as _json
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_unknown_broker_positions
              (symbol, qty, side, avg_price, order_history, resolved)
            VALUES (%s, %s, %s, %s, %s, FALSE)
            ON CONFLICT (symbol) WHERE NOT resolved DO UPDATE SET
              qty = %s, avg_price = %s, order_history = %s, last_seen_at = NOW()
            """,
            (symbol, apos["qty"], apos["side"], apos["avg_entry"], _json.dumps(order_history),
             apos["qty"], apos["avg_entry"], _json.dumps(order_history)),
        )
    conn.commit()
    return None


# ---------------------------------------------------------------------------
# Recon logic
# ---------------------------------------------------------------------------

def run_recon(conn, mode: str) -> bool:
    """Returns True if clean (exit 0), False if any unresolved mismatch (exit 1)."""
    try:
        alpaca_positions = fetch_alpaca_positions()
    except Exception as exc:
        reason = f"Failed to fetch Alpaca positions: {exc}"
        logger.error(reason)
        log_system_event(conn, "ERROR", "recon_agent", reason)
        insert_ticket(conn, "RECON_AGENT", "SYSTEM", "recon_mismatch",
                      {"error": reason, "mode": mode})
        set_no_trade_mode(conn, reason, "recon_agent")
        return False

    ledger_open = fetch_ledger_open(conn)

    alpaca_map = {}
    for p in alpaca_positions:
        sym = p.get("symbol", "")
        qty = float(p.get("qty", 0))
        alpaca_map[sym] = {
            "qty": abs(qty),
            "side": "long" if qty > 0 else "short",
            "avg_entry": float(p.get("avg_entry_price", 0)),
            "asset_class": p.get("asset_class", "us_equity"),
        }

    ledger_map: dict[str, list] = {}
    for row in ledger_open:
        ledger_map.setdefault(row["asset"], []).append(row)

    mismatches = []
    critical = False

    # 1: In Alpaca but not in ledger — attempt automatic reconstruction
    for sym, apos in alpaca_map.items():
        if sym in ledger_map:
            continue
        reconstructed_id = resolve_alpaca_not_ledger(conn, sym, apos)
        if reconstructed_id:
            mismatches.append({
                "class": "in_alpaca_not_ledger", "symbol": sym, "alpaca": apos,
                "resolution": "auto_reconstructed", "position_id": reconstructed_id,
            })
            logger.info("in_alpaca_not_ledger RESOLVED: %s auto-reconstructed", sym)
        else:
            logger.error("CRITICAL UNRESOLVED: %s qty=%.0f %s not in ledger, no matching order found",
                         sym, apos["qty"], apos["side"])
            mismatches.append({
                "class": "in_alpaca_not_ledger", "symbol": sym, "alpaca": apos,
                "resolution": "unresolved_needs_human",
            })
            critical = True

    # 2: In ledger but not in Alpaca — attempt automatic resolution
    for sym, rows in ledger_map.items():
        if sym in alpaca_map:
            continue
        for row in rows:
            outcome = resolve_ledger_not_alpaca(conn, row)
            mismatches.append({
                "class": "in_ledger_not_alpaca", "symbol": sym,
                "position_id": str(row["position_id"]), "resolution": outcome,
            })
            # RECON_PENDING/UNKNOWN are not critical by themselves — they
            # never block trading. CLOSED/EXPIRED are fully resolved.

    # 3: Qty mismatch — CRITICAL, no auto-resolution (adjusting a quantity
    # automatically risks masking a real problem rather than fixing one).
    #
    # Compares SIGNED net exposure (long positive, short negative), not raw
    # summed qty — Alpaca nets opposite-direction positions in the same
    # symbol into one broker-side position (e.g. one strategy long 5 shares
    # + another strategy short 3 shares of the same equity = one broker
    # position of +2). An unsigned qty sum would either false-positive on
    # this completely legitimate case, or (for the equity check this
    # replaces, which used to skip non-option assets entirely) never catch
    # a real drift at all. Applies to every asset_type now — this used to
    # be `if asset_type != "option": continue`, which is exactly why the
    # QQQ long(5)/short(3) vs broker(+2) situation was invisible to recon:
    # equities were never qty-checked at all, signed or otherwise.
    for sym in set(alpaca_map) & set(ledger_map):
        alpaca_side = alpaca_map[sym]["side"]
        broker_net = alpaca_map[sym]["qty"] * (1.0 if alpaca_side == "long" else -1.0)
        ledger_net = sum(
            float(row.get("qty") or 0) * (1.0 if row.get("direction") != "short" else -1.0)
            for row in ledger_map[sym]
        )
        if abs(broker_net - ledger_net) > 0.5:
            entry = {"class": "qty_mismatch", "symbol": sym,
                     "broker_net_qty": broker_net, "ledger_net_qty": ledger_net}
            logger.error("CRITICAL qty mismatch: %s broker_net=%.0f ledger_net=%.0f",
                        sym, broker_net, ledger_net)
            mismatches.append(entry)
            critical = True

    if not mismatches:
        insert_ticket(conn, "RECON_AGENT", "SYSTEM", "recon_ok", {
            "alpaca_positions": len(alpaca_positions),
            "ledger_open": len(ledger_open),
            "mode": mode,
        })
        logger.info("Recon CLEAN — %d Alpaca positions, %d ledger open", len(alpaca_positions), len(ledger_open))
        return True

    insert_ticket(conn, "RECON_AGENT", "SYSTEM", "recon_mismatch", {
        "mismatches": mismatches,
        "mode": mode,
        "critical": critical,
    })
    log_system_event(conn, "CRITICAL" if critical else "WARNING", "recon_agent",
                     f"Recon {'CRITICAL' if critical else 'non-critical'} mismatch: {len(mismatches)} issues "
                     f"({sum(1 for m in mismatches if m.get('resolution') in ('CLOSED', 'EXPIRED', 'auto_reconstructed'))} auto-resolved)",
                     {"mismatches": mismatches})

    if critical:
        unresolved = [m for m in mismatches if m.get("resolution") == "unresolved_needs_human"
                      or m["class"] == "qty_mismatch"]
        reason = "Recon critical mismatch: " + "; ".join(
            f"{m['class']} {m.get('symbol','')}" for m in unresolved
        )
        set_no_trade_mode(conn, reason, "recon_agent")
        try:
            from notifier import alert_recon_critical
            alert_recon_critical([f"{m['class']} {m.get('symbol','')}" for m in unresolved])
        except Exception:
            pass

    return not critical


# ---------------------------------------------------------------------------
# Cold start import
# ---------------------------------------------------------------------------

def cold_start_import(conn) -> None:
    """
    Import live Alpaca positions into ledger as UNATTRIBUTED.
    Only runs if ledger has zero open rows. Zero is confirmed, never assumed.
    """
    ledger_open = fetch_ledger_open(conn)
    if ledger_open:
        logger.info("Ledger has %d open rows — cold start import not needed", len(ledger_open))
        return

    try:
        alpaca_positions = fetch_alpaca_positions()
    except Exception as exc:
        logger.error("cold_start_import: Alpaca fetch failed: %s", exc)
        sys.exit(1)

    if not alpaca_positions:
        insert_ticket(conn, "RECON_AGENT", "SYSTEM", "cold_start_zero_confirmed",
                      {"message": "Alpaca confirms zero open positions — system starts clean"})
        logger.info("Cold start: Alpaca confirms zero positions")
        return

    imported = 0
    for p in alpaca_positions:
        _import_alpaca_position(conn, p)
        imported += 1

    insert_ticket(conn, "RECON_AGENT", "SYSTEM", "cold_start_import", {
        "imported": imported,
        "symbols": [p.get("symbol") for p in alpaca_positions],
        "note": "entry_time=now (flagged: actual entry time unknown)",
    })
    logger.info("Cold start import complete: %d positions as UNATTRIBUTED", imported)


def _import_alpaca_position(conn, p: dict) -> str:
    """
    Insert one live Alpaca position into the ledger as UNATTRIBUTED.

    qty is written from Alpaca's real position qty — omitting it (the
    original cold-start bug) left qty=NULL, which the very next recon's
    options qty check reads as ledger_qty=0 and flags as a brand-new
    CRITICAL qty_mismatch against the row we just imported. multiplier
    fixes the matching notional_risk under-count for options (quoted
    per-share, but one contract = 100 shares).
    """
    sym = p.get("symbol", "")
    qty = float(p.get("qty", 0))
    side = "long" if qty > 0 else "short"
    avg_entry = float(p.get("avg_entry_price", 0))
    asset_class = p.get("asset_class", "us_equity")
    asset_type = "option" if asset_class == "us_option" else "equity"
    multiplier = 100 if asset_type == "option" else 1

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_portfolio_ledger
              (bot_source, asset, asset_type, direction, notional_risk,
               qty, entry_price, entry_time, status, lifecycle_state)
            VALUES ('UNATTRIBUTED', %s, %s, %s, %s, %s, %s, NOW(), 'open', 'OPEN')
            RETURNING position_id
            """,
            (sym, asset_type, side, abs(qty) * avg_entry * multiplier, abs(qty), avg_entry),
        )
        position_id = str(cur.fetchone()[0])
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO position_state_history (position_id, previous_state, new_state, reason, source) "
            "VALUES (%s, NULL, 'OPEN', %s, 'recon_agent')",
            (position_id, "recon: imported as UNATTRIBUTED"),
        )
    conn.commit()
    logger.info("Imported UNATTRIBUTED: %s %s qty=%.0f", side, sym, abs(qty))
    return position_id


# ---------------------------------------------------------------------------
# Adopt untracked (human-invoked recovery)
# ---------------------------------------------------------------------------

def adopt_untracked(conn) -> int:
    """
    HUMAN-INVOKED ONLY. Import every in_alpaca_not_ledger position into the
    ledger as UNATTRIBUTED. This is the recovery path for the case cold-start
    import cannot touch: the ledger is NOT empty, but Alpaca holds positions
    the ledger never heard about. After adopting, run --clear-if-clean to
    verify recon passes and lift no_trade_mode.

    Distinct from run_recon's own automatic reconstruction
    (resolve_alpaca_not_ledger): that path only acts when a single,
    qty-exact matching order can be found — this command is the unconditional
    human override for cases it couldn't resolve on its own.

    Never touches symbols the ledger already tracks — a qty_mismatch is a
    different problem (fix the tracked row's qty, don't add a second row).
    """
    try:
        alpaca_positions = fetch_alpaca_positions()
    except Exception as exc:
        logger.error("adopt_untracked: Alpaca fetch failed: %s", exc)
        sys.exit(1)

    ledger_symbols = {row["asset"] for row in fetch_ledger_open(conn)}
    untracked = [p for p in alpaca_positions if p.get("symbol", "") not in ledger_symbols]

    if not untracked:
        logger.info("adopt_untracked: no untracked positions — nothing to adopt")
        return 0

    for p in untracked:
        position_id = _import_alpaca_position(conn, p)
        qty = float(p.get("qty", 0))
        apos = {"qty": qty, "side": "long" if qty > 0 else "short",
               "avg_entry": float(p.get("avg_entry_price", 0))}
        _mark_unknown_broker_position_resolved(conn, p.get("symbol", ""), apos, "human_cleared", position_id)

    insert_ticket(conn, "RECON_AGENT", "SYSTEM", "untracked_adopted", {
        "adopted": len(untracked),
        "symbols": [p.get("symbol") for p in untracked],
        "note": "human-invoked --adopt-untracked; entry_time=now (actual entry time unknown)",
    })
    log_system_event(conn, "WARNING", "recon_agent",
                     f"Adopted {len(untracked)} untracked Alpaca positions as UNATTRIBUTED",
                     {"symbols": [p.get("symbol") for p in untracked]})
    logger.info("adopt_untracked: %d positions adopted as UNATTRIBUTED", len(untracked))
    return len(untracked)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="NWT Reconciliation Agent")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--gate", action="store_true", help="Integrity gate check (exit 0=clean, 1=mismatch)")
    group.add_argument("--nightly", action="store_true", help="Nightly recon (always writes ticket)")
    group.add_argument("--cold-start-import", action="store_true", dest="cold_start",
                       help="Import Alpaca positions into empty ledger")
    group.add_argument("--clear-if-clean", action="store_true", dest="clear_if_clean",
                       help="Human-invoked only: run recon, and if clean, clear no_trade_mode.")
    group.add_argument("--adopt-untracked", action="store_true", dest="adopt_untracked",
                       help="Human-invoked only: import in_alpaca_not_ledger positions into the "
                            "ledger as UNATTRIBUTED (non-empty-ledger recovery — cold start only "
                            "handles an EMPTY ledger). Follow with --clear-if-clean.")
    args = parser.parse_args()

    conn = get_db()
    try:
        if not try_advisory_lock(conn, LOCK_NAME):
            logger.warning("recon_agent: another instance is already running — skipping this invocation")
            if args.gate:
                sys.exit(0)  # don't block trading on a concurrent recon already in flight
            return

        try:
            if args.cold_start:
                cold_start_import(conn)
            elif args.adopt_untracked:
                adopt_untracked(conn)
            elif args.gate:
                cold_start_import(conn)
                clean = run_recon(conn, "gate")
                sys.exit(0 if clean else 1)
            elif args.clear_if_clean:
                clean = run_recon(conn, "manual_clear_check")
                if clean:
                    clear_no_trade_mode(conn, "recon_agent_manual_clear")
                    logger.info("Recon clean — no_trade_mode cleared")
                else:
                    logger.error("Recon NOT clean — no_trade_mode left untouched")
                    sys.exit(1)
            else:  # nightly
                run_recon(conn, "nightly")
        finally:
            release_advisory_lock(conn, LOCK_NAME)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
