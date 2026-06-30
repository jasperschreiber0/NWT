"""
nwt_agents/prescreener.py
Runs at 13:15 UTC. Uses Claude Haiku to pre-screen layer0_data symbols.

Flow:
  1. Load layer0_data.json
  2. Apply hard deterministic filters (no LLM)
  3. Send survivors to Claude Haiku for conviction scoring
  4. Keep only symbols with score >= 5
  5. Write prescreened_symbols.json
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
    load_layer0_data,
    load_master_directives,
    log_system_event,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("prescreener")

AGENTS_DIR = Path(os.environ.get("NWT_AGENTS_DIR", Path(__file__).parent))
HAIKU_MODEL = os.environ.get("CLAUDE_HAIKU_MODEL", "claude-haiku-4-5-20251001")


# ---------------------------------------------------------------------------
# Hard filters (deterministic, no LLM)
# ---------------------------------------------------------------------------

def apply_hard_filters(symbols_data: dict, vix: float) -> tuple[list, list]:
    """
    Apply deterministic pre-filters before any LLM call.
    Returns (survivors, filtered_out_log).
    """
    survivors = []
    filtered = []

    for symbol, data in symbols_data.items():
        reasons = []

        if vix > 40:
            reasons.append(f"VIX={vix:.1f} > 40 — global no-trade")

        if data.get("earnings_within_5d", False):
            reasons.append("earnings_within_5d=True")

        iv = data.get("iv", 0.0)
        if iv > 0.80:
            reasons.append(f"IV={iv:.2f} > 0.80")

        if data.get("price", 0.0) == 0.0:
            reasons.append("price=0 (data error)")

        if reasons:
            filtered.append({"symbol": symbol, "reasons": reasons})
            logger.info("Hard filter excluded %s: %s", symbol, "; ".join(reasons))
        else:
            survivors.append(symbol)

    return survivors, filtered


# ---------------------------------------------------------------------------
# Haiku prescreener
# ---------------------------------------------------------------------------

def build_haiku_prompt(survivors: list, symbols_data: dict, vix: float, regime: dict) -> str:
    symbol_lines = []
    for s in survivors:
        d = symbols_data[s]
        iv_note = f"iv={d['iv']:.3f}({'options' if d.get('iv_source') == 'options' else 'histvol~' if d.get('iv_source') == 'histvol' else 'missing'})"
        symbol_lines.append(
            f"- {s}: price={d['price']}, momentum_5d={d['momentum_5d']:.3f}, "
            f"rsi_14={d['rsi_14']:.1f}, atr_14={d['atr_14']:.2f}, {iv_note}"
        )

    return f"""You are a trading pre-screener for an options strategy system.

Current market context:
- Regime: {regime.get('primary_regime', 'unknown')} (confidence: {regime.get('confidence', 0):.2f}, transition_risk: {regime.get('transition_risk', 0):.2f})
- Secondary regime: {regime.get('secondary_regime', 'none')}
- VIX: {vix:.1f}

Symbols passing hard filters:
{chr(10).join(symbol_lines)}

IV notation: "options" = live implied volatility; "histvol~" = historical vol proxy (estimated); "missing" = no data.

For each symbol, score the conviction for a directional options trade (0-10) based on:
- Current regime alignment
- Price momentum
- RSI positioning (extremes = contrarian, mid = trend)
- Implied volatility level (use histvol~ as reasonable estimate when live IV unavailable)
- Overall setup quality

Return ONLY valid JSON in this exact format (no markdown, no explanation):
[
  {{
    "symbol": "SPY",
    "score": 7,
    "direction": "long",
    "primary_signal": "momentum breakout above VWAP in risk_on regime",
    "skip_reason": null
  }}
]

Rules:
- score >= 5: include in final list
- score < 5: still include but with skip_reason explaining why
- direction must be "long" or "short"
- skip_reason is null if score >= 5
- Do NOT score symbols with iv_source=missing below 5 solely because of missing IV — use momentum and regime"""


def call_haiku(prompt: str) -> list:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    tokens_in = message.usage.input_tokens
    tokens_out = message.usage.output_tokens
    logger.info("Haiku call: %d input tokens, %d output tokens", tokens_in, tokens_out)

    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    return json.loads(raw), tokens_in, tokens_out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    conn = get_db()

    try:
        layer0 = load_layer0_data()
        if not layer0:
            logger.error("layer0_data.json is empty or missing — exiting")
            log_system_event(conn, "ERROR", "prescreener", "layer0_data.json missing")
            conn.close()
            sys.exit(1)

        try:
            directives = load_master_directives()
        except FileNotFoundError:
            logger.warning("master-directives.json not found — using neutral regime")
            directives = {"regime": {"primary_regime": "neutral", "confidence": 0.5, "transition_risk": 0.0}, "global_kill_switch": False}

        if directives.get("global_kill_switch", False):
            logger.warning("Global kill switch active — writing empty prescreened_symbols.json")
            out_path = AGENTS_DIR / "prescreened_symbols.json"
            with open(out_path, "w") as f:
                json.dump([], f)
            log_system_event(conn, "WARNING", "prescreener", "Kill switch active — prescreening skipped")
            conn.close()
            return

        vix = layer0.get("vix", 0.0)
        symbols_data = layer0.get("symbols", {})
        regime = directives.get("regime", {})

        if not symbols_data:
            logger.error("No symbol data in layer0_data.json")
            conn.close()
            sys.exit(1)

        # 1. Hard filters
        survivors, filtered_out = apply_hard_filters(symbols_data, vix)
        logger.info(
            "Hard filters: %d survivors, %d excluded from %d symbols",
            len(survivors), len(filtered_out), len(symbols_data),
        )

        if not survivors:
            logger.warning("All symbols filtered out by hard filters — writing empty prescreened_symbols.json")
            out_path = AGENTS_DIR / "prescreened_symbols.json"
            with open(out_path, "w") as f:
                json.dump([], f)
            log_system_event(
                conn, "WARNING", "prescreener",
                "All symbols excluded by hard filters",
                {"filtered_out": filtered_out},
            )
            conn.close()
            return

        # 2. Claude Haiku scoring
        prompt = build_haiku_prompt(survivors, symbols_data, vix, regime)
        try:
            scored, tokens_in, tokens_out = call_haiku(prompt)
        except Exception as exc:
            logger.error("Haiku call failed: %s", exc)
            log_system_event(conn, "ERROR", "prescreener", f"Haiku API call failed: {exc}")
            conn.close()
            sys.exit(1)

        # 3. Filter to score >= 5
        passed = [item for item in scored if item.get("score", 0) >= 5]
        logger.info(
            "Haiku scoring: %d symbols scored, %d passed (score >= 5)",
            len(scored), len(passed),
        )

        # Enrich with layer0 data for downstream consumers
        for item in passed:
            sym = item["symbol"]
            if sym in symbols_data:
                item["layer0"] = symbols_data[sym]

        out_path = AGENTS_DIR / "prescreened_symbols.json"
        with open(out_path, "w") as f:
            json.dump(passed, f, indent=2)

        logger.info("prescreened_symbols.json written: %d symbols", len(passed))
        log_system_event(
            conn,
            "INFO",
            "prescreener",
            f"Prescreening complete: {len(passed)} symbols passed",
            {
                "passed": [p["symbol"] for p in passed],
                "filtered_hard": [f["symbol"] for f in filtered_out],
                "tokens_haiku_in": tokens_in,
                "tokens_haiku_out": tokens_out,
                "tokens_used": {"haiku_in": tokens_in, "haiku_out": tokens_out},
            },
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
