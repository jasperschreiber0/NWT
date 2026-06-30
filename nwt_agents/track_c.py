"""
nwt_agents/track_c.py
Track C — Premium Seller. C1-C12. Runs at 14:00 UTC.

For each strategy C1-C12:
  1. Query genome from nwt_strategy_genome (NEVER hardcode parameters)
  2. Filter conviction tickets for genome asset_universe + regime match
  3. If match: build trade proposal and INSERT ticket to RISK_AGENT
  4. If no match: log_inactivity
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
    get_db,
    get_strategy_genome,
    insert_ticket,
    kill_switch_is_active,
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
logger = logging.getLogger("track_c")

ACCOUNT_SIZE = 97_000.0  # ~$97k equity
TRADE_PCT = 0.02         # 2% per trade


def regime_matches(genome_regime: str, current_regime: dict) -> bool:
    """Check if genome's regime string matches the current primary or secondary regime."""
    primary = current_regime.get("primary_regime", "").lower()
    secondary = (current_regime.get("secondary_regime") or "").lower()
    genome_r = (genome_regime or "").lower()
    if genome_r in ("any", "", "all"):
        return True
    return genome_r == primary or genome_r == secondary


def find_best_ticket(conviction_tickets: list, genome: dict, current_regime: dict) -> dict | None:
    """Return the highest-conviction ticket matching this genome's asset_universe + regime."""
    asset_universe = set(genome.get("asset_universe") or [])
    entry_threshold = float(genome.get("entry_threshold", 0.5))

    candidates = [
        t for t in conviction_tickets
        if t.get("symbol") in asset_universe
        and regime_matches(genome.get("regime"), current_regime)
        and float(t.get("conviction_score", 0)) / 10.0 >= entry_threshold
    ]

    if not candidates:
        return None
    # Return highest conviction score
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
            logger.warning("no_trade_mode SET — Track C exiting: %s", halt_reason)
            regime = directives.get("regime", {})
            for i in range(1, 13):
                log_inactivity(conn, f"C{i}", "C", "NO_TRADE_MODE", regime)
            log_system_event(conn, "WARNING", "track_c", f"no_trade_mode — all C strategies inactive: {halt_reason}")
            return

        if kill_switch_is_active(directives):
            logger.warning("Global kill switch active — Track C exiting without proposals")
            regime = directives.get("regime", {})
            for i in range(1, 13):
                log_inactivity(conn, f"C{i}", "C", "GLOBAL_KILL_SWITCH", regime)
            log_system_event(conn, "WARNING", "track_c", "Kill switch active — all C strategies inactive")
            return

        regime = directives.get("regime", {})
        conviction_tickets = load_conviction_tickets()

        proposals_submitted = 0

        # Pass 1 — each strategy finds its best conviction match
        candidates = []  # (strategy_id, genome, best_ticket)
        for i in range(1, 13):
            strategy_id = f"C{i}"

            try:
                genome = get_strategy_genome(conn, strategy_id)
            except RuntimeError as exc:
                logger.error("%s genome missing: %s", strategy_id, exc)
                log_system_event(conn, "ERROR", "track_c", str(exc))
                continue

            # Skip shadow mode strategies (they don't generate live tickets)
            if genome.get("shadow_mode", False):
                logger.info("%s in shadow_mode — skipping live proposal", strategy_id)
                log_inactivity(conn, strategy_id, "C", "SHADOW_MODE", regime)
                continue

            best_ticket = find_best_ticket(conviction_tickets, genome, regime)

            if best_ticket is None:
                reason = "NO_CONVICTION_MATCH"
                logger.info("%s: %s", strategy_id, reason)
                log_inactivity(conn, strategy_id, "C", reason, regime)
                continue

            candidates.append((strategy_id, genome, best_ticket))

        # Pass 2 — consolidate: at most ONE proposal per archetype per day.
        # 12 strategy_ids cannot accumulate meaningful samples in 60 days, and
        # multiple same-archetype proposals are correlated duplicates, not signal.
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
                log_inactivity(conn, strategy_id, "C", "ARCHETYPE_CONSOLIDATED", regime)

        for archetype, (strategy_id, genome, best_ticket) in winners.items():
            # Compute sizing (2% of account)
            base_notional = ACCOUNT_SIZE * TRADE_PCT
            sized_notional = compute_final_sizing(directives, base_notional, "us")

            if sized_notional <= 0:
                logger.info("%s: sized_notional=0 — us bot paused or weight=0", strategy_id)
                log_inactivity(conn, strategy_id, "C", "ZERO_SIZING", regime)
                continue

            symbol = best_ticket["symbol"]
            sq = best_ticket.get("signal_quality", {})

            proposal = {
                "from_track": "C",
                "strategy_id": strategy_id,
                "archetype": archetype,
                "symbol": symbol,
                "strategy_type": best_ticket.get("strategy_type", "iron_condor"),
                "direction": best_ticket.get("direction", "long"),
                "confidence": best_ticket.get("confidence", 0.0),
                "conviction_score": best_ticket.get("conviction_score", 0),
                "sized_notional": round(sized_notional, 2),
                "dte_target": best_ticket.get("dte_target", genome.get("dte_min", 14)),
                "dte_min": genome["dte_min"],
                "dte_max": genome["dte_max"],
                "strike_preference": best_ticket.get("strike_preference", "1_OTM"),
                "signal_quality": sq,
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
                    from_agent="TRACK_C",
                    to_agent="RISK_AGENT",
                    type_="TRADE_PROPOSAL",
                    payload=proposal,
                )
                logger.info("%s: submitted proposal ticket %s for %s", strategy_id, ticket_id, symbol)
                proposals_submitted += 1
            except Exception as exc:
                logger.error("%s: failed to insert ticket: %s", strategy_id, exc)
                log_system_event(conn, "ERROR", "track_c", f"{strategy_id} ticket insert failed: {exc}")

        log_system_event(
            conn,
            "INFO",
            "track_c",
            f"Track C complete: {proposals_submitted} proposals submitted",
            {"proposals_submitted": proposals_submitted},
        )
        logger.info("Track C done — %d proposals submitted", proposals_submitted)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
