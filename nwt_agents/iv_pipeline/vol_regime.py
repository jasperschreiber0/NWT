"""
iv_pipeline/vol_regime.py
Volatility regime filter: calm | elevated | stressed | unknown.

Inputs:
  - VIX-comparable level. Alpaca has no real VIX index feed, so the
    system uses SPY 30-DTE ATM IV × 100 as the level (a true
    options-market measure, unlike the old VIXY-share-price hack).
  - Term structure: term_slope = 30d ATM IV − 60d ATM IV. Positive beyond
    BACKWARDATION_THRESHOLD = backwardation (near-term fear) and escalates
    the regime one notch.

Consumption contract (wired into Risk Agent + track sizing):
  stressed → halt new premium-selling entries
  elevated → half size for premium selling
  calm     → normal
  unknown  → treated like elevated (conservative)
"""

from typing import Optional

VIX_CALM_BELOW = 20.0        # level < 20 → calm
VIX_STRESSED_FROM = 28.0     # level >= 28 → stressed
BACKWARDATION_THRESHOLD = 0.02   # 30d IV above 60d IV by 2+ vol points

_ESCALATE = {"calm": "elevated", "elevated": "stressed", "stressed": "stressed"}

# Strategy types that are net short premium — the only types the vol
# regime gate throttles. Debit structures are unaffected.
PREMIUM_SELLING_TYPES = {"iron_condor"}


def classify_vol_regime(
    vix_level: Optional[float], term_slope: Optional[float]
) -> dict:
    """
    Returns {"regime", "vix_level", "term_slope", "backwardation", "reason"}.
    vix_level None/0 is missing data → regime 'unknown' (never treat 0 as calm).
    """
    if vix_level is None or vix_level <= 0:
        return {
            "regime": "unknown",
            "vix_level": None,
            "term_slope": term_slope,
            "backwardation": None,
            "reason": "vix level unavailable — conservative default",
        }

    if vix_level < VIX_CALM_BELOW:
        regime = "calm"
    elif vix_level < VIX_STRESSED_FROM:
        regime = "elevated"
    else:
        regime = "stressed"

    backwardation = term_slope is not None and term_slope > BACKWARDATION_THRESHOLD
    reason = f"vix_level={vix_level:.1f}"
    if backwardation:
        regime = _ESCALATE[regime]
        reason += f", backwardation (term_slope={term_slope:+.3f} > {BACKWARDATION_THRESHOLD})"

    return {
        "regime": regime,
        "vix_level": round(vix_level, 2),
        "term_slope": round(term_slope, 4) if term_slope is not None else None,
        "backwardation": backwardation,
        "reason": reason,
    }


def premium_selling_multiplier(regime: str, iv_confidence: str = "high") -> float:
    """
    Sizing multiplier for premium-selling strategies.
    stressed → 0.0 (halt), elevated/unknown → 0.5, calm → 1.0.
    Low IV-rank confidence (bootstrap window) caps the multiplier at 0.5 —
    'low confidence is a no-trade or reduced-size signal'.
    """
    mult = {"calm": 1.0, "elevated": 0.5, "stressed": 0.0}.get(regime, 0.5)
    if iv_confidence == "low":
        mult = min(mult, 0.5)
    return mult


def is_premium_selling(strategy_type: str) -> bool:
    return (strategy_type or "").lower() in PREMIUM_SELLING_TYPES
