"""
nwt_agents/mutation_agent.py
Learning Layer C — Strategy Mutator. The most dangerous component in the
system: most quant systems fail here. Shadow mode is mandatory before any
promotion; this file enforces that, it does not just document it.

Two modes, both required in the cron schedule:

  --propose  (daily, after decay computation — e.g. 21:10 UTC, after
              learning_agent.py's 21:00 run)
      For each active, non-shadow Track C/D strategy with a decay signal
      and >=30 trades observed, propose ONE bounded parameter mutation as a
      new, INACTIVE, shadow_mode genome version. Never touches the active
      row. At most one pending shadow candidate per strategy at a time.

  --promote  (nightly, after shadow_decision_evaluator.py has filled in
              outcomes for the day — e.g. 21:35 UTC)
      For each pending shadow candidate, check the Learning Gate:
        - 100+ trades in the shadow sample
        - spanning >=2 distinct volatility/regime buckets
        - improvement in >=1 of: win rate, average adjusted PnL
        - no material tail-risk degradation (worst-decile outcome check)
      Promote (flip active) if the gate passes; reject-and-retire if the
      gate's sample is complete but doesn't clear it; otherwise leave
      pending (still gathering data). Respects nwt_system_flags.mutation_frozen
      (Risk Agent authority to block promotion, never proposal).

Bounded mutation types implemented in this pass: entry_threshold (tighten),
iv_filter_max (tighten), stop_loss_pct (tighten). DTE-range and per-regime
frequency mutations are not yet implemented — see the docstring on
propose_mutation() for why they need slightly different plumbing.
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import (
    check_no_trade_mode,
    count_distinct_trades,
    get_db,
    insert_ticket,
    is_mutation_frozen,
    log_system_event,
    upsert_agent_state,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mutation_agent")

# Tracks eligible for mutation. Track E is excluded in v1 — every E strategy
# is currently pinned shadow_mode=TRUE at the track level (see
# migrate_2026_06_archetypes.sql), so mutating an already-fully-shadow track
# adds a second layer of shadow with no live baseline to compare against.
MUTABLE_TRACKS = ("C", "D")

MIN_TRADES_TO_OBSERVE = 30       # "enough to observe, not enough to mutate" (CLAUDE.md)
MUTATION_COOLDOWN_DAYS = 14
LEARNING_GATE_MIN_TRADES = 100
LEARNING_GATE_MIN_REGIMES = 2
RETIREMENT_MIN_TRADES = 30       # enough to call a shadow candidate clearly bad
RETIREMENT_MIN_AGE_DAYS = 30
RETIREMENT_PNL_THRESHOLD = -0.05  # avg shadow_pnl_pct worse than -5% -> retire


# ---------------------------------------------------------------------------
# Propose
# ---------------------------------------------------------------------------

def fetch_mutable_strategies(conn) -> list:
    """Active, non-shadow genome rows on Track C/D with no pending shadow candidate."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT g.* FROM nwt_strategy_genome g
            WHERE g.track = ANY(%s) AND g.active = TRUE AND g.shadow_mode = FALSE
              AND NOT EXISTS (
                  SELECT 1 FROM nwt_strategy_genome s
                  WHERE s.strategy_id = g.strategy_id AND s.shadow_mode = TRUE AND s.active = FALSE
              )
            ORDER BY g.strategy_id
            """,
            (list(MUTABLE_TRACKS),),
        )
        return [dict(r) for r in cur.fetchall()]


def fetch_latest_decay(conn, strategy_id: str) -> dict:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM nwt_strategy_decay WHERE strategy_id = %s ORDER BY computed_at DESC LIMIT 1",
            (strategy_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else {}


def count_trade_outcomes(conn, strategy_id: str) -> int:
    """
    Count of real trades (not raw nwt_trade_outcomes rows — a multi-leg
    spread writes one row per leg, so a raw COUNT(*) clears
    MIN_TRADES_TO_OBSERVE on far fewer actual trades than intended for
    spread-heavy strategies). See shared_context.count_distinct_trades.
    """
    return count_distinct_trades(conn, strategy_id=strategy_id)


def recently_proposed(conn, strategy_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*) FROM nwt_mutation_log
            WHERE strategy_id = %s AND action = 'proposed'
              AND created_at > NOW() - INTERVAL '{MUTATION_COOLDOWN_DAYS} days'
            """,
            (strategy_id,),
        )
        return cur.fetchone()[0] > 0


def propose_mutation(conn, genome: dict, decay: dict) -> bool:
    """
    Pick ONE bounded parameter to adjust based on the decay signal, insert a
    new shadow genome version, and log the proposal. Returns True if a
    mutation was proposed.

    DTE-range and per-regime-frequency mutations aren't implemented here:
    DTE range changes the option contracts execution_agent.py resolves, which
    needs a wider shadow-evaluation change than a pure parameter comparison
    (the "would have won" proxy already treats dte_target as a simple window
    check, so it's compatible in principle — deferred to keep this pass to
    the three parameters with the most direct, already-supported comparison).
    """
    strategy_id = genome["strategy_id"]
    wl_trend = decay.get("win_loss_ratio_trend")
    expectancy_delta = float(decay.get("expectancy_delta") or 0)

    if wl_trend == "compressing":
        param, old_value = "entry_threshold", float(genome["entry_threshold"])
        new_value = min(old_value + 0.05, 0.85)
        reasoning = f"win_loss_ratio_trend=compressing — tightening entry_threshold {old_value} -> {new_value}"
    elif expectancy_delta < -0.1 and genome.get("iv_filter_max") is not None:
        param, old_value = "iv_filter_max", float(genome["iv_filter_max"])
        new_value = max(old_value * 0.9, 0.20)
        reasoning = f"expectancy_delta={expectancy_delta:.4f} — tightening iv_filter_max {old_value} -> {new_value}"
    elif genome.get("stop_loss_pct") is not None:
        param, old_value = "stop_loss_pct", abs(float(genome["stop_loss_pct"]))
        new_value = max(old_value * 0.85, 0.05)
        reasoning = f"generic decay flag — tightening stop_loss_pct {old_value} -> {new_value}"
    else:
        return False

    if abs(new_value - old_value) < 1e-6:
        return False  # already at the bound, nothing to propose

    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(version) FROM nwt_strategy_genome WHERE strategy_id = %s",
            (strategy_id,),
        )
        parent_version = genome["version"]
        new_version = cur.fetchone()[0] + 1

        new_row = dict(genome)
        new_row[param] = new_value
        cur.execute(
            """
            INSERT INTO nwt_strategy_genome
                (strategy_id, track, archetype, asset_universe, dte_min, dte_max,
                 iv_filter_max, entry_threshold, stop_loss_pct, profit_target_pct,
                 regime, version, active, shadow_mode, parent_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, TRUE, %s)
            """,
            (
                strategy_id, new_row["track"], new_row.get("archetype"), new_row.get("asset_universe"),
                new_row["dte_min"], new_row["dte_max"], new_row.get("iv_filter_max"),
                new_row["entry_threshold"], new_row.get("stop_loss_pct"), new_row["profit_target_pct"],
                new_row.get("regime"), new_version, parent_version,
            ),
        )
        cur.execute(
            """
            INSERT INTO nwt_mutation_log
                (strategy_id, parent_version, new_version, action, parameter_changed,
                 old_value, new_value, reasoning, evidence)
            VALUES (%s, %s, %s, 'proposed', %s, %s, %s, %s, %s)
            """,
            (strategy_id, parent_version, new_version, param, old_value, new_value,
             reasoning, __import__("json").dumps(decay, default=str)),
        )
    conn.commit()
    logger.info("%s: proposed shadow mutation v%d (%s: %s -> %s)",
                strategy_id, new_version, param, old_value, new_value)
    return True


def run_propose(conn) -> int:
    proposed = 0
    for genome in fetch_mutable_strategies(conn):
        strategy_id = genome["strategy_id"]
        trade_count = count_trade_outcomes(conn, strategy_id)
        if trade_count < MIN_TRADES_TO_OBSERVE:
            continue
        if recently_proposed(conn, strategy_id):
            continue
        decay = fetch_latest_decay(conn, strategy_id)
        if not decay:
            continue
        if not (decay.get("decay_flag") or decay.get("win_loss_ratio_trend") == "compressing"):
            continue  # no signal worth mutating on — patience, not randomness
        if propose_mutation(conn, genome, decay):
            proposed += 1
    return proposed


# ---------------------------------------------------------------------------
# Promote
# ---------------------------------------------------------------------------

def fetch_pending_shadows(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM nwt_strategy_genome WHERE shadow_mode = TRUE AND active = FALSE ORDER BY strategy_id"
        )
        return [dict(r) for r in cur.fetchall()]


def _sample_stats(conn, strategy_id: str, genome_version) -> dict:
    """
    Aggregate would_have_won/shadow_pnl_pct for a (strategy_id, genome_version)
    bucket. genome_version=None selects the baseline (un-tagged) candidates.
    """
    with conn.cursor() as cur:
        if genome_version is None:
            cur.execute(
                """
                SELECT would_have_won, shadow_pnl_pct, regime->>'primary_regime'
                FROM nwt_decision_inputs
                WHERE strategy_id = %s AND genome_version IS NULL
                  AND shadow_evaluated_at IS NOT NULL AND shadow_pnl_pct IS NOT NULL
                """,
                (strategy_id,),
            )
        else:
            cur.execute(
                """
                SELECT would_have_won, shadow_pnl_pct, regime->>'primary_regime'
                FROM nwt_decision_inputs
                WHERE strategy_id = %s AND genome_version = %s
                  AND shadow_evaluated_at IS NOT NULL AND shadow_pnl_pct IS NOT NULL
                """,
                (strategy_id, genome_version),
            )
        rows = cur.fetchall()

    if not rows:
        return {"n": 0, "regimes": 0, "win_rate": None, "avg_pnl": None, "p10_pnl": None}

    wins = [bool(r[0]) for r in rows]
    pnls = sorted(float(r[1]) for r in rows)
    regimes = {r[2] for r in rows if r[2]}
    p10_idx = max(int(len(pnls) * 0.10) - 1, 0)
    return {
        "n": len(rows),
        "regimes": len(regimes),
        "win_rate": sum(wins) / len(wins),
        "avg_pnl": sum(pnls) / len(pnls),
        "p10_pnl": pnls[p10_idx],
    }


def evaluate_promotion(conn, shadow: dict) -> tuple:
    """
    Returns (action, reasoning, evidence) where action is one of:
    'promote', 'reject', 'wait'.
    """
    strategy_id = shadow["strategy_id"]
    version = shadow["version"]
    stats = _sample_stats(conn, strategy_id, version)
    baseline = _sample_stats(conn, strategy_id, None)
    evidence = {"shadow": stats, "baseline": baseline}

    if stats["n"] < LEARNING_GATE_MIN_TRADES or stats["regimes"] < LEARNING_GATE_MIN_REGIMES:
        # Not enough data yet — check the separate, much cheaper retirement bar
        age_days = (datetime.now(timezone.utc) - shadow["created_at"].replace(tzinfo=timezone.utc)).days
        if (stats["n"] >= RETIREMENT_MIN_TRADES and age_days >= RETIREMENT_MIN_AGE_DAYS
                and stats["avg_pnl"] is not None and stats["avg_pnl"] < RETIREMENT_PNL_THRESHOLD):
            return "reject", (
                f"Shadow v{version} clearly underperforming (avg_pnl={stats['avg_pnl']:.4f}) "
                f"after {age_days}d / {stats['n']} trades — retiring before the full gate sample"
            ), evidence
        return "wait", (
            f"Learning Gate not yet met: n={stats['n']}/{LEARNING_GATE_MIN_TRADES}, "
            f"regimes={stats['regimes']}/{LEARNING_GATE_MIN_REGIMES}"
        ), evidence

    if baseline["n"] == 0 or baseline["win_rate"] is None:
        return "wait", "Learning Gate met but no baseline sample to compare against yet", evidence

    win_rate_improved = stats["win_rate"] > baseline["win_rate"]
    pnl_improved = stats["avg_pnl"] > baseline["avg_pnl"]
    # No tail-risk degradation: worst-decile outcome must not be meaningfully
    # worse than baseline's (allow up to 20% relative slack for noise).
    tail_ok = stats["p10_pnl"] >= baseline["p10_pnl"] * 1.20 if baseline["p10_pnl"] < 0 else stats["p10_pnl"] >= baseline["p10_pnl"] * 0.80

    if (win_rate_improved or pnl_improved) and tail_ok:
        return "promote", (
            f"Learning Gate passed: n={stats['n']}, regimes={stats['regimes']}, "
            f"win_rate {baseline['win_rate']:.3f}->{stats['win_rate']:.3f}, "
            f"avg_pnl {baseline['avg_pnl']:.4f}->{stats['avg_pnl']:.4f}, tail_ok={tail_ok}"
        ), evidence

    return "reject", (
        f"Learning Gate sample complete but no qualifying improvement: "
        f"win_rate {baseline['win_rate']:.3f}->{stats['win_rate']:.3f}, "
        f"avg_pnl {baseline['avg_pnl']:.4f}->{stats['avg_pnl']:.4f}, tail_ok={tail_ok}"
    ), evidence


def promote(conn, shadow: dict, reasoning: str, evidence: dict) -> None:
    strategy_id, version = shadow["strategy_id"], shadow["version"]
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE nwt_strategy_genome SET active = FALSE WHERE strategy_id = %s AND active = TRUE",
            (strategy_id,),
        )
        cur.execute(
            "UPDATE nwt_strategy_genome SET active = TRUE, shadow_mode = FALSE "
            "WHERE strategy_id = %s AND version = %s",
            (strategy_id, version),
        )
        cur.execute(
            """
            INSERT INTO nwt_mutation_log (strategy_id, parent_version, new_version, action, reasoning, evidence)
            VALUES (%s, %s, %s, 'promoted', %s, %s)
            """,
            (strategy_id, shadow.get("parent_version"), version, reasoning, __import__("json").dumps(evidence, default=str)),
        )
    conn.commit()
    insert_ticket(conn, "MUTATION_AGENT", "SYSTEM", "strategy_mutation_promoted", {
        "strategy_id": strategy_id, "version": version, "reasoning": reasoning, "evidence": evidence,
    })
    logger.warning("PROMOTED %s v%d: %s", strategy_id, version, reasoning)


def reject_and_retire(conn, shadow: dict, reasoning: str, evidence: dict) -> None:
    strategy_id, version = shadow["strategy_id"], shadow["version"]
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE nwt_strategy_genome SET shadow_mode = FALSE WHERE strategy_id = %s AND version = %s",
            (strategy_id, version),
        )
        cur.execute(
            """
            INSERT INTO nwt_mutation_log (strategy_id, parent_version, new_version, action, reasoning, evidence)
            VALUES (%s, %s, %s, 'retired', %s, %s)
            """,
            (strategy_id, shadow.get("parent_version"), version, reasoning, __import__("json").dumps(evidence, default=str)),
        )
    conn.commit()
    logger.info("RETIRED %s v%d (never promoted): %s", strategy_id, version, reasoning)


def run_promote(conn) -> dict:
    if is_mutation_frozen(conn):
        logger.warning("mutation_frozen is set — skipping all promotion checks this run")
        return {"frozen": True, "promoted": 0, "rejected": 0, "waiting": 0}

    counts = {"frozen": False, "promoted": 0, "rejected": 0, "waiting": 0}
    for shadow in fetch_pending_shadows(conn):
        action, reasoning, evidence = evaluate_promotion(conn, shadow)
        if action == "promote":
            promote(conn, shadow, reasoning, evidence)
            counts["promoted"] += 1
        elif action == "reject":
            reject_and_retire(conn, shadow, reasoning, evidence)
            counts["rejected"] += 1
        else:
            logger.info("%s v%d: %s", shadow["strategy_id"], shadow["version"], reasoning)
            counts["waiting"] += 1
    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="NWT Strategy Mutator (Learning Layer C)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--propose", action="store_true")
    group.add_argument("--promote", action="store_true")
    args = parser.parse_args()

    conn = get_db()
    try:
        halted, halt_reason = check_no_trade_mode(conn)

        if args.propose:
            if halted:
                logger.warning("no_trade_mode SET — proposing mutations anyway (read-only analysis, no live effect): %s", halt_reason)
            proposed = run_propose(conn)
            log_system_event(conn, "INFO", "mutation_agent", f"Propose run: {proposed} new shadow mutation(s)",
                             {"proposed": proposed})
            upsert_agent_state(conn, "mutation_agent", "ok", {"last_mode": "propose", "proposed": proposed})
            logger.info("Propose done — %d new shadow mutation(s)", proposed)
        else:
            counts = run_promote(conn)
            log_system_event(conn, "INFO", "mutation_agent", f"Promote run: {counts}", counts)
            upsert_agent_state(conn, "mutation_agent", "ok", {"last_mode": "promote", **counts})
            logger.info("Promote done — %s", counts)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
