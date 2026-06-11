"""
market_internals.py — Portfolio Brain: Market Data Layer
Fetches Day-1 market internals from Alpaca.
Returns None for any field that cannot be reliably computed.
Never raises on missing data — caller handles gracefully.
"""

import os
import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _alpaca_headers(api_key: str, secret_key: str) -> dict:
    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
        "Accept": "application/json",
    }


def _get_bars(
    data_url: str,
    api_key: str,
    secret_key: str,
    symbols: list[str],
    limit: int = 10,
) -> dict[str, list[dict]]:
    """
    Fetch daily bars for a list of symbols via the Alpaca Data API.
    Returns {symbol: [bar, ...]} — oldest bar first.
    Returns {} on any error so callers can handle gracefully.
    """
    url = f"{data_url}/v2/stocks/bars"
    start = (datetime.now(timezone.utc) - timedelta(days=limit * 2)).strftime("%Y-%m-%d")
    params = {
        "symbols": ",".join(symbols),
        "timeframe": "1Day",
        "start": start,
        "adjustment": "raw",
    }
    headers = _alpaca_headers(api_key, secret_key)
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        bars_map = data.get("bars", {})
        # Alpaca returns bars newest-first in some endpoints; sort oldest-first
        result = {}
        for sym, bars in bars_map.items():
            result[sym] = sorted(bars, key=lambda b: b["t"])
        return result
    except Exception as exc:
        logger.warning("Failed to fetch bars for %s: %s", symbols, exc)
        return {}


def _get_options_snapshot(
    data_url: str,
    api_key: str,
    secret_key: str,
    symbol: str = "SPY",
) -> Optional[dict]:
    """
    Fetch option chain snapshot (greeks/IV included) from the Alpaca DATA
    API — the trading API has no snapshot endpoint, which is why this
    previously always returned None.
    Returns raw snapshot dict or None on failure.
    """
    url = f"{data_url}/v1beta1/options/snapshots/{symbol}"
    headers = _alpaca_headers(api_key, secret_key)
    params = {"limit": 500}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch options snapshot for %s: %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# VIX
# ---------------------------------------------------------------------------

def _extract_vix(bars_map: dict) -> Optional[float]:
    """
    Return most-recent VIX close.
    Returns None if VIX data is missing or zero (zero = bad feed, not signal).
    """
    vix_bars = bars_map.get("VIXY") or bars_map.get("VIX")
    if not vix_bars:
        return None
    last = vix_bars[-1]
    val = last.get("c") or last.get("close")
    if val is None or val == 0:
        return None
    return float(val)


# ---------------------------------------------------------------------------
# DXY direction
# ---------------------------------------------------------------------------

def _extract_dxy_trend(bars_map: dict) -> Optional[str]:
    """
    Return 'rising', 'falling', or 'flat'.
    Uses UUP (USD Bull ETF) as DXY proxy since DXY is not directly on Alpaca.
    Returns None if data unavailable.
    """
    dxy_bars = bars_map.get("UUP")
    if not dxy_bars or len(dxy_bars) < 3:
        return None
    closes = [b.get("c", b.get("close", 0)) for b in dxy_bars]
    closes = [c for c in closes if c and c > 0]
    if len(closes) < 3:
        return None
    # Simple 3-day trend: compare latest to 3-bar-ago
    delta_pct = (closes[-1] - closes[-3]) / closes[-3]
    if delta_pct > 0.002:
        return "rising"
    if delta_pct < -0.002:
        return "falling"
    return "flat"


# ---------------------------------------------------------------------------
# SPY vs 5 trading days ago
# ---------------------------------------------------------------------------

def _spy_vs_5d(bars_map: dict) -> tuple[Optional[float], Optional[float]]:
    """
    Returns (spy_current_price, spy_vs_5d_pct).
    spy_vs_5d_pct > 0 means SPY is above where it was 5 trading days ago.
    Returns (None, None) if insufficient data.
    """
    spy_bars = bars_map.get("SPY")
    if not spy_bars or len(spy_bars) < 6:
        return None, None
    # bars are oldest-first; last bar is today, bar at index[-6] is 5 sessions ago
    current_close = spy_bars[-1].get("c", spy_bars[-1].get("close"))
    past_close = spy_bars[-6].get("c", spy_bars[-6].get("close"))
    if not current_close or not past_close or past_close == 0:
        return None, None
    current_close = float(current_close)
    past_close = float(past_close)
    pct = (current_close - past_close) / past_close
    return current_close, pct


# ---------------------------------------------------------------------------
# Breadth score (SPY vs QQQ relative momentum)
# ---------------------------------------------------------------------------

def _breadth_score(bars_map: dict) -> Optional[float]:
    """
    Breadth proxy: 5-day return of SPY vs QQQ.
    Score > 0.5 means broad market participating (both positive and SPY >= QQQ).
    Score < 0.5 means narrow leadership or declining.
    Returns None if data unavailable.
    """
    spy_bars = bars_map.get("SPY")
    qqq_bars = bars_map.get("QQQ")
    if not spy_bars or not qqq_bars or len(spy_bars) < 6 or len(qqq_bars) < 6:
        return None

    def _ret5(bars):
        closes = [b.get("c", b.get("close", 0)) for b in bars]
        closes = [c for c in closes if c and c > 0]
        if len(closes) < 6:
            return None
        return (closes[-1] - closes[-6]) / closes[-6]

    spy_ret = _ret5(spy_bars)
    qqq_ret = _ret5(qqq_bars)
    if spy_ret is None or qqq_ret is None:
        return None

    # Both positive and SPY not lagging badly → broad participation
    score = 0.5  # neutral baseline
    if spy_ret > 0 and qqq_ret > 0:
        score += 0.25  # both up
    elif spy_ret < 0 and qqq_ret < 0:
        score -= 0.25  # both down
    # SPY keeping pace with QQQ signals broader breadth
    if spy_ret >= qqq_ret * 0.8:
        score += 0.15
    else:
        score -= 0.10
    # Clip to [0, 1]
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# Put/call skew from SPY options snapshot
# ---------------------------------------------------------------------------

def _put_call_skew(snapshot: Optional[dict]) -> Optional[float]:
    """
    Compute a simple put/call skew from SPY options snapshot.
    skew = (avg_put_iv - avg_call_iv) / avg_call_iv
    Positive skew means puts are more expensive → bearish positioning.
    Returns None if data unavailable.
    """
    if not snapshot:
        return None

    # Alpaca options snapshot format: {"snapshots": {"SPY..": {greeks: {}, ...}}}
    # or flat dict depending on endpoint version
    contracts = snapshot.get("snapshots") or snapshot
    if not contracts or not isinstance(contracts, dict):
        return None

    put_ivs = []
    call_ivs = []

    for contract_key, contract_data in contracts.items():
        if not isinstance(contract_data, dict):
            continue
        greeks = contract_data.get("greeks") or {}
        iv = contract_data.get("impliedVolatility") or greeks.get("iv")
        if iv is None or iv <= 0:
            continue
        # OCC symbol format: ROOT + YYMMDD + C/P + 8-digit strike.
        # The C/P flag is always 9 chars from the end (strike is 8 digits) —
        # scanning the whole tail matched date digits and root letters.
        upper_key = contract_key.upper()
        if len(upper_key) < 16:
            continue
        cp_flag = upper_key[-9]
        if cp_flag == "P":
            put_ivs.append(float(iv))
        elif cp_flag == "C":
            call_ivs.append(float(iv))

    if not put_ivs or not call_ivs:
        return None

    avg_put = statistics.mean(put_ivs)
    avg_call = statistics.mean(call_ivs)
    if avg_call == 0:
        return None

    return (avg_put - avg_call) / avg_call


# ---------------------------------------------------------------------------
# Sector dispersion
# ---------------------------------------------------------------------------

SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI"]


def _sector_dispersion(bars_map: dict) -> Optional[float]:
    """
    Sector dispersion = std deviation of 5-day returns across sector ETFs.
    High dispersion (>0.03 = 3%) signals rotation or stress; low = synchronised move.
    Returns None if fewer than 3 sectors have data.
    """
    returns = []
    for etf in SECTOR_ETFS:
        etf_bars = bars_map.get(etf)
        if not etf_bars or len(etf_bars) < 6:
            continue
        closes = [b.get("c", b.get("close", 0)) for b in etf_bars]
        closes = [c for c in closes if c and c > 0]
        if len(closes) < 6:
            continue
        ret = (closes[-1] - closes[-6]) / closes[-6]
        returns.append(ret)

    if len(returns) < 3:
        return None
    if len(returns) == 1:
        return 0.0
    return statistics.stdev(returns)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def fetch_market_internals(
    alpaca_api_key: str,
    alpaca_secret_key: str,
    alpaca_base_url: str,
    alpaca_data_url: str,
) -> dict:
    """
    Fetch all Day-1 market internals from Alpaca.

    Returns a dict with keys:
        vix               float | None   — current VIX level (None if zero/missing)
        dxy_trend         str | None     — 'rising' | 'falling' | 'flat'
        spy_price         float | None   — current SPY close
        spy_vs_5d_ago     float | None   — SPY price 5 trading days ago close
        spy_vs_5d_pct     float | None   — % change SPY vs 5 trading days ago
        breadth_score     float | None   — 0..1 breadth proxy
        put_call_skew     float | None   — positive = bearish skew
        sector_dispersion float | None   — std dev of 5d returns across sectors

    Never raises. All fields may be None on error.
    """
    result: dict = {
        "vix": None,
        "dxy_trend": None,
        "spy_price": None,
        "spy_vs_5d_ago": None,
        "spy_vs_5d_pct": None,
        "breadth_score": None,
        "put_call_skew": None,
        "sector_dispersion": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # All symbols needed for one batch call
    equity_symbols = ["SPY", "QQQ", "VIXY", "UUP"] + SECTOR_ETFS
    bars_map = _get_bars(
        data_url=alpaca_data_url,
        api_key=alpaca_api_key,
        secret_key=alpaca_secret_key,
        symbols=equity_symbols,
        limit=12,  # 10 trading days + buffer
    )

    # VIX via VIXY proxy (VIX futures ETF — tradeable on Alpaca paper)
    result["vix"] = _extract_vix(bars_map)
    if result["vix"] is None:
        logger.info("VIX data missing or zero — treating as None")

    # DXY direction via UUP proxy
    result["dxy_trend"] = _extract_dxy_trend(bars_map)

    # SPY vs 5 trading days ago
    spy_price, spy_vs_5d_pct = _spy_vs_5d(bars_map)
    result["spy_price"] = spy_price
    result["spy_vs_5d_pct"] = spy_vs_5d_pct
    if spy_vs_5d_pct is not None and len(bars_map.get("SPY", [])) >= 6:
        spy_bars = bars_map["SPY"]
        result["spy_vs_5d_ago"] = float(
            spy_bars[-6].get("c", spy_bars[-6].get("close", 0))
        )

    # Breadth score
    result["breadth_score"] = _breadth_score(bars_map)

    # Put/call skew from options snapshot (DATA API — greeks/IV live there)
    snapshot = _get_options_snapshot(
        data_url=alpaca_data_url,
        api_key=alpaca_api_key,
        secret_key=alpaca_secret_key,
        symbol="SPY",
    )
    result["put_call_skew"] = _put_call_skew(snapshot)

    # Sector dispersion
    result["sector_dispersion"] = _sector_dispersion(bars_map)

    logger.info(
        "Market internals fetched: vix=%s dxy=%s spy_vs_5d_pct=%.4f breadth=%.2f pc_skew=%s sector_disp=%s",
        result["vix"],
        result["dxy_trend"],
        result["spy_vs_5d_pct"] or 0.0,
        result["breadth_score"] or 0.0,
        result["put_call_skew"],
        result["sector_dispersion"],
    )

    return result
