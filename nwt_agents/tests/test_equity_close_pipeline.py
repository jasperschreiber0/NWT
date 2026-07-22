"""
nwt_agents/tests/test_equity_close_pipeline.py
Regression tests for targeted remediation FIX 1 and FIX 2:

FIX 1: run_equity_position_monitor() used to call _close_equity_position()
directly, placing a real broker order with no nwt_tickets row, no claim,
no execution decision. It now only ever creates a CLOSE_REQUEST ticket
(_emit_equity_close_request) and leaves everything downstream — claim,
client_order_id, broker call, ledger update — to the existing
process_close_ticket() pipeline options exits already use.

FIX 2: execution_agent.py's _has_pending_close() (and the equity monitor's
own dedup) used a fixed 2-hour created_at lookback, not a true idempotency
guarantee. Both now use has_pending_close_ticket()/
has_pending_force_close_ticket() — state-based (any undecided ticket for
this position), not time-based.

Run against a throwaway Postgres (NWT_TEST_DB_DSN), never production:
    NWT_TEST_DB_DSN=postgresql://nwt_test:nwt_test_pw@localhost/nwt_eqclose_test \
        pytest nwt_agents/tests/test_equity_close_pipeline.py -v
"""
import json
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

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "execution"))
import engine  # noqa: E402
import ledger  # noqa: E402

from shared_context import has_pending_close_ticket as shared_has_pending_close_ticket  # noqa: E402


def _insert_equity_position(conn, entry_price=100.0, notional=1000.0, direction="long",
                            entry_time=None, stop_pct=None, target_pct=None, bot_source="US_BOT"):
    position_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_portfolio_ledger
              (position_id, bot_source, asset, asset_type, direction, entry_price,
               notional_risk, entry_time, stop_pct, target_pct, status)
            VALUES (%s, %s, 'AAPL', 'equity', %s, %s, %s, %s, %s, %s, 'open')
            """,
            (position_id, bot_source, direction, entry_price, notional,
             entry_time or datetime.now(timezone.utc), stop_pct, target_pct),
        )
    conn.commit()
    return position_id


def _fetch_close_request_tickets(conn, position_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticket_id, from_agent, payload FROM nwt_tickets "
            "WHERE type = 'CLOSE_REQUEST' AND payload->>'position_id' = %s",
            (position_id,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# FIX 1 — equity monitor creates a ticket, never calls the broker itself
# ---------------------------------------------------------------------------

def test_stop_loss_creates_exactly_one_close_request_ticket(conn, monkeypatch):
    position_id = _insert_equity_position(conn, entry_price=100.0, notional=1000.0,
                                          stop_pct=0.015, target_pct=0.025)
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: 98.0)  # -2% > -1.5% stop

    place_close_order_calls = []
    monkeypatch.setattr(engine, "place_close_order",
                        lambda *a, **k: place_close_order_calls.append(1) or {"id": "SHOULD_NOT_BE_CALLED"})

    engine.run_equity_position_monitor(conn)

    tickets = _fetch_close_request_tickets(conn, position_id)
    assert len(tickets) == 1
    ticket_id, from_agent, payload = tickets[0]
    assert from_agent == "EQUITY_MONITOR"
    assert payload["exit_reason"] == "stop"
    assert payload["trigger_source"] == "EQUITY_MONITOR"
    # No broker call from the monitor itself — that's process_close_ticket's job
    assert place_close_order_calls == []


def test_take_profit_creates_close_request_with_target_reason(conn, monkeypatch):
    position_id = _insert_equity_position(conn, entry_price=100.0, notional=1000.0,
                                          stop_pct=0.015, target_pct=0.025)
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: 103.0)  # +3% > +2.5% target
    monkeypatch.setattr(engine, "place_close_order", lambda *a, **k: {"id": "SHOULD_NOT_BE_CALLED"})

    engine.run_equity_position_monitor(conn)

    tickets = _fetch_close_request_tickets(conn, position_id)
    assert len(tickets) == 1
    assert tickets[0][2]["exit_reason"] == "target"


def test_max_hold_creates_close_request_with_max_hold_reason(conn, monkeypatch):
    old_entry = datetime.now(timezone.utc) - timedelta(days=25)
    position_id = _insert_equity_position(conn, entry_price=100.0, notional=1000.0, entry_time=old_entry)
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: 100.5)  # flat, would not stop/target
    monkeypatch.setattr(engine, "place_close_order", lambda *a, **k: {"id": "SHOULD_NOT_BE_CALLED"})

    engine.run_equity_position_monitor(conn)

    tickets = _fetch_close_request_tickets(conn, position_id)
    assert len(tickets) == 1
    assert tickets[0][2]["exit_reason"] == "max_hold"


def test_ticket_payload_contains_required_fields(conn, monkeypatch):
    position_id = _insert_equity_position(conn, entry_price=100.0, notional=1000.0,
                                          stop_pct=0.015, target_pct=0.025, direction="long")
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: 98.0)
    monkeypatch.setattr(engine, "place_close_order", lambda *a, **k: {"id": "SHOULD_NOT_BE_CALLED"})

    engine.run_equity_position_monitor(conn)

    _, _, payload = _fetch_close_request_tickets(conn, position_id)[0]
    assert payload["position_id"] == position_id
    assert payload["symbol"] == "AAPL"
    assert payload["asset_type"] == "equity"
    assert payload["direction"] == "long"
    assert payload["exit_reason"] == "stop"
    assert payload["trigger_source"] == "EQUITY_MONITOR"


def test_no_open_position_no_trigger_creates_no_ticket(conn, monkeypatch):
    position_id = _insert_equity_position(conn, entry_price=100.0, notional=1000.0,
                                          stop_pct=0.015, target_pct=0.025)
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: 100.2)  # inside both bands

    engine.run_equity_position_monitor(conn)

    assert _fetch_close_request_tickets(conn, position_id) == []


# ---------------------------------------------------------------------------
# FIX 2 — state-based dedup, not time-window based
# ---------------------------------------------------------------------------

def test_duplicate_monitor_run_does_not_create_a_second_ticket(conn, monkeypatch):
    position_id = _insert_equity_position(conn, entry_price=100.0, notional=1000.0,
                                          stop_pct=0.015, target_pct=0.025)
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: 98.0)
    monkeypatch.setattr(engine, "place_close_order", lambda *a, **k: {"id": "SHOULD_NOT_BE_CALLED"})

    engine.run_equity_position_monitor(conn)
    engine.run_equity_position_monitor(conn)
    engine.run_equity_position_monitor(conn)

    assert len(_fetch_close_request_tickets(conn, position_id)) == 1


def test_dedup_survives_past_the_old_2_hour_window(conn, monkeypatch):
    """
    The exact scenario the fix targets: the old _has_pending_close() used a
    flat 2-hour created_at lookback — a ticket still legitimately
    outstanding (undecided) past 2 hours would silently stop being
    protected. State-based dedup must keep working regardless of how much
    time has passed, as long as the ticket has no decision.
    """
    position_id = _insert_equity_position(conn, entry_price=100.0, notional=1000.0,
                                          stop_pct=0.015, target_pct=0.025)
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: 98.0)
    monkeypatch.setattr(engine, "place_close_order", lambda *a, **k: {"id": "SHOULD_NOT_BE_CALLED"})

    engine.run_equity_position_monitor(conn)
    tickets_before = _fetch_close_request_tickets(conn, position_id)
    assert len(tickets_before) == 1
    ticket_id = tickets_before[0][0]

    # Backdate the ticket past the old 2-hour window — still no decision.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE nwt_tickets SET created_at = NOW() - INTERVAL '5 hours' WHERE ticket_id = %s",
            (ticket_id,),
        )
    conn.commit()

    engine.run_equity_position_monitor(conn)  # "run monitor again"

    tickets_after = _fetch_close_request_tickets(conn, position_id)
    assert len(tickets_after) == 1  # still only one — the fix holds past 2 hours
    assert tickets_after[0][0] == ticket_id  # same ticket, not a new one


def test_dedup_allows_a_new_ticket_once_the_prior_one_is_decided(conn, monkeypatch):
    """
    A decided (non-pending) ticket must not permanently block a position
    from ever being retried — but it also must not be immediately
    retryable the instant it's decided, which was the actual bug behind
    the QQQ incident (a FAILED decision freed the position up for a brand
    new CLOSE_REQUEST on literally the next 5-minute monitor cycle,
    forever, with no backoff). schedule_close_attempt's bounded backoff
    (see execution/ledger.py) now sits between "decided" and "retryable
    again" — this test proves both halves: immediately after a FAILED
    decision, no new ticket is created (still cooling off); once the
    backoff window has passed, a new one is.
    """
    position_id = _insert_equity_position(conn, entry_price=100.0, notional=1000.0,
                                          stop_pct=0.015, target_pct=0.025)
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: 98.0)
    monkeypatch.setattr(engine, "place_close_order", lambda *a, **k: {"id": "SHOULD_NOT_BE_CALLED"})

    engine.run_equity_position_monitor(conn)
    ticket_id = _fetch_close_request_tickets(conn, position_id)[0][0]

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_ticket_decisions (ticket_id, decision, decided_by) "
            "VALUES (%s, 'FAILED', 'EXECUTION_ENGINE')",
            (ticket_id,),
        )
    conn.commit()
    ledger.record_force_close_outcome(conn, position_id, success=False, error="simulated failure")

    engine.run_equity_position_monitor(conn)  # still cooling off — must NOT create a second ticket
    assert len(_fetch_close_request_tickets(conn, position_id)) == 1

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE nwt_force_close_state SET next_retry_at = NOW() - INTERVAL '1 second' "
            "WHERE position_id = %s", (position_id,),
        )
    conn.commit()

    engine.run_equity_position_monitor(conn)  # backoff has elapsed — now eligible again
    assert len(_fetch_close_request_tickets(conn, position_id)) == 2


# ---------------------------------------------------------------------------
# End-to-end: the created ticket actually executes through the normal
# close pipeline (claim -> client_order_id -> broker -> decision + ledger)
# ---------------------------------------------------------------------------

def test_equity_close_ticket_executes_through_process_close_ticket(conn, monkeypatch):
    position_id = _insert_equity_position(conn, entry_price=100.0, notional=1000.0,
                                          stop_pct=0.015, target_pct=0.025)
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: 98.0)
    monkeypatch.setattr(engine, "place_close_order", lambda *a, **k: {"id": "SHOULD_NOT_BE_CALLED"})
    engine.run_equity_position_monitor(conn)
    ticket_id, _, payload = _fetch_close_request_tickets(conn, position_id)[0]

    place_close_order_calls = []
    monkeypatch.setattr(engine, "find_order_by_client_order_id", lambda coid: None)
    monkeypatch.setattr(
        engine, "place_close_order",
        lambda *a, **k: place_close_order_calls.append(1) or {"id": "real-close-order"},
    )
    monkeypatch.setattr(engine, "renew_ticket_claim", lambda *a, **k: True)
    monkeypatch.setattr(
        engine, "poll_order_until_filled",
        lambda order_id: {"id": order_id, "status": "filled", "filled_avg_price": "98.0"},
    )

    engine.process_close_ticket(conn, {"ticket_id": ticket_id, "payload": payload})

    assert len(place_close_order_calls) == 1  # the real close happens here, not in the monitor
    with conn.cursor() as cur:
        cur.execute("SELECT decision FROM nwt_ticket_decisions WHERE ticket_id = %s", (ticket_id,))
        (decision,) = cur.fetchone()
    assert decision == "EXECUTED"
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_portfolio_ledger WHERE position_id = %s", (position_id,))
        (status,) = cur.fetchone()
    assert status == "closed"


def test_no_direct_broker_close_function_remains_on_engine():
    """_close_equity_position (the old direct-broker-call function) must be
    gone — its replacement, _emit_equity_close_request, only ever creates
    a ticket."""
    assert not hasattr(engine, "_close_equity_position")
    assert hasattr(engine, "_emit_equity_close_request")


# ---------------------------------------------------------------------------
# has_pending_close_ticket usable from the nwt_agents side too (shared with
# execution_agent.py's options monitor — same dedup mechanism, same table)
# ---------------------------------------------------------------------------

def test_has_pending_close_ticket_shared_between_both_sides(conn, monkeypatch):
    position_id = _insert_equity_position(conn, entry_price=100.0, notional=1000.0,
                                          stop_pct=0.015, target_pct=0.025)
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: 98.0)
    monkeypatch.setattr(engine, "place_close_order", lambda *a, **k: {"id": "SHOULD_NOT_BE_CALLED"})

    engine.run_equity_position_monitor(conn)  # ticket created via the execution/ side

    # The nwt_agents/shared_context.py side must see the exact same outstanding ticket.
    assert shared_has_pending_close_ticket(conn, position_id) is True
