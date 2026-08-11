-- Migration: minimal execution-order idempotency
-- August 2026. Idempotent — safe to re-run.
--
-- Why: execution/engine.py's "ticket found -> Alpaca order placed -> decision
-- recorded" pattern is not crash-safe. insert_position() happens BEFORE
-- insert_decision() — a crash, a lost response, or a second concurrent
-- worker between those two calls leaves the ticket looking untouched to
-- fetch_pending_tickets()/fetch_force_close_tickets(), which only check for
-- an existing decision. Proven live 2026-08-06 and again 2026-08-07: EU_BOT's
-- FEZ ticket produced two real, separately-filled Alpaca orders from what
-- was meant to be one signal — the second order has zero ticket_id and zero
-- decision anywhere in the system, i.e. exactly this failure mode, not a
-- hypothetical one.
--
-- Two constraints, both required by the new claim-before-execute code in
-- execution/engine.py:
--
--   1. Exactly one EXECUTION_ENGINE decision row per ticket, FROM
--      2026-07-24 ONWARD. This is a PARTIAL index, not a full one — cutoff
--      and justification reused as-is from the parallel reliability
--      branch's own verified analysis of this same production table
--      (db/migrate_2026_07_execution_idempotency.sql on
--      claude/clarification-needed-mf1iww): production already carries 16
--      tickets with two legitimate EXECUTION_ENGINE decision rows each
--      (an older engine version's SUBMITTED -> EXECUTED tracking pattern
--      the current code doesn't have — grep confirms no "SUBMITTED" writes
--      by decided_by='EXECUTION_ENGINE' anywhere in execution/engine.py).
--      Latest such legacy row confirmed at 2026-07-23 13:00:03 UTC; this
--      cutoff has margin on both sides. This is what makes the CLAIMED ->
--      finalized upsert in insert_decision() safe, and what makes a second,
--      concurrent claim attempt on the same ticket fail (ON CONFLICT DO
--      NOTHING) instead of silently creating a second row.
--
--   2. Exactly one ledger row per (alpaca_order_id, asset) pair. Defense in
--      depth: even if a bug in the claim logic somehow let two
--      insert_position() calls through for the same real fill, this makes
--      that a hard database-level impossibility rather than a silent
--      duplicate. A single mleg order legitimately produces multiple ledger
--      rows for a spread (one per leg/asset), so the constraint is on the
--      pair, not on alpaca_order_id alone. NULL alpaca_order_id (recon_agent
--      .py's cold-start UNATTRIBUTED imports, which never went through order
--      placement) is explicitly excluded.
--
-- execution/engine.py's insert_decision() and claim_ticket() ON CONFLICT
-- clauses carry the identical WHERE predicate as constraint 1 for Postgres
-- to use it as their conflict-inference target — see IDEMPOTENCY_CUTOFF.

CREATE UNIQUE INDEX IF NOT EXISTS one_decision_per_agent
  ON nwt_ticket_decisions (ticket_id, decided_by)
  WHERE created_at >= '2026-07-24T00:00:00+00:00';

CREATE UNIQUE INDEX IF NOT EXISTS one_ledger_row_per_order_asset
  ON nwt_portfolio_ledger (alpaca_order_id, asset)
  WHERE alpaca_order_id IS NOT NULL;

-- write_trade_outcome() in execution/engine.py already carries an
-- `ON CONFLICT DO NOTHING` on this insert, written on the assumption that a
-- unique constraint on position_id existed — it didn't, so a resumed/
-- retried close ticket could write a second nwt_trade_outcomes row for the
-- same closed position. This activates that existing clause.
CREATE UNIQUE INDEX IF NOT EXISTS one_outcome_per_position
  ON nwt_trade_outcomes (position_id)
  WHERE position_id IS NOT NULL;
