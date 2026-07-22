"""
nwt_agents/system_health.py
Operational visibility: one script answering "what's actually going on"
instead of grepping logs and running ad-hoc psql queries by hand (the
process this codebase's own incident history shows takes hours). Every
safety lock is reported with WHAT/WHY/WHEN, not just a boolean flag.

Run: nwt_agents/system_health.py --report
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import get_db


def _age(ts) -> str:
    if ts is None:
        return "unknown"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() / 60)}m ago"
    if hours < 48:
        return f"{hours:.1f}h ago"
    return f"{hours / 24:.1f}d ago"


def build_report(conn) -> str:
    lines = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:

        # --- no_trade_mode: WHAT/WHY/WHEN, not just a boolean ---
        cur.execute("SELECT value, reason, set_by, updated_at FROM nwt_system_flags WHERE flag='no_trade_mode'")
        flag = cur.fetchone()
        lines.append("=" * 70)
        if flag and flag["value"]:
            lines.append("NO_TRADE_MODE: ACTIVE")
            lines.append(f"  Reason: {flag['reason']}")
            lines.append(f"  Set by: {flag['set_by']}")
            lines.append(f"  Since:  {flag['updated_at']} ({_age(flag['updated_at'])})")
        else:
            lines.append("NO_TRADE_MODE: clear")
        lines.append("=" * 70)

        # --- Open positions by lifecycle_state ---
        cur.execute(
            "SELECT lifecycle_state, COUNT(*) AS n FROM nwt_portfolio_ledger "
            "WHERE status='open' GROUP BY lifecycle_state ORDER BY n DESC"
        )
        rows = cur.fetchall()
        lines.append("\nOPEN POSITIONS BY LIFECYCLE STATE:")
        if not rows:
            lines.append("  (none)")
        for r in rows:
            flag_str = "  <-- needs attention" if r["lifecycle_state"] in ("RECON_PENDING", "RECONCILING", "UNKNOWN") else ""
            lines.append(f"  {r['lifecycle_state']:<16} {r['n']}{flag_str}")

        # --- Unresolved broker-only positions ---
        cur.execute(
            "SELECT symbol, qty, side, avg_price, first_seen_at FROM nwt_unknown_broker_positions "
            "WHERE NOT resolved ORDER BY first_seen_at"
        )
        rows = cur.fetchall()
        lines.append("\nUNRESOLVED BROKER-ONLY POSITIONS (in Alpaca, no ledger record, no auto-match found):")
        if not rows:
            lines.append("  (none)")
        for r in rows:
            lines.append(f"  {r['symbol']}: qty={r['qty']} {r['side']} avg_price={r['avg_price']} "
                         f"first_seen={_age(r['first_seen_at'])}")

        # --- Expired-but-still-open (should always be zero) ---
        cur.execute(
            "SELECT position_id, asset, entry_time FROM nwt_portfolio_ledger "
            "WHERE status='open' AND asset_type='option'"
        )
        rows = cur.fetchall()
        from shared_context import option_dte
        overdue = [r for r in rows if (option_dte(r["asset"]) or 0) < 0]
        lines.append(f"\nEXPIRED OPTIONS STILL OPEN: {len(overdue)} (should always be 0 — expiry_sweeper/Rule 12 gap if not)")
        for r in overdue:
            lines.append(f"  {r['asset']} (position_id={r['position_id']}) entered {_age(r['entry_time'])}")

        # --- Failed executions, last 24h ---
        cur.execute(
            "SELECT action, COUNT(*) AS n FROM nwt_execution_history "
            "WHERE error_state IS NOT NULL AND submitted_at > NOW() - INTERVAL '24 hours' "
            "GROUP BY action ORDER BY n DESC"
        )
        rows = cur.fetchall()
        lines.append("\nFAILED EXECUTION ATTEMPTS (last 24h):")
        if not rows:
            lines.append("  (none)")
        for r in rows:
            lines.append(f"  {r['action']}: {r['n']}")

        # --- Stale / stuck in-flight close orders ---
        cur.execute(
            """
            SELECT id, ticket_id, position_id, payload, alpaca_order_id, created_at, stale_since
            FROM nwt_inflight_orders
            WHERE kind = 'close' AND status = 'pending'
              AND created_at < NOW() - INTERVAL '30 minutes'
            ORDER BY created_at
            """
        )
        rows = cur.fetchall()
        lines.append("\nSTALE/STUCK IN-FLIGHT CLOSE ORDERS:")
        if not rows:
            lines.append("  (none)")
        for r in rows:
            payload = r["payload"] or {}
            symbol = payload.get("symbol", "?")
            age = _age(r["created_at"])
            flag = " [cancel already attempted]" if r["stale_since"] else ""
            lines.append(
                f"  ALERT: {symbol} close order stuck\n"
                f"    Position: {r['position_id']}\n"
                f"    Order:    {r['alpaca_order_id']}\n"
                f"    Age:      {age}{flag}\n"
                f"    Reason:   Broker accepted order but no fill\n"
                f"    Required action: reconcile / retry close"
            )

        # --- Force-close state summary ---
        cur.execute(
            "SELECT state, COUNT(*) AS n, MAX(attempt_count) AS max_attempts "
            "FROM nwt_force_close_state GROUP BY state ORDER BY n DESC"
        )
        rows = cur.fetchall()
        lines.append("\nFORCE-CLOSE STATE MACHINE:")
        if not rows:
            lines.append("  (no force-close history)")
        for r in rows:
            lines.append(f"  {r['state']:<20} {r['n']:<4} (max attempts seen: {r['max_attempts']})")

        # --- Stuck tickets (pending >30 min, no decision) ---
        cur.execute(
            "SELECT type, COUNT(*) AS n FROM nwt_tickets t "
            "WHERE t.created_at < NOW() - INTERVAL '30 minutes' "
            "AND NOT EXISTS (SELECT 1 FROM nwt_ticket_decisions d WHERE d.ticket_id = t.ticket_id) "
            "GROUP BY type ORDER BY n DESC"
        )
        rows = cur.fetchall()
        lines.append("\nSTUCK TICKETS (>30min, no decision):")
        if not rows:
            lines.append("  (none)")
        for r in rows:
            lines.append(f"  {r['type']}: {r['n']}")

        # --- Recent recon outcome ---
        cur.execute(
            "SELECT type, payload, created_at FROM nwt_tickets WHERE from_agent='RECON_AGENT' "
            "AND type IN ('recon_ok', 'recon_mismatch') ORDER BY created_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        lines.append("\nLAST RECON:")
        if row:
            lines.append(f"  {row['type']} at {row['created_at']} ({_age(row['created_at'])})")
        else:
            lines.append("  (no recon has ever run — recon_agent may not be scheduled)")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()

    conn = get_db()
    try:
        print(build_report(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
