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

# pnl_adjusted haircut — paper fills land at mid, which is optimistic for
# options spreads. We assume a real marketable order gives up this fraction
# of the half-spread on EACH side (1.0 = fill at bid/ask, 0.0 = fill at mid).
SPREAD_HAIRCUT_FRACTION = float(os.environ.get("NWT_SPREAD_HAIRCUT_FRACTION", "0.75"))
# Conservative default total spread (as fraction of price) when NBBO was not
# captured — missing data must not silently produce un-haircut numbers.
DEFAULT_SPREAD_PCT = {
    "option": float(os.environ.get("NWT_DEFAULT_OPTION_SPREAD_PCT", "0.05")),
    "equity": float(os.environ.get("NWT_DEFAULT_EQUITY_SPREAD_PCT", "0.0005")),
}


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


def _half_spread(price: float, bid, ask, asset_type: str) -> tuple:
    """
    (half_spread, spread_pct) from captured NBBO, falling back to the
    conservative default when quotes are missing or crossed.
    """
    bid = float(bid) if bid is not None else 0.0
    ask = float(ask) if ask is not None else 0.0
    if ask > bid > 0:
        mid = (ask + bid) / 2.0
        spread_pct = (ask - bid) / mid if mid > 0 else 0.0
        return (ask - bid) / 2.0, spread_pct
    spread_pct = DEFAULT_SPREAD_PCT.get(asset_type, DEFAULT_SPREAD_PCT["option"])
    return price * spread_pct / 2.0, spread_pct


def compute_pnl_adjusted(
    entry_price: float,
    exit_price: float,
    direction: str,
    notional: float,
    entry_bid, entry_ask, exit_bid, exit_ask,
    asset_type: str,
) -> tuple:
    """
    Spread-haircut PnL: shift entry and exit against the trade by
    SPREAD_HAIRCUT_FRACTION × half-spread on each side.
    Returns (pnl_adjusted_dollars, pnl_adjusted_pct, entry_spread_pct, exit_spread_pct).
    """
    hs_entry, entry_spread_pct = _half_spread(entry_price, entry_bid, entry_ask, asset_type)
    hs_exit, exit_spread_pct = _half_spread(exit_price, exit_bid, exit_ask, asset_type)

    if direction == "long":
        adj_entry = entry_price + SPREAD_HAIRCUT_FRACTION * hs_entry  # buy worse (higher)
        adj_exit = exit_price - SPREAD_HAIRCUT_FRACTION * hs_exit     # sell worse (lower)
        adj_pct = (adj_exit - adj_entry) / entry_price if entry_price > 0 else 0.0
    else:
        adj_entry = entry_price - SPREAD_HAIRCUT_FRACTION * hs_entry  # sell worse (lower)
        adj_exit = exit_price + SPREAD_HAIRCUT_FRACTION * hs_exit     # buy back worse (higher)
        adj_pct = (adj_entry - adj_exit) / entry_price if entry_price > 0 else 0.0

    return adj_pct * notional, adj_pct, entry_spread_pct, exit_spread_pct


def get_archetype(conn, strategy_id: str) -> Optional[str]:
    """Archetype for a strategy_id from the genome (active or not)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT archetype FROM nwt_strategy_genome WHERE strategy_id = %s",
            (strategy_id,),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


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

def compute_strategy_decay(conn, key: str, key_column: str = "strategy_id") -> None:
    """
    Compute and INSERT nwt_strategy_decay for a strategy_id or, when
    key_column='archetype', pooled across the archetype bucket — the only
    granularity with enough samples to mean anything in the first window.
    Uses pnl_adjusted (spread-haircut) when available; raw pnl as fallback.
    """
    if key_column not in ("strategy_id", "archetype"):
        raise ValueError(f"Invalid decay key column: {key_column}")
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COALESCE(pnl_adjusted, pnl), closed_at FROM nwt_trade_outcomes "
            f"WHERE {key_column} = %s ORDER BY closed_at ASC",
            (key,),
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
                key,
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
            "%s %s DECAY FLAG SET: expectancy_delta=%.4f, wl_trend=%s",
            key_column, key, expectancy_delta, wl_trend,
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

            # Strategy ID — ledger column is authoritative (written at fill);
            # ticket fishing is the legacy fallback
            strategy_id = (
                pos.get("strategy_id")
                or original_payload.get("strategy_id")
                or bot_source
                or "UNKNOWN"
            )

            # Archetype — attribution pools here; genome is the source of truth
            archetype = get_archetype(conn, strategy_id) or original_payload.get("archetype") or strategy_id

            # pnl_adjusted — spread-haircut PnL from captured NBBO (or default
            # spread when quotes are missing). This is the number that matters.
            asset_type = pos.get("asset_type", "option")
            pnl_adjusted, pnl_adjusted_pct, entry_spread_pct, exit_spread_pct = compute_pnl_adjusted(
                entry_price,
                exit_price,
                direction,
                notional,
                pos.get("entry_bid"),
                pos.get("entry_ask"),
                pos.get("exit_bid"),
                pos.get("exit_ask"),
                asset_type,
            )

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
                            (strategy_id, archetype, symbol, direction,
                             entry_price, entry_time, exit_price, exit_time,
                             pnl, pnl_pct,
                             pnl_adjusted, pnl_adjusted_pct,
                             entry_spread_pct, exit_spread_pct,
                             iv_at_entry, iv_at_exit,
                             regime_at_entry, regime_at_exit,
                             dte_at_entry,
                             slippage, slippage_adjusted_efficiency,
                             entry_timing_score, exit_timing_score,
                             thesis_validity,
                             expected_move_capture, realized_move_capture,
                             closed_at)
                        VALUES
                            (%s, %s, %s, %s,
                             %s, %s, %s, %s,
                             %s, %s,
                             %s, %s,
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
                            archetype,
                            asset,
                            direction,
                            entry_price,
                            entry_time,
                            exit_price,
                            exit_time,
                            round(pnl_dollars, 4),
                            round(pnl_pct, 6),
                            round(pnl_adjusted, 4),
                            round(pnl_adjusted_pct, 6),
                            round(entry_spread_pct, 6),
                            round(exit_spread_pct, 6),
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
                    "Logged outcome for %s (%s/%s): pnl=%.4f pnl_adjusted=%.4f exit_timing=%.2f entry_timing=%.2f",
                    asset, strategy_id, archetype, pnl_dollars, pnl_adjusted, exit_timing_score, entry_timing_score,
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

        # Pooled decay per archetype — the granularity with real sample sizes
        archetypes_with_data = set()
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT archetype FROM nwt_trade_outcomes WHERE archetype IS NOT NULL")
            for row in cur.fetchall():
                archetypes_with_data.add(row[0])

        for arch in archetypes_with_data:
            try:
                compute_strategy_decay(conn, arch, key_column="archetype")
            except Exception as exc:
                logger.error("Archetype decay computation failed for %s: %s", arch, exc)

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
