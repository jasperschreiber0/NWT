"""
iv_pipeline/alpaca_provider.py
Primary adapter: Alpaca options DATA API (option chain snapshots with
greeks/IV), NOT the trading API /v2/options/contracts listing — that
endpoint returns contract metadata without reliable IV, which is what
caused the old close-price-as-IV bug in layer0_builder.

Endpoint: GET {data_url}/v1beta1/options/snapshots/{underlying}
Feed: controlled by NWT_ALPACA_OPTIONS_FEED ('indicative' on the free tier,
'opra' with an options data subscription). If unset, Alpaca picks the
account default.
"""

import logging
import os
from datetime import date
from typing import Optional

import requests

from .provider import IVProvider, IVUnavailableError, OptionQuote

logger = logging.getLogger("iv_pipeline.alpaca")


def _parse_occ_symbol(occ: str) -> Optional[tuple]:
    """
    Parse an OCC option symbol like SPY260710C00580000 into
    (underlying, expiry, type, strike). Returns None on malformed input.
    """
    if len(occ) < 16:
        return None
    tail = occ[-15:]  # YYMMDD + C/P + 8-digit strike
    root = occ[:-15]
    try:
        expiry = date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6]))
        opt_type = {"C": "call", "P": "put"}[tail[6]]
        strike = int(tail[7:]) / 1000.0
    except (ValueError, KeyError):
        return None
    return root, expiry, opt_type, strike


class AlpacaIVProvider(IVProvider):
    name = "alpaca"

    def __init__(
        self,
        api_key: str = None,
        secret_key: str = None,
        data_url: str = None,
        feed: str = None,
        timeout: int = 20,
    ):
        self.api_key = api_key or os.environ["NWT_ALPACA_KEY_ID"]
        self.secret_key = secret_key or os.environ["NWT_ALPACA_SECRET_KEY"]
        self.data_url = (data_url or os.environ.get(
            "NWT_ALPACA_DATA_URL", "https://data.alpaca.markets")).rstrip("/")
        self.feed = feed or os.environ.get("NWT_ALPACA_OPTIONS_FEED") or None
        self.timeout = timeout

    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Chain snapshots (paginated)
    # ------------------------------------------------------------------

    def get_chain(
        self,
        underlying: str,
        expiry_gte: date,
        expiry_lte: date,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
    ) -> list[OptionQuote]:
        url = f"{self.data_url}/v1beta1/options/snapshots/{underlying}"
        params = {
            "expiration_date_gte": expiry_gte.isoformat(),
            "expiration_date_lte": expiry_lte.isoformat(),
            "limit": 1000,
        }
        if self.feed:
            params["feed"] = self.feed
        if strike_gte is not None:
            params["strike_price_gte"] = round(strike_gte, 2)
        if strike_lte is not None:
            params["strike_price_lte"] = round(strike_lte, 2)

        quotes: list[OptionQuote] = []
        page_token = None
        pages = 0
        while True:
            if page_token:
                params["page_token"] = page_token
            resp = requests.get(url, headers=self._headers(), params=params,
                                timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            snapshots = data.get("snapshots") or {}
            for occ, snap in snapshots.items():
                q = self._parse_snapshot(occ, snap)
                if q is not None:
                    quotes.append(q)
            page_token = data.get("next_page_token")
            pages += 1
            if not page_token or pages >= 10:
                break

        logger.info("alpaca chain %s %s..%s: %d contracts (%d with IV, feed=%s)",
                    underlying, expiry_gte, expiry_lte, len(quotes),
                    sum(1 for q in quotes if q.has_iv), self.feed or "default")

        if quotes and not any(q.has_iv for q in quotes):
            raise IVUnavailableError(
                f"Alpaca returned {len(quotes)} {underlying} contracts but none "
                f"carry implied volatility/greeks — the current data subscription "
                f"tier likely does not include IV (feed={self.feed or 'default'}). "
                f"Refusing to fall back silently."
            )
        return quotes

    @staticmethod
    def _parse_snapshot(occ: str, snap: dict) -> Optional[OptionQuote]:
        parsed = _parse_occ_symbol(occ)
        if parsed is None or not isinstance(snap, dict):
            return None
        root, expiry, opt_type, strike = parsed

        quote = snap.get("latestQuote") or {}
        greeks = snap.get("greeks") or {}
        iv = snap.get("impliedVolatility")
        if iv is None:
            iv = greeks.get("iv")  # defensive: some payloads nest IV in greeks
        daily_bar = snap.get("dailyBar") or {}

        try:
            return OptionQuote(
                symbol=occ,
                underlying=root,
                expiry=expiry,
                strike=strike,
                option_type=opt_type,
                bid=float(quote.get("bp") or 0.0),
                ask=float(quote.get("ap") or 0.0),
                iv=float(iv) if iv else None,
                delta=float(greeks["delta"]) if greeks.get("delta") is not None else None,
                volume=float(daily_bar.get("v") or 0.0),
            )
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Spot + closes
    # ------------------------------------------------------------------

    def get_spot(self, symbol: str) -> float:
        url = f"{self.data_url}/v2/stocks/{symbol}/trades/latest"
        resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return float(resp.json()["trade"]["p"])

    def get_daily_closes(self, symbol: str, days: int = 25) -> list[float]:
        from datetime import timedelta
        end = date.today()
        start = end - timedelta(days=days * 2 + 10)
        url = f"{self.data_url}/v2/stocks/{symbol}/bars"
        params = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timeframe": "1Day",
            "adjustment": "split",
            "limit": days * 2,
        }
        resp = requests.get(url, headers=self._headers(), params=params,
                            timeout=self.timeout)
        resp.raise_for_status()
        bars = resp.json().get("bars", []) or []
        closes = [float(b["c"]) for b in bars if b.get("c")]
        return closes[-days:]
