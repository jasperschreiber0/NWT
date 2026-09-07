-- NWT opportunity outcome lanes
-- Additive migration. Does not change order or allocation behavior.

CREATE TABLE IF NOT EXISTS nwt_opportunity_outcomes (
  opportunity_id UUID NOT NULL,
  lane TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  direction TEXT,
  track TEXT,
  regime JSONB,
  proposed_qty NUMERIC,
  applied_qty NUMERIC,
  entry_price NUMERIC,
  exit_price NUMERIC,
  pnl NUMERIC,
  pnl_pct NUMERIC,
  cost NUMERIC,
  outcome TEXT,
  decision TEXT,
  decision_reason TEXT,
  source_ticket_id UUID,
  source_decision_id BIGINT,
  opened_at TIMESTAMPTZ,
  closed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (opportunity_id, lane),
  CHECK (lane IN ('RAW_SHADOW', 'BRAIN_SHADOW', 'RISK_SHADOW', 'PAPER', 'LIVE')),
  CHECK (direction IS NULL OR direction IN ('long', 'short'))
);

CREATE INDEX IF NOT EXISTS idx_opportunity_outcomes_strategy
  ON nwt_opportunity_outcomes (strategy_id, lane, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_opportunity_outcomes_regime
  ON nwt_opportunity_outcomes USING GIN (regime);
CREATE INDEX IF NOT EXISTS idx_opportunity_outcomes_open
  ON nwt_opportunity_outcomes (closed_at)
  WHERE closed_at IS NULL;

COMMENT ON TABLE nwt_opportunity_outcomes IS
  'Parallel raw, Brain, Risk, paper, and live outcome lanes for each opportunity';

