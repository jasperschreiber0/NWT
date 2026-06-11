"""
iv_pipeline/polygon_provider.py
Stub adapter — shape only, no API key provisioned yet.

When activated: Polygon options snapshots
GET https://api.polygon.io/v3/snapshot/options/{underlying}
include implied_volatility and greeks per contract.
"""

from datetime import date
from typing import Optional

from .provider import IVProvider, OptionQuote


class PolygonIVProvider(IVProvider):
    name = "polygon"

    def __init__(self, api_key: str = None):
        self.api_key = api_key  # POLYGON_API_KEY when provisioned

    def get_chain(
        self,
        underlying: str,
        expiry_gte: date,
        expiry_lte: date,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
    ) -> list[OptionQuote]:
        raise NotImplementedError(
            "PolygonIVProvider is a stub — no API key provisioned. "
            "Implement against /v3/snapshot/options/{underlying}."
        )

    def get_spot(self, symbol: str) -> float:
        raise NotImplementedError("PolygonIVProvider is a stub")

    def get_daily_closes(self, symbol: str, days: int = 25) -> list[float]:
        raise NotImplementedError("PolygonIVProvider is a stub")
