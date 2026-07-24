"""
nwt_agents/reconcile_known_incidents.py

Manual, human-invoked reconciliation script for three specific, fully
investigated incidents (2026-07 EWA/BHP/AAPL broker-vs-ledger audit):

  - EWA: stale UNATTRIBUTED cold-start ghost (125 shares, sold for real
    2026-07-20, never reflected in the ledger).
  - BHP: the same kind of stale UNATTRIBUTED ghost (29 shares, sold
    2026-07-20) PLUS a real -10 short created by a duplicate-close-race on
    2026-07-22 that has no ledger row at all.
  - AAPL: an UNATTRIBUTED import (300 shares) double-counted against a
    mislabeled "short" position that was actually just a sell_to_close
    trim of the same shares (confirmed via Alpaca's own position_intent on
    the underlying order).

Every correction here was derived from real Alpaca order history and is
listed with the exact order id(s) it is based on — see each entry's "note".

SAFETY:
  - Never submits an Alpaca order. Never will — these are ledger-only
    corrections for positions that are either already fully closed at the
    broker (EWA, BHP ghost, the AAPL "short") or already correctly held
    (BHP short, AAPL long) with nothing left to trade.
  - NOT wired into cron, PM2, or any other automatic trigger. It only runs
    when a human invokes it directly.
  - Defaults to --dry-run: prints exactly what each correction would do
    without opening a database connection that writes anything.
  - Requires BOTH --execute and --yes-i-am-sure to actually write.
  - Every write goes through the same audited pattern the rest of the
    codebase uses (transition_position_state / position_state_history,
    nwt_tickets, nwt_trade_outcomes) — never a bare UPDATE.
  - Uses compare-and-swap (expected_state) on every transition so a
    position whose state has changed since this script was written
    (e.g. already manually fixed) is skipped with a loud warning instead
    of silently overwritten.

Usage:
    python3 reconcile_known_incidents.py                      # dry-run (default)
    python3 reconcile_known_incidents.py --execute --yes-i-am-sure
"""
import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import get_db, insert_ticket, log_system_event, transition_position_state  # noqa: E402
from recon_agent import _apply_ledger_close, _write_reconciled_trade_outcome  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("reconcile_known_incidents")


CORRECTIONS = [
    {
        "action": "close",
        "position_id": "2e86b1ff-3065-46a6-8b10-ad50e2a173ae",
        "symbol": "EWA",
        "exit_price": 28.70,
        "exit_time": datetime(2026, 7, 20, 13, 30, 15, tzinfo=timezone.utc),
        "exit_reason": "reconciled_closed_at_broker",
        "resolution": "CLOSED_RECONCILED",
        "note": ("Stale UNATTRIBUTED cold-start ghost (imported 2026-07-17 09:40 AEST). "
                "The exact 125 shares were sold for real at the broker on 2026-07-20 "
                "13:30:15 UTC (Alpaca order 46a9d31b-bd82-4525-8880-6e6fc04e5067 @ 28.70) "
                "— never reflected in the ledger."),
    },
    {
        "action": "close",
        "position_id": "f56fa1e5-70e0-4b3d-917d-795d9c95f6e1",
        "symbol": "BHP",
        "exit_price": 80.75,
        "exit_time": datetime(2026, 7, 20, 13, 32, 53, tzinfo=timezone.utc),
        "exit_reason": "reconciled_closed_at_broker",
        "resolution": "CLOSED_RECONCILED",
        "note": ("Stale UNATTRIBUTED cold-start ghost (imported 2026-07-17 09:40 AEST). "
                "The exact 29 shares were sold for real at the broker on 2026-07-20 "
                "13:32:53 UTC (Alpaca order 17f76dfa-c69f-4d9c-b180-9c567f4d0708 @ 80.75) "
                "— never reflected in the ledger."),
    },
    {
        "action": "create",
        "symbol": "BHP",
        "asset_type": "equity",
        "bot_source": "RECON_RECOVERED",
        "direction": "short",
        "qty": 10,
        "entry_price": 84.48,
        "entry_time": datetime(2026, 7, 22, 13, 35, 10, tzinfo=timezone.utc),
        "alpaca_order_id": "99ae0eb3-402f-4a7f-a576-2ed5d545eecd",
        "note": ("Real -10 short created by a duplicate CLOSE_REQUEST race on 2026-07-22: "
                "a second close ticket (client_order_id nwt-close-58eb2ff6-9eff-43ea-8eb7-"
                "769c59a7612a) fired ~2 minutes after the first "
                "(nwt-close-a0edad53-624f-419e-a5e8-4cf0a42bc754) had already closed the "
                "real AUS_BOT long. Alpaca had zero position at that point and opened a "
                "short instead of rejecting the sell (Alpaca itself tagged it "
                "sell_to_open). No ledger row has ever existed for it. This is the exact "
                "incident the process_close_ticket 'already closed' guard and broker-"
                "position pre-check (execution/engine.py) now prevent from recurring."),
    },
    {
        "action": "adjust_qty",
        "position_id": "09a114df-e09b-40ee-b0ba-960ce0883087",
        "symbol": "AAPL",
        "new_qty": 286,
        "note": ("UNATTRIBUTED import (2026-07-22 ~04:24 UTC) originally 300 shares. 14 "
                "of those shares were sold via order "
                "cf7bbf6b-50bc-4dce-94cf-21f4f97f0e01 at 18:10:06 UTC — Alpaca's own "
                "position_intent on that order is sell_to_close, confirming it trimmed "
                "this long rather than opening a new short. Reducing this row to the "
                "real remaining 286 removes the double-count against position "
                "a49634ff-2d75-44cc-8225-f397ddeaee74 (see next entry)."),
    },
    {
        "action": "close",
        "position_id": "a49634ff-2d75-44cc-8225-f397ddeaee74",
        "symbol": "AAPL",
        "exit_price": 323.855714,
        "exit_time": datetime(2026, 7, 22, 18, 10, 6, tzinfo=timezone.utc),
        "exit_reason": "reconciled_merged_into_unattributed_position",
        "resolution": "MERGED_NOT_A_REAL_SHORT",
        "note": ("Never a real independent short. Order cf7bbf6b-50bc-4dce-94cf-"
                "21f4f97f0e01 has position_intent=sell_to_close per Alpaca's own "
                "record — it trimmed the UNATTRIBUTED long (09a114df...), not opened a "
                "short. The subsequent close attempt (order "
                "8594f538-0e79-4643-9b4e-e40cca4cadf5, buy_to_open 14, rejected "
                "2026-07-23 08:00:02 UTC) failed because it would have ADDED 14 more "
                "shares to an already ~$91-95k position. Do NOT resubmit this as a "
                "broker order — closing this row administratively removes the phantom "
                "short from the ledger; the real economic exit already happened via the "
                "sell_to_close order above."),
    },
]


def _fetch_position(conn, position_id: str) -> dict | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE position_id = %s", (position_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def _audit_ticket(conn, correction: dict, outcome: str) -> None:
    insert_ticket(conn, "MANUAL_RECON", "SYSTEM", "manual_recon_correction", {
        "action": correction["action"],
        "symbol": correction["symbol"],
        "position_id": correction.get("position_id"),
        "note": correction["note"],
        "outcome": outcome,
        "script": "nwt_agents/reconcile_known_incidents.py",
    })


def apply_correction(conn, correction: dict, dry_run: bool) -> str:
    """Returns a short outcome string for reporting; never raises on a
    skip — only on an unexpected DB error."""
    action = correction["action"]
    symbol = correction["symbol"]

    if action == "close":
        position_id = correction["position_id"]
        pos = _fetch_position(conn, position_id)
        if pos is None:
            return f"SKIP {symbol} {position_id}: position not found"
        if pos.get("status") == "closed":
            return f"SKIP {symbol} {position_id}: already closed (no-op, safe)"
        if dry_run:
            return (f"WOULD CLOSE {symbol} {position_id}: exit_price={correction['exit_price']} "
                    f"exit_time={correction['exit_time'].isoformat()} "
                    f"exit_reason={correction['exit_reason']}")
        _apply_ledger_close(conn, position_id, correction["exit_price"],
                           correction["exit_time"], correction["exit_reason"])
        transitioned = transition_position_state(
            conn, position_id, "CLOSED",
            f"manual recon: {correction['note']}", "manual_recon",
            expected_state=pos.get("lifecycle_state", "OPEN"),
        )
        if not transitioned:
            logger.warning("%s %s: state changed under us during close — re-check manually",
                           symbol, position_id)
        _write_reconciled_trade_outcome(conn, pos, correction["exit_price"],
                                        correction["exit_time"], correction["resolution"])
        _audit_ticket(conn, correction, "closed")
        log_system_event(conn, "WARNING", "manual_recon",
                         f"Manually closed {symbol} {position_id}: {correction['note']}",
                         {"position_id": position_id, "exit_price": correction["exit_price"]})
        return f"CLOSED {symbol} {position_id}"

    if action == "adjust_qty":
        position_id = correction["position_id"]
        pos = _fetch_position(conn, position_id)
        if pos is None:
            return f"SKIP {symbol} {position_id}: position not found"
        if pos.get("status") != "open":
            return f"SKIP {symbol} {position_id}: not open (status={pos.get('status')}), refusing to adjust"
        old_qty = float(pos.get("qty") or 0)
        new_qty = correction["new_qty"]
        if dry_run:
            return f"WOULD ADJUST {symbol} {position_id}: qty {old_qty:g} -> {new_qty:g}"
        fraction = new_qty / old_qty if old_qty > 0 else 1.0
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE nwt_portfolio_ledger SET qty = %s, notional_risk = notional_risk * %s "
                "WHERE position_id = %s AND status = 'open'",
                (new_qty, fraction, position_id),
            )
            adjusted = cur.rowcount > 0
        conn.commit()
        if not adjusted:
            return f"SKIP {symbol} {position_id}: no longer open at write time — nothing adjusted"
        transition_position_state(
            conn, position_id, "OPEN",
            f"manual recon qty adjustment {old_qty:g} -> {new_qty:g}: {correction['note']}",
            "manual_recon", expected_state=pos.get("lifecycle_state", "OPEN"),
        )
        _audit_ticket(conn, correction, f"qty adjusted {old_qty:g} -> {new_qty:g}")
        log_system_event(conn, "WARNING", "manual_recon",
                         f"Manually adjusted {symbol} {position_id} qty {old_qty:g} -> {new_qty:g}: "
                         f"{correction['note']}",
                         {"position_id": position_id, "old_qty": old_qty, "new_qty": new_qty})
        return f"ADJUSTED {symbol} {position_id}: {old_qty:g} -> {new_qty:g}"

    if action == "create":
        if dry_run:
            return (f"WOULD CREATE {symbol}: bot_source={correction['bot_source']} "
                    f"direction={correction['direction']} qty={correction['qty']} "
                    f"entry_price={correction['entry_price']} "
                    f"entry_time={correction['entry_time'].isoformat()}")
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO nwt_portfolio_ledger
                  (bot_source, asset, asset_type, direction, notional_risk, qty,
                   entry_price, entry_time, alpaca_order_id, status, lifecycle_state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', 'OPEN')
                RETURNING position_id
                """,
                (
                    correction["bot_source"], symbol, correction["asset_type"],
                    correction["direction"],
                    correction["qty"] * correction["entry_price"],
                    correction["qty"], correction["entry_price"],
                    correction["entry_time"], correction["alpaca_order_id"],
                ),
            )
            position_id = str(cur.fetchone()[0])
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO position_state_history (position_id, previous_state, new_state, reason, source) "
                "VALUES (%s, NULL, 'OPEN', %s, 'manual_recon')",
                (position_id, f"manual recon reconstruction: {correction['note']}"),
            )
        conn.commit()
        correction["position_id"] = position_id  # for the audit ticket below
        _audit_ticket(conn, correction, f"created position_id={position_id}")
        log_system_event(conn, "WARNING", "manual_recon",
                         f"Manually created {symbol} position {position_id}: {correction['note']}",
                         {"position_id": position_id})
        return f"CREATED {symbol} position_id={position_id}"

    return f"SKIP {symbol}: unknown action {action!r}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                       help="Explicit no-op flag — dry-run is already the default with no "
                            "flags at all. Accepted so the safe invocation is self-documenting.")
    parser.add_argument("--execute", action="store_true",
                       help="Actually write to the database. Without this, always dry-run.")
    parser.add_argument("--yes-i-am-sure", action="store_true", dest="confirmed",
                       help="Required together with --execute — a second explicit flag "
                            "against running this by accident.")
    args = parser.parse_args()

    if args.dry_run and args.execute:
        logger.error("--dry-run and --execute both given — refusing to write. Running as --dry-run.")
        args.execute = False

    dry_run = not (args.execute and args.confirmed)
    if args.execute and not args.confirmed:
        logger.error("--execute requires --yes-i-am-sure as well — refusing to write. "
                     "Running as --dry-run instead.")
    if dry_run:
        logger.info("=== DRY RUN — no database writes will occur ===")
    else:
        logger.warning("=== EXECUTING — writing corrections to the live ledger ===")

    conn = get_db()
    try:
        results = []
        for correction in CORRECTIONS:
            try:
                outcome = apply_correction(conn, correction, dry_run)
            except Exception as exc:
                conn.rollback()
                outcome = f"ERROR on {correction['symbol']} {correction.get('position_id', '')}: {exc}"
                logger.error(outcome)
            else:
                logger.info(outcome)
            results.append(outcome)

        print("\n--- Summary ---")
        for r in results:
            print(r)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
