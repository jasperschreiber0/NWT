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


# ---------------------------------------------------------------------------
# Ticket claims — same atomic claim/lease mechanism as
# nwt_agents/shared_context.py (duplicated here, not imported, since
# execution/ is deployed separately from nwt_agents/ — same pattern already
# used for log_system_event above). See
# db/migrate_2026_07_execution_safety.sql for the schema and rationale.
# ---------------------------------------------------------------------------

TICKET_CLAIM_LEASE_SECONDS = 180


def claim_ticket(conn, ticket_id: str, worker_id: str,
                  lease_seconds: int = TICKET_CLAIM_LEASE_SECONDS) -> bool:
    """
    Atomically claim a ticket for exclusive processing. Returns True iff this
    call now owns the claim. See shared_context.claim_ticket for the full
    race-safety explanation — identical mechanism, same table.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_ticket_claims (ticket_id, claimed_by, lease_expires_at, status, updated_at)
            VALUES (%s, %s, NOW() + (%s * INTERVAL '1 second'), 'in_progress', NOW())
            ON CONFLICT (ticket_id) DO UPDATE
              SET claimed_by = EXCLUDED.claimed_by,
                  claimed_at = NOW(),
                  lease_expires_at = EXCLUDED.lease_expires_at,
                  status = 'in_progress',
                  updated_at = NOW()
              WHERE nwt_ticket_claims.status = 'failed'
                 OR (nwt_ticket_claims.status = 'in_progress'
                     AND nwt_ticket_claims.lease_expires_at < NOW())
            RETURNING ticket_id
            """,
            (ticket_id, worker_id, lease_seconds),
        )
        got_it = cur.fetchone() is not None
    conn.commit()
    return got_it


def release_ticket_claim(conn, ticket_id: str, status: str = "done") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE nwt_ticket_claims SET status = %s, updated_at = NOW() WHERE ticket_id = %s",
            (status, ticket_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Force-close outcome recording — the execution side of the state machine
# whose scheduling half (schedule_force_close_attempt) lives in
# nwt_agents/shared_context.py and is called by risk_agent.py. This records
# what actually happened to an attempt risk_agent already scheduled;
# attempt_count / next_retry_at bookkeeping stays owned by the scheduler,
# this function only classifies and stores the outcome of one attempt.
# ---------------------------------------------------------------------------

FORCE_CLOSE_BACKOFF_MINUTES = [1, 5, 15, 60, 360, 1440]  # must match shared_context.py


def record_force_close_outcome(conn, position_id: str, success: bool,
                                error: Optional[str] = None,
                                terminal: bool = False,
                                terminal_reason: Optional[str] = None) -> None:
    """
    Record the outcome of one FORCE_CLOSE attempt against nwt_force_close_state.

    success=True             -> state=SUCCESS. No further retries — the
                                 position is closed.
    terminal=True (implies failure) -> state=FAILED_TERMINAL. The broker has
                                 told us this can never succeed (position
                                 already gone, contract expired). No further
                                 retries; a human doesn't need to act, this
                                 is an expected end state.
    otherwise (retryable failure)   -> state=FAILED_RETRYABLE, and
                                 next_retry_at is set using the backoff
                                 schedule at whatever attempt_count the
                                 scheduler already stamped on this row.
    """
    if success:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE nwt_force_close_state
                SET state = 'SUCCESS', last_error = NULL, updated_at = NOW()
                WHERE position_id = %s
                """,
                (position_id,),
            )
        conn.commit()
        return

    if terminal:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE nwt_force_close_state
                SET state = 'FAILED_TERMINAL', last_error = %s, terminal_reason = %s, updated_at = NOW()
                WHERE position_id = %s
                """,
                (error, terminal_reason, position_id),
            )
        conn.commit()
        return

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT attempt_count FROM nwt_force_close_state WHERE position_id = %s",
            (position_id,),
        )
        row = cur.fetchone()
    attempt_count = row["attempt_count"] if row else 1
    backoff_minutes = FORCE_CLOSE_BACKOFF_MINUTES[
        min(attempt_count - 1, len(FORCE_CLOSE_BACKOFF_MINUTES) - 1)
    ]

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE nwt_force_close_state
            SET state = 'FAILED_RETRYABLE',
                last_error = %s,
                next_retry_at = NOW() + (%s * INTERVAL '1 minute'),
                updated_at = NOW()
            WHERE position_id = %s
            """,
            (error, backoff_minutes, position_id),
        )
    conn.commit()
