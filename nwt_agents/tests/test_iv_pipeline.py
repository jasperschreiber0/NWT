"""
Unit tests for nwt_agents/iv_pipeline — pure math, no network, no DB.
Run from nwt_agents/: python3 -m pytest tests/ -v
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from iv_pipeline.atm_iv import (
    MAX_CALL_PUT_DIVERGENCE,
    atm_iv_for_expiry,
    compute_atm_iv,
    compute_put_skew_25d,
    interpolate_iv,
    is_sane_iv,
    select_straddling_expiries,
)
from iv_pipeline.provider import OptionQuote
from iv_pipeline.signals import (
    confidence_label,
    hv_20d,
    iv_percentile,
    iv_rank,
)
from iv_pipeline.vol_regime import (
    classify_vol_regime,
    is_premium_selling,
    premium_selling_multiplier,
)

TODAY = date(2026, 6, 11)


def make_quote(strike, opt_type, iv, expiry, bid=1.0, ask=1.2, delta=None,
               underlying="SPY"):
    return OptionQuote(
        symbol=f"{underlying}{expiry.strftime('%y%m%d')}"
               f"{'C' if opt_type == 'call' else 'P'}{int(strike * 1000):08d}",
        underlying=underlying, expiry=expiry, strike=strike,
        option_type=opt_type, bid=bid, ask=ask, iv=iv, delta=delta,
    )


# ---------------------------------------------------------------------------
# Interpolation math
# ---------------------------------------------------------------------------

class TestInterpolation:
    def test_exact_midpoint(self):
        assert interpolate_iv(0.20, 20, 0.30, 40, target_dte=30) == pytest.approx(0.25)

    def test_weighted_toward_near(self):
        # target 30, near 25, far 45 → weight 0.25 toward far
        assert interpolate_iv(0.20, 25, 0.40, 45, 30) == pytest.approx(0.25)

    def test_same_dte_returns_near(self):
        assert interpolate_iv(0.22, 30, 0.50, 30, 30) == 0.22

    def test_clamped_no_extrapolation(self):
        # both expiries above target → clamps to nearer endpoint
        assert interpolate_iv(0.20, 35, 0.30, 50, 30) == pytest.approx(0.20)
        assert interpolate_iv(0.20, 10, 0.30, 20, 30) == pytest.approx(0.30)


class TestExpirySelection:
    def test_straddling(self):
        exps = [TODAY + timedelta(days=d) for d in (10, 25, 38, 60)]
        near, far = select_straddling_expiries(exps, TODAY, 30)
        assert (near - TODAY).days == 25 and (far - TODAY).days == 38

    def test_all_above_target(self):
        exps = [TODAY + timedelta(days=d) for d in (40, 55, 70)]
        near, far = select_straddling_expiries(exps, TODAY, 30)
        assert (near - TODAY).days == 40 and (far - TODAY).days == 55

    def test_single_expiry(self):
        exps = [TODAY + timedelta(days=21)]
        near, far = select_straddling_expiries(exps, TODAY, 30)
        assert near == far == exps[0]

    def test_no_future_expiries(self):
        assert select_straddling_expiries([TODAY - timedelta(days=1)], TODAY, 30) is None


# ---------------------------------------------------------------------------
# ATM IV extraction
# ---------------------------------------------------------------------------

class TestAtmIV:
    def test_call_put_average_at_atm(self):
        exp = TODAY + timedelta(days=30)
        quotes = [
            make_quote(100, "call", 0.20, exp),
            make_quote(100, "put", 0.24, exp),
            make_quote(110, "call", 0.30, exp),
            make_quote(110, "put", 0.32, exp),
        ]
        res = atm_iv_for_expiry(quotes, spot=101.0, expiry=exp)
        assert res["strike"] == 100 and res["iv"] == pytest.approx(0.22)
        assert res["method"] == "call_put_avg"

    def test_zero_bid_strike_skipped(self):
        exp = TODAY + timedelta(days=30)
        quotes = [
            make_quote(100, "call", 0.20, exp, bid=0.0),   # zero bid — skip
            make_quote(100, "put", 0.24, exp),
            make_quote(105, "call", 0.26, exp),
            make_quote(105, "put", 0.28, exp),
        ]
        res = atm_iv_for_expiry(quotes, spot=100.0, expiry=exp)
        assert res["strike"] == 105 and res["iv"] == pytest.approx(0.27)

    def test_divergent_call_put_rejected(self):
        exp = TODAY + timedelta(days=30)
        quotes = [
            make_quote(100, "call", 0.20, exp),
            make_quote(100, "put", 0.20 + MAX_CALL_PUT_DIVERGENCE + 0.01, exp),
            make_quote(105, "call", 0.25, exp),
            make_quote(105, "put", 0.27, exp),
        ]
        res = atm_iv_for_expiry(quotes, spot=100.0, expiry=exp)
        assert res["strike"] == 105  # divergent ATM strike skipped

    def test_missing_iv_handled(self):
        exp = TODAY + timedelta(days=30)
        quotes = [make_quote(100, "call", None, exp), make_quote(100, "put", None, exp)]
        assert atm_iv_for_expiry(quotes, spot=100.0, expiry=exp) is None

    def test_full_30dte_interpolation(self):
        e1, e2 = TODAY + timedelta(days=20), TODAY + timedelta(days=40)
        quotes = [
            make_quote(100, "call", 0.20, e1), make_quote(100, "put", 0.20, e1),
            make_quote(100, "call", 0.30, e2), make_quote(100, "put", 0.30, e2),
        ]
        res = compute_atm_iv(quotes, spot=100.0, today=TODAY, target_dte=30)
        assert res["iv"] == pytest.approx(0.25)
        assert res["dte_near"] == 20 and res["dte_far"] == 40

    def test_empty_chain(self):
        assert compute_atm_iv([], spot=100.0, today=TODAY) is None

    def test_sanity_bounds(self):
        assert not is_sane_iv(0.005)   # < 1%
        assert not is_sane_iv(4.5)     # > 400%
        assert not is_sane_iv(None)
        assert is_sane_iv(0.01) and is_sane_iv(0.22) and is_sane_iv(4.0)

    def test_insane_iv_rejected_in_chain(self):
        exp = TODAY + timedelta(days=30)
        quotes = [
            make_quote(100, "call", 9.0, exp),   # 900% — insane, rejected
            make_quote(100, "put", 9.0, exp),
        ]
        assert compute_atm_iv(quotes, spot=100.0, today=TODAY) is None


class TestPutSkew:
    def test_25_delta_skew(self):
        exp = TODAY + timedelta(days=30)
        quotes = [
            make_quote(95, "put", 0.26, exp, delta=-0.26),
            make_quote(90, "put", 0.30, exp, delta=-0.15),
            make_quote(100, "put", 0.22, exp, delta=-0.50),
        ]
        skew = compute_put_skew_25d(quotes, TODAY, atm_iv=0.22)
        assert skew == pytest.approx(0.04)

    def test_no_deltas_returns_none(self):
        exp = TODAY + timedelta(days=30)
        quotes = [make_quote(95, "put", 0.26, exp, delta=None)]
        assert compute_put_skew_25d(quotes, TODAY, atm_iv=0.22) is None

    def test_nothing_near_25_delta_refused(self):
        exp = TODAY + timedelta(days=30)
        quotes = [make_quote(100, "put", 0.22, exp, delta=-0.55)]
        assert compute_put_skew_25d(quotes, TODAY, atm_iv=0.22) is None


# ---------------------------------------------------------------------------
# IV rank / percentile edge cases
# ---------------------------------------------------------------------------

class TestIVRank:
    def test_normal_rank(self):
        history = [0.10, 0.20, 0.30]
        assert iv_rank(history, 0.20) == pytest.approx(0.5)

    def test_flat_history(self):
        assert iv_rank([0.20, 0.20, 0.20], 0.20) == 0.5

    def test_single_day_history(self):
        # one prior day, current above it → rank 1.0
        assert iv_rank([0.20], 0.30) == pytest.approx(1.0)

    def test_empty_history(self):
        assert iv_rank([], 0.25) is None

    def test_history_with_nones_and_zeros(self):
        assert iv_rank([None, 0.0, 0.10, 0.30], 0.20) == pytest.approx(0.5)

    def test_current_outside_history_clamped(self):
        # current becomes the new high → rank 1.0, never > 1
        assert iv_rank([0.10, 0.20], 0.50) == pytest.approx(1.0)

    def test_percentile(self):
        history = [0.10, 0.15, 0.20, 0.25]
        assert iv_percentile(history, 0.22) == pytest.approx(0.75)

    def test_percentile_empty(self):
        assert iv_percentile([], 0.22) is None


class TestConfidence:
    def test_bands(self):
        assert confidence_label(0) == "low"
        assert confidence_label(89) == "low"
        assert confidence_label(90) == "medium"
        assert confidence_label(249) == "medium"
        assert confidence_label(250) == "high"
        assert confidence_label(400) == "high"


class TestHV:
    def test_constant_prices_zero_vol(self):
        assert hv_20d([100.0] * 25) == pytest.approx(0.0)

    def test_insufficient_data(self):
        assert hv_20d([100.0] * 10) is None

    def test_known_volatility_positive(self):
        closes = [100 * (1.01 if i % 2 else 0.99) ** (i % 3 + 1) for i in range(25)]
        assert hv_20d(closes) > 0


# ---------------------------------------------------------------------------
# Vol regime bands (boundaries pinned)
# ---------------------------------------------------------------------------

class TestVolRegime:
    def test_calm(self):
        assert classify_vol_regime(15.0, -0.01)["regime"] == "calm"

    def test_calm_boundary_is_elevated(self):
        assert classify_vol_regime(20.0, 0.0)["regime"] == "elevated"

    def test_elevated(self):
        assert classify_vol_regime(24.0, 0.0)["regime"] == "elevated"

    def test_stressed_boundary(self):
        assert classify_vol_regime(28.0, 0.0)["regime"] == "stressed"

    def test_backwardation_escalates_calm(self):
        res = classify_vol_regime(15.0, 0.03)
        assert res["regime"] == "elevated" and res["backwardation"]

    def test_backwardation_escalates_elevated(self):
        assert classify_vol_regime(24.0, 0.05)["regime"] == "stressed"

    def test_backwardation_at_threshold_not_triggered(self):
        assert classify_vol_regime(15.0, 0.02)["regime"] == "calm"

    def test_missing_vix_unknown_never_calm(self):
        assert classify_vol_regime(None, 0.0)["regime"] == "unknown"
        assert classify_vol_regime(0.0, 0.0)["regime"] == "unknown"

    def test_multipliers(self):
        assert premium_selling_multiplier("calm") == 1.0
        assert premium_selling_multiplier("elevated") == 0.5
        assert premium_selling_multiplier("stressed") == 0.0
        assert premium_selling_multiplier("unknown") == 0.5
        # low IV-history confidence caps at half size even in calm
        assert premium_selling_multiplier("calm", iv_confidence="low") == 0.5
        assert premium_selling_multiplier("stressed", iv_confidence="low") == 0.0

    def test_premium_selling_classification(self):
        assert is_premium_selling("iron_condor")
        assert not is_premium_selling("long_call")
        assert not is_premium_selling("bull_call_spread")
        assert not is_premium_selling(None)
