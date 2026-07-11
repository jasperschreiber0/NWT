-- NWT — Codebase audit fixes — 2026-07-11
-- Run as: psql "$NWT_DB_DSN" -f migrate_2026_07_audit_fixes.sql
-- Idempotent — safe to re-run.
--
-- Bundles the schema changes needed for the audit fix pass:
--   1. Risk Agent sizing-reduction rules (3, 7) need a way to actually reduce
--      sizing rather than only log a warning — a decision-level multiplier
--      that execution_agent.py applies before submitting the order.
--   2. The equity position monitor in execution/engine.py silently discarded
--      the Brain-supplied stop_pct/target_pct because the ledger never
--      stored them — it now does.

BEGIN;

-- ============================================================
-- 1. Sizing multiplier on risk agent decisions
-- ============================================================
ALTER TABLE nwt_ticket_decisions
  ADD COLUMN IF NOT EXISTS sizing_multiplier NUMERIC;

COMMENT ON COLUMN nwt_ticket_decisions.sizing_multiplier IS
  'Optional 0-1 factor set by RISK_AGENT (Rules 3/7: slippage expansion, '
  'regime confidence < 0.4). execution_agent.py multiplies sized_notional '
  'by this before submitting the order. NULL/1.0 = no reduction.';

-- ============================================================
-- 2. Per-position exit parameters on the ledger
-- ============================================================
ALTER TABLE nwt_portfolio_ledger
  ADD COLUMN IF NOT EXISTS stop_pct NUMERIC,
  ADD COLUMN IF NOT EXISTS target_pct NUMERIC;

COMMENT ON COLUMN nwt_portfolio_ledger.stop_pct IS
  'Per-trade stop, copied from the approved ticket at fill time. '
  'run_equity_position_monitor() prefers this over the genome/hardcoded default.';
COMMENT ON COLUMN nwt_portfolio_ledger.target_pct IS
  'Per-trade profit target, copied from the approved ticket at fill time.';

-- ============================================================
-- 3. Regime history — needed for the "same regime 5+ sessions must cite
--    price evidence or reclassify to neutral" rule, which had no
--    persistence anywhere and was therefore entirely unimplemented.
-- ============================================================
CREATE TABLE IF NOT EXISTS nwt_regime_history (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  primary_regime  TEXT NOT NULL,
  confidence      NUMERIC,
  transition_risk NUMERIC,
  spy_vs_5d_pct   NUMERIC
);

CREATE INDEX IF NOT EXISTS idx_regime_history_computed_at
  ON nwt_regime_history (computed_at DESC);

COMMIT;

-- ============================================================
-- Post-migration checklist
-- ============================================================
-- 1. \d nwt_ticket_decisions   -- confirm sizing_multiplier present
-- 2. \d nwt_portfolio_ledger   -- confirm stop_pct, target_pct present
