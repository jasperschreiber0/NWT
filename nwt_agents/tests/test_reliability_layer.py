"""
nwt_agents/tests/test_reliability_layer.py
Regression tests for the position lifecycle state machine + autonomous
reconciliation (db/migrate_2026_07_reliability_layer.sql). Root incident:
SPY260720C00753000 rode past expiry, recon marked it status='suspect', and
nothing in the system ever looked at it again — a permanent, silent dead
end. These tests prove the replacement: every lifecycle_state transition is
recorded, expired/closed-at-broker positions resolve automatically, broker-
only positions get reconstructed or explicitly escalated (never silently
frozen), and the whole-run workers (recon_agent, expiry_sweeper) can't
double-process under concurrent cron invocations.

Run against a throwaway Postgres (NWT_TEST_DB_DSN), never production.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("NWT_DB_DSN", "postgresql://unused/unused")
os.environ.setdefault("NWT_ALPACA_KEY_ID", "test-key")
os.environ.setdefault("NWT_ALPACA_SECRET_KEY", "test-secret")

import shared_context  # noqa: E402
import recon_agent  # noqa: E402
import expiry_sweeper  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_ledger_position(conn, **overrides) -> str:
    defaults = {
        "bot_source": "NWT_TRACK_C",
        "asset": "SPY260720C00753000",
        "asset_type": "option",
        "direction": "long",
        "qty": 10,
        "entry_price": 4.67,
        "entry_time": datetime.now(timezone.utc) - timedelta(days=10),
        "status": "open",
        "lifecycle_state": "OPEN",
    }
    defaults.update(overrides)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join(["%s"] * len(defaults))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO nwt_portfolio_ledger ({cols}) VALUES ({placeholders}) RETURNING position_id",
            list(defaults.values()),
        )
        position_id = str(cur.fetchone()[0])
    conn.commit()
    return position_id


def _et_today():
    # option_dte() (shared_context.py) computes "today" in ET, not UTC —
    # these helpers must use the same reference or DTE arithmetic drifts by
    # a day whenever UTC and ET are on different calendar dates (i.e. most
    # of the day, since ET is 4-5 hours behind).
    return datetime.now(shared_context.ET_TZ).date()


def _future_expiry_symbol() -> str:
    d = (_et_today() + timedelta(days=10)).strftime("%y%m%d")
    return f"SPY{d}C00500000"


def _past_expiry_symbol() -> str:
    d = (_et_today() - timedelta(days=1)).strftime("%y%m%d")
    return f"SPY{d}C00500000"


def _today_expiry_symbol() -> str:
    d = _et_today().strftime("%y%m%d")
    return f"SPY{d}C00500000"


# ---------------------------------------------------------------------------
# transition_position_state — the core primitive
# ---------------------------------------------------------------------------

def test_transition_writes_history_and_syncs_legacy_status(conn):
    position_id = _insert_ledger_position(conn)

    ok = shared_context.transition_position_state(
        conn, position_id, "RECON_PENDING", "test transition", "test_source"
    )
    assert ok is True

    with conn.cursor() as cur:
        cur.execute("SELECT lifecycle_state, status FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        lifecycle_state, status = cur.fetchone()
    assert lifecycle_state == "RECON_PENDING"
    assert status == "open"  # RECON_PENDING must stay visible to status='open' consumers

    with conn.cursor() as cur:
        cur.execute(
            "SELECT previous_state, new_state, reason, source FROM position_state_history WHERE position_id=%s",
            (position_id,),
        )
        row = cur.fetchone()
    assert row == ("OPEN", "RECON_PENDING", "test transition", "test_source")


def test_transition_to_closed_maps_legacy_status_closed(conn):
    position_id = _insert_ledger_position(conn)
    shared_context.transition_position_state(conn, position_id, "EXPIRED", "expired", "test")
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        (status,) = cur.fetchone()
    assert status == "closed"


def test_transition_compare_and_swap_rejects_stale_expected_state(conn):
    position_id = _insert_ledger_position(conn)
    shared_context.transition_position_state(conn, position_id, "RECON_PENDING", "first", "a")

    # A second resolver believes the position is still OPEN (stale read) —
    # its CAS must be rejected, not silently overwrite the real state.
    ok = shared_context.transition_position_state(
        conn, position_id, "UNKNOWN", "stale writer", "b", expected_state="OPEN"
    )
    assert ok is False

    with conn.cursor() as cur:
        cur.execute("SELECT lifecycle_state FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        (state,) = cur.fetchone()
    assert state == "RECON_PENDING"  # unchanged by the rejected CAS


def test_transition_unknown_state_rejected():
    class FakeConn:
        pass
    with pytest.raises(ValueError):
        shared_context.transition_position_state(FakeConn(), "x", "NOT_A_REAL_STATE", "r", "s")


# ---------------------------------------------------------------------------
# Advisory locks — concurrency safety for whole-run workers
# ---------------------------------------------------------------------------

def test_advisory_lock_blocks_concurrent_holder(conn, conn2):
    assert shared_context.try_advisory_lock(conn, "test_lock_xyz") is True
    # A second, independent connection must NOT get the same lock.
    assert shared_context.try_advisory_lock(conn2, "test_lock_xyz") is False
    shared_context.release_advisory_lock(conn, "test_lock_xyz")
    # Now it's free again.
    assert shared_context.try_advisory_lock(conn2, "test_lock_xyz") is True
    shared_context.release_advisory_lock(conn2, "test_lock_xyz")


def test_advisory_lock_different_names_dont_block_each_other(conn, conn2):
    assert shared_context.try_advisory_lock(conn, "lock_a") is True
    assert shared_context.try_advisory_lock(conn2, "lock_b") is True
    shared_context.release_advisory_lock(conn, "lock_a")
    shared_context.release_advisory_lock(conn2, "lock_b")


# ---------------------------------------------------------------------------
# recon_agent: ledger OPEN, broker missing — autonomous resolution
# ---------------------------------------------------------------------------

def test_recon_expired_option_no_matching_order_becomes_expired(conn, monkeypatch):
    """The exact SPY260720C00753000 shape: option past expiry, Alpaca has
    no closing order for it (it just expired worthless) — must resolve to
    EXPIRED automatically, with a trade_outcomes row, not sit forever."""
    symbol = _past_expiry_symbol()
    position_id = _insert_ledger_position(conn, asset=symbol, entry_price=4.67, qty=10)

    monkeypatch.setattr(recon_agent, "fetch_symbol_orders", lambda sym, limit=50: [])

    row = {"position_id": position_id, "asset": symbol, "asset_type": "option",
           "direction": "long", "entry_price": 4.67, "qty": 10,
           "entry_time": datetime.now(timezone.utc) - timedelta(days=10),
           "bot_source": "NWT_TRACK_C", "strategy_id": "C1", "recon_attempts": 0}
    outcome = recon_agent.resolve_ledger_not_alpaca(conn, row)

    assert outcome == "EXPIRED"
    with conn.cursor() as cur:
        cur.execute("SELECT lifecycle_state, status, exit_price, exit_reason FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        state, status, exit_price, exit_reason = cur.fetchone()
    assert state == "EXPIRED"
    assert status == "closed"
    assert float(exit_price) == 0.0
    assert exit_reason == "expired_worthless"

    with conn.cursor() as cur:
        cur.execute("SELECT pnl, slippage_model FROM nwt_trade_outcomes WHERE position_id=%s", (position_id,))
        outcome_row = cur.fetchone()
    assert outcome_row is not None  # learning integrity: never invisible to the Learning Agent
    assert outcome_row[1] == "recon_expired"


def test_recon_matched_closing_order_becomes_closed_with_real_price(conn, monkeypatch):
    """Broker shows the position gone, but a real closing order explains it
    (manual close / external liquidation) — resolve to CLOSED with the
    ACTUAL fill price, not a guess."""
    symbol = _future_expiry_symbol()
    entry_time = datetime.now(timezone.utc) - timedelta(days=3)
    position_id = _insert_ledger_position(conn, asset=symbol, entry_price=4.67, qty=10, entry_time=entry_time)

    fake_order = {
        "id": "order-close-1", "side": "sell", "status": "filled",
        "filled_avg_price": "6.20",
        "filled_at": (entry_time + timedelta(days=1)).isoformat(),
    }
    monkeypatch.setattr(recon_agent, "fetch_symbol_orders", lambda sym, limit=50: [fake_order])

    row = {"position_id": position_id, "asset": symbol, "asset_type": "option",
           "direction": "long", "entry_price": 4.67, "qty": 10, "entry_time": entry_time,
           "bot_source": "NWT_TRACK_C", "strategy_id": "C1", "recon_attempts": 0}
    outcome = recon_agent.resolve_ledger_not_alpaca(conn, row)

    assert outcome == "CLOSED"
    with conn.cursor() as cur:
        cur.execute("SELECT lifecycle_state, exit_price, exit_reason FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        state, exit_price, exit_reason = cur.fetchone()
    assert state == "CLOSED"
    assert float(exit_price) == 6.20
    assert exit_reason == "reconciled_closed_at_broker"


def test_recon_ambiguous_mismatch_bounded_retry_then_escalates(conn, monkeypatch):
    """An equity mismatch (no expiry concept) with no matching order is
    genuinely ambiguous — must retry a bounded number of times, then
    escalate to UNKNOWN with a ticket, never loop forever silently."""
    position_id = _insert_ledger_position(conn, asset="AAPL", asset_type="equity", entry_price=300.0, qty=100)
    monkeypatch.setattr(recon_agent, "fetch_symbol_orders", lambda sym, limit=50: [])

    row = {"position_id": position_id, "asset": "AAPL", "asset_type": "equity",
           "direction": "long", "entry_price": 300.0, "qty": 100,
           "entry_time": datetime.now(timezone.utc), "bot_source": "US_BOT",
           "strategy_id": "US-1", "recon_attempts": 0}

    for expected_attempt in range(1, recon_agent.RECON_MAX_ATTEMPTS):
        outcome = recon_agent.resolve_ledger_not_alpaca(conn, row)
        assert outcome == "RECON_PENDING"
        with conn.cursor() as cur:
            cur.execute("SELECT recon_attempts, lifecycle_state FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
            attempts, state = cur.fetchone()
        assert attempts == expected_attempt
        assert state == "RECON_PENDING"
        row["recon_attempts"] = attempts

    # Final attempt crosses RECON_MAX_ATTEMPTS — must escalate, not retry forever.
    outcome = recon_agent.resolve_ledger_not_alpaca(conn, row)
    assert outcome == "UNKNOWN"
    with conn.cursor() as cur:
        cur.execute("SELECT lifecycle_state, status FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        state, status = cur.fetchone()
    assert state == "UNKNOWN"
    assert status == "open"  # still visible, not silently dropped

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_tickets WHERE type='recon_escalation_required'")
        (n,) = cur.fetchone()
    assert n == 1


def test_recon_broker_lookup_failure_does_not_falsely_conclude_expired(conn, monkeypatch):
    """A transient Alpaca API failure during the order-history lookup must
    NOT be treated the same as 'confirmed no closing order exists' — that
    would wrongly mark a real expiry-eligible option EXPIRED on a network
    blip instead of retrying."""
    symbol = _past_expiry_symbol()
    position_id = _insert_ledger_position(conn, asset=symbol, entry_price=4.67, qty=10)

    def _boom(sym, limit=50):
        raise ConnectionError("simulated network failure")
    monkeypatch.setattr(recon_agent, "fetch_symbol_orders", _boom)

    row = {"position_id": position_id, "asset": symbol, "asset_type": "option",
           "direction": "long", "entry_price": 4.67, "qty": 10,
           "entry_time": datetime.now(timezone.utc) - timedelta(days=10),
           "bot_source": "NWT_TRACK_C", "strategy_id": "C1", "recon_attempts": 0}
    outcome = recon_agent.resolve_ledger_not_alpaca(conn, row)

    assert outcome == "RECON_PENDING"  # NOT "EXPIRED" — inconclusive, not confirmed


# ---------------------------------------------------------------------------
# recon_agent: broker position exists, ledger missing — reconstruction
# ---------------------------------------------------------------------------

def test_recon_broker_only_position_auto_reconstructed(conn, monkeypatch):
    fake_order = {"id": "order-open-1", "side": "buy", "status": "filled",
                  "filled_qty": "300", "filled_avg_price": "317.50",
                  "filled_at": datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(recon_agent, "fetch_symbol_orders", lambda sym, limit=50: [fake_order])

    apos = {"qty": 300.0, "side": "long", "avg_entry": 317.5, "asset_class": "us_equity"}
    position_id = recon_agent.resolve_alpaca_not_ledger(conn, "AAPL", apos)

    assert position_id is not None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT bot_source, asset, qty, entry_price, lifecycle_state, status, alpaca_order_id "
            "FROM nwt_portfolio_ledger WHERE position_id=%s",
            (position_id,),
        )
        row = cur.fetchone()
    assert row[0] == "RECON_RECOVERED"
    assert row[1] == "AAPL"
    assert float(row[2]) == 300.0
    assert float(row[3]) == 317.5
    assert row[4] == "OPEN"
    assert row[5] == "open"
    assert row[6] == "order-open-1"

    with conn.cursor() as cur:
        cur.execute("SELECT resolved, resolution FROM nwt_unknown_broker_positions WHERE symbol='AAPL'")
        resolved, resolution = cur.fetchone()
    assert resolved is True
    assert resolution == "auto_reconstructed"


def test_recon_broker_only_position_unresolved_tracked_not_silently_dropped(conn, monkeypatch):
    monkeypatch.setattr(recon_agent, "fetch_symbol_orders", lambda sym, limit=50: [])

    apos = {"qty": 300.0, "side": "long", "avg_entry": 317.5, "asset_class": "us_equity"}
    position_id = recon_agent.resolve_alpaca_not_ledger(conn, "AAPL", apos)

    assert position_id is None  # not auto-resolved — genuinely unknown
    with conn.cursor() as cur:
        cur.execute(
            "SELECT qty, side, avg_price, resolved, first_seen_at FROM nwt_unknown_broker_positions WHERE symbol='AAPL'"
        )
        row = cur.fetchone()
    assert row is not None  # never silently dropped
    assert float(row[0]) == 300.0
    assert row[3] is False
    first_seen = row[4]

    # A second, later recon pass for the same still-unresolved symbol must
    # NOT reset first_seen_at — that's the whole point of tracking it.
    apos2 = dict(apos, avg_entry=318.0)
    recon_agent.resolve_alpaca_not_ledger(conn, "AAPL", apos2)
    with conn.cursor() as cur:
        cur.execute("SELECT avg_price, first_seen_at FROM nwt_unknown_broker_positions WHERE symbol='AAPL'")
        avg_price, first_seen_again = cur.fetchone()
    assert float(avg_price) == 318.0  # refreshed
    assert first_seen_again == first_seen  # but first_seen_at persists


def test_adopt_untracked_marks_unknown_broker_position_resolved(conn, monkeypatch):
    """
    Regression: adopt_untracked() imported the position into the ledger but
    never marked the corresponding nwt_unknown_broker_positions row
    resolved — system_health.py's report kept listing it as an unresolved
    broker-only position forever, even after a human had already cleared it.
    """
    apos = {"symbol": "AAPL", "qty": "300", "avg_entry_price": "317.5", "asset_class": "us_equity"}
    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions", lambda: [apos])
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_unknown_broker_positions (symbol, qty, side, avg_price, resolved) "
            "VALUES ('AAPL', 300, 'long', 317.5, FALSE)"
        )
    conn.commit()

    adopted = recon_agent.adopt_untracked(conn)
    assert adopted == 1

    with conn.cursor() as cur:
        cur.execute("SELECT resolved, resolution FROM nwt_unknown_broker_positions WHERE symbol='AAPL'")
        resolved, resolution = cur.fetchone()
    assert resolved is True
    assert resolution == "human_cleared"


def test_run_recon_no_trade_mode_only_cites_unresolved_mismatches(conn, monkeypatch):
    """After auto-resolving the AAPL broker-only position, no_trade_mode
    must NOT be set for it — only genuinely unresolved mismatches should
    still be able to trigger the halt."""
    fake_order = {"id": "o1", "side": "buy", "status": "filled",
                  "filled_qty": "300", "filled_avg_price": "317.50",
                  "filled_at": datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(recon_agent, "fetch_alpaca_positions",
                        lambda: [{"symbol": "AAPL", "qty": "300", "avg_entry_price": "317.5", "asset_class": "us_equity"}])
    monkeypatch.setattr(recon_agent, "fetch_symbol_orders", lambda sym, limit=50: [fake_order])

    clean = recon_agent.run_recon(conn, "test")
    assert clean is True  # fully auto-resolved — not critical

    with conn.cursor() as cur:
        cur.execute("SELECT value FROM nwt_system_flags WHERE flag='no_trade_mode'")
        row = cur.fetchone()
    assert row is None or row[0] is False


# ---------------------------------------------------------------------------
# expiry_sweeper
# ---------------------------------------------------------------------------

def test_expiry_sweeper_force_closes_expired_option_with_no_pending_ticket(conn):
    symbol = _today_expiry_symbol()
    position_id = _insert_ledger_position(conn, asset=symbol)

    expiry_sweeper.sweep(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT lifecycle_state FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        (state,) = cur.fetchone()
    assert state == "CLOSING"

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_tickets WHERE type='FORCE_CLOSE' AND payload->>'position_id'=%s",
                    (position_id,))
        (n,) = cur.fetchone()
    assert n == 1


def test_expiry_sweeper_skips_when_force_close_already_pending(conn):
    symbol = _today_expiry_symbol()
    position_id = _insert_ledger_position(conn, asset=symbol)
    shared_context.insert_ticket(conn, "RISK_AGENT", "EXECUTION_ENGINE", "FORCE_CLOSE",
                                 {"position_id": position_id, "symbol": symbol})

    expiry_sweeper.sweep(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_tickets WHERE type='FORCE_CLOSE' AND payload->>'position_id'=%s",
                    (position_id,))
        (n,) = cur.fetchone()
    assert n == 1  # unchanged — did not stack a second ticket


def test_expiry_sweeper_warns_one_day_before_expiry(conn):
    d = (_et_today() + timedelta(days=1)).strftime("%y%m%d")
    symbol = f"SPY{d}C00500000"
    position_id = _insert_ledger_position(conn, asset=symbol)

    expiry_sweeper.sweep(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT lifecycle_state FROM nwt_portfolio_ledger WHERE position_id=%s", (position_id,))
        (state,) = cur.fetchone()
    assert state == "OPEN"  # not force-closed yet — just warned
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_tickets WHERE type='expiry_warning'")
        (n,) = cur.fetchone()
    assert n == 1


def test_expiry_sweeper_ignores_positions_with_time_left(conn):
    _insert_ledger_position(conn, asset=_future_expiry_symbol())
    expiry_sweeper.sweep(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_tickets")
        (n,) = cur.fetchone()
    assert n == 0
