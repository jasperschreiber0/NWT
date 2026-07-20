"""
nwt_agents/tests/test_broker_idempotency.py
Regression tests for the final adversarial audit's central finding: no
order placement anywhere in execution/engine.py used a client_order_id.
No claim/lease sequencing on our side can fully close the window between
"Alpaca accepted an order" and "we durably recorded that anywhere" — a
process can be killed at any point in that window, and on recovery the
same ticket would be reprocessed from scratch, resubmitting the same real
order a second time. This is fixed with:
  - client_order_id_for(ticket_id) — deterministic per ticket
  - find_order_by_client_order_id() — check broker state before submitting
  - client_order_id passed to every place_*_order() call

Run against a throwaway Postgres (NWT_TEST_DB_DSN), never production:
    NWT_TEST_DB_DSN=postgresql://nwt_test:nwt_test_pw@localhost/nwt_idem_test \
        pytest nwt_agents/tests/test_broker_idempotency.py -v
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


# ---------------------------------------------------------------------------
# client_order_id_for — pure logic
# ---------------------------------------------------------------------------

def test_client_order_id_is_deterministic():
    ticket_id = str(uuid.uuid4())
    assert engine.client_order_id_for(ticket_id) == engine.client_order_id_for(ticket_id)


def test_client_order_id_differs_per_ticket():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert engine.client_order_id_for(a) != engine.client_order_id_for(b)


def test_client_order_id_respects_alpaca_length_limit():
    # Alpaca's client_order_id has historically been capped at 48 chars.
    long_ticket_id = str(uuid.uuid4()) + "-extra-suffix-that-would-overflow"
    assert len(engine.client_order_id_for(long_ticket_id)) <= 48


def test_client_order_id_prefix_distinguishes_call_sites():
    ticket_id = str(uuid.uuid4())
    entry = engine.client_order_id_for(ticket_id, prefix="nwt")
    close = engine.client_order_id_for(ticket_id, prefix="nwt-close")
    assert entry != close  # an entry and a close for the same ticket_id must not collide


# ---------------------------------------------------------------------------
# find_order_by_client_order_id — network call, mocked
# ---------------------------------------------------------------------------

def test_find_order_returns_match(monkeypatch):
    target = {"id": "order-123", "client_order_id": "nwt-abc", "status": "filled"}
    monkeypatch.setattr(engine, "alpaca_get", lambda path: [
        {"id": "order-999", "client_order_id": "nwt-other", "status": "filled"},
        target,
    ])

    found = engine.find_order_by_client_order_id("nwt-abc")

    assert found == target


def test_find_order_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(engine, "alpaca_get", lambda path: [
        {"id": "order-999", "client_order_id": "nwt-other", "status": "filled"},
    ])

    assert engine.find_order_by_client_order_id("nwt-abc") is None


def test_find_order_lookup_failure_returns_none_not_raise(monkeypatch):
    """A broken lookup must never itself block a legitimate first-time
    submission — it degrades to 'proceed as not-found', not a hard failure."""
    def _raise(path):
        raise ConnectionError("network partition")
    monkeypatch.setattr(engine, "alpaca_get", _raise)

    assert engine.find_order_by_client_order_id("nwt-abc") is None


# ---------------------------------------------------------------------------
# Order bodies actually carry client_order_id
# ---------------------------------------------------------------------------

def test_place_equity_order_includes_client_order_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: 100.0)
    monkeypatch.setattr(engine, "alpaca_post", lambda path, body: captured.update(body) or {"id": "o1"})

    engine.place_equity_order(
        {"symbol": "AAPL", "sized_notional": 1000, "time_in_force": "day", "direction": "long"},
        client_order_id="nwt-xyz",
    )

    assert captured["client_order_id"] == "nwt-xyz"


def test_place_options_order_single_leg_includes_client_order_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(engine, "alpaca_post", lambda path, body: captured.update(body) or {"id": "o1"})

    engine.place_options_order(
        {"qty": 1, "time_in_force": "day", "option_symbol": "AAPL260101C00500000"},
        client_order_id="nwt-xyz",
    )

    assert captured["client_order_id"] == "nwt-xyz"


def test_place_options_order_mleg_includes_client_order_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(engine, "alpaca_post", lambda path, body: captured.update(body) or {"id": "o1"})

    engine.place_options_order(
        {
            "qty": 1, "time_in_force": "day",
            "legs": [{"option_symbol": "AAPL260101C00500000", "side": "buy"},
                     {"option_symbol": "AAPL260101C00510000", "side": "sell"}],
        },
        client_order_id="nwt-xyz",
    )

    assert captured["client_order_id"] == "nwt-xyz"


def test_place_close_order_includes_client_order_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(engine, "alpaca_post", lambda path, body: captured.update(body) or {"id": "o1"})

    engine.place_close_order("AAPL", 10, "equity", client_order_id="nwt-close-xyz")

    assert captured["client_order_id"] == "nwt-close-xyz"


# ---------------------------------------------------------------------------
# Crash-and-retry simulation: process_ticket must NOT resubmit when an
# order for this ticket's client_order_id already exists at the broker.
# This is the actual failure-injection proof for Audit 2 — a "process
# killed after broker order submitted, before persistence" crash, followed
# by a retry, must recover the existing order instead of placing a second
# real one.
# ---------------------------------------------------------------------------

def _insert_trade_request_ticket(conn, payload: dict) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (from_agent, to_agent, type, payload) "
            "VALUES ('NWT_EXECUTION_AGENT', 'EXECUTION_ENGINE', 'TRADE_REQUEST', %s) RETURNING ticket_id",
            (json.dumps(payload),),
        )
        ticket_id = str(cur.fetchone()[0])
    conn.commit()
    return ticket_id


def test_process_ticket_recovers_existing_order_instead_of_resubmitting(conn, monkeypatch):
    """
    Simulates: a prior worker crashed right after Alpaca accepted the order
    but before anything was recorded. A recovering worker re-processes the
    same TRADE_REQUEST ticket from scratch. place_equity_order must NEVER
    be called a second time — the existing order must be reused.
    """
    payload = {
        "approved": True, "bot_source": "US_BOT", "symbol": "AAPL",
        "direction": "long", "strategy_id": "US-ORB-001", "sized_notional": 1000,
        "asset_type": "equity", "time_in_force": "day",
    }
    ticket_id = _insert_trade_request_ticket(conn, payload)
    ticket = {"ticket_id": ticket_id, "payload": payload}

    place_equity_order_calls = []
    monkeypatch.setattr(engine, "synchronous_risk_veto", lambda conn, payload: (False, ""))
    monkeypatch.setattr(engine, "check_directional_cap", lambda conn, direction, notional: (False, 0.0, 100000.0))
    monkeypatch.setattr(engine, "get_latest_quote", lambda symbol, asset_type: (99.0, 101.0))
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: 100.0)
    monkeypatch.setattr(
        engine, "find_order_by_client_order_id",
        lambda coid: {"id": "existing-order-1", "client_order_id": coid, "status": "accepted"},
    )
    monkeypatch.setattr(engine, "place_equity_order", lambda payload, coid: place_equity_order_calls.append(coid) or {"id": "SHOULD_NOT_BE_CALLED"})
    monkeypatch.setattr(engine, "renew_ticket_claim", lambda *a, **k: True)  # not under test here
    monkeypatch.setattr(
        engine, "poll_order_until_filled",
        lambda order_id: {"id": order_id, "status": "filled", "filled_avg_price": "100.5", "filled_qty": "10"},
    )

    engine.process_ticket(conn, ticket, directives={"global_kill_switch": False})

    assert place_equity_order_calls == []  # never resubmitted
    with conn.cursor() as cur:
        cur.execute("SELECT decision, reasoning FROM nwt_ticket_decisions WHERE ticket_id = %s", (ticket_id,))
        decision, reasoning = cur.fetchone()
    assert decision == "EXECUTED"
    assert "existing-order-1" in reasoning


def test_process_ticket_places_a_real_order_when_none_exists_yet(conn, monkeypatch):
    """Sanity check for the other branch: a genuine first-time submission
    must still place a real order — the fix must not block legitimate
    trades, only duplicate ones."""
    payload = {
        "approved": True, "bot_source": "US_BOT", "symbol": "AAPL",
        "direction": "long", "strategy_id": "US-ORB-001", "sized_notional": 1000,
        "asset_type": "equity", "time_in_force": "day",
    }
    ticket_id = _insert_trade_request_ticket(conn, payload)
    ticket = {"ticket_id": ticket_id, "payload": payload}

    place_equity_order_calls = []
    monkeypatch.setattr(engine, "synchronous_risk_veto", lambda conn, payload: (False, ""))
    monkeypatch.setattr(engine, "check_directional_cap", lambda conn, direction, notional: (False, 0.0, 100000.0))
    monkeypatch.setattr(engine, "get_latest_quote", lambda symbol, asset_type: (99.0, 101.0))
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: 100.0)
    monkeypatch.setattr(engine, "find_order_by_client_order_id", lambda coid: None)  # nothing exists yet
    monkeypatch.setattr(
        engine, "place_equity_order",
        lambda payload, coid: place_equity_order_calls.append(coid) or {"id": "new-order-1"},
    )
    monkeypatch.setattr(engine, "renew_ticket_claim", lambda *a, **k: True)  # not under test here
    monkeypatch.setattr(
        engine, "poll_order_until_filled",
        lambda order_id: {"id": order_id, "status": "filled", "filled_avg_price": "100.5", "filled_qty": "10"},
    )

    engine.process_ticket(conn, ticket, directives={"global_kill_switch": False})

    assert len(place_equity_order_calls) == 1
    assert place_equity_order_calls[0] == engine.client_order_id_for(ticket_id)


# ---------------------------------------------------------------------------
# ClaimLostError propagation — final adversarial audit finding: the return
# value of renew_ticket_claim() was being silently discarded at all four
# call sites, meaning renewal was cosmetic — a worker that had already lost
# its claim would keep processing (polling, writing decisions) as if it
# still owned the ticket, right alongside whichever worker actually
# reclaimed it. These tests prove a lost claim now aborts loudly instead.
# ---------------------------------------------------------------------------

def test_process_ticket_raises_and_writes_no_decision_on_lost_claim(conn, monkeypatch):
    payload = {
        "approved": True, "bot_source": "US_BOT", "symbol": "AAPL",
        "direction": "long", "strategy_id": "US-ORB-001", "sized_notional": 1000,
        "asset_type": "equity", "time_in_force": "day",
    }
    ticket_id = _insert_trade_request_ticket(conn, payload)
    ticket = {"ticket_id": ticket_id, "payload": payload}

    monkeypatch.setattr(engine, "synchronous_risk_veto", lambda conn, payload: (False, ""))
    monkeypatch.setattr(engine, "check_directional_cap", lambda conn, direction, notional: (False, 0.0, 100000.0))
    monkeypatch.setattr(engine, "get_latest_quote", lambda symbol, asset_type: (99.0, 101.0))
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: 100.0)
    monkeypatch.setattr(engine, "find_order_by_client_order_id", lambda coid: None)
    monkeypatch.setattr(engine, "place_equity_order", lambda payload, coid: {"id": "new-order-1"})
    # The order above is now "real" at the broker — renewal reporting lost
    # ownership right after this must not be treated as an ordinary poll failure.
    monkeypatch.setattr(engine, "renew_ticket_claim", lambda *a, **k: False)

    with pytest.raises(engine.ClaimLostError):
        engine.process_ticket(conn, ticket, directives={"global_kill_switch": False})

    with conn.cursor() as cur:
        cur.execute("SELECT decision FROM nwt_ticket_decisions WHERE ticket_id = %s", (ticket_id,))
        decisions = cur.fetchall()
    assert decisions == []  # not FAILED, not anything


def test_process_close_ticket_raises_and_writes_no_decision_on_lost_claim(conn, monkeypatch):
    position_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (position_id, bot_source, asset, asset_type, direction, status) "
            "VALUES (%s, 'NWT_TRACK_C', 'AAPL260101C00500000', 'option', 'long', 'open')",
            (position_id,),
        )
    conn.commit()

    payload = {"option_symbol": "AAPL260101C00500000", "position_id": position_id,
               "exit_reason": "target", "asset_type": "option", "qty": 1, "direction": "long"}
    ticket_id = _insert_trade_request_ticket(conn, payload)  # type doesn't matter for this helper
    ticket = {"ticket_id": ticket_id, "payload": payload}

    monkeypatch.setattr(engine, "find_order_by_client_order_id", lambda coid: None)
    monkeypatch.setattr(engine, "place_close_order", lambda *a, **k: {"id": "close-order-1"})
    monkeypatch.setattr(engine, "renew_ticket_claim", lambda *a, **k: False)

    with pytest.raises(engine.ClaimLostError):
        engine.process_close_ticket(conn, ticket)

    with conn.cursor() as cur:
        cur.execute("SELECT decision FROM nwt_ticket_decisions WHERE ticket_id = %s", (ticket_id,))
        decisions = cur.fetchall()
    assert decisions == []


# ---------------------------------------------------------------------------
# FORCE_CLOSE pre-flight state check — a crash-and-retry against an
# already-closed position must never even attempt the DELETE call.
# ---------------------------------------------------------------------------

def test_force_close_preflight_skips_delete_when_position_already_gone(conn, monkeypatch):
    position_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (position_id, bot_source, asset, asset_type, direction, status) "
            "VALUES (%s, 'NWT_TRACK_C', 'AAPL260101C00500000', 'option', 'long', 'open')",
            (position_id,),
        )
    conn.commit()
    payload = {"symbol": "AAPL260101C00500000", "position_id": position_id}
    ticket_id = _insert_trade_request_ticket(conn, payload)
    ticket = {"ticket_id": ticket_id, "payload": payload}

    class _NotFound(Exception):
        def __init__(self):
            self.response = type("R", (), {"status_code": 404})()

    delete_calls = []
    monkeypatch.setattr(engine, "alpaca_get", lambda path: (_ for _ in ()).throw(_NotFound()))
    monkeypatch.setattr(engine, "alpaca_delete", lambda path: delete_calls.append(path) or {"id": "should-not-happen"})

    engine.process_force_close(conn, ticket)

    assert delete_calls == []  # DELETE never attempted
    with conn.cursor() as cur:
        cur.execute("SELECT status, exit_reason FROM nwt_portfolio_ledger WHERE position_id = %s", (position_id,))
        status, exit_reason = cur.fetchone()
    assert status == "closed"
    assert exit_reason == "already_closed_at_broker"


def test_force_close_preflight_proceeds_when_position_still_open(conn, monkeypatch):
    position_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (position_id, bot_source, asset, asset_type, direction, status) "
            "VALUES (%s, 'NWT_TRACK_C', 'AAPL260101C00500000', 'option', 'long', 'open')",
            (position_id,),
        )
    conn.commit()
    payload = {"symbol": "AAPL260101C00500000", "position_id": position_id}
    ticket_id = _insert_trade_request_ticket(conn, payload)
    ticket = {"ticket_id": ticket_id, "payload": payload}

    delete_calls = []
    monkeypatch.setattr(engine, "alpaca_get", lambda path: {"qty": "1"})  # still open
    monkeypatch.setattr(engine, "get_latest_quote", lambda symbol, asset_type: (5.0, 5.2))
    monkeypatch.setattr(engine, "alpaca_delete", lambda path: delete_calls.append(path) or {"id": "liq-1"})
    monkeypatch.setattr(engine, "renew_ticket_claim", lambda *a, **k: True)
    monkeypatch.setattr(
        engine, "poll_order_until_filled",
        lambda order_id: {"id": order_id, "status": "filled", "filled_avg_price": "5.1"},
    )

    engine.process_force_close(conn, ticket)

    assert len(delete_calls) == 1  # the real liquidation attempt still happens


# ---------------------------------------------------------------------------
# Equity monitor close (no ticket/claim on this path) — client_order_id
# keyed by position_id is the only duplicate-order guard available here.
# ---------------------------------------------------------------------------

def test_close_equity_position_recovers_existing_order(conn, monkeypatch):
    position_id = str(uuid.uuid4())
    place_close_order_calls = []
    monkeypatch.setattr(
        engine, "find_order_by_client_order_id",
        lambda coid: {"id": "existing-eq-order", "status": "accepted"},
    )
    monkeypatch.setattr(
        engine, "place_close_order",
        lambda *a, **k: place_close_order_calls.append(1) or {"id": "SHOULD_NOT_BE_CALLED"},
    )
    monkeypatch.setattr(
        engine, "poll_order_until_filled",
        lambda order_id: {"id": order_id, "status": "filled", "filled_avg_price": "101.0"},
    )
    pos = {"position_id": position_id, "asset": "AAPL"}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (position_id, bot_source, asset, asset_type, status) "
            "VALUES (%s, 'US_BOT', 'AAPL', 'equity', 'open')",
            (position_id,),
        )
    conn.commit()

    engine._close_equity_position(conn, pos, current_price=100.0, position_id=position_id,
                                  symbol="AAPL", notional=1000.0, entry_price=100.0, exit_reason="target")

    assert place_close_order_calls == []


def test_process_force_close_raises_and_writes_no_decision_on_lost_claim(conn, monkeypatch):
    position_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (position_id, bot_source, asset, asset_type, direction, status) "
            "VALUES (%s, 'NWT_TRACK_C', 'AAPL260101C00500000', 'option', 'long', 'open')",
            (position_id,),
        )
    conn.commit()

    payload = {"symbol": "AAPL260101C00500000", "position_id": position_id}
    ticket_id = _insert_trade_request_ticket(conn, payload)
    ticket = {"ticket_id": ticket_id, "payload": payload}

    monkeypatch.setattr(engine, "alpaca_get", lambda path: {"qty": "1"})  # pre-flight: still open
    monkeypatch.setattr(engine, "get_latest_quote", lambda symbol, asset_type: (5.0, 5.2))
    # The liquidation DELETE below is "real" — renewal reporting lost
    # ownership right after this must not be treated as an ordinary failure.
    monkeypatch.setattr(engine, "alpaca_delete", lambda path: {"id": "liquidation-order-1"})
    monkeypatch.setattr(engine, "renew_ticket_claim", lambda *a, **k: False)

    with pytest.raises(engine.ClaimLostError):
        engine.process_force_close(conn, ticket)

    with conn.cursor() as cur:
        cur.execute("SELECT decision FROM nwt_ticket_decisions WHERE ticket_id = %s", (ticket_id,))
        decisions = cur.fetchall()
    assert decisions == []
