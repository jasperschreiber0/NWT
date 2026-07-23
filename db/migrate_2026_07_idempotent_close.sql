-- NWT — idempotent position close + recon race fix — 2026-07-23
-- Run as: psql "$NWT_DB_DSN" -f migrate_2026_07_idempotent_close.sql
-- Idempotent — safe to re-run.
--
-- execution/engine.py::write_trade_outcome() has always inserted with
-- "ON CONFLICT DO NOTHING" on nwt_trade_outcomes, intending position_id to
-- dedupe a position that gets closed twice (a retry, or two close paths
-- racing on the same position_id). Without a unique constraint on
-- position_id there is nothing for that clause to conflict against, so it
-- has silently been a no-op — duplicate trade_outcome rows for the same
-- position were never actually prevented. Legacy rows with a NULL
-- position_id (pre-attribution) are exempt via the partial index so they
-- can still occur more than once, matching existing test coverage
-- (test_legacy_row_with_no_position_id_is_its_own_trade).

BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS nwt_trade_outcomes_position_id_uniq
  ON nwt_trade_outcomes (position_id)
  WHERE position_id IS NOT NULL;

COMMIT;
