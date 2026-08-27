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

# Some nwt_agents modules (e.g. shadow_decision_evaluator.py) read Alpaca
# credentials from os.environ[...] (strict) at import time. Tests never talk
# to a real broker — this just supplies harmless dummy values so those
# modules can be imported at all.
os.environ.setdefault("NWT_ALPACA_KEY_ID", "test-key")
os.environ.setdefault("NWT_ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("NWT_ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("NWT_ALPACA_DATA_URL", "https://data.alpaca.markets")

TEST_DSN = os.environ.get("NWT_TEST_DB_DSN")

SCHEMA_SQL = """
DROP TABLE IF EXISTS nwt_trade_outcomes CASCADE;
DROP TABLE IF EXISTS nwt_portfolio_ledger CASCADE;

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
