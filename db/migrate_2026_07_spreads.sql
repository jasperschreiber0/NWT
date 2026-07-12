-- NWT Spread Support Migration — 2026-07-12
-- Run as: psql "$NWT_DB_DSN" -f migrate_2026_07_spreads.sql
--
-- Multi-leg (defined-risk) options structures are executed as one Alpaca
-- mleg order but tracked as one ledger row PER LEG (recon matches Alpaca
-- positions per contract). spread_group_id ties the legs together so the
-- position monitor can value and close the structure as a unit.

BEGIN;

ALTER TABLE nwt_portfolio_ledger
  ADD COLUMN IF NOT EXISTS spread_group_id UUID;

CREATE INDEX IF NOT EXISTS idx_ledger_spread_group
  ON nwt_portfolio_ledger (spread_group_id)
  WHERE spread_group_id IS NOT NULL;

COMMIT;
