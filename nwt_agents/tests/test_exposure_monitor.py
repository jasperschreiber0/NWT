"""
nwt_agents/tests/test_exposure_monitor.py
Regression tests for the directional-exposure visibility report
(nwt_agents/exposure_monitor.py). Monitoring only — these tests assert what
gets *reported*, never touch check_directional_cap() or any trading path.

Run against a throwaway Postgres (NWT_TEST_DB_DSN), never production:
    NWT_TEST_DB_DSN=postgresql://nwt_test:nwt_test_pw@localhost/nwt_exposure_test \
        pytest nwt_agents/tests/test_exposure_monitor.py -v
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

TEST_DSN = os.environ.get("NWT_TEST_DB_DSN")

SCHEMA_SQL = """
DROP TABLE IF EXISTS nwt_portfolio_ledger;
DROP TABLE IF EXISTS nwt_tickets;
DROP TABLE IF EXISTS nwt_system_log;

CREATE TABLE nwt_portfolio_ledger (
    position_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bot_source TEXT NOT NULL,
    strategy_id TEXT,
    asset TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    direction TEXT,
    notional_risk NUMERIC,
    qty NUMERIC,
    entry_price NUMERIC,
    entry_time TIMESTAMPTZ DEFAULT NOW(),
    status TEXT DEFAULT 'open',
    alpaca_order_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE nwt_tickets (
    ticket_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    type TEXT NOT NULL,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

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
    with c.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    c.commit()
    yield c
    c.rollback()
    c.close()


def _insert(conn, bot_source, asset, direction, notional, qty=1, entry_time=None, status="open"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_portfolio_ledger
                (bot_source, asset, asset_type, direction, notional_risk, qty, entry_time, status)
            VALUES (%s, %s, 'equity', %s, %s, %s, COALESCE(%s, NOW()), %s)
            """,
            (bot_source, asset, direction, notional, qty, entry_time, status),
        )
    conn.commit()


def test_report_reflects_exact_production_scenario(conn, monkeypatch):
    import exposure_monitor as em

    monkeypatch.setattr(em, "get_account_equity", lambda: 93_221.67)

    _insert(conn, "UNATTRIBUTED", "AAPL", "long", 90_805.00)
    _insert(conn, "NWT_TRACK_C", "SPY", "long", 698.40)

    report = em.build_report(conn)

    assert report["unattributed"]["total_notional"] == pytest.approx(90_805.00)
    assert report["unattributed"]["symbols"] == ["AAPL"]
    assert report["bot_controlled"]["total_long_notional"] == pytest.approx(698.40)
    assert report["combined"]["total_long_notional"] == pytest.approx(91_503.40)
    assert report["combined"]["pct_of_equity"] == pytest.approx(91_503.40 / 93_221.67)


def test_combined_exposure_alert_fires_above_90_percent(conn, monkeypatch):
    import exposure_monitor as em

    monkeypatch.setattr(em, "get_account_equity", lambda: 100_000.0)
    _insert(conn, "UNATTRIBUTED", "AAPL", "long", 91_000.0)  # 91% alone

    report = em.build_report(conn)

    assert report["combined"]["pct_of_equity"] > 0.90
    assert any("Combined directional exposure" in a for a in report["alerts"])


def test_no_alert_when_combined_exposure_below_threshold(conn, monkeypatch):
    import exposure_monitor as em

    monkeypatch.setattr(em, "get_account_equity", lambda: 100_000.0)
    _insert(conn, "UNATTRIBUTED", "AAPL", "long", 50_000.0)
    _insert(conn, "NWT_TRACK_C", "SPY", "long", 10_000.0)

    report = em.build_report(conn)

    assert report["combined"]["pct_of_equity"] == pytest.approx(0.60)
    assert report["alerts"] == []


def test_stale_unattributed_position_triggers_age_alert(conn, monkeypatch):
    import exposure_monitor as em

    monkeypatch.setattr(em, "get_account_equity", lambda: 500_000.0)  # keep combined % low
    old_entry = datetime.now(timezone.utc) - timedelta(days=45)
    _insert(conn, "UNATTRIBUTED", "AAPL", "long", 1_000.0, entry_time=old_entry)

    report = em.build_report(conn)

    assert report["unattributed"]["max_age_days"] >= 30
    assert any("UNATTRIBUTED exposure open" in a for a in report["alerts"])


def test_fresh_unattributed_position_does_not_trigger_age_alert(conn, monkeypatch):
    import exposure_monitor as em

    monkeypatch.setattr(em, "get_account_equity", lambda: 500_000.0)
    recent_entry = datetime.now(timezone.utc) - timedelta(days=2)
    _insert(conn, "UNATTRIBUTED", "AAPL", "long", 1_000.0, entry_time=recent_entry)

    report = em.build_report(conn)

    assert report["unattributed"]["max_age_days"] <= 2
    assert report["alerts"] == []


def test_closed_positions_excluded_from_every_figure(conn, monkeypatch):
    import exposure_monitor as em

    monkeypatch.setattr(em, "get_account_equity", lambda: 100_000.0)
    _insert(conn, "UNATTRIBUTED", "TSLA", "long", 80_000.0, status="closed")
    _insert(conn, "NWT_TRACK_C", "SPY", "long", 5_000.0, status="closed")

    report = em.build_report(conn)

    assert report["unattributed"]["total_notional"] == 0
    assert report["bot_controlled"]["total_long_notional"] == 0
    assert report["combined"]["total_long_notional"] == 0
    assert report["alerts"] == []


def test_run_writes_ticket_and_does_not_touch_no_trade_mode(conn, monkeypatch):
    import exposure_monitor as em

    monkeypatch.setattr(em, "get_account_equity", lambda: 93_221.67)
    _insert(conn, "UNATTRIBUTED", "AAPL", "long", 90_805.00)

    em.run(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT type, from_agent FROM nwt_tickets WHERE type='exposure_report'")
        row = cur.fetchone()
    assert row is not None
    assert row[1] == "EXPOSURE_MONITOR"

    # This is a pure monitoring script -- it must never create or reference
    # a no_trade_mode flag row (that table isn't even in this test's schema;
    # if exposure_monitor tried to touch it, this test would error, not pass).
