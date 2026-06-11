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
from iv_pipeline.alpaca_provider import AlpacaIVProvider
from iv_pipeline.pipeline import compute_ticker_iv
from iv_pipeline.provider import IVUnavailableError
from iv_pipeline.signals import compute_rank_signals
from iv_pipeline.store import get_iv_series
from iv_pipeline.vol_regime import classify_vol_regime
from shared_context import get_db, load_master_directives, log_inactivity, log_system_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("layer0_builder")

AGENTS_DIR = Path(os.environ.get("NWT_AGENTS_DIR", Path(__file__).parent))
ALPACA_DATA_URL = os.environ.get("NWT_ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")

ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ["NWT_ALPACA_KEY_ID"],
    "APCA-API-SECRET-KEY": os.environ["NWT_ALPACA_SECRET_KEY"],
}

SYMBOLS = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA", "VGK", "FXI", "KWEB", "MCHI"]


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


_EMPTY_IV_FIELDS = {
    "iv": 0.0,                 # 30-DTE ATM IV — 0.0 means MISSING, hard-filtered downstream
    "iv_rank": None,
    "iv_percentile": None,
    "iv_confidence": "low",
    "iv_history_days": 0,
    "term_slope": None,
    "put_skew_25d": None,
    "hv_20d": None,
    "hv_iv_spread": None,
}


def fetch_symbol_iv(provider, conn, symbol: str, closes: list) -> dict:
    """
    Real IV fields for one symbol via the IV pipeline:
    30-DTE ATM IV (interpolated, sanity-bounded) + rank/percentile with a
    confidence label from nwt_iv_history, term slope, 25-delta put skew,
    and the honestly-named hv_20d / hv_iv_spread realized leg.

    On IVUnavailableError (data tier has no IV) re-raises — caller must
    surface it, never proxy. Other failures return _EMPTY_IV_FIELDS.
    """
    try:
        snap = compute_ticker_iv(provider, symbol, closes=closes)
    except IVUnavailableError:
        raise
    except Exception as exc:
        logger.warning("IV pipeline failed for %s: %s", symbol, exc)
        return {**_EMPTY_IV_FIELDS, "put_call_volume_ratio": None}

    atm_iv = snap["atm_iv_30d"]
    fields = {
        **_EMPTY_IV_FIELDS,
        "iv": atm_iv or 0.0,
        "term_slope": snap["term_slope"],
        "put_skew_25d": snap["put_skew_25d"],
        "hv_20d": snap["hv_20d"],
        "hv_iv_spread": snap["hv_iv_spread"],
        "put_call_volume_ratio": snap["put_call_volume_ratio"],
    }
    if atm_iv:
        try:
            history = get_iv_series(conn, symbol)
            fields.update(compute_rank_signals(history, atm_iv))
        except Exception as exc:
            logger.warning("IV history read failed for %s: %s", symbol, exc)
    return fields


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

    logger.info("Fetching market data for %d symbols", len(SYMBOLS))

    provider = AlpacaIVProvider()
    iv_tier_blocked = False

    # Fetch earnings calendar
    earnings_map = fetch_earnings_within_5d(SYMBOLS)

    # Build per-symbol data
    symbols_data = {}
    for symbol in SYMBOLS:
        try:
            bars = fetch_bars(symbol, days=25)
            if len(bars) < 2:
                logger.warning("Insufficient bars for %s (%d bars)", symbol, len(bars))
                symbols_data[symbol] = {
                    "price": 0.0,
                    "momentum_5d": 0.0,
                    "rsi_14": 50.0,
                    "atr_14": 0.0,
                    **_EMPTY_IV_FIELDS,
                    "earnings_within_5d": earnings_map.get(symbol, False),
                    "error": "insufficient_bars",
                }
                continue

            closes = [float(b["c"]) for b in bars]
            current_price = closes[-1]
            momentum_5d = compute_momentum_5d(closes)
            rsi_14 = compute_rsi(closes)
            atr_14 = compute_atr(bars)

            # Real IV via pipeline — every optionable symbol, not just SPY/QQQ
            if iv_tier_blocked:
                iv_fields = {**_EMPTY_IV_FIELDS, "put_call_volume_ratio": None}
            else:
                try:
                    iv_fields = fetch_symbol_iv(provider, conn, symbol, closes)
                except IVUnavailableError as exc:
                    # Subscription tier has no IV — surface loudly ONCE,
                    # leave IV missing (downstream hard-filters iv<=0).
                    iv_tier_blocked = True
                    logger.error("IV UNAVAILABLE on current Alpaca data tier: %s", exc)
                    log_system_event(conn, "ERROR", "layer0_builder",
                                     "IV unavailable — Alpaca data tier has no "
                                     "greeks/IV; no symbol will pass IV gates today",
                                     {"error": str(exc)})
                    iv_fields = {**_EMPTY_IV_FIELDS, "put_call_volume_ratio": None}

            symbols_data[symbol] = {
                "price": round(current_price, 4),
                "momentum_5d": momentum_5d,
                "rsi_14": rsi_14,
                "atr_14": atr_14,
                **iv_fields,
                "earnings_within_5d": earnings_map.get(symbol, False),
            }
            logger.info(
                "%s: price=%.2f mom=%.3f rsi=%.1f atr=%.2f iv=%.3f rank=%s conf=%s earnings=%s",
                symbol, current_price, momentum_5d, rsi_14, atr_14,
                iv_fields["iv"], iv_fields["iv_rank"], iv_fields["iv_confidence"],
                earnings_map.get(symbol, False),
            )
        except Exception as exc:
            logger.error("Failed to process %s: %s", symbol, exc)
            symbols_data[symbol] = {
                "price": 0.0,
                "momentum_5d": 0.0,
                "rsi_14": 50.0,
                "atr_14": 0.0,
                **_EMPTY_IV_FIELDS,
                "earnings_within_5d": False,
                "error": str(exc),
            }

    spy = symbols_data.get("SPY", {})

    # VIX-comparable level: SPY 30-DTE ATM IV x 100 (a real options-market
    # measure). The old VIXY-share-price hack is gone — VIXY's price is an
    # ETF NAV, not a vol index. 0.0 still means MISSING, never a signal.
    vix = round(spy.get("iv", 0.0) * 100, 2)
    vix_source = "spy_atm_iv_30d_x100" if vix > 0 else "unavailable"

    # Vol regime filter — consumed by Risk Agent and track sizing:
    # stressed → halt premium selling; elevated → half size; calm → normal
    vol_regime = classify_vol_regime(vix if vix > 0 else None, spy.get("term_slope"))
    logger.info("VIX proxy: %.2f (%s) | vol_regime=%s (%s)",
                vix, vix_source, vol_regime["regime"], vol_regime["reason"])

    output = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "vix": vix,
        "vix_source": vix_source,
        "vol_regime": vol_regime,
        "symbols": symbols_data,
        "put_call_ratio": spy.get("put_call_volume_ratio") or 1.0,
        "spy_iv_skew": spy.get("put_skew_25d") or 0.0,
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
        {"symbols": list(symbols_data.keys()), "vix": vix,
         "vix_source": vix_source, "vol_regime": vol_regime["regime"],
         "iv_tier_blocked": iv_tier_blocked},
    )
    conn.close()


if __name__ == "__main__":
    main()
