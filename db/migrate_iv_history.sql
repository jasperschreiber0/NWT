-- IV history store for the real IV pipeline (nwt_agents/iv_pipeline/).
-- One row per (ticker, date), written daily by iv_snapshot_job.py after
-- US market open. IV rank/percentile are computed over this series with a
-- confidence label (low <90 days, medium 90-249, high 250+) until a full
-- 252-day window accumulates.

CREATE TABLE IF NOT EXISTS nwt_iv_history (
  ticker TEXT NOT NULL,
  date DATE NOT NULL,
  atm_iv_30d NUMERIC,        -- 30-DTE ATM IV, interpolated across straddling expiries
  atm_iv_60d NUMERIC,        -- 60-DTE ATM IV
  term_slope NUMERIC,        -- atm_iv_30d - atm_iv_60d (positive = backwardation)
  put_skew_25d NUMERIC,      -- 25-delta put IV - ATM IV
  hv_20d NUMERIC,            -- 20-day realized vol (annualized) — the HONEST name
  hv_iv_spread NUMERIC,      -- atm_iv_30d - hv_20d (vol risk premium)
  source TEXT,               -- provider name (alpaca | polygon | tradier)
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_iv_history_ticker_date
  ON nwt_iv_history (ticker, date DESC);
