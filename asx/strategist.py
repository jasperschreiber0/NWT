"""
AUS Dividend/Momentum Bot — Strategist (09:00 UTC)
Symbols: EWA, BHP, RIO (US-listed)
Alpha: Dividend capture + trend following (EMA crossover)
Holding period: 1-8 weeks

ISOLATION: ONLY 1-8 week trend + dividend calendar signals.
NO intraday signals. NO options. NO US technical overlays.
SIGNAL GENERATOR ONLY — zero order authority.
"""

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import psycopg2
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
BOT_DIR = Path(__file__).parent
SHARED_DIR = BOT_DIR.parent / "shared"
CANDIDATES_FILE = SHARED_DIR / "aus-candidates.json"
DIRECTIVES_FILE = SHARED_DIR / "master-directives.json"

load_dotenv(BOT_DIR / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AUS-STRAT] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("aus_strategist")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BOT_NAME = "aus"
AUS_SYMBOLS = ["EWA", "BHP", "RIO"]
FETCH_DAYS = 35  # buffer for weekends/holidays

# ISOLATION: disallow all intraday and US technical signals
DISALLOWED_SIGNALS = frozenset([
    "intraday", "ORB", "VWAP", "options", "US_MOMENTUM",
    "DXY", "ECB", "PBOC", "minute_bar",
])


def _enforce_isolation(label: str) -> None:
    for banned in DISALLOWED_SIGNALS:
        if banned.lower() in label.lower():
            raise RuntimeError(
                f"ISOLATION VIOLATION: AUS bot attempted to use '{label}'. "
                "Only 1-8 week trend and dividend calendar signals permitted."
            )


# ---------------------------------------------------------------------------
# Dividend calendar
# Approximation for paper trading.
# BHP: quarterly ~Mar/Jun/Sep/Dec
# RIO: quarterly ~Feb/May/Aug/Nov
# EWA: monthly (ETF, monthly distributions)
# ---------------------------------------------------------------------------
# Month numbers for each symbol's typical ex-dividend months
DIVIDEND_MONTHS: dict[str, list[int]] = {
    "BHP": [3, 6, 9, 12],
    "RIO": [2, 5, 8, 11],
    "EWA": list(range(1, 13)),  # monthly
}


def days_to_next_exdiv(symbol: str, today: date) -> int:
    """
    Approximate days until the next ex-dividend date.
    Assumes ex-div falls on the 15th of the relevant month.
    Returns a large number (999) if the symbol has no entry.
    """
    _enforce_isolation("dividend_calendar")  # allowed; documents intent

    months = DIVIDEND_MONTHS.get(symbol)
    if not months:
        return 999

    min_days = 999
    for month in months:
        for year_offset in [0, 1]:
            candidate_year = today.year + year_offset
            try:
                ex_div = date(candidate_year, month, 15)
            except ValueError:
                continue
            delta = (ex_div - today).days
            if delta >= 0 and delta < min_days:
                min_days = delta

    return min_days


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
            "Seed nwt_strategy_genome with AUS genome rows before starting."
        )
    cols = ["strategy_id", "track", "asset_universe", "entry_threshold",
            "stop_loss_pct", "profit_target_pct", "regime", "version", "active"]
    return dict(zip(cols, row))


def query_genome_aus(conn) -> dict:
    """
    Try AUS-DIV-001, then AUS-MOM-001. Raises RuntimeError if neither found.
    """
    for sid in ["AUS-DIV-001", "AUS-MOM-001"]:
        try:
            return query_genome(conn, sid)
        except RuntimeError:
            continue
    raise RuntimeError(
        "No genome found for AUS-DIV-001 or AUS-MOM-001 — refusing to run."
    )


def log_to_db(conn, level: str, message: str, payload: dict | None = None) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO nwt_system_log (level, component, message, payload) "
                "VALUES (%s, %s, %s, %s)",
                (level, "AUS_STRATEGIST", message, json.dumps(payload) if payload else None),
            )
        conn.commit()
    except Exception as exc:
        log.warning("DB log failed: %s", exc)


def log_inactivity(conn, strategy_id: str, reason: str, regime: dict) -> None:
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
# Data
# ---------------------------------------------------------------------------
def fetch_daily_bars(client: StockHistoricalDataClient, symbols: list[str]) -> dict:
    """Fetch 30-day daily bars for AUS symbols."""
    _enforce_isolation("daily_bars_only")  # allowed

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=FETCH_DAYS)
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
            if sym_df is not None and len(sym_df) >= 5:
                result[sym] = sym_df
        except KeyError:
            pass
    return result


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def compute_ema(prices: np.ndarray, period: int) -> np.ndarray:
    """EMA with Wilder-style smoothing."""
    ema = np.zeros_like(prices, dtype=float)
    k = 2.0 / (period + 1)
    ema[0] = prices[0]
    for i in range(1, len(prices)):
        ema[i] = prices[i] * k + ema[i - 1] * (1 - k)
    return ema


def momentum_positive(closes: np.ndarray) -> bool:
    """
    5-day EMA above 20-day EMA = positive momentum (weekly/monthly trend signal).
    ISOLATION: Daily bars only — no intraday.
    """
    _enforce_isolation("daily_momentum")

    if len(closes) < 20:
        return False
    ema5 = compute_ema(closes, 5)
    ema20 = compute_ema(closes, 20)
    return bool(ema5[-1] > ema20[-1])


def ewa_breadth_positive(bars_by_symbol: dict) -> bool:
    """
    EWA (Australia ETF) as a breadth gauge: if EWA itself is in uptrend,
    treat as positive breadth for BHP/RIO signals.
    """
    _enforce_isolation("australian_breadth")

    ewa_bars = bars_by_symbol.get("EWA")
    if ewa_bars is None or len(ewa_bars) < 20:
        return False
    closes = ewa_bars["close"].values.astype(float)
    return momentum_positive(closes)


# ---------------------------------------------------------------------------
# Signal scoring
# ---------------------------------------------------------------------------
def score_symbol(
    symbol: str,
    bars_df,
    bars_by_symbol: dict,
    today: date,
) -> tuple[float, str, dict]:
    """
    Compute dividend/momentum confidence score.

    Confidence components:
    - 0.5 base
    - +0.2 if dividend within 15 days
    - +0.2 if strong momentum (5d EMA > 20d EMA)
    - +0.1 if EWA breadth positive

    Returns (confidence, direction, details).
    direction is always 'long' for AUS (dividend capture + trend following is long-biased).
    """
    _enforce_isolation("aus_dividend_momentum")

    closes = bars_df["close"].values.astype(float)
    days_to_div = days_to_next_exdiv(symbol, today)
    mom = momentum_positive(closes)
    ewa_breadth = ewa_breadth_positive(bars_by_symbol)

    confidence = 0.5  # base

    div_boost = 0.0
    if days_to_div <= 15:
        div_boost = 0.2
    elif days_to_div <= 30:
        div_boost = 0.1  # softer boost for 16-30 day window
    confidence += div_boost

    mom_boost = 0.2 if mom else 0.0
    confidence += mom_boost

    breadth_boost = 0.1 if ewa_breadth else 0.0
    confidence += breadth_boost

    confidence = round(min(confidence, 0.95), 4)

    details = {
        "days_to_next_exdiv": days_to_div,
        "dividend_boost": div_boost,
        "momentum_positive": mom,
        "momentum_boost": mom_boost,
        "ewa_breadth_positive": ewa_breadth,
        "breadth_boost": breadth_boost,
        "last_close": float(closes[-1]),
    }

    # AUS bot is long-only (dividend capture + momentum trend following)
    # Per spec: no short signals, 1-8 week hold, no options
    direction = "long"

    return confidence, direction, details


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
    log.info("AUS dividend/momentum strategist starting (09:00 UTC)")

    # Step 1: Directives gate
    directives = load_directives()

    regime = directives.get("regime", {})
    if not isinstance(regime, dict):
        raise RuntimeError(f"regime must be dict (JSONB), got {type(regime)}")

    if directives.get("global_kill_switch", True):
        log.info("Global kill switch active — writing empty candidates and exiting")
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        return

    aus_perm = directives.get("bot_permissions", {}).get("aus", {})
    if aus_perm.get("status") == "paused":
        log.info("AUS bot status=paused — writing empty candidates and exiting")
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        return

    # Step 2: DB + genome
    conn = None
    try:
        conn = get_db_conn()
    except Exception as exc:
        log.error("DB connection failed: %s — refusing to run without genome", exc)
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        sys.exit(1)

    try:
        genome = query_genome_aus(conn)
        log.info("Genome loaded: %s v%s entry_threshold=%.2f",
                 genome["strategy_id"], genome["version"], genome["entry_threshold"])
    except RuntimeError as exc:
        log.error("%s", exc)
        log_to_db(conn, "ERROR", str(exc))
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        conn.close()
        sys.exit(1)

    # Step 3: Fetch data
    client = get_data_client()
    try:
        bars_by_symbol = fetch_daily_bars(client, AUS_SYMBOLS)
    except Exception as exc:
        log.error("Data fetch failed: %s", exc, exc_info=True)
        log_to_db(conn, "ERROR", f"AUS data fetch failed: {exc}")
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        conn.close()
        sys.exit(1)

    today = datetime.now(timezone.utc).date()
    entry_threshold = float(genome.get("entry_threshold") or 0.5)
    target_pct = float(genome.get("profit_target_pct") or 0.04)
    stop_pct = -abs(float(genome.get("stop_loss_pct") or 0.02))
    strategy_id = genome["strategy_id"]

    candidates = []
    for symbol in AUS_SYMBOLS:
        bars = bars_by_symbol.get(symbol)
        if bars is None:
            log.warning("%s: no data available — skipping", symbol)
            log_inactivity(conn, strategy_id, f"NO_DATA_{symbol}", regime)
            continue

        confidence, direction, details = score_symbol(symbol, bars, bars_by_symbol, today)

        log.info(
            "%s: confidence=%.3f days_to_div=%d momentum=%s ewa_breadth=%s",
            symbol, confidence,
            details["days_to_next_exdiv"],
            details["momentum_positive"],
            details["ewa_breadth_positive"],
        )

        if confidence < entry_threshold:
            log.info("%s: confidence %.3f < threshold %.3f — no signal", symbol, confidence, entry_threshold)
            log_inactivity(conn, strategy_id, f"CONFIDENCE_BELOW_THRESHOLD_{symbol}", regime)
            continue

        thesis_parts = [f"Dividend capture ({details['days_to_next_exdiv']}d to ex-div)"]
        if details["momentum_positive"]:
            thesis_parts.append("5d EMA > 20d EMA")
        if details["ewa_breadth_positive"]:
            thesis_parts.append("EWA breadth positive")

        candidate = {
            "bot": BOT_NAME,
            "symbol": symbol,
            "direction": direction,
            "confidence": confidence,
            "strategy_id": strategy_id,
            "signal_quality": {
                "entry_timing_score": round(min(confidence, 1.0), 4),
                "thesis_validity": " + ".join(thesis_parts),
                "expected_move_capture": 0.65,
            },
            "expected_payoff": {
                "target_pct": target_pct,
                "stop_pct": stop_pct,
            },
            "rationale": (
                f"AUS dividend/momentum (long): confidence={confidence:.2f}, "
                f"div={details['days_to_next_exdiv']}d, "
                f"momentum={'yes' if details['momentum_positive'] else 'no'}"
            ),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "_debug": details,
        }
        candidates.append(candidate)
        log.info("%s: candidate generated (confidence=%.3f)", symbol, confidence)

    CANDIDATES_FILE.write_text(json.dumps(candidates, indent=2))
    log.info("Wrote %d candidate(s) to %s", len(candidates), CANDIDATES_FILE)

    if not candidates:
        log_inactivity(conn, strategy_id, "NO_AUS_SIGNALS_PASSED", regime)

    log_to_db(conn, "INFO", f"AUS strategist complete: {len(candidates)} candidates", {
        "candidates": [c["symbol"] for c in candidates],
        "regime": regime,
    })

    conn.close()
    log.info("AUS strategist done")


if __name__ == "__main__":
    main()
