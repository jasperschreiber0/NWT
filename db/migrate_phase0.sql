-- NWT Phase 0 Schema Migration — 2026-06-10
-- Run as: psql "$NWT_DB_DSN" -f migrate_phase0.sql
-- Verify each section before proceeding to the next.

BEGIN;

-- ============================================================
-- 0.1 Replace silent DO INSTEAD NOTHING rules with loud trigger
-- ============================================================
DROP RULE IF EXISTS no_update_tickets ON nwt_tickets;
DROP RULE IF EXISTS no_delete_tickets ON nwt_tickets;

CREATE OR REPLACE FUNCTION reject_ticket_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'nwt_tickets is append-only — insert into nwt_ticket_decisions instead';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tickets_immutable ON nwt_tickets;
CREATE TRIGGER tickets_immutable
  BEFORE UPDATE OR DELETE ON nwt_tickets
  FOR EACH ROW EXECUTE FUNCTION reject_ticket_mutation();

-- ============================================================
-- 0.2 Control tables
-- ============================================================
CREATE TABLE IF NOT EXISTS nwt_system_flags (
  flag        TEXT PRIMARY KEY,
  value       BOOLEAN NOT NULL DEFAULT FALSE,
  reason      TEXT,
  set_by      TEXT,
  updated_at  TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO nwt_system_flags (flag, value) VALUES ('no_trade_mode', FALSE)
  ON CONFLICT (flag) DO NOTHING;

CREATE TABLE IF NOT EXISTS nwt_heartbeat (
  service     TEXT PRIMARY KEY,
  last_beat   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  status      TEXT DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS nwt_equity_curve (
  date        DATE PRIMARY KEY,
  equity      NUMERIC NOT NULL,
  source      TEXT DEFAULT 'alpaca',
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 0.3 Version the genome (PK becomes (strategy_id, version))
-- ============================================================
ALTER TABLE nwt_strategy_genome ADD COLUMN IF NOT EXISTS parent_version INTEGER;

DO $$
DECLARE
  pk_col_count INT;
BEGIN
  SELECT COUNT(*) INTO pk_col_count
  FROM pg_constraint c
  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
  WHERE c.conname = 'nwt_strategy_genome_pkey' AND c.contype = 'p';

  IF pk_col_count = 1 THEN
    ALTER TABLE nwt_strategy_genome DROP CONSTRAINT nwt_strategy_genome_pkey;
    ALTER TABLE nwt_strategy_genome ADD PRIMARY KEY (strategy_id, version);
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS one_active_genome
  ON nwt_strategy_genome (strategy_id) WHERE (active = TRUE);

ALTER TABLE nwt_strategy_genome DROP COLUMN IF EXISTS updated_at;
ALTER TABLE nwt_strategy_genome DROP COLUMN IF EXISTS shadow_mode;
ALTER TABLE nwt_strategy_genome DROP COLUMN IF EXISTS trade_count_to_promote;

-- ============================================================
-- 0.4 Link trade outcomes to ledger + add adjusted PnL columns
-- ============================================================
ALTER TABLE nwt_trade_outcomes
  ADD COLUMN IF NOT EXISTS position_id     UUID REFERENCES nwt_portfolio_ledger(position_id),
  ADD COLUMN IF NOT EXISTS pnl_adjusted    NUMERIC,
  ADD COLUMN IF NOT EXISTS slippage_model  TEXT;

-- ============================================================
-- 0.5 Exit reason on portfolio ledger
-- ============================================================
ALTER TABLE nwt_portfolio_ledger
  ADD COLUMN IF NOT EXISTS exit_reason TEXT;

COMMIT;

-- ============================================================
-- Verification queries (run manually after migration)
-- ============================================================
-- 1. Trigger test:
--    INSERT INTO nwt_tickets (from_agent,to_agent,type,payload) VALUES ('TEST','TEST','test','{}');
--    UPDATE nwt_tickets SET type='x' WHERE from_agent='TEST';  -- must raise exception
--
-- 2. Genome uniqueness:
--    INSERT two rows with same strategy_id and active=TRUE — second must fail on one_active_genome
--
-- 3. Tables exist:
--    \dt nwt_system_flags nwt_heartbeat nwt_equity_curve
