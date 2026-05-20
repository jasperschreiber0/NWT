"""
nwt_agents/conviction_engine.py
Runs at 13:30 UTC. Deep conviction analysis via Claude Sonnet.

For each prescreened symbol:
  - Call Sonnet for structured options strategy proposal
  - Enforce options strategy rules deterministically in code
  - Write conviction_tickets.json
  - INSERT each approved ticket into nwt_tickets (DB)
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from shared_context import (
    get_db,
    insert_ticket,
    load_layer0_data,
    load_master_directives,
    log_inactivity,
    log_system_event,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("conviction_engine")

AGENTS_DIR = Path(os.environ.get("NWT_AGENTS_DIR", Path(__file__).parent))
SONNET_MODEL = os.environ.get("CLAUDE_SONNET_MODEL", "claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Options strategy enforcement — CODE, not LLM
# ---------------------------------------------------------------------------

VALID_STRATEGY_TYPES = {
    "long_call", "long_put", "bull_call_spread", "bear_put_spread",
    "iron_condor", "vix_calls",
}


def enforce_strategy_rules(proposed_type: str, regime: dict, iv: float, symbol: str) -> tuple[str, str | None]:
    """
    Enforce options strategy rules from CLAUDE.md.
    Returns (allowed_strategy_type, rejection_reason_or_None).
    If the proposed strategy is not allowed, returns (correct_strategy, reason).
    """
    primary = regime.get("primary_regime", "neutral")
    secondary = regime.get("secondary_regime") or ""

    # Determine allowed strategy based on regime + IV
    if primary == "risk_on" and iv < 0.30:
        allowed = "long_call"
        rule = "risk_on + IV<30 → long_call"
    elif primary == "risk_on" and iv > 0.50:
        allowed = "bull_call_spread"
        rule = "risk_on + IV>50 → bull_call_spread"
    elif primary == "risk_off" and iv < 0.30:
        allowed = "long_put"
        rule = "risk_off + IV<30 → long_put"
    elif primary == "risk_off" and iv > 0.50:
        allowed = "bear_put_spread"
        rule = "risk_off + IV>50 → bear_put_spread"
    elif primary == "neutral":
        allowed = "iron_condor"
        rule = "neutral → iron_condor"
    elif primary == "geopolitical_stress" and iv > 0.40:
        allowed = "vix_calls"
        rule = "geopolitical_stress + IV>40 → vix_calls"
    elif primary == "recession_fear" and iv < 0.35:
        allowed = "long_put"
        rule = f"recession_fear + IV<35 → long_put SPY (symbol={symbol})"
    else:
        # Intermediate case: use what was proposed if it's in valid set
        if proposed_type in VALID_STRATEGY_TYPES:
            return proposed_type, None
        return "iron_condor", f"No regime rule matched for {primary}/IV={iv:.2f}, defaulted to iron_condor"

    if proposed_type != allowed:
        return allowed, f"Strategy overridden: proposed={proposed_type} → required={allowed} ({rule})"
    return allowed, None


# ---------------------------------------------------------------------------
# Sonnet conviction call
# ---------------------------------------------------------------------------

def build_sonnet_prompt(symbol: str, layer0_sym: dict, vix: float, regime: dict) -> str:
    return f"""You are an options strategy conviction engine for a systematic trading system.

Market context:
- Regime: {regime.get('primary_regime', 'unknown')} (confidence: {regime.get('confidence', 0):.2f}, transition_risk: {regime.get('transition_risk', 0):.2f})
- Secondary regime: {regime.get('secondary_regime', 'none')}
- VIX: {vix:.1f}

Symbol: {symbol}
- Price: {layer0_sym.get('price', 0):.2f}
- 5d momentum: {layer0_sym.get('momentum_5d', 0):.3f}
- RSI(14): {layer0_sym.get('rsi_14', 50):.1f}
- ATR(14): {layer0_sym.get('atr_14', 0):.2f}
- Implied volatility: {layer0_sym.get('iv', 0):.3f}
- Earnings within 5d: {layer0_sym.get('earnings_within_5d', False)}

Propose a specific options strategy for this symbol given the current regime and IV environment.

Return ONLY valid JSON with no markdown formatting:
{{
  "symbol": "{symbol}",
  "strategy_type": "long_call",
  "direction": "long",
  "confidence": 0.75,
  "conviction_score": 7,
  "dte_target": 30,
  "strike_preference": "ATM",
  "entry_rationale": "...",
  "regime_alignment": "aligned",
  "signal_quality": {{
    "entry_timing_score": 0.75,
    "thesis_validity": "...",
    "expected_move_capture": 0.65
  }}
}}

strategy_type must be one of: long_call, long_put, bull_call_spread, bear_put_spread, iron_condor, vix_calls
direction must be: long or short
regime_alignment must be: aligned, neutral, or misaligned
strike_preference must be: ATM or 1_OTM"""


def call_sonnet(prompt: str) -> tuple[dict, int, int]:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    tokens_in = message.usage.input_tokens
    tokens_out = message.usage.output_tokens

    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    return json.loads(raw), tokens_in, tokens_out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    conn = get_db()
    total_tokens_in = 0
    total_tokens_out = 0

    try:
        # Load prescreened symbols
        pre_path = AGENTS_DIR / "prescreened_symbols.json"
        if not pre_path.exists():
            logger.error("prescreened_symbols.json not found — run prescreener first")
            log_system_event(conn, "ERROR", "conviction_engine", "prescreened_symbols.json missing")
            conn.close()
            sys.exit(1)

        with open(pre_path) as f:
            prescreened = json.load(f)

        layer0 = load_layer0_data()
        try:
            directives = load_master_directives()
        except FileNotFoundError:
            directives = {"regime": {"primary_regime": "neutral", "confidence": 0.5, "transition_risk": 0.0}, "global_kill_switch": False}

        if not prescreened:
            logger.info("No prescreened symbols — writing empty conviction_tickets.json")
            regime = directives.get("regime", {})
            all_sids = [f"C{i}" for i in range(1, 13)] + [f"D{i}" for i in range(1, 13)] + [f"E{i}" for i in range(1, 13)]
            track_map = {**{f"C{i}": "C" for i in range(1, 13)},
                         **{f"D{i}": "D" for i in range(1, 13)},
                         **{f"E{i}": "E" for i in range(1, 13)}}
            for sid in all_sids:
                log_inactivity(conn, sid, track_map[sid], "NO_PRESCREENED_SYMBOLS", regime)

            out_path = AGENTS_DIR / "conviction_tickets.json"
            with open(out_path, "w") as f:
                json.dump([], f)
            log_system_event(conn, "INFO", "conviction_engine", "No prescreened symbols — no conviction tickets")
            conn.close()
            return

        vix = layer0.get("vix", 0.0)
        regime = directives.get("regime", {})
        symbols_data = layer0.get("symbols", {})

        conviction_tickets = []

        for item in prescreened:
            symbol = item["symbol"]
            layer0_sym = symbols_data.get(symbol, item.get("layer0", {}))
            iv = layer0_sym.get("iv", 0.0)

            logger.info("Processing conviction for %s", symbol)

            # Call Sonnet
            prompt = build_sonnet_prompt(symbol, layer0_sym, vix, regime)
            try:
                proposal, t_in, t_out = call_sonnet(prompt)
                total_tokens_in += t_in
                total_tokens_out += t_out
            except Exception as exc:
                logger.error("Sonnet call failed for %s: %s", symbol, exc)
                log_system_event(conn, "ERROR", "conviction_engine", f"Sonnet failed for {symbol}: {exc}")
                continue

            # Enforce strategy rules in code
            proposed_type = proposal.get("strategy_type", "iron_condor")
            corrected_type, override_reason = enforce_strategy_rules(proposed_type, regime, iv, symbol)
            if override_reason:
                logger.info("Strategy override for %s: %s", symbol, override_reason)
                proposal["strategy_type"] = corrected_type
                proposal["override_note"] = override_reason

            # Add metadata
            proposal["prescreener_score"] = item.get("score", 0)
            proposal["prescreener_direction"] = item.get("direction")
            proposal["iv_at_conviction"] = iv
            proposal["vix_at_conviction"] = vix
            proposal["regime_at_conviction"] = regime  # full JSONB object
            proposal["created_at"] = datetime.now(timezone.utc).isoformat()

            conviction_tickets.append(proposal)

            # INSERT into nwt_tickets (DB)
            try:
                ticket_id = insert_ticket(
                    conn,
                    from_agent="CONVICTION_ENGINE",
                    to_agent="TRACK_ROUTER",
                    type_="CONVICTION_TICKET",
                    payload=proposal,
                )
                proposal["ticket_id"] = ticket_id
                logger.info("Inserted conviction ticket %s for %s", ticket_id, symbol)
            except Exception as exc:
                logger.error("Failed to insert conviction ticket for %s: %s", symbol, exc)

        # Write conviction_tickets.json
        out_path = AGENTS_DIR / "conviction_tickets.json"
        with open(out_path, "w") as f:
            json.dump(conviction_tickets, f, indent=2)

        logger.info("conviction_tickets.json written: %d tickets", len(conviction_tickets))
        log_system_event(
            conn,
            "INFO",
            "conviction_engine",
            f"Conviction engine complete: {len(conviction_tickets)} tickets",
            {
                "tickets": [t["symbol"] for t in conviction_tickets],
                "tokens_used": {"sonnet_in": total_tokens_in, "sonnet_out": total_tokens_out},
            },
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
