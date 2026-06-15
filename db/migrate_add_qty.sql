-- Add qty column to nwt_portfolio_ledger.
-- Recon compares SUM(qty) vs Alpaca qty — not row count.
-- Backfill: options estimated from notional/entry_price/100, equities from notional/entry_price.

ALTER TABLE nwt_portfolio_ledger ADD COLUMN IF NOT EXISTS qty INTEGER;

UPDATE nwt_portfolio_ledger
SET qty = CASE
    WHEN asset_type = 'option' AND entry_price > 0
        THEN GREATEST(ROUND(notional_risk / (entry_price * 100))::INTEGER, 1)
    WHEN asset_type = 'equity' AND entry_price > 0
        THEN GREATEST(ROUND(notional_risk / entry_price)::INTEGER, 1)
    ELSE 1
END
WHERE qty IS NULL;
