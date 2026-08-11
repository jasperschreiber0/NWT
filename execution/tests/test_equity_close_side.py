"""
execution/tests/test_equity_close_side.py
Regression test for the equity-short-close side fix.

_close_equity_position() previously always called place_close_order() with
no `side`, which defaults to "sell" — correct for a long position, wrong
for a short one (selling ADDS to a short instead of covering it). This is
what turned a real -10 BHP short into -20 at the broker on 2026-07-28 while
the ledger row was marked 'closed'. Fixed by deriving close_side from the
position's own ledger direction, same as process_close_ticket already does
for CLOSE_REQUEST tickets.

No live Postgres or Alpaca needed. Mocks at the two real network
boundaries _close_equity_position crosses (get_broker_position,
place_close_order/finalize_order) and asserts on the actual `side` argument
place_close_order was called with — the exact contract the fix touches.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("NWT_DB_DSN", "postgresql://unused/unused")

import engine  # noqa: E402


def _run_close(direction, mock_place_close_order, mock_finalize_order, mock_get_broker_position):
    pos = {"position_id": "pos-1", "direction": direction, "qty": 10}

    mock_get_broker_position.return_value = {"qty": "10"}  # broker holds 10 shares, either direction
    mock_place_close_order.return_value = {"id": "order-1"}
    mock_finalize_order.return_value = {
        "id": "order-1", "status": "filled", "filled_avg_price": "100.0", "filled_qty": "10",
    }

    # _close_equity_position now routes the order call through
    # find_or_place_order(), which checks Alpaca for an existing order under
    # this client_order_id first (the crash-recovery/race-safety path added
    # by the execution-order idempotency fix). None here means "no existing
    # order found", so find_or_place_order proceeds to call place_close_order
    # exactly as before — this test is about the side= argument that call
    # receives, not about idempotency, which has its own dedicated test file.
    with patch("engine.alpaca_get_by_client_order_id", return_value=None), \
         patch("engine.reduce_position_qty", return_value=0.0), \
         patch("engine.log_reconciliation_event"), \
         patch("engine.log_system_event"):
        engine._close_equity_position(MagicMock(), pos, current_price=100.0, position_id="pos-1",
                                      symbol="TESTSYM", notional=1000.0, entry_price=100.0,
                                      exit_reason="target")


@patch("engine.get_broker_position")
@patch("engine.finalize_order")
@patch("engine.place_close_order")
def test_long_position_close_submits_sell(mock_place, mock_finalize, mock_broker_pos):
    _run_close("long", mock_place, mock_finalize, mock_broker_pos)
    called_side = mock_place.call_args.kwargs["side"]
    assert called_side == "sell", f"Closing a long position must SELL, got side={called_side!r}"


@patch("engine.get_broker_position")
@patch("engine.finalize_order")
@patch("engine.place_close_order")
def test_short_position_close_submits_buy(mock_place, mock_finalize, mock_broker_pos):
    _run_close("short", mock_place, mock_finalize, mock_broker_pos)
    called_side = mock_place.call_args.kwargs["side"]
    assert called_side == "buy", f"Closing a short position must BUY to cover, got side={called_side!r}"


@patch("engine.get_broker_position")
@patch("engine.finalize_order")
@patch("engine.place_close_order")
def test_missing_direction_defaults_to_long_close_behavior(mock_place, mock_finalize, mock_broker_pos):
    """
    Existing behavior for the common case (long, the only direction that
    existed before shorts started trading) must be unchanged: a row with no
    direction set falls back to sell, same as before this fix.
    """
    _run_close(None, mock_place, mock_finalize, mock_broker_pos)
    called_side = mock_place.call_args.kwargs["side"]
    assert called_side == "sell", f"Missing/legacy direction must still default to sell, got side={called_side!r}"
