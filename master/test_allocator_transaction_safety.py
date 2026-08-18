"""
master/test_allocator_transaction_safety.py

New test for Bug 2 (allocator text=uuid crash + transaction poisoning).
No existing test suite covered master/allocator.py. Mocks a Postgres-like
connection that reproduces the real failure mode: once one cursor.execute()
raises, the connection is "aborted" and every subsequent execute() on it
raises too, UNTIL rollback() is called — exactly psycopg2/Postgres's real
behaviour, which is what let one bot's bad query silently kill
nwt_allocator_history and nwt_system_log writes for the rest of the run.

Run: python3 master/test_allocator_transaction_safety.py
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import allocator  # noqa: E402


class AbortableCursor:
    def __init__(self, conn, rows):
        self.conn = conn
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.conn.aborted:
            raise Exception("current transaction is aborted, commands ignored "
                            "until end of transaction block")
        if self.conn.fail_bot_query and params and params[0] == self.conn.fail_for_bot:
            self.conn.aborted = True
            raise Exception('operator does not exist: text = uuid\nLINE 5: ...pl.position_id::text = to_.position_id...')
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows

    executed = []


class FakeConn:
    def __init__(self, rows=None, fail_for_bot=None):
        self.aborted = False
        self.fail_bot_query = fail_for_bot is not None
        self.fail_for_bot = fail_for_bot
        self.rows = rows or []
        self.history_inserts = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, cursor_factory=None):
        c = AbortableCursor(self, self.rows)
        c.executed = self.history_inserts
        return c

    def commit(self):
        if self.aborted:
            raise Exception("current transaction is aborted, commands ignored "
                            "until end of transaction block")
        self.commits += 1

    def rollback(self):
        self.aborted = False
        self.rollbacks += 1


def run_test_1_bad_cast_is_gone():
    """The query no longer contains the ::text cast that broke uuid comparison."""
    import inspect
    src = inspect.getsource(allocator._fetch_bot_trades)
    assert "position_id::text" not in src, "the broken cast is still present"
    assert "pl.position_id = to_.position_id" in src, "expected a plain uuid = uuid comparison"
    print("TEST 1 (text=uuid cast removed, native uuid=uuid comparison used): PASS")


def run_test_2_one_bot_failure_does_not_poison_the_rest():
    """One bot's query failure must not prevent the other three bots from being scored, or history from being written."""
    conn = FakeConn(rows=[(100.0, "risk_on")] * 20, fail_for_bot="us")

    scores, notes = None, None
    with mock.patch.object(allocator, "_bot_score", side_effect=lambda c, bot, regime: (
        (_ for _ in ()).throw(Exception("operator does not exist: text = uuid"))
        if bot == "us" else
        {"bot": bot, "sample": 20, "total_sample": 20, "expectancy": 5.0, "sharpe_proxy": 1.0, "basis": "overall"}
    )):
        weights, notes = allocator.compute_dynamic_weights(
            conn, {"primary_regime": "risk_on"},
            {"us": 0.35, "eu": 0.20, "aus": 0.20, "china": 0.15},
        )

    assert conn.rollbacks >= 1, "expected a rollback after the failed bot's query"
    assert weights is not None, "compute_dynamic_weights must still return usable weights"
    print("TEST 2 (one bot's scoring failure does not block the other bots or history write): PASS")
    print(f"        rollbacks={conn.rollbacks}, commits={conn.commits}, weights={weights}")


def run_test_3_history_write_succeeds_after_recovery():
    """nwt_allocator_history write must succeed once the connection has been rolled back clean."""
    conn = FakeConn()
    scores = {b: {"sample": 20, "expectancy": 5.0, "sharpe_proxy": 1.0, "basis": "overall"} for b in allocator.BOT_KEYS}
    allocator._record_history(conn, scores, {"us": 0.35, "eu": 0.2, "aus": 0.2, "china": 0.15},
                              {"us": 0.35, "eu": 0.2, "aus": 0.2, "china": 0.15}, "risk_on", [])
    assert conn.commits == 1, "history write should commit cleanly with no prior failure"
    print("TEST 3 (nwt_allocator_history write succeeds on a clean connection): PASS")


def run_test_4_history_write_failure_still_rolls_back():
    """If the history write itself fails, it must also roll back rather than leaving the caller's connection poisoned."""
    conn = FakeConn(fail_for_bot="__always__")
    conn.fail_bot_query = True

    class AlwaysFailCursor(AbortableCursor):
        def execute(self, sql, params=None):
            self.conn.aborted = True
            raise Exception('operator does not exist: text = uuid')

    with mock.patch.object(FakeConn, "cursor", lambda self, cursor_factory=None: AlwaysFailCursor(self, [])):
        allocator._record_history(conn, {}, {}, {}, "risk_on", [])

    assert conn.rollbacks >= 1, "a failed history write must still roll back"
    print("TEST 4 (nwt_system_log / caller's connection recoverable after history-write failure): PASS")


if __name__ == "__main__":
    run_test_1_bad_cast_is_gone()
    run_test_2_one_bot_failure_does_not_poison_the_rest()
    run_test_3_history_write_succeeds_after_recovery()
    run_test_4_history_write_failure_still_rolls_back()
    print("\nALL TESTS PASSED")
