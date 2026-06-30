"""
nwt_agents/layer0_builder.py
Runs at 13:00 UTC. Builds the raw data layer for the conviction engine.

Fetches price bars, computes technical indicators, checks earnings proximity,
pulls IV data, and writes layer0_data.json.
"""

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import integrity_gate
from shared_context import get_db, load_master_directives, log_inactivity, log_system_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("layer0_builder")

AGENTS_DIR = Path(os.environ.get("NWT_AGENTS_DIR", Path(__file__).parent))

def _strip_alpaca_url(raw: str) -> str:
    """Strip trailing slash and accidental /v2 suffix — a common .env misconfiguration
    that turns every API call into /v2/v2/... and causes silent 404s on all data fetches."""
    url = raw.rstrip("/")
    if url.endswith("/v2"):
        url = url[:-3]
    return url

ALPACA_DATA_URL = _strip_alpaca_url(os.environ.get("NWT_ALPACA_DATA_URL", "https://data.alpaca.markets"))
ALPACA_BASE_URL = _strip_alpaca_url(os.environ.get("NWT_ALPACA_BASE_URL", "https://paper-api.alpaca.markets"))

ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ["NWT_ALPACA_KEY_ID"],
    "APCA-API-SECRET-KEY": os.environ["NWT_ALPACA_SECRET_KEY"],
}

SYMBOLS = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA", "VGK", "FXI", "KWEB", "MCHI"]
OPTIONS_SYMBOLS = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA"]


# ---------------------------------------------------------------------------
# Alpaca data helpers
# ---------------------------------------------------------------------------

def fetch_bars(symbol: str, days: int = 25) -> list:
    """Fetch daily bars for a symbol. Returns list of bar dicts."""
    end = date.today()
    start = end - timedelta(days=days + 10)  # Extra buffer for market holidays
    url = f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/bars"
    params = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timeframe": "1Day",
        "adjustment": "split",
        "limit": 50,
    }
    resp = requests.get(url, headers=ALPACA_HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    bars = data.get("bars", [])
    return bars[-days:] if len(bars) >= days else bars


def fetch_options_snapshot(symbol: str) -> dict:
    """
    Fetch options snapshot for a symbol.
    Returns dict with iv, put_call_ratio, spy_iv_skew approximation.
    """
    url = f"{ALPACA_BASE_URL}/v2/options/contracts"
    # Get near-term (7-30 DTE) contracts
    today = date.today()
    exp_min = (today + timedelta(days=7)).isoformat()
    exp_max = (today + timedelta(days=30)).isoformat()
    params = {
        "underlying_symbols": symbol,
        "expiration_date_gte": exp_min,
        "expiration_date_lte": exp_max,
        "limit": 50,
    }
    try:
        resp = requests.get(url, headers=ALPACA_HEADERS, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        contracts = data.get("option_contracts", []) if isinstance(data, dict) else data
        if not contracts:
            return {"iv": 0.0, "put_call_ratio": 1.0, "iv_skew": 0.0}

        # Compute average IV from contracts that have implied_volatility.
        # Never fall back to close_price — contract dollar prices are not IV.
        ivs_call = []
        ivs_put = []
        for c in contracts:
            raw_iv = c.get("implied_volatility")
            if raw_iv is None:
                continue
            try:
                iv_float = float(raw_iv)
                # Sanity range: 1% to 300% annualised. Reject anything outside.
                if 0.01 <= iv_float <= 3.0:
                    if c.get("type") == "call":
                        ivs_call.append(iv_float)
                    elif c.get("type") == "put":
                        ivs_put.append(iv_float)
            except (TypeError, ValueError):
                continue

        all_ivs = ivs_call + ivs_put
        avg_iv = float(np.mean(all_ivs)) if all_ivs else 0.0
        avg_call_iv = float(np.mean(ivs_call)) if ivs_call else 0.0
        avg_put_iv = float(np.mean(ivs_put)) if ivs_put else 0.0

        n_calls = len(ivs_call)
        n_puts = len(ivs_put)
        put_call_ratio = (n_puts / n_calls) if n_calls > 0 else 1.0
        iv_skew = avg_put_iv - avg_call_iv  # positive = put skew elevated

        return {
            "iv": avg_iv,
            "put_call_ratio": round(put_call_ratio, 3),
            "iv_skew": round(iv_skew, 4),
        }
    except Exception as exc:
        logger.warning("Options snapshot for %s failed: %s", symbol, exc)
        return {"iv": 0.0, "put_call_ratio": 1.0, "iv_skew": 0.0}


def fetch_vix() -> float:
    """Fetch VIX level. Returns 0.0 on failure (treat as missing, not signal)."""
    try:
        url = f"{ALPACA_DATA_URL}/v2/stocks/VIXY/trades/latest"
        resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        price = float(data["trade"]["p"])
        # VIXY ~= VIX/10 as a rough proxy; use raw price as indicator
        return price
    except Exception as exc:
        logger.warning("VIX fetch failed (using 0.0 as missing): %s", exc)
        return 0.0


# ---------------------------------------------------------------------------
# Technical indicators
# ---------------------------------------------------------------------------

def compute_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0  # Neutral default
    closes_arr = np.array(closes, dtype=float)
    deltas = np.diff(closes_arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def compute_atr(bars: list, period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    true_ranges = []
    for i in range(1, len(bars)):
        high = float(bars[i]["h"])
        low = float(bars[i]["l"])
        prev_close = float(bars[i - 1]["c"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    if not true_ranges:
        return 0.0
    atr_vals = true_ranges[-period:]
    return round(float(np.mean(atr_vals)), 4)


def compute_momentum_5d(closes: list) -> float:
    """5-day price momentum: (close[-1] - close[-6]) / close[-6]"""
    if len(closes) < 6:
        return 0.0
    return round((closes[-1] - closes[-6]) / closes[-6], 4)


def compute_historical_vol(closes: list, period: int = 20) -> float:
    """Annualised historical volatility from log returns — used as IV proxy when options data is absent."""
    if len(closes) < period + 1:
        return 0.0
    log_returns = np.diff(np.log(np.array(closes[-(period + 1):], dtype=float)))
    return round(float(np.std(log_returns) * np.sqrt(252)), 4)


# ---------------------------------------------------------------------------
# Earnings check via Nasdaq API
# ---------------------------------------------------------------------------

def fetch_earnings_within_5d(symbols: list) -> dict:
    """
    Check Nasdaq earnings calendar for the next 5 trading days.
    Returns dict: {symbol: bool}
    """
    symbols_upper = {s.upper() for s in symbols}
    result = {s: False for s in symbols}

    for days_ahead in range(6):
        check_date = date.today() + timedelta(days=days_ahead)
        if check_date.weekday() >= 5:  # Skip weekends
            continue
        url = "https://api.nasdaq.com/api/calendar/earnings"
        params = {"date": check_date.isoformat()}
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            rows = (
                data.get("data", {}).get("rows", [])
                if isinstance(data.get("data"), dict)
                else []
            )
            for row in rows:
                ticker = (row.get("symbol") or "").upper()
                if ticker in symbols_upper:
                    result[ticker] = True
        except Exception as exc:
            logger.warning("Earnings fetch for %s failed: %s", check_date, exc)

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    integrity_gate.run_integrity_gate()

    conn = get_db()

    # Check global kill switch
    try:
        directives = load_master_directives()
        if directives.get("global_kill_switch", False):
            logger.warning("Global kill switch active — logging inactivity for all strategies and exiting")
            regime = directives.get("regime", {})
            all_strategy_ids = [f"C{i}" for i in range(1, 13)] + \
                               [f"D{i}" for i in range(1, 13)] + \
                               [f"E{i}" for i in range(1, 13)]
            track_map = {**{f"C{i}": "C" for i in range(1, 13)},
                         **{f"D{i}": "D" for i in range(1, 13)},
                         **{f"E{i}": "E" for i in range(1, 13)}}
            for sid in all_strategy_ids:
                log_inactivity(conn, sid, track_map[sid], "GLOBAL_KILL_SWITCH", regime)
            log_system_event(conn, "WARNING", "layer0_builder", "Kill switch active — layer0 skipped")
            conn.close()
            sys.exit(0)
    except FileNotFoundError:
        logger.warning("master-directives.json not found — proceeding without kill switch check")
        directives = {}

    logger.info("Alpaca DATA URL: %s", ALPACA_DATA_URL)
    logger.info("Alpaca BASE URL: %s", ALPACA_BASE_URL)
    logger.info("Fetching market data for %d symbols", len(SYMBOLS))

    # Fetch VIX
    vix = fetch_vix()
    logger.info("VIX: %.2f", vix)

    # Fetch options snapshots for all optionable symbols in our universe
    options_snaps = {}
    for sym in OPTIONS_SYMBOLS:
        options_snaps[sym] = fetch_options_snapshot(sym)

    spy_snap = options_snaps.get("SPY", {"iv": 0.0, "put_call_ratio": 1.0, "iv_skew": 0.0})
    put_call_ratio = spy_snap["put_call_ratio"]
    spy_iv_skew = spy_snap["iv_skew"]

    # Fetch earnings calendar
    earnings_map = fetch_earnings_within_5d(SYMBOLS)

    # Build per-symbol data
    symbols_data = {}
    for symbol in SYMBOLS:
        try:
            bars = fetch_bars(symbol, days=20)
            if len(bars) < 2:
                logger.warning("Insufficient bars for %s (%d bars)", symbol, len(bars))
                symbols_data[symbol] = {
                    "price": 0.0,
                    "momentum_5d": 0.0,
                    "rsi_14": 50.0,
                    "atr_14": 0.0,
                    "iv": 0.0,
                    "iv_source": "missing",
                    "earnings_within_5d": earnings_map.get(symbol, False),
                    "error": "insufficient_bars",
                }
                continue

            closes = [float(b["c"]) for b in bars]
            current_price = closes[-1]
            momentum_5d = compute_momentum_5d(closes)
            rsi_14 = compute_rsi(closes)
            atr_14 = compute_atr(bars)

            # IV: live options snapshot first, then historical vol proxy.
            # Historical vol is a reasonable estimate when options data is absent —
            # it avoids symbols being scored blind by Haiku on iv=0.
            snap_iv = options_snaps.get(symbol, {}).get("iv", 0.0)
            if snap_iv > 0.0:
                iv = snap_iv
                iv_source = "options"
            else:
                iv = compute_historical_vol(closes)
                iv_source = "histvol"

            symbols_data[symbol] = {
                "price": round(current_price, 4),
                "momentum_5d": momentum_5d,
                "rsi_14": rsi_14,
                "atr_14": atr_14,
                "iv": round(iv, 4),
                "iv_source": iv_source,
                "earnings_within_5d": earnings_map.get(symbol, False),
            }
            logger.info(
                "%s: price=%.2f mom=%.3f rsi=%.1f atr=%.2f iv=%.3f(%s) earnings=%s",
                symbol, current_price, momentum_5d, rsi_14, atr_14, iv, iv_source,
                earnings_map.get(symbol, False),
            )
        except Exception as exc:
            logger.error("Failed to process %s: %s", symbol, exc)
            symbols_data[symbol] = {
                "price": 0.0,
                "momentum_5d": 0.0,
                "rsi_14": 50.0,
                "atr_14": 0.0,
                "iv": 0.0,
                "iv_source": "missing",
                "earnings_within_5d": False,
                "error": str(exc),
            }

    output = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "vix": vix,
        "symbols": symbols_data,
        "put_call_ratio": put_call_ratio,
        "spy_iv_skew": spy_iv_skew,
    }

    out_path = AGENTS_DIR / "layer0_data.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    logger.info("layer0_data.json written to %s", out_path)
    log_system_event(
        conn,
        "INFO",
        "layer0_builder",
        "Layer 0 data built successfully",
        {"symbols": list(symbols_data.keys()), "vix": vix},
    )
    conn.close()


if __name__ == "__main__":
    main()
