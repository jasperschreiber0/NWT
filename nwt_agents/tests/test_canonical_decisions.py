"""
Regression tests for the canonical decision-observation model
(db/migrate_2026_08_canonical_decisions.sql + shared_context.py's
log_decision_input / mark_decision_outcome / link_decision_ticket /
link_decision_outcome).

DB-backed, same NWT_TEST_DB_DSN skip-if-absent convention as
test_trade_aggregation.py — this logic lives in real SQL (ON CONFLICT,
CHECK constraints, deterministic joins), not something a mock can honestly
exercise.
"""
import os
import uuid
from datetime import date, timedelta

import psycopg2
import pytest

TEST_DSN = os.environ.get("NWT_TEST_DB_DSN")

SCHEMA_SQL = """
DROP TABLE IF EXISTS nwt_decision_inputs CASCADE;
DROP TABLE IF EXISTS nwt_trade_outcomes CASCADE;
DROP TABLE IF EXISTS nwt_ticket_decisions CASCADE;
DROP TABLE IF EXISTS nwt_tickets CASCADE;
DROP TABLE IF EXISTS nwt_portfolio_ledger CASCADE;

CREATE TABLE nwt_tickets (
    ticket_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    type TEXT NOT NULL,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE nwt_ticket_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID REFERENCES nwt_tickets(ticket_id),
    decision TEXT NOT NULL,
    reasoning TEXT,
    decided_by TEXT,
    sizing_multiplier NUMERIC,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE nwt_portfolio_ledger (
    position_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bot_source TEXT NOT NULL,
    strategy_id TEXT,
    asset TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    direction TEXT,
    status TEXT DEFAULT 'open',
    ticket_id UUID REFERENCES nwt_tickets(ticket_id),
    spread_group_id UUID
);

CREATE TABLE nwt_trade_outcomes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id TEXT NOT NULL,
    archetype TEXT,
    symbol TEXT,
    direction TEXT,
    pnl NUMERIC,
    pnl_pct NUMERIC,
    pnl_adjusted NUMERIC,
    exit_time TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    position_id UUID REFERENCES nwt_portfolio_ledger(position_id)
);

CREATE TABLE nwt_decision_inputs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_date DATE NOT NULL,
    run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol TEXT,
    strategy_id TEXT,
    track TEXT,
    regime JSONB,
    conviction_score NUMERIC,
    signal_strength NUMERIC,
    asset_class TEXT,
    archetype TEXT,
    is_winner BOOLEAN,
    direction TEXT,
    entry_price_ref NUMERIC,
    target_pct NUMERIC,
    stop_pct NUMERIC,
    dte_target INTEGER,
    decision TEXT,
    rejection_reason TEXT,
    ticket_id UUID REFERENCES nwt_tickets(ticket_id),
    outcome_id UUID REFERENCES nwt_trade_outcomes(id),
    genome_version INTEGER,
    stage_reached TEXT CHECK (stage_reached IS NULL OR stage_reached IN ('SIGNAL', 'RISK', 'EXECUTION')),
    outcome_reason TEXT CHECK (outcome_reason IS NULL OR outcome_reason IN (
        'NO_EDGE', 'BELOW_THRESHOLD', 'RISK_VETOED', 'EXECUTION_FAILED',
        'STRUCTURALLY_IMPOSSIBLE', 'DUPLICATE_POSITION', 'EXECUTED'
    )),
    shadow_evaluated_at TIMESTAMPTZ,
    would_have_won BOOLEAN,
    shadow_exit_price NUMERIC,
    shadow_pnl_pct NUMERIC,
    shadow_mfe_pct NUMERIC,
    shadow_mae_pct NUMERIC,
    shadow_completion TEXT CHECK (shadow_completion IS NULL OR shadow_completion IN (
        'TARGET_HIT', 'STOP_HIT', 'HORIZON_EXPIRED'
    )),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_decision_inputs_dedup
  ON nwt_decision_inputs (strategy_id, COALESCE(genome_version, 0), COALESCE(symbol, ''), run_date);

CREATE TABLE IF NOT EXISTS nwt_system_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    level TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""


@pytest.fixture()
def conn():
    if not TEST_DSN:
        pytest.skip("NWT_TEST_DB_DSN not set — skipping DB-backed regression tests")
    c = psycopg2.connect(TEST_DSN)
    try:
        with c.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        c.commit()
        yield c
    finally:
        c.rollback()
        c.close()


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared_context import (  # noqa: E402
    link_decision_outcome,
    link_decision_ticket,
    log_decision_input,
    mark_decision_outcome,
)


def _regime():
    return {"primary_regime": "risk_on", "confidence": 0.7, "secondary_regime": None, "transition_risk": 0.2}


# ---------------------------------------------------------------------------
# 1-6: every strategist can persist an observation with the required fields
# ---------------------------------------------------------------------------

def test_log_decision_input_persists_all_required_fields(conn):
    row_id = log_decision_input(
        conn, run_date=date.today(), symbol="EWU", strategy_id="EU-MR-001",
        track="A", regime=_regime(), signal_strength=1.82, archetype="EU-MR-001",
        is_winner=True, decision="STRUCTURALLY_IMPOSSIBLE", direction="short",
        entry_price_ref=48.47, target_pct=0.03, stop_pct=-0.015, genome_version=3,
        asset_class="equity", stage_reached="SIGNAL", outcome_reason="STRUCTURALLY_IMPOSSIBLE",
    )
    assert row_id is not None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT strategy_id, genome_version, symbol, track, asset_class, "
            "       signal_strength, regime, entry_price_ref, target_pct, stop_pct, "
            "       stage_reached, outcome_reason, ticket_id "
            "FROM nwt_decision_inputs WHERE id = %s",
            (row_id,),
        )
        row = cur.fetchone()

    (strategy_id, genome_version, symbol, track, asset_class, signal_strength,
     regime, entry_price_ref, target_pct, stop_pct, stage_reached, outcome_reason, ticket_id) = row

    assert strategy_id == "EU-MR-001"
    assert genome_version == 3
    assert symbol == "EWU"
    assert track == "A"
    assert asset_class == "equity"
    assert float(signal_strength) == 1.82   # numeric, not buried in text
    assert regime["primary_regime"] == "risk_on"
    assert float(entry_price_ref) == 48.47
    assert float(target_pct) == 0.03
    assert float(stop_pct) == -0.015
    assert stage_reached == "SIGNAL"
    assert outcome_reason == "STRUCTURALLY_IMPOSSIBLE"
    assert ticket_id is None  # this incident never became a ticket


def test_ewu_regression_case_exact_shape_from_the_incident(conn):
    """
    The EU-MR-001/EWU incident, as specified in the task: a genuine short
    directional read at z-score=1.82, blocked as STRUCTURALLY_IMPOSSIBLE,
    never a ticket — and still eligible for counterfactual evaluation
    (entry_price_ref/target_pct/stop_pct/direction all present).
    """
    row_id = log_decision_input(
        conn, run_date=date.today(), symbol="EWU", strategy_id="EU-MR-001",
        track="A", regime=_regime(), signal_strength=1.82, archetype="EU-MR-001",
        is_winner=True, decision="STRUCTURALLY_IMPOSSIBLE", direction="short",
        entry_price_ref=48.47, target_pct=0.03, stop_pct=-0.015, genome_version=1,
        asset_class="equity", stage_reached="SIGNAL", outcome_reason="STRUCTURALLY_IMPOSSIBLE",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticket_id, outcome_id, entry_price_ref, target_pct, stop_pct, direction "
            "FROM nwt_decision_inputs WHERE id = %s", (row_id,),
        )
        ticket_id, outcome_id, entry_price_ref, target_pct, stop_pct, direction = cur.fetchone()

    assert ticket_id is None
    assert outcome_id is None
    assert direction == "short"
    # Eligible for shadow_decision_evaluator.py (all four fields present)
    assert entry_price_ref is not None and target_pct is not None and stop_pct is not None


# ---------------------------------------------------------------------------
# 7, 8: risk-vetoed / structurally-impossible signals remain observations
# ---------------------------------------------------------------------------

def test_risk_vetoed_signal_remains_a_learning_observation(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (from_agent, to_agent, type, payload) "
            "VALUES ('TRACK_C', 'RISK_AGENT', 'TRADE_PROPOSAL', '{}') RETURNING ticket_id"
        )
        ticket_id = str(cur.fetchone()[0])
    conn.commit()

    log_decision_input(
        conn, run_date=date.today(), symbol="SPY", strategy_id="C1", track="C",
        regime=_regime(), signal_strength=7.2, archetype="C-SHORT-PREMIUM-DIRECTIONAL",
        is_winner=True, decision="TRADE_PROPOSED", direction="long",
        entry_price_ref=512.0, target_pct=0.5, stop_pct=-0.5, dte_target=14,
        ticket_id=ticket_id, genome_version=None, stage_reached="SIGNAL",
    )

    mark_decision_outcome(conn, ticket_id, "RISK_VETOED")

    with conn.cursor() as cur:
        cur.execute("SELECT outcome_reason FROM nwt_decision_inputs WHERE ticket_id = %s", (ticket_id,))
        (outcome_reason,) = cur.fetchone()
    assert outcome_reason == "RISK_VETOED"


def test_mark_decision_outcome_never_overwrites_a_set_outcome(conn):
    """A late/duplicate call must not clobber an already-terminal state."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (from_agent, to_agent, type, payload) "
            "VALUES ('EU_EXECUTOR', 'EXECUTION_ENGINE', 'TRADE_REQUEST', '{}') RETURNING ticket_id"
        )
        ticket_id = str(cur.fetchone()[0])
    conn.commit()

    log_decision_input(
        conn, run_date=date.today(), symbol="VGK", strategy_id="EU-MR-001", track="A",
        regime=_regime(), signal_strength=-2.1, archetype="EU-MR-001", is_winner=True,
        decision="CANDIDATE", direction="long", entry_price_ref=60.0, target_pct=0.03,
        stop_pct=-0.015, ticket_id=ticket_id, genome_version=1, stage_reached="SIGNAL",
        outcome_reason="EXECUTED",
    )
    mark_decision_outcome(conn, ticket_id, "EXECUTION_FAILED")  # must be a no-op

    with conn.cursor() as cur:
        cur.execute("SELECT outcome_reason FROM nwt_decision_inputs WHERE ticket_id = %s", (ticket_id,))
        (outcome_reason,) = cur.fetchone()
    assert outcome_reason == "EXECUTED"


def test_controlled_vocabulary_rejects_arbitrary_outcome_reason(conn):
    with pytest.raises(psycopg2.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO nwt_decision_inputs (run_date, strategy_id, outcome_reason) "
                "VALUES (%s, 'X', 'MADE_UP_REASON')",
                (date.today(),),
            )
    conn.rollback()


# ---------------------------------------------------------------------------
# 9: executed decision eventually receives outcome_id (deterministic path)
# ---------------------------------------------------------------------------

def test_executed_decision_receives_outcome_id_via_deterministic_ticket_link(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (from_agent, to_agent, type, payload) "
            "VALUES ('EU_EXECUTOR', 'EXECUTION_ENGINE', 'TRADE_REQUEST', '{}') RETURNING ticket_id"
        )
        ticket_id = str(cur.fetchone()[0])

        cur.execute(
            "INSERT INTO nwt_portfolio_ledger (bot_source, asset, asset_type, status, ticket_id) "
            "VALUES ('EU_BOT', 'VGK', 'equity', 'closed', %s) RETURNING position_id",
            (ticket_id,),
        )
        position_id = str(cur.fetchone()[0])

        cur.execute(
            "INSERT INTO nwt_trade_outcomes (strategy_id, symbol, position_id, pnl_adjusted, closed_at) "
            "VALUES ('EU-MR-001', 'VGK', %s, 120.0, NOW()) RETURNING id",
            (position_id,),
        )
        outcome_id = str(cur.fetchone()[0])
    conn.commit()

    log_decision_input(
        conn, run_date=date.today(), symbol="VGK", strategy_id="EU-MR-001", track="A",
        regime=_regime(), signal_strength=-2.4, archetype="EU-MR-001", is_winner=True,
        decision="CANDIDATE", direction="long", entry_price_ref=60.0, target_pct=0.03,
        stop_pct=-0.015, ticket_id=ticket_id, genome_version=1, stage_reached="SIGNAL",
    )

    link_decision_outcome(conn, ticket_id, outcome_id)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT outcome_id, outcome_reason FROM nwt_decision_inputs WHERE ticket_id = %s", (ticket_id,)
        )
        got_outcome_id, outcome_reason = cur.fetchone()
    assert str(got_outcome_id) == outcome_id
    assert outcome_reason == "EXECUTED"


def test_link_decision_ticket_wires_strategist_row_to_executor_ticket(conn):
    """Track A: strategist writes the row first (no ticket yet), executor links it later."""
    run_date = date.today()
    log_decision_input(
        conn, run_date=run_date, symbol="FEZ", strategy_id="EU-MR-001", track="A",
        regime=_regime(), signal_strength=1.9, archetype="EU-MR-001", is_winner=True,
        decision="CANDIDATE", direction="short", entry_price_ref=45.0, target_pct=0.03,
        stop_pct=-0.015, genome_version=1, stage_reached="SIGNAL",
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (from_agent, to_agent, type, payload) "
            "VALUES ('EU_EXECUTOR', 'EXECUTION_ENGINE', 'TRADE_REQUEST', '{}') RETURNING ticket_id"
        )
        ticket_id = str(cur.fetchone()[0])
    conn.commit()

    link_decision_ticket(conn, "EU-MR-001", "FEZ", run_date, ticket_id, genome_version=1)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticket_id FROM nwt_decision_inputs WHERE strategy_id='EU-MR-001' AND symbol='FEZ'"
        )
        (linked,) = cur.fetchone()
    assert str(linked) == ticket_id


# ---------------------------------------------------------------------------
# 12: duplicate/retry cannot create a duplicate observation
# ---------------------------------------------------------------------------

def test_idempotent_retry_produces_exactly_one_row(conn):
    run_date = date.today()
    id1 = log_decision_input(
        conn, run_date=run_date, symbol="EWU", strategy_id="EU-MR-001", track="A",
        regime=_regime(), signal_strength=1.82, archetype="EU-MR-001", is_winner=True,
        decision="STRUCTURALLY_IMPOSSIBLE", direction="short", entry_price_ref=48.47,
        target_pct=0.03, stop_pct=-0.015, genome_version=1, stage_reached="SIGNAL",
        outcome_reason="STRUCTURALLY_IMPOSSIBLE",
    )
    # Simulate a cron retry / process restart re-evaluating the same symbol
    # on the same day — must be a no-op, not a second row.
    id2 = log_decision_input(
        conn, run_date=run_date, symbol="EWU", strategy_id="EU-MR-001", track="A",
        regime=_regime(), signal_strength=1.82, archetype="EU-MR-001", is_winner=True,
        decision="STRUCTURALLY_IMPOSSIBLE", direction="short", entry_price_ref=48.47,
        target_pct=0.03, stop_pct=-0.015, genome_version=1, stage_reached="SIGNAL",
        outcome_reason="STRUCTURALLY_IMPOSSIBLE",
    )

    assert id1 == id2

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM nwt_decision_inputs WHERE strategy_id='EU-MR-001' AND symbol='EWU' AND run_date=%s",
            (run_date,),
        )
        (count,) = cur.fetchone()
    assert count == 1


def test_different_symbols_or_days_are_not_collapsed(conn):
    """The idempotency key must not over-collapse genuinely distinct observations."""
    run_date = date.today()
    log_decision_input(
        conn, run_date=run_date, symbol="EWU", strategy_id="EU-MR-001", track="A",
        regime=_regime(), signal_strength=1.82, archetype="EU-MR-001", is_winner=True,
        decision="STRUCTURALLY_IMPOSSIBLE", direction="short", entry_price_ref=48.47,
        target_pct=0.03, stop_pct=-0.015, genome_version=1, stage_reached="SIGNAL",
        outcome_reason="STRUCTURALLY_IMPOSSIBLE",
    )
    log_decision_input(
        conn, run_date=run_date, symbol="VGK", strategy_id="EU-MR-001", track="A",
        regime=_regime(), signal_strength=-2.1, archetype="EU-MR-001", is_winner=True,
        decision="CANDIDATE", direction="long", entry_price_ref=60.0, target_pct=0.03,
        stop_pct=-0.015, genome_version=1, stage_reached="SIGNAL",
    )
    log_decision_input(
        conn, run_date=run_date - timedelta(days=1), symbol="EWU", strategy_id="EU-MR-001", track="A",
        regime=_regime(), signal_strength=1.7, archetype="EU-MR-001", is_winner=True,
        decision="STRUCTURALLY_IMPOSSIBLE", direction="short", entry_price_ref=47.0,
        target_pct=0.03, stop_pct=-0.015, genome_version=1, stage_reached="SIGNAL",
        outcome_reason="STRUCTURALLY_IMPOSSIBLE",
    )

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_decision_inputs WHERE strategy_id='EU-MR-001'")
        (count,) = cur.fetchone()
    assert count == 3


# ---------------------------------------------------------------------------
# 11: realized and shadow outcomes remain semantically separate
# ---------------------------------------------------------------------------

def test_realized_and_shadow_columns_never_conflated(conn):
    """
    An EXECUTED row's PnL lives only in nwt_trade_outcomes (via outcome_id);
    a non-executed row's PnL lives only in shadow_pnl_pct. Neither column
    set is ever populated on the wrong kind of row by this model.
    """
    row_id = log_decision_input(
        conn, run_date=date.today(), symbol="EWU", strategy_id="EU-MR-001", track="A",
        regime=_regime(), signal_strength=1.82, archetype="EU-MR-001", is_winner=True,
        decision="STRUCTURALLY_IMPOSSIBLE", direction="short", entry_price_ref=48.47,
        target_pct=0.03, stop_pct=-0.015, genome_version=1, stage_reached="SIGNAL",
        outcome_reason="STRUCTURALLY_IMPOSSIBLE",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT outcome_id, shadow_pnl_pct FROM nwt_decision_inputs WHERE id = %s", (row_id,)
        )
        outcome_id, shadow_pnl_pct = cur.fetchone()
    # Never executed -> no real outcome_id. Not yet shadow-evaluated -> NULL.
    assert outcome_id is None
    assert shadow_pnl_pct is None
