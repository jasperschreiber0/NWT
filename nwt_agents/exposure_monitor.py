"""
nwt_agents/exposure_monitor.py
Read-only visibility report over directional exposure — UNATTRIBUTED legacy
positions vs bot-controlled exposure vs combined account-wide exposure.

Monitoring only. Never trades, never touches no_trade_mode, and never calls
or modifies check_directional_cap() (execution/engine.py) or any sizing/
execution/strategy logic.

Context: check_directional_cap() was changed (2026-08-05) to exclude
UNATTRIBUTED rows from the bot-facing cap, specifically so legacy exposure
could no longer silently block trading. That trade-off was real: total
account directional exposure stopped being gated as one combined number.
This script is the visibility half of that trade-off — it reports the same
combined figure the cap used to enforce, so legacy exposure can't become
invisible risk just because it stopped being a blocker.

Writes one nwt_tickets row per run (type='exposure_report') and, on either
alert condition, a WARNING nwt_system_log row plus a Telegram alert.
Absence of a report is itself detectable, same pattern as recon_agent.py's
recon_ok ticket.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import clean_alpaca_base_url, get_db, insert_ticket, log_system_event  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("exposure_monitor")

ALPACA_BASE_URL = clean_alpaca_base_url(os.environ.get("NWT_ALPACA_BASE_URL", "https://paper-api.alpaca.markets"))
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("NWT_ALPACA_KEY_ID", ""),
    "APCA-API-SECRET-KEY": os.environ.get("NWT_ALPACA_SECRET_KEY", ""),
}

# Reporting thresholds only — never consulted by check_directional_cap() or
# any order-placement path. Changing these changes what gets reported, not
# what trades.
COMBINED_EXPOSURE_ALERT_PCT = 0.90
UNATTRIBUTED_AGE_ALERT_DAYS = 30


def get_account_equity() -> float:
    resp = requests.get(f"{ALPACA_BASE_URL}/v2/account", headers=ALPACA_HEADERS, timeout=15)
    resp.raise_for_status()
    return float(resp.json()["equity"])


def get_unattributed_positions(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT position_id, asset, direction, notional_risk, entry_time
            FROM nwt_portfolio_ledger
            WHERE status = 'open' AND bot_source = 'UNATTRIBUTED'
            ORDER BY entry_time
            """
        )
        rows = [dict(r) for r in cur.fetchall()]

    now = datetime.now(timezone.utc)
    positions = []
    for r in rows:
        entry_time = r["entry_time"]
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)
        positions.append({
            "position_id": str(r["position_id"]),
            "symbol": r["asset"],
            "direction": r["direction"],
            "notional_risk": float(r["notional_risk"] or 0),
            "age_days": (now - entry_time).days,
        })
    return positions


def get_long_exposure_by_source(conn) -> tuple:
    """
    Returns (bot_long, unattributed_long). bot_long mirrors
    check_directional_cap()'s own existing-exposure sum exactly (status=
    'open', direction='long', bot_source != 'UNATTRIBUTED') — a read-only
    duplicate of that query for reporting, not a call into it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COALESCE(SUM(notional_risk) FILTER (WHERE bot_source != 'UNATTRIBUTED'), 0) AS bot_long,
              COALESCE(SUM(notional_risk) FILTER (WHERE bot_source = 'UNATTRIBUTED'), 0) AS unattributed_long
            FROM nwt_portfolio_ledger
            WHERE status = 'open' AND direction = 'long'
            """
        )
        bot_long, unattributed_long = cur.fetchone()
    return float(bot_long), float(unattributed_long)


def build_report(conn) -> dict:
    equity = get_account_equity()
    unattributed_positions = get_unattributed_positions(conn)
    bot_long, unattributed_long = get_long_exposure_by_source(conn)
    combined_long = bot_long + unattributed_long

    unattributed_total_notional = sum(p["notional_risk"] for p in unattributed_positions)
    max_age_days = max((p["age_days"] for p in unattributed_positions), default=0)

    bot_pct = (bot_long / equity) if equity > 0 else 0.0
    combined_pct = (combined_long / equity) if equity > 0 else 0.0

    alerts = []
    if combined_pct > COMBINED_EXPOSURE_ALERT_PCT:
        alerts.append(
            f"Combined directional exposure {combined_pct:.1%} of equity exceeds "
            f"{COMBINED_EXPOSURE_ALERT_PCT:.0%} threshold "
            f"(bot {bot_long:.0f} + unattributed {unattributed_long:.0f} = {combined_long:.0f} "
            f"of {equity:.0f} equity)"
        )
    if max_age_days > UNATTRIBUTED_AGE_ALERT_DAYS:
        symbols = sorted({p["symbol"] for p in unattributed_positions})
        alerts.append(
            f"UNATTRIBUTED exposure open {max_age_days} days "
            f"(> {UNATTRIBUTED_AGE_ALERT_DAYS}-day threshold): {symbols}"
        )

    return {
        "equity": equity,
        "unattributed": {
            "total_notional": unattributed_total_notional,
            "symbols": sorted({p["symbol"] for p in unattributed_positions}),
            "positions": unattributed_positions,
            "max_age_days": max_age_days,
        },
        "bot_controlled": {
            "total_long_notional": bot_long,
            "pct_of_equity": bot_pct,
        },
        "combined": {
            "total_long_notional": combined_long,
            "pct_of_equity": combined_pct,
        },
        "alerts": alerts,
    }


def run(conn) -> dict:
    report = build_report(conn)
    insert_ticket(conn, "EXPOSURE_MONITOR", "SYSTEM", "exposure_report", report)

    if report["alerts"]:
        message = "; ".join(report["alerts"])
        logger.warning("Exposure alert: %s", message)
        log_system_event(conn, "WARNING", "exposure_monitor", message, report)
        try:
            from notifier import alert_exposure
            alert_exposure(report["alerts"], report)
        except Exception:
            pass
    else:
        logger.info(
            "Exposure report clean — bot=%.0f (%.1f%% equity), unattributed=%.0f, "
            "combined=%.1f%% equity=%.0f",
            report["bot_controlled"]["total_long_notional"],
            report["bot_controlled"]["pct_of_equity"] * 100,
            report["unattributed"]["total_notional"],
            report["combined"]["pct_of_equity"] * 100,
            report["equity"],
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="NWT directional exposure visibility report (read-only, never blocks trading)")
    parser.add_argument("--gate", action="store_true", help="exit 1 if any alert condition is currently tripped")
    args = parser.parse_args()

    conn = get_db()
    try:
        report = run(conn)
    finally:
        conn.close()

    if args.gate and report["alerts"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
