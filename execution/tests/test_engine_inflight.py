"""
execution/tests/test_engine_inflight.py
Regression tests for in-flight order tracking — the fix for the 2026-07-16
incident where the engine marked tickets FAILED after its 30s poll while the
order stayed live at Alpaca (BHP/EWA/RIO filled at the open untracked →
recon halt; close retries 422'd against the first, still-working close).

Needs NWT_TEST_DB_DSN (throwaway local/CI Postgres — never production).
Skips cleanly if unset. All Alpaca I/O is monkeypatched; the DB is real
because the contract under test is "every submitted order ends as EXECUTED,
FAILED, or a pending nwt_inflight_orders row".
"""

import json
import os
import sys
import uuid
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

TEST_DSN = os.environ.get("NWT_TEST_DB_DSN")

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "execution"))

# engine.py reads its env at import time — supply harmless test values
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.test.invalid")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("NWT_DB_DSN", TEST_DSN or "postgresql://invalid/invalid")

import engine  # noqa: E402

SCHEMA_FILES = [
    "db/schema.sql",
    "db/migrate_phase0.sql",
    "db/migrate_2026_07_audit_fixes.sql",
    "db/migrate_2026_07_inflight_orders.sql",
]


@pytest.fixture()
def conn():
    if not TEST_DSN:
        pytest.skip("NWT_TEST_DB_DSN not set — skipping DB-backed engine tests")
    c = psycopg2.connect(TEST_DSN)
    try:
        with c.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            for rel in SCHEMA_FILES:
                cur.execute((REPO_ROOT / rel).read_text())
        c.commit()
        yield c
    finally:
        c.rollback()
        c.close()


def _insert_trade_request(conn, payload: dict) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (from_agent, to_agent, type, payload) "
            "VALUES ('AUS_EXECUTOR', 'EXECUTION_ENGINE', 'TRADE_REQUEST', %s) RETURNING ticket_id",
            (json.dumps(payload),),
        )
        tid = str(cur.fetchone()[0])
    conn.commit()
    return tid


def _decisions(conn, ticket_id: str) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT decision, reasoning FROM nwt_ticket_decisions "
            "WHERE ticket_id=%s ORDER BY created_at",
            (ticket_id,),
        )
        return cur.fetchall()


def _inflight(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_inflight_orders ORDER BY created_at")
        return [dict(r) for r in cur.fetchall()]


def _ledger(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_portfolio_ledger ORDER BY created_at")
        return [dict(r) for r in cur.fetchall()]


EQUITY_PAYLOAD = {
    "approved": True,
    "bot_source": "AUS_BOT",
    "symbol": "BHP",
    "direction": "long",
    "strategy_id": "AUS-DIV-001",
    "sized_notional": 5000,
    "asset_type": "equity",
    "time_in_force": "gtc",
}


def _neutralize_gates(monkeypatch):
    monkeypatch.setattr(engine, "synchronous_risk_veto", lambda conn, payload: (False, ""))
    monkeypatch.setattr(engine, "check_directional_cap",
                        lambda conn, d, n: (False, n, 1e9))
    monkeypatch.setattr(engine, "get_latest_quote", lambda s, t: (49.9, 50.1))
    monkeypatch.setattr(engine, "get_current_price", lambda s: 50.0)


def test_unfilled_entry_is_tracked_not_failed(conn, monkeypatch):
    """The exact incident shape: GTC market order can't fill in the poll
    window → must become SUBMITTED + in-flight row, never FAILED."""
    _neutralize_gates(monkeypatch)
    monkeypatch.setattr(engine, "place_equity_order", lambda p: {"id": "ord-1"})
    monkeypatch.setattr(engine, "poll_order_until_filled",
                        lambda oid: {"id": oid, "status": "new", "filled_qty": "0"})

    ticket_id = _insert_trade_request(conn, EQUITY_PAYLOAD)
    engine.process_ticket(conn, {"ticket_id": ticket_id, "payload": EQUITY_PAYLOAD}, {})

    decisions = _decisions(conn, ticket_id)
    assert [d[0] for d in decisions] == ["SUBMITTED"]
    rows = _inflight(conn)
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    assert rows[0]["alpaca_order_id"] == "ord-1"
    assert rows[0]["kind"] == "entry"
    assert _ledger(conn) == []  # no fill yet — nothing in the ledger


def test_resolver_writes_ledger_on_late_fill(conn, monkeypatch):
    """The order fills at the open, cycles later — resolver must produce the
    identical ledger row an immediate fill would have."""
    _neutralize_gates(monkeypatch)
    monkeypatch.setattr(engine, "place_equity_order", lambda p: {"id": "ord-2"})
    monkeypatch.setattr(engine, "poll_order_until_filled",
                        lambda oid: {"id": oid, "status": "accepted", "filled_qty": "0"})
    ticket_id = _insert_trade_request(conn, EQUITY_PAYLOAD)
    engine.process_ticket(conn, {"ticket_id": ticket_id, "payload": EQUITY_PAYLOAD}, {})

    monkeypatch.setattr(engine, "alpaca_get", lambda path: {
        "id": "ord-2", "status": "filled",
        "filled_avg_price": "50.25", "filled_qty": "99",
    })
    engine.resolve_inflight_orders(conn)

    rows = _inflight(conn)
    assert rows[0]["status"] == "resolved" and rows[0]["resolution"] == "filled"
    ledger = _ledger(conn)
    assert len(ledger) == 1
    assert ledger[0]["asset"] == "BHP"
    assert float(ledger[0]["qty"]) == 99
    assert float(ledger[0]["entry_price"]) == 50.25
    assert ledger[0]["status"] == "open"
    assert [d[0] for d in _decisions(conn, ticket_id)] == ["SUBMITTED", "EXECUTED"]


def test_resolver_retires_dead_order(conn, monkeypatch):
    _neutralize_gates(monkeypatch)
    monkeypatch.setattr(engine, "place_equity_order", lambda p: {"id": "ord-3"})
    monkeypatch.setattr(engine, "poll_order_until_filled",
                        lambda oid: {"id": oid, "status": "new", "filled_qty": "0"})
    ticket_id = _insert_trade_request(conn, EQUITY_PAYLOAD)
    engine.process_ticket(conn, {"ticket_id": ticket_id, "payload": EQUITY_PAYLOAD}, {})

    monkeypatch.setattr(engine, "alpaca_get", lambda path: {
        "id": "ord-3", "status": "expired", "filled_qty": "0",
    })
    engine.resolve_inflight_orders(conn)

    rows = _inflight(conn)
    assert rows[0]["status"] == "dead"
    assert _ledger(conn) == []
    assert [d[0] for d in _decisions(conn, ticket_id)] == ["SUBMITTED", "FAILED"]


def test_close_request_skips_when_close_already_working(conn, monkeypatch):
    """The 422 loop: a second close must never be stacked on a working one."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (bot_source, asset, asset_type, direction, "
            "qty, entry_price, status) VALUES ('NWT_TRACK_C','AAPL260717C00312500','option',"
            "'long', 3, 2.5, 'open') RETURNING position_id"
        )
        position_id = str(cur.fetchone()[0])
    conn.commit()
    engine.record_inflight_order(conn, None, "prior-close-1", "close",
                                 {"symbol": "AAPL260717C00312500", "asset_type": "option"},
                                 position_id=position_id, exit_reason="hard_close")

    def _must_not_place(*a, **k):
        raise AssertionError("a second close order was placed while one was working")
    monkeypatch.setattr(engine, "place_close_order", _must_not_place)

    payload = {"symbol": "AAPL260717C00312500", "position_id": position_id,
               "asset_type": "option", "qty": 3, "exit_reason": "hard_close"}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (from_agent, to_agent, type, payload) "
            "VALUES ('RISK_AGENT','EXECUTION_ENGINE','CLOSE_REQUEST', %s) RETURNING ticket_id",
            (json.dumps(payload),),
        )
        ticket_id = str(cur.fetchone()[0])
    conn.commit()

    engine.process_close_ticket(conn, {"ticket_id": ticket_id, "payload": payload})
    assert [d[0] for d in _decisions(conn, ticket_id)] == ["SKIPPED"]


def test_inflight_close_resolution_closes_ledger(conn, monkeypatch):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (bot_source, asset, asset_type, direction, "
            "qty, entry_price, notional_risk, status) VALUES ('NWT_TRACK_C','SPY260814C00640000',"
            "'option','long', 2, 3.0, 600, 'open') RETURNING position_id"
        )
        position_id = str(cur.fetchone()[0])
    conn.commit()
    engine.record_inflight_order(conn, None, "close-9", "close",
                                 {"symbol": "SPY260814C00640000", "asset_type": "option",
                                  "strategy_id": "C1"},
                                 position_id=position_id, exit_reason="target")

    monkeypatch.setattr(engine, "alpaca_get", lambda path: {
        "id": "close-9", "status": "filled",
        "filled_avg_price": "4.50", "filled_qty": "2",
    })
    engine.resolve_inflight_orders(conn)

    ledger = _ledger(conn)
    assert ledger[0]["status"] == "closed"
    assert float(ledger[0]["exit_price"]) == 4.50
    assert ledger[0]["exit_reason"] == "target"
    with conn.cursor() as cur:
        cur.execute("SELECT pnl FROM nwt_trade_outcomes WHERE position_id=%s", (position_id,))
        row = cur.fetchone()
    assert row is not None
    # long 2 contracts, 3.00 -> 4.50 = +1.50 * 100 * 2
    assert float(row[0]) == pytest.approx(300.0)


def test_short_equity_position_is_closed_by_buying(conn, monkeypatch):
    """Covering a short must BUY — always selling doubles the short."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (bot_source, asset, asset_type, direction, "
            "qty, entry_price, notional_risk, status) VALUES ('EU_BOT','VGK','equity',"
            "'short', 40, 70.0, 2800, 'open') RETURNING position_id"
        )
        position_id = str(cur.fetchone()[0])
    conn.commit()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        pos = dict(cur.fetchone())

    seen = {}

    def fake_place_close(symbol, qty, asset_type, side="sell"):
        seen["side"], seen["qty"] = side, qty
        return {"id": "cover-1"}

    monkeypatch.setattr(engine, "place_close_order", fake_place_close)
    monkeypatch.setattr(engine, "poll_order_until_filled",
                        lambda oid: {"id": oid, "status": "filled",
                                     "filled_avg_price": "65.00", "filled_qty": "40"})

    engine._close_equity_position(conn, pos, 65.0, position_id, "VGK", 2800, 70.0, "target")

    assert seen["side"] == "buy"
    assert seen["qty"] == 40  # real ledger qty, not recomputed from notional
    assert _ledger(conn)[0]["status"] == "closed"
