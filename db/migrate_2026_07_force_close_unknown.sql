-- NWT — FORCE_CLOSE unknown-outcome handling — 2026-07-21
-- Run as: psql "$NWT_DB_DSN" -f migrate_2026_07_force_close_unknown.sql
-- Idempotent — safe to re-run.
--
-- Confirmed gap (targeted remediation round 4): process_force_close() can
-- (1) send DELETE /positions, (2) lose its claim before recording the
-- outcome, (3) raise ClaimLostError, (4) never call
-- record_force_close_outcome() — leaving nwt_force_close_state stuck at
-- ATTEMPTING forever, with no record that a real broker action may have
-- already happened. This adds an explicit UNKNOWN state (kept alongside
-- the existing PENDING/ATTEMPTING/SUCCESS/FAILED_RETRYABLE/
-- FAILED_TERMINAL/FAILED_REQUIRES_HUMAN states — not replacing them, so
-- the existing backoff/escalation machinery and its tests are unaffected)
-- plus two columns to record exactly which ticket/worker was in flight
-- when the outcome became unknown, so reconciliation has something to
-- query the broker against.

BEGIN;

ALTER TABLE nwt_force_close_state DROP CONSTRAINT IF EXISTS force_close_state_valid;
ALTER TABLE nwt_force_close_state ADD CONSTRAINT force_close_state_valid CHECK (
  state IN ('PENDING', 'ATTEMPTING', 'SUCCESS', 'FAILED_RETRYABLE',
            'FAILED_TERMINAL', 'FAILED_REQUIRES_HUMAN', 'UNKNOWN')
);

ALTER TABLE nwt_force_close_state
  ADD COLUMN IF NOT EXISTS last_ticket_id UUID,
  ADD COLUMN IF NOT EXISTS last_worker_id TEXT;

COMMENT ON COLUMN nwt_force_close_state.last_ticket_id IS
  'The ticket_id being processed when the outcome most recently became '
  'UNKNOWN (claim lost after a broker call may have already happened). '
  'NULL outside that scenario.';
COMMENT ON COLUMN nwt_force_close_state.last_worker_id IS
  'The WORKER_ID that lost its claim, for the same UNKNOWN scenario. '
  'NULL outside that scenario.';

COMMIT;

-- ============================================================
-- Post-migration checklist
-- ============================================================
-- 1. \d nwt_force_close_state   -- confirm UNKNOWN in the check constraint
--                                  and last_ticket_id/last_worker_id present
