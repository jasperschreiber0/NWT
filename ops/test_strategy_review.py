import os,json
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock
import psycopg2
import pytest
import strategy_review as s


def trade(i,net=-10,**changes):
    t=datetime(2026,9,16,tzinfo=timezone.utc)+timedelta(days=i)
    return dict(position_id=str(i),spread_group_id=None,strategy_id='EU-MR-001',status='closed',
        asset='VGK',asset_type='equity',direction='long',qty=10,entry_price=100,exit_price=99,
        entry_time=t,exit_time=t+timedelta(hours=5),pnl=-10,pnl_adjusted=net,exit_reason='stop',**changes)


def test_five_losses_restrict_twenty_retire_and_open_groups_do_not_count():
    assert s.review([trade(i) for i in range(4)])['strategies'][0]['state']=='UNCHANGED_UNPROVEN'
    assert s.review([trade(i) for i in range(5)])['strategies'][0]['state']=='LIMITED_EXPERIMENT'
    # Older entry dates avoid the independent new-cohort $100 loss limit.
    rows=[trade(i) for i in range(20)]
    assert s.review(rows)['strategies'][0]['state']=='SHADOW_ONLY'
    rows[-1]['status']='open'
    assert s.review(rows)['strategies'][0]['completed_groups']==19


def test_cost_loss_and_accounting_error_are_distinguished():
    a=trade(0);a.update(pnl=1,pnl_adjusted=-2,exit_price=100.1)
    r=s.review([a]);assert 'MODELED_COSTS_TURNED_GAIN_INTO_LOSS' in r['diagnoses'][0]['flags']
    a['pnl']=100
    assert s.review([a])['strategies'][0]['state']=='ACCOUNTING_HOLD'
    a['pnl']=1.08
    assert s.review([a])['strategies'][0]['state']=='UNCHANGED_UNPROVEN'
    assert 'MINOR_ACCOUNTING_DIFFERENCE; RETAIN_FOR_REVIEW' in s.review([a])['diagnoses'][0]['flags']


def test_new_cohort_loss_budget_stops_new_entries():
    a=trade(0,net=-101);a['entry_time']=datetime(2026,10,5,tzinfo=timezone.utc);a['exit_time']=a['entry_time']+timedelta(hours=1)
    assert s.review([a])['strategies'][0]['state']=='SHADOW_ONLY'


@pytest.fixture
def db():
    c=psycopg2.connect(os.environ['NWT_TEST_DB_DSN'])
    with c.cursor() as q:
        q.execute('CREATE TEMP TABLE nwt_system_log(id uuid DEFAULT gen_random_uuid(),level text,component text,message text,payload jsonb,created_at timestamptz DEFAULT clock_timestamp())')
        q.execute('CREATE TEMP TABLE nwt_portfolio_ledger(strategy_id text,status text,qty numeric,entry_price numeric,asset_type text)')
    c.commit();yield c;c.close()


def test_latched_restrictions_do_not_auto_restore_on_a_winner(db):
    a=dict(strategy='EU',state='LIMITED_EXPERIMENT',reason='losses')
    s.latch(db,[a])
    b=s.latch(db,[dict(strategy='EU',state='UNCHANGED_UNPROVEN',reason='recent improvement')])[0]
    assert b['state']=='LIMITED_EXPERIMENT' and b['restriction_latched']
    with db.cursor() as q:
        q.execute('SELECT count(*) FROM nwt_system_log');assert q.fetchone()[0]==1


def test_gate_counts_existing_positions_and_pending_orders(db,monkeypatch):
    monkeypatch.setattr(s,'load_rows',lambda *a:[trade(i) for i in range(5)])
    p=dict(strategy_id='EU-MR-001',asset_type='equity',direction='long',sized_notional=400)
    assert s.entry_gate(db,p,lambda:[]) is None and p['_experiment_budget']==400
    assert 'pending' in s.entry_gate(db,p,lambda:[{}])
    with db.cursor() as q:q.execute("INSERT INTO nwt_portfolio_ledger VALUES ('EU-MR-001','open',7,100,'equity')")
    db.commit()
    assert '$1000' in s.entry_gate(db,p,lambda:[])
    p['sized_notional']=501
    assert '$500' in s.entry_gate(db,p,lambda:[])
    p['sized_notional']=float('nan')
    assert '$500' in s.entry_gate(db,p,lambda:[])
