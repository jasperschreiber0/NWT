-- Seed all strategy genomes: Track C (C1-C12), Track D (D1-D12), Track E (E1-E12)
-- INSERT ... ON CONFLICT DO NOTHING for idempotency

-- ============================================================
-- TRACK C — Premium Seller
-- dte_min=7, dte_max=21, iv_filter_max=0.80
-- entry_threshold=0.5, stop_loss_pct=0.50, profit_target_pct=0.50
-- ============================================================

INSERT INTO nwt_strategy_genome
  (strategy_id, track, asset_universe, dte_min, dte_max, iv_filter_max, entry_threshold, stop_loss_pct, profit_target_pct, regime, version, active, shadow_mode, trade_count_to_promote)
VALUES
  -- C1: Bull call spread / short put — risk_on, SPY+QQQ
  ('C1', 'C', ARRAY['SPY', 'QQQ'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'risk_on', 1, TRUE, FALSE, 100),
  -- C2: Iron condor — neutral, SPY
  ('C2', 'C', ARRAY['SPY'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'neutral', 1, TRUE, FALSE, 100),
  -- C3: Short call spread — risk_on, QQQ
  ('C3', 'C', ARRAY['QQQ'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'risk_on', 1, TRUE, FALSE, 100),
  -- C4: Cash-secured put — risk_on, SPY
  ('C4', 'C', ARRAY['SPY'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'risk_on', 1, TRUE, FALSE, 100),
  -- C5: Iron condor — neutral, QQQ
  ('C5', 'C', ARRAY['QQQ'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'neutral', 1, TRUE, FALSE, 100),
  -- C6: Bear call spread — risk_off, SPY
  ('C6', 'C', ARRAY['SPY'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'risk_off', 1, TRUE, FALSE, 100),
  -- C7: Short strangle — neutral, SPY+QQQ (low IV, premium selling)
  ('C7', 'C', ARRAY['SPY', 'QQQ'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'neutral', 1, TRUE, FALSE, 100),
  -- C8: Bull put spread — risk_on, AAPL
  ('C8', 'C', ARRAY['AAPL'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'risk_on', 1, TRUE, FALSE, 100),
  -- C9: Bear call spread — risk_off, QQQ
  ('C9', 'C', ARRAY['QQQ'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'risk_off', 1, TRUE, FALSE, 100),
  -- C10: Iron condor — neutral, AAPL+TSLA
  ('C10', 'C', ARRAY['AAPL', 'TSLA'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'neutral', 1, TRUE, FALSE, 100),
  -- C11: Short put — fragile_liquidity, SPY (sell put into dip with IV elevated)
  ('C11', 'C', ARRAY['SPY'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'fragile_liquidity', 1, TRUE, FALSE, 100),
  -- C12: Bull put spread — inflation_concern, SPY+QQQ
  ('C12', 'C', ARRAY['SPY', 'QQQ'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'inflation_concern', 1, TRUE, FALSE, 100)
ON CONFLICT (strategy_id) DO NOTHING;

-- ============================================================
-- TRACK D — Aggressive Directional
-- dte_min=21, dte_max=45, iv_filter_max=0.80
-- entry_threshold=0.55, stop_loss_pct=0.50, profit_target_pct=1.00
-- ============================================================

INSERT INTO nwt_strategy_genome
  (strategy_id, track, asset_universe, dte_min, dte_max, iv_filter_max, entry_threshold, stop_loss_pct, profit_target_pct, regime, version, active, shadow_mode, trade_count_to_promote)
VALUES
  -- D1: Long calls — risk_on, SPY+QQQ
  ('D1', 'D', ARRAY['SPY', 'QQQ'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'risk_on', 1, TRUE, FALSE, 100),
  -- D2: Long puts — risk_off, SPY
  ('D2', 'D', ARRAY['SPY'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'risk_off', 1, TRUE, FALSE, 100),
  -- D3: Bull call spread — risk_on, QQQ (high IV)
  ('D3', 'D', ARRAY['QQQ'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'risk_on', 1, TRUE, FALSE, 100),
  -- D4: Bear put spread — risk_off, QQQ
  ('D4', 'D', ARRAY['QQQ'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'risk_off', 1, TRUE, FALSE, 100),
  -- D5: Long calls — risk_on, NVDA+AAPL (momentum breakout)
  ('D5', 'D', ARRAY['NVDA', 'AAPL'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'risk_on', 1, TRUE, FALSE, 100),
  -- D6: Long puts — recession_fear, SPY+QQQ
  ('D6', 'D', ARRAY['SPY', 'QQQ'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'recession_fear', 1, TRUE, FALSE, 100),
  -- D7: Long calls — risk_on, TSLA (high beta momentum)
  ('D7', 'D', ARRAY['TSLA'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'risk_on', 1, TRUE, FALSE, 100),
  -- D8: Bull call spread — inflation_concern, SPY (defensive long)
  ('D8', 'D', ARRAY['SPY'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'inflation_concern', 1, TRUE, FALSE, 100),
  -- D9: Long puts — geopolitical_stress, SPY+QQQ
  ('D9', 'D', ARRAY['SPY', 'QQQ'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'geopolitical_stress', 1, TRUE, FALSE, 100),
  -- D10: Long calls — fragile_liquidity, QQQ (mean reversion bounce)
  ('D10', 'D', ARRAY['QQQ'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'fragile_liquidity', 1, TRUE, FALSE, 100),
  -- D11: Bear put spread — risk_off, NVDA+TSLA (high beta sell-off)
  ('D11', 'D', ARRAY['NVDA', 'TSLA'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'risk_off', 1, TRUE, FALSE, 100),
  -- D12: Long calls — neutral (breakout from consolidation), SPY+QQQ
  ('D12', 'D', ARRAY['SPY', 'QQQ'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'neutral', 1, TRUE, FALSE, 100)
ON CONFLICT (strategy_id) DO NOTHING;

-- ============================================================
-- TRACK E — Vol Desk / Stat-Arb
-- dte_min=7, dte_max=30, iv_filter_max=0.80
-- entry_threshold=0.60, stop_loss_pct=0.50, profit_target_pct=0.75
-- quantitative_edge field mandatory on every proposed trade
-- ============================================================

INSERT INTO nwt_strategy_genome
  (strategy_id, track, asset_universe, dte_min, dte_max, iv_filter_max, entry_threshold, stop_loss_pct, profit_target_pct, regime, version, active, shadow_mode, trade_count_to_promote)
VALUES
  -- E1: Vol arb — any regime, SPY+VIX (realized vs implied spread)
  ('E1', 'E', ARRAY['SPY', 'VIX'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'any', 1, TRUE, FALSE, 100),
  -- E2: VIX calls — geopolitical + IV>40
  ('E2', 'E', ARRAY['VIX'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'geopolitical_stress', 1, TRUE, FALSE, 100),
  -- E3: Dispersion trade — any regime, SPY vs sector ETFs
  ('E3', 'E', ARRAY['SPY', 'XLK', 'XLF', 'XLE', 'XLV'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'any', 1, TRUE, FALSE, 100),
  -- E4: Calendar spread — neutral, SPY (term structure arb)
  ('E4', 'E', ARRAY['SPY'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'neutral', 1, TRUE, FALSE, 100),
  -- E5: IV crush play — any regime, QQQ (post-event premium selling)
  ('E5', 'E', ARRAY['QQQ'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'any', 1, TRUE, FALSE, 100),
  -- E6: Put/call skew arb — risk_on, SPY+QQQ (skew compression)
  ('E6', 'E', ARRAY['SPY', 'QQQ'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'risk_on', 1, TRUE, FALSE, 100),
  -- E7: Vol surface mean reversion — neutral, SPY
  ('E7', 'E', ARRAY['SPY'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'neutral', 1, TRUE, FALSE, 100),
  -- E8: Cross-asset vol arb — any regime, SPY+GLD+TLT
  ('E8', 'E', ARRAY['SPY', 'GLD', 'TLT'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'any', 1, TRUE, FALSE, 100),
  -- E9: Realized vol underperformance — risk_off, QQQ (buy cheap realized, sell IV)
  ('E9', 'E', ARRAY['QQQ'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'risk_off', 1, TRUE, FALSE, 100),
  -- E10: VIX term structure — fragile_liquidity, VIX (backwardation trade)
  ('E10', 'E', ARRAY['VIX'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'fragile_liquidity', 1, TRUE, FALSE, 100),
  -- E11: Correlation breakdown — any regime, SPY vs NVDA+AAPL+MSFT
  ('E11', 'E', ARRAY['SPY', 'NVDA', 'AAPL', 'MSFT'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'any', 1, TRUE, FALSE, 100),
  -- E12: Tail hedge — geopolitical_stress + recession_fear, SPY puts + VIX calls
  ('E12', 'E', ARRAY['SPY', 'VIX'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'geopolitical_stress', 1, TRUE, FALSE, 100)
ON CONFLICT (strategy_id) DO NOTHING;

-- ============================================================
-- TRACK A — Signal Bots (equity only, no options)
-- These rows are queried at startup by each bot's strategist.
-- stop_loss_pct / profit_target_pct are expressed as fractions (0.006 = 0.6%).
-- dte_min / dte_max: NULL for equity-only strategies.
-- iv_filter_max: NULL for equity-only strategies.
-- ============================================================

INSERT INTO nwt_strategy_genome
  (strategy_id, track, asset_universe, dte_min, dte_max, iv_filter_max, entry_threshold, stop_loss_pct, profit_target_pct, regime, version, active, shadow_mode, trade_count_to_promote)
VALUES
  -- US-ORB-001: Opening Range Breakout — US Flow Bot (intraday to 5d holds)
  ('US-ORB-001', 'A', ARRAY['SPY', 'QQQ', 'AAPL', 'TSLA', 'NVDA'], NULL, NULL, NULL, 0.60, 0.006, 0.012, 'any', 1, TRUE, FALSE, 100),

  -- EU-MR-001: Mean Reversion — EU Bot (2-20d holds)
  ('EU-MR-001', 'A', ARRAY['VGK', 'EWU', 'FEZ'], NULL, NULL, NULL, 0.50, 0.015, 0.030, 'any', 1, TRUE, FALSE, 100),

  -- AUS-DIV-001: Dividend Capture — AUS Bot (1-8wk holds)
  ('AUS-DIV-001', 'A', ARRAY['EWA', 'BHP', 'RIO'], NULL, NULL, NULL, 0.60, 0.020, 0.040, 'any', 1, TRUE, FALSE, 100),

  -- AUS-MOM-001: Momentum — AUS Bot fallback (1-8wk holds)
  ('AUS-MOM-001', 'A', ARRAY['EWA', 'BHP', 'RIO'], NULL, NULL, NULL, 0.55, 0.020, 0.040, 'any', 1, TRUE, FALSE, 100),

  -- CHINA-POL-001: Policy/Event — China Bot (1d to 3wk holds)
  ('CHINA-POL-001', 'A', ARRAY['FXI', 'KWEB', 'MCHI', 'BABA', 'TCEHY'], NULL, NULL, NULL, 0.50, 0.025, 0.050, 'any', 1, TRUE, FALSE, 100)

ON CONFLICT (strategy_id) DO NOTHING;
