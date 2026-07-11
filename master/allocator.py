"""
master/allocator.py — Learning Layer D: Portfolio Allocator.

Answers "where should capital go?" — never "what should we trade?". This is
not a separate service: master/strategist.py calls compute_dynamic_weights()
as the starting point for its own compute_bot_permissions(), replacing the
hardcoded BASELINE_WEIGHTS dict with a starting point that tilts toward
whichever bot has actually been working, conditioned on today's regime where
there's enough sample to trust that conditioning.

Cold start (no trade history yet) produces baseline_weights back, unchanged
— explicit, not an error, matching the rest of this codebase's convention.

A bot only gets tilted away from its baseline once it clears
MIN_SAMPLE_FOR_TILT closed trades; below that, its multiplier is exactly 1.0.
The tilt itself is a bounded z-score across bots (not an unbounded chase of
whichever bot got lucky), capped at +/-25% of baseline weight per run.
"""

import logging
import statistics
from datetime import datetime, timezone
from typing import Optional

import psycopg2.extras

logger = logging.getLogger(__name__)

BOT_KEYS = ("us", "eu", "aus", "china")
BOT_SOURCE = {"us": "US_BOT", "eu": "EU_BOT", "aus": "AUS_BOT", "china": "CHINA_BOT"}

MIN_SAMPLE_FOR_TILT = 15       # trades before a bot's own performance counts at all
MIN_SAMPLE_FOR_REGIME_TILT = 8  # trades in TODAY's regime before conditioning on it
LOOKBACK_TRADES = 60            # rolling window per bot (most recent N closed trades)
MAX_TILT = 0.25                 # +/-25% of baseline weight, per run


def _fetch_bot_trades(conn, bot_key: str, limit: int = LOOKBACK_TRADES) -> list:
    """
    Most recent closed trades for a bot, joined from nwt_trade_outcomes to
    nwt_portfolio_ledger via position_id (reliable since the learning_agent
    position_id fix — see db/migrate_2026_07_audit_fixes.sql history).
    Returns rows of (pnl_adjusted_or_pnl, primary_regime).
    """
    bot_source = BOT_SOURCE[bot_key]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(to_.pnl_adjusted, to_.pnl) AS pnl,
                   to_.regime_at_entry->>'primary_regime' AS primary_regime
            FROM nwt_trade_outcomes to_
            JOIN nwt_portfolio_ledger pl ON pl.position_id::text = to_.position_id
            WHERE pl.bot_source = %s AND to_.closed_at IS NOT NULL
              AND COALESCE(to_.pnl_adjusted, to_.pnl) IS NOT NULL
            ORDER BY to_.closed_at DESC
            LIMIT %s
            """,
            (bot_source, limit),
        )
        return cur.fetchall()


def _expectancy_and_sharpe(pnls: list) -> tuple:
    if not pnls:
        return None, None
    expectancy = statistics.mean(pnls)
    if len(pnls) < 2:
        return expectancy, None
    stdev = statistics.pstdev(pnls)
    sharpe_proxy = expectancy / stdev if stdev > 0 else None
    return expectancy, sharpe_proxy


def _bot_score(conn, bot_key: str, current_regime: str) -> dict:
    """One bot's performance snapshot: overall + regime-conditioned where trustworthy."""
    rows = _fetch_bot_trades(conn, bot_key)
    all_pnls = [float(r[0]) for r in rows]
    regime_pnls = [float(r[0]) for r in rows if r[1] == current_regime]

    overall_expectancy, overall_sharpe = _expectancy_and_sharpe(all_pnls)

    if len(regime_pnls) >= MIN_SAMPLE_FOR_REGIME_TILT:
        expectancy, sharpe = _expectancy_and_sharpe(regime_pnls)
        basis = "regime_conditioned"
        sample = len(regime_pnls)
    else:
        expectancy, sharpe = overall_expectancy, overall_sharpe
        basis = "overall"
        sample = len(all_pnls)

    return {
        "bot": bot_key,
        "sample": sample,
        "total_sample": len(all_pnls),
        "expectancy": expectancy,
        "sharpe_proxy": sharpe,
        "basis": basis,
    }


def compute_dynamic_weights(
    conn, regime: dict, baseline_weights: dict
) -> tuple[dict, list[str]]:
    """
    Returns (dynamic_weights, notes). dynamic_weights has the same keys as
    baseline_weights and sums to the same total — this redistributes share,
    it does not change how much total capital is deployed (that's
    compute_bot_permissions' job via kill switch / confidence / transition
    risk multipliers, applied afterward in strategist.py).
    """
    notes: list[str] = []
    current_regime = (regime or {}).get("primary_regime", "neutral")

    scores = {}
    for bot in BOT_KEYS:
        try:
            scores[bot] = _bot_score(conn, bot, current_regime)
        except Exception as exc:
            logger.warning("Allocator: score computation failed for %s: %s", bot, exc)
            scores[bot] = {"bot": bot, "sample": 0, "total_sample": 0,
                           "expectancy": None, "sharpe_proxy": None, "basis": "error"}

    tiltable = {b: s for b, s in scores.items()
                if s["total_sample"] >= MIN_SAMPLE_FOR_TILT and s["expectancy"] is not None}

    if len(tiltable) < 2:
        # Not enough bots with real history to compute a relative z-score —
        # cold start / early paper-trading window. Baseline, unchanged.
        notes.append(
            f"Allocator: only {len(tiltable)}/4 bots have >= {MIN_SAMPLE_FOR_TILT} trades "
            f"— using baseline weights unchanged"
        )
        _record_history(conn, scores, baseline_weights, baseline_weights, current_regime, notes)
        return dict(baseline_weights), notes

    expectancies = [s["expectancy"] for s in tiltable.values()]
    mean_exp = statistics.mean(expectancies)
    stdev_exp = statistics.pstdev(expectancies) if len(expectancies) > 1 else 0.0

    multipliers = {}
    for bot in BOT_KEYS:
        if bot not in tiltable or stdev_exp == 0:
            multipliers[bot] = 1.0
            continue
        z = (tiltable[bot]["expectancy"] - mean_exp) / stdev_exp
        z = max(-1.0, min(1.0, z))  # bounded z — one outlier bot can't dominate
        multipliers[bot] = 1.0 + MAX_TILT * z

    raw_weights = {bot: baseline_weights.get(bot, 0.0) * multipliers[bot] for bot in BOT_KEYS}
    total_baseline = sum(baseline_weights.get(bot, 0.0) for bot in BOT_KEYS)
    total_raw = sum(raw_weights.values())
    scale = (total_baseline / total_raw) if total_raw > 0 else 1.0
    dynamic_weights = {bot: round(raw_weights[bot] * scale, 4) for bot in BOT_KEYS}

    for bot in BOT_KEYS:
        if bot in tiltable:
            notes.append(
                f"Allocator: {bot} weight {baseline_weights.get(bot, 0):.4f} -> "
                f"{dynamic_weights[bot]:.4f} (basis={tiltable[bot]['basis']}, "
                f"n={tiltable[bot]['sample']}, expectancy={tiltable[bot]['expectancy']:.2f})"
            )

    _record_history(conn, scores, baseline_weights, dynamic_weights, current_regime, notes)
    return dynamic_weights, notes


def _record_history(conn, scores: dict, baseline_weights: dict, dynamic_weights: dict,
                     current_regime: str, notes: list) -> None:
    try:
        with conn.cursor() as cur:
            for bot in BOT_KEYS:
                s = scores.get(bot, {})
                cur.execute(
                    """
                    INSERT INTO nwt_allocator_history
                        (bot, regime, baseline_weight, dynamic_weight, sample_trades,
                         rolling_expectancy, sharpe_proxy, note)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        bot, current_regime, baseline_weights.get(bot), dynamic_weights.get(bot),
                        s.get("sample"), s.get("expectancy"), s.get("sharpe_proxy"),
                        s.get("basis"),
                    ),
                )
        conn.commit()
    except Exception as exc:
        logger.warning("Allocator: failed to write nwt_allocator_history: %s", exc)
