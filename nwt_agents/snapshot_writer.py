"""
nwt_agents/snapshot_writer.py
Fires every 15 minutes 13:00-21:00 UTC.
Writes current system state to NWT_AGENTS_DIR/snapshot.json.
"""

import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import get_db, load_master_directives

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("snapshot_writer")

AGENTS_DIR = Path(os.environ.get("NWT_AGENTS_DIR", Path(__file__).parent))


def fetch_open_positions(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT bot_source, asset, asset_type, direction, notional_risk, entry_time
            FROM nwt_portfolio_ledger
            WHERE status = 'open'
            ORDER BY entry_time DESC
            """
        )
        rows = cur.fetchall()
    result = []
    for row in rows:
        r = dict(row)
        if r.get("entry_time"):
            r["entry_time"] = r["entry_time"].isoformat()
        if r.get("notional_risk"):
            r["notional_risk"] = float(r["notional_risk"])
        result.append(r)
    return result


def fetch_today_ticket_counts(conn) -> dict:
    """Count proposals, approved, vetoed, executed today."""
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)

    counts = {
        "pending_proposals": 0,
        "approved_awaiting_execution": 0,
        "vetoed_today": 0,
        "executed_today": 0,
    }

    with conn.cursor() as cur:
        # Pending proposals: to_agent=RISK_AGENT, no decision yet
        cur.execute(
            """
            SELECT COUNT(*) FROM nwt_tickets t
            WHERE t.to_agent = 'RISK_AGENT'
              AND t.type = 'TRADE_PROPOSAL'
              AND NOT EXISTS (
                  SELECT 1 FROM nwt_ticket_decisions d WHERE d.ticket_id = t.ticket_id
              )
              AND t.created_at >= %s
            """,
            (today_start,),
        )
        counts["pending_proposals"] = cur.fetchone()[0]

        # Approved awaiting execution: RISK_AGENT approved, NWT_EXECUTION_AGENT not yet submitted
        cur.execute(
            """
            SELECT COUNT(DISTINCT t.ticket_id) FROM nwt_tickets t
            INNER JOIN nwt_ticket_decisions d ON d.ticket_id = t.ticket_id
            WHERE d.decision = 'APPROVED' AND d.decided_by = 'RISK_AGENT'
              AND NOT EXISTS (
                  SELECT 1 FROM nwt_ticket_decisions d2
                  WHERE d2.ticket_id = t.ticket_id AND d2.decided_by = 'NWT_EXECUTION_AGENT'
              )
              AND t.created_at >= %s
            """,
            (today_start,),
        )
        counts["approved_awaiting_execution"] = cur.fetchone()[0]

        # Vetoed today
        cur.execute(
            """
            SELECT COUNT(*) FROM nwt_ticket_decisions
            WHERE decision = 'VETOED'
              AND decided_by = 'RISK_AGENT'
              AND created_at >= %s
            """,
            (today_start,),
        )
        counts["vetoed_today"] = cur.fetchone()[0]

        # Executed today
        cur.execute(
            """
            SELECT COUNT(*) FROM nwt_ticket_decisions
            WHERE decision = 'EXECUTED'
              AND decided_by = 'EXECUTION_ENGINE'
              AND created_at >= %s
            """,
            (today_start,),
        )
        counts["executed_today"] = cur.fetchone()[0]

    return counts


def fetch_risk_agent_last_run(conn) -> str:
    """Get the timestamp of the last risk_agent system log entry."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT created_at FROM nwt_system_log
            WHERE component = 'risk_agent'
            ORDER BY created_at DESC LIMIT 1
            """
        )
        row = cur.fetchone()
    if row and row[0]:
        return row[0].isoformat()
    return None


def main() -> None:
    conn = get_db()

    try:
        try:
            directives = load_master_directives()
            regime = directives.get("regime", {})
            global_kill_switch = directives.get("global_kill_switch", False)
        except FileNotFoundError:
            regime = {}
            global_kill_switch = True  # Safe default if directives missing

        open_positions = fetch_open_positions(conn)
        ticket_counts = fetch_today_ticket_counts(conn)
        risk_agent_last_run = fetch_risk_agent_last_run(conn)

        snapshot = {
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "open_positions": len(open_positions),
            "pending_proposals": ticket_counts["pending_proposals"],
            "approved_awaiting_execution": ticket_counts["approved_awaiting_execution"],
            "vetoed_today": ticket_counts["vetoed_today"],
            "executed_today": ticket_counts["executed_today"],
            "regime": regime,
            "global_kill_switch": global_kill_switch,
            "risk_agent_last_run": risk_agent_last_run,
            "positions": open_positions,
        }

        out_path = AGENTS_DIR / "snapshot.json"
        with open(out_path, "w") as f:
            json.dump(snapshot, f, indent=2)

        logger.info(
            "Snapshot written: %d open positions, %d executed today, %d vetoed today, kill_switch=%s",
            len(open_positions),
            ticket_counts["executed_today"],
            ticket_counts["vetoed_today"],
            global_kill_switch,
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
