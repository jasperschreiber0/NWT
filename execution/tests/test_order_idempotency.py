"""
execution/tests/test_order_idempotency.py
Regression tests for the minimal execution-order idempotency fix.

Proven live 2026-08-06/07: EU_BOT's FEZ ticket produced two real, separately
filled Alpaca orders from what was meant to be one signal — the second order
had zero ticket_id and zero decision anywhere in the system. Root cause:
insert_position() happens before insert_decision(), and fetch_pending_tickets
only checks for an existing decision, so a crash/lost-response/concurrent
race between those two calls leaves a ticket looking untouched and eligible
for reprocessing.

Fix, two mechanisms:
  - claim_ticket(): an ATOMIC database claim via a partial unique index on
    nwt_ticket_decisions(ticket_id, decided_by) (db/migrate_2026_08_
    execution_idempotency.sql) — this is what actually closes the "two
    concurrent workers" race; a plain check-then-insert is NOT safe under
    concurrency, and this suite proves the real thing, not a mock of it.
  - client_order_id_for() / find_or_place_order(): protects the DIFFERENT
    "response lost after Alpaca accepted the order" scenario, using Alpaca's
    own server-side client_order_id uniqueness as the arbiter.

Run against a throwaway Postgres with the FULL migration chain applied
(schema.sql, then every migrate_*.sql in filename order, ending with
migrate_2026_08_execution_idempotency.sql):
    NWT_TEST_DB_DSN=postgresql://nwt_test:nwt_test_pw@localhost/nwt_idempotency_test \
        pytest execution/tests/test_order_idempotency.py -v
"""
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test_secret")
os.environ.setdefault("NWT_DB_DSN", "postgresql://unused/unused")

import engine  # noqa: E402

TEST_DSN = os.environ.get("NWT_TEST_DB_DSN")


@pytest.fixture
def conn():
    if not TEST_DSN:
        pytest.skip("NWT_TEST_DB_DSN not set — run against a throwaway Postgres with the full migration chain applied")
    c = psycopg2.connect(TEST_DSN)
    yield c
    with c.cursor() as cur:
        cur.execute("TRUNCATE nwt_ticket_decisions, nwt_tickets CASCADE")
    c.commit()
    c.close()


def _insert_ticket(conn) -> str:
    ticket_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_tickets (ticket_id, from_agent, to_agent, type, payload)
            VALUES (%s, 'EU_EXECUTOR', 'EXECUTION_ENGINE', 'TRADE_REQUEST', '{}'::jsonb)
            """,
            (ticket_id,),
        )
    conn.commit()
    return ticket_id


def _fresh_conn():
    return psycopg2.connect(TEST_DSN)


# ---------------------------------------------------------------------------
# Case 1 — normal success: one ticket, one order
# ---------------------------------------------------------------------------

def test_case1_normal_success_produces_exactly_one_order(conn):
    ticket_id = _insert_ticket(conn)
    assert engine.claim_ticket(conn, ticket_id) is True

    client_order_id = engine.client_order_id_for(ticket_id, "entry")
    place_calls = []

    def place_fn():
        place_calls.append(1)
        return {"id": "order-1", "status": "accepted"}

    with patch("engine.alpaca_get_by_client_order_id", return_value=None):
        order = engine.find_or_place_order(client_order_id, place_fn)

    assert order["id"] == "order-1"
    assert len(place_calls) == 1

    engine.insert_decision(conn, ticket_id, "EXECUTED", "Filled qty=1 at 100.0")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT decision FROM nwt_ticket_decisions WHERE ticket_id=%s AND decided_by='EXECUTION_ENGINE'",
            (ticket_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1, "Exactly one decision row — the CLAIMED row was finalized in place, not duplicated"
    assert rows[0]["decision"] == "EXECUTED"


# ---------------------------------------------------------------------------
# Case 2 — response lost after Alpaca accepts: retry must reconcile, not resubmit
# ---------------------------------------------------------------------------

def test_case2_lost_response_reconciles_instead_of_resubmitting():
    client_order_id = "nwt-entry-lost-response-ticket"
    place_calls = []

    def place_fn_that_loses_the_response():
        place_calls.append(1)
        raise ConnectionError("response never arrived, even though Alpaca accepted the order")

    # Attempt 1: Alpaca has never seen this client_order_id yet -> place_fn runs,
    # "accepts" the order, but the response is lost (simulated exception).
    with patch("engine.alpaca_get_by_client_order_id", return_value=None):
        with pytest.raises(ConnectionError):
            engine.find_or_place_order(client_order_id, place_fn_that_loses_the_response)
    assert len(place_calls) == 1

    # Attempt 2 (retry): Alpaca DOES now have an order under this client_order_id
    # (it was accepted on attempt 1) -> must reconcile, never call place_fn again.
    real_order = {"id": "order-2", "status": "filled", "filled_qty": "10"}
    with patch("engine.alpaca_get_by_client_order_id", return_value=real_order):
        order = engine.find_or_place_order(client_order_id, place_fn_that_loses_the_response)

    assert order is real_order
    assert len(place_calls) == 1, "place_fn must NOT be called again once Alpaca already has this client_order_id"


def test_case2b_concurrent_submit_conflict_is_reconciled_not_raised():
    """
    Both workers check first and see nothing (the TOCTOU window the user
    explicitly flagged) -> both call place_fn -> Alpaca itself rejects the
    second submission as a duplicate client_order_id (409/422). That
    rejection must be caught and reconciled, not treated as a hard failure.
    """
    client_order_id = "nwt-entry-race-ticket"
    winning_order = {"id": "order-3", "status": "accepted"}

    conflict_response = MagicMock()
    conflict_response.status_code = 422
    conflict_error = engine.requests.exceptions.HTTPError("duplicate client_order_id")
    conflict_error.response = conflict_response

    def place_fn_that_loses_the_race():
        raise conflict_error

    with patch("engine.alpaca_get_by_client_order_id", side_effect=[None, winning_order]):
        order = engine.find_or_place_order(client_order_id, place_fn_that_loses_the_race)

    assert order is winning_order


# ---------------------------------------------------------------------------
# Case 3 — process crashes after submission: ticket retried, stale claim resumed
# ---------------------------------------------------------------------------

def test_case3_crash_after_submission_stale_claim_is_resumed_and_reconciled(conn):
    ticket_id = _insert_ticket(conn)

    # Simulates a prior run: claimed the ticket, then crashed before ever
    # calling insert_decision (no order-placement outcome recorded at all).
    assert engine.claim_ticket(conn, ticket_id) is True

    # Immediately after "crashing": fetch_pending_tickets must NOT return
    # this ticket yet — the claim is still fresh, could be a live worker.
    pending = engine.fetch_pending_tickets(conn)
    assert ticket_id not in {str(t["ticket_id"]) for t in pending}, \
        "A fresh CLAIMED row must not look retryable yet"

    # A second claim attempt right now must also fail — not stale yet.
    assert engine.claim_ticket(conn, ticket_id) is False

    # Age the claim past CLAIM_STALE_SECONDS, simulating time passing after
    # the crash (a real restart would just be a later cron tick).
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE nwt_ticket_decisions SET created_at = %s "
            "WHERE ticket_id = %s AND decided_by = 'EXECUTION_ENGINE'",
            (datetime.now(timezone.utc) - timedelta(seconds=engine.CLAIM_STALE_SECONDS + 30), ticket_id),
        )
    conn.commit()

    # Now it must be retryable, and a resume claim must succeed.
    pending = engine.fetch_pending_tickets(conn)
    assert ticket_id in {str(t["ticket_id"]) for t in pending}
    assert engine.claim_ticket(conn, ticket_id) is True

    # The resumed attempt must reconcile against Alpaca before resubmitting —
    # the crashed run's order may well have actually gone through.
    client_order_id = engine.client_order_id_for(ticket_id, "entry")
    already_placed_order = {"id": "order-4", "status": "filled", "filled_qty": "5"}
    place_calls = []

    def place_fn():
        place_calls.append(1)
        return {"id": "SHOULD-NOT-BE-CALLED"}

    with patch("engine.alpaca_get_by_client_order_id", return_value=already_placed_order):
        order = engine.find_or_place_order(client_order_id, place_fn)

    assert order is already_placed_order
    assert len(place_calls) == 0, "Resumed attempt must reconcile, never blindly resubmit"

    engine.insert_decision(conn, ticket_id, "EXECUTED", "Filled qty=5 at 100.0")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT decision FROM nwt_ticket_decisions WHERE ticket_id=%s AND decided_by='EXECUTION_ENGINE'",
            (ticket_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1, "The CLAIMED row is finalized in place — no second decision row from the resume"
    assert rows[0]["decision"] == "EXECUTED"


# ---------------------------------------------------------------------------
# Case 4 — concurrent workers, same ticket: exactly ONE claim wins
# This is the actual race the user flagged — tested against real concurrent
# Postgres connections and real threads, not simulated sequentially.
# ---------------------------------------------------------------------------

def test_case4_concurrent_workers_same_ticket_exactly_one_claim_wins(conn):
    ticket_id = _insert_ticket(conn)

    results = [None, None]
    barrier = threading.Barrier(2)

    def worker(idx):
        worker_conn = _fresh_conn()
        try:
            barrier.wait(timeout=5)  # maximize actual temporal overlap
            results[idx] = engine.claim_ticket(worker_conn, ticket_id)
        finally:
            worker_conn.close()

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert sorted(results) == [False, True], (
        f"Exactly one of two concurrent claim attempts must win, got {results} — "
        "if this ever shows [True, True] the unique index isn't doing its job"
    )

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM nwt_ticket_decisions WHERE ticket_id=%s AND decided_by='EXECUTION_ENGINE'",
            (ticket_id,),
        )
        n = cur.fetchone()["n"]
    assert n == 1, "Exactly one CLAIMED row must exist after the race, not two"


# ---------------------------------------------------------------------------
# Case 5 — legitimate separate signals: both must still go through independently
# ---------------------------------------------------------------------------

def test_case5_two_distinct_tickets_both_produce_their_own_order(conn):
    ticket_a = _insert_ticket(conn)
    ticket_b = _insert_ticket(conn)

    assert engine.claim_ticket(conn, ticket_a) is True
    assert engine.claim_ticket(conn, ticket_b) is True

    client_order_id_a = engine.client_order_id_for(ticket_a, "entry")
    client_order_id_b = engine.client_order_id_for(ticket_b, "entry")
    assert client_order_id_a != client_order_id_b

    placed = []

    def place_fn_a():
        placed.append("A")
        return {"id": "order-A"}

    def place_fn_b():
        placed.append("B")
        return {"id": "order-B"}

    with patch("engine.alpaca_get_by_client_order_id", return_value=None):
        order_a = engine.find_or_place_order(client_order_id_a, place_fn_a)
        order_b = engine.find_or_place_order(client_order_id_b, place_fn_b)

    assert order_a["id"] == "order-A"
    assert order_b["id"] == "order-B"
    assert placed == ["A", "B"], "Both legitimate signals must independently reach Alpaca"

    engine.insert_decision(conn, ticket_a, "EXECUTED", "Filled qty=1 at 100.0")
    engine.insert_decision(conn, ticket_b, "EXECUTED", "Filled qty=1 at 200.0")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT ticket_id, decision FROM nwt_ticket_decisions WHERE decided_by='EXECUTION_ENGINE' ORDER BY ticket_id"
        )
        rows = cur.fetchall()
    assert len(rows) == 2
    assert {str(r["ticket_id"]) for r in rows} == {ticket_a, ticket_b}
