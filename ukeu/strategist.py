"""
EU Mean Reversion Bot — Strategist (09:30 UTC)
Symbols: VGK, EWU, FEZ
Alpha: Mean reversion + ECB policy lag
Holding period: 2-20 days

ISOLATION: Only mean reversion signals, European market hours data, ECB calendar
lag. NO US momentum triggers, NO DXY, NO US technical overlays.
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
import requests
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
BOT_DIR = Path(__file__).parent
SHARED_DIR = BOT_DIR.parent / "shared"
CANDIDATES_FILE = SHARED_DIR / "eu-candidates.json"
DIRECTIVES_FILE = SHARED_DIR / "master-directives.json"

load_dotenv(BOT_DIR / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EU-STRAT] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("eu_strategist")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BOT_NAME = "eu"
EU_SYMBOLS = ["VGK", "EWU", "FEZ"]
LOOKBACK_DAYS = 20  # z-score window
FETCH_DAYS = 30     # extra buffer for weekends/holidays

# Isolation guard
DISALLOWED_SIGNALS = frozenset([
    "US_MOMENTUM", "SPY", "QQQ", "DXY", "US_TECH", "AAPL", "TSLA", "NVDA",
    "sector_rotation_us",
])

# Shortability gate — a short signal on an asset Alpaca won't let this
# account short (e.g. EWU: shortable=False, hard-to-borrow) must never
# become a candidate. Checked against Alpaca's own /v2/assets/{symbol},
# cached to shared/asset-shortability-cache.json (same shared/ directory
# already used for candidates.json / master-directives.json) with a bounded
# TTL so borrowability changes are eventually picked up without hitting the
# API on every symbol evaluation. execution/engine.py holds an independent
# copy of this same check as a defense-in-depth backstop.
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
}
SHORTABILITY_CACHE_FILE = SHARED_DIR / "asset-shortability-cache.json"
SHORTABILITY_CACHE_TTL_HOURS = 24


def _enforce_isolation(label: str) -> None:
    """Hard isolation: EU bot must not use US momentum or DXY signals."""
    for banned in DISALLOWED_SIGNALS:
        if banned.upper() in label.upper():
            raise RuntimeError(
                f"ISOLATION VIOLATION: EU bot attempted to use '{label}'. "
                "Only European mean reversion and ECB lag signals permitted."
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
def query_genome_eu(conn) -> dict:
    """
    Query nwt_strategy_genome for an active EU strategy.
    Uses the first active strategy with track='A' and strategy_id starting with 'EU-'.
    Raises RuntimeError if none found.
    CRITICAL: No hardcoded parameters — all from genome.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT strategy_id, track, asset_universe, entry_threshold, "
            "       stop_loss_pct, profit_target_pct, regime, version "
            "FROM nwt_strategy_genome "
            "WHERE strategy_id LIKE 'EU-%' AND active = TRUE "
            "ORDER BY strategy_id ASC LIMIT 1"
        )
        row = cur.fetchone()
    if not row:
        raise RuntimeError(
            "No active genome row found for EU strategies (strategy_id LIKE 'EU-%') — "
            "refusing to run. Seed nwt_strategy_genome with EU genome rows first."
        )
    cols = ["strategy_id", "track", "asset_universe", "entry_threshold",
            "stop_loss_pct", "profit_target_pct", "regime", "version"]
    return dict(zip(cols, row))


def log_to_db(conn, level: str, message: str, payload: dict | None = None) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO nwt_system_log (level, component, message, payload) "
                "VALUES (%s, %s, %s, %s)",
                (level, "EU_STRATEGIST", message, json.dumps(payload) if payload else None),
            )
        conn.commit()
    except Exception as exc:
        log.warning("DB log failed: %s", exc)


def _load_shortability_cache() -> dict:
    try:
        return json.loads(SHORTABILITY_CACHE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_shortability_cache(cache: dict) -> None:
    try:
        SHORTABILITY_CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except OSError as exc:
        log.warning("Failed to write shortability cache: %s", exc)


def get_shortability(symbol: str) -> dict | None:
    """
    {'shortable': bool, 'easy_to_borrow': bool, 'borrow_status': str} for
    `symbol`, from Alpaca's own /v2/assets/{symbol}. Cached with a bounded
    TTL (SHORTABILITY_CACHE_TTL_HOURS) — never hits the API on every symbol
    evaluation, but re-checks once the cached entry goes stale so a future
    change in borrowability is picked up. Returns None only if there is no
    usable cache entry AND the live lookup also failed — callers must treat
    None as "unknown", never as "shortable".
    """
    cache = _load_shortability_cache()
    entry = cache.get(symbol)
    if entry:
        checked_at = datetime.fromisoformat(entry["checked_at"])
        if datetime.now(timezone.utc) - checked_at < timedelta(hours=SHORTABILITY_CACHE_TTL_HOURS):
            return entry

    try:
        url = f"{ALPACA_BASE_URL}/v2/assets/{symbol}"
        resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        fresh = {
            "shortable": bool(data.get("shortable", False)),
            "easy_to_borrow": bool(data.get("easy_to_borrow", False)),
            "borrow_status": data.get("borrow_status", "unknown"),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        cache[symbol] = fresh
        _save_shortability_cache(cache)
        return fresh
    except Exception as exc:
        log.warning("Shortability lookup failed for %s: %s", symbol, exc)
        return entry  # stale cache entry if any, else None


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
    """Fetch FETCH_DAYS of daily bars. Returns symbol -> sorted DataFrame."""
    _enforce_isolation("european_price_data")  # allowed; documents intent

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
# Signal computation
# ---------------------------------------------------------------------------
def compute_z_score(closes: np.ndarray, window: int = 20) -> float:
    """Z-score of the latest close relative to the rolling window."""
    if len(closes) < window:
        return 0.0
    window_closes = closes[-window:]
    mean = np.mean(window_closes)
    std = np.std(window_closes, ddof=1)
    if std == 0:
        return 0.0
    return float((closes[-1] - mean) / std)


def ecb_lag_confidence_boost(z_score: float) -> float:
    """
    ECB policy lag boost: European assets tend to price in ECB moves slowly.
    When z-score is between -1.5 and -2.5 (moderate oversold), there is
    historical tendency for mean reversion support from ECB policy lag.
    ISOLATION: this is purely an ECB-specific signal, not US momentum.
    """
    _enforce_isolation("european_ecb_calendar")  # allowed

    if -2.5 <= z_score <= -1.5:
        # Stronger boost nearer to -2.0 (peak ECB lag effect)
        proximity = 1.0 - abs(z_score - (-2.0)) / 0.5
        boost = 0.1 * max(0.0, proximity)
        return round(boost, 4)
    return 0.0


def analyse_symbol(symbol: str, bars_df, genome: dict) -> tuple[dict | None, dict | None]:
    """
    Run mean reversion analysis on one EU symbol.

    Returns (candidate, blocked_info) — exactly one is non-None, or both are
    None (no signal at all). `blocked_info` is set when a genuine short
    signal was found (passed the entry_threshold, i.e. a real opportunity)
    but is structurally unexecutable — the asset isn't shortable on Alpaca.
    That must never be conflated with "no signal": the caller logs it as
    STRUCTURALLY_IMPOSSIBLE, not NO_SIGNAL, so the Learning System can tell
    "no edge" apart from "edge found, broker won't let us take it".

    Only short signals are gated — a long candidate is never blocked by
    shortability, since it never needs to borrow the asset.
    """
    _enforce_isolation("european_mean_reversion")

    closes = bars_df["close"].values.astype(float)
    if len(closes) < LOOKBACK_DAYS:
        log.warning("%s: only %d days of data (need %d) — skipping", symbol, len(closes), LOOKBACK_DAYS)
        return None, None

    z_score = compute_z_score(closes, LOOKBACK_DAYS)
    log.info("%s: z-score=%.3f last_close=%.4f", symbol, z_score, closes[-1])

    # Determine signal direction
    if z_score < -1.5:
        direction = "long"   # oversold — expect reversion up
    elif z_score > 1.5:
        direction = "short"  # overbought — expect reversion down
    else:
        log.info("%s: z-score %.3f within neutral band (-1.5, +1.5) — no signal", symbol, z_score)
        return None, None

    # Base confidence from z-score magnitude
    # Stronger deviation = higher confidence, capped at 0.9
    base_confidence = min(abs(z_score) / 3.0, 0.9)

    # ECB lag boost (long signals only — ECB tends to support European assets)
    ecb_boost = ecb_lag_confidence_boost(z_score) if direction == "long" else 0.0
    confidence = round(min(base_confidence + ecb_boost, 0.95), 4)

    entry_threshold = float(genome.get("entry_threshold") or 0.5)
    if confidence < entry_threshold:
        log.info("%s: confidence %.3f < entry_threshold %.3f — no signal", symbol, confidence, entry_threshold)
        return None, None

    # Shortability gate — this is a genuine opportunity (confidence cleared
    # entry_threshold); if it's a short and the asset can't be shorted, it
    # must be reported as structurally impossible, not silently dropped.
    if direction == "short":
        shortability = get_shortability(symbol)
        if shortability is None or not shortability.get("shortable", False):
            log.warning("%s: genuine short signal blocked — not shortable (%s)",
                        symbol, shortability)
            return None, {
                "symbol": symbol,
                "direction": direction,
                "z_score": round(z_score, 4),
                "confidence": confidence,
                "shortability": shortability,
            }

    # Parameters from genome — never hardcoded
    target_pct = float(genome.get("profit_target_pct") or 0.03)
    stop_pct = -abs(float(genome.get("stop_loss_pct") or 0.015))
    strategy_id = genome["strategy_id"]

    window_mean = float(np.mean(closes[-LOOKBACK_DAYS:]))
    window_std = float(np.std(closes[-LOOKBACK_DAYS:], ddof=1))

    thesis = (
        f"Mean reversion {direction}: z-score={z_score:.2f}, "
        f"20d mean={window_mean:.4f}, std={window_std:.4f}"
    )
    if ecb_boost > 0:
        thesis += f", ECB lag boost +{ecb_boost:.2f}"

    candidate = {
        "bot": BOT_NAME,
        "symbol": symbol,
        "direction": direction,
        "confidence": confidence,
        "strategy_id": strategy_id,
        "signal_quality": {
            "entry_timing_score": round(min(abs(z_score) / 2.5, 1.0), 4),
            "thesis_validity": thesis,
            "expected_move_capture": 0.70,
        },
        "expected_payoff": {
            "target_pct": target_pct,
            "stop_pct": stop_pct,
        },
        "rationale": (
            f"EU mean reversion ({direction}) — z={z_score:.2f}, "
            f"confidence={confidence:.2f}, ECB_boost={ecb_boost:.2f}"
        ),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_debug": {
            "z_score": round(z_score, 4),
            "ecb_lag_boost": ecb_boost,
            "window_mean": round(window_mean, 4),
            "window_std": round(window_std, 4),
            "last_close": float(closes[-1]),
        },
    }
    return candidate, None


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
    log.info("EU mean reversion strategist starting (09:30 UTC)")

    # Step 1: Directives gate
    directives = load_directives()

    # Regime is always a dict (JSONB)
    regime = directives.get("regime", {})
    if not isinstance(regime, dict):
        raise RuntimeError(f"regime must be dict (JSONB), got {type(regime)}")

    if directives.get("global_kill_switch", True):
        log.info("Global kill switch active — writing empty candidates and exiting")
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        return

    eu_perm = directives.get("bot_permissions", {}).get("eu", {})
    if eu_perm.get("status") == "paused":
        log.info("EU bot status=paused — writing empty candidates and exiting")
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
        genome = query_genome_eu(conn)
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
        bars_by_symbol = fetch_daily_bars(client, EU_SYMBOLS)
    except Exception as exc:
        log.error("Data fetch failed: %s", exc, exc_info=True)
        log_to_db(conn, "ERROR", f"EU data fetch failed: {exc}")
        CANDIDATES_FILE.write_text(json.dumps([], indent=2))
        conn.close()
        sys.exit(1)

    # Step 4: Analyse each symbol
    candidates = []
    for symbol in EU_SYMBOLS:
        bars = bars_by_symbol.get(symbol)
        if bars is None:
            log.warning("%s: no data available — skipping", symbol)
            log_inactivity(conn, genome["strategy_id"], f"NO_DATA_{symbol}", regime)
            continue

        candidate, blocked = analyse_symbol(symbol, bars, genome)
        if blocked:
            reason = f"STRUCTURALLY_IMPOSSIBLE_SHORT_{symbol}"
            log_inactivity(conn, genome["strategy_id"], reason, regime)
            log_to_db(conn, "WARNING",
                      f"{symbol}: genuine short signal blocked — not shortable on Alpaca",
                      blocked)
        elif candidate:
            candidates.append(candidate)
            log.info("%s: candidate generated (direction=%s confidence=%.3f)",
                     symbol, candidate["direction"], candidate["confidence"])
        else:
            log_inactivity(conn, genome["strategy_id"], f"NO_SIGNAL_{symbol}", regime)

    # Step 5: Write output
    CANDIDATES_FILE.write_text(json.dumps(candidates, indent=2))
    log.info("Wrote %d candidate(s) to %s", len(candidates), CANDIDATES_FILE)

    if not candidates:
        log_inactivity(conn, genome["strategy_id"], "NO_EU_SIGNALS_PASSED", regime)

    log_to_db(conn, "INFO", f"EU strategist complete: {len(candidates)} candidates", {
        "candidates": [c["symbol"] for c in candidates],
        "regime": regime,
    })

    conn.close()
    log.info("EU strategist done")


if __name__ == "__main__":
    main()
