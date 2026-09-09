-- A recovered aggregate position can originate from many orders or an exercise.
-- Preserve that evidence without inventing a single opening order or strategy.
CREATE TABLE IF NOT EXISTS nwt_position_attribution (
    position_id UUID PRIMARY KEY REFERENCES nwt_portfolio_ledger(position_id),
    attribution_status TEXT NOT NULL CHECK (attribution_status IN
        ('verified_quantity_and_cost', 'verified_quantity_only')),
    source_order_ids TEXT[] NOT NULL DEFAULT '{}',
    source_activity_ids TEXT[] NOT NULL DEFAULT '{}',
    evidence JSONB NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
