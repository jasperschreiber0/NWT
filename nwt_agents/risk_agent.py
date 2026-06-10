"""
nwt_agents/risk_agent.py
THE MOST AUTHORITARIAN COMPONENT. No LLM. Pure deterministic code.
Fires every 5 minutes 13:00-21:00 UTC via cron.

Rules 0-13: veto individual trade proposals.
Rules 14-16: system-level enforcement (heartbeat, drawdown, VIX) — sets no_trade_mode.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import requests
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import (
    check_no_trade_mode,
    get_db,
    insert_decision,
    insert_ticket,
    load_master_directives,
    log_system_event,
    set_no_trade_mode,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("risk_agent")

ALPACA_BASE_URL = os.environ.get("NWT_ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
ALPACA_DATA_URL = os.environ.get("NWT_ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("NWT_ALPACA_KEY_ID", ""),
    "APCA-API-SECRET-KEY": os.environ.get("NWT_ALPACA_SECRET_KEY", ""),
}

ET_TZ = ZoneInfo("America/New_York")

VIX_KILL_THRESHOLD = 40.0
DRAWDOWN_KILL_THRESHOLD = 0.08
SLIPPAGE_EXPANSION_FACTOR = 2.0
CONSECUTIVE_LOSS_LIMIT = 4
NET_DELTA_CAP = 0.70
REGIME_CONFIDENCE_REDUCE = 0.40
REGIME_TRANSITION_PAUSE = 0.60
SPREAD_WIDENING_FACTOR = 3.0
EXECUTION_STALE_MINUTES = 30
HEARTBEAT_STALE_MINUTES = 5
ACCOUNT_SIZE = 97_000.0
INTRADAY_LOSS_LIMIT = -0.015 * ACCOUNT_SIZE  # -1.5% of account equity


# ---------------------------------------------------------------------------
# DST-aware time helpers
# ---------------------------------------------------------------------------

def _et_now() -> datetime:
    return datetime.now(ET_TZ)


def _hard_close_utc() -> datetime:
    """15:45 ET in UTC, fully DST-aware."""
    et_today = _et_now().date()
    hard_close = datetime(et_today.year, et_today.month, et_today.day, 15, 45, tzinfo=ET_TZ)
    return hard_close.astimezone(timezone.utc)


def _entry_cutoff_utc() -> datetime:
    """15:30 ET in UTC, fully DST-aware."""
    et_today = _et_now().date()
    cutoff = datetime(et_today.year, et_today.month, et_today.day, 15, 30, tzinfo=ET_TZ)
    return cutoff.astimezone(timezone.utc)


def _is_market_hours() -> bool:
    """True if current ET time is between 09:30 and 16:00."""
    et_now = _et_now()
    return datetime(1, 1, 1, 9, 30).time() <= et_now.time() <= datetime(1, 1, 1, 16, 0).time()


# ---------------------------------------------------------------------------
# State readers
# ---------------------------------------------------------------------------

def fetch_pending_proposals(conn) -> list:
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
    Compute current drawdown from nwt_equity_curve (30-day rolling peak).
    Falls back to trade outcomes if equity curve is empty.
    Returns drawdown as positive fraction (0.05 = 5%).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT equity FROM nwt_equity_curve
            ORDER BY date DESC LIMIT 30
            """
        )
        rows = cur.fetchall()

    if rows:
        equities = [float(r[0]) for r in rows]
        equities.reverse()  # oldest first
        peak = max(equities)
        current = equities[-1]
        if peak > 0:
            return max(0.0, (peak - current) / peak)
        return 0.0

    # Fallback: compute from trade PnL if equity curve not populated yet
    with conn.cursor() as cur:
        cur.execute("SELECT pnl FROM nwt_trade_outcomes ORDER BY closed_at ASC")
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
        else:
            try:
                track = json.loads(payload).get("track")
            except Exception:
                track = None
        if track:
            disabled.add(track)
    return disabled


def get_average_slippage(conn) -> float:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT AVG(realized_slippage) FROM nwt_portfolio_ledger
            WHERE realized_slippage IS NOT NULL
              AND entry_time > NOW() - INTERVAL '7 days'
            """
        )
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.001


def get_recent_slippage(conn) -> float:
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
    try:
        return float(load_master_directives().get("net_delta_estimate", 0.0))
    except Exception:
        return 0.0


def fetch_vix_with_fallback(conn) -> tuple[float | None, str]:
    """
    Returns (vix_value, source) or (None, 'unavailable').
    VIX=0 is treated as missing — never as a signal.
    Fallback: ATM SPY ~30 DTE IV from Alpaca options chain.
    """
    try:
        directives = load_master_directives()
        vix = directives.get("vix") or 0.0
        if vix > 0:
            return float(vix), "master_directives"
    except Exception:
        pass

    # Fallback: compute from ATM SPY 30-day IV
    try:
        from datetime import date, timedelta
        today = date.today()
        exp_target = (today + timedelta(days=30)).isoformat()
        url = f"{ALPACA_BASE_URL}/v2/options/contracts"
        params = {
            "underlying_symbols": "SPY",
            "expiration_date_gte": (today + timedelta(days=25)).isoformat(),
            "expiration_date_lte": (today + timedelta(days=35)).isoformat(),
            "type": "call",
            "limit": 10,
        }
        resp = requests.get(url, headers=ALPACA_HEADERS, params=params, timeout=15)
        resp.raise_for_status()
        contracts = resp.json().get("option_contracts", [])
        if contracts:
            # Get SPY price
            price_url = f"{ALPACA_DATA_URL}/v2/stocks/SPY/trades/latest"
            pr = requests.get(price_url, headers=ALPACA_HEADERS, timeout=10)
            pr.raise_for_status()
            spy_price = float(pr.json()["trade"]["p"])
            # Find ATM contract
            atm = min(contracts, key=lambda c: abs(float(c.get("strike_price", 0)) - spy_price))
            iv = float(atm.get("implied_volatility") or 0)
            if iv > 0:
                # Annualise to VIX-like scale (already annualised, multiply by 100)
                vix_proxy = iv * 100
                return vix_proxy, "spy_iv_proxy"
    except Exception as exc:
        logger.warning("VIX fallback computation failed: %s", exc)

    return None, "unavailable"


def execution_engine_is_stale(conn) -> bool:
    """Check heartbeat table first (new), fall back to ticket-based check."""
    # Primary: heartbeat table
    if _is_market_hours():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_beat FROM nwt_heartbeat WHERE service = 'execution_engine'"
            )
            row = cur.fetchone()
        if row:
            age = (datetime.now(timezone.utc) - row[0].replace(tzinfo=timezone.utc)).total_seconds()
            if age > HEARTBEAT_STALE_MINUTES * 60:
                return True
            return False
        # No heartbeat row → engine hasn't run yet

    # Fallback: ticket-based check
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=EXECUTION_STALE_MINUTES)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM nwt_tickets t
            WHERE t.to_agent = 'EXECUTION_ENGINE'
              AND t.type = 'TRADE_REQUEST'
              AND t.created_at < %s
              AND NOT EXISTS (
                  SELECT 1 FROM nwt_ticket_decisions d
                  WHERE d.ticket_id = t.ticket_id AND d.decided_by = 'EXECUTION_ENGINE'
              )
            """,
            (cutoff,),
        )
        pending_old = cur.fetchone()[0]

    if pending_old == 0:
        return False

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM nwt_ticket_decisions WHERE decided_by='EXECUTION_ENGINE' AND created_at > %s",
            (cutoff,),
        )
        recent = cur.fetchone()[0]

    return recent == 0 and pending_old > 0


def get_positions_past_hard_close(conn) -> list:
    hard_close = _hard_close_utc()
    now_utc = datetime.now(timezone.utc)
    if now_utc < hard_close:
        return []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM nwt_portfolio_ledger WHERE status='open' AND asset_type='option'"
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_api_anomaly(conn) -> bool:
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
        return cur.fetchone()[0] > 0


# ---------------------------------------------------------------------------
# Kill switch and track management
# ---------------------------------------------------------------------------

def get_intraday_pnl(conn) -> float:
    """Sum realized PnL (pnl_adjusted preferred) from positions closed today UTC."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(pnl_adjusted), SUM(pnl), 0)
            FROM nwt_trade_outcomes
            WHERE closed_at >= %s
            """,
            (today_start,),
        )
        row = cur.fetchone()
    return float(row[0] or 0)


def activate_global_kill_switch(conn, reason: str) -> None:
    """Sets no_trade_mode flag AND updates master-directives.json."""
    set_no_trade_mode(conn, f"KILL_SWITCH: {reason}", "risk_agent")
    log_system_event(conn, "CRITICAL", "risk_agent",
                     f"GLOBAL_KILL_SWITCH_ACTIVATED: {reason}", {"reason": reason})
    insert_ticket(conn, "RISK_AGENT", "SYSTEM", "kill_switch",
                  {"reason": reason, "activated_at": datetime.now(timezone.utc).isoformat()})
    logger.critical("GLOBAL KILL SWITCH ACTIVATED: %s", reason)
    try:
        from notifier import alert_kill_switch
        alert_kill_switch(reason)
    except Exception:
        pass
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
        logger.error("Failed to update master-directives.json kill switch: %s", exc)


def disable_track(conn, track: str, reason: str) -> None:
    log_system_event(conn, "CRITICAL", "risk_agent",
                     f"TRACK_DISABLED: Track {track} disabled — {reason}",
                     {"track": track, "reason": reason})
    logger.critical("Track %s DISABLED: %s", track, reason)


# ---------------------------------------------------------------------------
# System-level rules (run every cycle, independent of proposals)
# ---------------------------------------------------------------------------

def run_system_rules(conn) -> None:
    """
    Rules 14-16: heartbeat, drawdown, VIX.
    These set no_trade_mode if triggered — they don't veto individual proposals,
    they halt the whole system.
    """
    # Rule 14: Heartbeat check (handled inline in execution_engine_is_stale)
    # If stale during market hours → set no_trade_mode
    if _is_market_hours() and execution_engine_is_stale(conn):
        reason = f"Rule 14: Execution engine heartbeat stale (>{HEARTBEAT_STALE_MINUTES} min)"
        set_no_trade_mode(conn, reason, "risk_agent")
        insert_ticket(conn, "RISK_AGENT", "SYSTEM", "heartbeat_lost",
                      {"reason": reason, "at": datetime.now(timezone.utc).isoformat()})
        logger.critical(reason)
        try:
            from notifier import alert_heartbeat_lost
            alert_heartbeat_lost("execution_engine")
        except Exception:
            pass
        return

    # Rule 15: Drawdown > 8% from 30-day equity peak
    drawdown = get_current_drawdown(conn)
    if drawdown > DRAWDOWN_KILL_THRESHOLD:
        reason = f"Rule 15: Drawdown={drawdown:.1%} > {DRAWDOWN_KILL_THRESHOLD:.0%} from 30-day peak"
        activate_global_kill_switch(conn, reason)
        return

    # Rule 16: VIX > 40 (with fallback and degraded handling)
    vix, vix_source = fetch_vix_with_fallback(conn)
    if vix is None:
        insert_ticket(conn, "RISK_AGENT", "SYSTEM", "vix_degraded",
                      {"note": "VIX feed and IV proxy both unavailable — drawdown leg still enforced"})
        logger.warning("Rule 16: VIX unavailable — vix_degraded ticket written")
    elif vix > VIX_KILL_THRESHOLD:
        reason = f"Rule 16: VIX={vix:.1f} (source={vix_source}) > {VIX_KILL_THRESHOLD}"
        activate_global_kill_switch(conn, reason)
        return

    # Rule 17: Intraday loss > 1.5% of account equity
    intraday_pnl = get_intraday_pnl(conn)
    if intraday_pnl < INTRADAY_LOSS_LIMIT:
        reason = (
            f"Rule 17: Intraday PnL=${intraday_pnl:+.0f} < "
            f"${INTRADAY_LOSS_LIMIT:.0f} (-1.5% limit) — halting for the day"
        )
        activate_global_kill_switch(conn, reason)


# ---------------------------------------------------------------------------
# Per-proposal veto logic — Rules 0-13
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
    payload = ticket.get("payload") or {}
    regime = directives.get("regime", {})
    from_track = (payload.get("from_track") or "").upper()
    direction = (payload.get("direction") or "").lower()
    symbol = payload.get("symbol", "")

    now_utc = datetime.now(timezone.utc)
    entry_cutoff = _entry_cutoff_utc()

    # Rule 0: Past 15:30 ET entry cutoff
    if now_utc >= entry_cutoff:
        return "VETOED", (
            f"Rule 0: Past new-entry cutoff {entry_cutoff.strftime('%H:%M')} UTC "
            f"(15:30 ET) — no new positions"
        )

    # Rule 1: VIX > 40 (checked via system rules; proposal-level veto mirrors it)
    vix = directives.get("vix") or 0.0
    if vix > VIX_KILL_THRESHOLD:
        activate_global_kill_switch(conn, f"VIX={vix:.1f} > {VIX_KILL_THRESHOLD}")
        return "VETOED", f"Rule 1: VIX={vix:.1f} > {VIX_KILL_THRESHOLD}"

    # Rule 2: Drawdown > 8%
    if drawdown > DRAWDOWN_KILL_THRESHOLD:
        activate_global_kill_switch(conn, f"Drawdown={drawdown:.1%}")
        return "VETOED", f"Rule 2: Drawdown={drawdown:.1%} > {DRAWDOWN_KILL_THRESHOLD:.0%}"

    # Rule 3: Slippage expansion > 2x — flag but don't veto
    if baseline_slippage > 0 and recent_slippage > SLIPPAGE_EXPANSION_FACTOR * baseline_slippage:
        log_system_event(conn, "WARNING", "risk_agent",
                         f"Rule 3: Slippage expansion {recent_slippage:.4f} vs baseline {baseline_slippage:.4f}")

    # Rule 4: Consecutive losses >= 4 (same track)
    track_losses = consecutive_losses.get(from_track, 0)
    if from_track and track_losses >= CONSECUTIVE_LOSS_LIMIT:
        disable_track(conn, from_track, f"{track_losses} consecutive losses")
        disabled_tracks.add(from_track)
        return "VETOED", f"Rule 4: Track {from_track} has {track_losses} consecutive losses — disabled"

    # Rule 5: Net delta > 0.7 → no new longs
    if net_delta > NET_DELTA_CAP and direction == "long":
        return "VETOED", f"Rule 5: Net delta={net_delta:.2f} > {NET_DELTA_CAP} — no new longs"

    # Rule 6: Net delta < -0.7 → no new shorts
    if net_delta < -NET_DELTA_CAP and direction == "short":
        return "VETOED", f"Rule 6: Net delta={net_delta:.2f} < -{NET_DELTA_CAP} — no new shorts"

    # Rule 7: Regime confidence < 0.4 — log, don't veto (sizing handled by compute_final_sizing)
    confidence = regime.get("confidence", 1.0)
    if confidence < REGIME_CONFIDENCE_REDUCE:
        log_system_event(conn, "WARNING", "risk_agent",
                         f"Rule 7: Regime confidence={confidence:.2f} < {REGIME_CONFIDENCE_REDUCE} — sizing reduced")

    # Rule 8: Regime transition_risk > 0.6 → pause entries
    transition_risk = regime.get("transition_risk", 0.0)
    if transition_risk > REGIME_TRANSITION_PAUSE:
        return "VETOED", f"Rule 8: Regime transition_risk={transition_risk:.2f} > {REGIME_TRANSITION_PAUSE}"

    # Rule 9: API anomaly
    if api_anomaly:
        return "VETOED", "Rule 9: Recent API anomaly — pausing execution"

    # Rule 10: Spread widening > 3x (slippage as proxy)
    if baseline_slippage > 0 and recent_slippage > SPREAD_WIDENING_FACTOR * baseline_slippage:
        return "VETOED", (
            f"Rule 10: Spread widening — slippage={recent_slippage:.4f} "
            f"> {SPREAD_WIDENING_FACTOR}x baseline={baseline_slippage:.4f}"
        )

    # Rule 11: Execution engine unresponsive (heartbeat / ticket-based)
    if execution_stale:
        return "VETOED", "Rule 11: Execution engine unresponsive (no fills in last 30 min) — NO-TRADE MODE"

    # Rule 13: Track in cooling-off
    if from_track in disabled_tracks:
        return "VETOED", f"Rule 13: Track {from_track} is in cooling-off — proposals rejected"

    return "APPROVED", "All 13 risk rules passed"


# ---------------------------------------------------------------------------
# Hard close enforcement (Rule 12)
# ---------------------------------------------------------------------------

def force_close_past_hard_close(conn, positions: list) -> None:
    hard_close_utc = _hard_close_utc()
    for pos in positions:
        position_id = str(pos.get("position_id", ""))
        asset = pos.get("asset", "")
        logger.warning("Rule 12: Force close %s (position_id=%s) — past hard close %s UTC",
                       asset, position_id, hard_close_utc.strftime("%H:%M"))
        try:
            insert_ticket(
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
                    "exit_reason": "hard_close",
                    "position_id": position_id,
                },
            )
            log_system_event(conn, "CRITICAL", "risk_agent",
                             f"Rule 12: Force close ticket for {asset}",
                             {"position_id": position_id})
        except Exception as exc:
            logger.error("Failed to insert force close ticket for %s: %s", asset, exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    conn = get_db()
    try:
        # no_trade_mode check — risk agent is allowed to run even when halted
        # (it may be what CLEARS a halt condition, and system rules must still fire)
        # But it will not approve new proposals if halted.
        halted, halt_reason = check_no_trade_mode(conn)
        if halted:
            logger.warning("no_trade_mode is SET: %s — system rules will still run", halt_reason)

        try:
            directives = load_master_directives()
        except FileNotFoundError:
            logger.error("master-directives.json not found — exiting")
            sys.exit(1)

        # Run system-level rules every cycle (drawdown, VIX, heartbeat)
        run_system_rules(conn)

        # Hard close check (independent of proposals)
        past_close_positions = get_positions_past_hard_close(conn)
        if past_close_positions:
            force_close_past_hard_close(conn, past_close_positions)

        # Gather per-proposal state
        drawdown = get_current_drawdown(conn)
        consecutive_losses = get_consecutive_losses_by_track(conn)
        disabled_tracks = get_disabled_tracks(conn)
        baseline_slippage = get_average_slippage(conn)
        recent_slippage = get_recent_slippage(conn)
        net_delta = get_net_delta(conn)
        execution_stale = execution_engine_is_stale(conn)
        api_anomaly = get_api_anomaly(conn)

        pending = fetch_pending_proposals(conn)
        logger.info(
            "Risk agent: %d proposals | drawdown=%.2f%% | losses=%s | stale=%s | halted=%s",
            len(pending), drawdown * 100, consecutive_losses, execution_stale, halted,
        )

        approved_count = 0
        vetoed_count = 0

        for ticket in pending:
            ticket_id = str(ticket["ticket_id"])

            if halted:
                decision = "VETOED"
                reasoning = f"no_trade_mode is set: {halt_reason}"
            else:
                decision, reasoning = evaluate_proposal(
                    conn=conn, ticket=ticket, directives=directives,
                    drawdown=drawdown, consecutive_losses=consecutive_losses,
                    disabled_tracks=disabled_tracks, baseline_slippage=baseline_slippage,
                    recent_slippage=recent_slippage, net_delta=net_delta,
                    execution_stale=execution_stale, api_anomaly=api_anomaly,
                )

            insert_decision(conn, ticket_id, decision, reasoning, "RISK_AGENT")
            payload = ticket.get("payload") or {}

            if decision == "APPROVED":
                approved_count += 1
                logger.info("APPROVED: %s %s", payload.get("strategy_id", "?"), payload.get("symbol", "?"))
            else:
                vetoed_count += 1
                logger.info("VETOED: %s %s — %s", payload.get("strategy_id", "?"),
                            payload.get("symbol", "?"), reasoning)

        log_system_event(conn, "INFO", "risk_agent",
                         f"Risk agent run: {approved_count} approved, {vetoed_count} vetoed",
                         {"approved": approved_count, "vetoed": vetoed_count,
                          "drawdown_pct": round(drawdown * 100, 2),
                          "disabled_tracks": list(disabled_tracks), "halted": halted})
        logger.info("Risk agent done — %d approved, %d vetoed", approved_count, vetoed_count)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
