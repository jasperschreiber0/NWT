-- NWT — Full activation + shadow candidate tracking — 2026-07-08
-- Run as: psql "$NWT_DB_DSN" -f migrate_2026_07_shadow_candidates.sql
--
-- Context: migrate_phase1.sql (2026-06-10) concentrated Track C/D/E down to
-- 2 strategies each, picked by lowest strategy_id number rather than regime
-- coverage. Each strategy_id in seed_genome.sql is pinned to a specific
-- regime (risk_on, risk_off, neutral, fragile_liquidity, recession_fear,
-- geopolitical_stress, inflation_concern) and regime_matches() requires an
-- exact match — so concentrating to 2 strategies per track left most regime
-- states with zero eligible strategies, some days producing zero proposals
-- structurally, not for lack of edge. Since this is a paper-trading window
-- and the archetype consolidation in track_c/d/e.py already caps proposals
-- to one per archetype per day, reactivating all 12 costs nothing in
-- trade-frequency risk and maximises data breadth across regimes.

BEGIN;

-- ============================================================
-- 1. Reactivate all Track C/D/E strategies (revert migrate_phase1.sql 1.1)
-- ============================================================
UPDATE nwt_strategy_genome SET active = TRUE WHERE track IN ('C', 'D', 'E');

-- ============================================================
-- 2. Shadow candidate tracking — extend nwt_decision_inputs
-- (table existed since migrate_phase1.sql but nothing wrote to it yet)
--
-- Every strategy eligible to fire this run is now logged here by
-- track_c/d/e.py, whether or not it won the archetype-consolidation pick.
-- shadow_decision_evaluator.py later fills in would_have_won/shadow_pnl_pct
-- for non-winning candidates using underlying price action as a directional
-- proxy for what the position would have done — NOT a full options-premium
-- simulation (no historical chain snapshot is captured per candidate), so
-- treat shadow_pnl_pct as a signal-quality indicator, not a dollar estimate.
-- ============================================================
ALTER TABLE nwt_decision_inputs
  ADD COLUMN IF NOT EXISTS archetype           TEXT,
  ADD COLUMN IF NOT EXISTS is_winner           BOOLEAN,
  ADD COLUMN IF NOT EXISTS direction           TEXT,
  ADD COLUMN IF NOT EXISTS entry_price_ref     NUMERIC,
  ADD COLUMN IF NOT EXISTS target_pct          NUMERIC,
  ADD COLUMN IF NOT EXISTS stop_pct            NUMERIC,
  ADD COLUMN IF NOT EXISTS dte_target          INTEGER,
  ADD COLUMN IF NOT EXISTS shadow_evaluated_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS would_have_won      BOOLEAN,
  ADD COLUMN IF NOT EXISTS shadow_exit_price   NUMERIC,
  ADD COLUMN IF NOT EXISTS shadow_pnl_pct      NUMERIC;

CREATE INDEX IF NOT EXISTS idx_decision_inputs_shadow_pending
  ON nwt_decision_inputs (run_date)
  WHERE shadow_evaluated_at IS NULL AND entry_price_ref IS NOT NULL;

COMMIT;

-- ============================================================
-- Post-migration checklist
-- ============================================================
-- 1. Verify all 12 strategies active per track:
--    SELECT track, COUNT(*) FILTER (WHERE active) FROM nwt_strategy_genome
--    WHERE track IN ('C','D','E') GROUP BY track;
--
-- 2. Verify new columns landed:
--    \d nwt_decision_inputs
--
-- 3. After a few days of candidates accumulate with dte_target elapsed,
--    confirm shadow_decision_evaluator.py is filling in outcomes:
--    SELECT strategy_id, is_winner, would_have_won, shadow_pnl_pct
--    FROM nwt_decision_inputs WHERE shadow_evaluated_at IS NOT NULL
--    ORDER BY run_date DESC LIMIT 20;
