"""
nwt_agents/triage_agent.py
Runs 21:30 UTC daily (07:30 AEST) — after the overnight agents, learning
agent (21:00) and session_scorecard (21:15). The digest is waiting when the
operator starts their day.

MORNING OPERATOR TRIAGE. Read-only over all trading state. Zero order
authority — never places, sizes, clears flags, or edits genome. It only:
    detect -> classify -> deduplicate -> report.   Stop condition: digest emitted.

Why it exists (session_scorecard does NOT cover these):
  1. It reads EVERY component's errors in nwt_system_log — including the
     Track-A equity bots (US_ORB, US_NIGHTLY, ...) the scorecard ignores.
     A US-bot 401 was invisible to the scorecard; here it is a first-class fault.
  2. It is MARKET-CALENDAR AWARE. An integrity_gate halt because option chains
     are empty is EXPECTED on a holiday (the gate doing its job) — benign — but
     the same halt on a trading day is a real fault. Without this the correct
     holiday halts read as breakage.
  3. It has CROSS-DAY MEMORY (nwt_triage_findings). A fault that recurs every
     run is collapsed into ONE open finding with a first_seen date, so you fix
     it once instead of re-diagnosing the same error every morning.

Output: one digest via notifier (no-ops silently until Telegram is configured)
plus a persisted finding row per open fault (dashboard-visible) plus an
nwt_system_log summary line.
"""

import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

from shared_context import get_db, log_system_event, check_no_trade_mode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("triage_agent")

# How far back to scan logs. Covers a full overnight cycle (nightly 10:30 UTC ->
# ORB 18:05 UTC -> conviction/tracks). Occurrence counts are indicative, not exact.
LOOKBACK_HOURS = 26

# Components expected to leave a log line on a US trading day. If one is totally
# silent, that is a cron/deploy miss the error scan cannot see. Extend this list
# as the other Track-A bots' log component names are confirmed on the server.
TRACK_A_COMPONENTS = ["US_ORB", "US_NIGHTLY"]

# NYSE full closures 2026 — fallback only, used if the Alpaca calendar call fails.
US_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


# ---------------------------------------------------------------------------
# Market calendar — the benign/escalate discriminator
# ---------------------------------------------------------------------------

def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.environ.get("NWT_ALPACA_KEY_ID", ""),
        "APCA-API-SECRET-KEY": os.environ.get("NWT_ALPACA_SECRET_KEY", ""),
    }


def is_trading_day(day) -> tuple:
    """(is_trading, source). Alpaca /v2/calendar is authoritative; falls back to
    a weekend check + static 2026 holiday set if Alpaca is unreachable."""
    if day.weekday() >= 5:  # Sat/Sun
        return False, "weekend"
    try:
        base = os.environ["NWT_ALPACA_BASE_URL"].rstrip("/")
        resp = requests.get(
            f"{base}/v2/calendar",
            headers=_alpaca_headers(),
            params={"start": day.isoformat(), "end": day.isoformat()},
            timeout=15,
        )
        if resp.ok:
            open_dates = {d.get("date") for d in resp.json()}
            return (day.isoformat() in open_dates), "alpaca_calendar"
        logger.warning("Alpaca calendar HTTP %s — using static fallback", resp.status_code)
    except Exception as exc:
        logger.warning("Alpaca calendar unreachable (%s) — using static fallback", exc)
    return (day.isoformat() not in US_HOLIDAYS_2026), "static_fallback"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _error_class(message: str) -> str:
    """Normalize a log message into a stable error class for signature dedup."""
    m = (message or "").lower()
    if any(t in m for t in ("401", "403", "authorization required", "unauthorized", "forbidden")):
        return "auth_error"
    if ("options chain" in m and "empty" in m) or "no contracts" in m:
        return "options_chain_empty"
    if "recon" in m and ("mismatch" in m or "critical" in m):
        return "recon_mismatch"
    if "heartbeat" in m:
        return "heartbeat_lost"
    if "genome" in m:
        return "genome_missing"
    if "timeout" in m or "timed out" in m:
        return "timeout"
    if "connection" in m and any(t in m for t in ("refused", "reset", "error")):
        return "connection_error"
    return "error"


def _classify(level: str, error_class: str, trading_day: bool) -> tuple:
    """(severity, reason). severity in {'escalate', 'benign'}."""
    if error_class == "auth_error":
        return "escalate", "auth failure — not market-dependent"
    if error_class == "options_chain_empty":
        if not trading_day:
            return "benign", "market closed — gate halt expected"
        return "escalate", "options chain empty on a trading day"
    if error_class == "recon_mismatch":
        return "escalate", "ledger/Alpaca divergence — untracked risk"
    if error_class == "heartbeat_lost":
        if not trading_day:
            return "benign", "off-hours — heartbeat idle expected"
        return "escalate", "execution heartbeat lost during market hours"
    if level == "CRITICAL":
        if trading_day:
            return "escalate", "critical error on a trading day"
        return "benign", "critical on a closed session"
    return ("escalate", "error on a trading day") if trading_day else ("benign", "error on a closed session")


# ---------------------------------------------------------------------------
# Log collection
# ---------------------------------------------------------------------------

def collect_log_groups(conn, since) -> dict:
    """Group recent ERROR/CRITICAL rows by 'component:error_class' signature."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT component, level, message
            FROM nwt_system_log
            WHERE level IN ('ERROR', 'CRITICAL') AND created_at >= %s
            ORDER BY created_at DESC
            """,
            (since,),
        )
        rows = cur.fetchall()

    groups = defaultdict(lambda: {"count": 0, "sample": "", "component": "",
                                  "error_class": "", "level": ""})
    for component, level, message in rows:
        ec = _error_class(message)
        sig = f"{component}:{ec}"
        g = groups[sig]
        g["count"] += 1
        g["component"] = component
        g["error_class"] = ec
        if level == "CRITICAL" or not g["level"]:
            g["level"] = level
        if not g["sample"]:
            g["sample"] = (message or "").splitlines()[0][:160] if message else ""
    return groups


def check_silent_misses(conn, today, trading_day) -> list:
    """Track-A components that logged nothing at all on a trading day — a cron
    or deploy miss the error scan cannot detect (no error means no row)."""
    if not trading_day:
        return []
    misses = []
    for component in TRACK_A_COMPONENTS:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM nwt_system_log
                WHERE component = %s AND (created_at AT TIME ZONE 'UTC')::date = %s
                """,
                (component, today),
            )
            if cur.fetchone()[0] == 0:
                misses.append(component)
    return misses


# ---------------------------------------------------------------------------
# Cross-day dedup — nwt_triage_findings
# ---------------------------------------------------------------------------

def upsert_finding(conn, signature, component, error_class, sample, add_count) -> tuple:
    """Open a new finding or bump an existing open one. Returns
    (first_seen, total_occurrences, is_new)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, first_seen, occurrences FROM nwt_triage_findings "
            "WHERE signature = %s AND status = 'open'",
            (signature,),
        )
        existing = cur.fetchone()
        if existing:
            fid, first_seen, prior = existing
            cur.execute(
                "UPDATE nwt_triage_findings SET last_seen = NOW(), "
                "occurrences = occurrences + %s, sample_message = %s WHERE id = %s",
                (add_count, sample, fid),
            )
            result = (first_seen, prior + add_count, False)
        else:
            cur.execute(
                "INSERT INTO nwt_triage_findings "
                "(signature, component, error_class, severity, sample_message, occurrences) "
                "VALUES (%s, %s, %s, 'escalate', %s, %s) RETURNING first_seen",
                (signature, component, error_class, sample, add_count),
            )
            first_seen = cur.fetchone()[0]
            result = (first_seen, add_count, True)
    conn.commit()
    return result


def resolve_absent(conn, seen_signatures) -> list:
    """Mark open findings that did NOT recur as resolved (they recovered).
    Only called on trading days, when a fault actually had a chance to re-fire."""
    with conn.cursor() as cur:
        cur.execute("SELECT signature FROM nwt_triage_findings WHERE status = 'open'")
        open_sigs = [r[0] for r in cur.fetchall()]
        resolved = [s for s in open_sigs if s not in seen_signatures]
        for sig in resolved:
            cur.execute(
                "UPDATE nwt_triage_findings SET status = 'resolved', resolved_at = NOW() "
                "WHERE signature = %s AND status = 'open'",
                (sig,),
            )
    conn.commit()
    return resolved


# ---------------------------------------------------------------------------
# State snapshot
# ---------------------------------------------------------------------------

def gather_state(conn, today) -> dict:
    halted, reason = check_no_trade_mode(conn)
    state = {"no_trade_mode": halted, "no_trade_reason": reason}
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_portfolio_ledger WHERE status = 'open'")
        state["open_positions"] = cur.fetchone()[0]
        cur.execute("SELECT green FROM nwt_session_scorecard WHERE session_date = %s", (today,))
        row = cur.fetchone()
        state["scorecard"] = None if row is None else ("GREEN" if row[0] else "RED")
    return state


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

def build_digest(today, trading, cal_source, escalate, benign, resolved, state) -> str:
    mkt = "OPEN" if trading else "CLOSED"
    lines = [
        f"🩺 NWT Morning Triage — {today} ({today.strftime('%a')}, US market {mkt})",
        f"Overall: {'🟢 all clear' if not escalate else f'🔴 {len(escalate)} open fault(s)'}"
        f"  |  {len(benign)} benign, {len(resolved)} recovered",
        "",
    ]

    if escalate:
        lines.append("🔴 OPEN FAULTS — action needed:")
        for e in escalate:
            fs = e["first_seen"]
            fs = fs.strftime("%Y-%m-%d") if hasattr(fs, "strftime") else str(fs)
            age = "NEW today" if e["is_new"] else f"open since {fs}"
            lines.append(f"  • {e['component']} / {e['error_class']} — {age}, {e['total']}× total")
            if e.get("sample"):
                lines.append(f"      e.g. {e['sample']}")
        lines.append("")

    if benign:
        lines.append("🟢 EXPLAINED — no action (expected / holiday / off-hours):")
        for b in benign:
            lines.append(f"  • {b['component']} / {b['error_class']} ×{b['count']} — {b['reason']}")
        lines.append("")

    if resolved:
        lines.append("✅ RECOVERED since last run:")
        for sig in resolved:
            lines.append(f"  • {sig}")
        lines.append("")

    ntm = "🔴 ON" if state["no_trade_mode"] else "OFF"
    if state["no_trade_mode"] and state.get("no_trade_reason"):
        ntm += f" ({state['no_trade_reason']})"
    lines.append(
        f"State: no_trade_mode={ntm} | open positions={state['open_positions']} "
        f"| scorecard={state['scorecard'] or 'n/a'} | calendar={cal_source}"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def main() -> None:
    conn = get_db()
    now = datetime.now(timezone.utc)
    today = now.date()
    since = now - timedelta(hours=LOOKBACK_HOURS)

    try:
        trading, cal_source = is_trading_day(today)
        groups = collect_log_groups(conn, since)

        escalate, benign, seen_sigs = [], [], set()

        for sig, g in groups.items():
            severity, reason = _classify(g["level"], g["error_class"], trading)
            if severity == "escalate":
                first_seen, total, is_new = upsert_finding(
                    conn, sig, g["component"], g["error_class"], g["sample"], g["count"]
                )
                seen_sigs.add(sig)
                escalate.append({
                    "signature": sig, "component": g["component"],
                    "error_class": g["error_class"], "sample": g["sample"],
                    "first_seen": first_seen, "total": total, "is_new": is_new,
                })
            else:
                benign.append({
                    "component": g["component"], "error_class": g["error_class"],
                    "count": g["count"], "reason": reason,
                })

        for component in check_silent_misses(conn, today, trading):
            sig = f"{component}:silent_miss"
            first_seen, total, is_new = upsert_finding(
                conn, sig, component, "silent_miss", "No log output on a trading day", 1
            )
            seen_sigs.add(sig)
            escalate.append({
                "signature": sig, "component": component, "error_class": "silent_miss",
                "sample": "No log output on a trading day",
                "first_seen": first_seen, "total": total, "is_new": is_new,
            })

        # Only resolve on trading days — on a closed session a fault had no
        # chance to recur, so absence is not recovery.
        resolved = resolve_absent(conn, seen_sigs) if trading else []

        state = gather_state(conn, today)
        digest = build_digest(today, trading, cal_source, escalate, benign, resolved, state)

        log_system_event(
            conn,
            "WARNING" if escalate else "INFO",
            "triage_agent",
            f"Triage {today}: {len(escalate)} open fault(s), {len(benign)} benign, "
            f"{len(resolved)} recovered (market {'open' if trading else 'closed'})",
            {"escalate": [e["signature"] for e in escalate],
             "resolved": resolved, "trading_day": trading},
        )

        try:
            from notifier import send_triage_digest
            send_triage_digest(digest)
        except Exception as exc:
            logger.warning("notifier send failed (non-fatal): %s", exc)

        logger.info("\n%s", digest)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
