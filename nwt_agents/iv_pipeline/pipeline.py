"""
iv_pipeline/pipeline.py
Orchestration: one call computes the full IV snapshot for a ticker from any
IVProvider. Used by iv_snapshot_job.py (daily history), layer0_builder.py
(live signals) and verify_iv.py (eyeball check).

Every fetch and computation is logged with enough detail to audit a trade
decision after the fact.
"""

import logging
from datetime import date, timedelta
from typing import Optional

from .atm_iv import (
    compute_atm_iv,
    compute_put_call_volume_ratio,
    compute_put_skew_25d,
)
from .provider import IVProvider, IVUnavailableError
from .signals import hv_20d as compute_hv_20d

logger = logging.getLogger("iv_pipeline.pipeline")

# Chain fetch window: covers both the 30-DTE and 60-DTE targets
CHAIN_DTE_MIN = 7
CHAIN_DTE_MAX = 80
STRIKE_BAND_PCT = 0.15   # only fetch strikes within ±15% of spot


def compute_ticker_iv(
    provider: IVProvider,
    ticker: str,
    today: Optional[date] = None,
    closes: Optional[list[float]] = None,
) -> dict:
    """
    Full IV snapshot for one ticker:
      atm_iv_30d, atm_iv_60d, term_slope, put_skew_25d, hv_20d,
      hv_iv_spread, put_call_volume_ratio, spot, source, detail.
    Raises IVUnavailableError if the provider chain carries no IV at all
    (subscription tier problem — must be surfaced, not worked around).
    Missing individual signals are None.

    Pass `closes` (daily closes oldest-first) to skip the provider bars
    fetch for the hv_20d leg.
    """
    today = today or date.today()
    out = {
        "ticker": ticker,
        "date": today.isoformat(),
        "spot": None,
        "atm_iv_30d": None,
        "atm_iv_60d": None,
        "term_slope": None,
        "put_skew_25d": None,
        "hv_20d": None,
        "hv_iv_spread": None,
        "put_call_volume_ratio": None,
        "source": provider.name,
        "detail": {},
    }

    spot = provider.get_spot(ticker)
    out["spot"] = spot
    logger.info("%s spot=%.2f (source=%s)", ticker, spot, provider.name)

    quotes = provider.get_chain(
        ticker,
        expiry_gte=today + timedelta(days=CHAIN_DTE_MIN),
        expiry_lte=today + timedelta(days=CHAIN_DTE_MAX),
        strike_gte=spot * (1 - STRIKE_BAND_PCT),
        strike_lte=spot * (1 + STRIKE_BAND_PCT),
    )  # raises IVUnavailableError if chain has no IV — intentional

    res_30 = compute_atm_iv(quotes, spot, today, target_dte=30)
    res_60 = compute_atm_iv(quotes, spot, today, target_dte=60)

    if res_30:
        out["atm_iv_30d"] = round(res_30["iv"], 4)
        out["detail"]["atm_30"] = {
            "expiries": [str(res_30["expiry_near"]), str(res_30["expiry_far"])],
            "dtes": [res_30["dte_near"], res_30["dte_far"]],
            "strikes": [res_30["strike_near"], res_30["strike_far"]],
            "method": res_30["method"],
        }
        logger.info("%s atm_iv_30d=%.4f via %s (expiries %s/%s)", ticker,
                    res_30["iv"], res_30["method"],
                    res_30["expiry_near"], res_30["expiry_far"])
    else:
        logger.warning("%s: 30-DTE ATM IV not computable from %d quotes",
                       ticker, len(quotes))

    if res_60:
        out["atm_iv_60d"] = round(res_60["iv"], 4)

    if res_30 and res_60:
        out["term_slope"] = round(res_30["iv"] - res_60["iv"], 4)

    if res_30:
        skew = compute_put_skew_25d(quotes, today, res_30["iv"], target_dte=30)
        out["put_skew_25d"] = round(skew, 4) if skew is not None else None

    out["put_call_volume_ratio"] = compute_put_call_volume_ratio(quotes)

    # Realized leg — honestly named, never labeled IV
    try:
        if closes is None:
            closes = provider.get_daily_closes(ticker, days=25)
        hv = compute_hv_20d(closes)
        if hv is not None:
            out["hv_20d"] = round(hv, 4)
            if out["atm_iv_30d"] is not None:
                out["hv_iv_spread"] = round(out["atm_iv_30d"] - hv, 4)
    except Exception as exc:
        logger.warning("%s: hv_20d computation failed: %s", ticker, exc)

    return out
