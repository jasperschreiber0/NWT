-- NWT In-Flight Order Tracking — 2026-07-16
-- Run as: psql "$NWT_DB_DSN" -f migrate_2026_07_inflight_orders.sql
-- Idempotent — safe to re-run.
--
-- Why: the Execution Engine placed an order, polled it for ~30 seconds, and
-- if it hadn't filled yet marked the ticket FAILED and moved on — while the
-- order stayed live at Alpaca. Any later fill created a position the ledger
-- never heard about. This is exactly the 2026-07-16 incident: AUS bot GTC
-- market orders (BHP/EWA/RIO) placed before US market open couldn't fill
-- inside the poll window, were written off as FAILED, then filled at the
-- 09:30 ET open — recon correctly flagged three in_alpaca_not_ledger
-- positions and halted the system. The same pattern on the close path
-- (close order still working, retry placed a second one) caused the
-- repeating 422 "insufficient qty" close failures.
--
-- nwt_inflight_orders records every order the engine has submitted that has
-- not yet reached a terminal state. resolve_inflight_orders() (engine, every
-- run — including under no_trade_mode) polls each row and either writes the
-- ledger/outcome rows on fill, or retires the row when the order dies.

BEGIN;

CREATE TABLE IF NOT EXISTS nwt_inflight_orders (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id        UUID REFERENCES nwt_tickets(ticket_id),
  alpaca_order_id  TEXT NOT NULL UNIQUE,
  kind             TEXT NOT NULL,          -- 'entry' | 'close'
  payload          JSONB NOT NULL,         -- full execution payload (entry) or close context
  position_id      UUID,                   -- close orders: the ledger position being closed
  exit_reason      TEXT,                   -- close orders: target | stop | hard_close | ...
  status           TEXT NOT NULL DEFAULT 'pending',  -- pending | resolved | dead
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at      TIMESTAMPTZ,
  resolution       TEXT                    -- filled | canceled | expired | rejected | cancel_requested | ...
);

CREATE INDEX IF NOT EXISTS idx_inflight_pending
  ON nwt_inflight_orders (created_at)
  WHERE status = 'pending';

COMMENT ON TABLE nwt_inflight_orders IS
  'Orders submitted to Alpaca that have not reached a terminal state. The '
  'engine resolves these every run; a pending row is a live order the system '
  'still owns. Never delete pending rows by hand — cancel the Alpaca order '
  'first, then the resolver retires the row itself.';

COMMIT;

-- Post-migration checklist:
-- 1. \d nwt_inflight_orders
-- 2. SELECT * FROM nwt_inflight_orders WHERE status='pending';  -- should be empty on first run
