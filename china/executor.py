"""
China Policy/Event Bot — Executor (event-triggered, runs after strategist)
Reads china-candidates.json, computes sizing from directives, writes trade
requests to nwt_tickets. Does NOT place orders.

Capital base: $15,000 allocated to China bot.
time_in_force: 'day' for intraday holds, 'gtc' for multi-day (1d-3wk range).
Uses 'gtc' as default since hold period can extend to 3 weeks.
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
CANDIDATES_FILE = SHARED_DIR / "china-candidates.json"
DIRECTIVES_FILE = SHARED_DIR / "master-directives.json"

load_dotenv(BOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CHINA-EXEC] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("china_executor")

CHINA_CAPITAL_BASE = 15_000.0  # architecture spec: $15k allocated to China bot


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
                (level, "CHINA_EXECUTOR", message, json.dumps(payload) if payload else None),
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
            ("CHINA_EXECUTOR", "EXECUTION_ENGINE", "TRADE_REQUEST", json.dumps(payload)),
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("China executor starting (event-triggered)")

    # Step 1: Directives gate
    directives = load_directives()

    regime = directives.get("regime", {})
    if not isinstance(regime, dict):
        raise RuntimeError(f"regime must be dict (JSONB), got {type(regime)}")

    if directives.get("global_kill_switch", True):
        log.info("Global kill switch active — China executor exiting without tickets")
        return

    china_perm = directives.get("bot_permissions", {}).get("china", {})
    if china_perm.get("status") == "paused":
        log.info("China bot status=paused — executor exiting without tickets")
        return

    capital_weight = float(china_perm.get("capital_weight", 0.0))
    size_cap = float(china_perm.get("size_cap", 0.0))

    if capital_weight <= 0 or size_cap <= 0:
        log.info("China capital_weight=%.2f or size_cap=%.2f is zero — no tickets", capital_weight, size_cap)
        return

    # Step 2: Read candidates
    if not CANDIDATES_FILE.exists():
        log.info("china-candidates.json not found — no candidates to process")
        return

    with open(CANDIDATES_FILE) as f:
        candidates = json.load(f)

    if not candidates:
        log.info("china-candidates.json is empty — no tickets to write")
        return

    # Step 3: DB connect
    conn = None
    try:
        conn = get_db_conn()
    except Exception as exc:
        log.error("DB connection failed: %s — cannot write tickets", exc)
        sys.exit(1)

    # Step 4: Size and submit each candidate
    tickets_written = 0
    for candidate in candidates:
        try:
            confidence = float(candidate.get("confidence", 0.5))
            sized_notional = CHINA_CAPITAL_BASE * capital_weight * size_cap * confidence
            sized_notional = round(sized_notional, 2)

            # China holding period is 1 day to 3 weeks — use gtc as default
            # so position can be managed over the hold window
            ticket_payload = {
                "approved": True,  # directives gate (kill switch / status / sizing) already passed above
                "bot": candidate["bot"],
                "symbol": candidate["symbol"],
                "direction": candidate["direction"],
                "confidence": confidence,
                "strategy_id": candidate["strategy_id"],
                "signal_quality": candidate.get("signal_quality", {}),
                "expected_payoff": candidate.get("expected_payoff", {}),
                "rationale": candidate.get("rationale", ""),
                "generated_at": candidate.get("generated_at"),
                "bot_source": "CHINA_BOT",
                "asset_type": "equity",
                "sized_notional": sized_notional,
                "capital_weight": capital_weight,
                "size_cap": size_cap,
                "time_in_force": "gtc",  # China: 1d-3wk hold, gtc for multi-day positions
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
            log_to_db(conn, "ERROR", f"China ticket write failed for {candidate.get('symbol')}: {exc}")

    log_to_db(conn, "INFO", f"China executor wrote {tickets_written} ticket(s)", {
        "tickets": tickets_written,
        "capital_weight": capital_weight,
        "size_cap": size_cap,
    })
    log.info("China executor done — %d ticket(s) written", tickets_written)
    conn.close()


if __name__ == "__main__":
    main()
