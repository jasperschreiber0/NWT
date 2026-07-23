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
DROP TABLE IF EXISTS nwt_ticket_decisions;
DROP TABLE IF EXISTS nwt_tickets;
DROP TABLE IF EXISTS nwt_system_log;
DROP TABLE IF EXISTS nwt_portfolio_ledger;

CREATE TABLE nwt_portfolio_ledger (
    position_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bot_source TEXT NOT NULL,
    strategy_id TEXT,
    asset TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    direction TEXT,
    notional_risk NUMERIC,
    entry_price NUMERIC,
    status TEXT DEFAULT 'open',
    exit_price NUMERIC,
    exit_time TIMESTAMPTZ,
    realized_slippage NUMERIC,
    exit_reason TEXT,
    exit_bid NUMERIC,
    exit_ask NUMERIC,
    spread_group_id UUID
);

CREATE TABLE nwt_trade_outcomes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id TEXT NOT NULL,
    archetype TEXT,
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
    closed_at TIMESTAMPTZ,
    position_id UUID REFERENCES nwt_portfolio_ledger(position_id)
);

-- Partial unique index matching db/migrate_2026_07_idempotent_close.sql —
-- makes write_trade_outcome's ON CONFLICT DO NOTHING an actual guard
-- instead of a no-op, while still allowing multiple legacy NULL rows.
CREATE UNIQUE INDEX nwt_trade_outcomes_position_id_uniq
    ON nwt_trade_outcomes (position_id) WHERE position_id IS NOT NULL;

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

CREATE TABLE nwt_system_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    level TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL,
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
