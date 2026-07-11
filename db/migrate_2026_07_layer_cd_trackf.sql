-- NWT — Learning Layer C/D + Track F scanner — 2026-07-11
-- Run as: psql "$NWT_DB_DSN" -f migrate_2026_07_layer_cd_trackf.sql
-- Idempotent — safe to re-run.

BEGIN;

-- ============================================================
-- 1. Generic agent status surface (dashboard's /api/performance already
--    queries this via q_grace and silently got nothing — table never existed)
-- ============================================================
CREATE TABLE IF NOT EXISTS nwt_agent_state (
  agent       TEXT PRIMARY KEY,
  status      TEXT NOT NULL DEFAULT 'ok',   -- 'ok' | 'degraded' | 'error'
  detail      JSONB,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- 2. Strategy Mutator (Learning Layer C)
-- ============================================================

-- Shadow-mutation evaluations need to be tied to the specific genome
-- version they were evaluated against — nwt_decision_inputs previously only
-- tracked strategy_id, which conflates a strategy's baseline with any
-- shadow mutation candidate sharing the same strategy_id.
ALTER TABLE nwt_decision_inputs
  ADD COLUMN IF NOT EXISTS genome_version INTEGER;

CREATE INDEX IF NOT EXISTS idx_decision_inputs_genome_version
  ON nwt_decision_inputs (strategy_id, genome_version);

-- Risk Agent authority: "Freeze mutation promotion" — previously had no flag.
INSERT INTO nwt_system_flags (flag, value, reason, set_by)
VALUES ('mutation_frozen', FALSE, NULL, 'migration')
ON CONFLICT (flag) DO NOTHING;

-- Audit trail: every mutation proposal and promotion/rejection decision,
-- append-only, mirroring nwt_tickets' philosophy for this specific
-- high-stakes action ("the mutation engine is the most dangerous
-- component in the system").
CREATE TABLE IF NOT EXISTS nwt_mutation_log (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id       TEXT NOT NULL,
  parent_version    INTEGER NOT NULL,
  new_version       INTEGER,             -- NULL until a shadow version is actually created
  action            TEXT NOT NULL,        -- 'proposed' | 'promoted' | 'rejected' | 'retired'
  parameter_changed TEXT,
  old_value         NUMERIC,
  new_value         NUMERIC,
  reasoning         TEXT,
  evidence          JSONB,                -- sample size, regimes covered, comparison stats
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mutation_log_strategy
  ON nwt_mutation_log (strategy_id, created_at DESC);

-- ============================================================
-- 3. Portfolio Allocator (Learning Layer D) — audit trail
-- ============================================================
CREATE TABLE IF NOT EXISTS nwt_allocator_history (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  computed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  bot                TEXT NOT NULL,
  regime             TEXT,
  baseline_weight    NUMERIC,
  dynamic_weight     NUMERIC,
  sample_trades      INTEGER,
  rolling_expectancy NUMERIC,
  sharpe_proxy       NUMERIC,
  note               TEXT
);

CREATE INDEX IF NOT EXISTS idx_allocator_history_computed_at
  ON nwt_allocator_history (computed_at DESC);

-- ============================================================
-- 4. Track F — thematic bottleneck scanner
-- ============================================================

-- Per-ticker score for a given theme, one row per scan run (history kept —
-- dashboard reads DISTINCT ON (ticker) ... ORDER BY scored_at DESC for latest).
CREATE TABLE IF NOT EXISTS nwt_bottleneck_scores (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticker          TEXT NOT NULL,
  theme           TEXT NOT NULL,
  bottleneck_score NUMERIC NOT NULL,     -- 0-100
  mention_count   INTEGER,
  momentum        NUMERIC,               -- vs trailing baseline scans
  evidence        JSONB,                 -- filing accession numbers, matched terms
  scored_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bottleneck_scores_ticker
  ON nwt_bottleneck_scores (ticker, scored_at DESC);

-- Candidate themes not yet on the confirmed watchlist — human approves or
-- rejects via the dashboard before a theme's tickers count toward exposure
-- caps or nwt_track_f_candidates.
CREATE TABLE IF NOT EXISTS nwt_emerging_themes (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_theme TEXT NOT NULL UNIQUE,
  tickers         TEXT[],
  momentum        NUMERIC,
  evidence        JSONB,
  status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'approved' | 'rejected'
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  approved_at     TIMESTAMPTZ
);

-- Actionable, human-reviewable candidates surfaced from confirmed themes
-- (bottleneck_score above threshold). No order authority — this is a
-- research signal surface, not a trading track: CLAUDE.md defines no
-- sizing/risk/isolation rules for Track F, so it stays here in v1.
CREATE TABLE IF NOT EXISTS nwt_track_f_candidates (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticker           TEXT NOT NULL,
  theme            TEXT NOT NULL,
  bottleneck_score NUMERIC,
  rationale        TEXT,
  status           TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'approved' | 'rejected'
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_track_f_candidates_status
  ON nwt_track_f_candidates (status, created_at DESC);

COMMIT;

-- ============================================================
-- Post-migration checklist
-- ============================================================
-- 1. \dt nwt_agent_state nwt_mutation_log nwt_allocator_history
--    nwt_bottleneck_scores nwt_emerging_themes nwt_track_f_candidates
-- 2. SELECT * FROM nwt_system_flags WHERE flag='mutation_frozen';
-- 3. \d nwt_decision_inputs   -- confirm genome_version present
