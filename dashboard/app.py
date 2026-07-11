"""
dashboard/app.py
NorthWorld Trading — Command Centre Dashboard backend.
FastAPI + Bearer token auth. Serves static/index.html + JSON API.
"""

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
import psycopg2.extras
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv("/home/northworld/trading/nwt_agents/.env")

DB_DSN = os.environ["NWT_DB_DSN"]
TOKEN  = os.environ["NWT_DASHBOARD_TOKEN"]

SHARED = Path("/home/northworld/trading/shared")
PERF   = Path("/home/northworld/trading/performance")

THEME_TICKERS = {
    "ai_power":          ["ETN", "PWR", "VRT", "POWL", "EMR"],
    "ai_networking":     ["ANET", "AVGO", "CSCO"],
    "ai_cooling":        ["VRT", "TT", "GNRC"],
    "nuclear":           ["CCJ", "NNE", "LEU"],
    "robotics":          ["TDY", "ISRG", "ONTO"],
    "copper_constraint": ["FCX", "SCCO", "WIRE"],
}

app = FastAPI(docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def require_auth(request: Request) -> None:
    if request.headers.get("Authorization", "") != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def db_conn():
    return psycopg2.connect(DB_DSN, cursor_factory=psycopg2.extras.RealDictCursor)


def q(conn, sql: str, params=None) -> list:
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return [dict(r) for r in cur.fetchall()]


def q_grace(conn, sql: str, params=None) -> list:
    try:
        return q(conn, sql, params)
    except psycopg2.errors.UndefinedTable:
        conn.rollback()
        return []


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def file_mtime_iso(path: Path):
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return None


@app.get("/api/health")
def health(_: None = Depends(require_auth)):
    try:
        db_conn().close()
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "db_error",
            "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/portfolio")
def portfolio(_: None = Depends(require_auth)):
    conn = db_conn()
    try:
        positions  = q(conn, "SELECT * FROM nwt_portfolio_ledger WHERE status='open' ORDER BY entry_time DESC")
        directives = read_json(SHARED / "master-directives.json")
        by_bot: dict = {}
        for p in positions:
            bot = p.get("bot_source", "unknown")
            by_bot[bot] = by_bot.get(bot, 0) + 1
        return {
            "open_positions": positions,
            "directives": directives,
            "summary": {
                "total_open": len(positions),
                "by_bot": by_bot,
                "net_delta": directives.get("net_delta_estimate", 0),
                "net_vega":  directives.get("net_vega_estimate", 0),
            },
        }
    finally:
        conn.close()


@app.get("/api/performance")
def performance(_: None = Depends(require_auth)):
    conn = db_conn()
    try:
        summary        = read_json(PERF / "summary.json")
        agent_state    = q_grace(conn, "SELECT * FROM nwt_agent_state ORDER BY updated_at DESC")
        trade_outcomes = q_grace(conn, """
            SELECT strategy_id, SUM(pnl) AS total_pnl, COUNT(*) AS trades,
                   AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate
            FROM nwt_trade_outcomes GROUP BY strategy_id
        """)
        equity_raw = q_grace(conn, """
            SELECT exit_time, pnl FROM nwt_trade_outcomes
            WHERE exit_time IS NOT NULL ORDER BY exit_time ASC
        """)
        cumulative = 0.0
        equity_curve = []
        for row in equity_raw:
            cumulative += float(row.get("pnl") or 0)
            d = row["exit_time"]
            equity_curve.append({
                "date": d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d),
                "cumulative_pnl": round(cumulative, 2),
            })

        all_trades = q_grace(conn, "SELECT pnl FROM nwt_trade_outcomes")
        wins   = [t for t in all_trades if (t.get("pnl") or 0) > 0]
        losses = [t for t in all_trades if (t.get("pnl") or 0) < 0]
        win_rate      = len(wins) / len(all_trades) if all_trades else 0
        avg_win       = sum(float(t["pnl"]) for t in wins)  / len(wins)   if wins   else 0
        avg_loss      = abs(sum(float(t["pnl"]) for t in losses)) / len(losses) if losses else 0
        profit_factor = avg_win / avg_loss if avg_loss else 0

        peak = running = max_dd = 0.0
        for row in equity_raw:
            running += float(row.get("pnl") or 0)
            peak = max(peak, running)
            dd = (peak - running) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)

        tc = len(all_trades)
        return {
            "summary": summary,
            "agent_state": agent_state,
            "trade_outcomes": trade_outcomes,
            "equity_curve": equity_curve,
            "thresholds": {
                "win_rate":      {"value": round(win_rate, 3),      "target": 0.55, "pass": win_rate >= 0.55},
                "profit_factor": {"value": round(profit_factor, 2), "target": 1.5,  "pass": profit_factor >= 1.5},
                "max_drawdown":  {"value": round(max_dd, 3),        "target": 0.03, "pass": max_dd <= 0.03},
                "trade_count":   {"value": tc,                      "target": 60,   "pass": tc >= 60},
            },
        }
    finally:
        conn.close()


@app.get("/api/scorecard")
def scorecard(_: None = Depends(require_auth)):
    conn = db_conn()
    try:
        rows = q_grace(conn, """
            SELECT session_date, integrity_gate_passed, directives_fresh,
                   conviction_ran, tracks_ran, activity_logged,
                   risk_agent_clear, execution_clear, learning_agent_ran, green,
                   manual_interventions, details
            FROM nwt_session_scorecard
            ORDER BY session_date DESC LIMIT 30
        """)
        rows = list(reversed(rows))
        consecutive_green = 0
        for row in reversed(rows):
            if row.get("green"):
                consecutive_green += 1
            else:
                break
        return {"scorecard": rows, "consecutive_green": consecutive_green}
    finally:
        conn.close()


@app.get("/api/track-f/scores")
def track_f_scores(_: None = Depends(require_auth)):
    conn = db_conn()
    try:
        rows = q_grace(conn, "SELECT DISTINCT ON (ticker) * FROM nwt_bottleneck_scores ORDER BY ticker, scored_at DESC")
        rows.sort(key=lambda r: r.get("bottleneck_score") or 0, reverse=True)
        return rows
    finally:
        conn.close()


@app.get("/api/track-f/pending")
def track_f_pending(_: None = Depends(require_auth)):
    conn = db_conn()
    try:
        return q_grace(conn, "SELECT * FROM nwt_emerging_themes WHERE status='pending' ORDER BY momentum DESC")
    finally:
        conn.close()


def _track_f_update(sql: str, theme: str) -> dict:
    """
    Track F has no production scanner yet — nwt_emerging_themes does not
    exist in any schema/migration file. Rather than a raw 500 (UndefinedTable)
    when someone clicks Approve/Reject on the dashboard's "not yet deployed"
    tab, return a clear, typed error the frontend can show.
    """
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (theme,))
        conn.commit()
        return {"ok": True, "theme": theme}
    except psycopg2.errors.UndefinedTable:
        conn.rollback()
        raise HTTPException(
            status_code=501,
            detail="Track F is not deployed yet — nwt_emerging_themes does not exist.",
        )
    finally:
        conn.close()


@app.post("/api/track-f/approve")
async def track_f_approve(request: Request, _: None = Depends(require_auth)):
    body  = await request.json()
    theme = body.get("candidate_theme")
    if not theme:
        raise HTTPException(status_code=400, detail="candidate_theme required")
    return _track_f_update(
        "UPDATE nwt_emerging_themes SET status='approved', approved_at=NOW() WHERE candidate_theme=%s AND status='pending'",
        theme,
    )


@app.post("/api/track-f/reject")
async def track_f_reject(request: Request, _: None = Depends(require_auth)):
    body  = await request.json()
    theme = body.get("candidate_theme")
    if not theme:
        raise HTTPException(status_code=400, detail="candidate_theme required")
    return _track_f_update(
        "UPDATE nwt_emerging_themes SET status='rejected' WHERE candidate_theme=%s AND status='pending'",
        theme,
    )


@app.get("/api/track-f/candidates")
def track_f_candidates(_: None = Depends(require_auth)):
    conn = db_conn()
    try:
        return q_grace(conn, "SELECT * FROM nwt_track_f_candidates WHERE status IN ('pending','approved') ORDER BY created_at DESC LIMIT 20")
    finally:
        conn.close()


@app.get("/api/track-f/theme-exposure")
def track_f_theme_exposure(_: None = Depends(require_auth)):
    conn = db_conn()
    try:
        positions = q(conn, "SELECT asset, notional_risk FROM nwt_portfolio_ledger WHERE status='open'")
    finally:
        conn.close()

    total = sum(float(p.get("notional_risk") or 0) for p in positions)
    ticker_notional: dict = {}
    for p in positions:
        ticker_notional[p["asset"]] = ticker_notional.get(p["asset"], 0) + float(p.get("notional_risk") or 0)

    exposures: dict = {}
    for theme, tickers in THEME_TICKERS.items():
        notional = sum(ticker_notional.get(t, 0) for t in tickers)
        exposures[theme] = {
            "pct":      round(notional / total, 4) if total else 0,
            "tickers":  [t for t in tickers if ticker_notional.get(t, 0) > 0],
            "notional": round(notional, 2),
        }
    return {"cap": 0.15, "exposures": exposures}


@app.get("/api/tickets")
def tickets(_: None = Depends(require_auth)):
    conn = db_conn()
    try:
        rows = q(conn, """
            SELECT t.ticket_id, t.from_agent, t.to_agent, t.type, t.created_at,
                   d.decision, d.reasoning, d.decided_by
            FROM nwt_tickets t
            LEFT JOIN nwt_ticket_decisions d ON t.ticket_id = d.ticket_id
            ORDER BY t.created_at DESC LIMIT 50
        """)
        for r in rows:
            r["has_decision"] = r.get("decision") is not None
        return rows
    finally:
        conn.close()


@app.get("/api/budget")
def budget(_: None = Depends(require_auth)):
    conn = db_conn()
    try:
        return q_grace(conn, "SELECT * FROM nwt_budget_state ORDER BY cost_usd DESC")
    finally:
        conn.close()


@app.get("/api/health/pm2")
def health_pm2(_: None = Depends(require_auth)):
    try:
        result = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=10)
        procs  = json.loads(result.stdout)
        return [
            {
                "name":      p.get("name"),
                "status":    p.get("pm2_env", {}).get("status"),
                "restarts":  p.get("pm2_env", {}).get("restart_time", 0),
                "uptime_ms": p.get("pm2_env", {}).get("pm_uptime", 0),
                "memory_mb": round(p.get("monit", {}).get("memory", 0) / 1024 / 1024, 1),
                "cpu_pct":   p.get("monit", {}).get("cpu", 0),
            }
            for p in procs
        ]
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/api/health/agents")
def health_agents(_: None = Depends(require_auth)):
    conn = db_conn()
    try:
        agent_rows = q(conn, "SELECT from_agent, MAX(created_at) AS last_fired FROM nwt_tickets GROUP BY from_agent ORDER BY last_fired DESC")
    finally:
        conn.close()

    agent_last_fired = {
        r["from_agent"]: r["last_fired"].isoformat() if r["last_fired"] else None
        for r in agent_rows
    }

    shared_files = [
        "master-directives.json", "us-candidates.json", "eu-candidates.json",
        "aus-candidates.json", "china-candidates.json", "track_f_scores.json",
    ]
    file_last_modified = {f.replace(".json", ""): file_mtime_iso(SHARED / f) for f in shared_files}

    mem_pct = disk_pct = 0
    try:
        out = subprocess.run(["free", "-m"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                mem_pct = round(int(parts[2]) / int(parts[1]) * 100) if int(parts[1]) else 0
    except Exception:
        pass
    try:
        u = shutil.disk_usage("/")
        disk_pct = round(u.used / u.total * 100)
    except Exception:
        pass

    return {
        "agent_last_fired":   agent_last_fired,
        "file_last_modified": file_last_modified,
        "system": {"memory_pct": mem_pct, "disk_pct": disk_pct},
    }


app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.get("/")
def root():
    return FileResponse(str(Path(__file__).parent / "static" / "index.html"))
