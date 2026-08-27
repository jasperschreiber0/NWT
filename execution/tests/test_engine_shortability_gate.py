"""
Regression tests for the execution-engine defense-in-depth shortability
gate (see ukeu/tests/test_shortability_gate.py for the primary,
strategist-side gate this backstops).

Incident: EU_BOT repeatedly submitted short-EWU TRADE_REQUEST tickets;
Alpaca reports EWU shortable=False (hard-to-borrow) and rejects every one
with a 422. Even with the strategist-side gate in place, a stale cache
entry or a bug elsewhere could still let a short-on-non-shortable ticket
reach process_ticket() — this backstop must catch that case, classify it
as STRUCTURALLY_IMPOSSIBLE (never generic FAILED), and guarantee no order
ever reaches Alpaca.

Every DB/network call except the thing under test is mocked — this suite
verifies control flow and classification, not real Postgres/Alpaca
behavior.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

import engine


NOT_SHORTABLE = {
    "shortable": False, "easy_to_borrow": False, "borrow_status": "hard_to_borrow",
    "checked_at": "2026-08-27T00:00:00+00:00",
}
SHORTABLE = {
    "shortable": True, "easy_to_borrow": True, "borrow_status": "easy_to_borrow",
    "checked_at": "2026-08-27T00:00:00+00:00",
}


def _ticket(**payload_overrides) -> dict:
    payload = {
        "approved": True,
        "bot_source": "EU_BOT",
        "symbol": "EWU",
        "direction": "short",
        "strategy_id": "EU-MR-001",
        "sized_notional": 1000.0,
        "asset_type": "equity",
        "time_in_force": "day",
    }
    payload.update(payload_overrides)
    return {"ticket_id": "11111111-1111-1111-1111-111111111111", "payload": payload}


@pytest.fixture(autouse=True)
def _bypass_upstream_gates():
    """
    check_directional_cap and synchronous_risk_veto are unrelated controls
    with their own DB reads — stub them to "pass" so these tests isolate
    the shortability gate specifically.
    """
    with patch.object(engine, "check_directional_cap", return_value=(False, 0.0, 1_000_000.0)), \
         patch.object(engine, "synchronous_risk_veto", return_value=(False, "")):
        yield


def test_short_on_non_shortable_asset_is_rejected_before_any_order_call():
    conn = MagicMock()
    with patch.object(engine, "get_shortability", return_value=NOT_SHORTABLE) as mock_shortability, \
         patch.object(engine, "insert_decision") as mock_decision, \
         patch.object(engine, "log_system_event") as mock_log_event, \
         patch.object(engine, "place_equity_order") as mock_place, \
         patch.object(engine, "alpaca_post") as mock_alpaca_post:
        engine.process_ticket(conn, _ticket(), directives={})

    mock_shortability.assert_called_once_with("EWU")
    mock_place.assert_not_called()
    mock_alpaca_post.assert_not_called(), "no broker order may ever be submitted"

    assert mock_decision.call_count == 1
    _, ticket_id, decision, reasoning = mock_decision.call_args[0]
    assert decision == "STRUCTURALLY_IMPOSSIBLE"
    assert decision != "FAILED", "must be distinguishable from a generic execution failure"
    assert "EWU" in reasoning and "shortable" in reasoning.lower()

    assert mock_log_event.call_count == 1
    log_args = mock_log_event.call_args[0]
    assert log_args[2] == "execution_engine"


def test_short_on_shortable_asset_reaches_order_placement():
    """The gate must not block a legitimately shortable asset."""
    conn = MagicMock()
    marker = RuntimeError("reached-order-placement")
    with patch.object(engine, "get_shortability", return_value=SHORTABLE) as mock_shortability, \
         patch.object(engine, "insert_decision") as mock_decision, \
         patch.object(engine, "log_system_event"), \
         patch.object(engine, "get_latest_quote", return_value=(10.0, 10.05)), \
         patch.object(engine, "place_equity_order", side_effect=marker) as mock_place:
        engine.process_ticket(conn, _ticket(symbol="VGK"), directives={})

    mock_shortability.assert_called_once_with("VGK")
    mock_place.assert_called_once()
    # Falls through to the existing generic error path (unrelated to this gate)
    decision = mock_decision.call_args[0][2]
    assert decision == "FAILED"
    assert "reached-order-placement" in mock_decision.call_args[0][3]


def test_long_direction_never_triggers_shortability_check():
    """Requirement: do not block long trades merely because the asset is non-shortable."""
    conn = MagicMock()
    marker = RuntimeError("reached-order-placement")
    with patch.object(engine, "get_shortability") as mock_shortability, \
         patch.object(engine, "insert_decision"), \
         patch.object(engine, "log_system_event"), \
         patch.object(engine, "get_latest_quote", return_value=(10.0, 10.05)), \
         patch.object(engine, "place_equity_order", side_effect=marker) as mock_place:
        engine.process_ticket(conn, _ticket(symbol="EWU", direction="long"), directives={})

    mock_shortability.assert_not_called()
    mock_place.assert_called_once()


def test_options_short_direction_is_not_gated():
    """
    direction on an options ticket is the market thesis, not the order side
    (single-leg options are always buy-to-open — see CLAUDE.md gotcha on
    execution/engine.py::place_options_order). The equity shortability gate
    must not apply here.
    """
    conn = MagicMock()
    marker = RuntimeError("reached-order-placement")
    with patch.object(engine, "get_shortability") as mock_shortability, \
         patch.object(engine, "insert_decision"), \
         patch.object(engine, "log_system_event"), \
         patch.object(engine, "get_latest_quote", return_value=(1.0, 1.1)), \
         patch.object(engine, "place_options_order", side_effect=marker) as mock_place:
        engine.process_ticket(
            conn,
            _ticket(symbol="SPY", direction="short", asset_type="option",
                    option_symbol="SPY260828P00500000"),
            directives={},
        )

    mock_shortability.assert_not_called()
    mock_place.assert_called_once()


def test_unknown_shortability_fails_safe_and_blocks():
    """A failed lookup (None) must never be treated as 'shortable'."""
    conn = MagicMock()
    with patch.object(engine, "get_shortability", return_value=None), \
         patch.object(engine, "insert_decision") as mock_decision, \
         patch.object(engine, "log_system_event"), \
         patch.object(engine, "place_equity_order") as mock_place:
        engine.process_ticket(conn, _ticket(), directives={})

    mock_place.assert_not_called()
    assert mock_decision.call_args[0][2] == "STRUCTURALLY_IMPOSSIBLE"


# ---------------------------------------------------------------------------
# Cache behavior (own copy of the same cache the strategist-side gate uses —
# see ukeu/tests/test_shortability_gate.py for the equivalent coverage there)
# ---------------------------------------------------------------------------

def test_cache_hit_within_ttl_avoids_alpaca_call(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"EWU": NOT_SHORTABLE}))
    monkeypatch.setattr(engine, "SHORTABILITY_CACHE_FILE", cache_file)

    with patch.object(engine, "alpaca_get") as mock_alpaca_get:
        result = engine.get_shortability("EWU")

    mock_alpaca_get.assert_not_called()
    assert result["shortable"] is False


def test_stale_cache_triggers_refresh(tmp_path, monkeypatch):
    from datetime import timedelta
    stale_time = engine.datetime.now(engine.timezone.utc) - timedelta(
        hours=engine.SHORTABILITY_CACHE_TTL_HOURS + 1
    )
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"EWU": {**NOT_SHORTABLE, "checked_at": stale_time.isoformat()}}))
    monkeypatch.setattr(engine, "SHORTABILITY_CACHE_FILE", cache_file)

    fresh = {"shortable": True, "easy_to_borrow": True, "borrow_status": "easy_to_borrow"}
    with patch.object(engine, "alpaca_get", return_value=fresh) as mock_alpaca_get:
        result = engine.get_shortability("EWU")

    mock_alpaca_get.assert_called_once_with("/assets/EWU")
    assert result["shortable"] is True
    assert json.loads(cache_file.read_text())["EWU"]["shortable"] is True


def test_missing_cache_calls_alpaca_once_and_persists(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    monkeypatch.setattr(engine, "SHORTABILITY_CACHE_FILE", cache_file)

    payload = {"shortable": False, "easy_to_borrow": False, "borrow_status": "hard_to_borrow"}
    with patch.object(engine, "alpaca_get", return_value=payload) as mock_alpaca_get:
        result = engine.get_shortability("EWU")

    mock_alpaca_get.assert_called_once_with("/assets/EWU")
    assert result["shortable"] is False
    assert json.loads(cache_file.read_text())["EWU"]["shortable"] is False
