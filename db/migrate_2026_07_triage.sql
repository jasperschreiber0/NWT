-- nwt_agents — Morning Triage findings
-- Cross-day memory for triage_agent.py. Deduplicates recurring faults by
-- signature so a fault that re-fires every run is ONE open finding (with a
-- first_seen date and a running occurrence count), not N fresh alerts.
-- This is the layer session_scorecard lacks: it makes "same 401 as yesterday,
-- still open" visible instead of re-reporting it as new each morning.

CREATE TABLE IF NOT EXISTS nwt_triage_findings (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  signature      TEXT NOT NULL,                    -- normalized 'component:error_class'
  component      TEXT NOT NULL,
  error_class    TEXT NOT NULL,
  severity       TEXT NOT NULL DEFAULT 'escalate', -- 'escalate' | 'benign'
  status         TEXT NOT NULL DEFAULT 'open',     -- 'open' | 'resolved'
  first_seen     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  occurrences    INTEGER NOT NULL DEFAULT 1,
  sample_message TEXT,
  resolved_at    TIMESTAMPTZ
);

-- At most one OPEN finding per signature — this is the upsert target.
CREATE UNIQUE INDEX IF NOT EXISTS one_open_triage_finding
  ON nwt_triage_findings (signature) WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_triage_status
  ON nwt_triage_findings (status, last_seen DESC);
