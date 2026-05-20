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
        direction, delta_exposure, notional_risk, entry_price, entry_time,
        alpaca_order_id
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_portfolio_ledger
                (bot_source, asset, asset_type, direction, delta_exposure,
                 notional_risk, entry_price, entry_time, alpaca_order_id, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'open')
            RETURNING position_id
            """,
            (
                data["bot_source"],
                data["asset"],
                data["asset_type"],
                data.get("direction"),
                data.get("delta_exposure"),
                data.get("notional_risk"),
                data.get("entry_price"),
                data.get("entry_time", datetime.now(timezone.utc)),
                data.get("alpaca_order_id"),
            ),
        )
        position_id = cur.fetchone()[0]
    conn.commit()
    logger.info("Inserted position %s for %s (%s)", position_id, data["asset"], data["bot_source"])
    return str(position_id)


def close_position(conn, position_id: str, exit_price: float, slippage: float) -> None:
    """
    UPDATE nwt_portfolio_ledger: set status='closed', exit_price, exit_time, realized_slippage.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE nwt_portfolio_ledger
            SET status = 'closed',
                exit_price = %s,
                exit_time = %s,
                realized_slippage = %s
            WHERE position_id = %s
            """,
            (exit_price, datetime.now(timezone.utc), slippage, position_id),
        )
        if cur.rowcount == 0:
            logger.warning("close_position: no rows updated for position_id=%s", position_id)
    conn.commit()
    logger.info("Closed position %s at %.4f (slippage=%.4f)", position_id, exit_price, slippage)


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


def get_position_by_order_id(conn, alpaca_order_id: str) -> Optional[dict]:
    """
    Return the ledger row matching a given Alpaca order ID, or None.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM nwt_portfolio_ledger WHERE alpaca_order_id = %s",
            (alpaca_order_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


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
