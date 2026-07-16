"""
US Flow Bot — Executor (18:10 UTC, 5 minutes after the ORB signal generator)
Reads us-candidates.json, computes sizing from directives, writes trade
requests to nwt_tickets. Does NOT place orders — that is the Execution
Engine's role.

This closes the gap where us-candidates.json was written by
trade_1400_with_brackets.py but nothing ever converted it into
TRADE_REQUEST tickets — the US bot (the largest capital allocation) was a
signal generator whose signals dead-ended on disk.

Capital base: $35,000 allocated to US bot.
time_in_force: 'day' (intraday ORB entries — never carried by GTC;
architecture rule: day orders for US only).
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
BOT_DIR = Path(__file__).parent
SHARED_DIR = BOT_DIR.parent / "shared"
CANDIDATES_FILE = SHARED_DIR / "us-candidates.json"
DIRECTIVES_FILE = SHARED_DIR / "master-directives.json"

load_dotenv(BOT_DIR / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [US-EXEC] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("us_executor")

US_CAPITAL_BASE = 35_000.0  # architecture spec: $35k allocated to US bot


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def get_db_conn():
    return psycopg2.connect(os.environ["NWT_DB_DSN"])


def log_to_db(conn, level: str, message: str, payload: dict | None = None) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO nwt_system_log (level, component, message, payload) "
                "VALUES (%s, %s, %s, %s)",
                (level, "US_EXECUTOR", message, json.dumps(payload) if payload else None),
            )
        conn.commit()
    except Exception as exc:
        log.warning("DB log failed: %s", exc)


def write_ticket(conn, payload: dict) -> str:
    """Insert a TRADE_REQUEST ticket. Returns ticket_id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (from_agent, to_agent, type, payload) "
            "VALUES (%s, %s, %s, %s) RETURNING ticket_id",
            ("US_EXECUTOR", "EXECUTION_ENGINE", "TRADE_REQUEST", json.dumps(payload)),
        )
        ticket_id = str(cur.fetchone()[0])
    conn.commit()
    return ticket_id


# ---------------------------------------------------------------------------
# Directives
# ---------------------------------------------------------------------------
def load_directives() -> dict:
    if not DIRECTIVES_FILE.exists():
        log.warning("master-directives.json missing — defaulting to kill switch on")
        return {"global_kill_switch": True}
    with open(DIRECTIVES_FILE) as f:
        return json.load(f)


def _candidates_are_fresh(candidates: list) -> bool:
    """
    Only submit candidates generated TODAY (UTC). The ORB thesis is intraday;
    a stale us-candidates.json left over from a previous session must never
    become an order the next day.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    for c in candidates:
        generated_at = c.get("generated_at") or ""
        if not generated_at.startswith(today):
            return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("US executor starting (18:10 UTC)")

    # Step 1: Directives gate
    directives = load_directives()

    regime = directives.get("regime", {})
    if not isinstance(regime, dict):
        raise RuntimeError(f"regime must be dict (JSONB), got {type(regime)}")

    if directives.get("global_kill_switch", True):
        log.info("Global kill switch active — US executor exiting without tickets")
        return

    us_perm = directives.get("bot_permissions", {}).get("us", {})
    if us_perm.get("status") == "paused":
        log.info("US bot status=paused — executor exiting without tickets")
        return

    capital_weight = float(us_perm.get("capital_weight", 0.0))
    size_cap = float(us_perm.get("size_cap", 0.0))

    if capital_weight <= 0 or size_cap <= 0:
        log.info("US capital_weight=%.2f or size_cap=%.2f is zero — no tickets", capital_weight, size_cap)
        return

    # Step 2: Read candidates
    if not CANDIDATES_FILE.exists():
        log.info("us-candidates.json not found — no candidates to process")
        return

    with open(CANDIDATES_FILE) as f:
        candidates = json.load(f)

    if not candidates:
        log.info("us-candidates.json is empty — no tickets to write")
        return

    if not _candidates_are_fresh(candidates):
        log.warning("us-candidates.json contains candidates not generated today — refusing "
                    "to submit stale intraday signals")
        return

    # Step 3: DB connect
    conn = None
    try:
        conn = get_db_conn()
    except Exception as exc:
        log.error("DB connection failed: %s — cannot write tickets", exc)
        sys.exit(1)

    # Step 4: Size and submit
    tickets_written = 0
    for candidate in candidates:
        # Hard guard: US Track A submits equities only (Track C handles the
        # shared account's options via the nwt_agents stack)
        if candidate.get("asset_type", "equity") == "option":
            log.error("US executor received an options candidate for %s — rejected. "
                      "Track A US bot is equities only.", candidate.get("symbol"))
            log_to_db(conn, "ERROR", f"US options candidate rejected: {candidate.get('symbol')}")
            continue

        try:
            confidence = float(candidate.get("confidence", 0.5))
            sized_notional = US_CAPITAL_BASE * capital_weight * size_cap * confidence
            sized_notional = round(sized_notional, 2)

            expected_payoff = candidate.get("expected_payoff", {})
            ticket_payload = {
                "approved": True,  # directives gate (kill switch / status / sizing) already passed above
                "bot": candidate["bot"],
                "symbol": candidate["symbol"],
                "direction": candidate["direction"],
                "confidence": confidence,
                "strategy_id": candidate["strategy_id"],
                "signal_quality": candidate.get("signal_quality", {}),
                "expected_payoff": expected_payoff,
                "rationale": candidate.get("rationale", ""),
                "generated_at": candidate.get("generated_at"),
                "bot_source": "US_BOT",
                "asset_type": "equity",
                "sized_notional": sized_notional,
                "capital_weight": capital_weight,
                "size_cap": size_cap,
                "time_in_force": "day",  # US: intraday ORB — never GTC
                "stop_pct": expected_payoff.get("stop_pct"),
                "target_pct": expected_payoff.get("target_pct"),
                "regime_at_submission": regime,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }

            ticket_id = write_ticket(conn, ticket_payload)
            log.info(
                "Ticket written: %s %s %s sized_notional=$%.0f ticket_id=%s",
                candidate["symbol"], candidate["direction"], candidate["strategy_id"],
                sized_notional, ticket_id,
            )
            tickets_written += 1

        except Exception as exc:
            log.error("Failed to write ticket for %s: %s", candidate.get("symbol"), exc)
            log_to_db(conn, "ERROR", f"US ticket write failed for {candidate.get('symbol')}: {exc}")

    log_to_db(conn, "INFO", f"US executor wrote {tickets_written} ticket(s)", {
        "tickets": tickets_written,
        "capital_weight": capital_weight,
        "size_cap": size_cap,
    })
    log.info("US executor done — %d ticket(s) written", tickets_written)
    conn.close()


if __name__ == "__main__":
    main()
