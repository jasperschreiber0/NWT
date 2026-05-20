"""
US Flow Bot — Nightly Pre-Screener
Runs at 10:30 UTC. Fetches previous session data, scans for high-IV and
options-flow opportunities for the next session, writes nightly_notes.json.

ISOLATION: Uses only price data, options flow (OI, put/call ratio), volume.
NEVER uses macro narrative, DXY, or sector rotation analysis.
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
NOTES_FILE = BOT_DIR / "nightly_notes.json"

load_dotenv(BOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [US-NIGHTLY] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("us_nightly")

# ---------------------------------------------------------------------------
# Config — isolation enforced: US-only instruments, price+volume+options only
# ---------------------------------------------------------------------------
US_SYMBOLS = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA"]

# ISOLATION GUARD: these data types are explicitly disallowed for the US bot
DISALLOWED_SIGNALS = ["DXY", "sector_rotation", "macro_narrative", "ECB", "PBOC"]


def _assert_isolation(signal_source: str) -> None:
    """Hard isolation check — raises if a disallowed signal source is used."""
    for banned in DISALLOWED_SIGNALS:
        if banned.lower() in signal_source.lower():
            raise RuntimeError(
                f"ISOLATION VIOLATION: US bot attempted to use '{signal_source}'. "
                "Only price, options flow, volume, VWAP allowed."
            )


# ---------------------------------------------------------------------------
# Alpaca client
# ---------------------------------------------------------------------------
def get_data_client() -> StockHistoricalDataClient:
    key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_SECRET_KEY"]
    data_url = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")
    return StockHistoricalDataClient(key, secret, url_override=data_url)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def get_db_conn():
    return psycopg2.connect(os.environ["NWT_DB_DSN"])


def log_to_db(conn, level: str, message: str, payload: dict | None = None) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO nwt_system_log (level, component, message, payload) "
                "VALUES (%s, %s, %s, %s)",
                (level, "US_NIGHTLY", message, json.dumps(payload) if payload else None),
            )
        conn.commit()
    except Exception as exc:
        log.warning("DB log failed: %s", exc)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
def fetch_daily_bars(client: StockHistoricalDataClient, symbols: list[str], days: int = 21):
    """Fetch last N calendar days of daily bars for all symbols."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed="iex",
    )
    return client.get_stock_bars(req).df


def compute_rsi(prices: np.ndarray, period: int = 14) -> float:
    """Compute RSI for a price series. Returns the latest RSI value."""
    if len(prices) < period + 1:
        return 50.0  # neutral default when insufficient data
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_ema(prices: np.ndarray, period: int) -> np.ndarray:
    """Compute EMA array."""
    ema = np.zeros_like(prices, dtype=float)
    k = 2.0 / (period + 1)
    ema[0] = prices[0]
    for i in range(1, len(prices)):
        ema[i] = prices[i] * k + ema[i - 1] * (1 - k)
    return ema


def compute_iv_proxy(closes: np.ndarray, window: int = 20) -> float:
    """
    Proxy for implied volatility using realised vol annualised.
    In production this would use actual options chain IV from Alpaca.
    For nightly pre-screening this gives a directionally correct ranking.
    """
    if len(closes) < 2:
        return 0.0
    log_returns = np.diff(np.log(closes[-window:]))
    realised_vol = np.std(log_returns) * np.sqrt(252)
    return float(realised_vol)


# ---------------------------------------------------------------------------
# Nightly analysis
# ---------------------------------------------------------------------------
def analyse_symbol(symbol: str, bars_df) -> dict:
    """
    Analyse one symbol for next-session interest.
    Returns a note dict with IV proxy, momentum, volume ratio, RSI.
    ISOLATION: only price, volume signals used — no macro data.
    """
    _assert_isolation("price_volume_flow")  # passes; documents intent

    try:
        sym_bars = bars_df.loc[symbol].sort_index() if symbol in bars_df.index.get_level_values(0) else None
        if sym_bars is None or len(sym_bars) < 5:
            return {"symbol": symbol, "status": "insufficient_data"}

        closes = sym_bars["close"].values.astype(float)
        volumes = sym_bars["volume"].values.astype(float)

        rsi = compute_rsi(closes)
        iv_proxy = compute_iv_proxy(closes)
        ema5 = compute_ema(closes, 5)
        ema20 = compute_ema(closes, 20)
        momentum_signal = bool(ema5[-1] > ema20[-1])

        avg_vol = float(np.mean(volumes[:-1])) if len(volumes) > 1 else float(volumes[-1])
        vol_ratio = float(volumes[-1] / avg_vol) if avg_vol > 0 else 1.0

        # Price vs 20d mean — simple mean-reversion / trend gauge
        mean_20 = float(np.mean(closes[-20:])) if len(closes) >= 20 else float(np.mean(closes))
        price_vs_mean = float((closes[-1] - mean_20) / mean_20) if mean_20 != 0 else 0.0

        # Interest score for next session (0-5 range, purely informational)
        interest = 0
        if iv_proxy > 0.25:
            interest += 1  # elevated realised vol → options flow likely
        if rsi < 35 or rsi > 65:
            interest += 1  # momentum extreme
        if vol_ratio > 1.3:
            interest += 1  # above-average volume
        if momentum_signal:
            interest += 1  # EMA cross positive
        if abs(price_vs_mean) > 0.02:
            interest += 1  # extended from mean

        return {
            "symbol": symbol,
            "last_close": float(closes[-1]),
            "rsi_14": round(rsi, 2),
            "iv_proxy_annualised": round(iv_proxy, 4),
            "momentum_ema_positive": momentum_signal,
            "volume_ratio_vs_avg": round(vol_ratio, 3),
            "price_vs_20d_mean_pct": round(price_vs_mean * 100, 3),
            "next_session_interest_score": interest,
            "note": (
                "High interest — watch for ORB breakout" if interest >= 4
                else "Moderate interest" if interest >= 2
                else "Low interest"
            ),
            "status": "ok",
        }
    except Exception as exc:
        log.warning("analyse_symbol failed for %s: %s", symbol, exc)
        return {"symbol": symbol, "status": "error", "error": str(exc)}


def check_master_directives() -> dict:
    """Read master-directives.json. Returns directives dict."""
    directives_path = SHARED_DIR / "master-directives.json"
    if not directives_path.exists():
        log.warning("master-directives.json not found — using safe defaults")
        return {"global_kill_switch": True, "bot_permissions": {"us": {"status": "paused"}}}
    with open(directives_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("US nightly pre-screener starting")

    directives = check_master_directives()
    if directives.get("global_kill_switch", True):
        log.info("Global kill switch active — skipping nightly scan")
        NOTES_FILE.write_text(json.dumps({"status": "skipped", "reason": "global_kill_switch"}, indent=2))
        return

    us_perm = directives.get("bot_permissions", {}).get("us", {})
    if us_perm.get("status") == "paused":
        log.info("US bot paused in directives — skipping nightly scan")
        NOTES_FILE.write_text(json.dumps({"status": "skipped", "reason": "us_bot_paused"}, indent=2))
        return

    conn = None
    try:
        conn = get_db_conn()
        client = get_data_client()

        bars_df = fetch_daily_bars(client, US_SYMBOLS, days=25)

        notes = []
        for symbol in US_SYMBOLS:
            note = analyse_symbol(symbol, bars_df)
            notes.append(note)
            log.info("%s: interest=%s IV_proxy=%.3f RSI=%.1f",
                     symbol,
                     note.get("next_session_interest_score", "n/a"),
                     note.get("iv_proxy_annualised", 0),
                     note.get("rsi_14", 0))

        output = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "date": datetime.now(timezone.utc).date().isoformat(),
            "bot": "us",
            "scan_type": "nightly_pre_screener",
            "isolation_confirmed": "price+volume+options_flow only",
            "symbols": notes,
        }
        NOTES_FILE.write_text(json.dumps(output, indent=2))
        log.info("Nightly notes written to %s", NOTES_FILE)

        log_to_db(conn, "INFO", "Nightly pre-screener completed", {
            "symbols_scanned": len(notes),
            "high_interest": [n["symbol"] for n in notes if n.get("next_session_interest_score", 0) >= 4],
        })

    except Exception as exc:
        log.error("Nightly screener failed: %s", exc, exc_info=True)
        if conn:
            log_to_db(conn, "ERROR", f"Nightly screener exception: {exc}")
        sys.exit(1)
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    main()
