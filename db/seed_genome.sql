-- Seed all strategy genomes: Track C (C1-C12), Track D (D1-D12), Track E (E1-E12)
-- INSERT ... ON CONFLICT DO NOTHING for idempotency
--
-- ARCHETYPES: attribution and daily proposal dedup pool at archetype level.
-- 36 strategy_ids cannot reach meaningful per-strategy sample sizes in 60 days;
-- tracks fire at most ONE proposal per archetype per day, and the Learning Agent
-- computes win rates / decay per archetype. strategy_id is kept on every row.

-- ============================================================
-- TRACK C — Premium Seller
-- dte_min=7, dte_max=21, iv_filter_max=0.80
-- entry_threshold=0.5, stop_loss_pct=0.50, profit_target_pct=0.50
-- Archetypes: C-SHORT-PREMIUM-DIRECTIONAL | C-CONDOR-NEUTRAL
-- ============================================================

INSERT INTO nwt_strategy_genome
  (strategy_id, track, archetype, asset_universe, dte_min, dte_max, iv_filter_max, entry_threshold, stop_loss_pct, profit_target_pct, regime, version, active, shadow_mode, trade_count_to_promote)
VALUES
  -- C1: Bull call spread / short put — risk_on, SPY+QQQ
  ('C1', 'C', 'C-SHORT-PREMIUM-DIRECTIONAL', ARRAY['SPY', 'QQQ'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'risk_on', 1, TRUE, FALSE, 100),
  -- C2: Iron condor — neutral, SPY
  ('C2', 'C', 'C-CONDOR-NEUTRAL', ARRAY['SPY'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'neutral', 1, TRUE, FALSE, 100),
  -- C3: Short call spread — risk_on, QQQ
  ('C3', 'C', 'C-SHORT-PREMIUM-DIRECTIONAL', ARRAY['QQQ'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'risk_on', 1, TRUE, FALSE, 100),
  -- C4: Cash-secured put — risk_on, SPY
  ('C4', 'C', 'C-SHORT-PREMIUM-DIRECTIONAL', ARRAY['SPY'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'risk_on', 1, TRUE, FALSE, 100),
  -- C5: Iron condor — neutral, QQQ
  ('C5', 'C', 'C-CONDOR-NEUTRAL', ARRAY['QQQ'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'neutral', 1, TRUE, FALSE, 100),
  -- C6: Bear call spread — risk_off, SPY
  ('C6', 'C', 'C-SHORT-PREMIUM-DIRECTIONAL', ARRAY['SPY'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'risk_off', 1, TRUE, FALSE, 100),
  -- C7: Short strangle — neutral, SPY+QQQ (low IV, premium selling)
  ('C7', 'C', 'C-CONDOR-NEUTRAL', ARRAY['SPY', 'QQQ'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'neutral', 1, TRUE, FALSE, 100),
  -- C8: Bull put spread — risk_on, AAPL
  ('C8', 'C', 'C-SHORT-PREMIUM-DIRECTIONAL', ARRAY['AAPL'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'risk_on', 1, TRUE, FALSE, 100),
  -- C9: Bear call spread — risk_off, QQQ
  ('C9', 'C', 'C-SHORT-PREMIUM-DIRECTIONAL', ARRAY['QQQ'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'risk_off', 1, TRUE, FALSE, 100),
  -- C10: Iron condor — neutral, AAPL+TSLA
  ('C10', 'C', 'C-CONDOR-NEUTRAL', ARRAY['AAPL', 'TSLA'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'neutral', 1, TRUE, FALSE, 100),
  -- C11: Short put — fragile_liquidity, SPY (sell put into dip with IV elevated)
  ('C11', 'C', 'C-SHORT-PREMIUM-DIRECTIONAL', ARRAY['SPY'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'fragile_liquidity', 1, TRUE, FALSE, 100),
  -- C12: Bull put spread — inflation_concern, SPY+QQQ
  ('C12', 'C', 'C-SHORT-PREMIUM-DIRECTIONAL', ARRAY['SPY', 'QQQ'], 7, 21, 0.80, 0.50, 0.50, 0.50, 'inflation_concern', 1, TRUE, FALSE, 100)
ON CONFLICT (strategy_id) DO NOTHING;

-- ============================================================
-- TRACK D — Aggressive Directional
-- dte_min=21, dte_max=45, iv_filter_max=0.80
-- entry_threshold=0.55, stop_loss_pct=0.50, profit_target_pct=1.00
-- Archetypes: D-LONG-DIRECTIONAL | D-SPREAD-DIRECTIONAL
-- ============================================================

INSERT INTO nwt_strategy_genome
  (strategy_id, track, archetype, asset_universe, dte_min, dte_max, iv_filter_max, entry_threshold, stop_loss_pct, profit_target_pct, regime, version, active, shadow_mode, trade_count_to_promote)
VALUES
  -- D1: Long calls — risk_on, SPY+QQQ
  ('D1', 'D', 'D-LONG-DIRECTIONAL', ARRAY['SPY', 'QQQ'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'risk_on', 1, TRUE, FALSE, 100),
  -- D2: Long puts — risk_off, SPY
  ('D2', 'D', 'D-LONG-DIRECTIONAL', ARRAY['SPY'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'risk_off', 1, TRUE, FALSE, 100),
  -- D3: Bull call spread — risk_on, QQQ (high IV)
  ('D3', 'D', 'D-SPREAD-DIRECTIONAL', ARRAY['QQQ'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'risk_on', 1, TRUE, FALSE, 100),
  -- D4: Bear put spread — risk_off, QQQ
  ('D4', 'D', 'D-SPREAD-DIRECTIONAL', ARRAY['QQQ'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'risk_off', 1, TRUE, FALSE, 100),
  -- D5: Long calls — risk_on, NVDA+AAPL (momentum breakout)
  ('D5', 'D', 'D-LONG-DIRECTIONAL', ARRAY['NVDA', 'AAPL'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'risk_on', 1, TRUE, FALSE, 100),
  -- D6: Long puts — recession_fear, SPY+QQQ
  ('D6', 'D', 'D-LONG-DIRECTIONAL', ARRAY['SPY', 'QQQ'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'recession_fear', 1, TRUE, FALSE, 100),
  -- D7: Long calls — risk_on, TSLA (high beta momentum)
  ('D7', 'D', 'D-LONG-DIRECTIONAL', ARRAY['TSLA'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'risk_on', 1, TRUE, FALSE, 100),
  -- D8: Bull call spread — inflation_concern, SPY (defensive long)
  ('D8', 'D', 'D-SPREAD-DIRECTIONAL', ARRAY['SPY'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'inflation_concern', 1, TRUE, FALSE, 100),
  -- D9: Long puts — geopolitical_stress, SPY+QQQ
  ('D9', 'D', 'D-LONG-DIRECTIONAL', ARRAY['SPY', 'QQQ'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'geopolitical_stress', 1, TRUE, FALSE, 100),
  -- D10: Long calls — fragile_liquidity, QQQ (mean reversion bounce)
  ('D10', 'D', 'D-LONG-DIRECTIONAL', ARRAY['QQQ'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'fragile_liquidity', 1, TRUE, FALSE, 100),
  -- D11: Bear put spread — risk_off, NVDA+TSLA (high beta sell-off)
  ('D11', 'D', 'D-SPREAD-DIRECTIONAL', ARRAY['NVDA', 'TSLA'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'risk_off', 1, TRUE, FALSE, 100),
  -- D12: Long calls — neutral (breakout from consolidation), SPY+QQQ
  ('D12', 'D', 'D-LONG-DIRECTIONAL', ARRAY['SPY', 'QQQ'], 21, 45, 0.80, 0.55, 0.50, 1.00, 'neutral', 1, TRUE, FALSE, 100)
ON CONFLICT (strategy_id) DO NOTHING;

-- ============================================================
-- TRACK E — Vol Desk / Stat-Arb — SHADOW MODE
-- dte_min=7, dte_max=30, iv_filter_max=0.80
-- entry_threshold=0.60, stop_loss_pct=0.50, profit_target_pct=0.75
-- quantitative_edge field mandatory on every proposed trade
--
-- Entire track seeded shadow_mode=TRUE: it logs inactivity (SHADOW_MODE)
-- instead of trading until quantitative_edge computation is proven
-- deterministic and reliable (see Track E IV=0 false-edge incident, May 2026).
-- ============================================================

INSERT INTO nwt_strategy_genome
  (strategy_id, track, archetype, asset_universe, dte_min, dte_max, iv_filter_max, entry_threshold, stop_loss_pct, profit_target_pct, regime, version, active, shadow_mode, trade_count_to_promote)
VALUES
  -- E1: Vol arb — any regime, SPY+VIX (realized vs implied spread)
  ('E1', 'E', 'E-VOL-DESK', ARRAY['SPY', 'VIX'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'any', 1, TRUE, TRUE, 100),
  -- E2: VIX calls — geopolitical + IV>40
  ('E2', 'E', 'E-VOL-DESK', ARRAY['VIX'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'geopolitical_stress', 1, TRUE, TRUE, 100),
  -- E3: Dispersion trade — any regime, SPY vs sector ETFs
  ('E3', 'E', 'E-VOL-DESK', ARRAY['SPY', 'XLK', 'XLF', 'XLE', 'XLV'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'any', 1, TRUE, TRUE, 100),
  -- E4: Calendar spread — neutral, SPY (term structure arb)
  ('E4', 'E', 'E-VOL-DESK', ARRAY['SPY'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'neutral', 1, TRUE, TRUE, 100),
  -- E5: IV crush play — any regime, QQQ (post-event premium selling)
  ('E5', 'E', 'E-VOL-DESK', ARRAY['QQQ'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'any', 1, TRUE, TRUE, 100),
  -- E6: Put/call skew arb — risk_on, SPY+QQQ (skew compression)
  ('E6', 'E', 'E-VOL-DESK', ARRAY['SPY', 'QQQ'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'risk_on', 1, TRUE, TRUE, 100),
  -- E7: Vol surface mean reversion — neutral, SPY
  ('E7', 'E', 'E-VOL-DESK', ARRAY['SPY'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'neutral', 1, TRUE, TRUE, 100),
  -- E8: Cross-asset vol arb — any regime, SPY+GLD+TLT
  ('E8', 'E', 'E-VOL-DESK', ARRAY['SPY', 'GLD', 'TLT'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'any', 1, TRUE, TRUE, 100),
  -- E9: Realized vol underperformance — risk_off, QQQ (buy cheap realized, sell IV)
  ('E9', 'E', 'E-VOL-DESK', ARRAY['QQQ'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'risk_off', 1, TRUE, TRUE, 100),
  -- E10: VIX term structure — fragile_liquidity, VIX (backwardation trade)
  ('E10', 'E', 'E-VOL-DESK', ARRAY['VIX'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'fragile_liquidity', 1, TRUE, TRUE, 100),
  -- E11: Correlation breakdown — any regime, SPY vs NVDA+AAPL+MSFT
  ('E11', 'E', 'E-VOL-DESK', ARRAY['SPY', 'NVDA', 'AAPL', 'MSFT'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'any', 1, TRUE, TRUE, 100),
  -- E12: Tail hedge — geopolitical_stress + recession_fear, SPY puts + VIX calls
  ('E12', 'E', 'E-VOL-DESK', ARRAY['SPY', 'VIX'], 7, 30, 0.80, 0.60, 0.50, 0.75, 'geopolitical_stress', 1, TRUE, TRUE, 100)
ON CONFLICT (strategy_id) DO NOTHING;

-- ============================================================
-- TRACK A — Signal Bots (equity only, no options)
-- These rows are queried at startup by each bot's strategist.
-- stop_loss_pct / profit_target_pct are expressed as fractions (0.006 = 0.6%).
-- dte_min / dte_max: NULL for equity-only strategies.
-- iv_filter_max: NULL for equity-only strategies.
-- archetype = strategy_id (each Track A strategy is already its own bucket).
-- ============================================================

INSERT INTO nwt_strategy_genome
  (strategy_id, track, archetype, asset_universe, dte_min, dte_max, iv_filter_max, entry_threshold, stop_loss_pct, profit_target_pct, regime, version, active, shadow_mode, trade_count_to_promote)
VALUES
  -- US-ORB-001: Opening Range Breakout — US Flow Bot (intraday to 5d holds)
  ('US-ORB-001', 'A', 'US-ORB-001', ARRAY['SPY', 'QQQ', 'AAPL', 'TSLA', 'NVDA'], NULL, NULL, NULL, 0.60, 0.006, 0.012, 'any', 1, TRUE, FALSE, 100),

  -- EU-MR-001: Mean Reversion — EU Bot (2-20d holds)
  ('EU-MR-001', 'A', 'EU-MR-001', ARRAY['VGK', 'EWU', 'FEZ'], NULL, NULL, NULL, 0.50, 0.015, 0.030, 'any', 1, TRUE, FALSE, 100),

  -- AUS-DIV-001: Dividend Capture — AUS Bot (1-8wk holds)
  ('AUS-DIV-001', 'A', 'AUS-DIV-001', ARRAY['EWA', 'BHP', 'RIO'], NULL, NULL, NULL, 0.60, 0.020, 0.040, 'any', 1, TRUE, FALSE, 100),

  -- AUS-MOM-001: Momentum — AUS Bot fallback (1-8wk holds)
  ('AUS-MOM-001', 'A', 'AUS-MOM-001', ARRAY['EWA', 'BHP', 'RIO'], NULL, NULL, NULL, 0.55, 0.020, 0.040, 'any', 1, TRUE, FALSE, 100),

  -- CHINA-POL-001: Policy/Event — China Bot (1d to 3wk holds)
  ('CHINA-POL-001', 'A', 'CHINA-POL-001', ARRAY['FXI', 'KWEB', 'MCHI', 'BABA', 'TCEHY'], NULL, NULL, NULL, 0.50, 0.025, 0.050, 'any', 1, TRUE, FALSE, 100)

ON CONFLICT (strategy_id) DO NOTHING;
