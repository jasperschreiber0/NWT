#!/usr/bin/env python3
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

BASE = Path(__file__).parent
SHARED = BASE.parent / "shared"
PERF = BASE


def get_db():
    dsn = os.environ["NWT_DB_DSN"]
    return psycopg2.connect(dsn)


def compute_summary(conn):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT strategy_id, pnl, pnl_pct, direction, symbol,
                   regime_at_entry, iv_at_entry, dte_at_entry,
                   entry_timing_score, exit_timing_score,
                   slippage_adjusted_efficiency
            FROM nwt_trade_outcomes
            WHERE closed_at IS NOT NULL
            ORDER BY closed_at
        """)
        trades = cur.fetchall()

    if not trades:
        return {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "total_trades": 0,
            "win_rate": None,
            "profit_factor": None,
            "max_drawdown": None,
            "total_pnl": 0,
            "by_bot": {},
            "by_strategy": {}
        }

    wins = [t for t in trades if (t["pnl"] or 0) > 0]
    losses = [t for t in trades if (t["pnl"] or 0) <= 0]
    win_rate = len(wins) / len(trades) if trades else 0
    gross_profit = sum(float(t["pnl"] or 0) for t in wins)
    gross_loss = abs(sum(float(t["pnl"] or 0) for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

    # Max drawdown from cumulative PnL curve
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cumulative += float(t["pnl"] or 0)
        if cumulative > peak:
            peak = cumulative
        dd = (peak - cumulative) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    by_strategy = {}
    for t in trades:
        sid = t["strategy_id"]
        if sid not in by_strategy:
            by_strategy[sid] = {"trades": 0, "wins": 0, "pnl": 0.0}
        by_strategy[sid]["trades"] += 1
        if float(t["pnl"] or 0) > 0:
            by_strategy[sid]["wins"] += 1
        by_strategy[sid]["pnl"] += float(t["pnl"] or 0)

    for sid, s in by_strategy.items():
        s["win_rate"] = round(s["wins"] / s["trades"], 4) if s["trades"] > 0 else 0.0
        s["pnl"] = round(s["pnl"], 2)

    # Open positions summary from portfolio ledger
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT bot_source, asset, asset_type, direction, notional_risk, entry_time
            FROM nwt_portfolio_ledger
            WHERE status = 'open'
            ORDER BY entry_time DESC
        """)
        open_positions = cur.fetchall()

    open_summary = []
    for pos in open_positions:
        open_summary.append({
            "bot_source": pos["bot_source"],
            "asset": pos["asset"],
            "asset_type": pos["asset_type"],
            "direction": pos["direction"],
            "notional_risk": float(pos["notional_risk"] or 0),
            "entry_time": pos["entry_time"].isoformat() if pos["entry_time"] else None
        })

    return {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_trades": len(trades),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "max_drawdown": round(max_dd, 4),
        "total_pnl": round(sum(float(t["pnl"] or 0) for t in trades), 2),
        "open_positions_count": len(open_positions),
        "open_positions": open_summary,
        "by_bot": {},
        "by_strategy": by_strategy
    }


def log_event(conn, level, message, payload=None):
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO nwt_system_log (level, component, message, payload) VALUES (%s, %s, %s, %s)",
                (
                    level,
                    "perf-tracker",
                    message,
                    json.dumps(payload) if payload is not None else None
                )
            )
        conn.commit()
    except Exception as exc:
        # Log failure must not crash the tracker
        print(f"[perf-tracker] WARNING: could not write to nwt_system_log: {exc}", file=sys.stderr)


def write_equity_curve(conn) -> None:
    """
    Fetch Alpaca account equity and write today's row to nwt_equity_curve.
    Risk Agent's drawdown calculation reads this table.
    """
    import os
    import requests
    alpaca_base = os.environ.get("ALPACA_BASE_URL", "").rstrip("/")
    alpaca_key = os.environ.get("ALPACA_API_KEY", "")
    alpaca_secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not alpaca_base:
        print("[perf-tracker] ALPACA_BASE_URL not set — skipping equity curve write", file=sys.stderr)
        return
    try:
        resp = requests.get(
            f"{alpaca_base}/v2/account",
            headers={"APCA-API-KEY-ID": alpaca_key, "APCA-API-SECRET-KEY": alpaca_secret},
            timeout=15,
        )
        resp.raise_for_status()
        equity = float(resp.json().get("equity", 0))
        if equity <= 0:
            return
        today = datetime.now(timezone.utc).date()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO nwt_equity_curve (date, equity, source)
                VALUES (%s, %s, 'alpaca')
                ON CONFLICT (date) DO UPDATE SET equity=%s, source='alpaca'
                """,
                (today, equity, equity),
            )
        conn.commit()
        print(f"[perf-tracker] Equity curve: {today} equity={equity:.2f}")
    except Exception as exc:
        print(f"[perf-tracker] WARNING: equity curve write failed: {exc}", file=sys.stderr)


def main():
    conn = get_db()
    try:
        # Write today's equity to nwt_equity_curve (used by Risk Agent drawdown rule)
        write_equity_curve(conn)

        summary = compute_summary(conn)
        summary_path = PERF / "summary.json"
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2)
        log_event(
            conn,
            "INFO",
            f"Performance summary updated. Trades: {summary['total_trades']}, Win rate: {summary['win_rate']}",
            {
                "total_trades": summary["total_trades"],
                "win_rate": summary["win_rate"],
                "profit_factor": summary["profit_factor"],
                "max_drawdown": summary["max_drawdown"],
                "total_pnl": summary["total_pnl"]
            }
        )
        print(
            f"[perf-tracker] Done. {summary['total_trades']} trades, "
            f"win_rate={summary['win_rate']}, profit_factor={summary['profit_factor']}, "
            f"max_drawdown={summary['max_drawdown']}, total_pnl={summary['total_pnl']}"
        )
    except Exception as e:
        log_event(conn, "ERROR", f"perf-tracker failed: {e}")
        print(f"[perf-tracker] ERROR: {e}", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
