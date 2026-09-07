"""
Unit tests for shadow_decision_evaluator.py's simulate_outcome() — pure
function, no DB needed. Covers the generalisation this task adds: MFE/MAE
tracking and completion state, on top of the existing would_have_won logic
that already worked for Track C/D/E and (per this task) now also serves
Track A equities directly (no proxy needed — the underlying IS the
tradeable instrument).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import shadow_decision_evaluator as sde  # noqa: E402


def _bar(h, l, c):
    return {"h": h, "l": l, "c": c}


def test_target_hit_reports_completion_and_mfe():
    bars = [_bar(101, 99, 100.5), _bar(103, 100, 102.8)]  # target 3% hit on day 2
    won, exit_price, pnl_pct, mfe, mae, completion = sde.simulate_outcome(
        bars, "long", entry_price=100.0, target_pct=0.03, stop_pct=-0.015,
    )
    assert won is True
    assert completion == "TARGET_HIT"
    assert pnl_pct == 0.03
    assert mfe >= 0.03  # favorable excursion reached at least the target


def test_stop_hit_reports_completion_and_mae():
    bars = [_bar(100.5, 98, 98.5)]  # -1.5% stop hit day 1
    won, exit_price, pnl_pct, mfe, mae, completion = sde.simulate_outcome(
        bars, "long", entry_price=100.0, target_pct=0.03, stop_pct=-0.015,
    )
    assert won is False
    assert completion == "STOP_HIT"
    assert pnl_pct == -0.015
    assert mae >= 0.015


def test_horizon_expired_when_neither_threshold_touched():
    bars = [_bar(100.5, 99.5, 100.2), _bar(100.8, 99.8, 100.4)]
    won, exit_price, pnl_pct, mfe, mae, completion = sde.simulate_outcome(
        bars, "long", entry_price=100.0, target_pct=0.03, stop_pct=-0.015,
    )
    assert completion == "HORIZON_EXPIRED"
    assert exit_price == 100.4  # resolves at final close


def test_short_direction_mfe_mae_use_correct_sign():
    """For a short, favorable = price falling; adverse = price rising."""
    bars = [_bar(99, 96, 96.5)]  # price fell 4% low -> favorable for a short
    won, exit_price, pnl_pct, mfe, mae, completion = sde.simulate_outcome(
        bars, "short", entry_price=100.0, target_pct=0.03, stop_pct=-0.015,
    )
    assert won is True
    assert completion == "TARGET_HIT"
    assert mfe >= 0.03


def test_empty_bars_returns_all_none():
    result = sde.simulate_outcome([], "long", entry_price=100.0, target_pct=0.03, stop_pct=-0.015)
    assert result == (None, None, None, None, None, None)
