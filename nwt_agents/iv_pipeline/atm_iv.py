"""
iv_pipeline/atm_iv.py
Pure computation: 30-DTE ATM IV from a chain of OptionQuote.

Method (per spec):
  1. Take the two expiries straddling the target DTE (default 30).
  2. At each expiry, find the ATM strike (nearest to spot) where BOTH the
     call and the put have a valid non-zero-bid quote and sane IV.
  3. Average call/put IV at that strike.
  4. Linearly interpolate the two expiry IVs by DTE to the target.

Sanity bounds: IV < 1% or > 400% is rejected and logged; a strike where
call/put IV diverge by more than 15 vol points is rejected (bad quote).
No network access in this module — fully unit-testable.
"""

import logging
from datetime import date
from typing import Optional

from .provider import OptionQuote

logger = logging.getLogger("iv_pipeline.atm_iv")

IV_MIN = 0.01            # 1%  — below this the value is noise, reject
IV_MAX = 4.00            # 400% — above this the value is noise, reject
MAX_CALL_PUT_DIVERGENCE = 0.15   # 15 vol points — reject the strike
MAX_STRIKES_TO_TRY = 5   # walk outward from ATM if nearest strikes are bad


def is_sane_iv(iv: Optional[float]) -> bool:
    if iv is None:
        return False
    if iv < IV_MIN or iv > IV_MAX:
        logger.warning("Rejected insane IV value %.4f (bounds %.2f..%.2f)",
                       iv, IV_MIN, IV_MAX)
        return False
    return True


def select_straddling_expiries(
    expiries: list[date], today: date, target_dte: int = 30
) -> Optional[tuple[date, date]]:
    """
    Pick (near, far) expiries straddling target_dte. If no expiry on one
    side exists, use the two closest available on the other side. With a
    single expiry, returns it twice (no interpolation possible).
    """
    future = sorted({e for e in expiries if e > today})
    if not future:
        return None
    if len(future) == 1:
        return future[0], future[0]

    below = [e for e in future if (e - today).days <= target_dte]
    above = [e for e in future if (e - today).days > target_dte]
    if below and above:
        return below[-1], above[0]
    pool = above or below
    if len(pool) >= 2:
        # two closest to target
        pool = sorted(pool, key=lambda e: abs((e - today).days - target_dte))
        near, far = sorted(pool[:2])
        return near, far
    return pool[0], pool[0]


def atm_iv_for_expiry(
    quotes: list[OptionQuote], spot: float, expiry: date
) -> Optional[dict]:
    """
    ATM IV at one expiry: average of call IV and put IV at the strike
    nearest spot. Skips zero-bid / missing-IV strikes, walking outward up
    to MAX_STRIKES_TO_TRY strikes. Falls back to a single side only if no
    strike offers both sides. Returns {"iv", "strike", "method"} or None.
    """
    by_strike: dict[float, dict] = {}
    for q in quotes:
        if q.expiry != expiry:
            continue
        if not q.has_valid_quote or not q.has_iv or not is_sane_iv(q.iv):
            continue
        by_strike.setdefault(q.strike, {})[q.option_type] = q

    if not by_strike:
        return None

    strikes = sorted(by_strike, key=lambda k: abs(k - spot))

    # Preferred: both call and put at the same strike
    for strike in strikes[:MAX_STRIKES_TO_TRY]:
        sides = by_strike[strike]
        call, put = sides.get("call"), sides.get("put")
        if call and put:
            divergence = abs(call.iv - put.iv)
            if divergence > MAX_CALL_PUT_DIVERGENCE:
                logger.warning(
                    "Rejected strike %.2f exp %s: call/put IV diverge %.3f > %.2f "
                    "(call=%.3f put=%.3f)",
                    strike, expiry, divergence, MAX_CALL_PUT_DIVERGENCE,
                    call.iv, put.iv,
                )
                continue
            return {
                "iv": (call.iv + put.iv) / 2.0,
                "strike": strike,
                "method": "call_put_avg",
            }

    # Degraded: one side only
    for strike in strikes[:MAX_STRIKES_TO_TRY]:
        sides = by_strike[strike]
        side = sides.get("call") or sides.get("put")
        if side:
            logger.warning("ATM IV exp %s degraded to single-sided (%s @ %.2f)",
                           expiry, side.option_type, strike)
            return {"iv": side.iv, "strike": strike,
                    "method": f"single_{side.option_type}"}
    return None


def interpolate_iv(
    iv_near: float, dte_near: int, iv_far: float, dte_far: int, target_dte: int = 30
) -> float:
    """Linear interpolation by DTE, clamped to the endpoints (no extrapolation)."""
    if dte_far == dte_near:
        return iv_near
    w = (target_dte - dte_near) / (dte_far - dte_near)
    w = max(0.0, min(1.0, w))
    return iv_near + w * (iv_far - iv_near)


def compute_atm_iv(
    quotes: list[OptionQuote], spot: float, today: date, target_dte: int = 30
) -> Optional[dict]:
    """
    Full 30-DTE (or target) ATM IV from a chain. Returns
    {"iv", "expiry_near", "expiry_far", "dte_near", "dte_far",
     "strike_near", "strike_far", "method"} or None if not computable.
    """
    if spot <= 0 or not quotes:
        return None
    expiries = select_straddling_expiries([q.expiry for q in quotes], today, target_dte)
    if expiries is None:
        return None
    near, far = expiries

    res_near = atm_iv_for_expiry(quotes, spot, near)
    res_far = res_near if far == near else atm_iv_for_expiry(quotes, spot, far)
    if res_near is None and res_far is None:
        return None
    if res_near is None or res_far is None:
        # one usable expiry — take it as-is, flag in method
        usable, exp = (res_far, far) if res_near is None else (res_near, near)
        iv = usable["iv"]
        if not is_sane_iv(iv):
            return None
        return {
            "iv": iv,
            "expiry_near": exp, "expiry_far": exp,
            "dte_near": (exp - today).days, "dte_far": (exp - today).days,
            "strike_near": usable["strike"], "strike_far": usable["strike"],
            "method": usable["method"] + "_single_expiry",
        }

    dte_near, dte_far = (near - today).days, (far - today).days
    iv = interpolate_iv(res_near["iv"], dte_near, res_far["iv"], dte_far, target_dte)
    if not is_sane_iv(iv):
        return None
    return {
        "iv": iv,
        "expiry_near": near, "expiry_far": far,
        "dte_near": dte_near, "dte_far": dte_far,
        "strike_near": res_near["strike"], "strike_far": res_far["strike"],
        "method": f"{res_near['method']}+{res_far['method']}",
    }


def compute_put_skew_25d(
    quotes: list[OptionQuote], today: date, atm_iv: float, target_dte: int = 30
) -> Optional[float]:
    """
    25-delta put skew = IV of the put with delta nearest -0.25 (on the
    expiry nearest target DTE) minus ATM IV. Requires greeks; returns None
    if no put deltas are available.
    """
    puts = [
        q for q in quotes
        if q.option_type == "put" and q.delta is not None
        and q.has_valid_quote and q.has_iv and is_sane_iv(q.iv)
        and q.expiry > today
    ]
    if not puts or atm_iv is None:
        return None
    # nearest expiry to target DTE among puts
    best_expiry = min({p.expiry for p in puts},
                      key=lambda e: abs((e - today).days - target_dte))
    candidates = [p for p in puts if p.expiry == best_expiry]
    target = min(candidates, key=lambda p: abs(abs(p.delta) - 0.25))
    if abs(abs(target.delta) - 0.25) > 0.10:
        # nothing remotely near 25-delta — refuse rather than mislabel
        return None
    return target.iv - atm_iv


def compute_put_call_volume_ratio(quotes: list[OptionQuote]) -> Optional[float]:
    """Put/call ratio from daily contract volume (real flow, not contract counts)."""
    call_vol = sum(q.volume for q in quotes if q.option_type == "call")
    put_vol = sum(q.volume for q in quotes if q.option_type == "put")
    if call_vol <= 0:
        return None
    return put_vol / call_vol
