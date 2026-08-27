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

Look-ahead: the walk must only ever use bars strictly after decision_time.
For a strategist that decides pre-market (EU 09:30 UTC, AUS 09:00 UTC),
the decision-day's own daily bar (covering the session open onward) is
entirely after decision_time and is safe to include. For one that decides
intraday (China's 30-minute polls 14:00-18:00 UTC, the US ORB bot at
18:05 UTC, Track C/D/E at 14:00-14:30 UTC), that same bar's high/low would
also reflect price action from BEFORE decision_time — using it would let
the counterfactual "see" a move that may have already happened before the
signal fired. bar_walk_start_date() excludes the decision-day bar entirely
for intraday decisions, starting the walk the next calendar day instead.
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

    Excludes outcome_reason='EXECUTED': a row that became a real trade has
    a real nwt_trade_outcomes row already — shadow-evaluating it too would
    produce a second, counterfactual PnL figure sitting next to the real
    one, exactly the "pretend shadow PnL is realized PnL" confusion this
    table must not create. Track-agnostic by construction — the underlying
    IS the tradeable instrument for Track A (no proxy needed), and the
    directional-proxy walk already used for Track C/D/E options applies
    unchanged.

    Also returns run_at — needed by simulate_outcome's caller to decide
    whether the decision-day's own daily bar is safe to include in the walk
    (see INTRADAY_DECISION_CUTOFF_UTC below).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, symbol, direction, entry_price_ref, target_pct, stop_pct,
                   dte_target, run_date, run_at
            FROM nwt_decision_inputs
            WHERE shadow_evaluated_at IS NULL
              AND entry_price_ref IS NOT NULL
              AND target_pct IS NOT NULL
              AND stop_pct IS NOT NULL
              AND dte_target IS NOT NULL
              AND (outcome_reason IS NULL OR outcome_reason != 'EXECUTED')
              AND run_date + (dte_target || ' days')::interval <= NOW()
            ORDER BY run_date ASC
            LIMIT 500
            """
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# A decision made at or after this UTC time-of-day is treated as intraday —
# the decision-day's own DAILY bar (open covers the full session, 13:30-20:00
# UTC / 14:30-21:00 UTC in winter) would then straddle decision_time, mixing
# pre-decision price action into what must be a strictly-after-decision
# counterfactual walk. 12:00 UTC is safely before US market open in EITHER
# DST state (13:30 UTC summer / 14:30 UTC winter) — a decision this early can
# only be EU's 09:30 UTC or AUS's 09:00 UTC pre-market run, whose decision-day
# bar (entirely 13:30/14:30-20:00/21:00 UTC) is guaranteed entirely after
# decision_time regardless of exact DST offset. China (14:00-18:00 UTC), the
# US ORB bot (18:05 UTC), and Track C/D/E (14:00-14:30 UTC) all decide after
# this cutoff and are the ones this guards. Being coarse here is always safe
# in the conservative direction: a decision near the boundary that's actually
# pre-market can only cause the (harmless) loss of one legitimate day of
# signal, never the inclusion of a contaminated one — see the module
# docstring's "Look-ahead" note.
INTRADAY_DECISION_CUTOFF_UTC = 12  # hour, UTC, no DST adjustment needed — see above


def bar_walk_start_date(run_date: date, run_at: datetime | None) -> date:
    """
    The first date safe to include in the shadow walk. If the decision was
    made intraday (run_at's UTC hour >= INTRADAY_DECISION_CUTOFF_UTC), the
    decision-day's own daily bar is excluded entirely — its high/low would
    include price action from before decision_time, which is look-ahead
    contamination of the counterfactual, not "subsequent market data." The
    walk then starts the NEXT calendar day, whose bar is unambiguously
    entirely after decision_time. run_at missing (legacy rows, pre-dating
    this column being read) is treated as intraday — the conservative
    default, never the permissive one.
    """
    if run_at is None or run_at.astimezone(timezone.utc).hour >= INTRADAY_DECISION_CUTOFF_UTC:
        return run_date + timedelta(days=1)
    return run_date


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
    Walk bars chronologically; return
    (would_have_won, exit_price, pnl_pct, mfe_pct, mae_pct, completion).
    First threshold touched wins (stop wins same-day ties, conservative).
    If neither threshold is touched by the last bar, resolve at final close
    (completion='HORIZON_EXPIRED'). mfe_pct/mae_pct are the best/worst
    favorable-direction excursion seen at any point during the walk, tracked
    independently of which threshold eventually resolves the trade.
    """
    sign = 1.0 if direction != "short" else -1.0
    mfe_pct = 0.0
    mae_pct = 0.0

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

        mfe_pct = max(mfe_pct, favorable_pct)
        mae_pct = max(mae_pct, adverse_pct)

        if adverse_pct >= abs(stop_pct):
            exit_price = entry_price * (1 - sign * abs(stop_pct))
            return (False, round(exit_price, 4), round(-abs(stop_pct), 6),
                    round(mfe_pct, 6), round(mae_pct, 6), "STOP_HIT")
        if favorable_pct >= abs(target_pct):
            exit_price = entry_price * (1 + sign * abs(target_pct))
            return (True, round(exit_price, 4), round(abs(target_pct), 6),
                    round(mfe_pct, 6), round(mae_pct, 6), "TARGET_HIT")

    if not bars:
        return None, None, None, None, None, None

    final_close = float(bars[-1].get("c", entry_price))
    pnl_pct = sign * (final_close - entry_price) / entry_price
    return (pnl_pct > 0, round(final_close, 4), round(pnl_pct, 6),
            round(mfe_pct, 6), round(mae_pct, 6), "HORIZON_EXPIRED")


def main() -> None:
    conn = get_db()
    evaluated = 0
    skipped_no_bars = 0

    try:
        candidates = fetch_pending_candidates(conn)
        logger.info("Found %d pending shadow candidates", len(candidates))

        for row in candidates:
            run_date = row["run_date"]
            walk_start = bar_walk_start_date(run_date, row.get("run_at"))
            # dte_target is measured from run_date regardless of walk_start,
            # so an intraday decision's walk still ends dte_target days after
            # the actual decision date, just starting one day later.
            end_date = run_date + timedelta(days=row["dte_target"] + 2)  # small buffer for holidays
            end_date = min(end_date, date.today())
            if walk_start > end_date:
                # Horizon elapsed but the one-day intraday exclusion leaves
                # no evaluable days (e.g. dte_target=0 for an intraday
                # decision) — genuinely nothing to walk, not a fetch failure.
                skipped_no_bars += 1
                continue

            bars = fetch_bars(row["symbol"], walk_start, end_date)
            if not bars:
                skipped_no_bars += 1
                continue

            would_have_won, exit_price, pnl_pct, mfe_pct, mae_pct, completion = simulate_outcome(
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
                        shadow_pnl_pct = %s,
                        shadow_mfe_pct = %s,
                        shadow_mae_pct = %s,
                        shadow_completion = %s
                    WHERE id = %s
                    """,
                    (would_have_won, exit_price, pnl_pct, mfe_pct, mae_pct, completion, row["id"]),
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
