-- Separate evidence from quantity progress: do not manufacture historic bounds
-- or create progress rows that would suppress legitimate residual retries.
BEGIN;
CREATE TABLE IF NOT EXISTS nwt_close_receipt_bounds (
 position_id uuid NOT NULL REFERENCES nwt_portfolio_ledger(position_id),
 generation integer NOT NULL CHECK(generation>=0),
 order_id text NOT NULL CHECK(length(order_id)>0),
 bounds jsonb NOT NULL CHECK(jsonb_typeof(bounds)='object'),
 PRIMARY KEY(position_id,generation)
);
COMMIT;
