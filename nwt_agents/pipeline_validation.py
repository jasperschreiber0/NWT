"""
nwt_agents/pipeline_validation.py
Phase 3 evidence collector — proves (or disproves) each stage of the trading
pipeline against LIVE state. Run this on the server; it cannot be run from a
repo-only environment because every stage needs a live DB connection and/or
a live Alpaca session.

Usage:
    cd /home/northworld/trading/nwt_agents
    set -a && source .env && set +a
    python3 pipeline_validation.py

Exit code: 0 if every stage is PASS, 1 if any stage is FAIL. WARN does not
affect exit code (evidence exists but is outside a healthy threshold).

This intentionally does not "fix" anything or make trading decisions — it is
read-only evidence collection for the Phase 4 production readiness report.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import clean_alpaca_base_url, get_db  # noqa: E402

ALPACA_BASE_URL = clean_alpaca_base_url(os.environ.get("NWT_ALPACA_BASE_URL", "https://paper-api.alpaca.markets"))
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("NWT_ALPACA_KEY_ID", ""),
    "APCA-API-SECRET-KEY": os.environ.get("NWT_ALPACA_SECRET_KEY", ""),
}
SHARED_DIR = Path(os.environ.get("SHARED_DIR", Path(__file__).parent.parent / "shared"))
FRESH_HOURS = 24

results = []  # list of (stage_num, name, status, evidence_lines)


def record(stage: int, name: str, status: str, evidence: list) -> None:
    results.append((stage, name, status, evidence))


def _age_str(dt: datetime) -> str:
    if dt is None:
        return "unknown"
    age = datetime.now(timezone.utc) - dt
    hrs = age.total_seconds() / 3600
    return f"{hrs:.1f}h ago"


def _file_evidence(path: Path) -> tuple:
    """Returns (exists, mtime_utc, parsed_json_or_None, count_or_None)."""
    if not path.exists():
        return False, None, None, None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    try:
        data = json.loads(path.read_text())
    except Exception:
        return True, mtime, None, None
    count = len(data) if isinstance(data, list) else None
    return True, mtime, data, count


# ---------------------------------------------------------------------------
# Stage 1 — Market data
# ---------------------------------------------------------------------------

def check_market_data() -> None:
    evidence = []
    layer0_path = SHARED_DIR / "layer0_data.json"
    exists, mtime, data, _ = _file_evidence(layer0_path)
    if not exists:
        record(1, "Market Data", "FAIL", ["shared/layer0_data.json does not exist"])
        return
    stale = mtime < datetime.now(timezone.utc) - timedelta(hours=FRESH_HOURS)
    evidence.append(f"layer0_data.json mtime: {mtime.isoformat()} ({_age_str(mtime)})")
    if isinstance(data, dict) and "built_at" in data:
        evidence.append(f"built_at field: {data['built_at']}")
    status = "FAIL" if stale else "PASS"
    if stale:
        evidence.append(f"STALE — older than {FRESH_HOURS}h threshold")
    record(1, "Market Data", status, evidence)


# ---------------------------------------------------------------------------
# Stage 2 — Scanner
# ---------------------------------------------------------------------------

def check_scanner() -> None:
    evidence = []
    any_missing = False
    for bot in ("us", "eu", "aus", "china"):
        path = SHARED_DIR / f"{bot}-candidates.json"
        exists, mtime, data, count = _file_evidence(path)
        if not exists:
            evidence.append(f"{bot}: FILE MISSING")
            any_missing = True
            continue
        n = count if count is not None else (len(data.get("candidates", [])) if isinstance(data, dict) else "?")
        evidence.append(f"{bot}: {n} candidates, mtime {_age_str(mtime)}")
    record(2, "Scanner", "FAIL" if any_missing else "PASS", evidence)


# ---------------------------------------------------------------------------
# Stage 3 — AI decision engine
# ---------------------------------------------------------------------------

def check_ai_decisions(conn) -> None:
    evidence = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT from_agent, COUNT(*) AS n, MAX(created_at) AS last
            FROM nwt_tickets
            WHERE from_agent IN ('TRACK_C', 'TRACK_D', 'TRACK_E', 'NWT_TRACK_C', 'NWT_TRACK_D', 'NWT_TRACK_E')
              AND created_at > NOW() - INTERVAL '24 hours'
            GROUP BY from_agent
            """
        )
        rows = cur.fetchall()
    if not rows:
        record(3, "AI Decision Engine", "FAIL",
               ["No Track C/D/E tickets in nwt_tickets in the last 24h"])
        return
    for r in rows:
        evidence.append(f"{r['from_agent']}: {r['n']} tickets, last {r['last']}")
    record(3, "AI Decision Engine", "PASS", evidence)


# ---------------------------------------------------------------------------
# Stage 4 — Risk engine
# ---------------------------------------------------------------------------

def check_risk_engine(conn) -> None:
    evidence = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT decision, COUNT(*) AS n
            FROM nwt_ticket_decisions
            WHERE decided_by = 'RISK_AGENT' AND created_at > NOW() - INTERVAL '24 hours'
            GROUP BY decision
            """
        )
        rows = cur.fetchall()
    if not rows:
        record(4, "Risk Engine", "FAIL", ["No RISK_AGENT decisions in the last 24h"])
        return
    for r in rows:
        evidence.append(f"{r['decision']}: {r['n']}")
    record(4, "Risk Engine", "PASS", evidence)


# ---------------------------------------------------------------------------
# Stage 5 — Order execution
# ---------------------------------------------------------------------------

def check_order_execution(conn) -> None:
    evidence = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT decision, COUNT(*) AS n
            FROM nwt_ticket_decisions
            WHERE decided_by = 'EXECUTION_ENGINE'
              AND created_at > NOW() - INTERVAL '24 hours'
              AND reasoning ILIKE '%alpaca_order_id%'
            GROUP BY decision
            """
        )
        rows = cur.fetchall()
    if not rows:
        record(5, "Order Execution", "FAIL",
               ["No EXECUTION_ENGINE decisions referencing an alpaca_order_id in the last 24h"])
        return
    for r in rows:
        evidence.append(f"{r['decision']}: {r['n']}")
    record(5, "Order Execution", "PASS", evidence)


# ---------------------------------------------------------------------------
# Stage 6 — Fill handling
# ---------------------------------------------------------------------------

def check_fill_handling(conn) -> None:
    evidence = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT message, payload, created_at
            FROM nwt_system_log
            WHERE component = 'execution_engine'
              AND (message ILIKE '%Partial%' OR message ILIKE '%Executed%')
              AND created_at > NOW() - INTERVAL '24 hours'
            ORDER BY created_at DESC LIMIT 10
            """
        )
        rows = cur.fetchall()
    if not rows:
        record(6, "Fill Handling", "FAIL", ["No fill-related execution_engine log lines in the last 24h"])
        return
    partials = sum(1 for r in rows if "partial" in r["message"].lower())
    evidence.append(f"{len(rows)} fill events in last 24h, {partials} partial")
    for r in rows[:5]:
        evidence.append(f"  {r['created_at']}: {r['message']}")
    record(6, "Fill Handling", "PASS", evidence)


# ---------------------------------------------------------------------------
# Stage 7 — Ledger vs Alpaca
# ---------------------------------------------------------------------------

def check_ledger(conn) -> None:
    evidence = []
    try:
        resp = requests.get(f"{ALPACA_BASE_URL}/v2/positions", headers=ALPACA_HEADERS, timeout=20)
        resp.raise_for_status()
        alpaca_positions = resp.json()
    except Exception as exc:
        record(7, "Ledger vs Alpaca", "FAIL", [f"Could not fetch Alpaca positions: {exc}"])
        return

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE status IN ('open', 'pending_reconciliation')")
        ledger_open = [dict(r) for r in cur.fetchall()]

    alpaca_syms = {p["symbol"] for p in alpaca_positions}
    ledger_syms = {r["asset"] for r in ledger_open}
    missing_from_ledger = alpaca_syms - ledger_syms
    missing_from_alpaca = ledger_syms - alpaca_syms
    pending = [r for r in ledger_open if r.get("status") == "pending_reconciliation"]

    evidence.append(f"Alpaca open positions: {len(alpaca_positions)}")
    evidence.append(f"Ledger open+pending rows: {len(ledger_open)} ({len(pending)} pending_reconciliation)")
    if missing_from_ledger:
        evidence.append(f"in_alpaca_not_ledger: {sorted(missing_from_ledger)}")
    if missing_from_alpaca:
        evidence.append(f"in_ledger_not_alpaca: {sorted(missing_from_alpaca)}")

    status = "FAIL" if (missing_from_ledger or missing_from_alpaca) else "PASS"
    if pending and status == "PASS":
        status = "WARN"
    record(7, "Ledger vs Alpaca", status, evidence)


# ---------------------------------------------------------------------------
# Stage 8 — Close workflow
# ---------------------------------------------------------------------------

def check_close_workflow(conn) -> None:
    evidence = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT decision, COUNT(*) AS n
            FROM nwt_ticket_decisions
            WHERE decided_by = 'EXECUTION_ENGINE'
              AND created_at > NOW() - INTERVAL '7 days'
              AND (reasoning ILIKE '%broker-confirmed%' OR reasoning ILIKE '%Closed%'
                   OR reasoning ILIKE '%FORCE_CLOSE%')
            GROUP BY decision
            """
        )
        rows = cur.fetchall()
        cur.execute(
            """
            SELECT COUNT(*) AS n FROM nwt_system_log
            WHERE component = 'reconciliation' AND created_at > NOW() - INTERVAL '7 days'
            """
        )
        recon_events = cur.fetchone()["n"]
    if not rows:
        record(8, "Close Workflow", "FAIL", ["No close-related EXECUTION_ENGINE decisions in the last 7 days"])
        return
    for r in rows:
        evidence.append(f"{r['decision']}: {r['n']} (7d)")
    evidence.append(f"reconciliation events logged (7d): {recon_events}")
    record(8, "Close Workflow", "PASS", evidence)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"=== NWT Pipeline Validation — {datetime.now(timezone.utc).isoformat()} ===\n")

    check_market_data()
    check_scanner()

    conn = get_db()
    try:
        check_ai_decisions(conn)
        check_risk_engine(conn)
        check_order_execution(conn)
        check_fill_handling(conn)
        check_ledger(conn)
        check_close_workflow(conn)
    finally:
        conn.close()

    any_fail = False
    for stage, name, status, evidence in results:
        icon = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}[status]
        print(f"[{icon}] Stage {stage} — {name}")
        for line in evidence:
            print(f"    {line}")
        print()
        if status == "FAIL":
            any_fail = True

    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
