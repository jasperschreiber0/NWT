-- NWT Phase 1 Migration — 2026-06-10
-- Focus: strategy concentration, decision-input archiving, intraday PnL view
-- Run as: psql "$NWT_DB_DSN" -f migrate_phase1.sql

BEGIN;

-- ============================================================
-- 1.1 Strategy focus — deactivate all but 2 per track
-- Keeps the two lowest strategy_id numbers per track (C1/C2, D1/D2, E1/E2).
-- Edit the WHERE clause before running if you want different strategies.
-- ============================================================
UPDATE nwt_strategy_genome
SET active = FALSE
WHERE active = TRUE
  AND strategy_id NOT IN (
    (SELECT strategy_id FROM nwt_strategy_genome
     WHERE strategy_id LIKE 'C%' AND active = TRUE
     ORDER BY strategy_id ASC LIMIT 2)
    UNION ALL
    (SELECT strategy_id FROM nwt_strategy_genome
     WHERE strategy_id LIKE 'D%' AND active = TRUE
     ORDER BY strategy_id ASC LIMIT 2)
    UNION ALL
    (SELECT strategy_id FROM nwt_strategy_genome
     WHERE strategy_id LIKE 'E%' AND active = TRUE
     ORDER BY strategy_id ASC LIMIT 2)
  );

-- Verify: should show only 2 active per track
-- SELECT track, COUNT(*) FROM nwt_strategy_genome WHERE active=TRUE GROUP BY track;

-- ============================================================
-- 1.2 Decision-input archive
-- Stores every conviction stack run with full input context.
-- Used for: attribution on rejected trades, backtest replay, LLM vs baseline comparison.
-- ============================================================
CREATE TABLE IF NOT EXISTS nwt_decision_inputs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_date        DATE NOT NULL,
  run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  symbol          TEXT NOT NULL,
  strategy_id     TEXT,
  track           TEXT,            -- 'C', 'D', 'E'
  -- Market state at decision time
  regime          JSONB,
  vix             NUMERIC,
  iv_at_decision  NUMERIC,
  dte_at_decision INTEGER,
  spy_price       NUMERIC,
  -- Options chain snapshot (ATM ± 2 strikes, closest expiry in DTE range)
  chain_snapshot  JSONB,
  -- Conviction inputs
  conviction_score   NUMERIC,
  prescreener_score  NUMERIC,
  layer0_signals     JSONB,
  -- Outcome (filled in at close by learning agent)
  decision           TEXT,        -- 'TRADE_PROPOSED', 'REJECTED_RISK', 'REJECTED_TRACK', 'INACTIVITY'
  rejection_reason   TEXT,
  ticket_id          UUID REFERENCES nwt_tickets(ticket_id),
  outcome_id         UUID REFERENCES nwt_trade_outcomes(id),
  created_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_decision_inputs_run_date ON nwt_decision_inputs (run_date DESC);
CREATE INDEX IF NOT EXISTS idx_decision_inputs_strategy  ON nwt_decision_inputs (strategy_id, run_date DESC);

-- ============================================================
-- 1.3 Intraday PnL view (used by Risk Agent Rule 17)
-- ============================================================
CREATE OR REPLACE VIEW nwt_intraday_pnl AS
SELECT
  DATE_TRUNC('day', closed_at AT TIME ZONE 'UTC') AS trade_date,
  COUNT(*)                                          AS trades_closed,
  SUM(COALESCE(pnl_adjusted, pnl, 0))              AS pnl_total,
  SUM(COALESCE(pnl_adjusted, pnl, 0)) FILTER (WHERE COALESCE(pnl_adjusted, pnl, 0) > 0) AS gross_profit,
  SUM(COALESCE(pnl_adjusted, pnl, 0)) FILTER (WHERE COALESCE(pnl_adjusted, pnl, 0) <= 0) AS gross_loss
FROM nwt_trade_outcomes
WHERE closed_at IS NOT NULL
GROUP BY 1
ORDER BY 1 DESC;

-- ============================================================
-- 1.4 Add Telegram env var reminder to nwt_system_flags
-- (Not a schema change — just a flag record for tracking)
-- ============================================================
INSERT INTO nwt_system_flags (flag, value, reason, set_by)
VALUES ('telegram_configured', FALSE, 'Set to TRUE after TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID added to .env', 'migration')
ON CONFLICT (flag) DO NOTHING;

COMMIT;

-- ============================================================
-- Post-migration checklist
-- ============================================================
-- 1. Verify strategy count per track:
--    SELECT track, COUNT(*), array_agg(strategy_id ORDER BY strategy_id) active_strategies
--    FROM nwt_strategy_genome WHERE active=TRUE GROUP BY track;
--
-- 2. Verify decision_inputs table:
--    \d nwt_decision_inputs
--
-- 3. Verify intraday view:
--    SELECT * FROM nwt_intraday_pnl LIMIT 5;
--
-- 4. Add to .env on server:
--    TELEGRAM_BOT_TOKEN=<from @BotFather>
--    TELEGRAM_CHAT_ID=<your chat ID from @userinfobot>
--    Then: UPDATE nwt_system_flags SET value=TRUE WHERE flag='telegram_configured';
