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
    spot: Optional[float] = None,
) -> Optional[dict]:
    """
    Fetch the options chain snapshot for an underlying.
    Lives on the DATA API (v1beta1), not the trading API — the trading host
    404s this path (the previous /v2/options/snapshots on base_url never
    returned data for that reason).

    Without filters the endpoint returns the first 100 contracts sorted by
    OCC symbol — i.e. today's expiry, which carries no greeks/IV and is
    useless for the VIX proxy and put/call skew. When spot is known, ask
    the server for the window we actually consume: 10-60 DTE, strike within
    5% of spot.
    Returns raw snapshot dict or None on failure.
    """
    url = f"{data_url}/v1beta1/options/snapshots/{symbol}"
    headers = _alpaca_headers(api_key, secret_key)
    params = {"limit": 100}
    if spot:
        today = datetime.now(timezone.utc).date()
        params.update({
            "expiration_date_gte": (today + timedelta(days=10)).isoformat(),
            "expiration_date_lte": (today + timedelta(days=60)).isoformat(),
            "strike_price_gte": round(spot * 0.95, 2),
            "strike_price_lte": round(spot * 1.05, 2),
        })
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

def _vix_proxy_from_snapshot(snapshot: Optional[dict], spy_price: Optional[float]) -> Optional[float]:
    """
    VIX approximation: mean implied vol of near-the-money SPY options
    (strike within 5% of spot, 10-60 DTE) x 100.

    VIXY's share price must never be used here — it is an ETF dollar price
    (reverse splits, roll decay), not an index level, and comparing it to
    the VIX>40 kill-switch threshold produces false global halts whenever
    the ETF trades above $40. Missing data returns None — never a
    substitute number.
    """
    if not snapshot or not spy_price:
        return None
    contracts = snapshot.get("snapshots") or snapshot
    if not isinstance(contracts, dict):
        return None

    today = datetime.now(timezone.utc).date()
    ivs = []
    for contract_key, contract_data in contracts.items():
        if not isinstance(contract_data, dict):
            continue
        greeks = contract_data.get("greeks") or {}
        iv = greeks.get("iv") or contract_data.get("impliedVolatility")
        if not iv or float(iv) <= 0:
            continue
        # OCC symbol: ROOT + YYMMDD + C/P + strike*1000 (8 digits)
        key = contract_key.upper().strip()
        if len(key) < 15:
            continue
        try:
            strike = int(key[-8:]) / 1000.0
            expiry = datetime.strptime(key[-15:-9], "%y%m%d").date()
        except ValueError:
            continue
        dte = (expiry - today).days
        if not 10 <= dte <= 60:
            continue
        if abs(strike - spy_price) / spy_price > 0.05:
            continue
        ivs.append(float(iv))

    if not ivs:
        return None
    return round(statistics.mean(ivs) * 100.0, 2)


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
        iv = greeks.get("iv") or contract_data.get("impliedVolatility")
        if iv is None or iv <= 0:
            continue
        # Determine put vs call from contract symbol (P = put, C = call)
        # Alpaca option symbol format: SPY250620C00580000
        upper_key = contract_key.upper()
        if "P" in upper_key[6:]:  # skip the ticker prefix
            put_ivs.append(float(iv))
        elif "C" in upper_key[6:]:
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
    equity_symbols = ["SPY", "QQQ", "UUP"] + SECTOR_ETFS
    bars_map = _get_bars(
        data_url=alpaca_data_url,
        api_key=alpaca_api_key,
        secret_key=alpaca_secret_key,
        symbols=equity_symbols,
        limit=12,  # 10 trading days + buffer
    )

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

    # Options chain snapshot feeds both put/call skew and the VIX proxy —
    # spot-filtered so the server returns near-the-money 10-60 DTE contracts
    # instead of today's expiry
    snapshot = _get_options_snapshot(
        data_url=alpaca_data_url,
        api_key=alpaca_api_key,
        secret_key=alpaca_secret_key,
        symbol="SPY",
        spot=result["spy_price"],
    )
    result["put_call_skew"] = _put_call_skew(snapshot)

    # VIX approximation from SPY ATM IV (never VIXY share price)
    result["vix"] = _vix_proxy_from_snapshot(snapshot, result["spy_price"])
    if result["vix"] is None:
        logger.info("VIX proxy unavailable (no usable ATM SPY IV) — treating as None")

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
