"""
nwt_agents/tests/test_close_order_race_fixes.py
Regression tests for three related fixes to close-order/reconciliation
handling (see CLAUDE.md's "Known Gotchas" and execution/engine.py's own
docstrings for the mechanisms these protect):

Finding 1 — execution/engine.py::process_close_ticket must never retire a
close ticket (mark it FAILED/FAILED_REQUIRES_HUMAN) without first verifying
the order's CURRENT broker state. An order still active at Alpaca gets
cancelled and the ticket is left undecided for the next run; only a
confirmed terminal, unfilled order is retired, as FAILED_REQUIRES_HUMAN.

Finding 3 — execution/ledger.py::close_position() is idempotent (guarded on
status != 'closed'), and nwt_agents/recon_agent.py's in_ledger_not_alpaca
handling mirrors that guard so it can never overwrite a position another
process (the execution engine) legitimately closed in the meantime. The
matching nwt_trade_outcomes.position_id unique index (see
db/migrate_2026_07_idempotent_close.sql, mirrored in conftest.py's test
schema) makes write_trade_outcome's ON CONFLICT DO NOTHING an actual guard
against a duplicate outcome row for the same position.

Finding 4 — nwt_agents/execution_agent.py::_has_pending_close treats an
undecided CLOSE_REQUEST/FORCE_CLOSE ticket as pending regardless of age, not
just tickets created within the last 2 hours — so a still-active close
(now legitimately long-lived thanks to the Finding 1 fix) never gets a
second, redundant CLOSE_REQUEST fired at it.

Run against a throwaway Postgres (NWT_TEST_DB_DSN), never production:
    NWT_TEST_DB_DSN=postgresql://nwt_test:nwt_test_pw@localhost/nwt_findings_test \
        pytest nwt_agents/tests/test_close_order_race_fixes.py -v
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_NWT_AGENTS_DIR = Path(__file__).parent.parent
_EXECUTION_DIR = _NWT_AGENTS_DIR.parent / "execution"
sys.path.insert(0, str(_NWT_AGENTS_DIR))
sys.path.insert(0, str(_EXECUTION_DIR))

# execution/engine.py reads these at import time.
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("NWT_DB_DSN", "postgresql://unused/unused")

import engine  # noqa: E402
import recon_agent  # noqa: E402
from ledger import close_position  # noqa: E402
from execution_agent import _has_pending_close  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_open_position(conn, asset="SPY250101C00500000", asset_type="option",
                          direction="long", entry_price=1.00, notional=100.0) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_portfolio_ledger
                (bot_source, strategy_id, asset, asset_type, direction,
                 notional_risk, entry_price, status)
            VALUES ('TEST_BOT', 'D1', %s, %s, %s, %s, %s, 'open')
            RETURNING position_id
            """,
            (asset, asset_type, direction, notional, entry_price),
        )
        position_id = cur.fetchone()[0]
    conn.commit()
    return str(position_id)


def _insert_close_ticket(conn, position_id: str, created_at=None) -> str:
    created_at = created_at or datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_tickets (from_agent, to_agent, type, payload, created_at)
            VALUES ('NWT_EXECUTION_AGENT', 'EXECUTION_ENGINE', 'CLOSE_REQUEST', %s, %s)
            RETURNING ticket_id
            """,
            ('{"position_id": "%s"}' % position_id, created_at),
        )
        ticket_id = cur.fetchone()[0]
    conn.commit()
    return str(ticket_id)


def _decide(conn, ticket_id: str, decision: str, decided_by: str = "EXECUTION_ENGINE") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_ticket_decisions (ticket_id, decision, decided_by) VALUES (%s, %s, %s)",
            (ticket_id, decision, decided_by),
        )
    conn.commit()


def _decisions_for(conn, ticket_id: str) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT decision FROM nwt_ticket_decisions WHERE ticket_id = %s",
            (ticket_id,),
        )
        return [r[0] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Finding 3 — close_position() idempotency
# ---------------------------------------------------------------------------

def test_close_position_is_idempotent(conn):
    position_id = _insert_open_position(conn)

    first = close_position(conn, position_id, exit_price=5.00, slippage=0.01, exit_reason="target")
    second = close_position(conn, position_id, exit_price=999.00, slippage=0.99, exit_reason="stop")

    assert first is True
    assert second is False

    with conn.cursor() as cur:
        cur.execute("SELECT status, exit_price, exit_reason FROM nwt_portfolio_ledger WHERE position_id=%s",
                    (position_id,))
        status, exit_price, exit_reason = cur.fetchone()
    assert status == "closed"
    assert float(exit_price) == 5.00       # the second call's bogus price never lands
    assert exit_reason == "target"


def test_close_position_missing_row_returns_false(conn):
    bogus_id = str(uuid.uuid4())
    result = close_position(conn, bogus_id, exit_price=1.0, slippage=0.0, exit_reason="target")
    assert result is False


# ---------------------------------------------------------------------------
# Finding 3 — recon_agent must not overwrite a concurrently-closed position
# ---------------------------------------------------------------------------

def test_recon_does_not_overwrite_a_position_closed_by_another_process(conn):
    position_id = _insert_open_position(conn, asset="XYZ250101P00100000")

    # ledger_map represents recon's read taken *before* the race — the
    # position was open at that point, matching what fetch_ledger_open()
    # would have returned then.
    stale_ledger_map = {"XYZ250101P00100000": [{"position_id": position_id}]}

    # Between that read and recon's write, the execution engine legitimately
    # closes the position for real (fill, ledger update).
    closed = close_position(conn, position_id, exit_price=2.50, slippage=0.0, exit_reason="target")
    assert closed is True

    mismatches = recon_agent._handle_ledger_not_alpaca(conn, stale_ledger_map, alpaca_map={})

    assert mismatches == []  # not reported as a mismatch — it's a real close, not untracked risk

    with conn.cursor() as cur:
        cur.execute("SELECT status, exit_price FROM nwt_portfolio_ledger WHERE position_id=%s",
                    (position_id,))
        status, exit_price = cur.fetchone()
    assert status == "closed"          # never flipped back to 'suspect'
    assert float(exit_price) == 2.50   # the real close data survives


def test_recon_still_marks_suspect_when_no_race_occurred(conn):
    position_id = _insert_open_position(conn, asset="ABC250101C00050000")
    ledger_map = {"ABC250101C00050000": [{"position_id": position_id}]}

    mismatches = recon_agent._handle_ledger_not_alpaca(conn, ledger_map, alpaca_map={})

    assert len(mismatches) == 1
    assert mismatches[0]["class"] == "in_ledger_not_alpaca"

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        (status,) = cur.fetchone()
    assert status == "suspect"


# ---------------------------------------------------------------------------
# Finding 3 — duplicate trade_outcome prevention (unique index now enforced)
# ---------------------------------------------------------------------------

def test_write_trade_outcome_conflict_do_nothing_actually_dedupes(conn):
    position_id = _insert_open_position(conn, asset="DUPTEST250101C00100000")
    pos = {
        "position_id": position_id,
        "bot_source": "NWT_TRACK_D",
        "entry_price": 1.00,
        "notional_risk": 100.0,
        "direction": "long",
        "entry_time": datetime.now(timezone.utc),
    }

    engine.write_trade_outcome(conn, pos, fill_price=1.50, exit_reason="target", strategy_id="D1")
    engine.write_trade_outcome(conn, pos, fill_price=1.50, exit_reason="target", strategy_id="D1")

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_trade_outcomes WHERE position_id=%s", (position_id,))
        (count,) = cur.fetchone()
    assert count == 1  # second write is a no-op, not a duplicate row


# ---------------------------------------------------------------------------
# Finding 1 — process_close_ticket verify-before-retire
# ---------------------------------------------------------------------------

def test_active_broker_order_is_cancelled_and_retried_not_retired(conn):
    position_id = _insert_open_position(conn, asset="SPY250101C00500000")
    ticket = {
        "ticket_id": str(uuid.uuid4()),
        "payload": {
            "option_symbol": "SPY250101C00500000",
            "position_id": position_id,
            "exit_reason": "stop",
            "asset_type": "option",
            "qty": 1,
        },
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (ticket_id, from_agent, to_agent, type, payload) "
            "VALUES (%s, 'X', 'EXECUTION_ENGINE', 'CLOSE_REQUEST', '{}')",
            (ticket["ticket_id"],),
        )
    conn.commit()

    with patch.object(engine, "place_close_order", return_value={"id": "order-1"}), \
         patch.object(engine, "poll_order_until_filled", return_value={"status": "new", "filled_avg_price": None}), \
         patch.object(engine, "alpaca_get", return_value={"status": "accepted", "filled_avg_price": None}) as mock_get, \
         patch.object(engine, "alpaca_delete", return_value={}) as mock_delete:
        engine.process_close_ticket(conn, ticket)

    mock_get.assert_called_once_with("/orders/order-1")
    mock_delete.assert_called_once_with("/orders/order-1")

    # No decision at all — the ticket stays pending for the next run.
    assert _decisions_for(conn, ticket["ticket_id"]) == []

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        (status,) = cur.fetchone()
    assert status == "open"  # position must not be marked closed on a guess


def test_confirmed_terminal_unfilled_order_marks_failed_requires_human(conn):
    position_id = _insert_open_position(conn, asset="SPY250101C00500000")
    ticket = {
        "ticket_id": str(uuid.uuid4()),
        "payload": {
            "option_symbol": "SPY250101C00500000",
            "position_id": position_id,
            "exit_reason": "stop",
            "asset_type": "option",
            "qty": 1,
        },
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (ticket_id, from_agent, to_agent, type, payload) "
            "VALUES (%s, 'X', 'EXECUTION_ENGINE', 'CLOSE_REQUEST', '{}')",
            (ticket["ticket_id"],),
        )
    conn.commit()

    with patch.object(engine, "place_close_order", return_value={"id": "order-2"}), \
         patch.object(engine, "poll_order_until_filled", return_value={"status": "canceled", "filled_avg_price": None}), \
         patch.object(engine, "alpaca_get", return_value={"status": "canceled", "filled_avg_price": None}):
        engine.process_close_ticket(conn, ticket)

    decisions = _decisions_for(conn, ticket["ticket_id"])
    assert decisions == ["FAILED_REQUIRES_HUMAN"]

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        (status,) = cur.fetchone()
    assert status == "open"  # never closed without a real fill


def test_resumed_ticket_does_not_place_a_second_order(conn):
    position_id = _insert_open_position(conn, asset="SPY250101C00500000")
    ticket_id = str(uuid.uuid4())
    ticket = {
        "ticket_id": ticket_id,
        "payload": {
            "option_symbol": "SPY250101C00500000",
            "position_id": position_id,
            "exit_reason": "stop",
            "asset_type": "option",
            "qty": 1,
        },
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (ticket_id, from_agent, to_agent, type, payload) "
            "VALUES (%s, 'X', 'EXECUTION_ENGINE', 'CLOSE_REQUEST', '{}')",
            (ticket_id,),
        )
    conn.commit()
    # Simulate a prior run that placed an order and left the ticket pending.
    engine.log_system_event(conn, "INFO", "execution_engine", "Close order placed",
                            {"ticket_id": ticket_id, "alpaca_order_id": "order-3",
                             "symbol": "SPY250101C00500000", "position_id": position_id})

    with patch.object(engine, "place_close_order") as mock_place, \
         patch.object(engine, "alpaca_get", return_value={"status": "filled", "filled_avg_price": "3.25"}):
        engine.process_close_ticket(conn, ticket)

    mock_place.assert_not_called()  # resumed the existing order instead of placing a new one
    assert _decisions_for(conn, ticket_id) == ["EXECUTED"]


# ---------------------------------------------------------------------------
# Finding 4 — _has_pending_close treats "undecided" as pending regardless of age
# ---------------------------------------------------------------------------

def test_old_undecided_close_ticket_still_counts_as_pending(conn):
    position_id = _insert_open_position(conn)
    old_time = datetime.now(timezone.utc) - timedelta(hours=6)
    _insert_close_ticket(conn, position_id, created_at=old_time)

    # No EXECUTION_ENGINE decision was ever written (order still legitimately
    # in flight, per the Finding 1 fix) — must still block a new CLOSE_REQUEST
    # even though the ticket is well past the old fixed 2-hour window.
    assert _has_pending_close(conn, position_id) is True


def test_old_decided_close_ticket_no_longer_counts_as_pending(conn):
    position_id = _insert_open_position(conn)
    old_time = datetime.now(timezone.utc) - timedelta(hours=6)
    ticket_id = _insert_close_ticket(conn, position_id, created_at=old_time)
    _decide(conn, ticket_id, "FAILED_REQUIRES_HUMAN")

    # A resolved ticket outside the recency window must not block a fresh attempt.
    assert _has_pending_close(conn, position_id) is False


def test_recent_undecided_close_ticket_counts_as_pending(conn):
    position_id = _insert_open_position(conn)
    _insert_close_ticket(conn, position_id)

    assert _has_pending_close(conn, position_id) is True
