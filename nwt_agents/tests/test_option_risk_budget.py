import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import execution_agent as agent


@pytest.mark.parametrize("budget,price,expected", [
    (698.4, 30.83, 0), (698.4, 5, 1), (1000, 5, 2),
    (0, 5, 0), (1000, 0, 0), (1000, float('nan'), 0),
])
def test_single_contract_respects_budget(budget, price, expected):
    assert agent.compute_qty_from_notional(budget, price) == expected


def test_spread_size_uses_debit_or_credit_max_loss(monkeypatch):
    legs = [{"option_symbol": "L", "side": "buy", "strike_price": 100, "option_type": "call"},
            {"option_symbol": "S", "side": "sell", "strike_price": 105, "option_type": "call"}]
    monkeypatch.setattr(agent, "_get_option_price", lambda s: {'L': 4, 'S': 1}[s])
    assert agent.size_spread_qty(legs, 200) == 0
    assert agent.size_spread_qty(legs, 600) == 2
    monkeypatch.setattr(agent, "_get_option_price", lambda s: {'L': 1, 'S': 2}[s])
    assert agent.size_spread_qty(legs, 300) == 0
    assert agent.size_spread_qty(legs, 800) == 2
    monkeypatch.setattr(agent, "_get_option_price", lambda s: None)
    assert agent.size_spread_qty(legs, 1000) == 0


def test_spread_uses_saved_target(monkeypatch):
    legs = [{"asset": "L", "direction": "long", "entry_price": 4, "stop_pct": -.5, "target_pct": 1},
            {"asset": "S", "direction": "short", "entry_price": 2, "stop_pct": -.5, "target_pct": 1}]
    monkeypatch.setattr(agent, "option_dte", lambda s: 20)
    monkeypatch.setattr(agent, "_get_option_price", lambda s: {'L': 5.5, 'S': 2}[s])
    assert agent._spread_exit_reason(legs, False) is None  # +75%, below saved +100%
    monkeypatch.setattr(agent, "_get_option_price", lambda s: {'L': 6, 'S': 2}[s])
    assert agent._spread_exit_reason(legs, False) == 'target'


def test_saved_stop_and_legacy_defaults():
    assert agent._position_exit_thresholds({"stop_pct": -.25, "target_pct": 1}) == (-.25, 1)
    assert agent._position_exit_thresholds({}) == (-.5, .5)
    with pytest.raises(ValueError):
        agent._position_exit_thresholds({"stop_pct": 0, "target_pct": 1})


def test_single_monitor_waits_for_saved_target(monkeypatch):
    pos = {"position_id": "test", "asset": "DEMO", "entry_price": 10,
           "direction": "long", "stop_pct": -.5, "target_pct": 1}
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [pos]
    monkeypatch.setattr(agent, "option_dte", lambda s: 20)
    monkeypatch.setattr(agent, "_has_pending_close", lambda *args: False)
    emitted = []
    monkeypatch.setattr(agent, "_emit_close_request", lambda c, p, reason: emitted.append(reason))
    monkeypatch.setattr(agent, "_get_option_price", lambda s: 16)
    agent.monitor_options_positions(conn)
    assert emitted == []
    monkeypatch.setattr(agent, "_get_option_price", lambda s: 20)
    agent.monitor_options_positions(conn)
    assert emitted == ["target"]
