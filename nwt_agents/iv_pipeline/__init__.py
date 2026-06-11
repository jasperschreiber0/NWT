"""
nwt_agents/iv_pipeline — real implied volatility data pipeline.

Replaces the old layer0 "average IV over 50 arbitrary contracts" (which fell
back to option CLOSE PRICE when implied_volatility was absent) with a
provider-abstracted, 30-DTE ATM IV computation plus history-backed IV rank,
percentile, term structure, skew and vol-regime signals.

Modules:
    provider          IVProvider interface + OptionQuote dataclass
    alpaca_provider   Alpaca options data API adapter (primary)
    polygon_provider  stub adapter (no key yet)
    tradier_provider  stub adapter (no key yet)
    atm_iv            30-DTE ATM IV interpolation + skew + sanity bounds
    signals           iv_rank / iv_percentile / hv_20d / confidence
    vol_regime        calm | elevated | stressed classification
    store             nwt_iv_history Postgres read/write
"""

from .provider import IVProvider, IVUnavailableError, OptionQuote  # noqa: F401
