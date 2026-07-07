"""
EU Mean Reversion Bot — Executor (10:00 UTC)
Reads eu-candidates.json, computes sizing from directives, writes trade
requests to nwt_tickets. Does NOT place orders — that is the Execution Engine's role.

Capital base: $20,000 allocated to EU bot.
time_in_force: 'gtc' (EU instruments, multi-day holds).
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
CANDIDATES_FILE = SHARED_DIR / "eu-candidates.json"
DIRECTIVES_FILE = SHARED_DIR / "master-directives.json"

load_dotenv(BOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EU-EXEC] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("eu_executor")

EU_CAPITAL_BASE = 20_000.0  # architecture spec: $20k allocated to EU bot


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
                (level, "EU_EXECUTOR", message, json.dumps(payload) if payload else None),
            )
        conn.commit()
    except Exception as exc:
        log.warning("DB log failed: %s", exc)


def write_ticket(conn, payload: dict) -> str:
    """Insert a TRADE_REQUEST ticket and return the ticket_id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (from_agent, to_agent, type, payload) "
            "VALUES (%s, %s, %s, %s) RETURNING ticket_id",
            ("EU_EXECUTOR", "EXECUTION_ENGINE", "TRADE_REQUEST", json.dumps(payload)),
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
    log.info("EU executor starting (10:00 UTC)")

    # Step 1: Directives gate
    directives = load_directives()

    regime = directives.get("regime", {})
    if not isinstance(regime, dict):
        raise RuntimeError(f"regime must be dict (JSONB), got {type(regime)}")

    if directives.get("global_kill_switch", True):
        log.info("Global kill switch active — EU executor exiting without tickets")
        return

    eu_perm = directives.get("bot_permissions", {}).get("eu", {})
    if eu_perm.get("status") == "paused":
        log.info("EU bot status=paused — executor exiting without tickets")
        return

    capital_weight = float(eu_perm.get("capital_weight", 0.0))
    size_cap = float(eu_perm.get("size_cap", 0.0))

    if capital_weight <= 0 or size_cap <= 0:
        log.info("EU capital_weight=%.2f or size_cap=%.2f is zero — no tickets", capital_weight, size_cap)
        return

    # Step 2: Read candidates
    if not CANDIDATES_FILE.exists():
        log.info("eu-candidates.json not found — no candidates to process")
        return

    with open(CANDIDATES_FILE) as f:
        candidates = json.load(f)

    if not candidates:
        log.info("eu-candidates.json is empty — no tickets to write")
        return

    # Step 3: DB connect
    conn = None
    try:
        conn = get_db_conn()
    except Exception as exc:
        log.error("DB connection failed: %s — cannot write tickets", exc)
        sys.exit(1)

    # Step 4: Size and submit each candidate as a ticket
    tickets_written = 0
    for candidate in candidates:
        try:
            confidence = float(candidate.get("confidence", 0.5))

            # sized_notional = base_capital * capital_weight * size_cap * confidence
            sized_notional = EU_CAPITAL_BASE * capital_weight * size_cap * confidence
            sized_notional = round(sized_notional, 2)

            ticket_payload = {
                "approved": True,  # directives gate (kill switch / status / sizing) already passed above
                # Signal fields (passed through from strategist)
                "bot": candidate["bot"],
                "symbol": candidate["symbol"],
                "direction": candidate["direction"],
                "confidence": confidence,
                "strategy_id": candidate["strategy_id"],
                "signal_quality": candidate.get("signal_quality", {}),
                "expected_payoff": candidate.get("expected_payoff", {}),
                "rationale": candidate.get("rationale", ""),
                "generated_at": candidate.get("generated_at"),
                # Sizing fields (added by executor)
                "bot_source": "EU_BOT",
                "asset_type": "equity",
                "sized_notional": sized_notional,
                "capital_weight": capital_weight,
                "size_cap": size_cap,
                "time_in_force": "gtc",  # EU: multi-day, GTC required
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
            log_to_db(conn, "ERROR", f"Ticket write failed for {candidate.get('symbol')}: {exc}")

    log_to_db(conn, "INFO", f"EU executor wrote {tickets_written} ticket(s)", {
        "tickets": tickets_written,
        "capital_weight": capital_weight,
        "size_cap": size_cap,
    })
    log.info("EU executor done — %d ticket(s) written", tickets_written)
    conn.close()


if __name__ == "__main__":
    main()
