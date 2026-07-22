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
) -> bool:
    """
    UPDATE nwt_portfolio_ledger: set status='closed', exit_price, exit_time,
    realized_slippage, exit_reason, and exit NBBO (feeds the pnl_adjusted haircut).
    exit_reason: target | stop | hard_close | max_hold | kill_switch | manual

    WHERE status = 'open' makes this idempotent: if two FORCE_CLOSE tickets
    for the same position both reach this call (possible if a second ticket
    got created for a position whose first ticket was still queued — see
    risk_agent.py's schedule_force_close_attempt / has_pending_force_close_ticket
    for the primary fix), only the first one to actually run this UPDATE
    changes anything. Without this guard a second, later close (e.g. via
    the 404-already-closed fallback path, which can use a 0.0 price when no
    quote is available) could silently overwrite a correct exit_price with
    a wrong one.

    Returns True if this call actually closed the position (a row matched
    WHERE status='open'), False if it was already closed by someone else —
    callers that need to know whether THEY caused the close (e.g. to decide
    whether to write a trade outcome) must check this, not just the absence
    of an exception.
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
            WHERE position_id = %s AND status = 'open'
            """,
            (exit_price, datetime.now(timezone.utc), slippage, exit_reason, exit_bid, exit_ask, position_id),
        )
        actually_closed = cur.rowcount > 0
        if not actually_closed:
            logger.warning(
                "close_position: position_id=%s was not 'open' (already closed by another "
                "attempt, or does not exist) — no-op, not overwriting the existing exit data",
                position_id,
            )
    conn.commit()
    if actually_closed:
        logger.info("Closed position %s at %.4f slippage=%.4f reason=%s",
                    position_id, exit_price, slippage, exit_reason)
    return actually_closed


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

TICKET_CLAIM_LEASE_SECONDS = 300  # must match shared_context.py — see its own note:
# 300s is a starting margin, not a substitute for renewal. poll_order_until_filled
# in engine.py alone has a worst case of POLL_MAX(10) x (POLL_INTERVAL(3s) +
# alpaca_get's 15s timeout) = 180s — renew_ticket_claim() below is called right
# before that poll starts so it always gets a fresh full budget.


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


def release_ticket_claim(conn, ticket_id: str, worker_id: str, status: str = "done") -> bool:
    """
    worker_id is required and enforced via WHERE claimed_by = %s. See
    shared_context.release_ticket_claim for the full rationale — without
    this guard, a worker that no longer owns the claim could clobber a
    different worker's live claim out from under it. Returns True only if
    this worker's own claim was the one actually updated.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE nwt_ticket_claims SET status = %s, updated_at = NOW() "
            "WHERE ticket_id = %s AND claimed_by = %s",
            (status, ticket_id, worker_id),
        )
        released = cur.rowcount > 0
    conn.commit()
    return released


def renew_ticket_claim(conn, ticket_id: str, worker_id: str,
                        lease_seconds: int = TICKET_CLAIM_LEASE_SECONDS) -> bool:
    """
    Extend the lease on a claim this worker still holds. See
    shared_context.renew_ticket_claim for the full rationale — identical
    mechanism, same table. Only renews a claim this exact worker_id still
    owns with status='in_progress'; returns False (and the caller must stop)
    if the claim was already lost to lease expiry and reclaimed elsewhere.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE nwt_ticket_claims
            SET lease_expires_at = NOW() + (%s * INTERVAL '1 second'), updated_at = NOW()
            WHERE ticket_id = %s AND claimed_by = %s AND status = 'in_progress'
            """,
            (lease_seconds, ticket_id, worker_id),
        )
        renewed = cur.rowcount > 0
    conn.commit()
    return renewed


# ---------------------------------------------------------------------------
# Force-close outcome recording — the execution side of the state machine
# whose scheduling half (schedule_force_close_attempt) lives in
# nwt_agents/shared_context.py and is called by risk_agent.py. This records
# what actually happened to an attempt risk_agent already scheduled;
# attempt_count / next_retry_at bookkeeping stays owned by the scheduler,
# this function only classifies and stores the outcome of one attempt.
# ---------------------------------------------------------------------------

FORCE_CLOSE_BACKOFF_MINUTES = [1, 5, 15, 60, 360, 1440]  # must match shared_context.py


def get_force_close_state(conn, position_id: str) -> Optional[dict]:
    """Duplicated from nwt_agents/shared_context.py's twin function."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_force_close_state WHERE position_id = %s", (position_id,))
        row = cur.fetchone()
    return dict(row) if row else None


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


def record_force_close_unknown(conn, position_id: str, asset: str, ticket_id: str,
                                worker_id: Optional[str] = None,
                                reason: str = "CLAIM_LOST_AFTER_BROKER_ACTION") -> None:
    """
    Record that a FORCE_CLOSE attempt's outcome is genuinely unknown: a
    broker call (the liquidation DELETE) may have already happened, but
    this worker lost its claim before it could record what happened.
    Marking this FAILED would be wrong (the liquidation might have
    succeeded — retrying could double-liquidate); marking it SUCCESS would
    also be wrong (it might not have). UNKNOWN is a genuinely distinct
    state that must be resolved by reconciliation
    (engine.reconcile_unknown_force_close) against live broker state, not
    guessed at here.

    INSERT ... ON CONFLICT DO UPDATE rather than a plain UPDATE: in the
    normal flow a row always already exists (schedule_force_close_attempt
    creates it before any ticket does), but a plain UPDATE would silently
    affect zero rows — and silently record nothing — in any scenario where
    it doesn't (a ticket created some other way, a row deleted out from
    under it). Recording UNKNOWN must never be a silent no-op.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_force_close_state
              (position_id, asset, state, attempt_count, last_error, last_ticket_id,
               last_worker_id, updated_at)
            VALUES (%(position_id)s, %(asset)s, 'UNKNOWN', 1, %(reason)s, %(ticket_id)s,
                    %(worker_id)s, NOW())
            ON CONFLICT (position_id) DO UPDATE SET
              state = 'UNKNOWN',
              last_error = %(reason)s,
              last_ticket_id = %(ticket_id)s,
              last_worker_id = %(worker_id)s,
              updated_at = NOW()
            """,
            {"position_id": position_id, "asset": asset, "reason": reason,
             "ticket_id": ticket_id, "worker_id": worker_id},
        )
    conn.commit()


# ---------------------------------------------------------------------------
# CLOSE_REQUEST dedup — same mechanism as
# nwt_agents/shared_context.has_pending_close_ticket (duplicated here, not
# imported, same pattern as everything else in this file). Used by
# run_equity_position_monitor() in engine.py before creating a new
# CLOSE_REQUEST for a position — replaces relying on a fixed time window.
# ---------------------------------------------------------------------------

def record_client_order_id(conn, ticket_id: str, client_order_id: str) -> None:
    """
    Persist the client_order_id sent to Alpaca for this ticket, for
    auditability — called at the moment order submission begins (right
    after computing it, before the broker call), not after the outcome is
    known. ON CONFLICT DO NOTHING: client_order_id_for() is deterministic,
    so a crash-and-retry recomputes and re-records the identical value —
    this must be idempotent, not raise on the second attempt.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_order_submissions (ticket_id, client_order_id)
            VALUES (%s, %s)
            ON CONFLICT (ticket_id) DO NOTHING
            """,
            (ticket_id, client_order_id),
        )
    conn.commit()


def get_client_order_id(conn, ticket_id: str) -> Optional[str]:
    """Retrieve the client_order_id previously recorded for a ticket, or
    None if none was ever recorded (e.g. the ticket never reached order
    submission — REJECTED/VETOED before that point)."""
    with conn.cursor() as cur:
        cur.execute("SELECT client_order_id FROM nwt_order_submissions WHERE ticket_id = %s", (ticket_id,))
        row = cur.fetchone()
    return row[0] if row else None


def insert_ticket(conn, from_agent: str, to_agent: str, type_: str, payload: dict) -> str:
    """
    INSERT into nwt_tickets. Returns ticket_id as string. Duplicated from
    nwt_agents/shared_context.insert_ticket — same pattern as everything
    else in this file. Needed now that run_equity_position_monitor()
    creates CLOSE_REQUEST tickets instead of calling the broker directly.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_tickets (from_agent, to_agent, type, payload)
            VALUES (%s, %s, %s, %s)
            RETURNING ticket_id
            """,
            (from_agent, to_agent, type_, json.dumps(payload)),
        )
        ticket_id = cur.fetchone()[0]
    conn.commit()
    return str(ticket_id)


# ---------------------------------------------------------------------------
# Position lifecycle state machine — see
# db/migrate_2026_07_reliability_layer.sql. Replaces the ad-hoc
# status='suspect' dead end: recon_agent.py used to UPDATE status directly
# and nothing ever read it back. lifecycle_state is the explicit state (8
# values); the legacy `status` column is kept in sync automatically by
# LEGACY_STATUS_FOR_LIFECYCLE so every existing WHERE status='open' query
# keeps working without being individually migrated — critically, this means
# RECON_PENDING/RECONCILING/UNKNOWN positions map to status='open' (NOT
# 'suspect'), so they stay visible to risk_agent.py's Rule 12 and
# execution_agent.py's options monitor instead of silently disappearing from
# every automated consumer the moment recon flags them.
# ---------------------------------------------------------------------------

LEGACY_STATUS_FOR_LIFECYCLE = {
    "OPENING": "open", "OPEN": "open", "CLOSING": "open",
    "RECON_PENDING": "open", "RECONCILING": "open", "UNKNOWN": "open",
    "CLOSED": "closed", "EXPIRED": "closed",
}


def transition_position_state(conn, position_id: str, new_state: str, reason: str,
                               source: str, expected_state: Optional[str] = None,
                               correlation_id: Optional[str] = None) -> bool:
    """
    Atomically move a position to a new lifecycle_state, keep the legacy
    status column in sync, and append a position_state_history row —
    callers must never UPDATE lifecycle_state directly.

    expected_state, if given, makes this a compare-and-swap: the transition
    only applies if the row's current lifecycle_state still matches, so two
    concurrent resolvers racing on the same position can't both "win" and
    both append contradictory history. Returns False (no-op, no history
    written) if the position doesn't exist or expected_state didn't match.
    """
    if new_state not in LEGACY_STATUS_FOR_LIFECYCLE:
        raise ValueError(f"Unknown lifecycle_state: {new_state}")
    legacy_status = LEGACY_STATUS_FOR_LIFECYCLE[new_state]

    with conn.cursor() as cur:
        if expected_state is not None:
            cur.execute(
                """
                UPDATE nwt_portfolio_ledger SET lifecycle_state = %s, status = %s
                WHERE position_id = %s AND lifecycle_state = %s
                """,
                (new_state, legacy_status, position_id, expected_state),
            )
            previous_state = expected_state
        else:
            cur.execute(
                "SELECT lifecycle_state FROM nwt_portfolio_ledger WHERE position_id = %s FOR UPDATE",
                (position_id,),
            )
            row = cur.fetchone()
            if row is None:
                return False
            previous_state = row[0]
            cur.execute(
                "UPDATE nwt_portfolio_ledger SET lifecycle_state = %s, status = %s WHERE position_id = %s",
                (new_state, legacy_status, position_id),
            )
        updated = cur.rowcount > 0

    if not updated:
        return False

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO position_state_history
              (position_id, previous_state, new_state, reason, source, correlation_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (position_id, previous_state, new_state, reason, source, correlation_id),
        )
    conn.commit()
    logger.info("Position %s: %s -> %s (%s) [%s]", position_id, previous_state, new_state, reason, source)
    return True


# ---------------------------------------------------------------------------
# Process-level advisory locks — guards an entire cron-invoked script
# against an overlapping second invocation, for workers that operate on a
# whole candidate set per run rather than one claimable ticket at a time
# (resolve_inflight_orders' row fetch, recon_agent, risk_agent's Rule 12
# sweep) where per-row claim_ticket()-style locking doesn't apply.
# Session-scoped: released automatically when conn closes, or explicitly via
# release_advisory_lock(). crontab.txt has no flock, so this is the only
# guard these workers have.
# ---------------------------------------------------------------------------

def try_advisory_lock(conn, lock_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)::bigint)", (lock_name,))
        (acquired,) = cur.fetchone()
    return bool(acquired)


def release_advisory_lock(conn, lock_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtext(%s)::bigint)", (lock_name,))
        cur.fetchone()


# ---------------------------------------------------------------------------
# Unified execution history — one row per broker action attempt. See
# db/migrate_2026_07_reliability_layer.sql. Answers "what has this system
# ever done at the broker and what happened" from one table instead of
# grepping execution_engine.log.
# ---------------------------------------------------------------------------

def record_execution_attempt(conn, ticket_id: Optional[str], action: str, result: str,
                              client_order_id: Optional[str] = None,
                              broker_order_id: Optional[str] = None,
                              fill_state: Optional[str] = None,
                              error_state: Optional[str] = None,
                              payload: Optional[dict] = None) -> None:
    """
    action:      'submit_entry' | 'submit_close' | 'force_close' | 'preflight_check'
    result:      'accepted' | 'rejected' | 'error' | 'skipped'
    fill_state:  'filled' | 'partial' | 'pending' | 'canceled' | None
    error_state: HTTP status code / exception class name, None on success
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_execution_history
              (ticket_id, client_order_id, broker_order_id, action, result,
               fill_state, error_state, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (ticket_id, client_order_id, broker_order_id, action, result,
             fill_state, error_state, json.dumps(payload) if payload is not None else None),
        )
    conn.commit()


def has_pending_close_ticket(conn, position_id: str) -> bool:
    """True if a CLOSE_REQUEST ticket for this position exists with no
    EXECUTION_ENGINE decision yet — see shared_context.py's twin function
    for the full rationale."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
              SELECT 1 FROM nwt_tickets t
              WHERE t.type = 'CLOSE_REQUEST'
                AND t.payload->>'position_id' = %s
                AND NOT EXISTS (
                  SELECT 1 FROM nwt_ticket_decisions d
                  WHERE d.ticket_id = t.ticket_id AND d.decided_by = 'EXECUTION_ENGINE'
                )
            )
            """,
            (position_id,),
        )
        (exists,) = cur.fetchone()
    return bool(exists)


def has_pending_force_close_ticket(conn, position_id: str) -> bool:
    """Duplicated from nwt_agents/shared_context.py's twin function — see
    there for the full rationale (state-based, not time-window, dedup)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
              SELECT 1 FROM nwt_tickets t
              WHERE t.type = 'FORCE_CLOSE'
                AND t.payload->>'position_id' = %s
                AND NOT EXISTS (
                  SELECT 1 FROM nwt_ticket_decisions d
                  WHERE d.ticket_id = t.ticket_id AND d.decided_by = 'EXECUTION_ENGINE'
                )
            )
            """,
            (position_id,),
        )
        (exists,) = cur.fetchone()
    return bool(exists)


# ---------------------------------------------------------------------------
# Close-attempt scheduler — duplicated from
# nwt_agents/shared_context.schedule_force_close_attempt (same mechanism,
# same table, same pattern as everything else in this file). engine.py's
# own equity position monitor (run_equity_position_monitor /
# _emit_equity_close_request) calls this in-process, the same way
# risk_agent.py and execution_agent.py call the shared_context.py copy —
# both write into the SAME nwt_force_close_state row per position_id, so a
# CLOSE_REQUEST and a FORCE_CLOSE for the same position share one backoff
# clock regardless of which component or deployment unit created the
# ticket.
# ---------------------------------------------------------------------------

FORCE_CLOSE_MAX_ATTEMPTS = len(FORCE_CLOSE_BACKOFF_MINUTES)
FORCE_CLOSE_ATTEMPTING_COOLDOWN_MINUTES = 15


def schedule_close_attempt(conn, position_id: str, asset: str) -> bool:
    """
    Single atomic decision: should a new close-type ticket (CLOSE_REQUEST or
    FORCE_CLOSE) be created for this position right now? See
    shared_context.schedule_force_close_attempt for the full rationale —
    identical logic, same nwt_force_close_state table, kept in sync
    manually (same duplication pattern as claim_ticket/release_ticket_claim
    elsewhere in this file).
    """
    if has_pending_force_close_ticket(conn, position_id) or has_pending_close_ticket(conn, position_id):
        return False

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO nwt_force_close_state
              (position_id, asset, state, attempt_count, last_attempt_at, updated_at)
            VALUES (%(position_id)s, %(asset)s, 'ATTEMPTING', 1, NOW(), NOW())
            ON CONFLICT (position_id) DO UPDATE SET
              state = CASE
                WHEN nwt_force_close_state.attempt_count + 1 > %(max_attempts)s
                  THEN 'FAILED_REQUIRES_HUMAN'
                ELSE 'ATTEMPTING'
              END,
              attempt_count = nwt_force_close_state.attempt_count + 1,
              last_attempt_at = NOW(),
              escalated_at = CASE
                WHEN nwt_force_close_state.attempt_count + 1 > %(max_attempts)s THEN NOW()
                ELSE nwt_force_close_state.escalated_at
              END,
              updated_at = NOW()
            WHERE
              nwt_force_close_state.state NOT IN ('SUCCESS', 'FAILED_TERMINAL', 'FAILED_REQUIRES_HUMAN')
              AND (
                nwt_force_close_state.state != 'FAILED_RETRYABLE'
                OR nwt_force_close_state.next_retry_at IS NULL
                OR nwt_force_close_state.next_retry_at <= NOW()
              )
              AND (
                nwt_force_close_state.state != 'ATTEMPTING'
                OR nwt_force_close_state.last_attempt_at IS NULL
                OR nwt_force_close_state.last_attempt_at
                   <= NOW() - (%(attempting_cooldown)s * INTERVAL '1 minute')
              )
            RETURNING state, attempt_count
            """,
            {
                "position_id": position_id,
                "asset": asset,
                "max_attempts": FORCE_CLOSE_MAX_ATTEMPTS,
                "attempting_cooldown": FORCE_CLOSE_ATTEMPTING_COOLDOWN_MINUTES,
            },
        )
        row = cur.fetchone()
    conn.commit()

    if row is None:
        return False

    if row["state"] == "FAILED_REQUIRES_HUMAN":
        log_system_event(
            conn, "CRITICAL", "close_attempt_scheduler",
            f"Close attempts exhausted ({FORCE_CLOSE_MAX_ATTEMPTS}) for {asset} "
            f"(position_id={position_id}) — escalated to FAILED_REQUIRES_HUMAN, "
            f"no further automatic retries. Needs manual intervention.",
            {"position_id": position_id, "asset": asset, "attempt_count": row["attempt_count"]},
        )
        return False

    return True
