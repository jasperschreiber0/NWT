"""
nwt_agents/tests/test_execution_agent_resilience.py
Regression test for the deployment-readiness audit finding: in
execution_agent.py, the stretch of payload parsing and the pre_trade_veto
call between claim_ticket() succeeding and the nearest try/except was
unguarded, and main() has no outer exception handler. A single malformed
ticket would crash the whole process every cron cycle, forever, since it's
always the oldest unprocessed ticket (ORDER BY created_at ASC) and never
gets a decision written.

Fixed by wrapping the entire per-ticket body (_process_approved_proposal)
in a catch-all inside _handle_one_proposal, which turns ANY exception into
a terminal FAILED decision and a released (retryable) claim instead of
letting it propagate.

Run against a throwaway Postgres (NWT_TEST_DB_DSN), never production:
    NWT_TEST_DB_DSN=postgresql://nwt_test:nwt_test_pw@localhost/nwt_exec_agent_test \
        pytest nwt_agents/tests/test_execution_agent_resilience.py -v
"""
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_context import claim_ticket  # noqa: E402
from execution_agent import _handle_one_proposal  # noqa: E402


def _insert_proposal_ticket(conn, payload: dict) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (from_agent, to_agent, type, payload) "
            "VALUES ('RISK_AGENT', 'RISK_AGENT', 'TRADE_PROPOSAL', %s) RETURNING ticket_id",
            (json.dumps(payload),),
        )
        ticket_id = str(cur.fetchone()[0])
    conn.commit()
    return ticket_id


def test_malformed_sized_notional_does_not_raise(conn):
    """
    float(payload["sized_notional"]) is one of the first lines in
    _process_approved_proposal — a non-numeric value must not escape as an
    uncaught exception.
    """
    ticket_id = _insert_proposal_ticket(conn, {"symbol": "AAPL", "sized_notional": "not-a-number"})
    ticket = {"ticket_id": ticket_id, "payload": {"symbol": "AAPL", "sized_notional": "not-a-number"}}

    outcome = _handle_one_proposal(conn, ticket)  # must not raise

    assert outcome == "unhandled_error"


def test_malformed_ticket_gets_a_terminal_failed_decision(conn):
    """
    Before this fix, a crash here left NO decision at all, which is exactly
    what let the same ticket get re-selected and re-crash every cron cycle
    forever. A FAILED decision is what actually stops that.
    """
    ticket_id = _insert_proposal_ticket(conn, {"symbol": "AAPL", "dte_min": "seven"})
    ticket = {"ticket_id": ticket_id, "payload": {"symbol": "AAPL", "dte_min": "seven"}}

    _handle_one_proposal(conn, ticket)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT decision FROM nwt_ticket_decisions WHERE ticket_id = %s AND decided_by = 'NWT_EXECUTION_AGENT'",
            (ticket_id,),
        )
        decisions = [r[0] for r in cur.fetchall()]
    assert decisions == ["FAILED"]


def test_malformed_ticket_releases_its_claim_as_failed_not_stuck(conn):
    """The claim must not be left dangling at status='in_progress' forever
    — it must become 'failed' so a later run can legitimately retry it."""
    ticket_id = _insert_proposal_ticket(conn, {"symbol": "AAPL", "sized_notional": []})
    ticket = {"ticket_id": ticket_id, "payload": {"symbol": "AAPL", "sized_notional": []}}

    _handle_one_proposal(conn, ticket)

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_ticket_claims WHERE ticket_id = %s", (ticket_id,))
        (status,) = cur.fetchone()
    assert status == "failed"


def test_second_worker_cannot_process_the_same_malformed_ticket_concurrently(conn, conn2):
    """
    Even for a ticket that's about to fail, the claim must still do its job:
    only one worker gets to attempt it at a time.
    """
    ticket_id = _insert_proposal_ticket(conn, {"symbol": "AAPL", "sized_notional": "bad"})

    first = claim_ticket(conn, ticket_id, worker_id="worker-a")
    second = claim_ticket(conn2, ticket_id, worker_id="worker-b")

    assert first is True
    assert second is False


def test_a_second_valid_ticket_is_not_claimed_by_processing_the_first(conn):
    """
    Sanity check that claiming/handling one ticket has no effect on an
    unrelated ticket's claimability — i.e. one bad ticket's failure can't
    somehow lock out its neighbors via a shared resource.
    """
    bad_ticket_id = _insert_proposal_ticket(conn, {"symbol": "AAPL", "sized_notional": "bad"})
    other_ticket_id = _insert_proposal_ticket(conn, {"symbol": "TSLA", "sized_notional": 500})

    _handle_one_proposal(conn, {"ticket_id": bad_ticket_id, "payload": {"symbol": "AAPL", "sized_notional": "bad"}})

    still_claimable = claim_ticket(conn, other_ticket_id, worker_id="worker-a")
    assert still_claimable is True
