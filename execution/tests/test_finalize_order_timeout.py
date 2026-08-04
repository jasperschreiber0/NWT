"""
execution/tests/test_finalize_order_timeout.py
Regression test for the EU_EXECUTOR market-open timeout fix: POLL_MAX raised
from 10 to 20 (30s -> 60s). Verifies finalize_order() actually polls the new
number of times before giving up, and still correctly cancels + reads back
a genuinely-stuck order — the fix is a constant change only, not a logic
change, and this confirms the control flow around it is unaffected.

No live Postgres or Alpaca needed — pure mock of the HTTP layer.
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


def _resp(status_code, json_body):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body
    r.ok = status_code < 400
    r.text = str(json_body)
    return r


def test_poll_max_is_20():
    assert engine.POLL_MAX == 20, "EU market-open fix should double the poll window to 60s (20 x 3s)"


@patch("engine.time.sleep")  # never actually wait during the test
@patch("engine.requests.delete")
@patch("engine.requests.get")
def test_finalize_order_polls_full_new_window_then_cancels(mock_get, mock_delete, mock_sleep):
    """
    An order stuck at status='new' for the entire poll window (simulating
    the observed opening-auction latency) must be polled POLL_MAX times —
    not the old 10 — before finalize_order gives up and cancels it.
    """
    stuck_order = _resp(200, {"id": "order-123", "status": "new", "filled_qty": "0"})
    mock_get.return_value = stuck_order
    mock_delete.return_value = _resp(204, {})

    result = engine.finalize_order("order-123")

    # poll_order_until_filled makes POLL_MAX GET calls in its loop, plus one
    # more fallback GET after the loop exits; finalize_order then makes a
    # final GET to read back state after cancelling = POLL_MAX + 2 total.
    assert mock_get.call_count == engine.POLL_MAX + 2
    mock_delete.assert_called_once()
    assert result["status"] == "new"  # last GET response, since read-back reused the same mock


@patch("engine.time.sleep")
@patch("engine.requests.get")
def test_finalize_order_returns_immediately_on_fill(mock_get, mock_sleep):
    """An order that fills on the first check must not wait for the full window at all."""
    mock_get.return_value = _resp(200, {"id": "order-456", "status": "filled", "filled_qty": "10"})

    result = engine.finalize_order("order-456")

    assert mock_get.call_count == 1
    assert result["status"] == "filled"
    mock_sleep.assert_not_called()
