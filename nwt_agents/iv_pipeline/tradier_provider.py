"""
iv_pipeline/tradier_provider.py
Stub adapter — shape only, no API key provisioned yet.

When activated: Tradier market data
GET https://api.tradier.com/v1/markets/options/chains?symbol=X&expiration=Y&greeks=true
includes greeks.mid_iv / greeks.smv_vol per contract.
"""

from datetime import date
from typing import Optional

from .provider import IVProvider, OptionQuote


class TradierIVProvider(IVProvider):
    name = "tradier"

    def __init__(self, access_token: str = None):
        self.access_token = access_token  # TRADIER_ACCESS_TOKEN when provisioned

    def get_chain(
        self,
        underlying: str,
        expiry_gte: date,
        expiry_lte: date,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
    ) -> list[OptionQuote]:
        raise NotImplementedError(
            "TradierIVProvider is a stub — no access token provisioned. "
            "Implement against /v1/markets/options/chains with greeks=true."
        )

    def get_spot(self, symbol: str) -> float:
        raise NotImplementedError("TradierIVProvider is a stub")

    def get_daily_closes(self, symbol: str, days: int = 25) -> list[float]:
        raise NotImplementedError("TradierIVProvider is a stub")
