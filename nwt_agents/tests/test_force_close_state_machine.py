"""
nwt_agents/tests/test_force_close_state_machine.py
Regression tests for the force-close terminal-state fix confirmed by tracing
risk_agent.py's Rule 12: the old cooldown-only logic (15-minute gap, CRITICAL
log after 3 failures) had no terminal state, so a position that can never
close (e.g. an already-expired option) generated a brand new FORCE_CLOSE
ticket forever.

Covers both halves of the state machine:
  - shared_context.schedule_force_close_attempt (the scheduler risk_agent.py
    calls before creating a ticket — owns attempt_count/backoff/escalation)
  - execution/ledger.record_force_close_outcome (what engine.py calls after
    an attempt — owns success/terminal/retryable classification)
  - execution/engine._classify_force_close_failure (the actual terminal-vs-
    retryable judgment call, pure logic, no DB)

Run against a throwaway Postgres (NWT_TEST_DB_DSN), never production:
    NWT_TEST_DB_DSN=postgresql://nwt_test:nwt_test_pw@localhost/nwt_fc_test \
        pytest nwt_agents/tests/test_force_close_state_machine.py -v
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_context import (  # noqa: E402
    FORCE_CLOSE_BACKOFF_MINUTES,
    FORCE_CLOSE_MAX_ATTEMPTS,
    get_force_close_state,
    schedule_force_close_attempt,
)

# execution/engine.py reads several env vars at module import time
# (ALPACA_BASE_URL, ALPACA_API_KEY, ALPACA_SECRET_KEY, NWT_DB_DSN) — stub
# them so the module can be imported in isolation for its pure logic
# (_classify_force_close_failure, _option_dte), same as any other test that
# only needs a handful of functions from a script with side-effecting
# top-level config reads.
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("NWT_DB_DSN", "postgresql://unused/unused")

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "execution"))
from engine import _classify_force_close_failure, _option_dte  # noqa: E402
from ledger import record_force_close_outcome  # noqa: E402


def _insert_position(conn, asset="SPY260101C00500000"):
    position_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (position_id, bot_source, asset, asset_type, status) "
            "VALUES (%s, 'NWT_TRACK_C', %s, 'option', 'open')",
            (position_id, asset),
        )
    conn.commit()
    return position_id


def _seed_state(conn, position_id, asset, **fields):
    columns = ["position_id", "asset"] + list(fields.keys())
    values = [position_id, asset] + list(fields.values())
    placeholders = ", ".join(["%s"] * len(values))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO nwt_force_close_state ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
    conn.commit()


# ---------------------------------------------------------------------------
# schedule_force_close_attempt — scheduling half
# ---------------------------------------------------------------------------

def test_first_attempt_is_scheduled_and_recorded(conn):
    position_id = _insert_position(conn)

    should_attempt = schedule_force_close_attempt(conn, position_id, "SPY260101C00500000")

    assert should_attempt is True
    state = get_force_close_state(conn, position_id)
    assert state["state"] == "ATTEMPTING"
    assert state["attempt_count"] == 1


@pytest.mark.parametrize("terminal_state", ["SUCCESS", "FAILED_TERMINAL", "FAILED_REQUIRES_HUMAN"])
def test_terminal_states_never_schedule_another_attempt(conn, terminal_state):
    position_id = _insert_position(conn)
    _seed_state(conn, position_id, "SPY260101C00500000", state=terminal_state, attempt_count=5)

    should_attempt = schedule_force_close_attempt(conn, position_id, "SPY260101C00500000")

    assert should_attempt is False
    # Terminal means terminal — attempt_count must not have moved either.
    assert get_force_close_state(conn, position_id)["attempt_count"] == 5


def test_terminal_state_still_blocks_even_if_a_stale_retry_time_has_passed(conn):
    """A terminal state must win even when time-based fields would otherwise
    look eligible — state, not the clock, is the source of truth."""
    position_id = _insert_position(conn)
    _seed_state(
        conn, position_id, "SPY260101C00500000",
        state="FAILED_TERMINAL", attempt_count=2,
        next_retry_at=datetime.now(timezone.utc) - timedelta(days=1),
    )

    assert schedule_force_close_attempt(conn, position_id, "SPY260101C00500000") is False


def test_retryable_failure_blocked_before_backoff_window_elapses(conn):
    position_id = _insert_position(conn)
    _seed_state(
        conn, position_id, "SPY260101C00500000",
        state="FAILED_RETRYABLE", attempt_count=1,
        next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )

    assert schedule_force_close_attempt(conn, position_id, "SPY260101C00500000") is False


def test_retryable_failure_allowed_once_backoff_window_elapses(conn):
    position_id = _insert_position(conn)
    _seed_state(
        conn, position_id, "SPY260101C00500000",
        state="FAILED_RETRYABLE", attempt_count=1,
        next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    should_attempt = schedule_force_close_attempt(conn, position_id, "SPY260101C00500000")

    assert should_attempt is True
    state = get_force_close_state(conn, position_id)
    assert state["state"] == "ATTEMPTING"
    assert state["attempt_count"] == 2  # bumped from 1


def test_in_flight_attempt_is_protected_within_cooldown(conn):
    """An ATTEMPTING row with a very recent last_attempt_at means a previous
    call may still be waiting on Alpaca — must not double-fire."""
    position_id = _insert_position(conn)
    _seed_state(
        conn, position_id, "SPY260101C00500000",
        state="ATTEMPTING", attempt_count=1,
        last_attempt_at=datetime.now(timezone.utc),
    )

    assert schedule_force_close_attempt(conn, position_id, "SPY260101C00500000") is False


def test_stale_attempting_state_is_reclaimed_after_cooldown(conn):
    """If engine.py crashed without ever recording an outcome, the
    ATTEMPTING row must not block retries forever."""
    position_id = _insert_position(conn)
    _seed_state(
        conn, position_id, "SPY260101C00500000",
        state="ATTEMPTING", attempt_count=1,
        last_attempt_at=datetime.now(timezone.utc) - timedelta(minutes=30),
    )

    assert schedule_force_close_attempt(conn, position_id, "SPY260101C00500000") is True


def test_escalates_to_failed_requires_human_after_max_attempts(conn):
    """
    The core fix: once attempt_count would exceed FORCE_CLOSE_MAX_ATTEMPTS,
    the position must permanently stop generating tickets instead of
    retrying forever (the exact bug that produced 25+ FORCE_CLOSE tickets
    for one already-expired option in production).
    """
    position_id = _insert_position(conn)
    _seed_state(
        conn, position_id, "SPY260101C00500000",
        state="FAILED_RETRYABLE", attempt_count=FORCE_CLOSE_MAX_ATTEMPTS,
        next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    should_attempt = schedule_force_close_attempt(conn, position_id, "SPY260101C00500000")

    assert should_attempt is False
    state = get_force_close_state(conn, position_id)
    assert state["state"] == "FAILED_REQUIRES_HUMAN"
    assert state["escalated_at"] is not None

    # And it must be permanent — a later call, even well past any backoff,
    # must never schedule another attempt again.
    assert schedule_force_close_attempt(conn, position_id, "SPY260101C00500000") is False

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM nwt_system_log WHERE level = 'CRITICAL' AND component = 'risk_agent'"
        )
        (critical_count,) = cur.fetchone()
    assert critical_count >= 1


def test_repeated_retryable_failures_eventually_self_escalate(conn):
    """
    End-to-end walk through the whole ladder: PENDING -> ATTEMPTING ->
    FAILED_RETRYABLE, repeated, must land on FAILED_REQUIRES_HUMAN exactly
    at FORCE_CLOSE_MAX_ATTEMPTS and never generate a ticket after that.
    """
    position_id = _insert_position(conn)
    asset = "SPY260101C00500000"
    attempts_scheduled = 0

    for _ in range(FORCE_CLOSE_MAX_ATTEMPTS + 3):  # try well past the ceiling
        should_attempt = schedule_force_close_attempt(conn, position_id, asset)
        if not should_attempt:
            break
        attempts_scheduled += 1
        # Simulate engine.py recording a retryable failure, then force the
        # backoff window into the past so the next loop iteration is eligible
        # immediately (this test is about the attempt ceiling, not real time).
        record_force_close_outcome(conn, position_id, success=False, error="simulated transient error")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE nwt_force_close_state SET next_retry_at = NOW() - INTERVAL '1 second' "
                "WHERE position_id = %s",
                (position_id,),
            )
        conn.commit()

    assert attempts_scheduled == FORCE_CLOSE_MAX_ATTEMPTS
    assert get_force_close_state(conn, position_id)["state"] == "FAILED_REQUIRES_HUMAN"


# ---------------------------------------------------------------------------
# record_force_close_outcome — execution-side classification
# ---------------------------------------------------------------------------

def test_record_outcome_success_sets_state_success(conn):
    position_id = _insert_position(conn)
    _seed_state(conn, position_id, "SPY260101C00500000", state="ATTEMPTING", attempt_count=1)

    record_force_close_outcome(conn, position_id, success=True)

    assert get_force_close_state(conn, position_id)["state"] == "SUCCESS"


def test_success_stops_all_future_retries(conn):
    """A successful close must be a true dead end — schedule_force_close_attempt
    must never fire again for this position after this."""
    position_id = _insert_position(conn)
    _seed_state(conn, position_id, "SPY260101C00500000", state="ATTEMPTING", attempt_count=1)

    record_force_close_outcome(conn, position_id, success=True)

    assert schedule_force_close_attempt(conn, position_id, "SPY260101C00500000") is False


def test_record_outcome_terminal_sets_failed_terminal_with_reason(conn):
    position_id = _insert_position(conn)
    _seed_state(conn, position_id, "SPY260101C00500000", state="ATTEMPTING", attempt_count=1)

    record_force_close_outcome(
        conn, position_id, success=False, error="422 Unprocessable Entity",
        terminal=True, terminal_reason="Option expired 3d ago",
    )

    state = get_force_close_state(conn, position_id)
    assert state["state"] == "FAILED_TERMINAL"
    assert state["terminal_reason"] == "Option expired 3d ago"


def test_record_outcome_retryable_computes_next_retry_from_backoff_schedule(conn):
    position_id = _insert_position(conn)
    # attempt_count=3 -> FORCE_CLOSE_BACKOFF_MINUTES[2] == 15 minutes
    _seed_state(conn, position_id, "SPY260101C00500000", state="ATTEMPTING", attempt_count=3)

    before = datetime.now(timezone.utc)
    record_force_close_outcome(conn, position_id, success=False, error="timeout")
    after = datetime.now(timezone.utc)

    state = get_force_close_state(conn, position_id)
    assert state["state"] == "FAILED_RETRYABLE"
    expected_minutes = FORCE_CLOSE_BACKOFF_MINUTES[2]
    assert state["next_retry_at"] >= before + timedelta(minutes=expected_minutes) - timedelta(seconds=5)
    assert state["next_retry_at"] <= after + timedelta(minutes=expected_minutes) + timedelta(seconds=5)


# ---------------------------------------------------------------------------
# _classify_force_close_failure — pure terminal-vs-retryable judgment
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeHTTPError(Exception):
    def __init__(self, status_code, message="error"):
        super().__init__(message)
        self.response = _FakeResponse(status_code)


def test_classify_404_as_already_closed():
    already_closed, terminal, _ = _classify_force_close_failure(
        "SPY260101C00500000", "option", _FakeHTTPError(404, "position not found"),
    )
    assert already_closed is True
    assert terminal is False  # not a failure at all — treated as success upstream


def test_classify_expired_option_as_terminal():
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%y%m%d")
    expired_symbol = f"SPY{yesterday}C00500000"

    already_closed, terminal, reason = _classify_force_close_failure(
        expired_symbol, "option", _FakeHTTPError(422, "Unprocessable Entity"),
    )

    assert already_closed is False
    assert terminal is True
    assert "expired" in reason.lower()


def test_classify_unexpired_option_generic_error_as_retryable():
    future = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%y%m%d")
    live_symbol = f"SPY{future}C00500000"

    already_closed, terminal, _ = _classify_force_close_failure(
        live_symbol, "option", _FakeHTTPError(403, "Forbidden"),
    )

    assert already_closed is False
    assert terminal is False  # transient — must remain retryable, bounded by the scheduler


def test_option_dte_parses_expiry_from_occ_symbol():
    future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%y%m%d")
    dte = _option_dte(f"SPY{future}C00500000")
    assert dte in (4, 5, 6)  # allow for ET-vs-UTC day boundary


def test_option_dte_negative_for_expired_symbol():
    past = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%y%m%d")
    dte = _option_dte(f"SPY{past}C00500000")
    assert dte < 0


def test_option_dte_none_for_unparseable_symbol():
    assert _option_dte("AAPL") is None
