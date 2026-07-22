"""
nwt_agents/expiry_sweeper.py
Proactive option expiry protection. Runs daily, independent of risk_agent's
Rule 12 (which only force-closes DTE<=1 positions AFTER 15:45 ET hard close
same day) and independent of recon_agent (which only reacts AFTER a
position has already vanished from Alpaca). This is the third leg: catch
positions approaching expiry before either of those, and guarantee an
explicit signal — never silence — if a hard close was supposed to happen
and didn't.

Two jobs:
  1. Warn (T-1): any OPEN option position expiring tomorrow gets a WARNING
     ticket now, so a stuck position is visible a full day before it
     matters, not discovered after the fact via recon.
  2. Enforce (T-0 and overdue): any OPEN option position with DTE<=0 that
     does not already have a pending close/force-close ticket gets a
     FORCE_CLOSE scheduled immediately (via the same schedule_force_close_
     attempt state machine risk_agent's Rule 12 uses) and is transitioned
     to CLOSING. If Rule 12 already handled it same-day, this is a no-op
     (has_pending_force_close_ticket / already terminal in
     nwt_force_close_state will both say so). If this sweeper is the one
     that actually finds a DTE<=0 position, that itself means Rule 12
     missed it — logged as CRITICAL, since that should not happen and is
     worth investigating on its own.

Run: daily, before the conviction stack (e.g. 12:15 UTC) — see crontab.txt.
"""

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import (
    get_db,
    has_pending_force_close_ticket,
    insert_ticket,
    log_system_event,
    option_dte,
    release_advisory_lock,
    schedule_force_close_attempt,
    transition_position_state,
    try_advisory_lock,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("expiry_sweeper")

LOCK_NAME = "expiry_sweeper"


def fetch_open_options(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE status = 'open' AND asset_type = 'option'")
        return [dict(r) for r in cur.fetchall()]


def sweep(conn) -> None:
    positions = fetch_open_options(conn)
    warned = enforced = 0

    for pos in positions:
        position_id = str(pos["position_id"])
        asset = pos.get("asset", "")
        dte = option_dte(asset)
        if dte is None:
            continue  # unparseable symbol — Rule 12's own hard-close sweep fails closed on this instead

        if dte == 1:
            insert_ticket(conn, "EXPIRY_SWEEPER", "SYSTEM", "expiry_warning", {
                "position_id": position_id, "asset": asset, "dte": dte,
            })
            logger.warning("Expiry warning: %s (position_id=%s) expires tomorrow", asset, position_id)
            warned += 1
            continue

        if dte > 0:
            continue

        # DTE<=0: this should already have been force-closed by risk_agent's
        # Rule 12 same-day. If it wasn't, that's a real gap worth flagging —
        # not just silently doing Rule 12's job without comment.
        if has_pending_force_close_ticket(conn, position_id):
            continue  # already in flight, nothing to do

        if not schedule_force_close_attempt(conn, position_id, asset):
            continue  # terminal, cooling off, or already handled

        transition_position_state(conn, position_id, "CLOSING",
                                  f"expiry_sweeper: DTE={dte}, no pending close found — Rule 12 missed this",
                                  "expiry_sweeper")
        insert_ticket(
            conn, "EXPIRY_SWEEPER", "EXECUTION_ENGINE", "FORCE_CLOSE",
            {
                "approved": True, "bot_source": pos.get("bot_source", "EXPIRY_SWEEPER"),
                "symbol": asset, "option_symbol": asset, "direction": "close",
                "strategy_id": "EXPIRY_SWEEPER", "sized_notional": float(pos.get("notional_risk") or 0),
                "asset_type": "option", "time_in_force": "day", "exit_reason": "expiry_sweep",
                "position_id": position_id,
            },
        )
        log_system_event(conn, "CRITICAL", "expiry_sweeper",
                         f"Expiry sweeper force-closing {asset} (DTE={dte}) — Rule 12's same-day "
                         f"hard-close should have already caught this",
                         {"position_id": position_id, "asset": asset, "dte": dte})
        logger.critical("Expiry sweep ENFORCED: %s (position_id=%s) DTE=%d — Rule 12 gap", asset, position_id, dte)
        enforced += 1

    log_system_event(conn, "INFO", "expiry_sweeper",
                     f"Expiry sweep: {len(positions)} open options checked, {warned} warned, {enforced} enforced",
                     {"checked": len(positions), "warned": warned, "enforced": enforced})
    logger.info("Expiry sweep done: %d checked, %d warned, %d enforced", len(positions), warned, enforced)


def main() -> None:
    conn = get_db()
    try:
        if not try_advisory_lock(conn, LOCK_NAME):
            logger.warning("expiry_sweeper: another instance is already running — skipping this invocation")
            return
        try:
            sweep(conn)
        finally:
            release_advisory_lock(conn, LOCK_NAME)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
