"""
nwt_agents/risk_agent.py
THE MOST AUTHORITARIAN COMPONENT. No LLM. Pure deterministic code. 13 veto rules.
Fires every 5 minutes 13:00-21:00 UTC via cron.

Reads pending TRADE_PROPOSAL tickets (to_agent='RISK_AGENT') with no decision yet.
Applies all 13 rules. APPROVES or VETOES each proposal.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import requests
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import (
    get_db,
    insert_decision,
    load_master_directives,
    log_system_event,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("risk_agent")

ALPACA_BASE_URL = os.environ.get("NWT_ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("NWT_ALPACA_KEY_ID", ""),
    "APCA-API-SECRET-KEY": os.environ.get("NWT_ALPACA_SECRET_KEY", ""),
}

# Rule thresholds
VIX_KILL_THRESHOLD = 40.0
DRAWDOWN_KILL_THRESHOLD = 0.08       # 8%
SLIPPAGE_EXPANSION_FACTOR = 2.0      # 2x baseline
CONSECUTIVE_LOSS_LIMIT = 4
NET_DELTA_CAP = 0.70
REGIME_CONFIDENCE_REDUCE = 0.40
REGIME_TRANSITION_PAUSE = 0.60
HARD_CLOSE_UTC_HOUR = 21
HARD_CLOSE_UTC_MINUTE = 45
EXECUTION_STALE_MINUTES = 30
SPREAD_WIDENING_FACTOR = 3.0

# Account size for drawdown calculation
ACCOUNT_SIZE = 97_000.0


# ---------------------------------------------------------------------------
# State readers
# ---------------------------------------------------------------------------

def fetch_pending_proposals(conn) -> list:
    """TRADE_PROPOSAL tickets for RISK_AGENT with no decision yet."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT t.*
            FROM nwt_tickets t
            WHERE t.to_agent = 'RISK_AGENT'
              AND t.type = 'TRADE_PROPOSAL'
              AND NOT EXISTS (
                  SELECT 1 FROM nwt_ticket_decisions d
                  WHERE d.ticket_id = t.ticket_id
              )
            ORDER BY t.created_at ASC
            """
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_current_drawdown(conn) -> float:
    """
    Compute current drawdown from nwt_trade_outcomes.
    Returns drawdown as a positive fraction (e.g. 0.05 = 5%).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pnl FROM nwt_trade_outcomes ORDER BY closed_at ASC"
        )
        rows = cur.fetchall()

    if not rows:
        return 0.0

    pnls = [float(r[0]) for r in rows if r[0] is not None]
    if not pnls:
        return 0.0

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = (peak - cumulative) / (ACCOUNT_SIZE + peak) if (ACCOUNT_SIZE + peak) > 0 else 0.0
        max_dd = max(max_dd, dd)

    return max_dd


def get_consecutive_losses_by_track(conn) -> dict:
    """
    Count consecutive losses for each track from the last 4 closed trades per track.
    Returns dict: {track: consecutive_loss_count}
    """
    result = {}
    for track in ("C", "D", "E"):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pnl FROM nwt_trade_outcomes
                WHERE strategy_id LIKE %s
                ORDER BY closed_at DESC LIMIT 4
                """,
                (f"{track}%",),
            )
            rows = cur.fetchall()
        losses = sum(1 for r in rows if r[0] is not None and float(r[0]) < 0)
        result[track] = losses
    return result


def get_disabled_tracks(conn) -> set:
    """
    Return set of tracks currently disabled by the risk agent (cooling-off).
    Look for system log entries of TRACK_DISABLED level from last 24h.
    """
    disabled = set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload FROM nwt_system_log
            WHERE component = 'risk_agent'
              AND message LIKE 'TRACK_DISABLED%'
              AND created_at > NOW() - INTERVAL '24 hours'
            """
        )
        rows = cur.fetchall()
    for row in rows:
        payload = row[0]
        if isinstance(payload, dict):
            track = payload.get("track")
            if track:
                disabled.add(track)
        elif isinstance(payload, str):
            try:
                p = json.loads(payload)
                track = p.get("track")
                if track:
                    disabled.add(track)
            except Exception:
                pass
    return disabled


def get_average_slippage(conn) -> float:
    """Compute average slippage from recent ledger entries (baseline)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT AVG(realized_slippage) FROM nwt_portfolio_ledger
            WHERE realized_slippage IS NOT NULL
              AND entry_time > NOW() - INTERVAL '7 days'
            """
        )
        row = cur.fetchone()
    if row and row[0] is not None:
        return float(row[0])
    return 0.001  # Default 0.1% baseline


def get_recent_slippage(conn) -> float:
    """Average slippage from last 10 trades."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT realized_slippage FROM nwt_portfolio_ledger
            WHERE realized_slippage IS NOT NULL
            ORDER BY entry_time DESC LIMIT 10
            """
        )
        rows = cur.fetchall()
    if not rows:
        return 0.0
    return float(sum(r[0] for r in rows) / len(rows))


def get_net_delta(conn) -> float:
    """Read current net_delta_estimate from master-directives.json."""
    try:
        directives = load_master_directives()
        return float(directives.get("net_delta_estimate", 0.0))
    except Exception:
        return 0.0


def execution_engine_is_stale(conn) -> bool:
    """
    Check if execution engine has been unresponsive:
    No EXECUTED decisions in the last EXECUTION_STALE_MINUTES despite pending tickets.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=EXECUTION_STALE_MINUTES)

    with conn.cursor() as cur:
        # Are there tickets submitted to execution engine that are still pending?
        cur.execute(
            """
            SELECT COUNT(*) FROM nwt_tickets t
            WHERE t.to_agent = 'EXECUTION_ENGINE'
              AND t.type = 'TRADE_REQUEST'
              AND t.created_at < %s
              AND NOT EXISTS (
                  SELECT 1 FROM nwt_ticket_decisions d
                  WHERE d.ticket_id = t.ticket_id
                    AND d.decided_by = 'EXECUTION_ENGINE'
              )
            """,
            (cutoff,),
        )
        pending_old = cur.fetchone()[0]

    if pending_old == 0:
        return False  # No stale pending tickets

    # Check if execution engine produced any decisions recently
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM nwt_ticket_decisions
            WHERE decided_by = 'EXECUTION_ENGINE'
              AND created_at > %s
            """,
            (cutoff,),
        )
        recent_executions = cur.fetchone()[0]

    return recent_executions == 0 and pending_old > 0


def get_positions_past_hard_close(conn) -> list:
    """
    Return open positions past 21:45 UTC (15:45 ET + buffer).
    """
    now_utc = datetime.now(timezone.utc)
    if now_utc.hour < HARD_CLOSE_UTC_HOUR or (
        now_utc.hour == HARD_CLOSE_UTC_HOUR and now_utc.minute < HARD_CLOSE_UTC_MINUTE
    ):
        return []

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM nwt_portfolio_ledger WHERE status = 'open' AND asset_type = 'option'"
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_api_anomaly(conn) -> bool:
    """Check for recent API anomaly in system log."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM nwt_system_log
            WHERE level IN ('ERROR', 'CRITICAL')
              AND component IN ('execution_engine', 'execution_agent', 'integrity_gate')
              AND message ILIKE '%api%'
              AND created_at > NOW() - INTERVAL '15 minutes'
            """
        )
        count = cur.fetchone()[0]
    return count > 0


# ---------------------------------------------------------------------------
# Kill switch management
# ---------------------------------------------------------------------------

def activate_global_kill_switch(conn, reason: str) -> None:
    """Log kill switch activation. Actual kill switch lives in master-directives.json."""
    log_system_event(
        conn,
        "CRITICAL",
        "risk_agent",
        f"GLOBAL_KILL_SWITCH_ACTIVATED: {reason}",
        {"reason": reason},
    )
    logger.critical("GLOBAL KILL SWITCH ACTIVATED: %s", reason)
    # Write to master-directives.json
    try:
        from shared_context import _shared_dir
        path = _shared_dir() / "master-directives.json"
        with open(path) as f:
            directives = json.load(f)
        directives["global_kill_switch"] = True
        directives["kill_switch_reason"] = reason
        directives["kill_switch_activated_at"] = datetime.now(timezone.utc).isoformat()
        with open(path, "w") as f:
            json.dump(directives, f, indent=2)
    except Exception as exc:
        logger.error("Failed to write kill switch to master-directives.json: %s", exc)


def disable_track(conn, track: str, reason: str) -> None:
    log_system_event(
        conn,
        "CRITICAL",
        "risk_agent",
        f"TRACK_DISABLED: Track {track} disabled — {reason}",
        {"track": track, "reason": reason},
    )
    logger.critical("Track %s DISABLED: %s", track, reason)


# ---------------------------------------------------------------------------
# Core veto logic — 13 rules
# ---------------------------------------------------------------------------

def evaluate_proposal(
    conn,
    ticket: dict,
    directives: dict,
    drawdown: float,
    consecutive_losses: dict,
    disabled_tracks: set,
    baseline_slippage: float,
    recent_slippage: float,
    net_delta: float,
    execution_stale: bool,
    api_anomaly: bool,
) -> tuple[str, str]:
    """
    Apply all 13 risk rules to a proposal.
    Returns (decision, reasoning) where decision is 'APPROVED' or 'VETOED'.
    """
    payload = ticket.get("payload") or {}
    regime = directives.get("regime", {})
    vix = directives.get("vix") or 0.0
    from_track = (payload.get("from_track") or "").upper()
    direction = (payload.get("direction") or "").lower()
    symbol = payload.get("symbol", "")

    # RULE 1: VIX > 40 → global kill switch
    if vix > VIX_KILL_THRESHOLD:
        activate_global_kill_switch(conn, f"VIX={vix:.1f} > {VIX_KILL_THRESHOLD}")
        return "VETOED", f"Rule 1: VIX={vix:.1f} > {VIX_KILL_THRESHOLD} — global kill switch"

    # RULE 2: Drawdown > 8% → global kill switch
    if drawdown > DRAWDOWN_KILL_THRESHOLD:
        activate_global_kill_switch(conn, f"Drawdown={drawdown:.1%} > {DRAWDOWN_KILL_THRESHOLD:.0%}")
        return "VETOED", f"Rule 2: Drawdown={drawdown:.1%} > {DRAWDOWN_KILL_THRESHOLD:.0%} — global kill switch"

    # RULE 3: Slippage expansion > 2x baseline → reduce sizing (flag, still approve with warning)
    if baseline_slippage > 0 and recent_slippage > SLIPPAGE_EXPANSION_FACTOR * baseline_slippage:
        log_system_event(
            conn,
            "WARNING",
            "risk_agent",
            f"Rule 3: Slippage expansion detected ({recent_slippage:.4f} vs baseline {baseline_slippage:.4f})",
            {"recent": recent_slippage, "baseline": baseline_slippage},
        )
        # Note: we flag but do not veto — sizing reduction is handled by directives multiplier
        logger.warning(
            "Rule 3 WARN: slippage expansion %.4f vs baseline %.4f (proposal not vetoed but flagged)",
            recent_slippage, baseline_slippage,
        )

    # RULE 4: Consecutive losses >= 4 (same track) → disable track
    track_losses = consecutive_losses.get(from_track, 0)
    if from_track and track_losses >= CONSECUTIVE_LOSS_LIMIT:
        disable_track(conn, from_track, f"{track_losses} consecutive losses")
        disabled_tracks.add(from_track)
        return "VETOED", f"Rule 4: Track {from_track} has {track_losses} consecutive losses — track disabled"

    # RULE 5: Net delta > 0.7 → cap new long proposals
    if net_delta > NET_DELTA_CAP and direction == "long":
        return "VETOED", f"Rule 5: Net delta={net_delta:.2f} > {NET_DELTA_CAP} — no new long positions"

    # RULE 6: Net delta < -0.7 → cap new short proposals
    if net_delta < -NET_DELTA_CAP and direction == "short":
        return "VETOED", f"Rule 6: Net delta={net_delta:.2f} < -{NET_DELTA_CAP} — no new short positions"

    # RULE 7: Regime confidence < 0.4 → reduce sizing (log but approve with note)
    confidence = regime.get("confidence", 1.0)
    if confidence < REGIME_CONFIDENCE_REDUCE:
        log_system_event(
            conn,
            "WARNING",
            "risk_agent",
            f"Rule 7: Regime confidence={confidence:.2f} < {REGIME_CONFIDENCE_REDUCE} — sizing reduced",
        )
        # Note: sizing reduction enforced by compute_final_sizing in track agents, not a veto

    # RULE 8: Regime transition_risk > 0.6 → pause new entries
    transition_risk = regime.get("transition_risk", 0.0)
    if transition_risk > REGIME_TRANSITION_PAUSE:
        return "VETOED", f"Rule 8: Regime transition_risk={transition_risk:.2f} > {REGIME_TRANSITION_PAUSE} — pause new entries"

    # RULE 9: API anomaly detected → pause execution
    if api_anomaly:
        return "VETOED", "Rule 9: Recent API anomaly detected — pausing execution"

    # RULE 10: Spread widening > 3x normal (approximated via slippage proxy)
    # Use slippage as spread proxy — if very high, treat as spread widening
    if baseline_slippage > 0 and recent_slippage > SPREAD_WIDENING_FACTOR * baseline_slippage:
        return "VETOED", (
            f"Rule 10: Spread widening detected for {symbol} — "
            f"slippage={recent_slippage:.4f} > {SPREAD_WIDENING_FACTOR}x baseline={baseline_slippage:.4f}"
        )

    # RULE 11: Execution engine unresponsive → NO-TRADE MODE
    if execution_stale:
        return "VETOED", "Rule 11: Execution engine unresponsive (no fills in last 30 min) — NO-TRADE MODE"

    # RULE 12: Past hard close time for options — handle separately in hard_close_check

    # RULE 13: Track disabled by cooling-off
    if from_track in disabled_tracks:
        return "VETOED", f"Rule 13: Track {from_track} is in cooling-off period — proposals rejected"

    # All rules passed
    return "APPROVED", "All 13 risk rules passed"


# ---------------------------------------------------------------------------
# Hard close enforcement (Rule 12)
# ---------------------------------------------------------------------------

def force_close_past_hard_close(conn, positions: list) -> None:
    """
    For each open options position past 21:45 UTC, INSERT a force-close ticket.
    """
    for pos in positions:
        position_id = str(pos.get("position_id", ""))
        asset = pos.get("asset", "")
        logger.warning("Rule 12: Force close %s (position_id=%s) — past hard close time", asset, position_id)

        ticket_id_force = None
        try:
            from shared_context import insert_ticket
            ticket_id_force = insert_ticket(
                conn,
                from_agent="RISK_AGENT",
                to_agent="EXECUTION_ENGINE",
                type_="FORCE_CLOSE",
                payload={
                    "approved": True,
                    "bot_source": pos.get("bot_source", "RISK_AGENT"),
                    "symbol": asset,
                    "option_symbol": asset,
                    "direction": "close",
                    "strategy_id": "FORCE_CLOSE",
                    "sized_notional": float(pos.get("notional_risk", 0)),
                    "asset_type": "option",
                    "time_in_force": "day",
                    "reason": "Rule 12: Hard close time 21:45 UTC",
                    "position_id": position_id,
                },
            )
            log_system_event(
                conn,
                "CRITICAL",
                "risk_agent",
                f"Rule 12: Force close ticket inserted for {asset}",
                {"position_id": position_id, "force_close_ticket_id": ticket_id_force},
            )
        except Exception as exc:
            logger.error("Failed to insert force close ticket for %s: %s", asset, exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    conn = get_db()

    try:
        try:
            directives = load_master_directives()
        except FileNotFoundError:
            logger.error("master-directives.json not found — exiting")
            sys.exit(1)

        # Gather system state
        drawdown = get_current_drawdown(conn)
        consecutive_losses = get_consecutive_losses_by_track(conn)
        disabled_tracks = get_disabled_tracks(conn)
        baseline_slippage = get_average_slippage(conn)
        recent_slippage = get_recent_slippage(conn)
        net_delta = get_net_delta(conn)
        execution_stale = execution_engine_is_stale(conn)
        api_anomaly = get_api_anomaly(conn)

        # Rule 12: Hard close check (independent of proposals)
        past_close_positions = get_positions_past_hard_close(conn)
        if past_close_positions:
            force_close_past_hard_close(conn, past_close_positions)

        # Fetch pending proposals
        pending = fetch_pending_proposals(conn)
        logger.info(
            "Risk agent: %d pending proposals | drawdown=%.2f%% | consecutive_losses=%s | stale=%s",
            len(pending), drawdown * 100, consecutive_losses, execution_stale,
        )

        approved_count = 0
        vetoed_count = 0

        for ticket in pending:
            ticket_id = str(ticket["ticket_id"])
            decision, reasoning = evaluate_proposal(
                conn=conn,
                ticket=ticket,
                directives=directives,
                drawdown=drawdown,
                consecutive_losses=consecutive_losses,
                disabled_tracks=disabled_tracks,
                baseline_slippage=baseline_slippage,
                recent_slippage=recent_slippage,
                net_delta=net_delta,
                execution_stale=execution_stale,
                api_anomaly=api_anomaly,
            )

            insert_decision(conn, ticket_id, decision, reasoning, "RISK_AGENT")

            payload = ticket.get("payload") or {}
            symbol = payload.get("symbol", "?")
            strategy_id = payload.get("strategy_id", "?")

            if decision == "APPROVED":
                approved_count += 1
                logger.info("APPROVED: ticket=%s strategy=%s symbol=%s", ticket_id, strategy_id, symbol)
            else:
                vetoed_count += 1
                logger.info("VETOED: ticket=%s strategy=%s symbol=%s reason=%s", ticket_id, strategy_id, symbol, reasoning)

        log_system_event(
            conn,
            "INFO",
            "risk_agent",
            f"Risk agent run complete: {approved_count} approved, {vetoed_count} vetoed",
            {
                "approved": approved_count,
                "vetoed": vetoed_count,
                "drawdown_pct": round(drawdown * 100, 2),
                "consecutive_losses": consecutive_losses,
                "disabled_tracks": list(disabled_tracks),
                "execution_stale": execution_stale,
            },
        )
        logger.info("Risk agent done — %d approved, %d vetoed", approved_count, vetoed_count)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
