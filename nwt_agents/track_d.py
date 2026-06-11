"""
nwt_agents/track_d.py
Track D — Aggressive Directional. D1-D12. Runs at 14:00 UTC.

Focuses on directional trades (long_call, long_put, bull_call_spread, bear_put_spread).
Higher entry threshold (0.55+ from genome). DTE 21-45 from genome.
Only accepts conviction_tickets with conviction_score >= 7.
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
    apply_vol_sizing,
    check_no_trade_mode,
    compute_final_sizing,
    get_db,
    get_strategy_genome,
    insert_ticket,
    load_conviction_tickets,
    load_master_directives,
    log_inactivity,
    log_system_event,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("track_d")

ACCOUNT_SIZE = 97_000.0
TRADE_PCT = 0.02

# Track D: directional strategies only
DIRECTIONAL_STRATEGIES = {"long_call", "long_put", "bull_call_spread", "bear_put_spread"}

# Track D minimum conviction score
TRACK_D_MIN_CONVICTION = 7


def regime_matches(genome_regime: str, current_regime: dict) -> bool:
    primary = current_regime.get("primary_regime", "").lower()
    secondary = (current_regime.get("secondary_regime") or "").lower()
    genome_r = (genome_regime or "").lower()
    if genome_r in ("any", "", "all"):
        return True
    return genome_r == primary or genome_r == secondary


def find_best_directional_ticket(
    conviction_tickets: list,
    genome: dict,
    current_regime: dict,
) -> dict | None:
    """
    Return the highest-conviction directional ticket matching genome criteria.
    Track D enforces: conviction_score >= 7, directional strategies only.
    """
    asset_universe = set(genome.get("asset_universe") or [])
    entry_threshold = max(float(genome.get("entry_threshold", 0.55)), TRACK_D_MIN_CONVICTION / 10.0)

    candidates = [
        t for t in conviction_tickets
        if t.get("symbol") in asset_universe
        and regime_matches(genome.get("regime"), current_regime)
        and float(t.get("conviction_score", 0)) / 10.0 >= entry_threshold
        and t.get("strategy_type") in DIRECTIONAL_STRATEGIES
        and t.get("conviction_score", 0) >= TRACK_D_MIN_CONVICTION
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

        halted, halt_reason = check_no_trade_mode(conn)
        if halted:
            logger.warning("no_trade_mode SET — Track D exiting: %s", halt_reason)
            regime = directives.get("regime", {})
            for i in range(1, 13):
                log_inactivity(conn, f"D{i}", "D", "NO_TRADE_MODE", regime)
            log_system_event(conn, "WARNING", "track_d", f"no_trade_mode — all D strategies inactive: {halt_reason}")
            return

        if directives.get("global_kill_switch", False):
            logger.warning("Global kill switch active — Track D exiting")
            regime = directives.get("regime", {})
            for i in range(1, 13):
                log_inactivity(conn, f"D{i}", "D", "GLOBAL_KILL_SWITCH", regime)
            log_system_event(conn, "WARNING", "track_d", "Kill switch active — all D strategies inactive")
            return

        regime = directives.get("regime", {})
        conviction_tickets = load_conviction_tickets()

        proposals_submitted = 0

        # Pass 1 — each strategy finds its best conviction match
        candidates = []  # (strategy_id, genome, best_ticket)
        for i in range(1, 13):
            strategy_id = f"D{i}"

            try:
                genome = get_strategy_genome(conn, strategy_id)
            except RuntimeError as exc:
                logger.error("%s genome missing: %s", strategy_id, exc)
                log_system_event(conn, "ERROR", "track_d", str(exc))
                continue

            if genome.get("shadow_mode", False):
                logger.info("%s in shadow_mode — skipping live proposal", strategy_id)
                log_inactivity(conn, strategy_id, "D", "SHADOW_MODE", regime)
                continue

            best_ticket = find_best_directional_ticket(conviction_tickets, genome, regime)

            if best_ticket is None:
                # Distinguish reasons
                asset_universe = set(genome.get("asset_universe") or [])
                symbol_match = [t for t in conviction_tickets if t.get("symbol") in asset_universe]
                if not symbol_match:
                    reason = "NO_SYMBOL_MATCH"
                elif not [t for t in symbol_match if t.get("conviction_score", 0) >= TRACK_D_MIN_CONVICTION]:
                    reason = "CONVICTION_BELOW_THRESHOLD"
                elif not [t for t in symbol_match if t.get("strategy_type") in DIRECTIONAL_STRATEGIES]:
                    reason = "NO_DIRECTIONAL_STRATEGY_AVAILABLE"
                else:
                    reason = "NO_CONVICTION_MATCH"
                logger.info("%s: %s", strategy_id, reason)
                log_inactivity(conn, strategy_id, "D", reason, regime)
                continue

            candidates.append((strategy_id, genome, best_ticket))

        # Pass 2 — consolidate: at most ONE proposal per archetype per day
        # (same rationale as Track C — pool thin samples, drop correlated dupes)
        winners = {}  # archetype -> (strategy_id, genome, ticket)
        for strategy_id, genome, ticket in candidates:
            arch = genome.get("archetype") or strategy_id
            incumbent = winners.get(arch)
            if incumbent is None or float(ticket.get("conviction_score", 0)) > float(
                incumbent[2].get("conviction_score", 0)
            ):
                winners[arch] = (strategy_id, genome, ticket)

        for strategy_id, genome, _ in candidates:
            arch = genome.get("archetype") or strategy_id
            if winners[arch][0] != strategy_id:
                logger.info("%s: consolidated into archetype %s (lower conviction)", strategy_id, arch)
                log_inactivity(conn, strategy_id, "D", "ARCHETYPE_CONSOLIDATED", regime)

        for archetype, (strategy_id, genome, best_ticket) in winners.items():
            base_notional = ACCOUNT_SIZE * TRADE_PCT
            sized_notional = compute_final_sizing(directives, base_notional, "us")

            if sized_notional <= 0:
                logger.info("%s: sized_notional=0 — bot paused", strategy_id)
                log_inactivity(conn, strategy_id, "D", "ZERO_SIZING", regime)
                continue

            symbol = best_ticket["symbol"]
            strategy_type = best_ticket.get("strategy_type", "long_call")

            # Vol-regime gate (real IV pipeline) — only throttles premium
            # selling; Track D's debit structures pass through at 1.0x
            sized_notional, vol_gate = apply_vol_sizing(strategy_type, symbol, sized_notional)
            if sized_notional <= 0:
                logger.info("%s: premium selling halted — vol_regime=%s",
                            strategy_id, vol_gate["vol_regime"])
                log_inactivity(conn, strategy_id, "D", "REGIME_MISMATCH", regime)
                continue

            sq = best_ticket.get("signal_quality", {})

            # Track D: prefer longer DTE (21-45 from genome)
            dte_target = best_ticket.get("dte_target", genome.get("dte_max", 45))
            # Clamp to genome range
            dte_target = max(genome["dte_min"], min(genome["dte_max"], dte_target))

            proposal = {
                "from_track": "D",
                "strategy_id": strategy_id,
                "archetype": archetype,
                "symbol": symbol,
                "strategy_type": strategy_type,
                "vol_gate": vol_gate,
                "direction": best_ticket.get("direction", "long"),
                "confidence": best_ticket.get("confidence", 0.0),
                "conviction_score": best_ticket.get("conviction_score", 0),
                "sized_notional": round(sized_notional, 2),
                "dte_target": dte_target,
                "dte_min": genome["dte_min"],
                "dte_max": genome["dte_max"],
                "strike_preference": best_ticket.get("strike_preference", "ATM"),
                "signal_quality": sq,
                "stop_loss_pct": float(genome["stop_loss_pct"]),
                "profit_target_pct": float(genome["profit_target_pct"]),
                "iv_filter_max": float(genome["iv_filter_max"]),
                "regime_at_decision": regime,  # full JSONB object — not a string
                "iv_at_conviction": best_ticket.get("iv_at_conviction", 0.0),
                "entry_rationale": best_ticket.get("entry_rationale", ""),
                "conviction_ticket_id": best_ticket.get("ticket_id"),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            try:
                ticket_id = insert_ticket(
                    conn,
                    from_agent="TRACK_D",
                    to_agent="RISK_AGENT",
                    type_="TRADE_PROPOSAL",
                    payload=proposal,
                )
                logger.info("%s: submitted proposal ticket %s for %s", strategy_id, ticket_id, symbol)
                proposals_submitted += 1
            except Exception as exc:
                logger.error("%s: failed to insert ticket: %s", strategy_id, exc)
                log_system_event(conn, "ERROR", "track_d", f"{strategy_id} ticket insert failed: {exc}")

        log_system_event(
            conn,
            "INFO",
            "track_d",
            f"Track D complete: {proposals_submitted} proposals submitted",
            {"proposals_submitted": proposals_submitted},
        )
        logger.info("Track D done — %d proposals submitted", proposals_submitted)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
