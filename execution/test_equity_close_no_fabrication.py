"""
New, standalone test for the equity-close fabricated-price fix
(_close_equity_position in execution/engine.py). No existing test suite
covered this function. Mocks Alpaca HTTP, find_or_place_order, and
close_position; does not touch a real database or a real Alpaca account.

Root cause under test (the RIO incident): when an equity stop/target/
hard-close order never actually fills, the old code fell back to the
current quoted price and closed the ledger row anyway — fabricating a
close on a position that was never really sold at the broker.

Run: python3 execution/test_equity_close_no_fabrication.py
"""
import sys
from unittest import mock

sys.path.insert(0, "execution")
import engine  # noqa: E402


def run_test_1_unfilled_order_not_closed():
    """Order never fills (status != 'filled', no fill price) -> ledger NOT closed, no fabricated price."""
    pos = {"position_id": "pos-1", "direction": "long", "qty": 30}
    calls = {"close_position_called": False, "log": None}

    with mock.patch.object(engine, "client_order_id_for", return_value="nwt-eqmon-pos-1"), \
         mock.patch.object(engine, "find_or_place_order", return_value={"id": "order-1"}), \
         mock.patch.object(engine, "poll_order_until_filled",
                            return_value={"id": "order-1", "status": "canceled", "filled_avg_price": None}), \
         mock.patch.object(engine, "close_position",
                            side_effect=lambda *a, **k: calls.__setitem__("close_position_called", True)), \
         mock.patch.object(engine, "log_system_event",
                            side_effect=lambda *a, **k: calls.__setitem__("log", a)), \
         mock.patch.object(engine, "verify_post_fill_position") as verify_mock:
        engine._close_equity_position(None, pos, 100.24, "pos-1", "RIO", 3007.2, 100.24, "hard_close")

    assert calls["close_position_called"] is False, "close_position must NOT be called on an unfilled order"
    assert verify_mock.called is False, "post-fill verification must not run when nothing filled"
    assert calls["log"] is not None and calls["log"][1] == "WARNING"
    print("TEST 1 (unfilled equity close order -> ledger left open, no fabrication): PASS")


def run_test_2_real_fill_still_closes_normally():
    """Sanity: a genuine fill must still close the ledger exactly as before."""
    pos = {"position_id": "pos-2", "direction": "long", "qty": 30}
    calls = {"close_position": None}

    with mock.patch.object(engine, "client_order_id_for", return_value="nwt-eqmon-pos-2"), \
         mock.patch.object(engine, "find_or_place_order", return_value={"id": "order-2"}), \
         mock.patch.object(engine, "poll_order_until_filled",
                            return_value={"id": "order-2", "status": "filled", "filled_avg_price": "101.50"}), \
         mock.patch.object(engine, "close_position",
                            side_effect=lambda *a, **k: calls.__setitem__("close_position", a)), \
         mock.patch.object(engine, "log_system_event"), \
         mock.patch.object(engine, "verify_post_fill_position") as verify_mock:
        engine._close_equity_position(None, pos, 101.50, "pos-2", "RIO", 3045.0, 100.24, "target")

    assert calls["close_position"] is not None, "close_position must be called on a real fill"
    assert calls["close_position"][2] == 101.50, f"expected real fill price 101.50, got {calls['close_position'][2]}"
    assert verify_mock.called is True, "post-fill verification must still run on a real fill"
    print("TEST 2 (genuine fill still closes normally, unchanged behaviour): PASS")


if __name__ == "__main__":
    run_test_1_unfilled_order_not_closed()
    run_test_2_real_fill_still_closes_normally()
    print("\nALL TESTS PASSED")
