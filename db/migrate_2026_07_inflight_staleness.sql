-- NWT — In-flight close order staleness detection — 2026-07-23
-- Run as: psql "$NWT_DB_DSN" -f migrate_2026_07_inflight_staleness.sql
-- Idempotent — safe to re-run.
--
-- Why: the AAPL incident (2026-07-22 20:00 UTC) — a buy-to-close order was
-- accepted by Alpaca two seconds after market close and never filled.
-- has_pending_inflight_close() correctly prevented 8 duplicate close
-- orders from stacking on top of it (working as designed), but nothing
-- ever detects or escalates an in-flight order that simply never resolves
-- — resolve_inflight_orders() deliberately never cancels a 'close' kind
-- row ("a working close is still reducing risk"), which is the right
-- instinct but left no timeout at all. It can sit pending forever with
-- zero visibility.
--
-- No new status enum: reuses the existing pending/resolved/dead values.
-- stale_since marks when a close order first crossed the staleness
-- threshold (for visibility and to gate the one-time cancel attempt);
-- last_checked_at is updated on every poll for observability, independent
-- of whether the row's state actually changed.

BEGIN;

ALTER TABLE nwt_inflight_orders
  ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS stale_since TIMESTAMPTZ;

COMMENT ON COLUMN nwt_inflight_orders.stale_since IS
  'Set once, the first time a close-kind row is found still pending past '
  'INFLIGHT_CLOSE_STALE_MINUTES. Gates the one-time cancel attempt — never '
  're-triggers on later polls. If the row is still pending '
  'INFLIGHT_CLOSE_ESCALATE_MINUTES after creation, it is retired as dead '
  '(resolution=requires_human_stale_timeout) regardless of stale_since, '
  'handing the position back to schedule_close_attempt''s own bounded '
  'retry/escalation ceiling — this table never invents a second terminal '
  'state parallel to nwt_force_close_state.';

COMMIT;

-- Post-migration checklist:
-- 1. \d nwt_inflight_orders  -- confirm last_checked_at, stale_since present
