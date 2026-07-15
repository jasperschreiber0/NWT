"""
nwt_agents/tests/conftest.py
Regression tests for the trade-aggregation fix need a real Postgres
connection (the bug lives in GROUP BY / JOIN behavior, not something a
mock can exercise honestly). They run against NWT_TEST_DB_DSN — a
throwaway local/CI database, never the production nwt_agents DB — and
skip cleanly if that isn't configured/reachable rather than failing the
whole suite.
"""
import os

import psycopg2
import pytest

TEST_DSN = os.environ.get("NWT_TEST_DB_DSN")

SCHEMA_SQL = """
DROP TABLE IF EXISTS nwt_trade_outcomes;
DROP TABLE IF EXISTS nwt_portfolio_ledger;

CREATE TABLE nwt_portfolio_ledger (
    position_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bot_source TEXT NOT NULL,
    strategy_id TEXT,
    asset TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    direction TEXT,
    status TEXT DEFAULT 'open',
    spread_group_id UUID
);

CREATE TABLE nwt_trade_outcomes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id TEXT NOT NULL,
    archetype TEXT,
    symbol TEXT,
    direction TEXT,
    pnl NUMERIC,
    pnl_pct NUMERIC,
    pnl_adjusted NUMERIC,
    exit_time TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    position_id UUID REFERENCES nwt_portfolio_ledger(position_id)
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
