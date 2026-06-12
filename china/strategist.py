"""
China Policy/Event Bot — Strategist (event-triggered, not fixed cron)
Symbols: FXI, KWEB, MCHI, BABA, TCEHY
Alpha: Stimulus reaction, regulatory easing, ADR spread tracking
Holding period: 1 day to 3 weeks

ISOLATION: ONLY policy/event signals, ADR spreads, China-specific catalysts.
NO US technical overlays, NO ORB, NO VWAP, NO US macro signals.

This script is event-triggered — it must be invoked only after ADR liquidity
confirmation following US open. A placeholder cron entry exists; the real
trigger is an external ADR liquidity check (see CLAUDE.md).

SIGNAL GENERATOR ONLY — zero order authority.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import psycopg2
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
BOT_DIR = Path(__file__).parent
SHARED_DIR = BOT_DIR.parent / "shared"
CANDIDATES_FILE = SHARED_DIR / "china-candidates.json"
DIRECTIVES_FILE = SHARED_DIR / "master-directives.json"

load_dotenv(BOT_DIR / ".env", override=True)  # .env beats stale PM2 daemon env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CHINA-STRAT] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("china_strategist")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BOT_NAME = "china"
STRATEGY_ID = "CHINA-POL-001"

# All 5 China/HK proxy symbols
CHINA_SYMBOLS = ["FXI", "KWEB", "MCHI", "BABA", "TCEHY"]

# ADR spread threshold — if bid-ask > 3% of mid, liquidity is too thin
ADR_SPREAD_THRESHOLD = 0.03  # 3%

# Policy tailwind thresholds
FXI_KWEB_THRESHOLD = 0.02   # both up >2% in 5d = policy tailwind
BABA_MOMENTUM_THRESHOLD = 0.03  # BABA 5d momentum >3% = ADR spread confidence boost
MCHI_BROAD_STIMULUS_THRESHOLD = 0.01  # MCHI up while BABA/TCEHY up

# Entry confidence minimum (from genome; fallback default shown for reference only)
DEFAULT_ENTRY_THRESHOLD = 0.5

# ISOLATION: China bot must not use any US technical overlays
DISALLOWED_SIGNALS = frozenset([
    "ORB", "VWAP", "US_MOMENTUM", "SPY_TECHNICALS", "QQQ_TECHNICALS",
    "DXY", "ECB", "US_SECTOR_ROTATION",
])


def _enforce_isolation(label: str) -> None:
    for banned in DISALLOWED_SIGNALS:
        if banned.lower() in label.lower():
            raise RuntimeError(
                f"ISOLATION VIOLATION: China bot attempted to use '{label}'. "
                "Only policy/event signals, ADR spreads, and China catalysts permitted."
            )


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
def get_data_client() -> StockHistoricalDataClient:
    key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_SECRET_KEY"]
    data_url = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")
    return StockHistoricalDataClient(key, secret, url_override=data_url)


def get_db_conn():
    return psycopg2.connect(os.environ["NWT_DB_DSN"])


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def query_genome(conn, strategy_id: str) -> dict:
    """
    Query nwt_strategy_genome. Raises RuntimeError if not found.
    CRITICAL: No hardcoded strategy parameters.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT strategy_id, track, asset_universe, entry_threshold, "
            "       stop_loss_pct, profit_target_pct, regime, version, active "
            "FROM nwt_strategy_genome WHERE strategy_id = %s",
            (strategy_id,),
        )
        row = cur.fetchone()
    if not row:
        raise RuntimeError(
            f"No genome row found for {strategy_id} — refusing to run. "
            "Seed nwt_strategy_genome with CHINA-POL-001 before starting."
        )
    cols = ["strategy_id", "track", "asset_universe", "entry_threshold",
            "stop_loss_pct", "profit_target_pct", "regime", "version", "active"]
    return dict(zip(cols, row))


def log_to_db(conn, level: str, message: str, payload: dict | None = None) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO nwt_system_log (level, component, message, payload) "
                "VALUES (%s, %s, %s, %s)",
                (level, "CHINA_STRATEGIST", message, json.dumps(payload) if payload else None),
            )
        conn.commit()
    except Exception as exc:
        log.warning("DB log failed: %s", exc)


def log_inactivity(conn, strategy_id: str, reason: str, regime: dict) -> None:
    """Log an explicit inactivity decision. Inactivity is a first-class state."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO nwt_inactivity_log (strategy_id, track, reason, regime_at_decision) "
                "VALUES (%s, %s, %s, %s)",
                (strategy_id, "A", reason, json.dumps(regime)),
            )
        conn.commit()
        log.info("Inactivity logged: %s", reason)
    except Exception as exc:
        log.warning("Inactivity log failed: %s", exc)


# ---------------------------------------------------------------------------
# ADR liquidity check
# ---------------------------------------------------------------------------
def check_adr_liquidity(client: StockHistoricalDataClient) -> tuple[bool, str]:
    """
    Fetch current quotes for BABA and TCEHY.
    If bid-ask spread > 3% of mid for either, flag liquidity concern.
    Returns (liquidity_ok: bool, reason: str).

    ISOLATION: this is an ADR spread check — China-specific, not US technical overlay.
    """
    _enforce_isolation("adr_spread_china")  # allowed

    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=["BABA", "TCEHY"])
        quotes = client.get_stock_latest_quote(req)
    except Exception as exc:
        log.warning("ADR quote fetch failed: %s — treating as liquidity concern", exc)
        return False, f"QUOTE_FETCH_FAILED: {exc}"

    for symbol in ["BABA", "TCEHY"]:
        quote = quotes.get(symbol)
        if quote is None:
            log.warning("%s: no quote returned — liquidity concern", symbol)
            return False, f"NO_QUOTE_{symbol}"

        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)

        if bid <= 0 or ask <= 0:
            log.warning("%s: zero bid/ask — liquidity concern", symbol)
            return False, f"ZERO_BID_ASK_{symbol}"

        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid if mid > 0 else 1.0

        log.info("%s: bid=%.4f ask=%.4f spread_pct=%.4f", symbol, bid, ask, spread_pct)

        if spread_pct > ADR_SPREAD_THRESHOLD:
            reason = f"ADR_SPREAD_TOO_WIDE_{symbol}: {spread_pct:.4f} > {ADR_SPREAD_THRESHOLD}"
            log.info("%s: ADR liquidity concern: spread %.2f%% > threshold %.2f%%",
                     symbol, spread_pct * 100, ADR_SPREAD_THRESHOLD * 100)
            return False, reason

    return True, "ADR_LIQUIDITY_OK"


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
def fetch_5d_bars(client: StockHistoricalDataClient, symbols: list[str]) -> dict:
    """Fetch 5-day daily bars for China symbols."""
    _enforce_isolation("china_price_data")  # allowed

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=10)  # buffer for weekends/holidays
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed="iex",
    )
    df = client.get_stock_bars(req).df
    result = {}
    for sym in symbols:
        try:
            sym_df = df.loc[sym].sort_index() if sym in df.index.get_level_values(0) else None
            if sym_df is not None and len(sym_df) >= 2:
                result[sym] = sym_df
        except KeyError:
            pass
    return result


def compute_5d_momentum(bars_df) -> float:
    """
    5-day momentum: (latest close - oldest close) / oldest close.
    Returns 0.0 if insufficient data.
    """
    closes = bars_df["close"].values.astype(float)
    if len(closes) < 2:
        return 0.0
    oldest = closes[0]
    latest = closes[-1]
    if oldest == 0:
        return 0.0
    return float((latest - oldest) / oldest)


# ---------------------------------------------------------------------------
# Policy/event signal engine
# ---------------------------------------------------------------------------
def build_policy_signals(bars_by_symbol: dict) -> dict:
    """
    Compute policy/event signals from price action across China proxies.
    Uses only China-specific signals — no US technical overlays.

    Signal logic:
    1. Policy tailwind: FXI and KWEB both up >2% in 5 days
    2. Broad stimulus: MCHI up while BABA and TCEHY also up
    3. ADR spread confirmation: BABA 5d momentum >3%

    Returns a signals dict.
    """
    _enforce_isolation("china_policy_event")  # allowed

    signals = {}
    momenta = {}

    for sym in CHINA_SYMBOLS:
        bars = bars_by_symbol.get(sym)
        momenta[sym] = compute_5d_momentum(bars) if bars is not None else 0.0
        log.info("%s: 5d_momentum=%.4f (%.2f%%)", sym, momenta[sym], momenta[sym] * 100)

    # Signal 1: Policy tailwind — FXI and KWEB both up >2%
    fxi_up = momenta.get("FXI", 0) > FXI_KWEB_THRESHOLD
    kweb_up = momenta.get("KWEB", 0) > FXI_KWEB_THRESHOLD
    signals["policy_tailwind"] = fxi_up and kweb_up
    signals["fxi_momentum"] = momenta.get("FXI", 0)
    signals["kweb_momentum"] = momenta.get("KWEB", 0)

    # Signal 2: Broad stimulus — MCHI up + BABA up + TCEHY up
    mchi_up = momenta.get("MCHI", 0) > MCHI_BROAD_STIMULUS_THRESHOLD
    baba_up = momenta.get("BABA", 0) > 0
    tcehy_up = momenta.get("TCEHY", 0) > 0
    signals["broad_stimulus"] = mchi_up and baba_up and tcehy_up
    signals["mchi_momentum"] = momenta.get("MCHI", 0)

    # Signal 3: ADR spread proxy — BABA 5d momentum > 3%
    signals["adr_confidence"] = momenta.get("BABA", 0) > BABA_MOMENTUM_THRESHOLD
    signals["baba_momentum"] = momenta.get("BABA", 0)
    signals["tcehy_momentum"] = momenta.get("TCEHY", 0)

    return signals


def compute_candidate_confidence(signals: dict) -> float:
    """
    Aggregate policy signals into a confidence score.
    Base: 0.4
    +0.2 policy tailwind (FXI + KWEB both up >2%)
    +0.2 broad stimulus (MCHI + BABA + TCEHY all up)
    +0.1 ADR confirmation (BABA 5d momentum >3%)
    +0.1 FXI momentum strength bonus (>5%)
    """
    confidence = 0.4

    if signals.get("policy_tailwind"):
        confidence += 0.2

    if signals.get("broad_stimulus"):
        confidence += 0.2

    if signals.get("adr_confidence"):
        confidence += 0.1

    # FXI momentum strength bonus
    if signals.get("fxi_momentum", 0) > 0.05:
        confidence += 0.1

    return round(min(confidence, 0.95), 4)


def select_symbols_for_candidates(signals: dict, bars_by_symbol: dict) -> list[str]:
    """
    Select which symbols to generate candidates for based on signal strength.
    Policy tailwind: generate for FXI, KWEB, MCHI (broad ETF proxies).
    Broad stimulus: also add BABA, TCEHY if their momentum is positive.
    """
    selected = []

    if signals.get("policy_tailwind"):
        selected.extend(["FXI", "KWEB", "MCHI"])

    if signals.get("broad_stimulus"):
        if signals.get("baba_momentum", 0) > 0 and "BABA" not in selected:
            selected.append("BABA")
        if signals.get("tcehy_momentum", 0) > 0 and "TCEHY" not in selected:
            selected.append("TCEHY")

    # Only include symbols we actually have data for
    return [s for s in selected if s in bars_by_symbol]


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
    log.info("China policy/event strategist starting (event-triggered)")

    # Step 1: Directives gate
    directives = load_directives()

    regime = directives.get("regime", {})
    if not isinstance(regime, dict):
        raise RuntimeError(f"regime must be dict (JSONB), got {type(regime)}")

    if directives.get("global_kill_switch", True):
        log.info("Global kill switch active — writing empty candidates and exiting")
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        return

    china_perm = directives.get("bot_permissions", {}).get("china", {})
    if china_perm.get("status") == "paused":
        log.info("China bot status=paused — writing empty candidates and exiting")
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        return

    # Step 2: DB + genome (must happen before any trading logic)
    conn = None
    try:
        conn = get_db_conn()
    except Exception as exc:
        log.error("DB connection failed: %s — refusing to run without genome", exc)
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        sys.exit(1)

    try:
        genome = query_genome(conn, STRATEGY_ID)
        log.info("Genome loaded: %s v%s entry_threshold=%.2f",
                 STRATEGY_ID, genome["version"], genome["entry_threshold"])
    except RuntimeError as exc:
        log.error("%s", exc)
        log_to_db(conn, "ERROR", str(exc))
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        conn.close()
        sys.exit(1)

    # Step 3: ADR liquidity check — gate for the whole China bot
    client = get_data_client()
    liquidity_ok, liquidity_reason = check_adr_liquidity(client)

    if not liquidity_ok:
        log.info("ADR liquidity check failed: %s — logging inactivity and exiting", liquidity_reason)
        log_inactivity(conn, STRATEGY_ID, f"ADR_LIQUIDITY_CONCERN: {liquidity_reason}", regime)
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        conn.close()
        return

    log.info("ADR liquidity check passed — proceeding with signal generation")

    # Step 4: Fetch price data
    try:
        bars_by_symbol = fetch_5d_bars(client, CHINA_SYMBOLS)
    except Exception as exc:
        log.error("China data fetch failed: %s", exc, exc_info=True)
        log_to_db(conn, "ERROR", f"China data fetch failed: {exc}")
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        conn.close()
        sys.exit(1)

    # Step 5: Compute policy/event signals
    signals = build_policy_signals(bars_by_symbol)
    log.info(
        "Policy signals: tailwind=%s stimulus=%s adr_confirm=%s",
        signals.get("policy_tailwind"),
        signals.get("broad_stimulus"),
        signals.get("adr_confidence"),
    )

    entry_threshold = float(genome.get("entry_threshold") or DEFAULT_ENTRY_THRESHOLD)
    target_pct = float(genome.get("profit_target_pct") or 0.05)
    stop_pct = -abs(float(genome.get("stop_loss_pct") or 0.025))

    confidence = compute_candidate_confidence(signals)
    log.info("Aggregate confidence: %.3f (threshold: %.3f)", confidence, entry_threshold)

    if confidence < entry_threshold:
        reason = f"NO_POLICY_EDGE: confidence {confidence:.3f} < threshold {entry_threshold:.3f}"
        log.info(reason)
        log_inactivity(conn, STRATEGY_ID, reason, regime)
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        conn.close()
        return

    # Step 6: Select symbols and build candidates
    # Policy tailwind direction is always long (architecture spec)
    candidate_symbols = select_symbols_for_candidates(signals, bars_by_symbol)

    if not candidate_symbols:
        log.info("No symbols selected despite passing threshold — logging inactivity")
        log_inactivity(conn, STRATEGY_ID, "NO_SYMBOLS_SELECTED", regime)
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        conn.close()
        return

    thesis_parts = []
    if signals.get("policy_tailwind"):
        thesis_parts.append(
            f"Policy tailwind: FXI {signals['fxi_momentum']*100:.1f}% + KWEB {signals['kweb_momentum']*100:.1f}% (5d)"
        )
    if signals.get("broad_stimulus"):
        thesis_parts.append(
            f"Broad stimulus: MCHI {signals['mchi_momentum']*100:.1f}% + BABA/TCEHY positive"
        )
    if signals.get("adr_confidence"):
        thesis_parts.append(f"ADR confirmation: BABA {signals['baba_momentum']*100:.1f}% (5d)")

    thesis = " | ".join(thesis_parts) if thesis_parts else "China policy/event signal"

    candidates = []
    for symbol in candidate_symbols:
        # Per-symbol confidence: slight differentiation by individual momentum
        sym_momentum = {
            "FXI": signals.get("fxi_momentum", 0),
            "KWEB": signals.get("kweb_momentum", 0),
            "MCHI": signals.get("mchi_momentum", 0),
            "BABA": signals.get("baba_momentum", 0),
            "TCEHY": signals.get("tcehy_momentum", 0),
        }.get(symbol, 0)

        sym_confidence = round(min(confidence + min(sym_momentum * 0.5, 0.05), 0.95), 4)

        candidate = {
            "bot": BOT_NAME,
            "symbol": symbol,
            "direction": "long",  # China bot: policy tailwind = long only
            "confidence": sym_confidence,
            "strategy_id": STRATEGY_ID,
            "signal_quality": {
                "entry_timing_score": round(min(sym_momentum * 5, 1.0), 4) if sym_momentum > 0 else 0.5,
                "thesis_validity": thesis,
                "expected_move_capture": 0.70,
            },
            "expected_payoff": {
                "target_pct": target_pct,
                "stop_pct": stop_pct,
            },
            "rationale": (
                f"China policy/event (long): confidence={sym_confidence:.2f}, "
                f"5d_momentum={sym_momentum*100:.1f}%"
            ),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "_debug": {
                "signals": signals,
                "adr_liquidity_check": liquidity_reason,
                "sym_5d_momentum": round(sym_momentum, 4),
            },
        }
        candidates.append(candidate)
        log.info("%s: candidate generated (confidence=%.3f, 5d_mom=%.2f%%)",
                 symbol, sym_confidence, sym_momentum * 100)

    CANDIDATES_FILE.write_text(json.dumps(candidates, indent=2))
    log.info("Wrote %d candidate(s) to %s", len(candidates), CANDIDATES_FILE)

    log_to_db(conn, "INFO", f"China strategist complete: {len(candidates)} candidates", {
        "candidates": [c["symbol"] for c in candidates],
        "signals": signals,
        "regime": regime,
    })

    conn.close()
    log.info("China strategist done")


if __name__ == "__main__":
    main()
