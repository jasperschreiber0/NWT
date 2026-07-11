"""
regime_classifier.py — Portfolio Brain: Regime Classification Layer

Takes market_internals dict + portfolio exposure dict.
Returns a regime object:
    {
        "primary_regime": str,
        "confidence": float,       # 0..1
        "secondary_regime": str | None,
        "transition_risk": float,  # 0..1
    }

Valid primary_regime values:
    risk_on | risk_off | inflation_concern | recession_fear |
    geopolitical_stress | fragile_liquidity | neutral

CRITICAL RULE: if SPY is above its price 5 trading days ago,
primary_regime CANNOT be risk_off. Override enforced here.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

VALID_REGIMES = {
    "risk_on",
    "risk_off",
    "inflation_concern",
    "recession_fear",
    "geopolitical_stress",
    "fragile_liquidity",
    "neutral",
}

# "Same regime 5+ sessions must cite price evidence or reclassify to neutral"
CONSECUTIVE_SESSION_LIMIT = 5
_PRICE_EVIDENCE_THRESHOLD = 0.005  # 0.5% — matches the classifier's own smallest signal band

# ---------------------------------------------------------------------------
# Signal definitions
# Each signal function returns (regime_vote: str, strength: float 0..1)
# or None if the signal cannot fire (missing data).
# ---------------------------------------------------------------------------

def _signal_vix_extreme(internals: dict) -> Optional[tuple[str, float]]:
    """VIX > 40 → strong risk_off signal."""
    vix = internals.get("vix")
    if vix is None:
        return None
    if vix > 40:
        return ("risk_off", 0.95)
    if vix > 30:
        return ("risk_off", 0.65)
    if vix > 25:
        return ("risk_off", 0.40)
    if vix < 15:
        return ("risk_on", 0.55)
    return None


def _signal_vix_dxy_inflation(internals: dict) -> Optional[tuple[str, float]]:
    """VIX > 25 + DXY rising → inflation concern or fragile liquidity."""
    vix = internals.get("vix")
    dxy = internals.get("dxy_trend")
    if vix is None or dxy is None:
        return None
    if vix > 25 and dxy == "rising":
        return ("inflation_concern", 0.60)
    if vix > 20 and dxy == "rising":
        return ("fragile_liquidity", 0.45)
    return None


def _signal_spy_momentum(internals: dict) -> Optional[tuple[str, float]]:
    """SPY vs 5-day-ago determines directional regime signal."""
    spy_pct = internals.get("spy_vs_5d_pct")
    if spy_pct is None:
        return None
    if spy_pct > 0.03:
        return ("risk_on", 0.70)
    if spy_pct > 0.01:
        return ("risk_on", 0.50)
    if spy_pct < -0.03:
        return ("risk_off", 0.65)
    if spy_pct < -0.01:
        return ("risk_off", 0.45)
    return ("neutral", 0.30)


def _signal_breadth(internals: dict) -> Optional[tuple[str, float]]:
    """Breadth score > 0.5 → risk_on; < 0.35 → risk_off."""
    breadth = internals.get("breadth_score")
    if breadth is None:
        return None
    if breadth > 0.65:
        return ("risk_on", 0.55)
    if breadth > 0.5:
        return ("risk_on", 0.35)
    if breadth < 0.35:
        return ("risk_off", 0.50)
    return ("neutral", 0.25)


def _signal_put_call_skew(internals: dict) -> Optional[tuple[str, float]]:
    """Put/call skew > 0.15 → bearish positioning → risk_off signal."""
    skew = internals.get("put_call_skew")
    if skew is None:
        return None
    if skew > 0.20:
        return ("risk_off", 0.60)
    if skew > 0.15:
        return ("risk_off", 0.45)
    if skew < -0.05:
        # Calls more expensive than puts → complacency or bullish
        return ("risk_on", 0.40)
    return None


def _signal_sector_dispersion(internals: dict) -> Optional[tuple[str, float]]:
    """
    High sector dispersion (> 3%) → geopolitical_stress or neutral (rotation).
    Very low dispersion (< 0.5%) → crowded, possibly fragile_liquidity.
    """
    disp = internals.get("sector_dispersion")
    if disp is None:
        return None
    if disp > 0.05:
        return ("geopolitical_stress", 0.55)
    if disp > 0.03:
        return ("geopolitical_stress", 0.35)
    if disp < 0.005:
        return ("fragile_liquidity", 0.35)
    return None


def _signal_portfolio_exposure(exposure: dict) -> Optional[tuple[str, float]]:
    """
    High net delta from portfolio → slightly bullish signal context.
    Heavy net negative delta → slightly bearish.
    This informs transition_risk, not primary regime direction.
    """
    net_delta = exposure.get("net_delta_estimate")
    if net_delta is None:
        return None
    if net_delta > 0.5:
        return ("risk_on", 0.25)
    if net_delta < -0.5:
        return ("risk_off", 0.25)
    return None


# ---------------------------------------------------------------------------
# Regime aggregation
# ---------------------------------------------------------------------------

ALL_SIGNAL_FNS = [
    _signal_vix_extreme,
    _signal_vix_dxy_inflation,
    _signal_spy_momentum,
    _signal_breadth,
    _signal_put_call_skew,
    _signal_sector_dispersion,
    _signal_portfolio_exposure,
]


def _tally_signals(
    internals: dict, exposure: dict
) -> dict[str, float]:
    """
    Run all signals and accumulate weighted votes per regime.
    Returns {regime: cumulative_strength}.
    """
    votes: dict[str, float] = {}
    for fn in ALL_SIGNAL_FNS:
        sig = fn(internals) if fn != _signal_portfolio_exposure else fn(exposure)
        if sig is None:
            continue
        regime, strength = sig
        votes[regime] = votes.get(regime, 0.0) + strength
    return votes


def _compute_confidence(votes: dict[str, float], primary: str) -> float:
    """
    Confidence = primary regime's total vote mass / total vote mass across all regimes.
    Capped at 0.95.
    """
    total = sum(votes.values())
    if total == 0:
        return 0.3  # insufficient data → low confidence
    primary_mass = votes.get(primary, 0.0)
    return min(0.95, primary_mass / total)


def _compute_transition_risk(votes: dict[str, float], primary: str) -> float:
    """
    Transition risk = how much vote mass went to regimes OTHER than the primary,
    weighted against the total.
    High disagreement between signals → high transition risk.
    """
    total = sum(votes.values())
    if total == 0:
        return 0.5
    competing_mass = total - votes.get(primary, 0.0)
    raw = competing_mass / total
    # Scale: if half the mass is competing, transition_risk = 0.5; all competing = 1.0
    return min(1.0, raw)


def _secondary_regime(
    votes: dict[str, float], primary: str
) -> Optional[str]:
    """
    Return the second-highest voted regime if confidence < 0.8,
    and it has meaningful mass (> 0.15 of total).
    """
    total = sum(votes.values())
    if total == 0:
        return None
    sorted_regimes = sorted(votes.items(), key=lambda x: x[1], reverse=True)
    for regime, mass in sorted_regimes:
        if regime == primary:
            continue
        if mass / total > 0.15:
            return regime
    return None


# ---------------------------------------------------------------------------
# Session-persistence rule
# ---------------------------------------------------------------------------

def _has_price_evidence(primary: str, spy_vs_5d_pct: Optional[float]) -> bool:
    """
    Minimal test for "cite price evidence": does SPY's own 5-day move still
    support this regime label? risk_on needs SPY meaningfully up; risk_off/
    recession_fear/fragile_liquidity need SPY meaningfully down. Regimes
    without a clear price-direction implication (neutral, geopolitical_stress,
    inflation_concern) always pass — there's no single price fact that could
    contradict them the way a flat/rising SPY contradicts "risk_off".
    """
    if primary == "risk_on":
        return spy_vs_5d_pct is not None and spy_vs_5d_pct > _PRICE_EVIDENCE_THRESHOLD
    if primary in ("risk_off", "recession_fear", "fragile_liquidity"):
        return spy_vs_5d_pct is not None and spy_vs_5d_pct < -_PRICE_EVIDENCE_THRESHOLD
    return True


def _apply_session_persistence_rule(
    primary: str, regime_history: list[str], spy_vs_5d_pct: Optional[float]
) -> tuple[str, Optional[str]]:
    """
    CRITICAL RULE: same primary_regime held for 5+ consecutive prior sessions
    must cite fresh price evidence or be reclassified to neutral.
    `regime_history` is prior sessions' primary_regime values, most recent first.
    Returns (possibly-overridden primary, override_note | None).
    """
    if len(regime_history) < CONSECUTIVE_SESSION_LIMIT:
        return primary, None
    if not all(h == primary for h in regime_history[:CONSECUTIVE_SESSION_LIMIT]):
        return primary, None
    if _has_price_evidence(primary, spy_vs_5d_pct):
        return primary, None
    note = (
        f"Regime '{primary}' held for {CONSECUTIVE_SESSION_LIMIT}+ consecutive sessions "
        f"without fresh price evidence (spy_vs_5d_pct={spy_vs_5d_pct}) — reclassified to neutral"
    )
    logger.warning(note)
    return "neutral", note


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def classify_regime(
    internals: dict,
    exposure: dict,
    regime_history: Optional[list[str]] = None,
) -> dict:
    """
    Classify market regime from market internals and portfolio exposure.

    Parameters
    ----------
    internals : dict
        Output of market_internals.fetch_market_internals()
    exposure : dict
        Must contain at minimum: net_delta_estimate (float | None)
    regime_history : list[str], optional
        Prior sessions' primary_regime values, most recent first — feeds the
        "same regime 5+ sessions" rule. Omitted/empty = rule cannot fire yet
        (e.g. cold start).

    Returns
    -------
    dict with keys:
        primary_regime   : str
        confidence       : float (0..1)
        secondary_regime : str | None
        transition_risk  : float (0..1)
    """
    regime_history = regime_history or []
    vix = internals.get("vix")
    spy_vs_5d_pct = internals.get("spy_vs_5d_pct")

    # --- Hard override: VIX > 40 → risk_off, no debate ---
    if vix is not None and vix > 40:
        logger.warning("VIX=%.1f > 40 — forcing risk_off with high confidence", vix)
        return {
            "primary_regime": "risk_off",
            "confidence": 0.95,
            "secondary_regime": "fragile_liquidity",
            "transition_risk": 0.80,
        }

    # --- Run all signals and tally votes ---
    votes = _tally_signals(internals, exposure)

    if not votes:
        logger.warning("No signals fired — defaulting to neutral with low confidence")
        return {
            "primary_regime": "neutral",
            "confidence": 0.30,
            "secondary_regime": None,
            "transition_risk": 0.50,
        }

    # Determine primary regime (highest vote mass)
    primary = max(votes, key=lambda r: votes[r])

    # --- CRITICAL RULE: SPY above 5d ago → primary_regime CANNOT be risk_off ---
    if primary == "risk_off" and spy_vs_5d_pct is not None and spy_vs_5d_pct > 0:
        logger.info(
            "Overriding risk_off: SPY is +%.2f%% vs 5d ago — primary_regime cannot be risk_off",
            spy_vs_5d_pct * 100,
        )
        # Promote next best regime
        sorted_regimes = sorted(votes.items(), key=lambda x: x[1], reverse=True)
        primary = "neutral"  # safe fallback
        for regime, _ in sorted_regimes:
            if regime != "risk_off":
                primary = regime
                break

    confidence = _compute_confidence(votes, primary)
    transition_risk = _compute_transition_risk(votes, primary)

    # --- CRITICAL RULE: same regime 5+ sessions must cite price evidence ---
    primary, persistence_note = _apply_session_persistence_rule(primary, regime_history, spy_vs_5d_pct)
    if persistence_note:
        confidence = min(confidence, 0.5)
        transition_risk = max(transition_risk, 0.5)

    secondary = None
    if confidence < 0.80:
        secondary = _secondary_regime(votes, primary)

    regime_obj = {
        "primary_regime": primary,
        "confidence": round(confidence, 4),
        "secondary_regime": secondary,
        "transition_risk": round(transition_risk, 4),
    }

    logger.info(
        "Regime classified: primary=%s confidence=%.3f secondary=%s transition_risk=%.3f "
        "(votes=%s)",
        primary,
        confidence,
        secondary,
        transition_risk,
        {k: round(v, 3) for k, v in votes.items()},
    )

    return regime_obj
