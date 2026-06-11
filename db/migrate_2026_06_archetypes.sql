-- Migration: archetype consolidation, pnl_adjusted haircut, session scorecard
-- June 2026. Idempotent — safe to re-run.
--
-- Why: 36 strategy_ids cannot reach statistically meaningful sample sizes inside
-- the 60-day window. Attribution pools at archetype × regime level instead.
-- strategy_id is preserved on every row for later, finer-grained learning.

-- ============================================================
-- 1. Archetype column on genome + trade outcomes
-- ============================================================

ALTER TABLE nwt_strategy_genome ADD COLUMN IF NOT EXISTS archetype TEXT;
ALTER TABLE nwt_trade_outcomes  ADD COLUMN IF NOT EXISTS archetype TEXT;

-- Track C: short-premium directional vs neutral condor/strangle
UPDATE nwt_strategy_genome SET archetype = 'C-SHORT-PREMIUM-DIRECTIONAL'
WHERE strategy_id IN ('C1','C3','C4','C6','C8','C9','C11','C12');

UPDATE nwt_strategy_genome SET archetype = 'C-CONDOR-NEUTRAL'
WHERE strategy_id IN ('C2','C5','C7','C10');

-- Track D: outright long options vs debit spreads
UPDATE nwt_strategy_genome SET archetype = 'D-LONG-DIRECTIONAL'
WHERE strategy_id IN ('D1','D2','D5','D6','D7','D9','D10','D12');

UPDATE nwt_strategy_genome SET archetype = 'D-SPREAD-DIRECTIONAL'
WHERE strategy_id IN ('D3','D4','D8','D11');

-- Track E: moved to shadow mode until quantitative_edge quality is proven.
-- Shadow strategies log inactivity (SHADOW_MODE) instead of erroring out.
-- shadow_mode is a runtime flag read by the track agents — re-added here in
-- case an earlier migration removed it (orthogonal to genome versioning).
ALTER TABLE nwt_strategy_genome ADD COLUMN IF NOT EXISTS shadow_mode BOOLEAN DEFAULT FALSE;

UPDATE nwt_strategy_genome SET archetype = 'E-VOL-DESK', shadow_mode = TRUE
WHERE track = 'E';

-- Track A equity strategies: each is already its own bucket
UPDATE nwt_strategy_genome SET archetype = strategy_id
WHERE track = 'A' AND archetype IS NULL;

-- ============================================================
-- 2. pnl_adjusted haircut — NBBO capture + adjusted PnL columns
-- ============================================================

-- Ledger: quote context at entry/exit + strategy attribution at the source
ALTER TABLE nwt_portfolio_ledger ADD COLUMN IF NOT EXISTS strategy_id TEXT;
ALTER TABLE nwt_portfolio_ledger ADD COLUMN IF NOT EXISTS entry_bid NUMERIC;
ALTER TABLE nwt_portfolio_ledger ADD COLUMN IF NOT EXISTS entry_ask NUMERIC;
ALTER TABLE nwt_portfolio_ledger ADD COLUMN IF NOT EXISTS exit_bid NUMERIC;
ALTER TABLE nwt_portfolio_ledger ADD COLUMN IF NOT EXISTS exit_ask NUMERIC;

-- Outcomes: spread-haircut adjusted PnL. pnl_adjusted is the number that matters.
ALTER TABLE nwt_trade_outcomes ADD COLUMN IF NOT EXISTS pnl_adjusted NUMERIC;
ALTER TABLE nwt_trade_outcomes ADD COLUMN IF NOT EXISTS pnl_adjusted_pct NUMERIC;
ALTER TABLE nwt_trade_outcomes ADD COLUMN IF NOT EXISTS entry_spread_pct NUMERIC;
ALTER TABLE nwt_trade_outcomes ADD COLUMN IF NOT EXISTS exit_spread_pct NUMERIC;

-- ============================================================
-- 3. Green/red session scorecard
-- ============================================================

CREATE TABLE IF NOT EXISTS nwt_session_scorecard (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_date DATE NOT NULL UNIQUE,
  integrity_gate_passed BOOLEAN,
  directives_fresh BOOLEAN,
  conviction_ran BOOLEAN,
  tracks_ran BOOLEAN,
  activity_logged BOOLEAN,      -- proposals OR inactivity rows (do-nothing is valid)
  risk_agent_clear BOOLEAN,     -- ran AND no stale unprocessed proposals
  execution_clear BOOLEAN,      -- no stale unprocessed TRADE_REQUESTs
  learning_agent_ran BOOLEAN,
  manual_interventions INTEGER DEFAULT 0,
  green BOOLEAN,
  details JSONB,
  computed_at TIMESTAMPTZ DEFAULT NOW()
);
