"""
nwt_agents/track_e.py
Track E — Vol Desk / Stat-Arb. E1-E12. Runs at 14:30 UTC.

CRITICAL: Every proposed trade MUST include quantitative_edge field.
Missing quantitative_edge OR edge_magnitude < 0.3 → log_inactivity, skip.

Quantitative edge is computed from layer0_data:
  - IV skew (put IV - call IV → spy_iv_skew)
  - Vol spread (30d IV vs realized vol approximation)
  - Statistical confidence
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import integrity_gate
from shared_context import (
    check_no_trade_mode,
    compute_final_sizing,
    evaluate_shadow_mutation,
    get_active_strategy_ids,
    get_db,
    get_strategy_genome,
    insert_ticket,
    load_conviction_tickets,
    load_layer0_data,
    load_master_directives,
    log_decision_input,
    log_inactivity,
    log_system_event,
    regime_matches,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("track_e")

ACCOUNT_SIZE = 97_000.0
TRADE_PCT = 0.02

# Track E entry threshold (higher than C/D)
TRACK_E_MIN_CONVICTION_NORMALIZED = 0.60  # 6/10
MIN_QUANT_EDGE_MAGNITUDE = 0.30


def compute_quantitative_edge(layer0: dict, symbol: str, conviction_ticket: dict) -> dict:
    """
    Compute quantitative edge from layer0 data for a given symbol.
    Returns quantitative_edge dict with edge_type, edge_magnitude, edge_description, statistical_confidence.
    """
    spy_iv_skew = layer0.get("spy_iv_skew", 0.0)
    symbols_data = layer0.get("symbols", {})
    sym_data = symbols_data.get(symbol, {})
    iv = sym_data.get("iv", 0.0)
    vix = layer0.get("vix", 0.0)
    strategy_type = conviction_ticket.get("strategy_type", "")

    # IV=0 means Alpaca has no data for this symbol (only SPY/QQQ have IV).
    # Proceeding would amplify SPY skew by 100x via max(iv, 0.01) — reject early.
    if iv <= 0:
        return {
            "edge_type": "insufficient_data",
            "edge_magnitude": 0.0,
            "edge_description": f"IV data unavailable for {symbol} — cannot compute edge",
            "statistical_confidence": 0.0,
        }

    # Edge type 1: IV skew arb (put skew vs call skew)
    if abs(spy_iv_skew) > 0.02:
        skew_pct = abs(spy_iv_skew) / max(iv, 0.01)
        edge_magnitude = min(skew_pct * 2, 1.0)
        direction = "elevated put skew" if spy_iv_skew > 0 else "elevated call skew"
        return {
            "edge_type": "iv_skew",
            "edge_magnitude": round(edge_magnitude, 3),
            "edge_description": (
                f"SPY {direction} at {spy_iv_skew:.4f} — "
                f"{'selling premium is historically advantaged' if spy_iv_skew > 0 else 'call buying advantaged'}"
            ),
            "statistical_confidence": min(0.5 + edge_magnitude * 0.4, 0.95),
        }

    # Edge type 2: Vol spread — IV vs approximated realized vol
    # Approximate realized vol from ATR
    atr = sym_data.get("atr_14", 0.0)
    price = sym_data.get("price", 1.0)
    if price > 0 and atr > 0:
        realized_vol_proxy = (atr / price) * (252 ** 0.5)  # Annualized
        vol_spread = iv - realized_vol_proxy
        if abs(vol_spread) > 0.05:
            edge_magnitude = min(abs(vol_spread) / 0.20, 1.0)
            direction_desc = "IV elevated above realized" if vol_spread > 0 else "IV compressed below realized"
            return {
                "edge_type": "vol_spread",
                "edge_magnitude": round(edge_magnitude, 3),
                "edge_description": (
                    f"{symbol}: {direction_desc} — "
                    f"IV={iv:.3f}, realized_proxy={realized_vol_proxy:.3f}, spread={vol_spread:.3f}"
                ),
                "statistical_confidence": min(0.45 + edge_magnitude * 0.35, 0.90),
            }

    # Edge type 3: VIX regime edge (VIX above/below threshold)
    if vix > 0 and strategy_type in ("vix_calls", "long_put", "bear_put_spread"):
        if vix > 25:
            edge_magnitude = min((vix - 20) / 20.0, 1.0)
            return {
                "edge_type": "vol_spread",
                "edge_magnitude": round(edge_magnitude, 3),
                "edge_description": (
                    f"VIX={vix:.1f} — elevated volatility environment supports {strategy_type}"
                ),
                "statistical_confidence": 0.55 + min(edge_magnitude * 0.3, 0.35),
            }

    # No significant edge found
    return {
        "edge_type": "stat_arb",
        "edge_magnitude": 0.0,
        "edge_description": "No statistically significant quantitative edge detected",
        "statistical_confidence": 0.0,
    }


def find_best_ticket(
    conviction_tickets: list,
    genome: dict,
    current_regime: dict,
) -> dict | None:
    """Return highest-conviction ticket matching genome for Track E."""
    asset_universe = set(genome.get("asset_universe") or [])
    entry_threshold = float(genome.get("entry_threshold", TRACK_E_MIN_CONVICTION_NORMALIZED))

    candidates = [
        t for t in conviction_tickets
        if t.get("symbol") in asset_universe
        and regime_matches(genome.get("regime"), current_regime)
        and float(t.get("conviction_score", 0)) / 10.0 >= entry_threshold
    ]

    if not candidates:
        return None
    return max(candidates, key=lambda t: t.get("conviction_score", 0))


def main() -> None:
    integrity_gate.run_integrity_gate()
    conn = get_db()

    try:
        try:
            directives = load_master_directives()
        except FileNotFoundError:
            logger.error("master-directives.json not found — exiting")
            sys.exit(1)

        active_strategy_ids = get_active_strategy_ids(conn, "E")

        halted, halt_reason = check_no_trade_mode(conn)
        if halted:
            logger.warning("no_trade_mode SET — Track E exiting: %s", halt_reason)
            regime = directives.get("regime", {})
            for strategy_id in active_strategy_ids:
                log_inactivity(conn, strategy_id, "E", "NO_TRADE_MODE", regime)
            log_system_event(conn, "WARNING", "track_e", f"no_trade_mode — all E strategies inactive: {halt_reason}")
            return

        if directives.get("global_kill_switch", False):
            logger.warning("Global kill switch active — Track E exiting")
            regime = directives.get("regime", {})
            for strategy_id in active_strategy_ids:
                log_inactivity(conn, strategy_id, "E", "GLOBAL_KILL_SWITCH", regime)
            log_system_event(conn, "WARNING", "track_e", "Kill switch active — all E strategies inactive")
            return

        regime = directives.get("regime", {})
        conviction_tickets = load_conviction_tickets()
        layer0 = load_layer0_data()
        run_date = datetime.now(timezone.utc).date()

        proposals_submitted = 0

        for strategy_id in active_strategy_ids:

            try:
                genome = get_strategy_genome(conn, strategy_id)
            except RuntimeError as exc:
                logger.error("%s genome missing: %s", strategy_id, exc)
                log_system_event(conn, "ERROR", "track_e", str(exc))
                continue

            if genome.get("shadow_mode", False):
                logger.info("%s in shadow_mode — skipping live proposal", strategy_id)
                log_inactivity(conn, strategy_id, "E", "SHADOW_MODE", regime)
                continue

            evaluate_shadow_mutation(conn, strategy_id, "E", find_best_ticket,
                                     conviction_tickets, regime, layer0, run_date)

            best_ticket = find_best_ticket(conviction_tickets, genome, regime)

            if best_ticket is None:
                logger.info("%s: NO_CONVICTION_MATCH", strategy_id)
                log_inactivity(conn, strategy_id, "E", "NO_CONVICTION_MATCH", regime)
                continue

            symbol = best_ticket["symbol"]

            # CRITICAL: Compute quantitative_edge BEFORE building proposal
            quant_edge = compute_quantitative_edge(layer0, symbol, best_ticket)

            # Enforce: missing or insufficient edge → reject
            if quant_edge["edge_magnitude"] < MIN_QUANT_EDGE_MAGNITUDE:
                reason = "INSUFFICIENT_QUANT_EDGE"
                logger.info(
                    "%s: %s (symbol=%s, edge_magnitude=%.3f)",
                    strategy_id, reason, symbol, quant_edge["edge_magnitude"],
                )
                log_inactivity(conn, strategy_id, "E", reason, regime)
                continue

            base_notional = ACCOUNT_SIZE * TRADE_PCT
            sized_notional = compute_final_sizing(directives, base_notional, "us")

            dte_target = best_ticket.get("dte_target", genome.get("dte_min", 14))
            dte_target = max(genome["dte_min"], min(genome["dte_max"], dte_target))
            entry_price_ref = layer0.get("symbols", {}).get(symbol, {}).get("price") or None
            shadow_fields = {
                "direction": best_ticket.get("direction", "long"),
                "entry_price_ref": entry_price_ref,
                "target_pct": float(genome["profit_target_pct"]),
                "stop_pct": -abs(float(genome["stop_loss_pct"])),
                "dte_target": dte_target,
            }

            if sized_notional <= 0:
                logger.info("%s: sized_notional=0", strategy_id)
                log_inactivity(conn, strategy_id, "E", "ZERO_SIZING", regime)
                log_decision_input(
                    conn, run_date=run_date, symbol=symbol, strategy_id=strategy_id,
                    track="E", regime=regime, conviction_score=best_ticket.get("conviction_score", 0),
                    archetype=genome.get("archetype") or strategy_id, is_winner=True,
                    decision="REJECTED_TRACK", rejection_reason="ZERO_SIZING", **shadow_fields,
                )
                continue

            sq = best_ticket.get("signal_quality", {})

            proposal = {
                "from_track": "E",
                "strategy_id": strategy_id,
                "symbol": symbol,
                "strategy_type": best_ticket.get("strategy_type", "iron_condor"),
                "direction": best_ticket.get("direction", "long"),
                "confidence": best_ticket.get("confidence", 0.0),
                "conviction_score": best_ticket.get("conviction_score", 0),
                "sized_notional": round(sized_notional, 2),
                "dte_target": dte_target,
                "dte_min": genome["dte_min"],
                "dte_max": genome["dte_max"],
                "strike_preference": best_ticket.get("strike_preference", "ATM"),
                "signal_quality": sq,
                "quantitative_edge": quant_edge,  # MANDATORY for Track E
                "stop_loss_pct": float(genome["stop_loss_pct"]),
                "profit_target_pct": float(genome["profit_target_pct"]),
                "iv_filter_max": float(genome["iv_filter_max"]),
                "regime_at_decision": regime,  # full JSONB object
                "iv_at_conviction": best_ticket.get("iv_at_conviction", 0.0),
                "entry_rationale": best_ticket.get("entry_rationale", ""),
                "conviction_ticket_id": best_ticket.get("ticket_id"),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            try:
                ticket_id = insert_ticket(
                    conn,
                    from_agent="TRACK_E",
                    to_agent="RISK_AGENT",
                    type_="TRADE_PROPOSAL",
                    payload=proposal,
                )
                logger.info(
                    "%s: submitted proposal ticket %s for %s (edge=%.3f)",
                    strategy_id, ticket_id, symbol, quant_edge["edge_magnitude"],
                )
                proposals_submitted += 1
                log_decision_input(
                    conn, run_date=run_date, symbol=symbol, strategy_id=strategy_id,
                    track="E", regime=regime, conviction_score=best_ticket.get("conviction_score", 0),
                    archetype=genome.get("archetype") or strategy_id, is_winner=True,
                    decision="TRADE_PROPOSED", ticket_id=ticket_id, **shadow_fields,
                )
            except Exception as exc:
                logger.error("%s: failed to insert ticket: %s", strategy_id, exc)
                log_system_event(conn, "ERROR", "track_e", f"{strategy_id} ticket insert failed: {exc}")

        log_system_event(
            conn,
            "INFO",
            "track_e",
            f"Track E complete: {proposals_submitted} proposals submitted",
            {"proposals_submitted": proposals_submitted},
        )
        logger.info("Track E done — %d proposals submitted", proposals_submitted)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
