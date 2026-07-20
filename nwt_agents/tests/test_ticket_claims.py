"""
nwt_agents/tests/test_ticket_claims.py
Regression tests for the order-execution race condition confirmed by tracing
execution_agent.py and execution/engine.py: crontab.txt runs both every 5
minutes with no overlap guard, and both select unclaimed work with
"SELECT ... WHERE NOT EXISTS (a decision yet)", only marking a ticket handled
AFTER slow external Alpaca calls complete. Two overlapping cron runs could
both select the same ticket and both place a real duplicate broker order.

claim_ticket()/release_ticket_claim() (shared_context.py) close that window
with an atomic INSERT ... ON CONFLICT DO UPDATE ... WHERE guard against
nwt_ticket_claims. These tests prove, against real Postgres (not a mock),
that the guard actually serializes concurrent callers correctly.

Run against a throwaway Postgres (NWT_TEST_DB_DSN), never production:
    NWT_TEST_DB_DSN=postgresql://nwt_test:nwt_test_pw@localhost/nwt_claims_test \
        pytest nwt_agents/tests/test_ticket_claims.py -v
"""
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_context import claim_ticket, release_ticket_claim, renew_ticket_claim  # noqa: E402


def _insert_ticket(conn, ticket_type="TRADE_REQUEST"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (from_agent, to_agent, type, payload) "
            "VALUES ('TEST', 'EXECUTION_ENGINE', %s, '{}') RETURNING ticket_id",
            (ticket_type,),
        )
        ticket_id = str(cur.fetchone()[0])
    conn.commit()
    return ticket_id


# ---------------------------------------------------------------------------
# Sequential correctness
# ---------------------------------------------------------------------------

def test_first_caller_claims_successfully(conn):
    ticket_id = _insert_ticket(conn)

    got_it = claim_ticket(conn, ticket_id, worker_id="worker-a")

    assert got_it is True
    with conn.cursor() as cur:
        cur.execute("SELECT claimed_by, status FROM nwt_ticket_claims WHERE ticket_id = %s", (ticket_id,))
        claimed_by, status = cur.fetchone()
    assert claimed_by == "worker-a"
    assert status == "in_progress"


def test_second_caller_cannot_claim_a_live_claim(conn, conn2):
    """
    The exact scenario from the audit: two processes (here, two independent
    DB connections, simulating two overlapping cron-launched workers) both
    try to claim the same not-yet-processed ticket. Only one may succeed —
    this is what stops a duplicate broker order from ever being placed.
    """
    ticket_id = _insert_ticket(conn)

    first = claim_ticket(conn, ticket_id, worker_id="worker-a")
    second = claim_ticket(conn2, ticket_id, worker_id="worker-b")

    assert first is True
    assert second is False  # worker-b must never process this ticket


def test_release_then_reclaim_by_a_different_worker(conn):
    """After a worker finishes and releases (status='done'), the ticket must
    NOT be reclaimable — completion is permanent, unlike a stale lease."""
    ticket_id = _insert_ticket(conn)

    claim_ticket(conn, ticket_id, worker_id="worker-a")
    release_ticket_claim(conn, ticket_id, worker_id="worker-a", status="done")

    reclaimed = claim_ticket(conn, ticket_id, worker_id="worker-b")

    assert reclaimed is False


def test_failed_release_allows_retry_by_another_worker(conn):
    """A worker that hits an exception releases status='failed' — a later
    run (this cron cycle or the next) must be able to retry the ticket."""
    ticket_id = _insert_ticket(conn)

    claim_ticket(conn, ticket_id, worker_id="worker-a")
    release_ticket_claim(conn, ticket_id, worker_id="worker-a", status="failed")

    retried = claim_ticket(conn, ticket_id, worker_id="worker-b")

    assert retried is True


def test_stale_lease_is_reclaimable_after_a_crash(conn):
    """
    A worker that crashes mid-processing (no release call ever happens)
    leaves status='in_progress' forever unless the lease has an expiry.
    Simulate a crash by inserting a claim whose lease already expired, then
    confirm a fresh worker can take over instead of the ticket being stuck.
    """
    ticket_id = _insert_ticket(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_ticket_claims (ticket_id, claimed_by, lease_expires_at, status)
            VALUES (%s, 'crashed-worker', NOW() - INTERVAL '1 minute', 'in_progress')
            """,
            (ticket_id,),
        )
    conn.commit()

    recovered = claim_ticket(conn, ticket_id, worker_id="worker-b")

    assert recovered is True
    with conn.cursor() as cur:
        cur.execute("SELECT claimed_by FROM nwt_ticket_claims WHERE ticket_id = %s", (ticket_id,))
        (claimed_by,) = cur.fetchone()
    assert claimed_by == "worker-b"


def test_live_lease_is_not_reclaimable_before_expiry(conn):
    """The mirror case of the stale-lease test — a claim that is still
    within its lease window must not be stealable by a second worker."""
    ticket_id = _insert_ticket(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_ticket_claims (ticket_id, claimed_by, lease_expires_at, status)
            VALUES (%s, 'still-working', NOW() + INTERVAL '5 minutes', 'in_progress')
            """,
            (ticket_id,),
        )
    conn.commit()

    stolen = claim_ticket(conn, ticket_id, worker_id="worker-b")

    assert stolen is False


# ---------------------------------------------------------------------------
# True concurrency — two real threads, two real connections, racing on the
# exact same INSERT at (as close as the test harness can get to) the same
# instant. This is the strongest evidence available that the atomicity
# comes from Postgres's row lock, not from Python-level call ordering.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Ownership guard on release — final adversarial audit finding: a worker
# that no longer owns a claim (lease expired, reclaimed by someone else)
# must never be able to clobber that other worker's LIVE claim via
# release_ticket_claim. Before this fix, release had no claimed_by check at
# all, so a stale worker calling release(status='failed') would flip a
# currently-active claim to 'failed' — making it immediately reclaimable by
# a THIRD worker regardless of the second worker's lease_expires_at. This
# is the concrete "two workers both believe they own the ticket" scenario.
# ---------------------------------------------------------------------------

def test_release_by_a_worker_that_never_owned_the_claim_is_a_noop(conn):
    ticket_id = _insert_ticket(conn)
    claim_ticket(conn, ticket_id, worker_id="worker-a")

    released = release_ticket_claim(conn, ticket_id, worker_id="worker-b", status="failed")

    assert released is False
    with conn.cursor() as cur:
        cur.execute("SELECT claimed_by, status FROM nwt_ticket_claims WHERE ticket_id = %s", (ticket_id,))
        claimed_by, status = cur.fetchone()
    assert claimed_by == "worker-a"
    assert status == "in_progress"  # untouched — worker-a's claim survives


def test_stale_worker_cannot_sabotage_the_reclaiming_workers_live_claim(conn, conn2):
    """
    The exact scenario: worker-a's lease expires, worker-b legitimately
    reclaims and is actively processing, and THEN worker-a (unaware it lost
    ownership — e.g. it's still running a slow operation, or its own
    process is just slow to reach its except-block) tries to release. That
    release must not affect worker-b's now-active claim at all.
    """
    ticket_id = _insert_ticket(conn)
    claim_ticket(conn, ticket_id, worker_id="worker-a", lease_seconds=1)
    time.sleep(1.2)
    claim_ticket(conn2, ticket_id, worker_id="worker-b")  # worker-b legitimately reclaims

    # worker-a, unaware it lost the claim, tries to release as if it still owned it
    released = release_ticket_claim(conn, ticket_id, worker_id="worker-a", status="failed")

    assert released is False
    with conn.cursor() as cur:
        cur.execute("SELECT claimed_by, status FROM nwt_ticket_claims WHERE ticket_id = %s", (ticket_id,))
        claimed_by, status = cur.fetchone()
    # worker-b's claim must be completely untouched by worker-a's stale release
    assert claimed_by == "worker-b"
    assert status == "in_progress"

    # And a third worker must still be correctly blocked, exactly as if
    # worker-a's release call had never happened.
    third = claim_ticket(conn2, ticket_id, worker_id="worker-c")
    assert third is False


# ---------------------------------------------------------------------------
# Lease renewal — deployment-readiness audit fix: a fixed, un-renewed lease
# is close to or below the worst-case processing time of the very
# operations it protects (engine.py's poll_order_until_filled can take up
# to ~180s). renew_ticket_claim() lets a legitimately slow, still-owning
# worker extend its own lease instead of relying solely on picking a big
# enough constant.
# ---------------------------------------------------------------------------

def test_renew_extends_lease_for_the_owning_worker(conn):
    ticket_id = _insert_ticket(conn)
    claim_ticket(conn, ticket_id, worker_id="worker-a", lease_seconds=5)

    with conn.cursor() as cur:
        cur.execute("SELECT lease_expires_at FROM nwt_ticket_claims WHERE ticket_id = %s", (ticket_id,))
        (before,) = cur.fetchone()

    renewed = renew_ticket_claim(conn, ticket_id, worker_id="worker-a", lease_seconds=600)

    assert renewed is True
    with conn.cursor() as cur:
        cur.execute("SELECT lease_expires_at FROM nwt_ticket_claims WHERE ticket_id = %s", (ticket_id,))
        (after,) = cur.fetchone()
    assert after > before + timedelta(seconds=500)


def test_renew_prevents_lease_expiry_from_letting_a_second_worker_in(conn, conn2):
    """
    The exact scenario the fix targets: a slow-but-alive worker renews
    before its original lease would have expired, so a second worker that
    shows up afterward still cannot claim the ticket.
    """
    ticket_id = _insert_ticket(conn)
    claim_ticket(conn, ticket_id, worker_id="worker-a", lease_seconds=2)

    time.sleep(1)  # partway through the short lease, but not expired yet
    renewed = renew_ticket_claim(conn, ticket_id, worker_id="worker-a", lease_seconds=600)
    assert renewed is True

    time.sleep(2)  # past the ORIGINAL 2s lease, well within the renewed one

    stolen = claim_ticket(conn2, ticket_id, worker_id="worker-b")
    assert stolen is False  # worker-a's renewed lease is still live


def test_renew_fails_for_a_worker_that_already_lost_the_claim(conn, conn2):
    """A worker whose lease already expired and got reclaimed elsewhere must
    not be able to renew its way back into ownership."""
    ticket_id = _insert_ticket(conn)
    claim_ticket(conn, ticket_id, worker_id="worker-a", lease_seconds=1)
    time.sleep(1.2)
    claim_ticket(conn2, ticket_id, worker_id="worker-b")  # worker-b now owns it

    renewed = renew_ticket_claim(conn, ticket_id, worker_id="worker-a")

    assert renewed is False


def test_concurrent_threads_racing_on_first_claim_only_one_wins(conn, conn2):
    ticket_id = _insert_ticket(conn)

    results = {}
    barrier = threading.Barrier(2)

    def _attempt(name, connection):
        barrier.wait()  # release both threads as close to simultaneously as possible
        results[name] = claim_ticket(connection, ticket_id, worker_id=name)

    t1 = threading.Thread(target=_attempt, args=("worker-1", conn))
    t2 = threading.Thread(target=_attempt, args=("worker-2", conn2))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    winners = [w for w, got_it in results.items() if got_it]
    assert len(winners) == 1, f"expected exactly one winner, got {results}"
