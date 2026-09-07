-- NWT — Statistical validity fixes before the 60-day collection window — 2026-08-27
-- Run as: psql "$NWT_DB_DSN" -f migrate_2026_08_read_identity_and_shadow_fix.sql
-- Idempotent — safe to re-run.
--
-- Fixes one real idempotency defect found auditing the canonical decision-
-- observation model before starting collection:
--
-- China's strategist polls every 30 minutes, 14:00-18:00 UTC (crontab.txt),
-- re-fetching live price data each run — successive polls can genuinely
-- produce different directional reads for the same symbol on the same day
-- (momentum/confidence evolves through the session). The idempotency key
-- shipped in migrate_2026_08_canonical_decisions.sql was
-- (strategy_id, genome_version, symbol, run_date) — day-granularity only,
-- so it silently collapsed all ~9 daily China polls into a single row
-- (first-write-wins via ON CONFLICT), discarding up to 8 genuine distinct
-- observations per symbol per day. Every other strategist (EU/AUS/US,
-- Track C/D/E) genuinely evaluates each symbol at most once per day, so
-- day-granularity was and remains correct for them.
--
-- Fix: add poll_slot, a text label identifying which scheduled read this
-- is within the day (empty string '' for every once-daily strategist —
-- day-granularity unchanged for them; e.g. '14:00', '14:30', ... for
-- China's 30-minute polls). Included in the idempotency key so:
--   - a retry of the SAME poll slot (cron overlap, process restart within
--     that slot) still collides -> one row, per the required invariant.
--   - a genuinely later poll slot on the same day does NOT collide -> a
--     new, separate row, also per the required invariant.

BEGIN;

ALTER TABLE nwt_decision_inputs
  ADD COLUMN IF NOT EXISTS poll_slot TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN nwt_decision_inputs.poll_slot IS
  'Which scheduled read within the day this observation belongs to. Empty '
  'string for every once-daily strategist (EU/AUS/US, Track C/D/E) -- day '
  'granularity is already correct for them. Non-empty only for '
  'poll-cadence strategists (China: "14:00"/"14:30"/... per crontab.txt), '
  'so a later genuine poll is never collapsed into an earlier one by the '
  'idempotency key, while a retry of the same poll slot still collides.';

DROP INDEX IF EXISTS idx_decision_inputs_dedup;
CREATE UNIQUE INDEX idx_decision_inputs_dedup
  ON nwt_decision_inputs (
    strategy_id,
    COALESCE(genome_version, 0),
    COALESCE(symbol, ''),
    run_date,
    poll_slot
  );

COMMIT;

-- ============================================================
-- Post-migration checklist
-- ============================================================
-- 1. \d nwt_decision_inputs   -- confirm poll_slot present, dedup index rebuilt
-- 2. Same poll slot twice (e.g. two China strategist runs both tagged
--    '14:00' on the same day/symbol/strategy/genome_version) must collide
--    to one row; a different slot ('14:30') must not.
