"""
Regression tests for the equity max_hold/stop/target close-side defect.

Incident: EU-MR-001 short equity positions (FEZ, VGK) were repeatedly
"closed" by _close_equity_position() (called from run_equity_position_monitor
for max_hold/stop/target exits) without ever determining the position's own
direction. place_close_order() defaults side="sell" — correct for closing a
long, but for a short this places ANOTHER sell, adding to the short instead
of covering it. The ledger was then marked closed unconditionally, without
even checking whether the order actually filled — so NWT believed each
position was closed while Alpaca's real short kept growing (recon later
caught a 396-share FEZ / 196-share VGK live short with zero matching ledger
exposure).

Fix: determine close_side from pos["direction"] exactly like the already-
correct process_close_ticket() does, and gate the ledger mutation on the
order actually reaching status="filled" with a real fill price — matching
process_close_ticket()'s existing fill-status gate.

Every DB/network call except the thing under test is mocked — this suite
verifies control flow (side, gating), not real Postgres/Alpaca behavior.
"""
from unittest.mock import MagicMock, patch

import pytest

import engine


def _pos(**overrides) -> dict:
    pos = {
        "position_id": "e7bfbece-fa3a-42dd-8340-b0c456eb73d0",
        "asset": "FEZ",
        "asset_type": "equity",
        "direction": "short",
        "bot_source": "EU_BOT",
        "entry_price": 71.99,
        "notional_risk": 2231.76,
    }
    pos.update(overrides)
    return pos


def _order(order_id="order-1"):
    return {"id": order_id}


def _filled(fill_price=70.92, status="filled"):
    return {"filled_avg_price": fill_price, "status": status}


# ---------------------------------------------------------------------------
# The core defect: side must match direction
# ---------------------------------------------------------------------------

def test_short_position_close_submits_buy_not_sell():
    """A short position must be closed with side='buy' (buy-to-cover)."""
    conn = MagicMock()
    pos = _pos(direction="short")
    with patch.object(engine, "compute_qty_from_notional", return_value=31), \
         patch.object(engine, "place_close_order", return_value=_order()) as mock_place, \
         patch.object(engine, "poll_order_until_filled", return_value=_filled()), \
         patch.object(engine, "close_position") as mock_close, \
         patch.object(engine, "log_system_event"):
        engine._close_equity_position(conn, pos, current_price=70.92, position_id=pos["position_id"],
                                       symbol="FEZ", notional=2231.76, entry_price=71.99,
                                       exit_reason="max_hold")

    mock_place.assert_called_once_with("FEZ", 31, "equity", side="buy")
    mock_close.assert_called_once()


def test_long_position_close_still_submits_sell():
    """A long position must still close with side='sell' — no regression for the common case."""
    conn = MagicMock()
    pos = _pos(direction="long")
    with patch.object(engine, "compute_qty_from_notional", return_value=31), \
         patch.object(engine, "place_close_order", return_value=_order()) as mock_place, \
         patch.object(engine, "poll_order_until_filled", return_value=_filled()), \
         patch.object(engine, "close_position") as mock_close, \
         patch.object(engine, "log_system_event"):
        engine._close_equity_position(conn, pos, current_price=70.92, position_id=pos["position_id"],
                                       symbol="FEZ", notional=2231.76, entry_price=71.99,
                                       exit_reason="target")

    mock_place.assert_called_once_with("FEZ", 31, "equity", side="sell")
    mock_close.assert_called_once()


def test_missing_direction_defaults_to_sell_not_buy():
    """pos.get('direction', 'long') — an absent direction must fall back to the long/sell case, never buy."""
    conn = MagicMock()
    pos = _pos()
    del pos["direction"]
    with patch.object(engine, "compute_qty_from_notional", return_value=31), \
         patch.object(engine, "place_close_order", return_value=_order()) as mock_place, \
         patch.object(engine, "poll_order_until_filled", return_value=_filled()), \
         patch.object(engine, "close_position"), \
         patch.object(engine, "log_system_event"):
        engine._close_equity_position(conn, pos, current_price=70.92, position_id=pos["position_id"],
                                       symbol="FEZ", notional=2231.76, entry_price=71.99,
                                       exit_reason="max_hold")

    mock_place.assert_called_once_with("FEZ", 31, "equity", side="sell")


# ---------------------------------------------------------------------------
# The second defect: the ledger must not close on an unfilled/rejected order
# ---------------------------------------------------------------------------

def test_unfilled_order_does_not_close_the_ledger():
    conn = MagicMock()
    pos = _pos(direction="short")
    with patch.object(engine, "compute_qty_from_notional", return_value=31), \
         patch.object(engine, "place_close_order", return_value=_order()), \
         patch.object(engine, "poll_order_until_filled",
                       return_value=_filled(fill_price=None, status="rejected")), \
         patch.object(engine, "close_position") as mock_close, \
         patch.object(engine, "log_system_event") as mock_log:
        engine._close_equity_position(conn, pos, current_price=70.92, position_id=pos["position_id"],
                                       symbol="FEZ", notional=2231.76, entry_price=71.99,
                                       exit_reason="max_hold")

    mock_close.assert_not_called(), "the ledger must stay open when the covering order didn't fill"
    assert any(call.args[1] == "ERROR" for call in mock_log.call_args_list)


def test_zero_fill_price_does_not_close_the_ledger():
    conn = MagicMock()
    pos = _pos(direction="short")
    with patch.object(engine, "compute_qty_from_notional", return_value=31), \
         patch.object(engine, "place_close_order", return_value=_order()), \
         patch.object(engine, "poll_order_until_filled",
                       return_value=_filled(fill_price=0, status="filled")), \
         patch.object(engine, "close_position") as mock_close, \
         patch.object(engine, "log_system_event"):
        engine._close_equity_position(conn, pos, current_price=70.92, position_id=pos["position_id"],
                                       symbol="FEZ", notional=2231.76, entry_price=71.99,
                                       exit_reason="max_hold")

    mock_close.assert_not_called()


def test_filled_order_closes_the_ledger_with_correct_slippage():
    conn = MagicMock()
    pos = _pos(direction="short")
    with patch.object(engine, "compute_qty_from_notional", return_value=31), \
         patch.object(engine, "place_close_order", return_value=_order()), \
         patch.object(engine, "poll_order_until_filled", return_value=_filled(fill_price=70.92)), \
         patch.object(engine, "close_position") as mock_close, \
         patch.object(engine, "log_system_event"):
        engine._close_equity_position(conn, pos, current_price=71.00, position_id=pos["position_id"],
                                       symbol="FEZ", notional=2231.76, entry_price=71.99,
                                       exit_reason="max_hold")

    mock_close.assert_called_once()
    call_args = mock_close.call_args[0]
    # (conn, position_id, fill_price, slippage, exit_reason)
    assert call_args[1] == pos["position_id"]
    assert call_args[2] == 70.92
    assert call_args[4] == "max_hold"
