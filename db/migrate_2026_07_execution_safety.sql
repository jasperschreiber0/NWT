-- NWT — Execution safety: idempotent ticket claiming + force-close terminal states — 2026-07-21
-- Run as: psql "$NWT_DB_DSN" -f migrate_2026_07_execution_safety.sql
-- Idempotent — safe to re-run.
--
-- Confirmed by code audit (traced, not assumed):
--   1. crontab.txt runs risk_agent.py / execution_agent.py / execution/engine.py
--      every 5 minutes with no flock/overlap guard. Their ticket-selection
--      queries are "SELECT unclaimed WHERE NOT EXISTS (a decision yet)" and
--      only write the claiming decision AFTER slow external Alpaca calls
--      complete (quote fetch, chain resolution, order placement, up to
--      POLL_MAX*POLL_INTERVAL=30s polling per order). That is a real
--      TOCTOU window: two overlapping cron runs can both select the same
--      not-yet-decided ticket and both place a real broker order for it.
--   2. risk_agent.py's Rule 12 (force_close_past_hard_close) already had a
--      15-minute cooldown + 3-attempt CRITICAL escalation, but no terminal
--      state — a position that can never close (expired option, permanently
--      rejected) generates a fresh FORCE_CLOSE ticket forever.
--
-- This migration adds:
--   nwt_ticket_claims     — atomic single-owner claim + lease, so a ticket
--                           can only be actively worked by one process at a
--                           time, with stale-lease recovery after a crash.
--   nwt_force_close_state — explicit per-position lifecycle
--                           (PENDING/ATTEMPTING/SUCCESS/FAILED_RETRYABLE/
--                           FAILED_TERMINAL/FAILED_REQUIRES_HUMAN) with
--                           exponential backoff and a hard attempt ceiling.

BEGIN;

-- ============================================================
-- 1. Ticket claims — one live owner per ticket_id, lease-based recovery
-- ============================================================
CREATE TABLE IF NOT EXISTS nwt_ticket_claims (
  ticket_id        UUID PRIMARY KEY REFERENCES nwt_tickets(ticket_id),
  claimed_by       TEXT NOT NULL,
  claimed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  lease_expires_at TIMESTAMPTZ NOT NULL,
  status           TEXT NOT NULL DEFAULT 'in_progress',
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ticket_claims_status_valid CHECK (status IN ('in_progress', 'done', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_ticket_claims_lease
  ON nwt_ticket_claims (lease_expires_at) WHERE status = 'in_progress';

COMMENT ON TABLE nwt_ticket_claims IS
  'Atomic claim table protecting the window between "ticket selected" and '
  '"decision written". claim_ticket() uses INSERT ... ON CONFLICT DO UPDATE '
  '... WHERE (not currently claimed OR lease expired) RETURNING ticket_id — '
  'exactly one concurrent caller gets a row back. Lease expiry lets a '
  'crashed worker''s claim be safely reclaimed instead of deadlocking the '
  'ticket forever.';

-- ============================================================
-- 2. Force-close lifecycle — explicit terminal states, bounded retries
-- ============================================================
CREATE TABLE IF NOT EXISTS nwt_force_close_state (
  position_id     UUID PRIMARY KEY REFERENCES nwt_portfolio_ledger(position_id),
  asset           TEXT NOT NULL,
  state           TEXT NOT NULL DEFAULT 'PENDING',
  attempt_count   INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TIMESTAMPTZ,
  last_error      TEXT,
  next_retry_at   TIMESTAMPTZ,
  terminal_reason TEXT,
  escalated_at    TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT force_close_state_valid CHECK (
    state IN ('PENDING', 'ATTEMPTING', 'SUCCESS', 'FAILED_RETRYABLE',
              'FAILED_TERMINAL', 'FAILED_REQUIRES_HUMAN')
  )
);

CREATE INDEX IF NOT EXISTS idx_force_close_state_state
  ON nwt_force_close_state (state);

COMMENT ON TABLE nwt_force_close_state IS
  'One row per ledger position under force-close. State machine: '
  'PENDING -> ATTEMPTING -> SUCCESS, or ATTEMPTING -> FAILED_RETRYABLE '
  '(exponential backoff via next_retry_at) -> ATTEMPTING again, or '
  '-> FAILED_TERMINAL (broker confirms unrecoverable: expired/already '
  'closed) or FAILED_REQUIRES_HUMAN (exhausted the attempt ceiling). '
  'SUCCESS/FAILED_TERMINAL/FAILED_REQUIRES_HUMAN are terminal — '
  'risk_agent.py must never generate another FORCE_CLOSE ticket for a '
  'position in one of these states.';

COMMIT;

-- ============================================================
-- Post-migration checklist
-- ============================================================
-- 1. \d nwt_ticket_claims        -- confirm table + status check constraint
-- 2. \d nwt_force_close_state    -- confirm table + state check constraint
-- 3. SELECT COUNT(*) FROM nwt_ticket_claims;        -- expect 0 on fresh apply
-- 4. SELECT COUNT(*) FROM nwt_force_close_state;    -- expect 0 on fresh apply
