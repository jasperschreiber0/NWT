"""
nwt_agents/tests/test_broker_aware_reconciliation.py
Regression tests for the fixes to the 2026-07-22 AAPL/BHP incidents:
  1. Broker-aware position intent validation (execution/engine.py) —
     never record a new short/long position from a fill unless the
     broker's own pre-trade position confirms genuinely new exposure.
  2. UNATTRIBUTED notional counts against available capital
     (execution/engine.py::get_unattributed_notional,
     master/strategist.py::compute_exposure).
  3. Close-workflow guards (execution/engine.py::process_close_ticket) —
     refuses an already-closed position or a broker mismatch instead of
     submitting a doomed/phantom-creating order.

Run against a throwaway Postgres (NWT_TEST_DB_DSN):
    NWT_TEST_DB_DSN=postgresql://nwt_test:nwt_test_pw@localhost/nwt_findings_test \
        pytest nwt_agents/tests/test_broker_aware_reconciliation.py -v
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from psycopg2.extras import RealDictCursor

_NWT_AGENTS_DIR = Path(__file__).parent.parent
_REPO_ROOT = _NWT_AGENTS_DIR.parent
sys.path.insert(0, str(_NWT_AGENTS_DIR))
sys.path.insert(0, str(_REPO_ROOT / "execution"))
sys.path.insert(0, str(_REPO_ROOT / "master"))

os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.test.invalid")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("NWT_DB_DSN", os.environ.get("NWT_TEST_DB_DSN", "postgresql://invalid/invalid"))

import engine  # noqa: E402
import strategist  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_ledger_row(conn, asset, direction, qty, bot_source="TEST_BOT",
                       entry_price=100.0, status="open"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_portfolio_ledger
                (bot_source, asset, asset_type, direction, qty, entry_price,
                 notional_risk, status)
            VALUES (%s, %s, 'equity', %s, %s, %s, %s, %s)
            RETURNING position_id
            """,
            (bot_source, asset, direction, qty, entry_price, qty * entry_price, status),
        )
        position_id = str(cur.fetchone()[0])
    conn.commit()
    return position_id


def _insert_ticket(conn, ticket_type, payload):
    import json
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (from_agent, to_agent, type, payload) "
            "VALUES ('TEST', 'EXECUTION_ENGINE', %s, %s) RETURNING ticket_id",
            (ticket_type, json.dumps(payload)),
        )
        ticket_id = str(cur.fetchone()[0])
    conn.commit()
    return ticket_id


# ---------------------------------------------------------------------------
# 1. Selling part of an existing long
# ---------------------------------------------------------------------------

def test_selling_part_of_existing_long_reduces_not_opens_short():
    result = engine.classify_equity_exposure(
        broker_pos_before={"qty": 300, "side": "long"}, side="sell", filled_qty=14,
    )
    assert result["opening_qty"] == 0.0
    assert result["reducing_qty"] == 14.0
    assert result["reduces_existing"] is True


def test_selling_more_than_existing_long_opens_the_excess_as_a_new_short():
    result = engine.classify_equity_exposure(
        broker_pos_before={"qty": 10, "side": "long"}, side="sell", filled_qty=14,
    )
    assert result["reducing_qty"] == 10.0
    assert result["opening_qty"] == 4.0
    assert result["reduces_existing"] is True


def test_record_entry_fill_reduces_existing_ledger_row_not_a_new_short(conn):
    # The exact AAPL shape: a 300-share UNATTRIBUTED long already exists;
    # a "short 14" entry ticket's fill must reduce it, not create a phantom short.
    existing_id = _insert_ledger_row(conn, "AAPL", "long", 300, bot_source="UNATTRIBUTED",
                                     entry_price=317.5)
    ticket_id = _insert_ticket(conn, "TRADE_REQUEST", {})

    payload = {
        "asset_type": "equity", "symbol": "AAPL", "direction": "short",
        "bot_source": "US_BOT", "strategy_id": "US-ORB-001", "sized_notional": 4536.0,
        "broker_position_before_entry": {"qty": 300, "side": "long"},
    }
    filled_order = {"filled_avg_price": "323.855714", "filled_qty": "14"}

    engine.record_entry_fill(conn, ticket_id, payload, filled_order, "order-abc")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE asset='AAPL' ORDER BY entry_time")
        rows = [dict(r) for r in cur.fetchall()]

    # No new short row — only the original (now-reduced) long row exists.
    assert len(rows) == 1
    assert rows[0]["position_id"] == existing_id
    assert rows[0]["direction"] == "long"
    assert float(rows[0]["qty"]) == 286.0
    assert rows[0]["status"] == "open"

    with conn.cursor() as cur:
        cur.execute("SELECT decision FROM nwt_ticket_decisions WHERE ticket_id=%s", (ticket_id,))
        decisions = [r[0] for r in cur.fetchall()]
    assert decisions == ["EXECUTED"]


# ---------------------------------------------------------------------------
# 2. Opening a genuine short
# ---------------------------------------------------------------------------

def test_opening_genuine_short_when_broker_is_flat():
    result = engine.classify_equity_exposure(broker_pos_before=None, side="sell", filled_qty=10)
    assert result["opening_qty"] == 10.0
    assert result["reducing_qty"] == 0.0
    assert result["reduces_existing"] is False


def test_record_entry_fill_opens_a_real_new_short_when_broker_is_flat(conn):
    ticket_id = _insert_ticket(conn, "TRADE_REQUEST", {})
    payload = {
        "asset_type": "equity", "symbol": "BHP", "direction": "short",
        "bot_source": "AUS_BOT", "strategy_id": "AUS-1", "sized_notional": 800.0,
        "broker_position_before_entry": None,
    }
    filled_order = {"filled_avg_price": "84.48", "filled_qty": "10"}

    engine.record_entry_fill(conn, ticket_id, payload, filled_order, "order-xyz")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE asset='BHP'")
        rows = [dict(r) for r in cur.fetchall()]
    assert len(rows) == 1
    assert rows[0]["direction"] == "short"
    assert float(rows[0]["qty"]) == 10.0


# ---------------------------------------------------------------------------
# 3. Closing a genuine short (regression — must not be blocked by the new guards)
# ---------------------------------------------------------------------------

def test_closing_a_genuine_short_proceeds_when_broker_confirms_it(conn):
    position_id = _insert_ledger_row(conn, "BHP", "short", 10, bot_source="RECON_RECOVERED",
                                     entry_price=84.48)
    ticket = {
        "ticket_id": str(uuid.uuid4()),
        "payload": {"symbol": "BHP", "position_id": position_id, "exit_reason": "target",
                   "asset_type": "equity", "qty": 10},
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (ticket_id, from_agent, to_agent, type, payload) "
            "VALUES (%s, 'X', 'EXECUTION_ENGINE', 'CLOSE_REQUEST', '{}')",
            (ticket["ticket_id"],),
        )
    conn.commit()

    with patch.object(engine, "get_alpaca_position", return_value={"qty": 10, "side": "short"}), \
         patch.object(engine, "has_pending_inflight_close", return_value=False), \
         patch.object(engine, "get_open_orders", return_value=[]), \
         patch.object(engine, "client_order_id_for", return_value="nwt-close-test"), \
         patch.object(engine, "record_client_order_id"), \
         patch.object(engine, "find_order_by_client_order_id", return_value=None), \
         patch.object(engine, "place_close_order", return_value={"id": "order-cover"}), \
         patch.object(engine, "record_execution_attempt"), \
         patch.object(engine, "renew_ticket_claim", return_value=True), \
         patch.object(engine, "poll_order_until_filled",
                      return_value={"status": "filled", "filled_avg_price": "83.00", "filled_qty": "10"}), \
         patch.object(engine, "record_force_close_outcome"):
        engine.process_close_ticket(conn, ticket)

    with conn.cursor() as cur:
        cur.execute("SELECT decision FROM nwt_ticket_decisions WHERE ticket_id=%s", (ticket["ticket_id"],))
        decisions = [r[0] for r in cur.fetchall()]
    assert decisions == ["EXECUTED"]  # not blocked by the new broker-mismatch guard

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        (status,) = cur.fetchone()
    assert status == "closed"


# ---------------------------------------------------------------------------
# 4. Buying while no short exists
# ---------------------------------------------------------------------------

def test_buying_while_no_short_exists_is_a_plain_new_long():
    result = engine.classify_equity_exposure(broker_pos_before=None, side="buy", filled_qty=50)
    assert result["opening_qty"] == 50.0
    assert result["reduces_existing"] is False


def test_buying_while_broker_already_long_is_a_plain_new_long_not_a_close():
    # Buying more of an existing LONG is not "closing a short" — must not
    # be misclassified as a reduction just because a position already exists.
    result = engine.classify_equity_exposure(
        broker_pos_before={"qty": 100, "side": "long"}, side="buy", filled_qty=20,
    )
    assert result["opening_qty"] == 20.0
    assert result["reduces_existing"] is False


def test_process_close_ticket_refuses_to_submit_when_broker_exposure_absent(conn):
    # The exact BHP incident shape: ledger believes a position is open, but
    # the broker holds nothing (or the wrong thing) — must not submit an
    # order that would either be rejected or create phantom exposure.
    position_id = _insert_ledger_row(conn, "BHP", "long", 29, bot_source="UNATTRIBUTED",
                                     entry_price=82.10)
    ticket = {
        "ticket_id": str(uuid.uuid4()),
        "payload": {"symbol": "BHP", "position_id": position_id, "exit_reason": "hard_close",
                   "asset_type": "equity", "qty": 29},
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (ticket_id, from_agent, to_agent, type, payload) "
            "VALUES (%s, 'X', 'EXECUTION_ENGINE', 'CLOSE_REQUEST', '{}')",
            (ticket["ticket_id"],),
        )
    conn.commit()

    with patch.object(engine, "get_alpaca_position", return_value={"qty": 10, "side": "short"}) as mock_get, \
         patch.object(engine, "place_close_order") as mock_place:
        engine.process_close_ticket(conn, ticket)

    mock_get.assert_called_once()
    mock_place.assert_not_called()  # never reaches the broker

    with conn.cursor() as cur:
        cur.execute("SELECT decision FROM nwt_ticket_decisions WHERE ticket_id=%s", (ticket["ticket_id"],))
        decisions = [r[0] for r in cur.fetchall()]
    assert decisions == ["FAILED_REQUIRES_HUMAN"]

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        (status,) = cur.fetchone()
    assert status == "open"  # untouched — no phantom close


def test_process_close_ticket_skips_an_already_closed_position(conn):
    position_id = _insert_ledger_row(conn, "BHP", "long", 10, entry_price=84.0, status="closed")
    ticket = {
        "ticket_id": str(uuid.uuid4()),
        "payload": {"symbol": "BHP", "position_id": position_id, "exit_reason": "target",
                   "asset_type": "equity", "qty": 10},
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (ticket_id, from_agent, to_agent, type, payload) "
            "VALUES (%s, 'X', 'EXECUTION_ENGINE', 'CLOSE_REQUEST', '{}')",
            (ticket["ticket_id"],),
        )
    conn.commit()

    with patch.object(engine, "get_alpaca_position") as mock_get, \
         patch.object(engine, "place_close_order") as mock_place:
        engine.process_close_ticket(conn, ticket)

    mock_get.assert_not_called()  # short-circuits before even checking the broker
    mock_place.assert_not_called()

    with conn.cursor() as cur:
        cur.execute("SELECT decision FROM nwt_ticket_decisions WHERE ticket_id=%s", (ticket["ticket_id"],))
        decisions = [r[0] for r in cur.fetchall()]
    assert decisions == ["SKIPPED"]


# ---------------------------------------------------------------------------
# 5. UNATTRIBUTED capital affecting allocation
# ---------------------------------------------------------------------------

def test_get_unattributed_notional_sums_only_open_unattributed_rows(conn):
    _insert_ledger_row(conn, "AAPL", "long", 286, bot_source="UNATTRIBUTED", entry_price=317.5)
    _insert_ledger_row(conn, "SPY", "long", 10, bot_source="US_BOT", entry_price=500.0)
    _insert_ledger_row(conn, "EWA", "long", 999, bot_source="UNATTRIBUTED", entry_price=1.0,
                       status="closed")  # closed — must not count

    total = engine.get_unattributed_notional(conn)
    assert total == pytest.approx(286 * 317.5)


def test_check_bot_permissions_reduces_available_capital_for_unattributed_exposure(conn):
    _insert_ledger_row(conn, "AAPL", "long", 286, bot_source="UNATTRIBUTED", entry_price=317.5)
    directives = {"bot_permissions": {"us": {"status": "active", "capital_weight": 1.0, "size_cap": 1.0}}}

    with patch.object(engine, "get_alpaca_account_equity", return_value=97_000.0):
        # sized_notional larger than (97000 - unattributed) must be vetoed,
        # even though it would fit under raw equity alone.
        unattributed = 286 * 317.5  # ~90,755
        available = 97_000.0 - unattributed
        vetoed, reason = engine._check_bot_permissions(conn, directives, "US_BOT", available + 500)
        assert vetoed is True
        assert "unattributed" in reason.lower()

        vetoed2, _ = engine._check_bot_permissions(conn, directives, "US_BOT", available - 500)
        assert vetoed2 is False


def test_compute_exposure_surfaces_unattributed_notional_and_count():
    positions = [
        {"notional_risk": 90755.0, "delta_exposure": 1.0, "direction": "long",
         "asset_type": "equity", "bot_source": "UNATTRIBUTED"},
        {"notional_risk": 5000.0, "delta_exposure": -1.0, "direction": "short",
         "asset_type": "equity", "bot_source": "US_BOT"},
    ]
    exposure = strategist.compute_exposure(positions)
    assert exposure["unattributed_notional"] == pytest.approx(90755.0)
    assert exposure["unattributed_count"] == 1


def test_compute_exposure_zero_unattributed_when_none_present():
    positions = [
        {"notional_risk": 5000.0, "delta_exposure": 1.0, "direction": "long",
         "asset_type": "equity", "bot_source": "US_BOT"},
    ]
    exposure = strategist.compute_exposure(positions)
    assert exposure["unattributed_notional"] == 0.0
    assert exposure["unattributed_count"] == 0


# ---------------------------------------------------------------------------
# recon_agent — directional conflict + standing unattributed warning
# ---------------------------------------------------------------------------

def test_recon_flags_directional_conflict_distinctly_from_qty_mismatch(conn, monkeypatch):
    import recon_agent

    _insert_ledger_row(conn, "BHP", "long", 29, bot_source="UNATTRIBUTED", entry_price=82.10)

    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions",
                        lambda: [{"symbol": "BHP", "qty": "-10", "avg_entry_price": "84.48"}])
    monkeypatch.setattr(recon_agent, "set_no_trade_mode", lambda *a, **k: None)

    clean = recon_agent.run_recon(conn, "test")
    assert clean is False

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT payload FROM nwt_tickets WHERE type='recon_mismatch' ORDER BY created_at DESC LIMIT 1"
        )
        row = cur.fetchone()
    classes = {m["class"] for m in row["payload"]["mismatches"]}
    assert "directional_conflict" in classes
    assert "qty_mismatch" not in classes  # this specific case is a sign conflict, not just qty drift


def test_recon_warns_on_unattributed_exposure_even_when_qty_matches(conn, monkeypatch):
    import recon_agent

    _insert_ledger_row(conn, "AAPL", "long", 286, bot_source="UNATTRIBUTED", entry_price=317.5)

    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions",
                        lambda: [{"symbol": "AAPL", "qty": "286", "avg_entry_price": "317.5"}])

    clean = recon_agent.run_recon(conn, "test")
    # Net quantities match exactly — must not be critical/halt trading...
    assert clean is True

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT payload FROM nwt_tickets WHERE type='recon_mismatch' ORDER BY created_at DESC LIMIT 1"
        )
        row = cur.fetchone()
    # ...but the unattributed exposure must still be surfaced, not silently invisible.
    assert row is not None
    classes = {m["class"] for m in row["payload"]["mismatches"]}
    assert "unattributed_exposure" in classes
    assert row["payload"]["critical"] is False
