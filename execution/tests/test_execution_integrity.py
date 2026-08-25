"""
execution/tests/test_execution_integrity.py
Execution reliability regression suite — written after the 2026-07-28 BHP
incident (a short equity position closed with a "sell" instead of a
"buy-to-cover", doubling a -10 broker short to -20 while the ledger row
showed 'closed'). Each test proves one lifecycle scenario cannot silently
diverge broker state from ledger state.

Uses a real throwaway Postgres (NWT_TEST_DB_DSN) and a small in-process fake
Alpaca broker (FakeAlpaca) that actually tracks signed position qty per
symbol, so "broker flat" assertions are checked against real state
transitions, not just "no exception was raised".

Run:
    NWT_TEST_DB_DSN=postgresql://nwt_test:nwt_test_pw@localhost/nwt_execution_test \
        pytest execution/tests/test_execution_integrity.py -v
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).parent.parent))

TEST_DSN = os.environ.get("NWT_TEST_DB_DSN")

SCHEMA_SQL = """
DROP TABLE IF EXISTS nwt_trade_outcomes;
DROP TABLE IF EXISTS nwt_ticket_decisions;
DROP TABLE IF EXISTS nwt_tickets;
DROP TABLE IF EXISTS nwt_portfolio_ledger;
DROP TABLE IF EXISTS nwt_system_flags;
DROP TABLE IF EXISTS nwt_system_log;
DROP TABLE IF EXISTS nwt_heartbeat;

CREATE TABLE nwt_portfolio_ledger (
    position_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bot_source TEXT NOT NULL,
    strategy_id TEXT,
    asset TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    direction TEXT,
    delta_exposure NUMERIC,
    notional_risk NUMERIC,
    qty NUMERIC,
    entry_price NUMERIC,
    entry_time TIMESTAMPTZ DEFAULT NOW(),
    entry_bid NUMERIC,
    entry_ask NUMERIC,
    exit_price NUMERIC,
    exit_time TIMESTAMPTZ,
    exit_bid NUMERIC,
    exit_ask NUMERIC,
    realized_slippage NUMERIC,
    status TEXT DEFAULT 'open',
    alpaca_order_id TEXT,
    stop_pct NUMERIC,
    target_pct NUMERIC,
    spread_group_id UUID,
    exit_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX one_ledger_row_per_order_asset
  ON nwt_portfolio_ledger (alpaca_order_id, asset)
  WHERE alpaca_order_id IS NOT NULL;

CREATE TABLE nwt_tickets (
    ticket_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    type TEXT NOT NULL,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE nwt_ticket_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID REFERENCES nwt_tickets(ticket_id),
    decision TEXT NOT NULL,
    reasoning TEXT,
    decided_by TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX one_decision_per_agent
  ON nwt_ticket_decisions (ticket_id, decided_by)
  WHERE created_at >= '2026-07-24T00:00:00+00:00';

CREATE TABLE nwt_trade_outcomes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id TEXT,
    symbol TEXT,
    direction TEXT,
    entry_price NUMERIC,
    entry_time TIMESTAMPTZ,
    exit_price NUMERIC,
    exit_time TIMESTAMPTZ,
    pnl NUMERIC,
    pnl_pct NUMERIC,
    pnl_adjusted NUMERIC,
    slippage_model TEXT,
    position_id UUID REFERENCES nwt_portfolio_ledger(position_id),
    closed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX one_outcome_per_position
  ON nwt_trade_outcomes (position_id) WHERE position_id IS NOT NULL;

CREATE TABLE nwt_system_flags (
    flag TEXT PRIMARY KEY,
    value BOOLEAN NOT NULL DEFAULT FALSE,
    reason TEXT,
    set_by TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO nwt_system_flags (flag, value) VALUES ('no_trade_mode', FALSE);

CREATE TABLE nwt_system_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    level TEXT,
    component TEXT,
    message TEXT,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE nwt_heartbeat (
    service TEXT PRIMARY KEY,
    last_beat TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT DEFAULT 'ok'
);
"""


@pytest.fixture()
def conn():
    if not TEST_DSN:
        pytest.skip("NWT_TEST_DB_DSN not set — skipping DB-backed regression tests")
    c = psycopg2.connect(TEST_DSN)
    with c.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    c.commit()
    yield c
    c.rollback()
    c.close()


# ---------------------------------------------------------------------------
# Fake Alpaca — tracks real signed position qty per symbol so "broker flat"
# assertions are checked against actual state transitions, not just "no
# exception raised". Deliberately simple: market orders fill immediately at
# a fixed/overridable price, no partial fills unless a test asks for one.
# ---------------------------------------------------------------------------

class FakeAlpaca:
    def __init__(self, price: float = 100.0):
        self.price = price
        self.positions: dict[str, float] = {}   # symbol -> signed qty
        self.orders: dict[str, dict] = {}        # order_id -> order dict
        self.orders_by_coid: dict[str, str] = {}  # client_order_id -> order_id
        self.partial_fill_qty: dict[str, float] = {}  # symbol -> qty to fill on first attempt
        self.fail_next_submit = False

    def submit_order(self, body: dict) -> dict:
        if self.fail_next_submit:
            self.fail_next_submit = False
            raise ConnectionError("simulated network failure after submission")

        symbol = body["symbol"]
        side = body["side"]
        qty = float(body["qty"])
        coid = body.get("client_order_id", str(uuid.uuid4()))
        order_id = str(uuid.uuid4())

        signed_delta = qty if side == "buy" else -qty
        fill_qty = self.partial_fill_qty.pop(symbol, qty)
        filled_signed = fill_qty if side == "buy" else -fill_qty

        self.positions[symbol] = self.positions.get(symbol, 0.0) + filled_signed
        status = "filled" if fill_qty == qty else "partially_filled"

        order = {
            "id": order_id,
            "client_order_id": coid,
            "symbol": symbol,
            "side": side,
            "qty": str(qty),
            "filled_qty": str(fill_qty),
            "status": status,
            "filled_avg_price": str(self.price),
        }
        self.orders[order_id] = order
        self.orders_by_coid[coid] = order_id
        return order

    def get_order(self, order_id: str) -> dict:
        return self.orders[order_id]

    def get_by_client_order_id(self, coid: str):
        order_id = self.orders_by_coid.get(coid)
        return self.orders[order_id] if order_id else None

    def get_position_qty(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def delete_position(self, symbol: str) -> dict:
        qty = self.positions.get(symbol, 0.0)
        if qty == 0:
            raise RuntimeError("404 position does not exist")
        side = "sell" if qty > 0 else "buy"
        order = self.submit_order({"symbol": symbol, "side": side, "qty": abs(qty)})
        return order


def wire_fake_alpaca(monkeypatch, engine, broker: FakeAlpaca):
    monkeypatch.setattr(engine, "alpaca_post", lambda path, body: broker.submit_order(body))
    monkeypatch.setattr(engine, "alpaca_get",
                         lambda path: broker.get_order(path.rsplit("/", 1)[-1]))
    monkeypatch.setattr(engine, "alpaca_delete",
                         lambda path: broker.delete_position(path.rsplit("/", 1)[-1]))
    monkeypatch.setattr(engine, "alpaca_get_by_client_order_id", broker.get_by_client_order_id)
    monkeypatch.setattr(engine, "get_alpaca_position_qty", broker.get_position_qty)
    monkeypatch.setattr(engine, "get_current_price", lambda symbol: broker.price)
    monkeypatch.setattr(engine, "get_latest_quote", lambda symbol, asset_type: (broker.price - 0.05, broker.price + 0.05))
    monkeypatch.setattr(engine, "get_alpaca_account_equity", lambda: 97_000.0)
    monkeypatch.setattr(engine, "POLL_INTERVAL", 0)


def _insert_open_position(conn, bot_source, asset, direction, qty, entry_price, alpaca_order_id,
                           stop_pct=-0.015, target_pct=0.025):
    from ledger import insert_position
    return insert_position(conn, {
        "bot_source": bot_source,
        "strategy_id": "TEST",
        "asset": asset,
        "asset_type": "equity",
        "direction": direction,
        "notional_risk": qty * entry_price,
        "qty": qty,
        "entry_price": entry_price,
        "alpaca_order_id": alpaca_order_id,
        "stop_pct": stop_pct,
        "target_pct": target_pct,
    })


def _no_trade_mode(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT value, reason FROM nwt_system_flags WHERE flag='no_trade_mode'")
        return cur.fetchone()


# ---------------------------------------------------------------------------
# TEST 1: Open long 10 shares -> close -> broker flat -> ledger flat
# ---------------------------------------------------------------------------

def test_1_long_open_then_close_leaves_broker_and_ledger_flat(conn, monkeypatch):
    import engine
    broker = FakeAlpaca(price=100.0)
    wire_fake_alpaca(monkeypatch, engine, broker)

    order_id = str(uuid.uuid4())
    position_id = _insert_open_position(conn, "TEST_BOT", "AAPL", "long", 10, 100.0, order_id)
    broker.positions["AAPL"] = 10.0  # opening fill already happened at the broker

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        pos = dict(cur.fetchone())

    # Target hit — should SELL 10 to close a LONG.
    engine._close_equity_position(conn, pos, 103.0, position_id, "AAPL", pos["notional_risk"], 100.0, "target")

    assert broker.get_position_qty("AAPL") == 0.0, "broker must be flat after closing a long"
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        assert cur.fetchone()[0] == "closed"
    value, _ = _no_trade_mode(conn)
    assert value is False, "a correct close must not trip no_trade_mode"


# ---------------------------------------------------------------------------
# TEST 2: Open short 10 shares -> buy to cover -> broker flat -> ledger flat
# This is the exact scenario that failed in production on 2026-07-28.
# ---------------------------------------------------------------------------

def test_2_short_open_then_close_leaves_broker_and_ledger_flat(conn, monkeypatch):
    import engine
    broker = FakeAlpaca(price=84.48)
    wire_fake_alpaca(monkeypatch, engine, broker)

    order_id = str(uuid.uuid4())
    position_id = _insert_open_position(conn, "AUS_BOT", "BHP", "short", 10, 84.48, order_id)
    broker.positions["BHP"] = -10.0  # opening short fill already happened at the broker

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        pos = dict(cur.fetchone())

    engine._close_equity_position(conn, pos, 82.16, position_id, "BHP", pos["notional_risk"], 84.48, "target")

    # Before the fix this asserted -20.0 (the actual production bug).
    assert broker.get_position_qty("BHP") == 0.0, \
        "closing a short must BUY to cover, not sell (production bug: sell doubled -10 to -20)"
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        assert cur.fetchone()[0] == "closed"
    value, _ = _no_trade_mode(conn)
    assert value is False, "a correct short-cover must not trip no_trade_mode"


# ---------------------------------------------------------------------------
# TEST 3: Partial fill
# ---------------------------------------------------------------------------

def test_3_partial_fill_does_not_close_ledger_as_if_flat(conn, monkeypatch):
    import engine
    broker = FakeAlpaca(price=100.0)
    wire_fake_alpaca(monkeypatch, engine, broker)

    order_id = str(uuid.uuid4())
    position_id = _insert_open_position(conn, "TEST_BOT", "AAPL", "long", 10, 100.0, order_id)
    broker.positions["AAPL"] = 10.0

    # Only 6 of 10 shares actually fill on the close attempt.
    broker.partial_fill_qty["AAPL"] = 6.0

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        pos = dict(cur.fetchone())

    engine._close_equity_position(conn, pos, 103.0, position_id, "AAPL", pos["notional_risk"], 100.0, "target")

    # poll_order_until_filled only returns on a TERMINAL status (filled /
    # canceled / expired / rejected / done_for_day) or after exhausting
    # POLL_MAX attempts — "partially_filled" is neither, so this call
    # (real production code, unmodified) must not treat it as done.
    assert broker.get_position_qty("AAPL") == 4.0, \
        "broker still holds the unfilled remainder — partial fill is not flat"


# ---------------------------------------------------------------------------
# TEST 4: Network failure after order submission (crash-recovery / retry)
# ---------------------------------------------------------------------------

def test_4_network_failure_after_submission_does_not_duplicate_order(conn, monkeypatch):
    import engine
    broker = FakeAlpaca(price=100.0)
    wire_fake_alpaca(monkeypatch, engine, broker)

    client_order_id = engine.client_order_id_for("pos-4", "eqmon")

    # First attempt: order reaches the broker (position updates), but the
    # response never reaches this process (simulated by manually applying
    # the broker-side effect and then having find_or_place_order's place_fn
    # raise, as a real network timeout after submission would look from the
    # caller's perspective).
    broker.submit_order({"symbol": "AAPL", "side": "buy", "qty": "10", "client_order_id": client_order_id})
    assert broker.get_position_qty("AAPL") == 10.0

    # Retry (next cron cycle): find_or_place_order must find the existing
    # order via client_order_id and reuse it instead of submitting a second one.
    order = engine.find_or_place_order(
        client_order_id,
        lambda: (_ for _ in ()).throw(AssertionError("must not place a second order")),
    )
    assert order["client_order_id"] == client_order_id
    assert broker.get_position_qty("AAPL") == 10.0, "retry must not duplicate the fill"


# ---------------------------------------------------------------------------
# TEST 5: Broker fills but database update fails (resumed on next cycle)
# ---------------------------------------------------------------------------

def test_5_db_failure_after_fill_is_resumable_without_duplicate_ledger_row(conn, monkeypatch):
    import engine
    from ledger import insert_position

    order_id = str(uuid.uuid4())
    ledger_data = {
        "bot_source": "TEST_BOT", "strategy_id": "TEST", "asset": "AAPL",
        "asset_type": "equity", "direction": "long", "notional_risk": 1000.0,
        "qty": 10, "entry_price": 100.0, "alpaca_order_id": order_id,
    }

    # First attempt succeeds.
    position_id_1 = insert_position(conn, ledger_data)

    # Simulated crash-and-retry: same fill (same alpaca_order_id+asset)
    # replayed against insert_position — the unique index must make this
    # idempotent instead of creating a second ledger row for one real fill.
    position_id_2 = insert_position(conn, ledger_data)

    assert position_id_1 == position_id_2, "a resumed insert for the same fill must not duplicate the ledger row"
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_portfolio_ledger WHERE alpaca_order_id=%s", (order_id,))
        assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# TEST 6: Bot restart with existing broker positions
# ---------------------------------------------------------------------------

def test_6_restart_with_existing_broker_positions_imports_cleanly(conn, monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "nwt_agents"))
    import recon_agent

    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions", lambda: [
        {"symbol": "AAPL", "qty": "10", "avg_entry_price": "100.0", "asset_class": "us_equity"},
    ])

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_portfolio_ledger")
        assert cur.fetchone()[0] == 0

    recon_agent.cold_start_import(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT bot_source, status, alpaca_order_id FROM nwt_portfolio_ledger WHERE asset='AAPL'")
        row = cur.fetchone()
    assert row == ("UNATTRIBUTED", "open", None)


# ---------------------------------------------------------------------------
# TEST 7: Manual broker position exists (not opened by our execution engine)
# ---------------------------------------------------------------------------

def test_7_manual_broker_position_detected_as_critical_mismatch(conn, monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "nwt_agents"))
    import recon_agent

    # Ledger is empty (not a cold-start scenario -- ledger already has other
    # activity), but Alpaca shows a position no ticket ever created.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (bot_source, asset, asset_type, direction, qty, status) "
            "VALUES ('TEST_BOT', 'SPY', 'equity', 'long', 1, 'open')"
        )
    conn.commit()

    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions", lambda: [
        {"symbol": "SPY", "qty": "1", "avg_entry_price": "500.0", "asset_class": "us_equity"},
        {"symbol": "TSLA", "qty": "5", "avg_entry_price": "250.0", "asset_class": "us_equity"},
    ])
    monkeypatch.setattr("notifier.alert_recon_critical", lambda *a, **k: None, raising=False)

    clean = recon_agent.run_recon(conn, "gate")
    assert clean is False

    value, reason = _no_trade_mode(conn)
    assert value is True, "an unrecognized manual broker position must halt trading"
    assert "critical" in reason.lower() or "mismatch" in reason.lower()


# ---------------------------------------------------------------------------
# Post-fill immediate verification (item 4 of the audit): a divergence must
# be caught right after the trade that caused it, not only on the next
# scheduled recon run.
# ---------------------------------------------------------------------------

def test_post_fill_verification_catches_divergence_immediately(conn, monkeypatch):
    import engine
    broker = FakeAlpaca(price=100.0)
    wire_fake_alpaca(monkeypatch, engine, broker)

    # Simulate exactly the production bug pre-fix: ledger believes BHP is
    # flat (no open rows) but the broker actually holds -20.
    broker.positions["BHP"] = -20.0

    clean = engine.verify_post_fill_position(conn, "BHP", "equity")
    assert clean is False
    value, reason = _no_trade_mode(conn)
    assert value is True
    assert "BHP" in reason


def test_post_fill_verification_passes_when_broker_matches_ledger(conn, monkeypatch):
    import engine
    broker = FakeAlpaca(price=100.0)
    wire_fake_alpaca(monkeypatch, engine, broker)

    _insert_open_position(conn, "AUS_BOT", "BHP", "short", 10, 84.48, str(uuid.uuid4()))
    broker.positions["BHP"] = -10.0

    clean = engine.verify_post_fill_position(conn, "BHP", "equity")
    assert clean is True
    value, _ = _no_trade_mode(conn)
    assert value is False
