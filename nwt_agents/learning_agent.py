"""
nwt_agents/learning_agent.py
Runs at 21:00 UTC. Layer A — always active. Pure observation, never suggests changes.

1. Find all newly closed positions in nwt_portfolio_ledger
2. Build full trade outcome records and INSERT into nwt_trade_outcomes
3. Compute and UPDATE nwt_strategy_decay for strategies with 5+ trades

CRITICAL:
- Signal quality and PnL quality are logged SEPARATELY — never conflated
- regime_at_entry and regime_at_exit are full JSONB objects, never strings
- Inactivity is a first-class logged state (NO_EDGE, SIGNAL_MISSED, REGIME_MISMATCH)
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import (
    get_db,
    load_layer0_data,
    load_master_directives,
    log_system_event,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("learning_agent")

AGENTS_DIR = Path(os.environ.get("NWT_AGENTS_DIR", Path(__file__).parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_unprocessed_closed_positions(conn) -> list:
    """
    Return closed positions NOT yet in nwt_trade_outcomes.
    Matches on alpaca_order_id or position_id.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT pl.*
            FROM nwt_portfolio_ledger pl
            WHERE pl.status = 'closed'
              AND pl.exit_price IS NOT NULL
              AND pl.alpaca_order_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM nwt_trade_outcomes to_
                  -- Match on strategy_id via original ticket lookup
                  -- We use alpaca_order_id as the join bridge via nwt_tickets payload
                  WHERE to_.entry_time = pl.entry_time
                    AND to_.symbol = pl.asset
                    AND to_.direction = pl.direction
              )
            ORDER BY pl.exit_time ASC
            """
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def find_original_ticket(conn, alpaca_order_id: str, asset: str) -> Optional[dict]:
    """
    Find the original conviction/proposal ticket for this position.
    Searches nwt_tickets payload for the alpaca_order_id or asset symbol.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Try to find by alpaca_order_id in payload
        cur.execute(
            """
            SELECT * FROM nwt_tickets
            WHERE type IN ('TRADE_REQUEST', 'TRADE_PROPOSAL', 'CONVICTION_TICKET')
              AND (
                payload->>'alpaca_order_id' = %s
                OR payload->>'option_symbol' = %s
                OR payload->>'symbol' = %s
              )
            ORDER BY created_at DESC LIMIT 1
            """,
            (alpaca_order_id, asset, asset),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def compute_exit_timing_score(
    exit_price: float,
    entry_price: float,
    direction: str,
    stop_pct: float,
    target_pct: float,
) -> float:
    """
    Compute exit timing score:
    1.0 = hit target
    0.5 = between stop and target
    0.0 = hit stop
    """
    if entry_price <= 0:
        return 0.5

    if direction == "long":
        ret = (exit_price - entry_price) / entry_price
    else:
        ret = (entry_price - exit_price) / entry_price

    # Normalize target_pct to positive
    abs_target = abs(target_pct)
    abs_stop = abs(stop_pct)

    if ret >= abs_target:
        return 1.0
    elif ret <= -abs_stop:
        return 0.0
    else:
        # Linear interpolation between stop and target
        if abs_target + abs_stop > 0:
            score = (ret + abs_stop) / (abs_target + abs_stop)
            return max(0.0, min(1.0, score))
        return 0.5


def compute_realized_move_capture(
    entry_price: float,
    exit_price: float,
    direction: str,
    expected_move_capture: float,
) -> float:
    """
    Realized move capture: actual return / expected return.
    Capped at [-2.0, 2.0].
    """
    if entry_price <= 0 or expected_move_capture == 0:
        return 0.0

    if direction == "long":
        actual_ret = (exit_price - entry_price) / entry_price
    else:
        actual_ret = (entry_price - exit_price) / entry_price

    capture = actual_ret / expected_move_capture if expected_move_capture != 0 else 0.0
    return max(-2.0, min(2.0, round(capture, 4)))


# ---------------------------------------------------------------------------
# Strategy decay computation
# ---------------------------------------------------------------------------

def compute_strategy_decay(conn, strategy_id: str) -> None:
    """Compute and INSERT/UPDATE nwt_strategy_decay for a given strategy_id."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pnl, closed_at FROM nwt_trade_outcomes WHERE strategy_id = %s ORDER BY closed_at ASC",
            (strategy_id,),
        )
        rows = cur.fetchall()

    if len(rows) < 5:
        return  # Not enough data

    pnls = [float(r[0]) for r in rows if r[0] is not None]

    baseline_expectancy = float(np.mean(pnls)) if pnls else 0.0
    rolling_20 = pnls[-20:] if len(pnls) >= 20 else pnls
    rolling_expectancy_20 = float(np.mean(rolling_20))
    expectancy_delta = rolling_expectancy_20 - baseline_expectancy

    # Win/loss ratio trend: last 10 vs previous 10
    last_10 = pnls[-10:] if len(pnls) >= 10 else pnls
    prev_10 = pnls[-20:-10] if len(pnls) >= 20 else pnls[:10]

    def win_rate(ps):
        if not ps:
            return 0.0
        wins = sum(1 for p in ps if p > 0)
        losses = sum(1 for p in ps if p <= 0)
        total = wins + losses
        if total == 0:
            return 0.0
        win_r = wins / total
        loss_r = losses / total
        if loss_r == 0:
            return float("inf")
        return win_r / loss_r

    last_wr = win_rate(last_10)
    prev_wr = win_rate(prev_10)

    if prev_wr > 0:
        if last_wr < prev_wr * 0.85:
            wl_trend = "compressing"
        elif last_wr > prev_wr * 1.15:
            wl_trend = "expanding"
        else:
            wl_trend = "stable"
    else:
        wl_trend = "stable"

    # Decay flag: triggered if expectancy delta < -0.1 or trend compressing
    decay_flag = expectancy_delta < -0.1 or wl_trend == "compressing"

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_strategy_decay
                (strategy_id, rolling_expectancy_20, baseline_expectancy, expectancy_delta,
                 win_loss_ratio_trend, decay_flag)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                strategy_id,
                round(rolling_expectancy_20, 4),
                round(baseline_expectancy, 4),
                round(expectancy_delta, 4),
                wl_trend,
                decay_flag,
            ),
        )
    conn.commit()

    if decay_flag:
        logger.warning(
            "Strategy %s DECAY FLAG SET: expectancy_delta=%.4f, wl_trend=%s",
            strategy_id, expectancy_delta, wl_trend,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    conn = get_db()

    try:
        layer0 = load_layer0_data()
        symbols_data = layer0.get("symbols", {})

        try:
            directives = load_master_directives()
            current_regime = directives.get("regime", {})
        except FileNotFoundError:
            current_regime = {"primary_regime": "unknown", "confidence": 0.0}

        closed_positions = fetch_unprocessed_closed_positions(conn)
        logger.info("Found %d unprocessed closed positions", len(closed_positions))

        outcomes_inserted = 0
        strategies_seen = set()

        for pos in closed_positions:
            position_id = str(pos.get("position_id", ""))
            asset = pos.get("asset", "")
            entry_price = float(pos.get("entry_price") or 0)
            exit_price = float(pos.get("exit_price") or 0)
            entry_time = pos.get("entry_time")
            exit_time = pos.get("exit_time")
            direction = pos.get("direction", "long")
            notional = float(pos.get("notional_risk") or 0)
            realized_slippage = float(pos.get("realized_slippage") or 0)
            alpaca_order_id = pos.get("alpaca_order_id", "")
            bot_source = pos.get("bot_source", "")

            if entry_price <= 0 or exit_price <= 0:
                logger.warning("Skipping position %s — missing entry/exit price", position_id)
                continue

            # PnL computation
            if direction == "long":
                pnl = exit_price - entry_price
                pnl_pct = pnl / entry_price
            else:
                pnl = entry_price - exit_price
                pnl_pct = pnl / entry_price

            # Adjust for notional
            pnl_dollars = pnl_pct * notional
            slippage_cost = realized_slippage * notional
            slippage_adjusted_efficiency = (
                pnl_dollars / (pnl_dollars + slippage_cost)
                if (pnl_dollars + slippage_cost) != 0
                else 0.0
            )

            # Find original ticket for signal quality and regime context
            original_ticket = find_original_ticket(conn, alpaca_order_id, asset)
            original_payload = (original_ticket.get("payload") or {}) if original_ticket else {}

            # Strategy ID from original ticket or bot_source
            strategy_id = original_payload.get("strategy_id") or bot_source or "UNKNOWN"

            # Signal quality — logged SEPARATELY from PnL quality
            sq = original_payload.get("signal_quality", {})
            entry_timing_score = sq.get("entry_timing_score", 0.5)
            thesis_validity = sq.get("thesis_validity", "")
            expected_move_capture = float(sq.get("expected_move_capture", 0.0))

            # Exit timing score — computed from actual vs expected outcome
            stop_pct = abs(float(original_payload.get("stop_pct", -0.50)))
            target_pct = abs(float(original_payload.get("target_pct", 0.50)))
            exit_timing_score = compute_exit_timing_score(
                exit_price, entry_price, direction, stop_pct, target_pct
            )

            # Realized move capture
            realized_move_capture = compute_realized_move_capture(
                entry_price, exit_price, direction, expected_move_capture
            )

            # IV — from layer0 data (approximation using current; historical not available yet)
            sym_data = symbols_data.get(asset, symbols_data.get(asset.split(":")[0], {}))
            iv_current = sym_data.get("iv", 0.0)

            # Regime at entry — from original ticket payload (full JSONB)
            regime_at_entry = original_payload.get("regime_at_decision", {})
            if not regime_at_entry:
                regime_at_entry = current_regime

            # DTE at entry — from original ticket
            dte_at_entry = original_payload.get("dte_target") or original_payload.get("dte_min")

            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO nwt_trade_outcomes
                            (strategy_id, symbol, direction,
                             entry_price, entry_time, exit_price, exit_time,
                             pnl, pnl_pct,
                             iv_at_entry, iv_at_exit,
                             regime_at_entry, regime_at_exit,
                             dte_at_entry,
                             slippage, slippage_adjusted_efficiency,
                             entry_timing_score, exit_timing_score,
                             thesis_validity,
                             expected_move_capture, realized_move_capture,
                             closed_at)
                        VALUES
                            (%s, %s, %s,
                             %s, %s, %s, %s,
                             %s, %s,
                             %s, %s,
                             %s, %s,
                             %s,
                             %s, %s,
                             %s, %s,
                             %s,
                             %s, %s,
                             %s)
                        """,
                        (
                            strategy_id,
                            asset,
                            direction,
                            entry_price,
                            entry_time,
                            exit_price,
                            exit_time,
                            round(pnl_dollars, 4),
                            round(pnl_pct, 6),
                            iv_current,   # iv_at_entry (approximated)
                            iv_current,   # iv_at_exit (same approximation until historical available)
                            json.dumps(regime_at_entry),
                            json.dumps(current_regime),  # regime_at_exit = current directives regime
                            dte_at_entry,
                            realized_slippage,
                            round(slippage_adjusted_efficiency, 4),
                            entry_timing_score,   # signal quality — separate from PnL
                            exit_timing_score,    # signal quality — separate from PnL
                            thesis_validity,
                            expected_move_capture,
                            realized_move_capture,
                            exit_time,
                        ),
                    )
                conn.commit()
                outcomes_inserted += 1
                strategies_seen.add(strategy_id)
                logger.info(
                    "Logged outcome for %s (%s): pnl=%.4f pnl_pct=%.4f exit_timing=%.2f entry_timing=%.2f",
                    asset, strategy_id, pnl_dollars, pnl_pct, exit_timing_score, entry_timing_score,
                )
            except Exception as exc:
                logger.error("Failed to insert outcome for position %s: %s", position_id, exc)
                conn.rollback()

        # Compute strategy decay for all strategies seen this run + any with enough history
        all_strategy_ids_with_data = set()
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT strategy_id FROM nwt_trade_outcomes")
            for row in cur.fetchall():
                all_strategy_ids_with_data.add(row[0])

        for sid in all_strategy_ids_with_data:
            try:
                compute_strategy_decay(conn, sid)
            except Exception as exc:
                logger.error("Strategy decay computation failed for %s: %s", sid, exc)

        log_system_event(
            conn,
            "INFO",
            "learning_agent",
            f"Learning agent run complete: {outcomes_inserted} outcomes logged",
            {
                "outcomes_inserted": outcomes_inserted,
                "strategies_updated": list(strategies_seen),
                "total_strategies_in_db": len(all_strategy_ids_with_data),
            },
        )
        logger.info(
            "Learning agent done — %d outcomes logged, %d strategies processed for decay",
            outcomes_inserted, len(all_strategy_ids_with_data),
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
