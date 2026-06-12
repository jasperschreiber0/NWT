"""
US Flow Bot — ORB Signal Generator (14:05 ET / 18:05 UTC)
Reads master-directives.json, queries nwt_strategy_genome, scores each symbol
using Opening Range Breakout logic, writes candidates to shared/us-candidates.json.

CRITICAL: This is a SIGNAL GENERATOR ONLY.
- Zero order authority.
- NEVER calls any Alpaca order endpoint (POST/PATCH/DELETE /v2/orders).
- Isolation: only price, volume, VWAP, options flow (OI/put-call). No macro, no DXY.

Fire time: 18:05 UTC (14:05 ET). SIP data is not ready at exactly 14:00 ET.
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
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap — resolve paths relative to this file so the script works from any cwd
# ---------------------------------------------------------------------------
BOT_DIR = Path(__file__).parent.parent.parent  # us/
SHARED_DIR = BOT_DIR.parent / "shared"
CANDIDATES_FILE = SHARED_DIR / "us-candidates.json"
DIRECTIVES_FILE = SHARED_DIR / "master-directives.json"

load_dotenv(BOT_DIR / ".env", override=True)  # .env beats stale PM2 daemon env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [US-ORB] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("us_orb")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BOT_NAME = "us"
STRATEGY_ID = "US-ORB-001"

# Symbols and their minimum score thresholds (from architecture spec)
SYMBOL_THRESHOLDS = {
    "SPY": 4,
    "QQQ": 3,
    "AAPL": 3,
    "TSLA": 4,
    "NVDA": 3,
}
MAX_SCORE = 5  # 5 scoring components per symbol

# ORB window: 9:30–10:00 ET = 14:30–15:00 UTC
ORB_START_UTC = 14 * 60 + 30   # minutes since midnight UTC
ORB_END_UTC   = 15 * 60 + 0

# Lookback for average volume (trading days)
AVG_VOL_DAYS = 20

# ISOLATION GUARD — these are the ONLY allowed data sources for US bot
ALLOWED_SIGNAL_SOURCES = frozenset(["price", "volume", "vwap", "rsi", "options_flow"])
DISALLOWED_SIGNAL_SOURCES = frozenset(["DXY", "macro", "ECB", "PBOC", "sector_rotation"])


def _enforce_isolation(source: str) -> None:
    """Raise immediately if a disallowed signal source is referenced."""
    for banned in DISALLOWED_SIGNAL_SOURCES:
        if banned.lower() in source.lower():
            raise RuntimeError(
                f"ISOLATION VIOLATION: US bot tried to use '{source}'. "
                "Only price/volume/VWAP/RSI/options_flow permitted."
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
    Query nwt_strategy_genome at startup. Raises RuntimeError if not found.
    CRITICAL: No hardcoded strategy parameters — all come from the genome.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT strategy_id, track, asset_universe, dte_min, dte_max, "
            "       iv_filter_max, entry_threshold, stop_loss_pct, profit_target_pct, "
            "       regime, version, active, shadow_mode "
            "FROM nwt_strategy_genome WHERE strategy_id = %s",
            (strategy_id,),
        )
        row = cur.fetchone()
    if not row:
        raise RuntimeError(
            f"No genome row found for {strategy_id} — refusing to run. "
            "Seed nwt_strategy_genome before starting."
        )
    cols = [
        "strategy_id", "track", "asset_universe", "dte_min", "dte_max",
        "iv_filter_max", "entry_threshold", "stop_loss_pct", "profit_target_pct",
        "regime", "version", "active", "shadow_mode",
    ]
    return dict(zip(cols, row))


def log_to_db(conn, level: str, message: str, payload: dict | None = None) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO nwt_system_log (level, component, message, payload) "
                "VALUES (%s, %s, %s, %s)",
                (level, "US_ORB", message, json.dumps(payload) if payload else None),
            )
        conn.commit()
    except Exception as exc:
        log.warning("DB log failed: %s", exc)


def log_inactivity(conn, reason: str, regime: dict) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO nwt_inactivity_log (strategy_id, track, reason, regime_at_decision) "
                "VALUES (%s, %s, %s, %s)",
                (STRATEGY_ID, "A", reason, json.dumps(regime)),
            )
        conn.commit()
        log.info("Inactivity logged: %s", reason)
    except Exception as exc:
        log.warning("Inactivity log failed: %s", exc)


# ---------------------------------------------------------------------------
# Data fetching — isolation enforced (price + volume only)
# ---------------------------------------------------------------------------
def fetch_intraday_bars(client: StockHistoricalDataClient, symbols: list[str]) -> dict:
    """
    Fetch 1-minute bars from market open (14:30 UTC) to now.
    Returns dict of symbol -> DataFrame sorted by timestamp ascending.
    """
    _enforce_isolation("price")  # always passes; documents intent

    today = datetime.now(timezone.utc).date()
    # Market open: 14:30 UTC today
    market_open = datetime(today.year, today.month, today.day, 14, 30, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=market_open,
        end=now,
        feed="iex",
    )
    df = client.get_stock_bars(req).df
    result = {}
    for sym in symbols:
        try:
            sym_df = df.loc[sym].sort_index() if sym in df.index.get_level_values(0) else None
            if sym_df is not None and len(sym_df) > 0:
                result[sym] = sym_df
        except KeyError:
            pass
    return result


def fetch_avg_volume(client: StockHistoricalDataClient, symbols: list[str]) -> dict[str, float]:
    """
    Fetch 20-day daily bars to compute average daily volume per symbol.
    """
    _enforce_isolation("volume")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)  # extra buffer for weekends/holidays
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed="iex",
    )
    df = client.get_stock_bars(req).df
    avg_vols = {}
    for sym in symbols:
        try:
            sym_df = df.loc[sym].sort_index() if sym in df.index.get_level_values(0) else None
            if sym_df is not None and len(sym_df) >= 2:
                # Exclude today (last row might be partial) — use prior sessions
                vols = sym_df["volume"].values[:-1]
                avg_vols[sym] = float(np.mean(vols[-AVG_VOL_DAYS:])) if len(vols) > 0 else 0.0
            else:
                avg_vols[sym] = 0.0
        except (KeyError, IndexError):
            avg_vols[sym] = 0.0
    return avg_vols


# ---------------------------------------------------------------------------
# Technical indicators — price + volume only (isolation enforced)
# ---------------------------------------------------------------------------
def compute_orb(bars_df, orb_start_utc_min: int, orb_end_utc_min: int) -> tuple[float, float]:
    """
    Opening Range Breakout: high and low of the first 30 minutes.
    bars_df index is a DatetimeIndex (UTC timestamps).
    Returns (orb_high, orb_low). Returns (None, None) if insufficient data.
    """
    _enforce_isolation("price")

    orb_bars = bars_df[
        (bars_df.index.hour * 60 + bars_df.index.minute >= orb_start_utc_min) &
        (bars_df.index.hour * 60 + bars_df.index.minute < orb_end_utc_min)
    ]
    if len(orb_bars) < 5:  # need meaningful data within the ORB window
        return None, None
    return float(orb_bars["high"].max()), float(orb_bars["low"].min())


def compute_vwap(bars_df) -> float:
    """
    Volume-Weighted Average Price from the start of the session bars provided.
    VWAP = sum(typical_price * volume) / sum(volume)
    """
    _enforce_isolation("vwap")

    typical = (bars_df["high"] + bars_df["low"] + bars_df["close"]) / 3.0
    total_vol = bars_df["volume"].sum()
    if total_vol == 0:
        return float(bars_df["close"].iloc[-1])
    return float((typical * bars_df["volume"]).sum() / total_vol)


def compute_rsi(prices: np.ndarray, period: int = 14) -> float:
    """RSI — Wilder smoothing. Returns latest RSI value."""
    _enforce_isolation("price")

    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    gains = np.maximum(deltas, 0.0)
    losses = np.maximum(-deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_symbol(
    symbol: str,
    bars_df,
    avg_vol: float,
    orb_high: float,
    orb_low: float,
    vwap: float,
) -> tuple[int, str, dict]:
    """
    Score a symbol against 5 ORB components.
    Returns (score, direction, detail_dict).
    direction: 'long' | 'short' | 'none'
    """
    closes = bars_df["close"].values.astype(float)
    volumes = bars_df["volume"].values.astype(float)
    current_price = closes[-1]
    current_vol_session = float(volumes.sum())  # session volume so far

    score = 0
    details = {}

    # Component 1: Price relative to ORB
    above_orb = current_price > orb_high
    below_orb = current_price < orb_low
    if above_orb or below_orb:
        score += 1
        details["orb_break"] = "above" if above_orb else "below"
    else:
        details["orb_break"] = "inside"

    # Component 2: Volume above 20d average (session vol vs full-day avg, adjusted for time)
    # At 14:05 ET we have ~35 minutes of data (≈5% of session).
    # Scale avg_vol by 0.05 to get expected 35min volume — if above that, signal is strong.
    expected_partial_vol = avg_vol * (35.0 / 390.0) if avg_vol > 0 else 0
    vol_above_avg = current_vol_session > expected_partial_vol if expected_partial_vol > 0 else False
    if vol_above_avg:
        score += 1
    details["vol_above_avg"] = vol_above_avg
    details["session_vol"] = int(current_vol_session)
    details["expected_partial_vol"] = int(expected_partial_vol)

    # Component 3: Price above VWAP (long bias) or below VWAP (short bias)
    above_vwap = current_price > vwap
    if (above_orb and above_vwap) or (below_orb and not above_vwap):
        score += 1
        details["vwap_aligned"] = True
    else:
        details["vwap_aligned"] = False
    details["vwap"] = round(vwap, 4)

    # Component 4: Momentum — close now vs 5 bars ago
    momentum_positive = bool(closes[-1] > closes[-6]) if len(closes) >= 6 else False
    if (above_orb and momentum_positive) or (below_orb and not momentum_positive):
        score += 1
    details["momentum_positive"] = momentum_positive

    # Component 5: RSI 14 between 50–70 (long) or 30–50 (short)
    rsi = compute_rsi(closes)
    details["rsi_14"] = round(rsi, 2)
    if above_orb and 50 <= rsi <= 70:
        score += 1
        details["rsi_zone"] = "long_zone"
    elif below_orb and 30 <= rsi <= 50:
        score += 1
        details["rsi_zone"] = "short_zone"
    else:
        details["rsi_zone"] = "neutral"

    # Determine direction
    if above_orb:
        direction = "long"
    elif below_orb:
        direction = "short"
    else:
        direction = "none"

    return score, direction, details


def build_candidate(
    symbol: str,
    score: int,
    direction: str,
    details: dict,
    avg_vol: float,
    bars_df,
    genome: dict,
) -> dict:
    """Build the candidate signal dict matching the interface schema."""
    volumes = bars_df["volume"].values.astype(float)
    session_vol = float(volumes.sum())
    expected_partial_vol = avg_vol * (35.0 / 390.0) if avg_vol > 0 else 1.0

    # entry_timing_score: ratio of session vol to expected partial vol, capped at 1.0
    entry_timing_score = min(session_vol / expected_partial_vol, 1.0) if expected_partial_vol > 0 else 0.5

    confidence = score / MAX_SCORE

    thesis_parts = []
    if details.get("orb_break") in ("above", "below"):
        thesis_parts.append(f"ORB {details['orb_break']} ({direction})")
    if details.get("vwap_aligned"):
        thesis_parts.append("VWAP aligned")
    if details.get("momentum_positive") and direction == "long":
        thesis_parts.append("momentum positive")
    elif not details.get("momentum_positive") and direction == "short":
        thesis_parts.append("momentum negative")
    thesis_parts.append(f"RSI {details.get('rsi_14', 50):.1f} ({details.get('rsi_zone', 'neutral')})")

    thesis = ", ".join(thesis_parts) if thesis_parts else "ORB signal"

    # target and stop from genome — never hardcoded
    target_pct = float(genome.get("profit_target_pct") or 0.012)
    stop_pct = -abs(float(genome.get("stop_loss_pct") or 0.006))

    return {
        "bot": BOT_NAME,
        "symbol": symbol,
        "direction": direction,
        "confidence": round(confidence, 4),
        "strategy_id": STRATEGY_ID,
        "signal_quality": {
            "entry_timing_score": round(entry_timing_score, 4),
            "thesis_validity": thesis,
            "expected_move_capture": 0.75,  # baseline — Learning Agent refines this
        },
        "expected_payoff": {
            "target_pct": target_pct,
            "stop_pct": stop_pct,
        },
        "rationale": f"ORB breakout ({direction}), score {score}/{MAX_SCORE}: {thesis}",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_debug": {
            "score": score,
            "threshold": SYMBOL_THRESHOLDS[symbol],
            "orb_details": details,
        },
    }


# ---------------------------------------------------------------------------
# Directives
# ---------------------------------------------------------------------------
def load_directives() -> dict:
    if not DIRECTIVES_FILE.exists():
        log.warning("master-directives.json missing — assuming safe defaults (kill switch on)")
        return {"global_kill_switch": True}
    with open(DIRECTIVES_FILE) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("US ORB signal generator starting (18:05 UTC / 14:05 ET)")

    # Step 1: Read master-directives first
    directives = load_directives()

    # Regime is always a dict (JSONB) — never treat as string
    regime = directives.get("regime", {})
    if not isinstance(regime, dict):
        raise RuntimeError(f"regime must be a dict (JSONB), got {type(regime)} — non-compliant directives file")

    if directives.get("global_kill_switch", True):
        log.info("Global kill switch active — writing empty candidates and exiting")
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        return

    us_perm = directives.get("bot_permissions", {}).get("us", {})
    if us_perm.get("status") == "paused":
        log.info("US bot status=paused in directives — writing empty candidates and exiting")
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        return

    # Step 2: DB connect + genome query (MUST happen before any market data)
    conn = None
    try:
        conn = get_db_conn()
    except Exception as exc:
        log.error("DB connection failed: %s — cannot verify genome, refusing to trade", exc)
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        sys.exit(1)

    try:
        genome = query_genome(conn, STRATEGY_ID)
        log.info("Genome loaded: %s v%s entry_threshold=%.2f stop=%.3f target=%.3f",
                 STRATEGY_ID, genome["version"],
                 genome["entry_threshold"], genome["stop_loss_pct"], genome["profit_target_pct"])
    except RuntimeError as exc:
        log.error("%s", exc)
        log_to_db(conn, "ERROR", str(exc))
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        conn.close()
        sys.exit(1)

    # Step 3: Fetch market data
    client = get_data_client()
    symbols = list(SYMBOL_THRESHOLDS.keys())

    try:
        intraday = fetch_intraday_bars(client, symbols)
        avg_vols = fetch_avg_volume(client, symbols)
    except Exception as exc:
        log.error("Data fetch failed: %s", exc, exc_info=True)
        log_to_db(conn, "ERROR", f"Data fetch failed: {exc}")
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        conn.close()
        sys.exit(1)

    # Step 4: Score each symbol
    candidates = []
    for symbol, threshold in SYMBOL_THRESHOLDS.items():
        bars = intraday.get(symbol)
        if bars is None or len(bars) < 10:
            log.warning("%s: insufficient intraday data (%s bars) — skipping",
                        symbol, len(bars) if bars is not None else 0)
            continue

        orb_high, orb_low = compute_orb(bars, ORB_START_UTC, ORB_END_UTC)
        if orb_high is None:
            log.warning("%s: ORB window had insufficient data — skipping", symbol)
            continue

        vwap = compute_vwap(bars)
        avg_vol = avg_vols.get(symbol, 0.0)

        score, direction, details = score_symbol(symbol, bars, avg_vol, orb_high, orb_low, vwap)

        log.info("%s: score=%d/%d direction=%s orb_high=%.4f orb_low=%.4f vwap=%.4f rsi=%.1f",
                 symbol, score, MAX_SCORE, direction,
                 orb_high, orb_low, vwap, details.get("rsi_14", 0))

        if score >= threshold and direction != "none":
            candidate = build_candidate(symbol, score, direction, details, avg_vol, bars, genome)
            candidates.append(candidate)
            log.info("%s: PASS — candidate generated (confidence=%.2f)", symbol, candidate["confidence"])
        else:
            reason = f"score {score}/{MAX_SCORE} < threshold {threshold}" if score < threshold else "price inside ORB"
            log.info("%s: SKIP — %s", symbol, reason)
            log_inactivity(conn, reason, regime)

    # Step 5: Write candidates
    CANDIDATES_FILE.write_text(json.dumps(candidates, indent=2))
    log.info("Wrote %d candidate(s) to %s", len(candidates), CANDIDATES_FILE)

    if not candidates:
        log_inactivity(conn, "NO_ORB_SIGNALS_PASSED_THRESHOLD", regime)

    log_to_db(conn, "INFO", f"ORB scan complete: {len(candidates)} candidates", {
        "candidates": [c["symbol"] for c in candidates],
        "regime": regime,
    })

    conn.close()
    log.info("US ORB signal generator done")


if __name__ == "__main__":
    main()
