-- NWT — Canonical decision-observation model — 2026-08-27
-- Run as: psql "$NWT_DB_DSN" -f migrate_2026_08_canonical_decisions.sql
-- Idempotent — safe to re-run.
--
-- Generalises nwt_decision_inputs (previously Track C/D/E-only) into the
-- canonical learning observation for EVERY track: one row per genuine
-- directional read a strategist forms, whether or not it becomes a trade.
--
-- Context: the Learning Agent, Portfolio Allocator, and every win-rate/
-- expectancy metric in the codebase currently compute exclusively over
-- nwt_trade_outcomes (executed + closed trades). Track A (equity) bots have
-- no durable feature persistence at all for a signal that didn't clear
-- entry_threshold or was blocked downstream (risk veto, structurally
-- impossible, execution failure) — only a reason STRING in
-- nwt_inactivity_log, no numeric signal_strength, no regime, no reference
-- price. This migration does not add a parallel table — it generalises the
-- one that already does this correctly for part of Track C/D.

BEGIN;

-- ============================================================
-- 1. symbol becomes nullable
--
-- evaluate_shadow_mutation() (shared_context.py) already passes symbol=None
-- for its SHADOW_MUTATION_NO_MATCH row — this has been silently failing
-- against the NOT NULL constraint since that code was written (caught by
-- log_decision_input's own try/except, logged as a WARNING, never surfaced).
-- China's aggregate-level NO_POLICY_EDGE gate (confidence computed across
-- the whole basket before any symbol is selected) has the same shape: a
-- genuine directional read with no single symbol to attribute it to.
-- ============================================================
ALTER TABLE nwt_decision_inputs ALTER COLUMN symbol DROP NOT NULL;

-- ============================================================
-- 2. Canonical fields — generalising beyond Track C/D/E
-- ============================================================
ALTER TABLE nwt_decision_inputs
  ADD COLUMN IF NOT EXISTS asset_class     TEXT,   -- 'equity' | 'option' — same vocabulary as nwt_portfolio_ledger.asset_type
  ADD COLUMN IF NOT EXISTS signal_strength NUMERIC, -- the strategy's actual numeric directional signal (z-score, ORB score, conviction, confidence) — never buried in rationale text
  ADD COLUMN IF NOT EXISTS stage_reached   TEXT,   -- 'SIGNAL' | 'RISK' | 'EXECUTION' — furthest pipeline stage this observation reached
  ADD COLUMN IF NOT EXISTS outcome_reason  TEXT;   -- controlled vocabulary, see constraint below

COMMENT ON COLUMN nwt_decision_inputs.signal_strength IS
  'Canonical numeric signal (z-score / ORB score / conviction / confidence). '
  'conviction_score is retained for Track C/D/E backward compatibility and '
  'is written with the same value for those tracks — signal_strength is the '
  'column every new caller, all tracks, should read.';

-- Controlled vocabulary (CLAUDE.md "Do Nothing as a First-Class State",
-- extended). NULL is allowed — legacy pre-migration rows, and rows still
-- pending a downstream outcome (e.g. a submitted ticket awaiting risk/
-- execution resolution) have no terminal reason yet.
ALTER TABLE nwt_decision_inputs DROP CONSTRAINT IF EXISTS decision_inputs_outcome_reason_check;
ALTER TABLE nwt_decision_inputs ADD CONSTRAINT decision_inputs_outcome_reason_check
  CHECK (outcome_reason IS NULL OR outcome_reason IN (
    'NO_EDGE', 'BELOW_THRESHOLD', 'RISK_VETOED', 'EXECUTION_FAILED',
    'STRUCTURALLY_IMPOSSIBLE', 'DUPLICATE_POSITION', 'EXECUTED'
  ));

ALTER TABLE nwt_decision_inputs DROP CONSTRAINT IF EXISTS decision_inputs_stage_reached_check;
ALTER TABLE nwt_decision_inputs ADD CONSTRAINT decision_inputs_stage_reached_check
  CHECK (stage_reached IS NULL OR stage_reached IN ('SIGNAL', 'RISK', 'EXECUTION'));

-- ============================================================
-- 3. Idempotency — one genuine directional read = one row
--
-- Deterministic key: a strategist evaluates each symbol at most once per
-- (strategy_id, genome_version, run_date). Cron retries, process restarts,
-- and recovery-job re-runs on the same day collide on this key and become a
-- no-op via ON CONFLICT, not a duplicate row. genome_version is NULLable
-- (baseline strategies) so the index is built on COALESCE(genome_version,0);
-- symbol is nullable too (whole-basket observations) so it is also coalesced.
-- ============================================================
CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_inputs_dedup
  ON nwt_decision_inputs (
    strategy_id,
    COALESCE(genome_version, 0),
    COALESCE(symbol, ''),
    run_date
  );

-- ============================================================
-- 4. Shadow evaluator outputs — MFE/MAE and completion state
-- ============================================================
ALTER TABLE nwt_decision_inputs
  ADD COLUMN IF NOT EXISTS shadow_mfe_pct    NUMERIC,  -- max favorable excursion observed during the walk
  ADD COLUMN IF NOT EXISTS shadow_mae_pct    NUMERIC,  -- max adverse excursion observed during the walk
  ADD COLUMN IF NOT EXISTS shadow_completion TEXT;      -- 'TARGET_HIT' | 'STOP_HIT' | 'HORIZON_EXPIRED'

ALTER TABLE nwt_decision_inputs DROP CONSTRAINT IF EXISTS decision_inputs_shadow_completion_check;
ALTER TABLE nwt_decision_inputs ADD CONSTRAINT decision_inputs_shadow_completion_check
  CHECK (shadow_completion IS NULL OR shadow_completion IN ('TARGET_HIT', 'STOP_HIT', 'HORIZON_EXPIRED'));

-- ============================================================
-- 5. Deterministic realized-outcome linkage
--
-- nwt_decision_inputs.outcome_id already existed and was dead (nothing ever
-- wrote to it — no FK path existed to populate it deterministically).
-- nwt_portfolio_ledger had no reference back to the ticket that created it,
-- so learning_agent.py could only recover the originating ticket via a
-- fuzzy alpaca_order_id/symbol search (find_original_ticket) — sufficient
-- for regime/signal-quality enrichment, not the identifier propagation this
-- linkage needs. Setting this at insert_position() time (execution/engine.py
-- already has ticket_id in scope there) gives outcome_id a real, exact path:
-- decision_inputs.ticket_id -> ledger.ticket_id -> ledger.position_id ->
-- nwt_trade_outcomes.position_id -> nwt_trade_outcomes.id.
-- ============================================================
ALTER TABLE nwt_portfolio_ledger
  ADD COLUMN IF NOT EXISTS ticket_id UUID REFERENCES nwt_tickets(ticket_id);

CREATE INDEX IF NOT EXISTS idx_ledger_ticket_id
  ON nwt_portfolio_ledger (ticket_id) WHERE ticket_id IS NOT NULL;

COMMIT;

-- ============================================================
-- Post-migration checklist
-- ============================================================
-- 1. \d nwt_decision_inputs   -- confirm asset_class, signal_strength,
--    stage_reached, outcome_reason, shadow_mfe_pct, shadow_mae_pct,
--    shadow_completion present; symbol nullable
-- 2. \d nwt_portfolio_ledger  -- confirm ticket_id present
-- 3. SELECT indexname FROM pg_indexes WHERE tablename='nwt_decision_inputs'
--    AND indexname='idx_decision_inputs_dedup';
-- 4. INSERT two nwt_decision_inputs rows with the same
--    (strategy_id, genome_version, symbol, run_date) — second must be a
--    no-op via ON CONFLICT, not a new row (application-level, see
--    shared_context.log_decision_input).
