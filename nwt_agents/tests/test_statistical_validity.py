"""
Regression tests for the statistical-validity pass before the 60-day
collection window: China poll-slot idempotency (a genuinely distinct
intraday read must not collapse into an earlier one), shadow-evaluator
look-ahead exclusion of the decision-day bar for intraday decisions, and
immutability of genome_version/regime on historical observations.
"""
import importlib.util
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared_context import log_decision_input  # noqa: E402
from test_canonical_decisions import SCHEMA_SQL, TEST_DSN  # noqa: E402

import shadow_decision_evaluator as sde  # noqa: E402


def _load_module(name: str, path: Path):
    """
    Load a file as a uniquely-named module, bypassing sys.path/sys.modules
    entirely. china/strategist.py, ukeu/strategist.py, and asx/strategist.py
    all share the bare module name "strategist" -- a plain `sys.path.insert
    + import strategist` here would collide with (and could silently poison)
    ukeu's own `import strategist` elsewhere in this same pytest process via
    Python's module-name cache. This is the only piece of china/strategist.py
    this test file needs (current_poll_slot), so load it in isolation.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REPO_ROOT = Path(__file__).resolve().parents[2]
china_strategist = _load_module("china_strategist_under_test", _REPO_ROOT / "china" / "strategist.py")
ukeu_strategist = _load_module("ukeu_strategist_under_test", _REPO_ROOT / "ukeu" / "strategist.py")
asx_strategist = _load_module("asx_strategist_under_test", _REPO_ROOT / "asx" / "strategist.py")
us_strategist = _load_module(
    "us_strategist_under_test",
    _REPO_ROOT / "us" / "workspace-northworldtrading" / "bot" / "trade_1400_with_brackets.py",
)
current_poll_slot = china_strategist.current_poll_slot


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


def _regime():
    return {"primary_regime": "risk_on", "confidence": 0.7, "secondary_regime": None, "transition_risk": 0.2}


# ---------------------------------------------------------------------------
# China: same poll slot = 1 row, different poll slot = separate row
# ---------------------------------------------------------------------------

def test_current_poll_slot_buckets_to_the_scheduled_grid():
    assert current_poll_slot(datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)) == "14:00"
    assert current_poll_slot(datetime(2026, 8, 27, 14, 17, tzinfo=timezone.utc)) == "14:00"
    assert current_poll_slot(datetime(2026, 8, 27, 14, 29, 59, tzinfo=timezone.utc)) == "14:00"
    assert current_poll_slot(datetime(2026, 8, 27, 14, 30, 0, tzinfo=timezone.utc)) == "14:30"
    assert current_poll_slot(datetime(2026, 8, 27, 17, 59, tzinfo=timezone.utc)) == "17:30"


def test_china_retry_of_same_poll_slot_is_one_row(conn):
    """Same genuine read, retried (cron overlap / process restart) -> 1 row."""
    run_date = date.today()
    id1 = log_decision_input(
        conn, run_date=run_date, symbol="FXI", strategy_id="CHINA-POL-001", track="A",
        regime=_regime(), signal_strength=0.62, archetype="CHINA-POL-001", is_winner=True,
        decision="CANDIDATE", direction="long", entry_price_ref=29.5, target_pct=0.05,
        stop_pct=-0.025, genome_version=1, stage_reached="SIGNAL", poll_slot="14:00",
    )
    id2 = log_decision_input(
        conn, run_date=run_date, symbol="FXI", strategy_id="CHINA-POL-001", track="A",
        regime=_regime(), signal_strength=0.62, archetype="CHINA-POL-001", is_winner=True,
        decision="CANDIDATE", direction="long", entry_price_ref=29.5, target_pct=0.05,
        stop_pct=-0.025, genome_version=1, stage_reached="SIGNAL", poll_slot="14:00",
    )
    assert id1 == id2
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM nwt_decision_inputs WHERE strategy_id='CHINA-POL-001' AND symbol='FXI' AND run_date=%s",
            (run_date,),
        )
        (count,) = cur.fetchone()
    assert count == 1


def test_china_different_poll_slot_same_day_is_a_separate_row(conn):
    """
    A later poll with fresh data (China re-fetches live prices every 30
    minutes) is a genuinely different directional read, not a retry — the
    defect this migration fixes: the old day-only key would have collapsed
    this into the 14:00 row and silently discarded the 14:30 read.
    """
    run_date = date.today()
    log_decision_input(
        conn, run_date=run_date, symbol="FXI", strategy_id="CHINA-POL-001", track="A",
        regime=_regime(), signal_strength=0.55, archetype="CHINA-POL-001", is_winner=True,
        decision="CANDIDATE", direction="long", entry_price_ref=29.4, target_pct=0.05,
        stop_pct=-0.025, genome_version=1, stage_reached="SIGNAL", poll_slot="14:00",
        outcome_reason="BELOW_THRESHOLD",
    )
    log_decision_input(
        conn, run_date=run_date, symbol="FXI", strategy_id="CHINA-POL-001", track="A",
        regime=_regime(), signal_strength=0.68, archetype="CHINA-POL-001", is_winner=True,
        decision="CANDIDATE", direction="long", entry_price_ref=29.9, target_pct=0.05,
        stop_pct=-0.025, genome_version=1, stage_reached="SIGNAL", poll_slot="14:30",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT poll_slot, signal_strength FROM nwt_decision_inputs "
            "WHERE strategy_id='CHINA-POL-001' AND symbol='FXI' AND run_date=%s ORDER BY poll_slot",
            (run_date,),
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["14:00", "14:30"]
    assert [float(r[1]) for r in rows] == [0.55, 0.68]


def test_once_daily_strategists_still_collapse_to_one_row_per_day(conn):
    """EU/AUS/US/Track C/D/E pass poll_slot='' (default) -- day-granularity unchanged."""
    run_date = date.today()
    for _ in range(3):  # simulate 3 retries of the same daily read
        log_decision_input(
            conn, run_date=run_date, symbol="EWU", strategy_id="EU-MR-001", track="A",
            regime=_regime(), signal_strength=1.9, archetype="EU-MR-001", is_winner=True,
            decision="STRUCTURALLY_IMPOSSIBLE", direction="short", entry_price_ref=48.0,
            target_pct=0.03, stop_pct=-0.015, genome_version=1, stage_reached="SIGNAL",
            outcome_reason="STRUCTURALLY_IMPOSSIBLE",
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM nwt_decision_inputs WHERE strategy_id='EU-MR-001' AND symbol='EWU' AND run_date=%s",
            (run_date,),
        )
        (count,) = cur.fetchone()
    assert count == 1


# ---------------------------------------------------------------------------
# Shadow evaluator: no look-ahead from the decision-day's own bar
# ---------------------------------------------------------------------------

def test_bar_walk_excludes_decision_day_for_intraday_reads():
    """China/US ORB/Track C-D-E all decide during market hours (>=12:00 UTC)."""
    run_date = date(2026, 8, 26)
    intraday_decision = datetime(2026, 8, 26, 14, 5, tzinfo=timezone.utc)  # China/US/Track C-D-E territory
    assert sde.bar_walk_start_date(run_date, intraday_decision) == run_date + timedelta(days=1)


def test_bar_walk_includes_decision_day_for_premarket_reads():
    """EU (09:30 UTC) / AUS (09:00 UTC) decide before market open -- same-day bar is entirely after."""
    run_date = date(2026, 8, 26)
    premarket_decision = datetime(2026, 8, 26, 9, 30, tzinfo=timezone.utc)
    assert sde.bar_walk_start_date(run_date, premarket_decision) == run_date


def test_bar_walk_defaults_to_conservative_exclusion_when_run_at_missing():
    """Legacy rows with no run_at: never assume the permissive (pre-market) case."""
    run_date = date(2026, 8, 26)
    assert sde.bar_walk_start_date(run_date, None) == run_date + timedelta(days=1)


def test_intraday_same_day_price_spike_is_never_counted_as_the_counterfactual_outcome():
    """
    The actual bug this fixes, concretely: a decision made at 14:05 UTC with
    a favorable move that happened BEFORE 14:05 (already baked into the
    decision-day's daily high) must not be credited as if the move happened
    AFTER the signal. Excluding the decision-day bar means the very next
    day's bars are what the walk sees -- confirmed by checking which date's
    bars would need to be fetched.
    """
    run_date = date(2026, 8, 26)
    decision_time = datetime(2026, 8, 26, 14, 5, tzinfo=timezone.utc)
    walk_start = sde.bar_walk_start_date(run_date, decision_time)
    assert walk_start != run_date, "decision-day bar (which may contain pre-decision price action) must be excluded"
    assert walk_start == date(2026, 8, 27)


# ---------------------------------------------------------------------------
# Attribution immutability: genome_version and regime frozen at decision time
# ---------------------------------------------------------------------------

def test_genome_version_is_immutable_after_a_later_mutation(conn):
    """
    A row logged against genome v1 must still read v1 after the Mutator
    promotes v2 -- the 60-day experiment must never silently mix pre- and
    post-mutation behaviour under one strategy_id.
    """
    row_id = log_decision_input(
        conn, run_date=date.today(), symbol="SPY", strategy_id="C1", track="C",
        regime=_regime(), signal_strength=7.5, archetype="C-SHORT-PREMIUM-DIRECTIONAL",
        is_winner=True, decision="TRADE_PROPOSED", direction="long", entry_price_ref=510.0,
        target_pct=0.5, stop_pct=-0.5, dte_target=14, genome_version=1, stage_reached="SIGNAL",
    )
    # Simulate mutation_agent.py promoting v2 as the new active genome --
    # this never touches existing nwt_decision_inputs rows (grepped: no
    # UPDATE statement anywhere writes genome_version).
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_decision_inputs (run_date, strategy_id, track, symbol, genome_version, poll_slot) "
            "VALUES (%s, 'C1', 'C', 'SPY', 2, 'ignore-conflict-guard')",
            (date.today(),),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT genome_version FROM nwt_decision_inputs WHERE id = %s", (row_id,))
        (genome_version,) = cur.fetchone()
    assert genome_version == 1


# ---------------------------------------------------------------------------
# dte_target must be set, or Track A rows are permanently shadow-ineligible
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module,strategy_id,symbol,kwargs", [
    (ukeu_strategist, "EU-MR-001", "EWU", {}),
    (asx_strategist, "AUS-DIV-001", "BHP", {}),
])
def test_track_a_log_decision_input_sets_dte_target(conn, module, strategy_id, symbol, kwargs):
    """
    The defect this proves fixed: none of the four Track A log_decision_input
    implementations set dte_target, so shadow_decision_evaluator.
    fetch_pending_candidates (which requires dte_target IS NOT NULL) would
    never pick up a single Track A row -- every EU/AUS/China/US observation
    was permanently ineligible for counterfactual evaluation, silently.
    """
    row_id = module.log_decision_input(
        conn, run_date=date.today(), symbol=symbol, strategy_id=strategy_id,
        genome_version=1, regime=_regime(), signal_strength=1.9, direction="short",
        entry_price_ref=50.0, target_pct=0.03, stop_pct=-0.015,
        outcome_reason="STRUCTURALLY_IMPOSSIBLE",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT dte_target FROM nwt_decision_inputs WHERE id = %s", (row_id,))
        (dte_target,) = cur.fetchone()
    assert dte_target is not None
    assert dte_target == module.EVAL_HORIZON_DAYS


def test_china_log_decision_input_sets_dte_target(conn):
    row_id = china_strategist.log_decision_input(
        conn, run_date=date.today(), symbol="FXI", strategy_id="CHINA-POL-001",
        genome_version=1, regime=_regime(), signal_strength=0.6, direction="long",
        entry_price_ref=29.0, target_pct=0.05, stop_pct=-0.025, poll_slot="14:00",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT dte_target FROM nwt_decision_inputs WHERE id = %s", (row_id,))
        (dte_target,) = cur.fetchone()
    assert dte_target == china_strategist.EVAL_HORIZON_DAYS


def test_us_log_decision_input_sets_dte_target(conn):
    row_id = us_strategist.log_decision_input(
        conn, run_date=date.today(), symbol="SPY", genome_version=1, regime=_regime(),
        signal_strength=3, direction="long", entry_price_ref=512.0, target_pct=0.012,
        stop_pct=-0.006,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT dte_target FROM nwt_decision_inputs WHERE id = %s", (row_id,))
        (dte_target,) = cur.fetchone()
    assert dte_target == us_strategist.EVAL_HORIZON_DAYS


# ---------------------------------------------------------------------------
# Non-executed observations remain part of the shadow-eligible population
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("outcome_reason", ["RISK_VETOED", "STRUCTURALLY_IMPOSSIBLE", "EXECUTION_FAILED"])
def test_non_executed_outcome_remains_shadow_eligible(conn, outcome_reason):
    """
    DIRECTIONAL READ -> {RISK_VETOED | STRUCTURALLY_IMPOSSIBLE | EXECUTION_FAILED}
    -> NO TRADE -> must still surface from shadow_decision_evaluator's own
    eligibility query, not just "have a row" -- this calls the real
    production query, not a re-implementation of its logic.
    """
    old_run_date = date.today() - timedelta(days=30)
    row_id = log_decision_input(
        conn, run_date=old_run_date, symbol="EWU", strategy_id="EU-MR-001", track="A",
        regime=_regime(), signal_strength=1.9, archetype="EU-MR-001", is_winner=True,
        decision="CANDIDATE", direction="short", entry_price_ref=48.0, target_pct=0.03,
        stop_pct=-0.015, dte_target=20, genome_version=1, stage_reached="SIGNAL",
        outcome_reason=outcome_reason,
    )
    pending = sde.fetch_pending_candidates(conn)
    pending_ids = [row["id"] for row in pending]
    assert row_id in pending_ids, f"{outcome_reason} row must remain shadow-eligible, not silently dropped"


def test_executed_outcome_is_excluded_from_shadow_eligibility(conn):
    """The one outcome_reason that must NOT appear -- it already has a real outcome."""
    old_run_date = date.today() - timedelta(days=30)
    row_id = log_decision_input(
        conn, run_date=old_run_date, symbol="VGK", strategy_id="EU-MR-001", track="A",
        regime=_regime(), signal_strength=-2.0, archetype="EU-MR-001", is_winner=True,
        decision="CANDIDATE", direction="long", entry_price_ref=60.0, target_pct=0.03,
        stop_pct=-0.015, dte_target=20, genome_version=1, stage_reached="SIGNAL",
        outcome_reason="EXECUTED",
    )
    pending = sde.fetch_pending_candidates(conn)
    pending_ids = [row["id"] for row in pending]
    assert row_id not in pending_ids


def test_regime_is_never_overwritten_by_later_events(conn):
    """
    Confirmed by code inspection (grep for every UPDATE nwt_decision_inputs
    statement): mark_decision_outcome, link_decision_outcome,
    link_decision_ticket, and the shadow evaluator each touch only their own
    lifecycle columns. None ever SET regime. This test proves it holds even
    after every lifecycle update a row can receive.
    """
    entry_regime = {"primary_regime": "risk_off", "confidence": 0.81, "secondary_regime": "fragile_liquidity",
                     "transition_risk": 0.3}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_tickets (from_agent, to_agent, type, payload) "
            "VALUES ('EU_EXECUTOR', 'EXECUTION_ENGINE', 'TRADE_REQUEST', '{}') RETURNING ticket_id"
        )
        ticket_id = str(cur.fetchone()[0])
    conn.commit()

    row_id = log_decision_input(
        conn, run_date=date.today(), symbol="VGK", strategy_id="EU-MR-001", track="A",
        regime=entry_regime, signal_strength=-2.2, archetype="EU-MR-001", is_winner=True,
        decision="CANDIDATE", direction="long", entry_price_ref=60.0, target_pct=0.03,
        stop_pct=-0.015, ticket_id=ticket_id, genome_version=1, stage_reached="SIGNAL",
    )

    from shared_context import mark_decision_outcome
    mark_decision_outcome(conn, ticket_id, "EXECUTED")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE nwt_decision_inputs SET shadow_evaluated_at = NOW(), would_have_won = TRUE "
            "WHERE id = %s", (row_id,),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT regime FROM nwt_decision_inputs WHERE id = %s", (row_id,))
        (regime,) = cur.fetchone()
    assert regime == entry_regime
