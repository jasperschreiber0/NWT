-- NWT — persist client_order_id for auditability — 2026-07-21
-- Run as: psql "$NWT_DB_DSN" -f migrate_2026_07_order_submissions.sql
-- Idempotent — safe to re-run.
--
-- client_order_id_for(ticket_id) is deterministic — it can always be
-- recomputed from a ticket_id alone, so this table is a convenience index
-- for humans/tooling (so "what client_order_id did we send Alpaca for
-- ticket X" is a lookup, not a recomputation), not a new source of truth.
-- nwt_tickets is append-only (INSERT-only, enforced by a Postgres rule),
-- so client_order_id cannot live there as a column filled in after the
-- fact — this is a separate, normal (non-append-only) table instead,
-- written once at the moment order submission begins, before the outcome
-- is known.

BEGIN;

CREATE TABLE IF NOT EXISTS nwt_order_submissions (
  ticket_id        UUID PRIMARY KEY REFERENCES nwt_tickets(ticket_id),
  client_order_id  TEXT NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_order_submissions_client_order_id
  ON nwt_order_submissions (client_order_id);

COMMENT ON TABLE nwt_order_submissions IS
  'One row per ticket_id whose order submission began — records the exact '
  'client_order_id sent to Alpaca, for audit/lookup. client_order_id_for() '
  'remains the source of truth (it is deterministic and always '
  'recomputable from ticket_id alone); this table exists so a human does '
  'not have to recompute it to look up what was sent for a given ticket.';

COMMIT;

-- ============================================================
-- Post-migration checklist
-- ============================================================
-- 1. \d nwt_order_submissions   -- confirm table + FK to nwt_tickets
