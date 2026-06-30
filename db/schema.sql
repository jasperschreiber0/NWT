-- Core ticket tables (append-only enforced)
CREATE TABLE IF NOT EXISTS nwt_tickets (
  ticket_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  from_agent TEXT NOT NULL,
  to_agent TEXT NOT NULL,
  type TEXT NOT NULL,
  payload JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS nwt_ticket_decisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id UUID REFERENCES nwt_tickets(ticket_id),
  decision TEXT NOT NULL,
  reasoning TEXT,
  decided_by TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Append-only enforcement
CREATE OR REPLACE RULE no_update_tickets AS ON UPDATE TO nwt_tickets DO INSTEAD NOTHING;
CREATE OR REPLACE RULE no_delete_tickets AS ON DELETE TO nwt_tickets DO INSTEAD NOTHING;

-- Portfolio ledger (single source of truth — all bots, all tracks)
CREATE TABLE IF NOT EXISTS nwt_portfolio_ledger (
  position_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bot_source TEXT NOT NULL,
  strategy_id TEXT,
  asset TEXT NOT NULL,
  asset_type TEXT NOT NULL,
  direction TEXT,
  delta_exposure NUMERIC,
  notional_risk NUMERIC,
  entry_price NUMERIC,
  entry_time TIMESTAMPTZ,
  entry_bid NUMERIC,            -- NBBO at entry — feeds pnl_adjusted haircut
  entry_ask NUMERIC,
  exit_price NUMERIC,
  exit_time TIMESTAMPTZ,
  exit_bid NUMERIC,             -- NBBO at exit
  exit_ask NUMERIC,
  realized_slippage NUMERIC,
  exit_reason TEXT,
  status TEXT DEFAULT 'open',
  alpaca_order_id TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Trade outcomes (Layer A — always active)
CREATE TABLE IF NOT EXISTS nwt_trade_outcomes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id TEXT NOT NULL,
  archetype TEXT,               -- attribution pools at archetype × regime level
  symbol TEXT,
  direction TEXT,
  entry_price NUMERIC,
  entry_time TIMESTAMPTZ,
  exit_price NUMERIC,
  exit_time TIMESTAMPTZ,
  pnl NUMERIC,
  pnl_pct NUMERIC,
  pnl_adjusted NUMERIC,         -- spread-haircut PnL — the number that matters
  pnl_adjusted_pct NUMERIC,
  entry_spread_pct NUMERIC,
  exit_spread_pct NUMERIC,
  iv_at_entry NUMERIC,
  iv_at_exit NUMERIC,
  regime_at_entry JSONB,
  regime_at_exit JSONB,
  dte_at_entry INTEGER,
  slippage NUMERIC,
  slippage_adjusted_efficiency NUMERIC,
  entry_timing_score NUMERIC,
  exit_timing_score NUMERIC,
  thesis_validity TEXT,
  expected_move_capture NUMERIC,
  realized_move_capture NUMERIC,
  closed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Strategy decay tracking (leading indicator)
CREATE TABLE IF NOT EXISTS nwt_strategy_decay (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id TEXT NOT NULL,
  computed_at TIMESTAMPTZ DEFAULT NOW(),
  rolling_expectancy_20 NUMERIC,
  baseline_expectancy NUMERIC,
  expectancy_delta NUMERIC,
  win_loss_ratio_trend TEXT,
  false_positive_rate NUMERIC,
  avg_recovery_days NUMERIC,
  decay_flag BOOLEAN DEFAULT FALSE
);

-- Strategy genome (runtime rule — agents query this at startup)
CREATE TABLE IF NOT EXISTS nwt_strategy_genome (
  strategy_id TEXT PRIMARY KEY,
  track TEXT NOT NULL,
  archetype TEXT,               -- strategy bucket — tracks fire max 1 proposal per archetype/day
  asset_universe TEXT[],
  dte_min INTEGER,
  dte_max INTEGER,
  iv_filter_max NUMERIC,
  entry_threshold NUMERIC,
  stop_loss_pct NUMERIC,
  profit_target_pct NUMERIC,
  regime TEXT,
  version INTEGER DEFAULT 1,
  active BOOLEAN DEFAULT TRUE,
  shadow_mode BOOLEAN DEFAULT FALSE,
  trade_count_to_promote INTEGER DEFAULT 100,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- System event log (used by risk agent, integrity gate)
CREATE TABLE IF NOT EXISTS nwt_system_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  level TEXT NOT NULL,
  component TEXT NOT NULL,
  message TEXT NOT NULL,
  payload JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Green/red session scorecard (one row per trading session)
CREATE TABLE IF NOT EXISTS nwt_session_scorecard (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_date DATE NOT NULL UNIQUE,
  integrity_gate_passed BOOLEAN,
  directives_fresh BOOLEAN,
  conviction_ran BOOLEAN,
  tracks_ran BOOLEAN,
  activity_logged BOOLEAN,      -- proposals OR inactivity rows (do-nothing is valid)
  risk_agent_clear BOOLEAN,     -- ran AND no stale unprocessed proposals
  execution_clear BOOLEAN,      -- no stale unprocessed TRADE_REQUESTs
  learning_agent_ran BOOLEAN,
  manual_interventions INTEGER DEFAULT 0,
  green BOOLEAN,
  details JSONB,
  computed_at TIMESTAMPTZ DEFAULT NOW()
);

-- Inactivity log (do-nothing is a first-class logged state)
CREATE TABLE IF NOT EXISTS nwt_inactivity_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id TEXT NOT NULL,
  track TEXT NOT NULL,
  reason TEXT NOT NULL,
  regime_at_decision JSONB,
  logged_at TIMESTAMPTZ DEFAULT NOW()
);
