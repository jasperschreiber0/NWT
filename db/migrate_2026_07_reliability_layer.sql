-- NWT — Position lifecycle state machine + autonomous reconciliation — 2026-07-22
-- Run as: psql "$NWT_DB_DSN" -f migrate_2026_07_reliability_layer.sql
-- Idempotent — safe to re-run.
--
-- Why: nwt_portfolio_ledger.status (open/closed/suspect) is overloaded to
-- mean two different things — "is this position still held" and "is this
-- ledger row trustworthy" — and every automated consumer (risk_agent.py's
-- Rule 12, execution_agent.py's options monitor, execution/engine.py's
-- force-close path) filters WHERE status='open'. The moment recon_agent.py
-- flips a row to 'suspect' it becomes invisible to every one of them
-- independently — there is no code path that ever reads a 'suspect' row
-- back to resolve it. That is what stranded SPY260720C00753000: recon
-- correctly detected the drift, marked it suspect, and nothing ever looked
-- at it again.
--
-- This migration:
--   1. Adds an explicit lifecycle_state to nwt_portfolio_ledger (8 states),
--      alongside the legacy status column (kept in sync, not removed —
--      other consumers not touched in this pass still read it safely).
--   2. Adds position_state_history — every transition, with reason/source.
--   3. Adds nwt_execution_history — one row per broker action attempt,
--      the single place to answer "what has this system ever done at the
--      broker and what happened", replacing log-grepping.
--   4. Adds nwt_unknown_broker_positions — persistent tracking for
--      in_alpaca_not_ledger cases recon cannot automatically reconstruct,
--      so "first seen" survives across recon runs instead of resetting.
--   5. Adds recon retry bookkeeping columns so RECON_PENDING positions get
--      bounded retries with escalation, mirroring nwt_force_close_state's
--      already-proven pattern, instead of silent infinite suspension.

BEGIN;

-- ============================================================
-- 1. Explicit position lifecycle state
-- ============================================================
ALTER TABLE nwt_portfolio_ledger
  ADD COLUMN IF NOT EXISTS lifecycle_state TEXT NOT NULL DEFAULT 'OPEN';

ALTER TABLE nwt_portfolio_ledger DROP CONSTRAINT IF EXISTS ledger_lifecycle_state_valid;
ALTER TABLE nwt_portfolio_ledger ADD CONSTRAINT ledger_lifecycle_state_valid CHECK (
  lifecycle_state IN ('OPENING', 'OPEN', 'CLOSING', 'CLOSED', 'EXPIRED',
                       'RECON_PENDING', 'RECONCILING', 'UNKNOWN')
);

ALTER TABLE nwt_portfolio_ledger
  ADD COLUMN IF NOT EXISTS recon_attempts INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_recon_attempt_at TIMESTAMPTZ;

-- Backfill existing rows from the legacy status column. One-time; re-running
-- this UPDATE is harmless (idempotent — same mapping every time) but only
-- meaningful before any lifecycle_state has diverged from status, so it's
-- gated to rows that still look untouched (freshly defaulted to 'OPEN' by
-- the ADD COLUMN above but whose real status says otherwise).
UPDATE nwt_portfolio_ledger SET lifecycle_state = 'CLOSED'
  WHERE status = 'closed' AND lifecycle_state = 'OPEN';
UPDATE nwt_portfolio_ledger SET lifecycle_state = 'RECON_PENDING'
  WHERE status = 'suspect' AND lifecycle_state = 'OPEN';

CREATE INDEX IF NOT EXISTS idx_ledger_lifecycle_state
  ON nwt_portfolio_ledger (lifecycle_state)
  WHERE lifecycle_state NOT IN ('CLOSED', 'EXPIRED');

COMMENT ON COLUMN nwt_portfolio_ledger.lifecycle_state IS
  'Explicit state machine, separate from the legacy status column. '
  'OPENING/OPEN/CLOSING/CLOSED/EXPIRED = normal lifecycle. '
  'RECON_PENDING = recon found a mismatch, resolution not yet determined '
  '(retried automatically, never a silent dead end — see recon_agent.py). '
  'RECONCILING = an automatic resolution attempt is in progress right now. '
  'UNKNOWN = resolution attempted and inconclusive after recon_attempts '
  'exhausted retries; requires human review but is still visible to every '
  'health/observability query, unlike bare status=suspect was.';

-- ============================================================
-- 2. Position state transition history
-- ============================================================
CREATE TABLE IF NOT EXISTS position_state_history (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  position_id     UUID NOT NULL REFERENCES nwt_portfolio_ledger(position_id),
  previous_state  TEXT,
  new_state       TEXT NOT NULL,
  reason          TEXT NOT NULL,
  source          TEXT NOT NULL,
  correlation_id  UUID,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_position_state_history_position
  ON position_state_history (position_id, created_at);

COMMENT ON TABLE position_state_history IS
  'Append-only. Every lifecycle_state transition on nwt_portfolio_ledger is '
  'written here by transition_position_state() (shared_context.py / '
  'execution/ledger.py) — never by a bare UPDATE. source identifies which '
  'component made the transition (recon_agent, risk_agent, execution_engine, '
  'expiry_sweeper, human).';

-- ============================================================
-- 3. Unified execution history — every broker action attempt
-- ============================================================
CREATE TABLE IF NOT EXISTS nwt_execution_history (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id        UUID REFERENCES nwt_tickets(ticket_id),
  client_order_id  TEXT,
  broker_order_id  TEXT,
  action           TEXT NOT NULL,   -- 'submit_entry' | 'submit_close' | 'force_close' | 'preflight_check'
  submitted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  result           TEXT NOT NULL,   -- 'accepted' | 'rejected' | 'error' | 'skipped'
  fill_state       TEXT,            -- 'filled' | 'partial' | 'pending' | 'canceled' | NULL
  error_state      TEXT,            -- HTTP status / exception class, NULL on success
  payload          JSONB,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_execution_history_ticket ON nwt_execution_history (ticket_id);
CREATE INDEX IF NOT EXISTS idx_execution_history_recent ON nwt_execution_history (submitted_at);

COMMENT ON TABLE nwt_execution_history IS
  'One row per broker action attempt (order submit, close, force-close '
  'DELETE, pre-flight position check) across execution/engine.py. Answers '
  '"what has this system ever done at the broker" from a single table '
  'instead of grepping execution_engine.log.';

-- ============================================================
-- 4. Broker-only position tracking (in_alpaca_not_ledger, unresolved)
-- ============================================================
CREATE TABLE IF NOT EXISTS nwt_unknown_broker_positions (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol           TEXT NOT NULL,
  qty              NUMERIC NOT NULL,
  side             TEXT NOT NULL,
  avg_price        NUMERIC NOT NULL,
  first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  order_history    JSONB,
  resolved         BOOLEAN NOT NULL DEFAULT FALSE,
  resolution       TEXT,             -- 'auto_reconstructed' | 'human_cleared' | NULL
  resolved_at      TIMESTAMPTZ,
  reconstructed_position_id UUID REFERENCES nwt_portfolio_ledger(position_id),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Partial unique index (not a table constraint) so ON CONFLICT can target
-- "the current unresolved row for this symbol" specifically, while still
-- allowing a symbol to recur as a fresh unknown-position incident after a
-- prior occurrence was resolved (multiple resolved=TRUE rows over time are
-- expected and fine — only one unresolved row per symbol at a time).
CREATE UNIQUE INDEX IF NOT EXISTS idx_unknown_broker_positions_open
  ON nwt_unknown_broker_positions (symbol) WHERE NOT resolved;

COMMENT ON TABLE nwt_unknown_broker_positions IS
  'One row per (symbol) currently unexplained at the broker with no ledger '
  'record. first_seen_at persists across recon runs via UPSERT on '
  '(symbol, resolved=FALSE) instead of resetting every cycle. '
  'order_history captures the Alpaca order-history search recon attempted, '
  'so a human reviewing this already has everything gathered.';

COMMIT;

-- ============================================================
-- Post-migration checklist
-- ============================================================
-- 1. \d nwt_portfolio_ledger              -- confirm lifecycle_state + check constraint
-- 2. \d position_state_history
-- 3. \d nwt_execution_history
-- 4. \d nwt_unknown_broker_positions
-- 5. SELECT lifecycle_state, COUNT(*) FROM nwt_portfolio_ledger GROUP BY 1;
--    -- confirm the status->lifecycle_state backfill mapped every row sanely
