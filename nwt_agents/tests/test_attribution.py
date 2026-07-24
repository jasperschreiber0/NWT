"""
nwt_agents/tests/test_attribution.py
Regression tests for the cold-start import / position attribution audit
(see nwt_agents/attribution_audit.py and the CLAUDE.md-driven review that
produced it).

Covers:
  A. Fresh startup with existing broker positions -- cold_start_import()
     imports cleanly, marks rows UNATTRIBUTED, assigns no false bot ownership.
  B. Execution-created position -- insert_position() produces a row with
     full provenance (real bot_source, non-null alpaca_order_id) that the
     attribution audit does NOT flag.
  C. Unknown-origin position (bot_source outside the known allowlist, e.g.
     a hand-written 'RECON_RECOVERED' row) -- stays visible/queryable,
     doesn't raise, and the audit flags it without touching no_trade_mode
     or corrupting trade-outcome joins.
  D. Reconciliation mismatch -- a CRITICAL mismatch (qty_mismatch) sets
     no_trade_mode; a non-critical mismatch (in_ledger_not_alpaca alone)
     does not; and --clear-if-clean only clears once recon is genuinely
     clean.

Run against a throwaway Postgres (NWT_TEST_DB_DSN), never production:
    NWT_TEST_DB_DSN=postgresql://nwt_test:nwt_test_pw@localhost/nwt_attribution_test \
        pytest nwt_agents/tests/test_attribution.py -v
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "execution"))

TEST_DSN = os.environ.get("NWT_TEST_DB_DSN")

SCHEMA_SQL = """
DROP TABLE IF EXISTS nwt_trade_outcomes;
DROP TABLE IF EXISTS nwt_ticket_decisions;
DROP TABLE IF EXISTS nwt_tickets;
DROP TABLE IF EXISTS nwt_portfolio_ledger;
DROP TABLE IF EXISTS nwt_system_flags;
DROP TABLE IF EXISTS nwt_system_log;

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
"""


@pytest.fixture()
def conn():
    if not TEST_DSN:
        pytest.skip("NWT_TEST_DB_DSN not set — skipping DB-backed regression tests")
    c = psycopg2.connect(TEST_DSN)
    try:
        with c.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        c.commit()
        yield c
    finally:
        c.rollback()
        c.close()


# ---------------------------------------------------------------------------
# A. Fresh startup with existing broker positions
# ---------------------------------------------------------------------------

def test_cold_start_import_marks_unattributed_no_false_ownership(conn, monkeypatch):
    import recon_agent

    alpaca_positions = [
        {"symbol": "AAPL", "qty": "286", "avg_entry_price": "180.50", "asset_class": "us_equity"},
        {"symbol": "SPY", "qty": "-10", "avg_entry_price": "512.40", "asset_class": "us_equity"},
    ]
    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions", lambda: alpaca_positions)

    # Ledger is empty -- this is the cold-start precondition.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_portfolio_ledger")
        assert cur.fetchone()[0] == 0

    recon_agent.cold_start_import(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT bot_source, asset, direction, alpaca_order_id, status FROM nwt_portfolio_ledger ORDER BY asset")
        rows = cur.fetchall()

    assert len(rows) == 2
    for bot_source, asset, direction, alpaca_order_id, status in rows:
        # No false ownership: every imported row is UNATTRIBUTED, never a
        # real bot identifier guessed from symbol/direction.
        assert bot_source == "UNATTRIBUTED"
        assert status == "open"
        # No order ever placed for an imported position -- alpaca_order_id
        # must stay NULL, which is what lets it be distinguished from a
        # position our own execution engine created.
        assert alpaca_order_id is None

    directions = {r[1]: r[2] for r in rows}
    assert directions["AAPL"] == "long"
    assert directions["SPY"] == "short"

    # A ticket documenting the import must exist (audit trail).
    with conn.cursor() as cur:
        cur.execute("SELECT type, from_agent FROM nwt_tickets WHERE type='cold_start_import'")
        ticket = cur.fetchone()
    assert ticket is not None
    assert ticket[1] == "RECON_AGENT"


def test_cold_start_import_is_noop_when_ledger_already_populated(conn, monkeypatch):
    import recon_agent

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (bot_source, asset, asset_type, status) "
            "VALUES ('US_BOT', 'SPY', 'equity', 'open')"
        )
    conn.commit()

    called = {"count": 0}

    def _fail_if_called():
        called["count"] += 1
        raise AssertionError("fetch_alpaca_positions should not be called when ledger is non-empty")

    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions", _fail_if_called)

    recon_agent.cold_start_import(conn)
    assert called["count"] == 0


# ---------------------------------------------------------------------------
# B. Execution-created position has full provenance
# ---------------------------------------------------------------------------

def test_execution_created_position_has_full_provenance(conn):
    from ledger import insert_position
    from attribution_audit import check_unknown_bot_sources

    position_id = insert_position(conn, {
        "bot_source": "NWT_TRACK_D",
        "strategy_id": "D11",
        "asset": "TSLA260814P00315000",
        "asset_type": "option",
        "direction": "long",
        "notional_risk": 251.42,
        "qty": 1,
        "entry_price": 2.51,
        "alpaca_order_id": str(uuid.uuid4()),
    })

    with conn.cursor() as cur:
        cur.execute(
            "SELECT bot_source, strategy_id, alpaca_order_id, entry_price FROM nwt_portfolio_ledger "
            "WHERE position_id = %s", (position_id,)
        )
        bot_source, strategy_id, alpaca_order_id, entry_price = cur.fetchone()

    # Provenance chain: known bot, known strategy, and a real order id --
    # everything needed to trace this row back to an actual fill.
    assert bot_source == "NWT_TRACK_D"
    assert strategy_id == "D11"
    assert alpaca_order_id is not None
    assert float(entry_price) == pytest.approx(2.51)

    # The audit must NOT flag a properly-attributed, execution-created row.
    assert check_unknown_bot_sources(conn) == []


# ---------------------------------------------------------------------------
# C. Unknown-origin position
# ---------------------------------------------------------------------------

def test_unknown_origin_position_visible_flagged_no_corruption(conn):
    from attribution_audit import check_unknown_bot_sources

    # Simulates a hand-written manual-recovery row like the 'RECON_RECOVERED'
    # bot_source found in production -- no code path in this repo inserts
    # that value, so it must have come from a direct SQL write.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (bot_source, asset, asset_type, direction, qty, "
            "entry_price, status, alpaca_order_id) "
            "VALUES ('RECON_RECOVERED', 'BHP', 'equity', 'short', 10, 84.48, 'open', %s) "
            "RETURNING position_id",
            (str(uuid.uuid4()),),
        )
        position_id = cur.fetchone()[0]
    conn.commit()

    # Remains visible: a plain query still finds it, same as any other row.
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_portfolio_ledger WHERE position_id = %s", (position_id,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "open"

    # Flagged: the audit surfaces it as an unrecognized bot_source rather
    # than silently accepting or silently dropping it.
    flagged = check_unknown_bot_sources(conn)
    assert len(flagged) == 1
    assert flagged[0]["bot_source"] == "RECON_RECOVERED"

    # Does not corrupt learning data: nwt_trade_outcomes has no dependency
    # on bot_source being in the allowlist -- the FK is on position_id only,
    # so an unrecognized-but-real position_id still joins correctly.
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS nwt_trade_outcomes ("
            "  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            "  strategy_id TEXT, position_id UUID REFERENCES nwt_portfolio_ledger(position_id), pnl NUMERIC"
            ")"
        )
        cur.execute(
            "INSERT INTO nwt_trade_outcomes (strategy_id, position_id, pnl) VALUES (%s, %s, %s)",
            ("UNKNOWN", position_id, 12.5),
        )
    conn.commit()  # no FK violation == the join integrity holds regardless of bot_source


# ---------------------------------------------------------------------------
# D. Reconciliation mismatch — blocks trading only when required, clears correctly
# ---------------------------------------------------------------------------

def _no_trade_mode(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT value, set_by FROM nwt_system_flags WHERE flag='no_trade_mode'")
        return cur.fetchone()


def test_critical_mismatch_sets_no_trade_mode(conn, monkeypatch):
    import recon_agent

    # Ledger has EWA at 44 (long); broker reports 100 -- a real qty_mismatch.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (bot_source, asset, asset_type, direction, qty, status) "
            "VALUES ('AUS_BOT', 'EWA', 'equity', 'long', 44, 'open')"
        )
    conn.commit()

    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions", lambda: [
        {"symbol": "EWA", "qty": "100", "avg_entry_price": "28.5", "asset_class": "us_equity"},
    ])
    monkeypatch.setattr("notifier.alert_recon_critical", lambda *a, **k: None, raising=False)

    clean = recon_agent.run_recon(conn, "nightly")
    assert clean is False

    value, set_by = _no_trade_mode(conn)
    assert value is True
    assert set_by == "recon_agent"


def test_noncritical_mismatch_does_not_set_no_trade_mode(conn, monkeypatch):
    import recon_agent

    # Ledger has a position the broker has no record of at all -- classified
    # in_ledger_not_alpaca, which is non-critical (marks 'suspect' only).
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (bot_source, asset, asset_type, direction, qty, status) "
            "VALUES ('AUS_BOT', 'RIO', 'equity', 'long', 5, 'open') RETURNING position_id"
        )
        position_id = cur.fetchone()[0]
    conn.commit()

    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions", lambda: [])

    clean = recon_agent.run_recon(conn, "nightly")
    assert clean is False  # a mismatch was recorded...

    value, _ = _no_trade_mode(conn)
    assert value is False  # ...but it must NOT have blocked trading

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_portfolio_ledger WHERE position_id = %s", (position_id,))
        assert cur.fetchone()[0] == "suspect"


def test_clear_if_clean_only_clears_when_genuinely_clean(conn, monkeypatch):
    import recon_agent

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE nwt_system_flags SET value=TRUE, reason='test critical mismatch', "
            "set_by='recon_agent' WHERE flag='no_trade_mode'"
        )
        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (bot_source, asset, asset_type, direction, qty, status) "
            "VALUES ('AUS_BOT', 'EWA', 'equity', 'long', 44, 'open')"
        )
    conn.commit()

    # Still mismatched -- clear-if-clean must refuse to clear.
    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions", lambda: [
        {"symbol": "EWA", "qty": "100", "avg_entry_price": "28.5", "asset_class": "us_equity"},
    ])
    monkeypatch.setattr("notifier.alert_recon_critical", lambda *a, **k: None, raising=False)
    clean = recon_agent.run_recon(conn, "manual_clear_check")
    assert clean is False
    value, _ = _no_trade_mode(conn)
    assert value is True, "no_trade_mode must stay set while a critical mismatch is unresolved"

    # Now genuinely clean -- broker agrees with ledger.
    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions", lambda: [
        {"symbol": "EWA", "qty": "44", "avg_entry_price": "28.5", "asset_class": "us_equity"},
    ])
    clean = recon_agent.run_recon(conn, "manual_clear_check")
    assert clean is True
    recon_agent.clear_no_trade_mode(conn, "recon_agent_manual_clear")

    value, set_by = _no_trade_mode(conn)
    assert value is False
    assert set_by == "recon_agent_manual_clear"
