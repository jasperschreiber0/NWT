-- Migration: P0-1 execution idempotency (claim-then-finalize + duplicate-order protection)
-- July 2026. Idempotent — safe to re-run.
--
-- Why: execution/engine.py's "ticket found -> Alpaca order placed -> decision
-- recorded" pattern was not crash-safe. A process kill (or an unhandled
-- exception in an unwrapped code path) between placing a live order and
-- recording nwt_ticket_decisions left the ticket looking untouched, so the
-- next 5-minute cron cycle retried it and placed a SECOND live order for the
-- same signal. This migration adds the constraints the new claim-before-
-- execute code in execution/engine.py depends on:
--
--   1. Exactly one EXECUTION_ENGINE decision row can exist per ticket, FROM
--      2026-07-24 ONWARD (see below for why this is a partial, not a full,
--      index). This is what makes the CLAIMED -> finalized upsert in
--      insert_decision() safe, and what makes a concurrent/duplicate claim
--      attempt fail loudly (ON CONFLICT) instead of silently creating a
--      second row.
--
--   2. Exactly one ledger row can exist per (alpaca_order_id, asset). A
--      resumed/retried ticket that reuses an already-placed order (found via
--      client_order_id) must not re-insert the same fill twice. A single
--      order legitimately produces multiple ledger rows for a multi-leg
--      spread (one per leg/asset), so the constraint is on the pair, not on
--      alpaca_order_id alone. NULL alpaca_order_id (e.g. recon_agent.py's
--      cold-start UNATTRIBUTED imports, which never went through an order
--      placement) is explicitly excluded — nothing to dedupe there.
--
-- Constraint 1 is intentionally a PARTIAL index (created_at >= 2026-07-24),
-- not a full one, discovered during the first deploy attempt: production
-- already has 16 tickets carrying two legitimate EXECUTION_ENGINE decision
-- rows each (e.g. SUBMITTED -> later resolved to EXECUTED), from an older
-- version of the engine that tracked in-flight orders across cycles — a
-- mechanism the current code no longer has (grep for "SUBMITTED" or
-- "in-flight" in execution/engine.py: no matches). Those rows are real
-- audit history for a single fill each, not duplicate orders, and are not
-- touched or deleted by this migration. The latest such row confirmed in
-- production is 2026-07-23 13:00:03 UTC; 2026-07-24 00:00:00 UTC is a safe
-- cutoff with margin on both sides — after all known legacy rows, before
-- this fix's actual deploy. Old tickets are already terminal and are never
-- re-fetched by fetch_pending_tickets/fetch_force_close_tickets, so they
-- can never collide with this constraint going forward regardless.
-- execution/engine.py's insert_decision() and claim_or_resume_ticket() ON
-- CONFLICT clauses must carry the identical predicate for Postgres to use
-- this index as their conflict-inference target — see IDEMPOTENCY_CUTOFF.

CREATE UNIQUE INDEX IF NOT EXISTS one_decision_per_agent
  ON nwt_ticket_decisions (ticket_id, decided_by)
  WHERE created_at >= '2026-07-24T00:00:00+00:00';

CREATE UNIQUE INDEX IF NOT EXISTS one_ledger_row_per_order_asset
  ON nwt_portfolio_ledger (alpaca_order_id, asset)
  WHERE alpaca_order_id IS NOT NULL;

-- write_trade_outcome() in execution/engine.py already has an
-- `ON CONFLICT DO NOTHING` clause on this insert, evidently written on the
-- assumption that a unique constraint on position_id existed — it didn't,
-- so a resumed/retried close ticket could write a second nwt_trade_outcomes
-- row for the same closed position. This makes that existing ON CONFLICT
-- clause actually do what it already looked like it was meant to do.
CREATE UNIQUE INDEX IF NOT EXISTS one_outcome_per_position
  ON nwt_trade_outcomes (position_id)
  WHERE position_id IS NOT NULL;
