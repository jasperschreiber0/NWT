"""
execution/ledger.py
Handles all reads/writes to nwt_portfolio_ledger in Postgres.
Single source of truth for all positions across all bots and all tracks.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)


def insert_position(conn, data: dict) -> str:
    """
    INSERT a new position into nwt_portfolio_ledger.
    Returns the new position_id (UUID as string).

    Required keys in data:
        bot_source, asset, asset_type
    Optional keys:
        strategy_id, direction, delta_exposure, notional_risk, qty, entry_price,
        entry_time, entry_bid, entry_ask, alpaca_order_id, stop_pct, target_pct,
        spread_group_id

    spread_group_id ties together the per-leg ledger rows of one multi-leg
    (defined-risk) structure — recon matches Alpaca per contract, so legs are
    individual rows, and the position monitor values/closes them as a unit
    via this id.

    qty is the actual filled contract/share count from Alpaca — recon_agent.py
    sums it per symbol to reconcile against Alpaca's live position, so it must
    reflect the real fill, not row count or a pre-fill estimate.

    stop_pct/target_pct persist the per-trade exit parameters the ticket
    actually carried (Brain->Execution contract fields) so the equity
    position monitor can use them instead of falling back to a genome/
    hardcoded default.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_portfolio_ledger
                (bot_source, strategy_id, asset, asset_type, direction, delta_exposure,
                 notional_risk, qty, entry_price, entry_time, entry_bid, entry_ask,
                 alpaca_order_id, stop_pct, target_pct, spread_group_id, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open')
            RETURNING position_id
            """,
            (
                data["bot_source"],
                data.get("strategy_id"),
                data["asset"],
                data["asset_type"],
                data.get("direction"),
                data.get("delta_exposure"),
                data.get("notional_risk"),
                data.get("qty"),
                data.get("entry_price"),
                data.get("entry_time", datetime.now(timezone.utc)),
                data.get("entry_bid"),
                data.get("entry_ask"),
                data.get("alpaca_order_id"),
                data.get("stop_pct"),
                data.get("target_pct"),
                data.get("spread_group_id"),
            ),
        )
        position_id = cur.fetchone()[0]
    conn.commit()
    logger.info("Inserted position %s for %s (%s)", position_id, data["asset"], data["bot_source"])
    return str(position_id)


def close_position(
    conn,
    position_id: str,
    exit_price: float,
    slippage: float,
    exit_reason: str = "unknown",
    exit_bid: Optional[float] = None,
    exit_ask: Optional[float] = None,
) -> None:
    """
    UPDATE nwt_portfolio_ledger: set status='closed', exit_price, exit_time,
    realized_slippage, exit_reason, and exit NBBO (feeds the pnl_adjusted haircut).
    exit_reason: target | stop | hard_close | max_hold | kill_switch | manual
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE nwt_portfolio_ledger
            SET status = 'closed',
                exit_price = %s,
                exit_time = %s,
                realized_slippage = %s,
                exit_reason = %s,
                exit_bid = %s,
                exit_ask = %s
            WHERE position_id = %s
            """,
            (exit_price, datetime.now(timezone.utc), slippage, exit_reason, exit_bid, exit_ask, position_id),
        )
        if cur.rowcount == 0:
            logger.warning("close_position: no rows updated for position_id=%s", position_id)
    conn.commit()
    logger.info("Closed position %s at %.4f slippage=%.4f reason=%s",
                position_id, exit_price, slippage, exit_reason)


def reduce_position_qty(
    conn,
    position_id: str,
    filled_qty: float,
    fill_price: float,
    slippage: float,
    exit_reason: str,
    exit_bid: Optional[float] = None,
    exit_ask: Optional[float] = None,
) -> float:
    """
    Apply a (possibly partial) close fill: decrement the ledger row's qty by
    filled_qty. If that exhausts the position, delegate to close_position()
    for the full close semantics (status='closed', exit_price, etc). If any
    qty remains, the row stays status='open' with the reduced qty — the
    position is still live and must keep being monitored/reconciled.

    Returns the remaining qty (0.0 if now fully closed). Never assumes a
    close order fully closed the position — the caller passes the ACTUAL
    filled_qty Alpaca reported, not the qty that was requested.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT qty FROM nwt_portfolio_ledger WHERE position_id = %s FOR UPDATE",
            (position_id,),
        )
        row = cur.fetchone()
        if not row:
            logger.warning("reduce_position_qty: position %s not found", position_id)
            return 0.0
        current_qty = float(row["qty"]) if row["qty"] is not None else filled_qty
        remaining = round(max(current_qty - filled_qty, 0.0), 6)

        if remaining <= 0:
            close_position(conn, position_id, fill_price, slippage, exit_reason,
                           exit_bid=exit_bid, exit_ask=exit_ask)
            return 0.0

        cur.execute(
            "UPDATE nwt_portfolio_ledger SET qty = %s WHERE position_id = %s",
            (remaining, position_id),
        )
    conn.commit()
    logger.warning("Partial close on position %s: qty %.4f -> %.4f (%.4f filled, still OPEN)",
                    position_id, current_qty, remaining, filled_qty)
    return remaining


def mark_pending_reconciliation(conn, position_id: str, reason: str) -> None:
    """
    Flag a ledger row as needing human/recon review instead of trusting it as
    a normal open position — used when a fill price or fill quantity can't be
    resolved with confidence (e.g. an mleg leg with no resolvable price, or a
    multi-leg order that only partially filled). status stays out of both
    'open' (position monitors would act on unreliable data) and 'closed'
    (would hide real, possibly-untracked broker exposure).
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE nwt_portfolio_ledger SET status = 'pending_reconciliation' WHERE position_id = %s",
            (position_id,),
        )
    conn.commit()
    logger.warning("Position %s marked pending_reconciliation: %s", position_id, reason)


def log_reconciliation_event(
    conn,
    mismatch_type: str,
    symbol: str,
    ledger_value,
    broker_value,
    severity: str,
    recommended_action: str,
    extra: Optional[dict] = None,
) -> None:
    """
    Structured reconciliation-event log, written synchronously at the moment
    execution/engine.py detects ledger/broker divergence while handling a
    close (not just on recon_agent.py's nightly/gate schedule). Shares its
    vocabulary (mismatch_type/severity/recommended_action) with recon_agent.py
    so both surfaces are queryable the same way.
    severity: 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL'
    """
    level = severity if severity in ("INFO", "WARNING", "ERROR", "CRITICAL") else "WARNING"
    payload = {
        "reconciliation_status": "mismatch",
        "mismatch_type": mismatch_type,
        "symbol": symbol,
        "ledger_value": ledger_value,
        "broker_value": broker_value,
        "severity": level,
        "recommended_action": recommended_action,
    }
    if extra:
        payload.update(extra)
    log_system_event(
        conn, level, "reconciliation",
        f"{mismatch_type}: {symbol} ledger={ledger_value} broker={broker_value}",
        payload,
    )


def get_open_positions(conn, bot_source: Optional[str] = None) -> list:
    """
    SELECT all open positions from nwt_portfolio_ledger.
    If bot_source is given, filter to that source only.
    Returns list of dicts.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if bot_source:
            cur.execute(
                "SELECT * FROM nwt_portfolio_ledger WHERE status = 'open' AND bot_source = %s ORDER BY entry_time DESC",
                (bot_source,),
            )
        else:
            cur.execute(
                "SELECT * FROM nwt_portfolio_ledger WHERE status = 'open' ORDER BY entry_time DESC"
            )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def log_system_event(
    conn,
    level: str,
    component: str,
    message: str,
    payload: Optional[dict] = None,
) -> None:
    """
    INSERT a row into nwt_system_log.
    level: 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL'

    Like insert_decision() in engine.py, this is routinely the first write
    attempted after a caught exception (every except block across engine.py
    logs here), so the prior statement may have left conn's transaction
    aborted. Roll back first so this call always lands regardless of what
    failed before it — see insert_decision()'s docstring for the full
    reasoning and the concrete crash-recovery scenario that surfaces it.
    """
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_system_log (level, component, message, payload) VALUES (%s, %s, %s, %s)",
            (level, component, message, json.dumps(payload) if payload is not None else None),
        )
    conn.commit()
