"""
iv_pipeline/provider.py
Provider abstraction for options IV data. Consumers depend only on this
interface — swapping Alpaca for Polygon/Tradier must not touch any consumer.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


class IVUnavailableError(RuntimeError):
    """
    Raised when the provider returned a chain but NO contract carries
    implied volatility / greeks. This usually means the data subscription
    tier does not include IV — callers must surface this loudly, never
    silently fall back to a proxy.
    """


@dataclass
class OptionQuote:
    """Normalized single-contract quote. All providers map into this shape."""
    symbol: str                      # OCC symbol, e.g. SPY260710C00580000
    underlying: str
    expiry: date
    strike: float
    option_type: str                 # 'call' | 'put'
    bid: float = 0.0
    ask: float = 0.0
    iv: Optional[float] = None       # annualized, decimal (0.20 = 20%)
    delta: Optional[float] = None
    volume: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def has_valid_quote(self) -> bool:
        """Zero-bid strikes are untradeable noise — exclude from ATM IV."""
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    @property
    def has_iv(self) -> bool:
        return self.iv is not None and self.iv > 0


class IVProvider(ABC):
    """
    Interface every IV data source must implement.

    Contract:
      - get_chain returns ALL contracts in the expiry/strike window as
        OptionQuote, paginating internally.
      - get_chain raises IVUnavailableError if quotes exist but none carry IV.
      - get_spot returns the latest underlying trade price.
      - get_daily_closes returns up to `days` most-recent daily closes,
        oldest first (used for the realized-vol leg).
    """

    name: str = "abstract"

    @abstractmethod
    def get_chain(
        self,
        underlying: str,
        expiry_gte: date,
        expiry_lte: date,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
    ) -> list[OptionQuote]:
        ...

    @abstractmethod
    def get_spot(self, symbol: str) -> float:
        ...

    @abstractmethod
    def get_daily_closes(self, symbol: str, days: int = 25) -> list[float]:
        ...
