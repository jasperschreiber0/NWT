"""
nwt_agents/attribution_audit.py
Read-only reconciliation report over nwt_portfolio_ledger provenance.

Does not trade, does not modify positions, does not touch no_trade_mode.
Its only side effect is writing an nwt_tickets row (type='attribution_audit')
and, on findings, an nwt_system_log WARNING — same append-only pattern every
other read-only check in this system uses (recon_agent.py --nightly, morning
triage). Safe to run on any schedule or ad hoc.

Why this exists: recon_agent.py and the execution engine only ever write
bot_source='UNATTRIBUTED' (cold-start import) or a real bot identifier.
There is no DB constraint enforcing that, so any row whose bot_source is
neither a known bot nor 'UNATTRIBUTED' got there through a path this codebase
does not contain -- almost certainly a direct manual SQL write (e.g. a
human patching a reconciliation issue by hand). That is not necessarily
wrong (CLAUDE.md explicitly allows a human to clear no_trade_mode after
manual acknowledgement), but it is currently invisible: nothing distinguishes
"recon_agent verified this" from "someone hand-edited the ledger and left a
note that looks like recon_agent wrote it". This script makes that
distinction visible without changing how anything trades.

Checks:
  1. Ledger rows with a bot_source outside the known allowlist (real bots +
     'UNATTRIBUTED') -- e.g. the 'RECON_RECOVERED' row found in production,
     which no code path in this repo ever inserts.
  2. Open UNATTRIBUTED positions older than ATTRIBUTION_STALE_DAYS -- these
     are silently excluded from run_equity_position_monitor() (see
     execution/engine.py) and so receive no automated stop/target/hard-close
     management; a human needs to either attribute them to a bot or close
     them manually.
  3. recon_mismatch tickets whose payload contains a mismatch "class" that
     run_recon() in recon_agent.py does not itself produce -- i.e. a ticket
     shaped like recon output but not written by recon_agent.py.
  4. no_trade_mode clear events (nwt_system_flags.set_by) that don't match
     the fixed set of values the real code paths use -- same "looks
     automated but wasn't" signal as (3), applied to the safety flag itself.
"""

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import get_db, insert_ticket, log_system_event  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("attribution_audit")

# Real bot identifiers this codebase actually assigns as bot_source, plus the
# one sentinel recon_agent.py's cold_start_import() writes. Extend this list
# in lockstep with any new bot -- it is intentionally not derived from a
# live query, so a code review catches new bot_source values the same way
# it would catch a new table.
KNOWN_BOT_SOURCES = {
    "US_BOT", "EU_BOT", "AUS_BOT", "CHINA_BOT",
    "NWT_TRACK_C", "NWT_TRACK_D", "NWT_TRACK_E",
    "UNATTRIBUTED",
}

# The only mismatch classes run_recon() in recon_agent.py actually emits.
KNOWN_RECON_MISMATCH_CLASSES = {
    "in_alpaca_not_ledger", "side_mismatch", "in_ledger_not_alpaca", "qty_mismatch",
}

# The only set_by values the real no_trade_mode-clearing code path
# (recon_agent.py --clear-if-clean) ever writes.
KNOWN_CLEAR_SOURCES = {"recon_agent_manual_clear"}

ATTRIBUTION_STALE_DAYS = 3


def check_unknown_bot_sources(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT position_id, bot_source, asset, status, entry_time, alpaca_order_id
            FROM nwt_portfolio_ledger
            WHERE bot_source IS NOT NULL
            ORDER BY entry_time DESC
            """
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows if r["bot_source"] not in KNOWN_BOT_SOURCES]


def check_stale_unattributed(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT position_id, asset, qty, entry_time, alpaca_order_id
            FROM nwt_portfolio_ledger
            WHERE bot_source = 'UNATTRIBUTED'
              AND status = 'open'
              AND entry_time < NOW() - INTERVAL '%s days'
            ORDER BY entry_time
            """,
            (ATTRIBUTION_STALE_DAYS,),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def check_unrecognized_recon_tickets(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT ticket_id, from_agent, payload, created_at
            FROM nwt_tickets
            WHERE type = 'recon_mismatch'
            ORDER BY created_at DESC
            LIMIT 100
            """
        )
        rows = cur.fetchall()
    flagged = []
    for r in rows:
        payload = r["payload"] or {}
        classes = {m.get("class") for m in payload.get("mismatches", [])}
        unknown = classes - KNOWN_RECON_MISMATCH_CLASSES
        if unknown or r["from_agent"] != "RECON_AGENT":
            flagged.append({**dict(r), "unknown_classes": sorted(unknown)})
    return flagged


def check_unrecognized_clears(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT flag, value, reason, set_by, updated_at
            FROM nwt_system_flags
            WHERE flag = 'no_trade_mode'
            """
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows
            if r["value"] is False and r["set_by"] not in KNOWN_CLEAR_SOURCES and r["set_by"]]


def run_audit(conn) -> bool:
    """Returns True if clean (no findings)."""
    unknown_sources = check_unknown_bot_sources(conn)
    stale = check_stale_unattributed(conn)
    unrecognized_tickets = check_unrecognized_recon_tickets(conn)
    unrecognized_clears = check_unrecognized_clears(conn)

    findings = {
        "unknown_bot_sources": unknown_sources,
        "stale_unattributed_positions": stale,
        "unrecognized_recon_tickets": unrecognized_tickets,
        "unrecognized_no_trade_mode_clears": unrecognized_clears,
    }
    total = sum(len(v) for v in findings.values())

    insert_ticket(conn, "ATTRIBUTION_AUDIT", "SYSTEM", "attribution_audit", {
        "total_findings": total,
        **findings,
    })

    if total == 0:
        logger.info("Attribution audit CLEAN — no unknown bot_source, no stale UNATTRIBUTED "
                    "positions, no unrecognized recon/clear events")
        return True

    logger.warning("Attribution audit found %d issue(s): %d unknown bot_source, "
                    "%d stale UNATTRIBUTED, %d unrecognized recon tickets, "
                    "%d unrecognized no_trade_mode clears",
                    total, len(unknown_sources), len(stale),
                    len(unrecognized_tickets), len(unrecognized_clears))
    log_system_event(conn, "WARNING", "attribution_audit",
                      f"{total} provenance issue(s) found", findings)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="NWT ledger attribution/provenance audit (read-only)")
    parser.add_argument("--gate", action="store_true",
                         help="Exit 1 if any finding, for wiring into a monitoring check "
                              "(never blocks trading -- this script never touches no_trade_mode)")
    args = parser.parse_args()

    conn = get_db()
    try:
        clean = run_audit(conn)
    finally:
        conn.close()

    if args.gate and not clean:
        sys.exit(1)


if __name__ == "__main__":
    main()
