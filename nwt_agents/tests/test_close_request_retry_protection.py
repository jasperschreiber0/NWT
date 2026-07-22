"""
nwt_agents/tests/test_close_request_retry_protection.py
Regression tests for the QQQ incident: Strategy A held +5 QQQ long,
Strategy B held -3 QQQ short. Alpaca nets same-symbol exposure into one
broker position (+2), so Strategy A's CLOSE_REQUEST (sell qty=5) was
rejected with "insufficient qty available". Nothing bounded the retry —
run_equity_position_monitor emitted a fresh CLOSE_REQUEST every 5-minute
cycle forever, because has_pending_close_ticket() only checks ticket
EXISTENCE, and a FAILED decision is terminal (frees the position up for
another ticket immediately). It only stopped by luck when Strategy B's own
stop-loss independently fired.

Fixed by:
  - schedule_close_attempt (execution/ledger.py) / schedule_force_close_attempt
    (nwt_agents/shared_context.py) now gate CLOSE_REQUEST creation the same
    way Rule 12 already gated FORCE_CLOSE — bounded retries with backoff,
    shared per position_id regardless of which ticket type is involved.
  - recon_agent.py's qty_mismatch check now covers every asset_type (was
    options-only) and compares SIGNED net exposure, so it correctly
    ignores legitimate opposite-direction positions netting cleanly while
    still catching real drift.

Run against a throwaway Postgres (NWT_TEST_DB_DSN), never production.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("NWT_DB_DSN", "postgresql://unused/unused")
os.environ.setdefault("NWT_ALPACA_KEY_ID", "test-key")
os.environ.setdefault("NWT_ALPACA_SECRET_KEY", "test-secret")

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "execution"))
import engine  # noqa: E402
import ledger  # noqa: E402

import recon_agent  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_ledger_position(conn, **overrides) -> str:
    defaults = {
        "bot_source": "US_BOT", "asset": "QQQ", "asset_type": "equity",
        "direction": "long", "qty": 5, "entry_price": 500.0,
        "entry_time": datetime.now(timezone.utc) - timedelta(days=1),
        "status": "open", "lifecycle_state": "OPEN",
    }
    defaults.update(overrides)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join(["%s"] * len(defaults))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO nwt_portfolio_ledger ({cols}) VALUES ({placeholders}) RETURNING position_id",
            list(defaults.values()),
        )
        position_id = str(cur.fetchone()[0])
    conn.commit()
    return position_id


class _FakeHTTPError(Exception):
    def __init__(self, status_code, body_json=None, body_text=""):
        # Mirror requests.exceptions.HTTPError's str() shape (e.g. "403
        # Client Error: Forbidden for url: ..."), not just the bare status
        # code — code elsewhere reads str(exc) as the human-readable error.
        super().__init__(f"{status_code} Client Error: {body_text}")
        self.response = _FakeResponse(status_code, body_json, body_text)


class _FakeResponse:
    def __init__(self, status_code, body_json, body_text):
        self.status_code = status_code
        self._body_json = body_json
        self.text = body_text

    def json(self):
        if self._body_json is None:
            raise ValueError("no json body")
        return self._body_json


QTY_MISMATCH_ERROR = _FakeHTTPError(
    403,
    body_json={"available": "2", "code": 40310000, "existing_qty": "2",
              "held_for_orders": "0",
              "message": "insufficient qty available for order (requested: 5, available: 2)",
              "symbol": "QQQ"},
    body_text='{"code":40310000,"message":"insufficient qty available for order (requested: 5, available: 2)"}',
)


# ---------------------------------------------------------------------------
# Scenario A — recon: single long, broker qty lower — mismatch detected
# ---------------------------------------------------------------------------

def test_recon_detects_equity_qty_mismatch_single_position(conn, monkeypatch):
    _insert_ledger_position(conn, asset="QQQ", direction="long", qty=5)

    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions",
                        lambda: [{"symbol": "QQQ", "qty": "2", "avg_entry_price": "500", "asset_class": "us_equity"}])

    clean = recon_agent.run_recon(conn, "test")
    assert clean is False

    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM nwt_tickets WHERE type='recon_mismatch' ORDER BY created_at DESC LIMIT 1")
        (payload,) = cur.fetchone()
    classes = [m["class"] for m in payload["mismatches"]]
    assert "qty_mismatch" in classes

    with conn.cursor() as cur:
        cur.execute("SELECT value FROM nwt_system_flags WHERE flag='no_trade_mode'")
        (halted,) = cur.fetchone()
    assert halted is True


# ---------------------------------------------------------------------------
# Scenario B — recon: two strategies netting cleanly — no false positive
# ---------------------------------------------------------------------------

def test_recon_signed_net_exposure_matches_no_false_positive(conn, monkeypatch):
    _insert_ledger_position(conn, asset="QQQ", direction="long", qty=5, bot_source="STRATEGY_A")
    _insert_ledger_position(conn, asset="QQQ", direction="short", qty=3, bot_source="STRATEGY_B")

    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions",
                        lambda: [{"symbol": "QQQ", "qty": "2", "avg_entry_price": "500", "asset_class": "us_equity"}])

    clean = recon_agent.run_recon(conn, "test")
    assert clean is True  # net +5 - 3 = +2 == broker +2 — healthy, no mismatch

    with conn.cursor() as cur:
        cur.execute("SELECT value FROM nwt_system_flags WHERE flag='no_trade_mode'")
        row = cur.fetchone()
    assert row is None or row[0] is False


def test_recon_signed_net_exposure_still_catches_real_drift_with_two_rows(conn, monkeypatch):
    """Two legitimate rows, but broker doesn't match the net — must still flag it."""
    _insert_ledger_position(conn, asset="QQQ", direction="long", qty=5, bot_source="STRATEGY_A")
    _insert_ledger_position(conn, asset="QQQ", direction="short", qty=3, bot_source="STRATEGY_B")

    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions",
                        lambda: [{"symbol": "QQQ", "qty": "9", "avg_entry_price": "500", "asset_class": "us_equity"}])

    clean = recon_agent.run_recon(conn, "test")
    assert clean is False  # net ledger +2 != broker +9


# ---------------------------------------------------------------------------
# Scenario C — bounded retry / escalation for repeatedly failing closes
# ---------------------------------------------------------------------------

def test_schedule_close_attempt_caps_retries_then_escalates(conn):
    position_id = _insert_ledger_position(conn)

    for i in range(ledger.FORCE_CLOSE_MAX_ATTEMPTS):
        should_attempt = ledger.schedule_close_attempt(conn, position_id, "QQQ")
        assert should_attempt is True
        # Simulate a terminal decision on the ticket that would have been
        # created, and a failed outcome, so the position is eligible again
        # (state=FAILED_RETRYABLE) — but push next_retry_at into the past
        # so this test doesn't have to sleep through real backoff windows.
        ledger.record_force_close_outcome(conn, position_id, success=False, error="insufficient qty available")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE nwt_force_close_state SET next_retry_at = NOW() - INTERVAL '1 second' "
                "WHERE position_id = %s", (position_id,),
            )
        conn.commit()

    # Attempt count has now exceeded FORCE_CLOSE_MAX_ATTEMPTS — must refuse
    # and escalate, never retry forever.
    should_attempt = ledger.schedule_close_attempt(conn, position_id, "QQQ")
    assert should_attempt is False

    state = ledger.get_force_close_state(conn, position_id)
    assert state["state"] == "FAILED_REQUIRES_HUMAN"

    # And it stays refused permanently, even well past any backoff.
    assert ledger.schedule_close_attempt(conn, position_id, "QQQ") is False

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM nwt_system_log WHERE level='CRITICAL' AND component='close_attempt_scheduler'"
        )
        (n,) = cur.fetchone()
    assert n >= 1


def test_equity_monitor_does_not_stack_close_requests_when_in_backoff(conn):
    """
    The exact QQQ shape: a close attempt already failed and is inside its
    backoff window — the equity monitor's _emit_equity_close_request must
    NOT create a second CLOSE_REQUEST on top of it.
    """
    position_id = _insert_ledger_position(conn)
    ledger.schedule_close_attempt(conn, position_id, "QQQ")  # first attempt, ATTEMPTING
    ledger.record_force_close_outcome(conn, position_id, success=False, error="insufficient qty available")
    # next_retry_at is now in the future (1 minute backoff) — still cooling off.

    pos = {"position_id": position_id, "bot_source": "US_BOT", "direction": "long"}
    engine._emit_equity_close_request(conn, pos, position_id, "QQQ", 2500.0, 500.0, "target")

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_tickets WHERE type='CLOSE_REQUEST'")
        (n,) = cur.fetchone()
    assert n == 0  # still in backoff — nothing created


def test_equity_monitor_creates_close_request_when_eligible(conn):
    position_id = _insert_ledger_position(conn)
    pos = {"position_id": position_id, "bot_source": "US_BOT", "direction": "long", "qty": 5}
    engine._emit_equity_close_request(conn, pos, position_id, "QQQ", 2500.0, 500.0, "target")

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_tickets WHERE type='CLOSE_REQUEST'")
        (n,) = cur.fetchone()
    assert n == 1


# ---------------------------------------------------------------------------
# Broker qty-mismatch error classification (pure logic)
# ---------------------------------------------------------------------------

def test_classify_close_order_failure_identifies_qty_mismatch():
    terminal, reason = engine._classify_close_order_failure("QQQ", "equity", QTY_MISMATCH_ERROR)
    assert terminal is False  # retryable, bounded by schedule_close_attempt — not immediately terminal
    assert "BROKER_QTY_MISMATCH" in reason


def test_classify_close_order_failure_generic_error_not_confused_with_qty_mismatch():
    generic = _FakeHTTPError(500, body_json={"message": "internal error"}, body_text="internal error")
    terminal, reason = engine._classify_close_order_failure("QQQ", "equity", generic)
    assert terminal is False
    assert "BROKER_QTY_MISMATCH" not in reason


def test_classify_close_order_failure_expired_option_is_terminal():
    d = (datetime.now(engine.ET_TZ) - timedelta(days=1)).strftime("%y%m%d")
    expired_symbol = f"SPY{d}C00500000"
    generic = _FakeHTTPError(422, body_json={"message": "unprocessable"}, body_text="unprocessable")
    terminal, reason = engine._classify_close_order_failure(expired_symbol, "option", generic)
    assert terminal is True
    assert "expired" in reason.lower()


def test_process_close_ticket_records_outcome_into_force_close_state_on_failure(conn, monkeypatch):
    position_id = _insert_ledger_position(conn)
    # Realistic flow: schedule_close_attempt always creates/advances the
    # nwt_force_close_state row BEFORE a ticket exists — process_close_ticket
    # only records outcomes against an already-scheduled attempt.
    assert ledger.schedule_close_attempt(conn, position_id, "QQQ") is True
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (ticket_id, from_agent, to_agent, type, payload) "
            "VALUES (gen_random_uuid(), 'EQUITY_MONITOR', 'EXECUTION_ENGINE', 'CLOSE_REQUEST', %s) "
            "RETURNING ticket_id",
            ['{"position_id": "%s", "symbol": "QQQ", "asset_type": "equity", "qty": 5, "direction": "long", "exit_reason": "target"}' % position_id],
        )
        ticket_id = str(cur.fetchone()[0])
    conn.commit()
    ticket = {"ticket_id": ticket_id, "payload": {
        "position_id": position_id, "symbol": "QQQ", "asset_type": "equity",
        "qty": 5, "direction": "long", "exit_reason": "target",
    }}

    monkeypatch.setattr(engine, "has_pending_inflight_close", lambda *a, **k: False)
    monkeypatch.setattr(engine, "get_open_orders", lambda *a, **k: [])
    monkeypatch.setattr(engine, "find_order_by_client_order_id", lambda *a, **k: None)
    def _raise_qty_mismatch(*a, **k):
        raise QTY_MISMATCH_ERROR
    monkeypatch.setattr(engine, "place_close_order", _raise_qty_mismatch)

    engine.process_close_ticket(conn, ticket)

    state = engine.get_force_close_state(conn, position_id)
    assert state is not None
    assert state["state"] == "FAILED_RETRYABLE"
    assert "insufficient qty" in state["last_error"].lower()
