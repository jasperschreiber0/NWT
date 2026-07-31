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
    # layer0_data.json is written by layer0_builder.py into nwt_agents/ itself
    # (shared_context.py's _agents_dir(), NOT _shared_dir()) — shared/ only
    # holds the Track A equity bots' candidate JSONs (checked in Stage 2).
    layer0_path = Path(os.environ.get("NWT_AGENTS_DIR", Path(__file__).parent)) / "layer0_data.json"
    exists, mtime, data, _ = _file_evidence(layer0_path)
    if not exists:
        record(1, "Market Data", "FAIL", [f"{layer0_path} does not exist"])
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
    """
    Every decision decided_by='EXECUTION_ENGINE' in the window — not filtered
    by reasoning text, which varies by code path (entry vs close vs
    force-close vs spread) and is not a reliable signal to grep. EXECUTED/
    PARTIAL means an order actually reached Alpaca and got a fill;
    REJECTED/FAILED means a ticket was seen but never placed.
    """
    evidence = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT decision, COUNT(*) AS n
            FROM nwt_ticket_decisions
            WHERE decided_by = 'EXECUTION_ENGINE' AND created_at > NOW() - INTERVAL '24 hours'
            GROUP BY decision
            """
        )
        rows = cur.fetchall()
    if not rows:
        record(5, "Order Execution", "FAIL",
               ["No EXECUTION_ENGINE decisions of any kind in the last 24h — "
                "either nothing was queued, or the engine isn't running"])
        return
    counts = {r["decision"]: r["n"] for r in rows}
    reached_alpaca = counts.get("EXECUTED", 0) + counts.get("PARTIAL", 0)
    for decision, n in counts.items():
        evidence.append(f"{decision}: {n}")
    if reached_alpaca == 0:
        evidence.append("0 EXECUTED/PARTIAL — nothing actually reached Alpaca; "
                        "everything was REJECTED/FAILED before or during submission")
        record(5, "Order Execution", "WARN", evidence)
    else:
        record(5, "Order Execution", "PASS", evidence)


# ---------------------------------------------------------------------------
# Stage 6 — Fill handling
# ---------------------------------------------------------------------------

def check_fill_handling(conn) -> None:
    """
    Reads the ledger directly rather than log message text — proves fills
    actually landed as data (a priced entry row, a closed row), independent
    of exactly how any given code path phrases its log line.
    """
    evidence = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS n FROM nwt_portfolio_ledger
            WHERE entry_time > NOW() - INTERVAL '24 hours' AND entry_price IS NOT NULL
            """
        )
        new_fills = cur.fetchone()["n"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM nwt_portfolio_ledger WHERE exit_time > NOW() - INTERVAL '24 hours'"
        )
        closed_fills = cur.fetchone()["n"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM nwt_portfolio_ledger WHERE status = 'pending_reconciliation'"
        )
        pending = cur.fetchone()["n"]
    evidence.append(f"New positions ledgered with a real entry_price (24h): {new_fills}")
    evidence.append(f"Positions closed (24h): {closed_fills}")
    evidence.append(f"Currently pending_reconciliation (unresolved fill data — needs manual review): {pending}")
    if new_fills == 0 and closed_fills == 0:
        record(6, "Fill Handling", "FAIL", evidence + ["No fill activity ledgered in the last 24h"])
        return
    record(6, "Fill Handling", "WARN" if pending > 0 else "PASS", evidence)


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
    """
    Joins to nwt_tickets.type to identify close tickets structurally
    (CLOSE_REQUEST / FORCE_CLOSE) instead of guessing from reasoning text.
    """
    evidence = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT d.decision, COUNT(*) AS n
            FROM nwt_ticket_decisions d
            JOIN nwt_tickets t ON t.ticket_id = d.ticket_id
            WHERE d.decided_by = 'EXECUTION_ENGINE'
              AND t.type IN ('CLOSE_REQUEST', 'FORCE_CLOSE')
              AND d.created_at > NOW() - INTERVAL '7 days'
            GROUP BY d.decision
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
        record(8, "Close Workflow", "FAIL",
               ["No CLOSE_REQUEST/FORCE_CLOSE decisions from EXECUTION_ENGINE in the last 7 days"])
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
