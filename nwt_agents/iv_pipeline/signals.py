"""
iv_pipeline/signals.py
History-derived IV signals + the honestly-named realized-vol leg.

The old system fed HV-flavored proxies (and even option close prices)
into gates labeled "IV". Realized vol stays useful as the realized leg of
the vol risk premium signal — but it is named hv_20d, never "iv".
"""

import math
from typing import Optional

# Confidence bands for history-window depth (trading days)
CONFIDENCE_LOW_BELOW = 90
CONFIDENCE_HIGH_FROM = 250

IV_RANK_WINDOW_DAYS = 252   # 52 trading weeks


def hv_20d(closes: list[float], window: int = 20) -> Optional[float]:
    """
    Annualized 20-day historical (realized) volatility from daily closes,
    oldest first. Returns None with insufficient data.
    """
    closes = [c for c in closes if c and c > 0]
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(len(closes) - window, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def iv_rank(history: list[float], current: float) -> Optional[float]:
    """
    (current − window low) / (window high − window low), over whatever
    history exists (bootstrap-aware — confidence is reported separately).
    Flat history → 0.5 (no information, neutral). Empty history → None.
    """
    vals = [v for v in history if v is not None and v > 0]
    if not vals:
        return None
    lo, hi = min(vals + [current]), max(vals + [current])
    if hi == lo:
        return 0.5
    return max(0.0, min(1.0, (current - lo) / (hi - lo)))


def iv_percentile(history: list[float], current: float) -> Optional[float]:
    """Fraction of history days with IV strictly below current. Empty → None."""
    vals = [v for v in history if v is not None and v > 0]
    if not vals:
        return None
    return sum(1 for v in vals if v < current) / len(vals)


def confidence_label(history_days: int) -> str:
    """low < 90 days, medium 90–249, high 250+."""
    if history_days >= CONFIDENCE_HIGH_FROM:
        return "high"
    if history_days >= CONFIDENCE_LOW_BELOW:
        return "medium"
    return "low"


def compute_rank_signals(history: list[float], current: float) -> dict:
    """
    Bundle of history-dependent signals for one ticker.
    history = atm_iv_30d series oldest-first, up to 252 most-recent days.
    """
    window = [v for v in history if v is not None and v > 0][-IV_RANK_WINDOW_DAYS:]
    return {
        "iv_rank": iv_rank(window, current),
        "iv_percentile": iv_percentile(window, current),
        "iv_history_days": len(window),
        "iv_confidence": confidence_label(len(window)),
    }
