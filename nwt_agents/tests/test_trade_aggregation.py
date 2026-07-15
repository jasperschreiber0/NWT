"""
nwt_agents/tests/test_trade_aggregation.py
Regression tests for the trade-accounting fix: nwt_trade_outcomes is one
row per LEG, not per TRADE. A multi-leg spread (iron_condor etc.) writes
2-4 outcome rows tied together via nwt_portfolio_ledger.spread_group_id;
every consumer must count/aggregate at the trade level, not the row level.

Run against a throwaway Postgres (NWT_TEST_DB_DSN), never production:
    NWT_TEST_DB_DSN=postgresql://nwt_test:nwt_test_pw@localhost/nwt_trade_agg_test \
        pytest nwt_agents/tests/test_trade_aggregation.py -v
"""
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_context import count_distinct_trades, get_distinct_trade_pnls  # noqa: E402

BASE_TIME = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _insert_ledger_leg(conn, strategy_id, spread_group_id=None):
    position_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_portfolio_ledger
                (position_id, bot_source, strategy_id, asset, asset_type, status, spread_group_id)
            VALUES (%s, 'TEST_BOT', %s, 'TESTSYM', 'option', 'closed', %s)
            """,
            (position_id, strategy_id, spread_group_id),
        )
    return position_id


def _insert_outcome_leg(conn, strategy_id, position_id, pnl, closed_at, archetype=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_trade_outcomes (strategy_id, archetype, symbol, pnl, closed_at, position_id)
            VALUES (%s, %s, 'TESTSYM', %s, %s, %s)
            """,
            (strategy_id, archetype, pnl, closed_at, position_id),
        )
    conn.commit()


def make_single_leg_trade(conn, strategy_id, pnl, closed_at, archetype=None):
    """One position, one outcome row — the position_id itself IS the trade identity."""
    position_id = _insert_ledger_leg(conn, strategy_id, spread_group_id=None)
    _insert_outcome_leg(conn, strategy_id, position_id, pnl, closed_at, archetype)


def make_spread_trade(conn, strategy_id, leg_pnls, closed_at, archetype=None):
    """N legs, each its own ledger row/position_id, tied by one shared spread_group_id."""
    spread_group_id = str(uuid.uuid4())
    for pnl in leg_pnls:
        position_id = _insert_ledger_leg(conn, strategy_id, spread_group_id=spread_group_id)
        _insert_outcome_leg(conn, strategy_id, position_id, pnl, closed_at, archetype)
    return spread_group_id


# ---------------------------------------------------------------------------
# Required scenarios
# ---------------------------------------------------------------------------

def test_single_leg_trade_counts_as_one(conn):
    make_single_leg_trade(conn, "D1", pnl=42.0, closed_at=BASE_TIME)

    trades = get_distinct_trade_pnls(conn, strategy_id="D1")

    assert len(trades) == 1
    assert trades[0][0] == 42.0


def test_iron_condor_four_legs_counts_as_one_trade(conn):
    leg_pnls = [-48.0, -50.0, -55.0, -20.0]
    make_spread_trade(conn, "C10", leg_pnls, closed_at=BASE_TIME)

    trades = get_distinct_trade_pnls(conn, strategy_id="C10")

    assert len(trades) == 1
    assert trades[0][0] == sum(leg_pnls)  # combined PnL across all 4 legs


def test_two_iron_condors_eight_legs_counts_as_two_trades(conn):
    make_spread_trade(conn, "C10", [-48.0, -50.0, -55.0, -20.0], closed_at=BASE_TIME)
    make_spread_trade(conn, "C10", [30.0, 25.0, -10.0, -5.0], closed_at=BASE_TIME + timedelta(days=1))

    trades = get_distinct_trade_pnls(conn, strategy_id="C10")

    assert len(trades) == 2  # 8 outcome rows, 2 real trades


def test_losing_iron_condor_gives_one_consecutive_loss_not_four(conn):
    # 4 legs, every leg a loss — the exact shape that used to false-trigger
    # risk_agent.py's TRACK_DISABLED rule (CONSECUTIVE_LOSS_LIMIT = 4).
    make_spread_trade(conn, "C10", [-48.0, -50.0, -55.0, -20.0], closed_at=BASE_TIME)

    from risk_agent import get_consecutive_losses_by_track  # noqa: E402

    losses_by_track = get_consecutive_losses_by_track(conn)

    assert losses_by_track["C"] == 1  # one losing trade, not 4 losing legs
    assert losses_by_track["C"] < 4   # must NOT clear CONSECUTIVE_LOSS_LIMIT


def test_25_iron_condors_do_not_satisfy_a_100_trade_threshold(conn):
    for i in range(25):
        make_spread_trade(
            conn, "C5", [-10.0, 5.0, -3.0, 8.0],
            closed_at=BASE_TIME + timedelta(hours=i),
        )

    # Sanity: this really did write 100 raw outcome rows (25 trades x 4 legs).
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM nwt_trade_outcomes WHERE strategy_id = 'C5'")
        raw_row_count = cur.fetchone()[0]
    assert raw_row_count == 100

    from mutation_agent import MIN_TRADES_TO_OBSERVE, count_trade_outcomes  # noqa: E402

    real_trade_count = count_trade_outcomes(conn, "C5")

    assert real_trade_count == 25
    assert real_trade_count != raw_row_count
    # 25 real trades must not look like 100 to any trade-count gate. The raw
    # row count (100) would have wrongly cleared MIN_TRADES_TO_OBSERVE=30;
    # the real count (25) does not.
    assert real_trade_count < MIN_TRADES_TO_OBSERVE * 1  # 25 < 30, still short
    assert raw_row_count >= 100


# ---------------------------------------------------------------------------
# Supporting coverage
# ---------------------------------------------------------------------------

def test_count_distinct_trades_matches_get_distinct_trade_pnls(conn):
    make_single_leg_trade(conn, "D2", pnl=10.0, closed_at=BASE_TIME)
    make_spread_trade(conn, "D2", [-10.0, -10.0, -10.0], closed_at=BASE_TIME + timedelta(hours=1))

    assert count_distinct_trades(conn, strategy_id="D2") == 2


def test_strategy_prefix_filters_by_track(conn):
    make_single_leg_trade(conn, "C1", pnl=1.0, closed_at=BASE_TIME)
    make_single_leg_trade(conn, "C2", pnl=2.0, closed_at=BASE_TIME)
    make_single_leg_trade(conn, "D1", pnl=3.0, closed_at=BASE_TIME)

    c_trades = get_distinct_trade_pnls(conn, strategy_prefix="C")

    assert len(c_trades) == 2


def test_order_and_limit_for_consecutive_loss_style_queries(conn):
    # Most recent 4 trades, oldest to newest: win, win, loss, loss
    make_single_leg_trade(conn, "D3", pnl=10.0, closed_at=BASE_TIME)
    make_single_leg_trade(conn, "D3", pnl=10.0, closed_at=BASE_TIME + timedelta(hours=1))
    make_single_leg_trade(conn, "D3", pnl=-10.0, closed_at=BASE_TIME + timedelta(hours=2))
    make_single_leg_trade(conn, "D3", pnl=-10.0, closed_at=BASE_TIME + timedelta(hours=3))
    # A 5th, older losing trade must NOT be included once we ask for the last 4.
    make_single_leg_trade(conn, "D3", pnl=-999.0, closed_at=BASE_TIME - timedelta(days=1))

    last_4 = get_distinct_trade_pnls(conn, strategy_id="D3", order="DESC", limit=4)

    assert len(last_4) == 4
    assert -999.0 not in [pnl for pnl, _ in last_4]
    losses = sum(1 for pnl, _ in last_4 if pnl < 0)
    assert losses == 2


def test_archetype_grouping_also_collapses_legs(conn):
    make_spread_trade(conn, "C11", [-5.0, -5.0], closed_at=BASE_TIME, archetype="C-CONDOR-NEUTRAL")

    trades = get_distinct_trade_pnls(conn, archetype="C-CONDOR-NEUTRAL")

    assert len(trades) == 1
    assert trades[0][0] == -10.0


def test_legacy_row_with_no_position_id_is_its_own_trade(conn):
    # Rows written before position_id existed — each must still count as one
    # trade (the same fallback fetch_unprocessed_closed_positions relies on).
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nwt_trade_outcomes (strategy_id, symbol, pnl, closed_at, position_id) "
            "VALUES ('D4', 'LEGACY', -7.0, %s, NULL)",
            (BASE_TIME,),
        )
    conn.commit()

    trades = get_distinct_trade_pnls(conn, strategy_id="D4")

    assert len(trades) == 1
    assert trades[0][0] == -7.0
