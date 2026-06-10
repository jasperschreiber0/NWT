"""
dashboard/app.py
NWT read-only monitoring dashboard.
Reads from Postgres (nwt_agents DB) + master-directives.json.
Access at http://<server>:8080
"""

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

DIRECTIVES_PATH = Path(
    os.environ.get(
        "NWT_DIRECTIVES_PATH",
        "/home/northworld/trading/shared/master-directives.json",
    )
)
DB_DSN = os.environ["NWT_DB_DSN"]

app = FastAPI(docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_db():
    return psycopg2.connect(DB_DSN)


def load_directives() -> dict:
    try:
        return json.loads(DIRECTIVES_PATH.read_text())
    except Exception:
        return {}


def _q(conn, sql, params=None):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return [dict(r) for r in cur.fetchall()]


def _q1(conn, sql, params=None):
    rows = _q(conn, sql, params)
    return rows[0] if rows else {}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    conn = get_db()
    try:
        summary = _q1(
            conn,
            """
            SELECT
                COUNT(DISTINCT t.ticket_id) FILTER (WHERE t.type IN (
                    'CONVICTION_TICKET','PRESCREENED_TICKET','CONVICTION_RESULT'
                ))                                                                   AS conviction_count,
                COUNT(DISTINCT t.ticket_id) FILTER (WHERE t.type = 'TRADE_PROPOSAL') AS proposal_count,
                COUNT(DISTINCT t.ticket_id) FILTER (WHERE t.type = 'TRADE_REQUEST')  AS trade_request_count,
                COUNT(*) FILTER (WHERE d.decision = 'APPROVED'
                    AND d.decided_by = 'RISK_AGENT')                                AS risk_approved,
                COUNT(*) FILTER (WHERE d.decision = 'VETOED')                      AS vetoed,
                COUNT(*) FILTER (WHERE d.decision = 'SUBMITTED')                   AS submitted,
                COUNT(*) FILTER (WHERE d.decision = 'FAILED')                      AS failed
            FROM nwt_tickets t
            LEFT JOIN nwt_ticket_decisions d ON d.ticket_id = t.ticket_id
            WHERE (t.created_at AT TIME ZONE 'UTC')::date = %s
            """,
            (datetime.now(timezone.utc).date(),),
        )

        decisions = _q(
            conn,
            """
            SELECT
                t.type,
                t.from_agent,
                t.to_agent,
                COALESCE(t.payload->>'symbol', '')           AS symbol,
                COALESCE(t.payload->>'strategy_id', '')      AS strategy_id,
                COALESCE(t.payload->>'direction', '')        AS direction,
                COALESCE(t.payload->>'sized_notional', '')   AS sized_notional,
                d.decision,
                d.decided_by,
                LEFT(COALESCE(d.reasoning, ''), 140)         AS reasoning,
                d.created_at
            FROM nwt_ticket_decisions d
            JOIN nwt_tickets t ON t.ticket_id = d.ticket_id
            ORDER BY d.created_at DESC
            LIMIT 40
            """,
        )

        open_positions = _q(
            conn,
            """
            SELECT bot_source, asset, asset_type, direction,
                   notional_risk, entry_price, entry_time
            FROM nwt_portfolio_ledger
            WHERE status = 'open'
            ORDER BY entry_time DESC
            """,
        )

        system_log = _q(
            conn,
            """
            SELECT level, component, message, created_at
            FROM nwt_system_log
            ORDER BY created_at DESC
            LIMIT 20
            """,
        )

        ticket_types_today = _q(
            conn,
            """
            SELECT type, COUNT(*) AS cnt
            FROM nwt_tickets
            WHERE (created_at AT TIME ZONE 'UTC')::date = %s
            GROUP BY type
            ORDER BY cnt DESC
            """,
            (datetime.now(timezone.utc).date(),),
        )

        try:
            scorecard = _q(
                conn,
                """
                SELECT session_date, integrity_gate_passed, directives_fresh,
                       conviction_ran, tracks_ran, activity_logged,
                       risk_agent_clear, execution_clear, learning_agent_ran, green
                FROM nwt_session_scorecard
                ORDER BY session_date DESC
                LIMIT 14
                """,
            )
        except Exception:
            conn.rollback()
            scorecard = []  # table not migrated yet — render empty strip

        scorecard = list(reversed(scorecard))  # oldest → newest, left to right
        consecutive_green = 0
        for row in reversed(scorecard):
            if row.get("green"):
                consecutive_green += 1
            else:
                break

    finally:
        conn.close()

    directives = load_directives()
    regime = directives.get("regime", {})
    kill_switch = directives.get("global_kill_switch", None)
    bot_permissions = directives.get("bot_permissions", {})

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "summary": summary,
            "decisions": decisions,
            "open_positions": open_positions,
            "system_log": system_log,
            "ticket_types_today": ticket_types_today,
            "scorecard": scorecard,
            "consecutive_green": consecutive_green,
            "directives": directives,
            "regime": regime,
            "kill_switch": kill_switch,
            "bot_permissions": bot_permissions,
            "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "today": datetime.now(timezone.utc).date().isoformat(),
        },
    )
