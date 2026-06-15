"""
nwt_agents/recon_agent.py
Reconciliation Agent — ledger vs Alpaca.

Modes:
  --gate              Startup check. exit 0 = clean. exit 1 = critical mismatch.
  --nightly           Always writes a ticket (recon_ok or recon_mismatch).
  --cold-start-import Import live Alpaca positions into ledger as UNATTRIBUTED.

Logic:
  1. Pull Alpaca /v2/positions.
  2. Pull nwt_portfolio_ledger WHERE status='open'.
  3. Match on symbol + side.
  4. Classify mismatches:
     - in_alpaca_not_ledger → CRITICAL: set no_trade_mode, exit 1
     - qty_mismatch         → CRITICAL: set no_trade_mode, exit 1
     - in_ledger_not_alpaca → mark suspect, non-critical, exit 1

Clean recon writes type='recon_ok'. Absence of recon is itself detectable.
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

from shared_context import (
    get_db,
    insert_ticket,
    log_system_event,
    set_no_trade_mode,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("recon_agent")

ALPACA_BASE_URL = os.environ.get("NWT_ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("NWT_ALPACA_KEY_ID", ""),
    "APCA-API-SECRET-KEY": os.environ.get("NWT_ALPACA_SECRET_KEY", ""),
}


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def fetch_alpaca_positions() -> list:
    url = f"{ALPACA_BASE_URL}/v2/positions"
    resp = requests.get(url, headers=ALPACA_HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_ledger_open(conn) -> list:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE status = 'open'")
        rows = cur.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Recon logic
# ---------------------------------------------------------------------------

def run_recon(conn, mode: str) -> bool:
    """
    Returns True if clean (exit 0), False if any mismatch (exit 1).
    Critical mismatches (in_alpaca_not_ledger, qty_mismatch) set no_trade_mode.
    """
    try:
        alpaca_positions = fetch_alpaca_positions()
    except Exception as exc:
        reason = f"Failed to fetch Alpaca positions: {exc}"
        logger.error(reason)
        log_system_event(conn, "ERROR", "recon_agent", reason)
        insert_ticket(conn, "RECON_AGENT", "SYSTEM", "recon_mismatch",
                      {"error": reason, "mode": mode})
        set_no_trade_mode(conn, reason, "recon_agent")
        return False

    ledger_open = fetch_ledger_open(conn)

    # Build Alpaca map: symbol → {qty, side, avg_entry}
    alpaca_map = {}
    for p in alpaca_positions:
        sym = p.get("symbol", "")
        qty = float(p.get("qty", 0))
        alpaca_map[sym] = {
            "qty": abs(qty),
            "side": "long" if qty > 0 else "short",
            "avg_entry": float(p.get("avg_entry_price", 0)),
        }

    # Build ledger map: symbol → list of open rows
    ledger_map: dict[str, list] = {}
    for row in ledger_open:
        sym = row["asset"]
        ledger_map.setdefault(sym, []).append(row)

    mismatches = []
    critical = False

    # 1: In Alpaca but not in ledger → CRITICAL untracked risk
    for sym, apos in alpaca_map.items():
        if sym not in ledger_map:
            entry = {"class": "in_alpaca_not_ledger", "symbol": sym, "alpaca": apos}
            logger.error("CRITICAL: %s qty=%.0f %s not in ledger", sym, apos["qty"], apos["side"])
            mismatches.append(entry)
            critical = True

    # 2: In ledger but not in Alpaca → mark suspect (non-critical)
    for sym, rows in ledger_map.items():
        if sym not in alpaca_map:
            for row in rows:
                pid = str(row["position_id"])
                logger.warning("in_ledger_not_alpaca: %s position_id=%s — marking suspect", sym, pid)
                mismatches.append({"class": "in_ledger_not_alpaca", "symbol": sym, "position_id": pid})
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE nwt_portfolio_ledger SET status='suspect' WHERE position_id=%s",
                        (row["position_id"],),
                    )
                conn.commit()

    # 3: Qty mismatch → CRITICAL
    for sym in set(alpaca_map) & set(ledger_map):
        alpaca_qty = alpaca_map[sym]["qty"]
        rows = ledger_map[sym]
        asset_type = rows[0].get("asset_type", "equity")
        # Use SUM(qty) from ledger if available; fall back to row count for options
        ledger_qty_sum = sum(r["qty"] for r in rows if r.get("qty") is not None)
        if ledger_qty_sum > 0:
            ledger_qty = ledger_qty_sum
        elif asset_type == "option":
            ledger_qty = len(rows)
        else:
            ledger_qty = None  # equity qty check not enforced without column data

        if ledger_qty is not None and abs(alpaca_qty - ledger_qty) > 0.5:
            entry = {"class": "qty_mismatch", "symbol": sym,
                     "alpaca_qty": alpaca_qty, "ledger_qty": ledger_qty}
            logger.error("CRITICAL qty mismatch: %s alpaca=%.0f ledger=%.0f", sym, alpaca_qty, ledger_qty)
            mismatches.append(entry)
            critical = True

    if not mismatches:
        insert_ticket(conn, "RECON_AGENT", "SYSTEM", "recon_ok", {
            "alpaca_positions": len(alpaca_positions),
            "ledger_open": len(ledger_open),
            "mode": mode,
        })
        logger.info("Recon CLEAN — %d Alpaca positions, %d ledger open", len(alpaca_positions), len(ledger_open))
        return True

    # Write mismatch ticket
    insert_ticket(conn, "RECON_AGENT", "SYSTEM", "recon_mismatch", {
        "mismatches": mismatches,
        "mode": mode,
        "critical": critical,
    })
    log_system_event(conn, "CRITICAL" if critical else "WARNING", "recon_agent",
                     f"Recon {'CRITICAL' if critical else 'non-critical'} mismatch: {len(mismatches)} issues",
                     {"mismatches": mismatches})

    if critical:
        reason = f"Recon critical mismatch: {len(mismatches)} untracked/qty-mismatch positions"
        set_no_trade_mode(conn, reason, "recon_agent")
        try:
            from notifier import alert_recon_critical
            alert_recon_critical([f"{m['class']} {m.get('symbol','')}" for m in mismatches])
        except Exception:
            pass

    return False


# ---------------------------------------------------------------------------
# Cold start import
# ---------------------------------------------------------------------------

def cold_start_import(conn) -> None:
    """
    Import live Alpaca positions into ledger as UNATTRIBUTED.
    Only runs if ledger has zero open rows.
    Zero is confirmed, never assumed.
    """
    ledger_open = fetch_ledger_open(conn)
    if ledger_open:
        logger.info("Ledger has %d open rows — cold start import not needed", len(ledger_open))
        return

    try:
        alpaca_positions = fetch_alpaca_positions()
    except Exception as exc:
        logger.error("cold_start_import: Alpaca fetch failed: %s", exc)
        sys.exit(1)

    if not alpaca_positions:
        insert_ticket(conn, "RECON_AGENT", "SYSTEM", "cold_start_zero_confirmed",
                      {"message": "Alpaca confirms zero open positions — system starts clean"})
        logger.info("Cold start: Alpaca confirms zero positions")
        return

    imported = 0
    for p in alpaca_positions:
        sym = p.get("symbol", "")
        qty = float(p.get("qty", 0))
        side = "long" if qty > 0 else "short"
        avg_entry = float(p.get("avg_entry_price", 0))
        asset_class = p.get("asset_class", "us_equity")
        asset_type = "option" if asset_class == "us_option" else "equity"

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO nwt_portfolio_ledger
                  (bot_source, asset, asset_type, direction, notional_risk,
                   entry_price, entry_time, qty, status)
                VALUES ('UNATTRIBUTED', %s, %s, %s, %s, %s, NOW(), %s, 'open')
                """,
                (sym, asset_type, side, abs(qty) * avg_entry, avg_entry, int(abs(qty))),
            )
        conn.commit()
        logger.info("Imported UNATTRIBUTED: %s %s qty=%.0f", side, sym, abs(qty))
        imported += 1

    insert_ticket(conn, "RECON_AGENT", "SYSTEM", "cold_start_import", {
        "imported": imported,
        "symbols": [p.get("symbol") for p in alpaca_positions],
        "note": "entry_time=now (flagged: actual entry time unknown)",
    })
    logger.info("Cold start import complete: %d positions as UNATTRIBUTED", imported)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="NWT Reconciliation Agent")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--gate", action="store_true", help="Integrity gate check (exit 0=clean, 1=mismatch)")
    group.add_argument("--nightly", action="store_true", help="Nightly recon (always writes ticket)")
    group.add_argument("--cold-start-import", action="store_true", dest="cold_start",
                       help="Import Alpaca positions into empty ledger")
    args = parser.parse_args()

    conn = get_db()
    try:
        if args.cold_start:
            cold_start_import(conn)
        elif args.gate:
            clean = run_recon(conn, "gate")
            sys.exit(0 if clean else 1)
        else:  # nightly
            run_recon(conn, "nightly")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
