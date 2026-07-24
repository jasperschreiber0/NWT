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
    Returns the position_id (UUID as string) — either newly created, or the
    pre-existing one if this exact (alpaca_order_id, asset) was already
    inserted.

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

    P0-1: idempotent on (alpaca_order_id, asset) — one_ledger_row_per_order_
    asset in migrate_2026_07_execution_idempotency.sql. A resumed ticket
    that reused an already-filled order (via execution/engine.py's
    find_or_place_order) must not create a second ledger row for the same
    fill; this makes calling insert_position twice for the same order+asset
    safe and return the same position_id both times. Rows with no
    alpaca_order_id (e.g. recon_agent.py's cold-start UNATTRIBUTED imports)
    are unaffected — the constraint only applies where alpaca_order_id is
    set, so every such row is always a fresh INSERT.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_portfolio_ledger
                (bot_source, strategy_id, asset, asset_type, direction, delta_exposure,
                 notional_risk, qty, entry_price, entry_time, entry_bid, entry_ask,
                 alpaca_order_id, stop_pct, target_pct, spread_group_id, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open')
            ON CONFLICT (alpaca_order_id, asset) WHERE alpaca_order_id IS NOT NULL
              DO NOTHING
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
        row = cur.fetchone()

        if row is None:
            # Conflict — this (alpaca_order_id, asset) was already inserted
            # by a prior attempt. Look up and reuse that row's position_id
            # rather than silently doing nothing (the caller needs an id).
            cur.execute(
                "SELECT position_id FROM nwt_portfolio_ledger WHERE alpaca_order_id = %s AND asset = %s",
                (data.get("alpaca_order_id"), data["asset"]),
            )
            row = cur.fetchone()
            conn.commit()
            position_id = row[0]
            logger.warning(
                "insert_position: (alpaca_order_id=%s, asset=%s) already exists — reusing position_id=%s",
                data.get("alpaca_order_id"), data["asset"], position_id,
            )
            return str(position_id)

        position_id = row[0]
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
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_system_log (level, component, message, payload) VALUES (%s, %s, %s, %s)",
            (level, component, message, json.dumps(payload) if payload is not None else None),
        )
    conn.commit()
