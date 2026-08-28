"""
Regression tests for the genome_version attribution fix.

Confirmed production defect: track_c.py / track_d.py / track_e.py all call
get_strategy_genome() and use the returned genome for strategy parameters
(dte_min/max, stop_loss_pct, profit_target_pct, ...) but never extracted
genome["version"] into any of their log_decision_input() calls -- so every
Track C/D/E canonical observation was persisted with genome_version=NULL,
100% of the time, confirmed against live production data. This defeats
mutation-lineage attribution (Learning Layer C needs genome_version to tell
a baseline observation apart from a shadow-mutation-candidate observation
for the same strategy).

Fix: each of the (already-existing) log_decision_input() calls in
track_c.py/track_d.py/track_e.py now passes genome_version=genome.get("version"),
using the same genome object each strategist already loads via
get_strategy_genome(). Nothing else changed -- no new table, no schema
change, no touch to log_decision_input() itself, no backfill of historical
NULL rows.

DB-backed against real Postgres (NWT_TEST_DB_DSN), same convention as
test_canonical_decisions.py / test_statistical_validity.py -- genome
lookup, ON CONFLICT idempotency, and full main() wiring are exactly the
kind of logic a mock can't honestly exercise.
"""
import importlib.util
import sys
from datetime import date
from pathlib import Path
from unittest import mock

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_canonical_decisions import SCHEMA_SQL, TEST_DSN  # noqa: E402

GENOME_SCHEMA_SQL = """
DROP TABLE IF EXISTS nwt_strategy_genome CASCADE;
DROP TABLE IF EXISTS nwt_system_flags CASCADE;

CREATE TABLE nwt_strategy_genome (
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    track TEXT NOT NULL,
    archetype TEXT,
    asset_universe TEXT[],
    dte_min INTEGER,
    dte_max INTEGER,
    iv_filter_max NUMERIC,
    entry_threshold NUMERIC,
    stop_loss_pct NUMERIC,
    profit_target_pct NUMERIC,
    regime TEXT,
    shadow_mode BOOLEAN DEFAULT FALSE,
    active BOOLEAN DEFAULT TRUE,
    parent_version INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (strategy_id, version)
);
CREATE UNIQUE INDEX one_active_genome_test ON nwt_strategy_genome (strategy_id) WHERE active;

CREATE TABLE nwt_system_flags (
    flag TEXT PRIMARY KEY,
    value BOOLEAN NOT NULL DEFAULT FALSE,
    reason TEXT,
    set_by TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO nwt_system_flags (flag, value) VALUES ('no_trade_mode', FALSE);
"""


def _load_module(name: str, path: Path):
    """
    track_c.py/track_d.py/track_e.py are distinct module names already (no
    collision risk like the per-bot strategist.py files), but load by path
    anyway for consistency with test_statistical_validity.py's pattern and
    to guarantee a fresh module object per test session.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REPO_ROOT = Path(__file__).resolve().parents[2]
track_c = _load_module("track_c_under_test", _REPO_ROOT / "nwt_agents" / "track_c.py")
track_d = _load_module("track_d_under_test", _REPO_ROOT / "nwt_agents" / "track_d.py")
track_e = _load_module("track_e_under_test", _REPO_ROOT / "nwt_agents" / "track_e.py")


class _NonClosingConnProxy:
    """
    Wraps a real psycopg2 connection so track_c/d/e's `finally: conn.close()`
    doesn't tear down the test fixture's connection out from under later
    assertions in the same test.
    """
    def __init__(self, real_conn):
        self._real = real_conn

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self):
        pass  # no-op -- the test fixture owns the real close


@pytest.fixture()
def conn():
    if not TEST_DSN:
        pytest.skip("NWT_TEST_DB_DSN not set — skipping DB-backed regression tests")
    c = psycopg2.connect(TEST_DSN)
    try:
        with c.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(GENOME_SCHEMA_SQL)
        c.commit()
        yield c
    finally:
        c.rollback()
        c.close()


def _regime():
    return {"primary_regime": "risk_on", "confidence": 0.7, "secondary_regime": None, "transition_risk": 0.2}


def _insert_genome(conn, strategy_id, version, track, **overrides):
    fields = {
        "archetype": strategy_id,
        "asset_universe": ["SPY", "QQQ", "AAPL", "TSLA"],
        "dte_min": 14,
        "dte_max": 45,
        "iv_filter_max": 60.0,
        "entry_threshold": 0.5,
        "stop_loss_pct": 0.5,
        "profit_target_pct": 0.5,
        "regime": None,
        "shadow_mode": False,
        "active": True,
    }
    fields.update(overrides)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_strategy_genome "
            "(strategy_id, version, track, archetype, asset_universe, dte_min, dte_max, "
            " iv_filter_max, entry_threshold, stop_loss_pct, profit_target_pct, regime, "
            " shadow_mode, active) "
            "VALUES (%(strategy_id)s, %(version)s, %(track)s, %(archetype)s, %(asset_universe)s, "
            "        %(dte_min)s, %(dte_max)s, %(iv_filter_max)s, %(entry_threshold)s, "
            "        %(stop_loss_pct)s, %(profit_target_pct)s, %(regime)s, %(shadow_mode)s, %(active)s)",
            {"strategy_id": strategy_id, "version": version, "track": track, **fields},
        )
    conn.commit()


def _conviction_ticket(symbol="SPY", conviction_score=8, strategy_type="iron_condor", direction="long"):
    return {
        "symbol": symbol,
        "conviction_score": conviction_score,
        "strategy_type": strategy_type,
        "direction": direction,
        "confidence": 0.8,
        "dte_target": 21,
        "iv_at_conviction": 30.0,
        "entry_rationale": "test fixture ticket",
        "ticket_id": "conviction-fixture-1",
    }


def _run_track_main(track_module, conn, monkeypatch, conviction_tickets, layer0=None, directives=None):
    """
    Drive the real main() for track_c/d/e against a real Postgres connection,
    mocking only file-I/O and out-of-scope subsystems (integrity gate,
    shadow-mutation evaluation, capital sizing) so the genome_version wiring
    runs through the actual production code path, not a re-implementation.
    """
    layer0 = layer0 or {
        "spy_iv_skew": 0.0,
        "vix": 18.0,
        "symbols": {t["symbol"]: {"price": 100.0, "iv": 0.35, "atr_14": 1.0} for t in conviction_tickets},
    }
    directives = directives or {"regime": _regime(), "global_kill_switch": False, "bot_permissions": {}}

    monkeypatch.setattr(track_module, "get_db", lambda: _NonClosingConnProxy(conn))
    monkeypatch.setattr(track_module.integrity_gate, "run_integrity_gate", lambda *a, **k: None)
    monkeypatch.setattr(track_module, "load_master_directives", lambda: directives)
    monkeypatch.setattr(track_module, "load_conviction_tickets", lambda: conviction_tickets)
    monkeypatch.setattr(track_module, "load_layer0_data", lambda: layer0)
    monkeypatch.setattr(track_module, "check_no_trade_mode", lambda c: (False, None))
    monkeypatch.setattr(track_module, "compute_final_sizing", lambda *a, **k: 1000.0)
    monkeypatch.setattr(track_module, "evaluate_shadow_mutation", lambda *a, **k: None)

    track_module.main()


def _rows_for(conn, strategy_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, genome_version, signal_strength, regime, direction, strategy_id, outcome_reason "
            "FROM nwt_decision_inputs WHERE strategy_id = %s ORDER BY run_at DESC",
            (strategy_id,),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# 1-3: each track persists the genome version returned by get_strategy_genome()
# ---------------------------------------------------------------------------

def test_track_c_persists_genome_version(conn, monkeypatch):
    _insert_genome(conn, "C1", version=5, track="C", entry_threshold=0.3)
    _run_track_main(
        track_c, conn, monkeypatch,
        conviction_tickets=[_conviction_ticket(symbol="SPY", conviction_score=8)],
    )
    rows = _rows_for(conn, "C1")
    assert rows, "expected at least one nwt_decision_inputs row for C1"
    assert all(r["genome_version"] == 5 for r in rows)


def test_track_d_persists_genome_version(conn, monkeypatch):
    _insert_genome(conn, "D1", version=3, track="D", entry_threshold=0.3)
    _run_track_main(
        track_d, conn, monkeypatch,
        conviction_tickets=[_conviction_ticket(symbol="SPY", conviction_score=9, strategy_type="long_call")],
    )
    rows = _rows_for(conn, "D1")
    assert rows, "expected at least one nwt_decision_inputs row for D1"
    assert all(r["genome_version"] == 3 for r in rows)


def test_track_e_persists_genome_version(conn, monkeypatch):
    _insert_genome(conn, "E1", version=7, track="E", entry_threshold=0.3)
    _run_track_main(
        track_e, conn, monkeypatch,
        conviction_tickets=[_conviction_ticket(symbol="SPY", conviction_score=8)],
        layer0={
            "spy_iv_skew": 0.05,  # forces the iv_skew edge path, edge_magnitude > 0.30 threshold
            "vix": 18.0,
            "symbols": {"SPY": {"price": 100.0, "iv": 0.35, "atr_14": 1.0}},
        },
    )
    rows = _rows_for(conn, "E1")
    assert rows, "expected at least one nwt_decision_inputs row for E1"
    assert all(r["genome_version"] == 7 for r in rows)


# ---------------------------------------------------------------------------
# 4: a later genome mutation never alters an existing observation's stored
#    genome_version (immutability)
# ---------------------------------------------------------------------------

def test_genome_mutation_does_not_alter_historical_observation(conn, monkeypatch):
    _insert_genome(conn, "C1", version=1, track="C", entry_threshold=0.3)
    _run_track_main(
        track_c, conn, monkeypatch,
        conviction_tickets=[_conviction_ticket(symbol="SPY", conviction_score=8)],
    )
    rows_before = _rows_for(conn, "C1")
    assert rows_before and all(r["genome_version"] == 1 for r in rows_before)
    historical_id_count = len(rows_before)

    # Simulate mutation_agent.py --promote: deactivate v1, activate v2.
    with conn.cursor() as cur:
        cur.execute("UPDATE nwt_strategy_genome SET active = FALSE WHERE strategy_id='C1' AND version=1")
    conn.commit()
    _insert_genome(conn, "C1", version=2, track="C", entry_threshold=0.3, parent_version=1)

    # A later day's run against the mutated (v2) genome must not touch the
    # v1-attributed historical rows already in the table.
    rows_after_mutation = _rows_for(conn, "C1")
    assert len(rows_after_mutation) == historical_id_count
    assert all(r["genome_version"] == 1 for r in rows_after_mutation), (
        "genome mutation must not rewrite genome_version on pre-existing observations"
    )


# ---------------------------------------------------------------------------
# 5: existing idempotency still holds with genome_version now populated
# ---------------------------------------------------------------------------

def test_idempotency_still_holds_with_genome_version_populated(conn, monkeypatch):
    _insert_genome(conn, "C1", version=4, track="C", entry_threshold=0.3)
    tickets = [_conviction_ticket(symbol="SPY", conviction_score=8)]

    _run_track_main(track_c, conn, monkeypatch, conviction_tickets=tickets)
    first_run_rows = _rows_for(conn, "C1")

    # Same directional read, retried (cron overlap / process restart) same day.
    _run_track_main(track_c, conn, monkeypatch, conviction_tickets=tickets)
    second_run_rows = _rows_for(conn, "C1")

    assert len(second_run_rows) == len(first_run_rows), (
        "retrying the same directional read on the same day must not create a duplicate row "
        "now that genome_version is populated and part of the dedup key"
    )


# ---------------------------------------------------------------------------
# 6: signal_strength, regime, direction, strategy_id and other canonical
#    fields remain unchanged by this fix
# ---------------------------------------------------------------------------

def test_other_canonical_fields_unchanged_by_the_fix(conn, monkeypatch):
    _insert_genome(conn, "C1", version=2, track="C", entry_threshold=0.3)
    _run_track_main(
        track_c, conn, monkeypatch,
        conviction_tickets=[_conviction_ticket(symbol="SPY", conviction_score=8, direction="long")],
    )
    rows = _rows_for(conn, "C1")
    assert rows
    row = rows[0]
    assert row["strategy_id"] == "C1"
    assert row["symbol"] == "SPY"
    assert row["direction"] == "long"
    assert float(row["signal_strength"]) == 8.0
    assert row["regime"]["primary_regime"] == "risk_on"
    # genome_version is the only newly-populated field this fix adds.
    assert row["genome_version"] == 2
