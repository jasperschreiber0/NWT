import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from digest_trades import fetch_digest_trades

DAY = date(2026, 9, 9)


@pytest.fixture
def ledger(conn):
    with conn.cursor() as c:
        c.execute("""
            DROP TABLE IF EXISTS nwt_position_attribution;
            ALTER TABLE nwt_portfolio_ledger
              ADD COLUMN qty numeric, ADD COLUMN entry_price numeric,
              ADD COLUMN exit_price numeric, ADD COLUMN entry_time timestamptz,
              ADD COLUMN exit_time timestamptz, ADD COLUMN lifecycle_state text,
              ADD COLUMN entry_bid numeric, ADD COLUMN entry_ask numeric,
              ADD COLUMN exit_bid numeric, ADD COLUMN exit_ask numeric;
            ALTER TABLE nwt_trade_outcomes ADD COLUMN entry_time timestamptz;
            CREATE TABLE nwt_position_attribution (
                position_id uuid PRIMARY KEY REFERENCES nwt_portfolio_ledger,
                attribution_status text);
        """)
    return conn


def recovered(conn, status='verified_quantity_and_cost', exit_time='2026-09-09T15:10:02Z'):
    pid = str(uuid.uuid4())
    with conn.cursor() as c:
        c.execute("""INSERT INTO nwt_portfolio_ledger
            (position_id,bot_source,strategy_id,asset,asset_type,direction,status,
             lifecycle_state,qty,entry_price,exit_price,entry_time,exit_time,
             entry_bid,entry_ask,exit_bid,exit_ask)
            VALUES(%s,'RECON_RECOVERED','RECON_RECOVERY','FEZ','equity','short',
             'closed','CLOSED',396,71.516515,69.67,'2026-09-06T00:26:26Z',%s,
             71.50,71.54,69.66,69.68)""", (pid, exit_time))
        c.execute('INSERT INTO nwt_position_attribution VALUES(%s,%s)', (pid, status))
    return pid


def test_recovered_short_is_included_without_learning_write(ledger):
    recovered(ledger)
    trades = fetch_digest_trades(ledger, DAY)
    assert len(trades) == 1
    assert trades[0][0] == pytest.approx(722.30994)
    with ledger.cursor() as c:
        c.execute('SELECT COUNT(*) FROM nwt_trade_outcomes')
        assert c.fetchone()[0] == 0


def test_existing_outcome_wins_without_double_count(ledger):
    pid = recovered(ledger)
    with ledger.cursor() as c:
        c.execute("""INSERT INTO nwt_trade_outcomes(strategy_id,position_id,pnl,pnl_adjusted,closed_at)
                     VALUES('RECON_RECOVERY',%s,731.22,700,'2026-09-09T15:10:02Z')""", (pid,))
    trades = fetch_digest_trades(ledger, DAY)
    assert len(trades) == 1 and trades[0][0] == 700


def test_partial_provenance_and_qa_are_not_counted(ledger):
    recovered(ledger, 'verified_quantity_only')
    pid = recovered(ledger)
    with ledger.cursor() as c:
        c.execute("UPDATE nwt_portfolio_ledger SET strategy_id='QA_PAPER_LIFECYCLE' WHERE position_id=%s", (pid,))
    assert fetch_digest_trades(ledger, DAY) == []


def test_utc_day_has_exclusive_upper_boundary(ledger):
    recovered(ledger, exit_time='2026-09-10T00:00:00Z')
    recovered(ledger, exit_time='2026-09-08T23:59:59Z')
    assert fetch_digest_trades(ledger, DAY) == []


def test_legacy_outcome_is_not_counted_twice(ledger):
    recovered(ledger)
    with ledger.cursor() as c:
        c.execute("""INSERT INTO nwt_trade_outcomes
            (strategy_id,symbol,direction,entry_time,pnl,closed_at)
            VALUES('RECON_RECOVERY','FEZ','short','2026-09-06T00:26:26Z',700,'2026-09-09T15:10:02Z')""")
    assert len(fetch_digest_trades(ledger, DAY)) == 1


def test_spread_is_one_trade_on_last_leg_close_day(ledger):
    group = str(uuid.uuid4())
    with ledger.cursor() as c:
        for pnl, closed in [(20, '2026-09-08T23:59:59Z'), (-5, '2026-09-09T00:00:01Z')]:
            pid = str(uuid.uuid4())
            c.execute("""INSERT INTO nwt_portfolio_ledger
                (position_id,bot_source,asset,asset_type,status,spread_group_id)
                VALUES(%s,'NWT_TRACK_D','TEST','option','closed',%s)""", (pid, group))
            c.execute("""INSERT INTO nwt_trade_outcomes(strategy_id,position_id,pnl,closed_at)
                VALUES('D1',%s,%s,%s)""", (pid, pnl, closed))
    assert fetch_digest_trades(ledger, date(2026, 9, 8)) == []
    trades = fetch_digest_trades(ledger, DAY)
    assert len(trades) == 1 and trades[0][0] == 15
