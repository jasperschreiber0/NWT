-- Migration: P0-1 execution idempotency (claim-then-finalize + duplicate-order protection)
-- July 2026. Idempotent — safe to re-run.
--
-- Why: execution/engine.py's "ticket found -> Alpaca order placed -> decision
-- recorded" pattern was not crash-safe. A process kill (or an unhandled
-- exception in an unwrapped code path) between placing a live order and
-- recording nwt_ticket_decisions left the ticket looking untouched, so the
-- next 5-minute cron cycle retried it and placed a SECOND live order for the
-- same signal. This migration adds the two constraints the new
-- claim-before-execute code in execution/engine.py depends on:
--
--   1. Exactly one EXECUTION_ENGINE decision row can ever exist per ticket.
--      This is what makes the CLAIMED -> finalized upsert in
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

CREATE UNIQUE INDEX IF NOT EXISTS one_decision_per_agent
  ON nwt_ticket_decisions (ticket_id, decided_by);

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
