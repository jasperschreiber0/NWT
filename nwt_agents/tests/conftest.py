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
DROP TABLE IF EXISTS nwt_order_submissions;
DROP TABLE IF EXISTS nwt_inflight_orders;
DROP TABLE IF EXISTS nwt_strategy_genome;
DROP TABLE IF EXISTS nwt_force_close_state;
DROP TABLE IF EXISTS nwt_ticket_claims;
DROP TABLE IF EXISTS position_state_history;
DROP TABLE IF EXISTS nwt_execution_history;
DROP TABLE IF EXISTS nwt_unknown_broker_positions;
DROP TABLE IF EXISTS nwt_system_flags;
DROP TABLE IF EXISTS nwt_ticket_decisions;
DROP TABLE IF EXISTS nwt_tickets;
DROP TABLE IF EXISTS nwt_trade_outcomes;
DROP TABLE IF EXISTS nwt_portfolio_ledger;
DROP TABLE IF EXISTS nwt_system_log;

-- Mirrors db/schema.sql + migrate_phase0.sql's exit_reason +
-- migrate_2026_07_audit_fixes.sql's stop_pct/target_pct — kept in sync so
-- tests exercise the real production column set, not an approximation.
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
    entry_time TIMESTAMPTZ,
    entry_bid NUMERIC,
    entry_ask NUMERIC,
    exit_price NUMERIC,
    exit_time TIMESTAMPTZ,
    exit_bid NUMERIC,
    exit_ask NUMERIC,
    realized_slippage NUMERIC,
    exit_reason TEXT,
    stop_pct NUMERIC,
    target_pct NUMERIC,
    status TEXT DEFAULT 'open',
    alpaca_order_id TEXT,
    spread_group_id UUID,
    lifecycle_state TEXT NOT NULL DEFAULT 'OPEN' CHECK (
        lifecycle_state IN ('OPENING', 'OPEN', 'CLOSING', 'CLOSED', 'EXPIRED',
                             'RECON_PENDING', 'RECONCILING', 'UNKNOWN')
    ),
    recon_attempts INTEGER NOT NULL DEFAULT 0,
    last_recon_attempt_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
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
    pnl NUMERIC,
    pnl_pct NUMERIC,
    pnl_adjusted NUMERIC,
    slippage_model TEXT,
    exit_time TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    position_id UUID REFERENCES nwt_portfolio_ledger(position_id)
);

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
    sizing_multiplier NUMERIC,
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

-- Mirrors db/migrate_2026_07_execution_safety.sql exactly, so these tests
-- exercise the real production schema, not an approximation of it.
CREATE TABLE nwt_ticket_claims (
    ticket_id UUID PRIMARY KEY REFERENCES nwt_tickets(ticket_id),
    claimed_by TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_progress',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ticket_claims_status_valid CHECK (status IN ('in_progress', 'done', 'failed'))
);

CREATE TABLE nwt_force_close_state (
    position_id UUID PRIMARY KEY REFERENCES nwt_portfolio_ledger(position_id),
    asset TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    next_retry_at TIMESTAMPTZ,
    terminal_reason TEXT,
    escalated_at TIMESTAMPTZ,
    last_ticket_id UUID,
    last_worker_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT force_close_state_valid CHECK (
      state IN ('PENDING', 'ATTEMPTING', 'SUCCESS', 'FAILED_RETRYABLE',
                'FAILED_TERMINAL', 'FAILED_REQUIRES_HUMAN', 'UNKNOWN')
    )
);

-- Mirrors db/migrate_2026_07_order_submissions.sql
CREATE TABLE nwt_order_submissions (
    ticket_id UUID PRIMARY KEY REFERENCES nwt_tickets(ticket_id),
    client_order_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Mirrors db/migrate_2026_07_reliability_layer.sql
CREATE TABLE position_state_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id UUID NOT NULL REFERENCES nwt_portfolio_ledger(position_id),
    previous_state TEXT,
    new_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    correlation_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE nwt_execution_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID REFERENCES nwt_tickets(ticket_id),
    client_order_id TEXT,
    broker_order_id TEXT,
    action TEXT NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    result TEXT NOT NULL,
    fill_state TEXT,
    error_state TEXT,
    payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE nwt_unknown_broker_positions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol TEXT NOT NULL,
    qty NUMERIC NOT NULL,
    side TEXT NOT NULL,
    avg_price NUMERIC NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    order_history JSONB,
    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    resolution TEXT,
    resolved_at TIMESTAMPTZ,
    reconstructed_position_id UUID REFERENCES nwt_portfolio_ledger(position_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX idx_unknown_broker_positions_open
  ON nwt_unknown_broker_positions (symbol) WHERE NOT resolved;

CREATE TABLE nwt_system_flags (
    flag TEXT PRIMARY KEY,
    value BOOLEAN NOT NULL DEFAULT FALSE,
    reason TEXT,
    set_by TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Mirrors db/migrate_2026_07_inflight_orders.sql
CREATE TABLE nwt_inflight_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID REFERENCES nwt_tickets(ticket_id),
    alpaca_order_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    position_id UUID,
    exit_reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    resolution TEXT,
    last_checked_at TIMESTAMPTZ,
    stale_since TIMESTAMPTZ
);

-- Mirrors db/schema.sql — needed by run_equity_position_monitor()'s
-- genome fallback lookup when a ledger row has no stop_pct/target_pct.
CREATE TABLE nwt_strategy_genome (
    strategy_id TEXT NOT NULL,
    track TEXT NOT NULL,
    stop_loss_pct NUMERIC,
    profit_target_pct NUMERIC,
    version INTEGER NOT NULL DEFAULT 1,
    active BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (strategy_id, version)
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


@pytest.fixture()
def conn2(conn):
    """
    A second, independent connection to the same test DB — for tests that
    prove concurrency safety (e.g. two workers racing on the same claim).
    Depends on `conn` so schema setup has already run before this connects.
    """
    c = psycopg2.connect(TEST_DSN)
    try:
        yield c
    finally:
        c.rollback()
        c.close()
