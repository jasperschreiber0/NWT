"""
nwt_agents/tests/test_force_close_unknown.py
Regression tests for targeted remediation FIX 3: process_force_close() can
(1) send DELETE /positions, (2) lose its claim afterwards, (3) raise
ClaimLostError, (4) never call record_force_close_outcome() — leaving
nwt_force_close_state stuck at ATTEMPTING forever with no record that a
real broker action may have already happened.

Fixed with an explicit UNKNOWN state: record_force_close_unknown() is
called (with ticket_id/worker_id/reason) before ClaimLostError propagates,
and an "UNKNOWN" decision is written for that ticket (distinct from Path
1/2's silent ClaimLostError handling) so has_pending_force_close_ticket()
doesn't block a future attempt from ever being scheduled.
reconcile_unknown_force_close() then resolves UNKNOWN against live broker
state on the next attempt for that position, before doing anything else.

Run against a throwaway Postgres (NWT_TEST_DB_DSN), never production:
    NWT_TEST_DB_DSN=postgresql://nwt_test:nwt_test_pw@localhost/nwt_fc_unknown_test \
        pytest nwt_agents/tests/test_force_close_unknown.py -v
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("NWT_DB_DSN", "postgresql://unused/unused")

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "execution"))
import engine  # noqa: E402

from shared_context import (  # noqa: E402
    get_force_close_state,
    has_pending_force_close_ticket,
    schedule_force_close_attempt,
)


def _insert_position(conn, asset="AAPL260101C00500000"):
    position_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (position_id, bot_source, asset, asset_type, direction, status) "
            "VALUES (%s, 'NWT_TRACK_C', %s, 'option', 'long', 'open')",
            (position_id, asset),
        )
    conn.commit()
    return position_id


def _insert_force_close_ticket(conn, position_id, asset):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (from_agent, to_agent, type, payload) "
            "VALUES ('RISK_AGENT', 'EXECUTION_ENGINE', 'FORCE_CLOSE', %s) RETURNING ticket_id",
            (json.dumps({"position_id": position_id, "symbol": asset}),),
        )
        ticket_id = str(cur.fetchone()[0])
    conn.commit()
    return ticket_id


# ---------------------------------------------------------------------------
# Scenario: force close succeeds at the broker, then the worker loses its
# claim before recording anything.
# ---------------------------------------------------------------------------

def test_claim_lost_after_delete_records_unknown_not_failed(conn, monkeypatch):
    asset = "AAPL260101C00500000"
    position_id = _insert_position(conn, asset)
    ticket_id = _insert_force_close_ticket(conn, position_id, asset)
    ticket = {"ticket_id": ticket_id, "payload": {"position_id": position_id, "symbol": asset}}

    monkeypatch.setattr(engine, "alpaca_get", lambda path: {"qty": "1"})  # pre-flight: still open
    monkeypatch.setattr(engine, "get_latest_quote", lambda symbol, asset_type: (5.0, 5.2))
    monkeypatch.setattr(engine, "alpaca_delete", lambda path: {"id": "liq-order-1"})  # broker call SUCCEEDS
    monkeypatch.setattr(engine, "renew_ticket_claim", lambda *a, **k: False)  # then claim is lost

    with pytest.raises(engine.ClaimLostError):
        engine.process_force_close(conn, ticket)

    state = get_force_close_state(conn, position_id)
    assert state["state"] == "UNKNOWN"
    assert state["state"] != "FAILED_RETRYABLE"  # must NOT be marked as an ordinary failure
    assert state["last_ticket_id"] == uuid.UUID(ticket_id) if not isinstance(state["last_ticket_id"], str) else ticket_id
    assert "CLAIM_LOST_AFTER_BROKER_ACTION" in state["last_error"]


def test_unknown_outcome_writes_a_decision_so_scheduling_is_not_blocked(conn, monkeypatch):
    """
    Unlike Path 1/2's ClaimLostError handling (deliberately silent — no
    reconciliation mechanism exists for those), FORCE_CLOSE writes an
    explicit UNKNOWN decision for the original ticket, so
    has_pending_force_close_ticket() stops seeing it as outstanding and a
    future attempt can actually be scheduled to run reconciliation.
    """
    asset = "AAPL260101C00500000"
    position_id = _insert_position(conn, asset)
    ticket_id = _insert_force_close_ticket(conn, position_id, asset)
    ticket = {"ticket_id": ticket_id, "payload": {"position_id": position_id, "symbol": asset}}

    monkeypatch.setattr(engine, "alpaca_get", lambda path: {"qty": "1"})
    monkeypatch.setattr(engine, "get_latest_quote", lambda symbol, asset_type: (5.0, 5.2))
    monkeypatch.setattr(engine, "alpaca_delete", lambda path: {"id": "liq-order-1"})
    monkeypatch.setattr(engine, "renew_ticket_claim", lambda *a, **k: False)

    with pytest.raises(engine.ClaimLostError):
        engine.process_force_close(conn, ticket)

    with conn.cursor() as cur:
        cur.execute("SELECT decision FROM nwt_ticket_decisions WHERE ticket_id = %s", (ticket_id,))
        (decision,) = cur.fetchone()
    assert decision == "UNKNOWN"

    # And scheduling must no longer be blocked by this "resolved" ticket.
    assert has_pending_force_close_ticket(conn, position_id) is False


def test_schedule_force_close_attempt_allows_immediate_retry_from_unknown(conn):
    asset = "AAPL260101C00500000"
    position_id = _insert_position(conn, asset)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_force_close_state (position_id, asset, state, attempt_count, last_error) "
            "VALUES (%s, %s, 'UNKNOWN', 1, 'CLAIM_LOST_AFTER_BROKER_ACTION')",
            (position_id, asset),
        )
    conn.commit()

    # No decision exists blocking it (simulating the fix above already ran) —
    # UNKNOWN must be immediately schedulable, not held to a long backoff,
    # since resolving genuine ambiguity should happen as soon as possible.
    should_attempt = schedule_force_close_attempt(conn, position_id, asset)

    assert should_attempt is True


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def test_reconciliation_resolves_to_success_when_broker_confirms_closed(conn, monkeypatch):
    asset = "AAPL260101C00500000"
    position_id = _insert_position(conn, asset)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_force_close_state (position_id, asset, state, attempt_count) "
            "VALUES (%s, %s, 'UNKNOWN', 1)",
            (position_id, asset),
        )
    conn.commit()

    class _NotFound(Exception):
        def __init__(self):
            self.response = type("R", (), {"status_code": 404})()

    monkeypatch.setattr(engine, "alpaca_get", lambda path: (_ for _ in ()).throw(_NotFound()))

    resolved = engine.reconcile_unknown_force_close(conn, position_id, asset)

    assert resolved == "SUCCESS"
    assert get_force_close_state(conn, position_id)["state"] == "SUCCESS"
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_portfolio_ledger WHERE position_id = %s", (position_id,))
        (status,) = cur.fetchone()
    assert status == "closed"  # the ledger gets closed too, not just the state row


def test_reconciliation_resolves_to_failed_retryable_when_broker_shows_still_open(conn, monkeypatch):
    asset = "AAPL260101C00500000"
    position_id = _insert_position(conn, asset)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_force_close_state (position_id, asset, state, attempt_count) "
            "VALUES (%s, %s, 'UNKNOWN', 1)",
            (position_id, asset),
        )
    conn.commit()

    monkeypatch.setattr(engine, "alpaca_get", lambda path: {"qty": "1"})  # still open

    resolved = engine.reconcile_unknown_force_close(conn, position_id, asset)

    assert resolved == "FAILED_RETRYABLE"
    state = get_force_close_state(conn, position_id)
    assert state["state"] == "FAILED_RETRYABLE"
    assert state["next_retry_at"] is not None
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_portfolio_ledger WHERE position_id = %s", (position_id,))
        (status,) = cur.fetchone()
    assert status == "open"  # NOT closed — broker says it's still there


def test_reconciliation_leaves_unknown_on_transient_broker_error(conn, monkeypatch):
    """A flaky lookup must not be treated as either outcome — better to
    leave it UNKNOWN for the next attempt than guess wrong."""
    asset = "AAPL260101C00500000"
    position_id = _insert_position(conn, asset)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_force_close_state (position_id, asset, state, attempt_count) "
            "VALUES (%s, %s, 'UNKNOWN', 1)",
            (position_id, asset),
        )
    conn.commit()

    monkeypatch.setattr(engine, "alpaca_get", lambda path: (_ for _ in ()).throw(ConnectionError("blip")))

    resolved = engine.reconcile_unknown_force_close(conn, position_id, asset)

    assert resolved is None
    assert get_force_close_state(conn, position_id)["state"] == "UNKNOWN"  # unchanged


def test_reconciliation_is_a_noop_when_state_is_not_unknown(conn, monkeypatch):
    asset = "AAPL260101C00500000"
    position_id = _insert_position(conn, asset)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_force_close_state (position_id, asset, state, attempt_count) "
            "VALUES (%s, %s, 'ATTEMPTING', 1)",
            (position_id, asset),
        )
    conn.commit()

    calls = []
    monkeypatch.setattr(engine, "alpaca_get", lambda path: calls.append(1))

    resolved = engine.reconcile_unknown_force_close(conn, position_id, asset)

    assert resolved is None
    assert calls == []  # never even queried the broker — nothing to reconcile


def test_process_force_close_reconciles_before_attempting_a_new_delete(conn, monkeypatch):
    """
    End-to-end: a position stuck in UNKNOWN gets a fresh FORCE_CLOSE ticket
    (as schedule_force_close_attempt now permits), and processing that
    ticket must reconcile FIRST — if the broker confirms the position is
    already gone, no new DELETE should ever be attempted.
    """
    asset = "AAPL260101C00500000"
    position_id = _insert_position(conn, asset)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_force_close_state (position_id, asset, state, attempt_count) "
            "VALUES (%s, %s, 'UNKNOWN', 1)",
            (position_id, asset),
        )
    conn.commit()
    ticket_id = _insert_force_close_ticket(conn, position_id, asset)
    ticket = {"ticket_id": ticket_id, "payload": {"position_id": position_id, "symbol": asset}}

    class _NotFound(Exception):
        def __init__(self):
            self.response = type("R", (), {"status_code": 404})()

    delete_calls = []
    monkeypatch.setattr(engine, "alpaca_get", lambda path: (_ for _ in ()).throw(_NotFound()))
    monkeypatch.setattr(engine, "alpaca_delete", lambda path: delete_calls.append(1) or {"id": "should-not-happen"})

    engine.process_force_close(conn, ticket)

    assert delete_calls == []  # reconciliation resolved it before any DELETE was attempted
    assert get_force_close_state(conn, position_id)["state"] == "SUCCESS"
    with conn.cursor() as cur:
        cur.execute("SELECT decision FROM nwt_ticket_decisions WHERE ticket_id = %s", (ticket_id,))
        (decision,) = cur.fetchone()
    assert decision == "SKIPPED"
