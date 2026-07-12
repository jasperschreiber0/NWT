"""
nwt_agents/shadow_decision_evaluator.py
Runs once daily (after learning_agent). Evaluates nwt_decision_inputs rows
whose dte_target window has fully elapsed and fills in:
  would_have_won, shadow_exit_price, shadow_pnl_pct

This answers "would this candidate have won?" for every strategy that was
eligible to trade but never got a real ticket — either because another
strategy won the archetype-consolidation pick, or because sizing was zero.

IMPORTANT SIMPLIFICATION: this walks the underlying's daily bars from the
decision date and checks target_pct/stop_pct against each day's high/low
as a directional proxy for what the options position would have done. It
does NOT simulate the actual option premium (no historical chain snapshot
is captured per candidate at decision time — building one would require
per-candidate options chain snapshots, out of scope here). Treat
shadow_pnl_pct as a signal-quality indicator (did the underlying move the
way this strategy needed it to), not a dollar PnL estimate. Real PnL only
ever comes from nwt_trade_outcomes for actually-executed trades.

On a same-day ambiguity (a bar's high clears target AND its low clears
stop), this resolves conservatively toward the stop — consistent with
"assume the worse outcome when order isn't observable" used elsewhere in
the system (e.g. VIX=0 treated as missing, not favorable).
"""

import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from shared_context import clean_alpaca_base_url, get_db, log_system_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("shadow_decision_evaluator")

ALPACA_DATA_URL = clean_alpaca_base_url(os.environ.get("NWT_ALPACA_DATA_URL", "https://data.alpaca.markets"))
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ["NWT_ALPACA_KEY_ID"],
    "APCA-API-SECRET-KEY": os.environ["NWT_ALPACA_SECRET_KEY"],
}


def fetch_pending_candidates(conn) -> list:
    """
    Rows whose dte_target window has fully elapsed and haven't been
    shadow-evaluated yet. entry_price_ref must be present — candidates
    logged without a layer0 price (rare, missing data) are left NULL
    forever rather than guessed at.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, symbol, direction, entry_price_ref, target_pct, stop_pct,
                   dte_target, run_date
            FROM nwt_decision_inputs
            WHERE shadow_evaluated_at IS NULL
              AND entry_price_ref IS NOT NULL
              AND target_pct IS NOT NULL
              AND stop_pct IS NOT NULL
              AND dte_target IS NOT NULL
              AND run_date + (dte_target || ' days')::interval <= NOW()
            ORDER BY run_date ASC
            LIMIT 500
            """
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_bars(symbol: str, start: date, end: date) -> list:
    """Daily bars for symbol from start to end (inclusive). [] on any failure."""
    url = f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/bars"
    params = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timeframe": "1Day",
        "adjustment": "split",
        "limit": 100,
    }
    try:
        resp = requests.get(url, headers=ALPACA_HEADERS, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json().get("bars", [])
    except Exception as exc:
        logger.warning("Failed to fetch bars for %s: %s", symbol, exc)
        return []


def simulate_outcome(bars: list, direction: str, entry_price: float, target_pct: float, stop_pct: float):
    """
    Walk bars chronologically; return (would_have_won, exit_price, pnl_pct).
    First threshold touched wins (stop wins same-day ties, conservative).
    If neither threshold is touched by the last bar, resolve at final close.
    """
    sign = 1.0 if direction != "short" else -1.0

    for bar in bars:
        high = float(bar.get("h", 0))
        low = float(bar.get("l", 0))
        if not high or not low:
            continue

        if direction == "short":
            # Favorable move is DOWN; adverse move is UP.
            favorable_pct = (entry_price - low) / entry_price
            adverse_pct = (high - entry_price) / entry_price
        else:
            favorable_pct = (high - entry_price) / entry_price
            adverse_pct = (entry_price - low) / entry_price

        if adverse_pct >= abs(stop_pct):
            exit_price = entry_price * (1 - sign * abs(stop_pct))
            return False, round(exit_price, 4), round(-abs(stop_pct), 6)
        if favorable_pct >= abs(target_pct):
            exit_price = entry_price * (1 + sign * abs(target_pct))
            return True, round(exit_price, 4), round(abs(target_pct), 6)

    if not bars:
        return None, None, None

    final_close = float(bars[-1].get("c", entry_price))
    pnl_pct = sign * (final_close - entry_price) / entry_price
    return pnl_pct > 0, round(final_close, 4), round(pnl_pct, 6)


def main() -> None:
    conn = get_db()
    evaluated = 0
    skipped_no_bars = 0

    try:
        candidates = fetch_pending_candidates(conn)
        logger.info("Found %d pending shadow candidates", len(candidates))

        for row in candidates:
            run_date = row["run_date"]
            end_date = run_date + timedelta(days=row["dte_target"] + 2)  # small buffer for holidays
            end_date = min(end_date, date.today())

            bars = fetch_bars(row["symbol"], run_date, end_date)
            if not bars:
                skipped_no_bars += 1
                continue

            would_have_won, exit_price, pnl_pct = simulate_outcome(
                bars,
                row["direction"] or "long",
                float(row["entry_price_ref"]),
                float(row["target_pct"]),
                float(row["stop_pct"]),
            )

            if would_have_won is None:
                skipped_no_bars += 1
                continue

            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE nwt_decision_inputs
                    SET shadow_evaluated_at = NOW(),
                        would_have_won = %s,
                        shadow_exit_price = %s,
                        shadow_pnl_pct = %s
                    WHERE id = %s
                    """,
                    (would_have_won, exit_price, pnl_pct, row["id"]),
                )
            conn.commit()
            evaluated += 1

        log_system_event(
            conn, "INFO", "shadow_decision_evaluator",
            f"Evaluated {evaluated} shadow candidates, {skipped_no_bars} skipped (no bar data)",
            {"evaluated": evaluated, "skipped_no_bars": skipped_no_bars},
        )
        logger.info("Done — evaluated=%d skipped_no_bars=%d", evaluated, skipped_no_bars)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
