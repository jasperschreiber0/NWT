import json
import os
import sqlite3
import sys
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import psycopg2
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import paper_bridge as bridge
from rehearsal import size,update
from unified_performance import cash_adjustment,expenses,trace
from autonomous_recovery import candidates,SAFE
from install_autonomous_paper import schedule,configuration


def fixture_evidence(now):
    experiment='momentum-20-none';cohort=dict(entry=now.date().isoformat(),exit=(now+timedelta(days=7)).date().isoformat(),
        entry_at=(now-timedelta(minutes=5)).isoformat(),frozen_at=(now-timedelta(hours=10)).isoformat(),
        allocation={experiment:.2},targets={experiment:{'XLK':.5}},data_sha256='frozen-data')
    report=dict(allocation={experiment:.2},hypotheses={experiment:{'verdict':{'state':'PAPER_QUALIFIED'},'spec':{}}})
    verdicts={experiment:dict(state='PAPER_QUALIFIED')}
    payload=dict(bot_source='RESEARCH_LAB',strategy_id=bridge.identity('core:'+experiment),asset_type='equity',direction='long',
        symbol='XLK',sized_notional=500.,time_in_force='day',stop_pct=.03,
        research=dict(study='core',experiment=experiment,entry=cohort['entry'],exit=cohort['exit'],policy=bridge.POLICY['version'],source_hash='frozen-data'))
    return report,cohort,verdicts,payload


def test_independent_gate_requires_paper_qualification_trial_and_budget(tmp_path,monkeypatch):
    now=datetime.now(timezone.utc);report,cohort,verdicts,payload=fixture_evidence(now)
    monkeypatch.setattr(bridge,'STATE',tmp_path)
    (tmp_path/'trial.json').write_text(json.dumps({'status':'PASSED'}))
    monkeypatch.setattr(bridge,'evidence',lambda *a:(report,[cohort],verdicts,set()))
    monkeypatch.setattr(bridge,'ledger_rows',lambda *a:[])
    monkeypatch.setattr(bridge,'exposure',lambda *a:{})
    monkeypatch.setattr(bridge,'symbol_in_use',lambda *a:False)
    assert bridge.gate(None,payload,'https://paper-api.alpaca.markets',now)
    with pytest.raises(ValueError,match='paper endpoint'):bridge.gate(None,payload,'https://api.alpaca.markets',now)
    monkeypatch.setattr(bridge,'exposure',lambda *a:{'another':2800})
    with pytest.raises(ValueError,match='aggregate'):bridge.gate(None,payload,'https://paper-api.alpaca.markets',now)
    (tmp_path/'trial.json').write_text(json.dumps({'status':'RUNNING'}))
    with pytest.raises(ValueError,match='trial'):bridge.gate(None,payload,'https://paper-api.alpaca.markets',now)


def test_demotion_and_late_entry_cannot_be_promoted():
    now=datetime.now(timezone.utc);r,c,v,p=fixture_evidence(now);key=p['research']['experiment']
    assert bridge.eligible(key,c,r,v,set(),now)
    assert not bridge.eligible(key,c,r,v,{key},now)
    assert not bridge.eligible(key,c,r,v,set(),now+timedelta(hours=1))


def test_quote_cash_limit_whole_units_and_staleness():
    now=datetime.now(timezone.utc);q=dict(bp=49.99,ap=50,bs=10,**{'as':10},t=now.isoformat())
    qty,limit=bridge.quote_order(q,500,now)
    assert qty==9 and qty*limit<=500
    with pytest.raises(ValueError):bridge.quote_order(q,500,now+timedelta(minutes=2))
    with pytest.raises(ValueError):bridge.quote_order(q,49,now)
    with pytest.raises(ValueError):bridge.quote_order(dict(q,bp=51),500,now)


def test_actual_outcome_feedback_reduces_and_stops_allocation():
    rows=[dict(status='closed',pnl_adjusted=-10,exit_time=i) for i in range(5)]
    assert bridge.feedback(rows)['scale']==.5
    assert bridge.feedback(rows*2)['scale']==0
    assert bridge.feedback([dict(status='closed',pnl_adjusted=None)])['scale']==0


def test_rehearsal_whole_shares_and_cash_constraints():
    result=size({'SPY':.1,'XLK':.1},{'SPY':760,'XLK':100},5000,5000)
    assert 'SPY' not in result and result['XLK']==4
    assert size({'XLK':.1},{'XLK':100},50,5000)=={}


def test_costs_and_cash_transfers_are_not_trading_profit():
    assert expenses({},30)['accrued_usd'] is None
    config={s:{'monthly_usd':10} for s in ['alpaca','hetzner','railway','supabase']}
    assert expenses(config,365.25/12)['accrued_usd']==pytest.approx(40)
    flows=cash_adjustment([dict(activity_type='CSD',net_amount='5000',id='1'),dict(activity_type='CSW',net_amount='-1000',id='2'),
        dict(activity_type='FEE',net_amount='-1',id='3'),dict(activity_type='JNLC',net_amount='10',id='4')])
    assert flows['net_external_cash']==4000 and flows['ambiguous_transfer_ids']==['4']


def test_safe_recovery_never_replays_orders_and_limits_retries():
    now=datetime.now(timezone.utc);jobs=configuration({})
    latest={'unified-performance':dict(status='failed',started=now.timestamp())}
    selected,blocked=candidates(jobs,latest,[],now,False)
    assert selected==['unified-performance'] and 'engine' not in SAFE and 'paper-bridge' not in SAFE
    attempts=[dict(job='unified-performance',started=now.timestamp())]*2
    assert candidates(jobs,latest,attempts,now,False)[1]
    jobs['unified-performance']['args']=['execution/engine.py']
    assert candidates(jobs,latest,[],now,False)[0]==[]


def test_install_is_idempotent_and_preserves_existing_schedule():
    original='SHELL=/bin/bash\n0 9 * * * original\n'
    updated=schedule(original)
    assert original in updated and schedule(updated)==updated


@pytest.fixture
def db():
    conn=psycopg2.connect(os.environ['NWT_TEST_DB_DSN'])
    with conn.cursor() as q:
        q.execute('CREATE TEMP TABLE nwt_tickets(ticket_id uuid PRIMARY KEY,from_agent text,to_agent text,type text,payload jsonb,created_at timestamptz DEFAULT now())')
        q.execute('CREATE TEMP TABLE nwt_decision_inputs(id uuid PRIMARY KEY,run_date date,symbol text,strategy_id text,track text,ticket_id uuid,decision text,layer0_signals jsonb)')
    conn.commit();yield conn;conn.close()


def test_proposal_retry_is_idempotent_and_links_raw_decision(db,monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'nwt_agents'))
    import opportunity_outcomes
    called=[];monkeypatch.setattr(opportunity_outcomes,'record_decision_outcome',lambda *a,**k:called.append(a[1]))
    now=datetime.now(timezone.utc);r,c,v,p=fixture_evidence(now)
    identifier=bridge.ticket_id('core:'+p['research']['experiment'],c['entry'],'XLK')
    assert bridge.insert_ticket(db,identifier,'TRADE_REQUEST',p)
    assert not bridge.insert_ticket(db,identifier,'TRADE_REQUEST',p)
    with db.cursor() as q:
        q.execute('SELECT count(*) FROM nwt_tickets');assert q.fetchone()[0]==1
        q.execute('SELECT count(*) FROM nwt_decision_inputs');assert q.fetchone()[0]==1
    assert len(called)==1


def test_raw_record_failure_rolls_back_ticket_and_decision(db,monkeypatch):
    import opportunity_outcomes
    def fail(*a,**k):raise RuntimeError('raw unavailable')
    monkeypatch.setattr(opportunity_outcomes,'record_decision_outcome',fail)
    now=datetime.now(timezone.utc);r,c,v,p=fixture_evidence(now)
    with pytest.raises(RuntimeError):bridge.insert_ticket(db,bridge.ticket_id('x',c['entry'],'XLK'),'TRADE_REQUEST',p)
    db.rollback()
    with db.cursor() as q:
        q.execute('SELECT count(*) FROM nwt_tickets');assert q.fetchone()[0]==0


def test_stock_study_does_not_mutate_frozen_core():
    import strategy_lab
    from broader_research import study,BASKETS
    original=json.dumps(strategy_lab.POLICY,sort_keys=True)
    m=study('stocks')
    assert m.SYMBOLS==BASKETS['stocks'] and len(m.catalog())==36
    assert json.dumps(strategy_lab.POLICY,sort_keys=True)==original


def test_rehearsal_executes_whole_units_and_preserves_settlement(tmp_path):
    lab=tmp_path/'lab';lab.mkdir();source=sqlite3.connect(lab/'lab.sqlite')
    source.execute('CREATE TABLE records(kind TEXT,key TEXT,payload TEXT,PRIMARY KEY(kind,key))')
    dates=['2026-10-02','2026-10-05','2026-10-06','2026-10-07','2026-10-08','2026-10-09','2026-10-12','2026-10-13']
    cal=[dict(date=d,open='09:30',close='16:00') for d in dates]
    cohort=dict(entry=dates[1],exit=dates[6],entry_at=dates[1]+'T13:30:00+00:00',portfolio_weights={'XLK':.6})
    source.execute('INSERT INTO records VALUES (?,?,?)',('cohort',dates[1],json.dumps(cohort)));source.commit();source.close()
    data={'XLK':[dict(t=d+'T04:00:00Z',o=110 if i==6 else 100,c=110 if i==6 else 100) for i,d in enumerate(dates)]}
    folder=tmp_path/'small'
    first=update(folder,lab,{'XLK':data['XLK'][:1]},cal,datetime(2026,10,2,22,tzinfo=timezone.utc))
    assert first['prepared']==1 and first['equity']==5000
    second=update(folder,lab,{'XLK':data['XLK'][:7]},cal,datetime(2026,10,12,22,tzinfo=timezone.utc))
    assert second['completed_cohorts']==1 and second['net']==pytest.approx(38.74)
    assert update(folder,lab,{'XLK':data['XLK'][:7]},cal,datetime(2026,10,12,22,tzinfo=timezone.utc))==second
    c=sqlite3.connect(folder/'rehearsal.sqlite');r=json.loads(c.execute("SELECT payload FROM records WHERE kind='roundtrip'").fetchone()[0])
    assert r['settled_after']=='2026-10-13' and r['quantities']=={'XLK':4};c.close()
    late=update(tmp_path/'late',lab,{'XLK':data['XLK'][:7]},cal,datetime(2026,10,12,22,tzinfo=timezone.utc))
    assert late['completed_cohorts']==0 and late['missed']
