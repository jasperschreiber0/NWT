"""
nwt_agents/session_scorecard.py
Runs at 21:15 UTC (after the learning agent) on trading days.

Writes ONE green/red row per session to nwt_session_scorecard. The 60-day
dataset clock only counts consecutive green sessions — a session is green
when every pipeline stage left evidence in Postgres, with zero manual touch.

All checks are outcome-based ("did tickets/logs/decisions appear today?"),
never freshness-based ("is the process running?"). Inactivity counts as
activity: a day where every track logged NO_EDGE is a green day.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from shared_context import get_db, load_master_directives, log_system_event

AGENTS_DIR = Path(os.environ.get("NWT_AGENTS_DIR", Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("session_scorecard")

STALE_MINUTES = 30  # a proposal/request unprocessed for longer than this = stuck pipeline


def _count(conn, sql: str, params=None) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()[0]


def check_integrity_gate(conn, today) -> tuple:
    passes = _count(
        conn,
        """
        SELECT COUNT(*) FROM nwt_system_log
        WHERE component = 'integrity_gate' AND level = 'INFO'
          AND (created_at AT TIME ZONE 'UTC')::date = %s
        """,
        (today,),
    )
    failures = _count(
        conn,
        """
        SELECT COUNT(*) FROM nwt_system_log
        WHERE component = 'integrity_gate' AND level = 'CRITICAL'
          AND (created_at AT TIME ZONE 'UTC')::date = %s
        """,
        (today,),
    )
    return passes > 0 and failures == 0, {"gate_passes": passes, "gate_failures": failures}


def check_directives_fresh(today) -> tuple:
    try:
        directives = load_master_directives()
    except FileNotFoundError:
        return False, {"directives_date": None}
    d = directives.get("date")
    return d == today.isoformat(), {"directives_date": d}


def check_conviction_ran(conn, today) -> tuple:
    n = _count(
        conn,
        """
        SELECT COUNT(*) FROM nwt_system_log
        WHERE component = 'conviction_engine'
          AND (created_at AT TIME ZONE 'UTC')::date = %s
        """,
        (today,),
    )
    return n > 0, {"conviction_log_entries": n}


def check_tracks_ran(conn, today) -> tuple:
    detail = {}
    ok = True
    # Track E is in shadow mode — its evidence is inactivity rows, counted below
    for component in ("track_c", "track_d"):
        n = _count(
            conn,
            """
            SELECT COUNT(*) FROM nwt_system_log
            WHERE component = %s AND (created_at AT TIME ZONE 'UTC')::date = %s
            """,
            (component, today),
        )
        detail[component] = n
        ok = ok and n > 0
    return ok, detail


def check_activity_logged(conn, today) -> tuple:
    """Proposals OR inactivity rows. 'No edge present' is a valid green outcome."""
    proposals = _count(
        conn,
        """
        SELECT COUNT(*) FROM nwt_tickets
        WHERE type = 'TRADE_PROPOSAL'
          AND (created_at AT TIME ZONE 'UTC')::date = %s
        """,
        (today,),
    )
    inactivity = _count(
        conn,
        """
        SELECT COUNT(*) FROM nwt_inactivity_log
        WHERE (logged_at AT TIME ZONE 'UTC')::date = %s
        """,
        (today,),
    )
    return (proposals + inactivity) > 0, {"proposals": proposals, "inactivity_rows": inactivity}


def check_risk_agent_clear(conn, today) -> tuple:
    ran = _count(
        conn,
        """
        SELECT COUNT(*) FROM nwt_system_log
        WHERE component = 'risk_agent'
          AND (created_at AT TIME ZONE 'UTC')::date = %s
        """,
        (today,),
    )
    stale = _count(
        conn,
        f"""
        SELECT COUNT(*) FROM nwt_tickets t
        WHERE t.to_agent = 'RISK_AGENT' AND t.type = 'TRADE_PROPOSAL'
          AND (t.created_at AT TIME ZONE 'UTC')::date = %s
          AND t.created_at < NOW() - INTERVAL '{STALE_MINUTES} minutes'
          AND NOT EXISTS (
              SELECT 1 FROM nwt_ticket_decisions d WHERE d.ticket_id = t.ticket_id
          )
        """,
        (today,),
    )
    return ran > 0 and stale == 0, {"risk_log_entries": ran, "stale_proposals": stale}


def check_execution_clear(conn, today) -> tuple:
    """No TRADE_REQUEST/FORCE_CLOSE left unprocessed — a quiet day with zero requests is green."""
    stale = _count(
        conn,
        f"""
        SELECT COUNT(*) FROM nwt_tickets t
        WHERE t.to_agent = 'EXECUTION_ENGINE'
          AND t.type IN ('TRADE_REQUEST', 'FORCE_CLOSE')
          AND (t.created_at AT TIME ZONE 'UTC')::date = %s
          AND t.created_at < NOW() - INTERVAL '{STALE_MINUTES} minutes'
          AND NOT EXISTS (
              SELECT 1 FROM nwt_ticket_decisions d
              WHERE d.ticket_id = t.ticket_id AND d.decided_by = 'EXECUTION_ENGINE'
          )
        """,
        (today,),
    )
    return stale == 0, {"stale_requests": stale}


def check_learning_agent_ran(conn, today) -> tuple:
    n = _count(
        conn,
        """
        SELECT COUNT(*) FROM nwt_system_log
        WHERE component = 'learning_agent'
          AND (created_at AT TIME ZONE 'UTC')::date = %s
        """,
        (today,),
    )
    return n > 0, {"learning_log_entries": n}


def main() -> None:
    conn = get_db()
    today = datetime.now(timezone.utc).date()

    try:
        checks = {}
        details = {}

        checks["integrity_gate_passed"], details["integrity_gate"] = check_integrity_gate(conn, today)
        checks["directives_fresh"], details["directives"] = check_directives_fresh(today)
        checks["conviction_ran"], details["conviction"] = check_conviction_ran(conn, today)
        checks["tracks_ran"], details["tracks"] = check_tracks_ran(conn, today)
        checks["activity_logged"], details["activity"] = check_activity_logged(conn, today)
        checks["risk_agent_clear"], details["risk_agent"] = check_risk_agent_clear(conn, today)
        checks["execution_clear"], details["execution"] = check_execution_clear(conn, today)
        checks["learning_agent_ran"], details["learning_agent"] = check_learning_agent_ran(conn, today)

        green = all(checks.values())

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO nwt_session_scorecard
                    (session_date, integrity_gate_passed, directives_fresh,
                     conviction_ran, tracks_ran, activity_logged,
                     risk_agent_clear, execution_clear, learning_agent_ran,
                     green, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_date) DO UPDATE SET
                    integrity_gate_passed = EXCLUDED.integrity_gate_passed,
                    directives_fresh = EXCLUDED.directives_fresh,
                    conviction_ran = EXCLUDED.conviction_ran,
                    tracks_ran = EXCLUDED.tracks_ran,
                    activity_logged = EXCLUDED.activity_logged,
                    risk_agent_clear = EXCLUDED.risk_agent_clear,
                    execution_clear = EXCLUDED.execution_clear,
                    learning_agent_ran = EXCLUDED.learning_agent_ran,
                    green = EXCLUDED.green,
                    details = EXCLUDED.details,
                    computed_at = NOW()
                """,
                (
                    today,
                    checks["integrity_gate_passed"],
                    checks["directives_fresh"],
                    checks["conviction_ran"],
                    checks["tracks_ran"],
                    checks["activity_logged"],
                    checks["risk_agent_clear"],
                    checks["execution_clear"],
                    checks["learning_agent_ran"],
                    green,
                    json.dumps(details),
                ),
            )
        conn.commit()

        failed = [k for k, v in checks.items() if not v]
        log_system_event(
            conn,
            "INFO" if green else "WARNING",
            "session_scorecard",
            f"Session {today}: {'GREEN' if green else 'RED — failed: ' + ', '.join(failed)}",
            {"green": green, "checks": checks},
        )
        logger.info("Session %s scored %s%s", today, "GREEN" if green else "RED",
                    "" if green else f" — failed: {failed}")

        # Send combined daily digest now that scorecard is ready.
        # cost_summary.json is written by cost_agent.py at 21:00 UTC (15 min ago).
        try:
            from notifier import send_daily_digest_with_scorecard
            cost_path = AGENTS_DIR / "cost_summary.json"
            with open(cost_path) as f:
                cost_data = json.load(f)
            t = cost_data.get("today", {})
            send_daily_digest_with_scorecard(
                trades_today=t.get("trades_closed", 0),
                pnl_today=float(t.get("pnl_today", 0)),
                cost_today=float(t.get("estimated_cost_usd", {}).get("total_cost_usd", 0)),
                cost_per_trade=t.get("cost_per_trade_usd"),
                inactivity_today=t.get("inactivity_tickets", 0),
                approved_today=t.get("risk_approved", 0),
                vetoed_today=t.get("risk_vetoed", 0),
                no_trade_mode=cost_data.get("no_trade_mode", False),
                open_positions=cost_data.get("open_positions", 0),
                session_green=green,
                failed_checks=failed,
            )
        except Exception as exc:
            logger.warning("Daily digest send failed (non-fatal): %s", exc)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
