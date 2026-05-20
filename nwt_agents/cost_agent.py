"""
nwt_agents/cost_agent.py
Runs at 21:00 UTC alongside learning_agent.
Tracks token usage and estimates API costs from nwt_system_log.

Writes:
  - cost_summary.json to NWT_AGENTS_DIR
  - nwt_system_log entry (level=INFO, component=cost_agent)
"""

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import get_db, log_system_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("cost_agent")

AGENTS_DIR = Path(os.environ.get("NWT_AGENTS_DIR", Path(__file__).parent))

# Cost per 1M tokens (USD)
HAIKU_INPUT_COST_PER_M = 0.80
HAIKU_OUTPUT_COST_PER_M = 4.00
SONNET_INPUT_COST_PER_M = 3.00
SONNET_OUTPUT_COST_PER_M = 15.00


def fetch_token_usage_today(conn) -> dict:
    """
    Sum token usage from nwt_system_log entries for today.
    Looks for payload containing 'tokens_used' dict.
    """
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)

    totals = {
        "haiku_in": 0,
        "haiku_out": 0,
        "sonnet_in": 0,
        "sonnet_out": 0,
    }

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT payload, component FROM nwt_system_log
            WHERE payload IS NOT NULL
              AND created_at >= %s
            """,
            (today_start,),
        )
        rows = cur.fetchall()

    for row in rows:
        payload = row["payload"]
        if not isinstance(payload, dict):
            try:
                payload = json.loads(payload)
            except Exception:
                continue

        tokens_used = payload.get("tokens_used")
        if not isinstance(tokens_used, dict):
            continue

        totals["haiku_in"] += tokens_used.get("haiku_in", 0)
        totals["haiku_out"] += tokens_used.get("haiku_out", 0)
        totals["sonnet_in"] += tokens_used.get("sonnet_in", 0)
        totals["sonnet_out"] += tokens_used.get("sonnet_out", 0)

    return totals


def compute_costs(totals: dict) -> dict:
    """
    Estimate USD cost from token counts.
    """
    haiku_cost = (
        totals["haiku_in"] / 1_000_000 * HAIKU_INPUT_COST_PER_M
        + totals["haiku_out"] / 1_000_000 * HAIKU_OUTPUT_COST_PER_M
    )
    sonnet_cost = (
        totals["sonnet_in"] / 1_000_000 * SONNET_INPUT_COST_PER_M
        + totals["sonnet_out"] / 1_000_000 * SONNET_OUTPUT_COST_PER_M
    )
    total_cost = haiku_cost + sonnet_cost

    return {
        "haiku_cost_usd": round(haiku_cost, 6),
        "sonnet_cost_usd": round(sonnet_cost, 6),
        "total_cost_usd": round(total_cost, 6),
    }


def fetch_cumulative_costs(conn) -> dict:
    """Sum all historical token usage from nwt_system_log."""
    totals = {
        "haiku_in": 0,
        "haiku_out": 0,
        "sonnet_in": 0,
        "sonnet_out": 0,
    }

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT payload FROM nwt_system_log WHERE payload IS NOT NULL"
        )
        rows = cur.fetchall()

    for row in rows:
        payload = row["payload"]
        if not isinstance(payload, dict):
            try:
                payload = json.loads(payload)
            except Exception:
                continue
        tokens_used = payload.get("tokens_used")
        if not isinstance(tokens_used, dict):
            continue
        totals["haiku_in"] += tokens_used.get("haiku_in", 0)
        totals["haiku_out"] += tokens_used.get("haiku_out", 0)
        totals["sonnet_in"] += tokens_used.get("sonnet_in", 0)
        totals["sonnet_out"] += tokens_used.get("sonnet_out", 0)

    return totals


def main() -> None:
    conn = get_db()

    try:
        today_tokens = fetch_token_usage_today(conn)
        today_costs = compute_costs(today_tokens)

        cumulative_tokens = fetch_cumulative_costs(conn)
        cumulative_costs = compute_costs(cumulative_tokens)

        summary = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "date": date.today().isoformat(),
            "today": {
                "tokens": today_tokens,
                "estimated_cost_usd": today_costs,
            },
            "cumulative": {
                "tokens": cumulative_tokens,
                "estimated_cost_usd": cumulative_costs,
            },
            "cost_rates": {
                "haiku_input_per_m": HAIKU_INPUT_COST_PER_M,
                "haiku_output_per_m": HAIKU_OUTPUT_COST_PER_M,
                "sonnet_input_per_m": SONNET_INPUT_COST_PER_M,
                "sonnet_output_per_m": SONNET_OUTPUT_COST_PER_M,
            },
        }

        out_path = AGENTS_DIR / "cost_summary.json"
        with open(out_path, "w") as f:
            json.dump(summary, f, indent=2)

        logger.info(
            "Cost summary: today=$%.4f (haiku=$%.4f, sonnet=$%.4f) | cumulative=$%.4f",
            today_costs["total_cost_usd"],
            today_costs["haiku_cost_usd"],
            today_costs["sonnet_cost_usd"],
            cumulative_costs["total_cost_usd"],
        )

        log_system_event(
            conn,
            "INFO",
            "cost_agent",
            f"Daily cost: ${today_costs['total_cost_usd']:.4f} | Cumulative: ${cumulative_costs['total_cost_usd']:.4f}",
            summary,
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
