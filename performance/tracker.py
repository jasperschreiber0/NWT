#!/usr/bin/env python3
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

BASE = Path(__file__).parent
SHARED = BASE.parent / "shared"
PERF = BASE


def get_db():
    load_dotenv(BASE.parent / 'nwt_agents' / '.env')
    dsn = os.environ["NWT_DB_DSN"]
    return psycopg2.connect(dsn)


def equity_drawdown(conn):
    with conn.cursor() as cur:
        cur.execute('SELECT date,equity FROM nwt_equity_curve WHERE equity>0 ORDER BY date')
        rows = cur.fetchall()
    peak = 0.0
    worst = 0.0
    for _, value in rows:
        equity = float(value)
        peak = max(peak, equity)
        worst = max(worst, (peak-equity)/peak)
    return (round(worst, 4) if len(rows)>1 else None), len(rows)


def compute_summary(conn):
    # nwt_trade_outcomes is one row per LEG, not per trade — a multi-leg
    # spread (bull_call_spread/bear_put_spread/iron_condor) writes 2-4 rows
    # for what is actually one trade, tied together via
    # nwt_portfolio_ledger.spread_group_id. Collapse to one row per real
    # trade (COALESCE(spread_group_id, position_id, id) as the trade
    # identity) before computing win_rate/profit_factor/total_trades, or a
    # single losing iron condor reports as 4 losing trades.
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT
                COALESCE(pl.spread_group_id, to_.position_id, to_.id) AS trade_key,
                MAX(to_.strategy_id) AS strategy_id,
                SUM(COALESCE(to_.pnl_adjusted, to_.pnl)) AS pnl,
                MAX(to_.closed_at) AS closed_at
            FROM nwt_trade_outcomes to_
            LEFT JOIN nwt_portfolio_ledger pl ON pl.position_id = to_.position_id
            WHERE to_.closed_at IS NOT NULL
            GROUP BY trade_key
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

    # Recorded account-equity observations include starting capital and open P&L.
    # A decline in cumulative realized profit is not account drawdown.
    max_dd, equity_observations = equity_drawdown(conn)

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
        "max_drawdown": max_dd,
        "drawdown_basis": "recorded_account_equity_only_not_full_history_or_intraday",
        "equity_observations": equity_observations,
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
    alpaca_base = os.environ.get("NWT_ALPACA_BASE_URL", "").rstrip("/")
    alpaca_key = os.environ.get("NWT_ALPACA_KEY_ID", "")
    alpaca_secret = os.environ.get("NWT_ALPACA_SECRET_KEY", "")
    if not alpaca_base:
        raise RuntimeError('NWT_ALPACA_BASE_URL missing; equity curve not updated')
    if alpaca_base != 'https://paper-api.alpaca.markets':
        raise RuntimeError('Paper broker endpoint required')
    try:
        resp = requests.get(
            f"{alpaca_base}/v2/account",
            headers={"APCA-API-KEY-ID": alpaca_key, "APCA-API-SECRET-KEY": alpaca_secret},
            timeout=15,
        )
        resp.raise_for_status()
        equity = float(resp.json().get("equity", 0))
        if equity <= 0:
            raise ValueError('Invalid broker equity')
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
        conn.rollback()
        raise RuntimeError('Equity curve update failed') from exc


def main():
    conn = get_db()
    try:
        # Write today's equity to nwt_equity_curve (used by Risk Agent drawdown rule)
        write_equity_curve(conn)

        summary = compute_summary(conn)
        summary_path = PERF / "summary.json"
        temporary = summary_path.with_suffix('.tmp')
        with open(temporary, "w") as f:
            json.dump(summary, f, indent=2)
        temporary.replace(summary_path)
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
