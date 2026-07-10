-- Migration: add real filled contract/share qty to the ledger
-- July 2026. Idempotent — safe to re-run.
--
-- Why: nwt_portfolio_ledger had no qty column. recon_agent.py inferred
-- options contract count from row COUNT ("1 ledger row = 1 contract"),
-- which breaks whenever a single order fills more than one contract —
-- routine at 2% account sizing against typical premiums. This caused a
-- false-positive CRITICAL qty_mismatch and no_trade_mode halt on
-- 2026-07-10 (AAPL260717C00312500: 2 ledger rows, but each order actually
-- filled 3 contracts = 6 total, matching Alpaca's live position exactly).
--
-- After running this migration, run nwt_agents/backfill_ledger_qty.py once
-- to populate qty for existing open positions from Alpaca's real fill data
-- before the next recon --gate run — otherwise every open option position
-- will show qty=NULL and re-trigger the same class of false mismatch.

ALTER TABLE nwt_portfolio_ledger ADD COLUMN IF NOT EXISTS qty NUMERIC;
