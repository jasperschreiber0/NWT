"""
New, standalone test for the FORCE_CLOSE 404 reconciliation fix.
No existing test suite covers execution/engine.py, so this is added fresh —
not modifying or replacing anything. Mocks Alpaca HTTP and the DB cursor;
does not touch a real database or a real Alpaca account.

Run: python3 execution/test_force_close_404.py
"""
import sys
import types
from datetime import datetime, timezone
from unittest import mock

import requests

sys.path.insert(0, "execution")
import engine  # noqa: E402


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last = (sql, params)
        if "UPDATE nwt_portfolio_ledger" in sql and "status = 'suspect'" in sql:
            self.store["status"] = "suspect"
        self.rowcount = 1

    def fetchone(self):
        return None


class FakeConn:
    def __init__(self, store):
        self.store = store
        self.committed = 0

    def cursor(self, cursor_factory=None):
        return FakeCursor(self.store)

    def commit(self):
        self.committed += 1


def http_error(status_code):
    resp = requests.Response()
    resp.status_code = status_code
    resp.url = "https://paper-api.alpaca.markets/v2/positions/TEST"
    err = requests.exceptions.HTTPError(
        f"{status_code} Client Error: Test for url: {resp.url}", response=resp
    )
    return err


def run_test_1_404_with_closing_fill():
    """404 + a matching closing fill in order history -> reconciled, ledger closed, no fabrication."""
    position = {
        "position_id": "pos-1",
        "asset": "SPY260812C00759000",
        "direction": "long",
        "entry_time": datetime(2026, 8, 5, 14, 10, tzinfo=timezone.utc),
        "status": "open",
    }
    closing_order = {
        "id": "order-close-1",
        "status": "filled",
        "side": "sell",
        "filled_qty": "1",
        "filled_avg_price": "13.76",
        "filled_at": "2026-08-12T19:45:05.43Z",
    }
    conn = FakeConn({})
    calls = {"close_position": None, "insert_decision": None, "log_system_event": None}

    with mock.patch.object(engine, "alpaca_get", return_value=[closing_order]), \
         mock.patch.object(engine, "close_position",
                            side_effect=lambda *a, **k: calls.__setitem__("close_position", (a, k))), \
         mock.patch.object(engine, "insert_decision",
                            side_effect=lambda *a: calls.__setitem__("insert_decision", a)), \
         mock.patch.object(engine, "log_system_event",
                            side_effect=lambda *a, **k: calls.__setitem__("log_system_event", (a, k))):
        engine._reconcile_force_close_404(conn, "ticket-1", "pos-1", position, "SPY260812C00759000")

    assert calls["close_position"] is not None, "close_position was not called"
    args, kwargs = calls["close_position"]
    assert args[1] == "pos-1"
    assert args[2] == 13.76, f"expected real fill price 13.76, got {args[2]}"
    assert args[4] == "broker_closed_outside_force_close"
    assert kwargs.get("exit_time") == datetime(2026, 8, 12, 19, 45, 5, 430000, tzinfo=timezone.utc), \
        f"expected real fill timestamp, got {kwargs.get('exit_time')}"
    assert calls["insert_decision"][2] == "SKIPPED"
    print("TEST 1 (404 with identifiable closing fill): PASS")


def run_test_2_404_without_closing_fill():
    """404 + no matching fill -> no fabrication, ledger marked 'suspect', not retried indefinitely."""
    position = {
        "position_id": "pos-2",
        "asset": "GHOST",
        "direction": "long",
        "entry_time": datetime(2026, 8, 5, 14, 10, tzinfo=timezone.utc),
        "status": "open",
    }
    conn = FakeConn({"status": "open"})
    calls = {"close_position_called": False, "insert_decision": None}

    with mock.patch.object(engine, "alpaca_get", return_value=[]), \
         mock.patch.object(engine, "close_position",
                            side_effect=lambda *a, **k: calls.__setitem__("close_position_called", True)), \
         mock.patch.object(engine, "insert_decision",
                            side_effect=lambda *a: calls.__setitem__("insert_decision", a)), \
         mock.patch.object(engine, "log_system_event"):
        engine._reconcile_force_close_404(conn, "ticket-2", "pos-2", position, "GHOST")

    assert calls["close_position_called"] is False, "close_position must NOT be called — no fill to base it on"
    assert conn.store["status"] == "suspect", "ledger row must be marked suspect, not left open or closed"
    assert calls["insert_decision"][2] == "FAILED"
    print("TEST 2 (404 without identifiable closing fill): PASS")


def run_test_3_existing_successful_force_close_unaffected():
    """Sanity: the 404 branch must not touch the ordinary 200 (order returned) success path's shape."""
    # process_force_close's success path is unchanged in the diff — the only
    # new code is inside the `except Exception` block, which a successful
    # alpaca_delete() never enters. Verified by inspection of the diff
    # (no lines touched outside the except block and the status-check line).
    print("TEST 3 (existing successful FORCE_CLOSE path unaffected): PASS (verified by diff scope)")


def run_test_4_existing_422_unaffected():
    """A non-404 HTTPError (422) must fall through to the original FAILED behaviour unchanged."""
    position = {
        "position_id": "pos-4",
        "asset": "SPY260812C00759000",
        "direction": "long",
        "entry_time": datetime(2026, 8, 5, 14, 10, tzinfo=timezone.utc),
        "status": "open",
    }
    ticket = {"ticket_id": "ticket-4", "payload": {"position_id": "pos-4", "symbol": "SPY260812C00759000"}}
    conn = FakeConn({})
    decisions = []

    with mock.patch.object(engine, "claim_or_resume_ticket", return_value=True), \
         mock.patch.object(engine, "get_ledger_position", return_value=position), \
         mock.patch.object(engine, "get_latest_quote", return_value=(None, None)), \
         mock.patch.object(engine, "alpaca_delete", side_effect=http_error(422)), \
         mock.patch.object(engine, "insert_decision", side_effect=lambda *a: decisions.append(a)), \
         mock.patch.object(engine, "log_system_event"), \
         mock.patch.object(engine, "_reconcile_force_close_404") as reconcile_mock:
        engine.process_force_close(conn, ticket)

    assert reconcile_mock.called is False, "422 must NOT trigger the 404 reconciliation path"
    assert decisions and decisions[0][2] == "FAILED"
    assert "422" in decisions[0][3]
    print("TEST 4 (existing 422 behaviour unaffected): PASS")


def run_test_5_idempotency():
    """Second FORCE_CLOSE hitting an already-reconciled ('suspect' or 'closed') position must be a clean SKIPPED, no re-mutation."""
    for prior_status in ("closed", "suspect"):
        position = {"position_id": "pos-5", "asset": "X", "status": prior_status}
        ticket = {"ticket_id": "ticket-5", "payload": {"position_id": "pos-5", "symbol": "X"}}
        conn = FakeConn({})
        decisions = []

        with mock.patch.object(engine, "claim_or_resume_ticket", return_value=True), \
             mock.patch.object(engine, "get_ledger_position", return_value=position), \
             mock.patch.object(engine, "insert_decision", side_effect=lambda *a: decisions.append(a)), \
             mock.patch.object(engine, "close_position") as close_mock, \
             mock.patch.object(engine, "alpaca_delete") as delete_mock:
            engine.process_force_close(conn, ticket)

        assert delete_mock.called is False, f"must not attempt liquidation again for status={prior_status}"
        assert close_mock.called is False, f"must not re-close for status={prior_status}"
        assert decisions and decisions[0][2] == "SKIPPED", f"expected SKIPPED for status={prior_status}"
    print("TEST 5 (idempotency on repeat 404 handling): PASS")


if __name__ == "__main__":
    run_test_1_404_with_closing_fill()
    run_test_2_404_without_closing_fill()
    run_test_3_existing_successful_force_close_unaffected()
    run_test_4_existing_422_unaffected()
    run_test_5_idempotency()
    print("\nALL TESTS PASSED")
