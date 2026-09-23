BEGIN;
ALTER TABLE nwt_portfolio_ledger ADD COLUMN IF NOT EXISTS close_generation integer NOT NULL DEFAULT 0;
ALTER TABLE nwt_portfolio_ledger ADD COLUMN IF NOT EXISTS close_requested boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS nwt_close_progress (
 position_id uuid NOT NULL REFERENCES nwt_portfolio_ledger(position_id),
 generation integer NOT NULL,
 order_id uuid NOT NULL,
 requested_qty numeric NOT NULL CHECK(requested_qty>0),
 recorded_qty numeric NOT NULL DEFAULT 0 CHECK(recorded_qty>=0),
 recorded_value numeric NOT NULL DEFAULT 0,
 PRIMARY KEY(position_id,generation),
 CHECK(recorded_qty<=requested_qty)
);
COMMIT;
