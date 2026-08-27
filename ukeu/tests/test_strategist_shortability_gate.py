"""
Regression tests for the EWU short-shortability gate.

Incident: EU_BOT kept emitting short signals on EWU (Alpaca reports it
shortable=False, hard-to-borrow). Nothing checked shortability before the
signal became a candidate -> ticket -> approved trade, so it sailed through
risk approval and 422'd at the broker repeatedly (6x across 5 sessions,
Aug 21-26 2026), contributing nothing to nwt_trade_outcomes and getting
logged as a generic execution FAILED indistinguishable from a real
slippage/API failure.

analyse_symbol() now gates short signals on shortability before they can
become a candidate, and reports a genuine-but-unexecutable short as
STRUCTURALLY_IMPOSSIBLE (via the (candidate, blocked) tuple) rather than
silently as NO_SIGNAL.
"""
import json
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import strategist  # noqa: E402


GENOME = {
    "strategy_id": "EU-MR-001",
    "entry_threshold": 0.5,
    "profit_target_pct": 0.03,
    "stop_loss_pct": 0.015,
}

NOT_SHORTABLE = {
    "shortable": False, "easy_to_borrow": False, "borrow_status": "hard_to_borrow",
    "checked_at": "2026-08-27T00:00:00+00:00",
}
SHORTABLE = {
    "shortable": True, "easy_to_borrow": True, "borrow_status": "easy_to_borrow",
    "checked_at": "2026-08-27T00:00:00+00:00",
}


def _bars_for_z_score(target_z: float, window: int = strategist.LOOKBACK_DAYS) -> pd.DataFrame:
    """
    Synthetic (fixed-seed) closes whose LAST value has z-score ~= target_z
    against the preceding window — independent of real market data.
    """
    rng = np.random.default_rng(42)
    base = 100 + rng.normal(0, 0.3, size=window)
    mean = base.mean()
    std = base.std(ddof=1)
    last = mean + target_z * std
    closes = np.concatenate([base, [last]])
    return pd.DataFrame({"close": closes})


# ---------------------------------------------------------------------------
# Gate behavior
# ---------------------------------------------------------------------------

@patch.object(strategist, "get_shortability")
def test_short_on_non_shortable_asset_is_blocked_not_silently_dropped(mock_shortability):
    mock_shortability.return_value = NOT_SHORTABLE
    bars = _bars_for_z_score(2.5)  # overbought -> short signal

    candidate, blocked = strategist.analyse_symbol("EWU", bars, GENOME)

    assert candidate is None, "a structurally impossible short must never become a candidate"
    assert blocked is not None, "must be reported, not silently treated as no-signal"
    assert blocked["symbol"] == "EWU"
    assert blocked["direction"] == "short"
    assert blocked["shortability"] == NOT_SHORTABLE
    assert blocked["outcome_reason"] == "STRUCTURALLY_IMPOSSIBLE"
    mock_shortability.assert_called_once_with("EWU")


@patch.object(strategist, "get_shortability")
def test_short_on_shortable_asset_is_unaffected(mock_shortability):
    mock_shortability.return_value = SHORTABLE
    bars = _bars_for_z_score(2.5)

    candidate, blocked = strategist.analyse_symbol("VGK", bars, GENOME)

    assert blocked is None
    assert candidate is not None
    assert candidate["direction"] == "short"


@patch.object(strategist, "get_shortability")
def test_long_signal_on_ewu_is_never_gated_or_checked(mock_shortability):
    """Requirement: do not block long trades merely because the asset is non-shortable."""
    bars = _bars_for_z_score(-2.5)  # oversold -> long signal

    candidate, blocked = strategist.analyse_symbol("EWU", bars, GENOME)

    assert blocked is None
    assert candidate is not None
    assert candidate["direction"] == "long"
    mock_shortability.assert_not_called()


@patch.object(strategist, "get_shortability")
def test_unknown_shortability_fails_safe_and_blocks_short(mock_shortability):
    """A failed lookup (None) must never be treated as 'shortable'."""
    mock_shortability.return_value = None
    bars = _bars_for_z_score(2.5)

    candidate, blocked = strategist.analyse_symbol("EWU", bars, GENOME)

    assert candidate is None
    assert blocked is not None


def test_no_signal_in_neutral_band_is_unaffected():
    bars = _bars_for_z_score(0.2)
    candidate, blocked = strategist.analyse_symbol("VGK", bars, GENOME)
    assert candidate is None and blocked is None


def test_confidence_below_entry_threshold_is_a_learning_observation_not_dropped():
    """
    A direction was assigned (|z-score| > 1.5) but confidence didn't clear
    entry_threshold — a genuine, weaker opportunity. Per the canonical
    decision-observation model this must be reported as BELOW_THRESHOLD,
    not silently dropped as if no thesis had formed at all — the shortability
    check must still never run (never got past the earlier threshold gate).
    """
    bars = _bars_for_z_score(2.2)
    weak_genome = {**GENOME, "entry_threshold": 0.95}
    with patch.object(strategist, "get_shortability") as mock_shortability:
        candidate, observation = strategist.analyse_symbol("EWU", bars, weak_genome)
        mock_shortability.assert_not_called()
    assert candidate is None
    assert observation is not None
    assert observation["outcome_reason"] == "BELOW_THRESHOLD"
    assert observation["direction"] == "short"
    assert observation["entry_price_ref"] is not None
    assert observation["target_pct"] == weak_genome["profit_target_pct"]
    assert observation["stop_pct"] == -abs(weak_genome["stop_loss_pct"])


def test_neutral_band_still_produces_no_observation_at_all():
    """
    z-score inside (-1.5, 1.5): no direction is ever assigned, so per the
    canonical model's own rule (only a GENUINE directional read is logged)
    this must stay (None, None), not become a manufactured BELOW_THRESHOLD row.
    """
    bars = _bars_for_z_score(0.3)
    candidate, observation = strategist.analyse_symbol("EWU", bars, GENOME)
    assert candidate is None and observation is None


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_cache_hit_within_ttl_avoids_api_call(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"EWU": NOT_SHORTABLE}))
    monkeypatch.setattr(strategist, "SHORTABILITY_CACHE_FILE", cache_file)

    with patch.object(strategist.requests, "get") as mock_get:
        result = strategist.get_shortability("EWU")

    mock_get.assert_not_called()
    assert result["shortable"] is False


def test_stale_cache_triggers_refresh_and_picks_up_change(tmp_path, monkeypatch):
    """Requirement: cache must refresh so a future borrowability change is recognised."""
    stale_time = strategist.datetime.now(strategist.timezone.utc) - timedelta(
        hours=strategist.SHORTABILITY_CACHE_TTL_HOURS + 1
    )
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({
        "EWU": {**NOT_SHORTABLE, "checked_at": stale_time.isoformat()}
    }))
    monkeypatch.setattr(strategist, "SHORTABILITY_CACHE_FILE", cache_file)

    fresh_payload = {"shortable": True, "easy_to_borrow": True, "borrow_status": "easy_to_borrow"}
    with patch.object(strategist.requests, "get", return_value=_FakeResponse(fresh_payload)) as mock_get:
        result = strategist.get_shortability("EWU")

    mock_get.assert_called_once()
    assert result["shortable"] is True
    assert json.loads(cache_file.read_text())["EWU"]["shortable"] is True


def test_missing_cache_file_calls_api_once_and_persists_result(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    monkeypatch.setattr(strategist, "SHORTABILITY_CACHE_FILE", cache_file)

    payload = {"shortable": False, "easy_to_borrow": False, "borrow_status": "hard_to_borrow"}
    with patch.object(strategist.requests, "get", return_value=_FakeResponse(payload)) as mock_get:
        result = strategist.get_shortability("EWU")

    mock_get.assert_called_once()
    assert result["shortable"] is False
    assert cache_file.exists()
    assert json.loads(cache_file.read_text())["EWU"]["shortable"] is False


def test_api_failure_with_no_cache_returns_none(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    monkeypatch.setattr(strategist, "SHORTABILITY_CACHE_FILE", cache_file)

    with patch.object(strategist.requests, "get", side_effect=Exception("network down")):
        result = strategist.get_shortability("EWU")

    assert result is None


def test_api_failure_falls_back_to_stale_cache(tmp_path, monkeypatch):
    stale_time = strategist.datetime.now(strategist.timezone.utc) - timedelta(
        hours=strategist.SHORTABILITY_CACHE_TTL_HOURS + 1
    )
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({
        "EWU": {**NOT_SHORTABLE, "checked_at": stale_time.isoformat()}
    }))
    monkeypatch.setattr(strategist, "SHORTABILITY_CACHE_FILE", cache_file)

    with patch.object(strategist.requests, "get", side_effect=Exception("network down")):
        result = strategist.get_shortability("EWU")

    assert result is not None
    assert result["shortable"] is False
