"""
nwt_agents/notifier.py
Telegram alerting. Fire-and-forget. Never crashes a trading agent.

Env vars required:
  TELEGRAM_BOT_TOKEN  — bot token from @BotFather
  TELEGRAM_CHAT_ID    — your chat ID (use @userinfobot to get it)

If either var is missing, all calls silently no-op.
"""

import logging
import os
from datetime import datetime, timezone

import requests

logger = logging.getLogger("notifier")

_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
_API_URL = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
_TIMEOUT = 8


def _send(text: str) -> None:
    if not _BOT_TOKEN or not _CHAT_ID:
        return
    try:
        requests.post(
            _API_URL,
            json={"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("Telegram send failed (non-fatal): %s", exc)


def alert_no_trade_mode(reason: str) -> None:
    _send(f"🚨 <b>NWT — NO_TRADE_MODE SET</b>\n{reason}\n{_ts()}")


def alert_kill_switch(reason: str) -> None:
    _send(f"🛑 <b>NWT — KILL SWITCH ACTIVATED</b>\n{reason}\n{_ts()}")


def alert_heartbeat_lost(service: str) -> None:
    _send(f"💔 <b>NWT — HEARTBEAT LOST</b>\nService: {service}\n{_ts()}")


def alert_recon_critical(mismatches: list) -> None:
    lines = "\n".join(f"  • {m}" for m in mismatches[:10])
    _send(f"⚠️ <b>NWT — RECON CRITICAL MISMATCH</b>\n{lines}\n{_ts()}")


def alert_zero_tickets(by_utc: str) -> None:
    _send(f"⚠️ <b>NWT — ZERO TICKETS</b>\nNo trade tickets by {by_utc} UTC\n{_ts()}")


def send_daily_digest(
    *,
    trades_today: int,
    pnl_today: float,
    cost_today: float,
    cost_per_trade: float | None,
    inactivity_today: int,
    approved_today: int,
    vetoed_today: int,
    no_trade_mode: bool,
    open_positions: int,
) -> None:
    status = "🔴 HALTED" if no_trade_mode else "🟢 LIVE"
    cpt = f"${cost_per_trade:.4f}" if cost_per_trade is not None else "n/a"
    msg = (
        f"📊 <b>NWT Daily Digest</b> — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n"
        f"Status: {status}\n"
        f"Trades closed: {trades_today}  |  PnL: ${pnl_today:+.2f}\n"
        f"Risk: approved={approved_today}  vetoed={vetoed_today}  inactive={inactivity_today}\n"
        f"Open positions: {open_positions}\n"
        f"API cost: ${cost_today:.4f}  |  cost/trade: {cpt}\n"
        f"{_ts()}"
    )
    _send(msg)


def send_daily_digest_with_scorecard(
    *,
    trades_today: int,
    pnl_today: float,
    cost_today: float,
    cost_per_trade: float | None,
    inactivity_today: int,
    approved_today: int,
    vetoed_today: int,
    no_trade_mode: bool,
    open_positions: int,
    session_green: bool,
    failed_checks: list,
) -> None:
    """Combined daily digest + session scorecard. Sent at 21:15 UTC from session_scorecard."""
    status = "🔴 HALTED" if no_trade_mode else "🟢 LIVE"
    scorecard = "🟢 GREEN" if session_green else f"🔴 RED — {', '.join(failed_checks)}"
    cpt = f"${cost_per_trade:.4f}" if cost_per_trade is not None else "n/a"
    msg = (
        f"📊 <b>NWT Daily Digest</b> — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n"
        f"Status: {status}  |  Session: {scorecard}\n"
        f"Trades closed: {trades_today}  |  PnL: ${pnl_today:+.2f}\n"
        f"Risk: approved={approved_today}  vetoed={vetoed_today}  inactive={inactivity_today}\n"
        f"Open positions: {open_positions}\n"
        f"API cost: ${cost_today:.4f}  |  cost/trade: {cpt}\n"
        f"{_ts()}"
    )
    _send(msg)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
