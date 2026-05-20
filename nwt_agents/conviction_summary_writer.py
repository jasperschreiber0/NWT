"""
nwt_agents/conviction_summary_writer.py
Runs at 13:45 UTC. Reads conviction_tickets.json, writes human-readable conviction_summary.txt.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from shared_context import get_db, load_conviction_tickets, load_master_directives, log_system_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("conviction_summary_writer")

AGENTS_DIR = Path(os.environ.get("NWT_AGENTS_DIR", Path(__file__).parent))


def format_summary(tickets: list, directives: dict) -> str:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    regime = directives.get("regime", {})
    primary = regime.get("primary_regime", "unknown")
    confidence = regime.get("confidence", 0.0)
    transition_risk = regime.get("transition_risk", 0.0)
    secondary = regime.get("secondary_regime") or "none"
    vix = directives.get("vix") or "n/a"

    lines = [
        "NorthWorld Trading — Conviction Summary",
        f"Date: {now_str}",
        f"Regime: {primary} (confidence: {confidence:.2f}, transition_risk: {transition_risk:.2f})",
        f"Secondary: {secondary}",
        f"VIX: {vix}",
        f"Total conviction tickets: {len(tickets)}",
        "",
    ]

    if not tickets:
        lines.append("No conviction tickets generated this session.")
        lines.append("")
        lines.append("Reason: No symbols survived prescreening or no prescreened symbols generated edge.")
        return "\n".join(lines)

    for i, ticket in enumerate(tickets, 1):
        sq = ticket.get("signal_quality", {})
        override = ticket.get("override_note", "")

        lines.append(f"--- TICKET {i} ---")
        lines.append(
            f"Symbol: {ticket.get('symbol')} | "
            f"Strategy: {ticket.get('strategy_type')} | "
            f"Direction: {ticket.get('direction')}"
        )
        lines.append(
            f"Conviction: {ticket.get('conviction_score')}/10 | "
            f"Confidence: {ticket.get('confidence', 0):.2f}"
        )
        lines.append(
            f"DTE target: {ticket.get('dte_target')} | "
            f"Strike: {ticket.get('strike_preference')} | "
            f"Regime alignment: {ticket.get('regime_alignment')}"
        )
        lines.append(
            f"Entry timing: {sq.get('entry_timing_score', 0):.2f} | "
            f"Expected move capture: {sq.get('expected_move_capture', 0):.2f}"
        )
        lines.append(f"Thesis: {sq.get('thesis_validity', 'n/a')}")
        lines.append(f"Rationale: {ticket.get('entry_rationale', 'n/a')}")
        if override:
            lines.append(f"[OVERRIDE] {override}")
        if ticket.get("ticket_id"):
            lines.append(f"Ticket ID: {ticket['ticket_id']}")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    conn = get_db()

    try:
        tickets = load_conviction_tickets()
        try:
            directives = load_master_directives()
        except FileNotFoundError:
            directives = {
                "regime": {"primary_regime": "neutral", "confidence": 0.5, "transition_risk": 0.0},
                "vix": None,
                "global_kill_switch": False,
            }

        summary_text = format_summary(tickets, directives)

        out_path = AGENTS_DIR / "conviction_summary.txt"
        out_path.write_text(summary_text)

        logger.info("conviction_summary.txt written (%d tickets)", len(tickets))
        log_system_event(
            conn,
            "INFO",
            "conviction_summary_writer",
            f"Summary written: {len(tickets)} tickets",
            {"ticket_symbols": [t.get("symbol") for t in tickets]},
        )

        # Print to stdout for cron log visibility
        print(summary_text, flush=True)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
