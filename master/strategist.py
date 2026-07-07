"""
strategist.py — Portfolio Brain (master-strategist)
Fires: 21:30 UTC daily (after US close)

Role: Fund manager — reads ledger, classifies regime, allocates capital,
      outputs master-directives.json.
      NEVER generates trade ideas. NEVER reads Alpaca positions API directly.
      Single source of truth for regime and allocation is this script's output.

Usage:
    python3 master/strategist.py
"""

import json
import logging
import os
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap: load .env from master/ directory regardless of CWD
# ---------------------------------------------------------------------------

MASTER_DIR = Path(__file__).parent.resolve()
ENV_PATH = MASTER_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

# ---------------------------------------------------------------------------
# Logging setup (before any other imports that use logging)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("master-strategist")

# Local modules — import after .env is loaded
from market_internals import fetch_market_internals  # noqa: E402
from regime_classifier import classify_regime  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SHARED_DIR = MASTER_DIR.parent / "shared"
DIRECTIVES_PATH = SHARED_DIR / "master-directives.json"

# Baseline capital weights (from CLAUDE.md: ~$97k total)
BASELINE_WEIGHTS = {
    "us":    0.36,   # $35k / $97k
    "eu":    0.21,   # $20k / $97k
    "aus":   0.20,   # $20k (CLAUDE.md baseline)
    "china": 0.15,   # $15k / $97k
}

# Maximum single-bot weight (sanity cap)
MAX_WEIGHT = 0.65

# ---------------------------------------------------------------------------
# Integrity Gate
# ---------------------------------------------------------------------------

class IntegrityError(Exception):
    """Raised when any startup check fails — system must not trade."""


def _check_db(dsn: str) -> psycopg2.extensions.connection:
    """Verify DB connectivity. Returns open connection."""
    try:
        conn = psycopg2.connect(dsn)
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        logger.info("Integrity gate: DB connectivity OK")
        return conn
    except Exception as exc:
        raise IntegrityError(f"DB connectivity failed: {exc}") from exc


def _check_alpaca(base_url: str, api_key: str, secret_key: str) -> None:
    """Verify Alpaca API connectivity."""
    url = f"{base_url}/v2/account"
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        account = resp.json()
        equity = account.get("equity", "?")
        logger.info("Integrity gate: Alpaca OK — equity=$%s", equity)
    except Exception as exc:
        raise IntegrityError(f"Alpaca connectivity failed: {exc}") from exc


def run_integrity_checks(
    dsn: str,
    base_url: str,
    api_key: str,
    secret_key: str,
) -> psycopg2.extensions.connection:
    """
    Run all startup integrity checks.
    Returns DB connection on success.
    Raises IntegrityError on any failure — caller must not trade.
    """
    conn = _check_db(dsn)
    _check_alpaca(base_url, api_key, secret_key)
    logger.info("Integrity gate: all checks passed")
    return conn

# ---------------------------------------------------------------------------
# Portfolio Ledger Reader
# ---------------------------------------------------------------------------

def read_open_positions(conn: psycopg2.extensions.connection) -> list[dict]:
    """
    Read all open positions from nwt_portfolio_ledger.
    Returns list of dicts (column: value).
    NEVER reads Alpaca positions API.
    """
    query = """
        SELECT
            position_id,
            bot_source,
            asset,
            asset_type,
            direction,
            delta_exposure,
            notional_risk,
            entry_price,
            entry_time,
            status
        FROM nwt_portfolio_ledger
        WHERE status = 'open'
        ORDER BY entry_time DESC
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query)
            rows = cur.fetchall()
        positions = [dict(r) for r in rows]
        logger.info("Ledger: %d open position(s) found", len(positions))
        return positions
    except Exception as exc:
        logger.error("Failed to read nwt_portfolio_ledger: %s", exc)
        return []

# ---------------------------------------------------------------------------
# Net Delta / Vega estimation
# ---------------------------------------------------------------------------

def compute_exposure(positions: list[dict]) -> dict:
    """
    Compute portfolio-level net delta and net vega estimates.

    net_delta_estimate:
        sum(delta_exposure * notional_risk) / total_notional
        Normalised to [-1, 1]. Positive = net long, negative = net short.
        0.0 if no open positions (cold start is explicit, not an error).

    net_vega_estimate:
        For options positions only: 0.1 * notional_risk (simplified, no full Greeks).
        Normalised by total options notional.
        0.0 if no options positions.

    Returns dict: {net_delta_estimate, net_vega_estimate, total_notional,
                   options_notional, equity_count, options_count}
    """
    if not positions:
        logger.info("Exposure: no open positions — cold start, zero exposure assumed")
        return {
            "net_delta_estimate": 0.0,
            "net_vega_estimate": 0.0,
            "total_notional": 0.0,
            "options_notional": 0.0,
            "equity_count": 0,
            "options_count": 0,
        }

    weighted_delta_sum = 0.0
    total_notional = 0.0
    options_vega_sum = 0.0
    options_notional = 0.0
    equity_count = 0
    options_count = 0

    for pos in positions:
        notional = float(pos.get("notional_risk") or 0)
        delta = float(pos.get("delta_exposure") or 0)
        direction = (pos.get("direction") or "long").lower()
        asset_type = (pos.get("asset_type") or "equity").lower()

        if notional <= 0:
            continue

        # Direction sign: long = +1, short = -1
        dir_sign = 1.0 if direction == "long" else -1.0

        total_notional += notional
        weighted_delta_sum += dir_sign * delta * notional

        if asset_type == "option":
            options_count += 1
            options_notional += notional
            # Vega approximation: 0.1 * notional (simplified)
            options_vega_sum += 0.1 * notional
        else:
            equity_count += 1

    # Normalise delta to [-1, 1]
    if total_notional > 0:
        net_delta = max(-1.0, min(1.0, weighted_delta_sum / total_notional))
    else:
        net_delta = 0.0

    # Normalise vega (relative to options notional; cap at 1.0)
    if options_notional > 0:
        net_vega = min(1.0, options_vega_sum / options_notional)
    else:
        net_vega = 0.0

    logger.info(
        "Exposure: net_delta=%.4f net_vega=%.4f total_notional=%.0f "
        "options=%.0f equity_positions=%d options_positions=%d",
        net_delta,
        net_vega,
        total_notional,
        options_notional,
        equity_count,
        options_count,
    )

    return {
        "net_delta_estimate": round(net_delta, 4),
        "net_vega_estimate": round(net_vega, 4),
        "total_notional": round(total_notional, 2),
        "options_notional": round(options_notional, 2),
        "equity_count": equity_count,
        "options_count": options_count,
    }

# ---------------------------------------------------------------------------
# Drawdown computation
# ---------------------------------------------------------------------------

def compute_drawdown(conn: psycopg2.extensions.connection) -> float:
    """
    Compute current drawdown from nwt_equity_curve (real account equity,
    30-day rolling peak) — mirrors nwt_agents/risk_agent.py::get_current_drawdown().
    Falls back to nwt_trade_outcomes cumulative pnl_adjusted only if the
    equity curve has no rows yet (cold start).
    Returns 0.0 if no data at all. Drawdown is expressed as a fraction
    (e.g. 0.08 = 8%). Must NOT be computed as % of cumulative trade PnL —
    with few trades that denominator is tiny and produces false kill-switch
    triggers unrelated to real account risk.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT equity FROM nwt_equity_curve ORDER BY date DESC LIMIT 30")
            rows = cur.fetchall()
    except Exception as exc:
        logger.warning("Could not read nwt_equity_curve for drawdown: %s", exc)
        rows = []

    if rows:
        equities = [float(r[0]) for r in rows]
        equities.reverse()
        peak = max(equities)
        current = equities[-1]
        max_dd = max(0.0, (peak - current) / peak) if peak > 0 else 0.0
        logger.info(
            "Drawdown (equity curve): peak=%.2f current=%.2f max_drawdown=%.4f (%.2f%%)",
            peak, current, max_dd, max_dd * 100,
        )
        return round(max_dd, 6)

    # Cold start fallback — equity curve not populated yet
    query = """
        SELECT COALESCE(pnl_adjusted, pnl) AS pnl, closed_at
        FROM nwt_trade_outcomes
        WHERE closed_at IS NOT NULL AND COALESCE(pnl_adjusted, pnl) IS NOT NULL
        ORDER BY closed_at ASC
    """
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            trade_rows = cur.fetchall()
    except Exception as exc:
        logger.warning("Could not read nwt_trade_outcomes for drawdown: %s", exc)
        return 0.0

    if not trade_rows:
        logger.info("Drawdown: no equity curve and no closed trades yet — drawdown=0.0 (cold start)")
        return 0.0

    cum_pnl = 0.0
    peak = 0.0
    max_dd = 0.0

    for (pnl, _) in trade_rows:
        if pnl is None:
            continue
        cum_pnl += float(pnl)
        if cum_pnl > peak:
            peak = cum_pnl
        if peak > 0:
            dd = (peak - cum_pnl) / peak
            if dd > max_dd:
                max_dd = dd

    logger.info(
        "Drawdown (cold-start trade fallback): cumulative_pnl=%.2f peak=%.2f max_drawdown=%.4f (%.2f%%)",
        cum_pnl,
        peak,
        max_dd,
        max_dd * 100,
    )
    return round(max_dd, 6)

# ---------------------------------------------------------------------------
# Kill switch logic
# ---------------------------------------------------------------------------

def evaluate_kill_switch(vix: Optional[float], drawdown: float) -> tuple[bool, str]:
    """
    Returns (kill_switch_active, reason).
    Kill switch triggers: VIX > 40 OR drawdown > 8%.
    VIX=0 or None means missing data — do not trigger kill switch on missing data.
    """
    if vix is not None and vix > 40:
        reason = f"VIX={vix:.1f} exceeds threshold of 40"
        logger.warning("KILL SWITCH TRIGGERED: %s", reason)
        return True, reason

    if drawdown > 0.08:
        reason = f"Drawdown={drawdown*100:.2f}% exceeds threshold of 8%"
        logger.warning("KILL SWITCH TRIGGERED: %s", reason)
        return True, reason

    return False, ""

# ---------------------------------------------------------------------------
# Bot permissions
# ---------------------------------------------------------------------------

def compute_bot_permissions(
    regime: dict,
    exposure: dict,
    kill_switch: bool,
    vix: Optional[float],
) -> tuple[dict, list[str]]:
    """
    Compute per-bot permission objects based on regime + exposure + kill switch.

    Rules (in order of precedence):
    1. global_kill_switch=True → all bots paused, all size_caps=0
    2. regime.confidence < 0.5 → all capital_weights reduced 30%
    3. regime.transition_risk > 0.5 → all size_caps capped at 0.5
    4. China bot: paused if geopolitical_stress confidence > 0.7
    5. EU bot: reduced if inflation_concern
    6. AUS bot: reduced if regime is risk_off or recession_fear

    Returns (bot_permissions_dict, conflict_notes_list)
    """
    conflict_notes = []
    confidence = float(regime.get("confidence", 0.5))
    transition_risk = float(regime.get("transition_risk", 0.3))
    primary = regime.get("primary_regime", "neutral")

    # Start from baseline weights
    weights = dict(BASELINE_WEIGHTS)
    size_caps = {bot: 1.0 for bot in weights}
    statuses = {bot: "active" for bot in weights}

    # --- Rule 1: Kill switch ---
    if kill_switch:
        for bot in weights:
            weights[bot] = 0.0
            size_caps[bot] = 0.0
            statuses[bot] = "paused"
        conflict_notes.append("Global kill switch active — all bots paused")
        return _build_permissions(statuses, weights, size_caps), conflict_notes

    # --- Rule 2: Low confidence → reduce all weights 30% ---
    if confidence < 0.5:
        factor = 0.70
        conflict_notes.append(
            f"Regime confidence={confidence:.3f} < 0.5 — all weights reduced 30%"
        )
        for bot in weights:
            weights[bot] = round(weights[bot] * factor, 4)

    # --- Rule 3: High transition risk → cap all size_caps at 0.5 ---
    if transition_risk > 0.5:
        conflict_notes.append(
            f"Transition risk={transition_risk:.3f} > 0.5 — all size_caps capped at 0.50"
        )
        for bot in size_caps:
            size_caps[bot] = min(size_caps[bot], 0.5)

    # --- Regime-specific adjustments ---

    if primary == "risk_off":
        # US bot: equity bot doesn't benefit from risk-off as much
        weights["us"] = round(weights["us"] * 0.6, 4)
        size_caps["us"] = min(size_caps["us"], 0.6)
        statuses["us"] = "reduced"
        conflict_notes.append("risk_off: US bot sizing reduced")

        # AUS bot: reduce (trend may be broken)
        weights["aus"] = round(weights["aus"] * 0.5, 4)
        size_caps["aus"] = min(size_caps["aus"], 0.5)
        statuses["aus"] = "reduced"
        conflict_notes.append("risk_off: AUS bot reduced")

    elif primary == "recession_fear":
        weights["aus"] = round(weights["aus"] * 0.4, 4)
        size_caps["aus"] = 0.0
        statuses["aus"] = "paused"
        conflict_notes.append("recession_fear: AUS bot paused (dividend/trend unreliable)")

        weights["china"] = round(weights["china"] * 0.5, 4)
        size_caps["china"] = min(size_caps["china"], 0.5)
        statuses["china"] = "reduced"
        conflict_notes.append("recession_fear: China bot reduced")

    elif primary == "inflation_concern":
        weights["eu"] = round(weights["eu"] * 0.5, 4)
        size_caps["eu"] = min(size_caps["eu"], 0.5)
        statuses["eu"] = "reduced"
        conflict_notes.append("inflation_concern: EU mean-reversion may be disrupted — reduced")

    elif primary == "geopolitical_stress":
        weights["china"] = round(weights["china"] * 0.4, 4)
        size_caps["china"] = 0.0
        statuses["china"] = "paused"
        conflict_notes.append("geopolitical_stress: China bot paused (ADR spreads unreliable)")

    elif primary == "fragile_liquidity":
        # Reduce aggressive bots, keep defensive ones
        for bot in ["us", "china"]:
            weights[bot] = round(weights[bot] * 0.6, 4)
            size_caps[bot] = min(size_caps[bot], 0.6)
            if statuses[bot] == "active":
                statuses[bot] = "reduced"
        conflict_notes.append("fragile_liquidity: US and China bots reduced")

    elif primary == "risk_on":
        # Baseline weights — no additional reduction
        pass

    # --- VIX comfort zone check (not a kill switch, just a nudge) ---
    if vix is not None and 30 < vix <= 40:
        for bot in size_caps:
            size_caps[bot] = min(size_caps[bot], 0.7)
        conflict_notes.append(f"VIX={vix:.1f} in 30-40 range — all size_caps nudged down to 0.70 max")

    # --- Normalize weights to sum to 1 (excluding paused bots) ---
    total_weight = sum(weights[b] for b in weights if statuses[b] != "paused")
    if total_weight > 0:
        scale = min(1.0, 1.0 / total_weight) if total_weight > 1.0 else 1.0
        for bot in weights:
            if statuses[bot] != "paused":
                weights[bot] = round(min(weights[bot] * scale, MAX_WEIGHT), 4)

    # Ensure paused bots have 0 weight
    for bot in weights:
        if statuses[bot] == "paused":
            weights[bot] = 0.0
            size_caps[bot] = 0.0

    return _build_permissions(statuses, weights, size_caps), conflict_notes


def _build_permissions(
    statuses: dict, weights: dict, size_caps: dict
) -> dict:
    return {
        bot: {
            "status": statuses[bot],
            "capital_weight": round(weights[bot], 4),
            "size_cap": round(size_caps[bot], 4),
        }
        for bot in ["us", "eu", "aus", "china"]
    }

# ---------------------------------------------------------------------------
# Reasoning string
# ---------------------------------------------------------------------------

def build_reasoning(
    regime: dict,
    internals: dict,
    exposure: dict,
    kill_switch: bool,
    kill_switch_reason: str,
    drawdown: float,
    conflict_notes: list[str],
    permissions: dict,
) -> str:
    vix = internals.get("vix")
    spy_pct = internals.get("spy_vs_5d_pct")
    breadth = internals.get("breadth_score")
    dxy = internals.get("dxy_trend")
    pc_skew = internals.get("put_call_skew")
    sector_disp = internals.get("sector_dispersion")

    lines = [
        "=== Portfolio Brain — Regime & Allocation Reasoning ===",
        f"Date: {date.today().isoformat()}  |  Run: {datetime.now(timezone.utc).isoformat()}",
        "",
        "--- Market Internals ---",
        f"  VIX: {vix if vix is not None else 'MISSING (feed zero or unavailable)'}",
        f"  DXY trend: {dxy or 'MISSING'}",
        f"  SPY vs 5d ago: {f'{spy_pct*100:.2f}%' if spy_pct is not None else 'MISSING'}",
        f"  Breadth score: {f'{breadth:.3f}' if breadth is not None else 'MISSING'}",
        f"  Put/call skew: {f'{pc_skew:.4f}' if pc_skew is not None else 'MISSING'}",
        f"  Sector dispersion: {f'{sector_disp:.4f}' if sector_disp is not None else 'MISSING'}",
        "",
        "--- Regime Classification ---",
        f"  Primary: {regime['primary_regime']}",
        f"  Confidence: {regime['confidence']:.4f}",
        f"  Secondary: {regime.get('secondary_regime') or 'none'}",
        f"  Transition risk: {regime['transition_risk']:.4f}",
    ]

    if regime["confidence"] < 0.5:
        lines.append("  ⚠ Low confidence — allocator operating conservatively")
    if regime["transition_risk"] > 0.5:
        lines.append("  ⚠ High transition risk — all sizing reduced 50%")

    lines += [
        "",
        "--- Portfolio Exposure ---",
        f"  Net delta estimate: {exposure['net_delta_estimate']:.4f}",
        f"  Net vega estimate: {exposure['net_vega_estimate']:.4f}",
        f"  Total notional open: ${exposure['total_notional']:.0f}",
        f"  Open positions: {exposure['equity_count']} equity, {exposure['options_count']} options",
        "",
        "--- Drawdown ---",
        f"  Max drawdown (cumulative): {drawdown*100:.2f}%",
        "",
        "--- Kill Switch ---",
        f"  Active: {kill_switch}",
    ]

    if kill_switch:
        lines.append(f"  Reason: {kill_switch_reason}")

    lines += [
        "",
        "--- Bot Permissions ---",
    ]
    for bot, perm in permissions.items():
        lines.append(
            f"  {bot}: status={perm['status']} "
            f"capital_weight={perm['capital_weight']:.4f} "
            f"size_cap={perm['size_cap']:.4f}"
        )

    if conflict_notes:
        lines += ["", "--- Allocation Adjustments Applied ---"]
        for note in conflict_notes:
            lines.append(f"  • {note}")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Postgres system log
# ---------------------------------------------------------------------------

def log_to_postgres(
    conn: psycopg2.extensions.connection,
    level: str,
    message: str,
    payload: Optional[dict] = None,
) -> None:
    """Insert a record into nwt_system_log. Never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO nwt_system_log (level, component, message, payload)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    level,
                    "master-strategist",
                    message,
                    json.dumps(payload) if payload else None,
                ),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("Failed to write to nwt_system_log: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Write master-directives.json
# ---------------------------------------------------------------------------

def write_directives(directives: dict) -> None:
    """
    Write master-directives.json to the shared directory.
    Creates shared/ if it doesn't exist (first run).
    """
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = DIRECTIVES_PATH.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(directives, f, indent=2, default=str)
        tmp_path.replace(DIRECTIVES_PATH)
        logger.info("master-directives.json written to %s", DIRECTIVES_PATH)
    except Exception as exc:
        logger.error("Failed to write master-directives.json: %s", exc)
        raise

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Returns 0 on success, 1 on integrity failure, 2 on unexpected error.
    """
    logger.info("=== Portfolio Brain (master-strategist) starting ===")

    # --- Load env ---
    dsn = os.environ.get("NWT_DB_DSN")
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    base_url = (os.environ.get("ALPACA_BASE_URL") or "").rstrip("/")
    data_url = (os.environ.get("ALPACA_DATA_URL") or "").rstrip("/")

    missing = [
        k for k, v in {
            "NWT_DB_DSN": dsn,
            "ALPACA_API_KEY": api_key,
            "ALPACA_SECRET_KEY": secret_key,
            "ALPACA_BASE_URL": base_url,
            "ALPACA_DATA_URL": data_url,
        }.items()
        if not v
    ]
    if missing:
        logger.critical("Missing required env vars: %s — entering NO-TRADE MODE", missing)
        return 1

    # Guard against double /v2/v2/ — ALPACA_BASE_URL must NOT have trailing /v2
    if base_url.endswith("/v2"):
        logger.critical(
            "ALPACA_BASE_URL has trailing /v2 ('%s') — this causes double /v2/v2/ errors. "
            "Remove the /v2 suffix from .env — entering NO-TRADE MODE",
            base_url,
        )
        return 1

    conn: Optional[psycopg2.extensions.connection] = None

    try:
        # --- Integrity gate ---
        try:
            conn = run_integrity_checks(
                dsn=dsn,
                base_url=base_url,
                api_key=api_key,
                secret_key=secret_key,
            )
        except IntegrityError as exc:
            logger.critical("INTEGRITY CHECK FAILED: %s — entering NO-TRADE MODE", exc)
            return 1

        # --- Market internals ---
        logger.info("Fetching market internals...")
        internals = fetch_market_internals(
            alpaca_api_key=api_key,
            alpaca_secret_key=secret_key,
            alpaca_base_url=base_url,
            alpaca_data_url=data_url,
        )

        # --- Portfolio ledger ---
        logger.info("Reading portfolio ledger...")
        positions = read_open_positions(conn)
        exposure = compute_exposure(positions)

        # --- Drawdown ---
        logger.info("Computing drawdown...")
        drawdown = compute_drawdown(conn)

        # --- Regime classification ---
        logger.info("Classifying regime...")
        regime = classify_regime(internals, exposure)

        # --- Kill switch ---
        vix = internals.get("vix")
        kill_switch, kill_reason = evaluate_kill_switch(vix, drawdown)

        # --- Bot permissions ---
        logger.info("Computing bot permissions...")
        permissions, conflict_notes = compute_bot_permissions(
            regime=regime,
            exposure=exposure,
            kill_switch=kill_switch,
            vix=vix,
        )

        # --- Reasoning ---
        reasoning = build_reasoning(
            regime=regime,
            internals=internals,
            exposure=exposure,
            kill_switch=kill_switch,
            kill_switch_reason=kill_reason,
            drawdown=drawdown,
            conflict_notes=conflict_notes,
            permissions=permissions,
        )

        # --- Build directives ---
        directives = {
            "date": date.today().isoformat(),
            "regime": regime,
            "vix": vix,
            "global_kill_switch": kill_switch,
            "net_delta_estimate": exposure["net_delta_estimate"],
            "net_vega_estimate": exposure["net_vega_estimate"],
            "bot_permissions": permissions,
            "conflict_notes": "; ".join(conflict_notes) if conflict_notes else "",
            "reasoning": reasoning,
        }

        # --- Write output ---
        write_directives(directives)

        # --- Log to Postgres ---
        summary = (
            f"Regime: {regime['primary_regime']} "
            f"(confidence={regime['confidence']:.3f}, "
            f"transition_risk={regime['transition_risk']:.3f}). "
            f"Kill switch: {kill_switch}. "
            f"VIX: {vix if vix is not None else 'MISSING'}. "
            f"Drawdown: {drawdown*100:.2f}%. "
            f"Open positions: {exposure['equity_count']+exposure['options_count']}."
        )
        log_to_postgres(
            conn,
            level="INFO",
            message=summary,
            payload={
                "regime": regime,
                "vix": vix,
                "kill_switch": kill_switch,
                "drawdown": drawdown,
                "net_delta": exposure["net_delta_estimate"],
                "net_vega": exposure["net_vega_estimate"],
                "permissions": permissions,
            },
        )

        logger.info("=== master-strategist complete ===")
        logger.info(summary)
        return 0

    except Exception as exc:
        logger.critical("Unexpected error in master-strategist: %s", exc)
        logger.critical(traceback.format_exc())
        if conn:
            try:
                log_to_postgres(
                    conn,
                    level="CRITICAL",
                    message=f"master-strategist crashed: {exc}",
                    payload={"traceback": traceback.format_exc()},
                )
            except Exception:
                pass
        return 2

    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
