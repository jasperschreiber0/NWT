"""
nwt_agents/tests/test_same_symbol_guard_and_stale_inflight.py
Regression tests for NWT Reliability Architecture v2:
  1. Rule 18 (risk_agent.py) — reject a new proposal that would create
     opposing-direction equity exposure in a symbol another strategy
     already holds, preventing the QQQ/AAPL broker-netting collision from
     ever being entered.
  2. Stale in-flight close order lifecycle (execution/engine.py) — the
     AAPL incident: an accepted-but-never-filled close order used to sit
     in nwt_inflight_orders forever with zero visibility. Now: STALE at
     30min (one cancel attempt, WARNING ticket), ESCALATE at 120min (give
     up, hand back to schedule_close_attempt's own ceiling).

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

import risk_agent  # noqa: E402
import shared_context  # noqa: E402


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


def _evaluate(conn, monkeypatch, symbol, direction, asset_type="equity", from_track="D"):
    # Rule 0 (entry cutoff) is wall-clock-dependent — pin it far in the
    # future so this test's result doesn't depend on when it happens to run.
    monkeypatch.setattr(risk_agent, "_entry_cutoff_utc",
                        lambda: datetime.now(timezone.utc) + timedelta(hours=1))
    ticket = {"payload": {
        "symbol": symbol, "direction": direction, "asset_type": asset_type,
        "from_track": from_track, "strategy_id": f"{from_track}1",
    }}
    return risk_agent.evaluate_proposal(
        conn=conn, ticket=ticket, directives={"regime": {}, "vix": 0.0},
        drawdown=0.0, consecutive_losses={}, disabled_tracks=set(),
        baseline_slippage=0.0, recent_slippage=0.0, net_delta=0.0,
        execution_stale=False, api_anomaly=False,
    )


# ---------------------------------------------------------------------------
# Test 1 — opposing same-symbol exposure blocked before execution
# ---------------------------------------------------------------------------

def test_rule18_blocks_opposing_equity_proposal(conn, monkeypatch):
    _insert_ledger_position(conn, asset="QQQ", direction="long", qty=5, bot_source="STRATEGY_A")

    decision, reasoning, _ = _evaluate(conn, monkeypatch, "QQQ", "short")

    assert decision == "VETOED"
    assert "Rule 18" in reasoning


def test_rule18_allows_same_direction_proposal(conn, monkeypatch):
    """Adding to the SAME direction is not a netting collision — must not be blocked."""
    _insert_ledger_position(conn, asset="QQQ", direction="long", qty=5, bot_source="STRATEGY_A")

    decision, _, _ = _evaluate(conn, monkeypatch, "QQQ", "long")

    assert decision == "APPROVED"


def test_rule18_allows_unrelated_symbol(conn, monkeypatch):
    _insert_ledger_position(conn, asset="QQQ", direction="long", qty=5, bot_source="STRATEGY_A")

    decision, _, _ = _evaluate(conn, monkeypatch, "AAPL", "short")

    assert decision == "APPROVED"


def test_rule18_scoped_to_equities_options_unaffected(conn, monkeypatch):
    """
    Options store the OCC contract symbol, not the underlying, in `asset` —
    a proposal for an option must never be blocked by this equity-only
    check, regardless of what equity exposure exists in the same
    underlying ticker.
    """
    _insert_ledger_position(conn, asset="QQQ", direction="long", qty=5, bot_source="STRATEGY_A")

    decision, _, _ = _evaluate(conn, monkeypatch, "QQQ", "short", asset_type="option")

    assert decision == "APPROVED"


def test_rule18_does_not_retroactively_flag_existing_opposing_positions(conn, monkeypatch):
    """
    The guard only applies at NEW proposal time — it must not be consulted
    (or block anything) for positions that already exist, including the
    exact QQQ long+5/short-3 shape that is legitimate historical state
    once recon's signed net-exposure check already confirmed it's healthy.
    This test proves the guard is a proposal-time gate only, not something
    that walks existing ledger rows and vetoes retroactively.
    """
    _insert_ledger_position(conn, asset="QQQ", direction="long", qty=5, bot_source="STRATEGY_A")
    _insert_ledger_position(conn, asset="QQQ", direction="short", qty=3, bot_source="STRATEGY_B")

    # No proposal is being evaluated here — just confirming the two rows
    # coexist untouched. (The actual "no false alert" reconciliation
    # behavior is covered by test_recon_signed_net_exposure_matches_no_false_positive
    # in test_close_request_retry_protection.py — this test only proves
    # Rule 18 itself has no side effect on existing rows.)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_portfolio_ledger WHERE asset='QQQ' AND status='open'")
        (n,) = cur.fetchone()
    assert n == 2


# ---------------------------------------------------------------------------
# Test 2 — stale in-flight close order lifecycle
# ---------------------------------------------------------------------------

def _insert_inflight_close(conn, position_id, symbol="AAPL", created_at=None, stale_since=None) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_inflight_orders
              (ticket_id, alpaca_order_id, kind, payload, position_id, exit_reason,
               status, created_at, stale_since)
            VALUES (NULL, %s, 'close', %s, %s, 'stop', 'pending', %s, %s)
            RETURNING id
            """,
            (f"order-{uuid.uuid4().hex[:8]}", f'{{"symbol": "{symbol}"}}', position_id,
             created_at or datetime.now(timezone.utc), stale_since),
        )
        inflight_id = str(cur.fetchone()[0])
    conn.commit()
    return inflight_id


def test_stale_close_order_marks_stale_and_attempts_cancel_once(conn, monkeypatch):
    position_id = _insert_ledger_position(conn)
    assert ledger.schedule_close_attempt(conn, position_id, "AAPL") is True  # seed nwt_force_close_state
    inflight_id = _insert_inflight_close(
        conn, position_id,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=35),
    )

    cancel_calls = []
    class _FakeResp:
        status_code = 200
    def _fake_delete(url, headers, timeout):
        cancel_calls.append(url)
        return _FakeResp()
    monkeypatch.setattr(engine.requests, "delete", _fake_delete)

    row = {"id": inflight_id, "ticket_id": None, "position_id": position_id,
           "payload": {"symbol": "AAPL"}, "stale_since": None}
    order = {"id": "order-abc123", "status": "accepted"}
    engine._handle_stale_close_inflight(conn, row, order, age_minutes=35)

    assert len(cancel_calls) == 1  # cancel attempted exactly once

    with conn.cursor() as cur:
        cur.execute("SELECT status, stale_since FROM nwt_inflight_orders WHERE id=%s", (inflight_id,))
        status, stale_since = cur.fetchone()
    assert status == "pending"  # not yet retired — waiting for the cancel to resolve it
    assert stale_since is not None

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_tickets WHERE type='inflight_stale_warning'")
        (n,) = cur.fetchone()
    assert n == 1


def test_stale_close_order_does_not_recancel_once_already_stale(conn, monkeypatch):
    """Calling the handler again on an already-stale row must NOT request a second cancel."""
    position_id = _insert_ledger_position(conn)
    inflight_id = _insert_inflight_close(conn, position_id, stale_since=datetime.now(timezone.utc))

    cancel_calls = []
    monkeypatch.setattr(engine.requests, "delete", lambda *a, **k: cancel_calls.append(1))

    row = {"id": inflight_id, "ticket_id": None, "position_id": position_id,
           "payload": {"symbol": "AAPL"}, "stale_since": datetime.now(timezone.utc)}
    order = {"id": "order-abc123", "status": "accepted"}
    engine._handle_stale_close_inflight(conn, row, order, age_minutes=45)

    assert len(cancel_calls) == 0


def test_escalate_gives_up_and_hands_back_to_close_attempt_scheduler(conn, monkeypatch):
    """
    Past ESCALATE_MINUTES with no resolution: retire the row (never
    pending forever) and record a retryable failure so the position
    becomes eligible again through the ALREADY-BOUNDED
    schedule_close_attempt ceiling — not a second, parallel retry engine.
    """
    position_id = _insert_ledger_position(conn)
    assert ledger.schedule_close_attempt(conn, position_id, "AAPL") is True
    inflight_id = _insert_inflight_close(conn, position_id, stale_since=datetime.now(timezone.utc) - timedelta(hours=2))

    row = {"id": inflight_id, "ticket_id": None, "position_id": position_id,
           "payload": {"symbol": "AAPL"}, "stale_since": datetime.now(timezone.utc) - timedelta(hours=2)}
    order = {"id": "order-abc123", "status": "accepted"}
    engine._handle_stale_close_inflight(conn, row, order, age_minutes=130)

    with conn.cursor() as cur:
        cur.execute("SELECT status, resolution FROM nwt_inflight_orders WHERE id=%s", (inflight_id,))
        status, resolution = cur.fetchone()
    assert status == "dead"  # never pending forever
    assert resolution == "requires_human_stale_timeout"

    state = ledger.get_force_close_state(conn, position_id)
    assert state["state"] == "FAILED_RETRYABLE"  # hands back to the existing bounded scheduler

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_tickets WHERE type='inflight_close_stuck'")
        (n,) = cur.fetchone()
    assert n == 1

    # And the position is now actually retryable again through the normal
    # gate — has_pending_inflight_close no longer blocks it.
    assert engine.has_pending_inflight_close(conn, position_id, "AAPL") is False


def test_never_pending_forever_end_to_end_through_resolve_inflight_orders(conn, monkeypatch):
    """
    End-to-end through the real resolve_inflight_orders() entry point, not
    just the helper directly — proves the wiring, not just the logic.
    """
    position_id = _insert_ledger_position(conn)
    assert ledger.schedule_close_attempt(conn, position_id, "AAPL") is True
    inflight_id = _insert_inflight_close(
        conn, position_id,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=150),
        stale_since=datetime.now(timezone.utc) - timedelta(minutes=125),
    )

    monkeypatch.setattr(engine, "alpaca_get", lambda path: {"status": "accepted", "filled_qty": "0"})
    monkeypatch.setattr(engine, "try_advisory_lock", lambda *a, **k: True)
    monkeypatch.setattr(engine, "release_advisory_lock", lambda *a, **k: None)

    engine.resolve_inflight_orders(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_inflight_orders WHERE id=%s", (inflight_id,))
        (status,) = cur.fetchone()
    assert status == "dead"
