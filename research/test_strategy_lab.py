import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import strategy_lab as lab
from research_engine import SYMBOLS


def data_set(n=280):
    days=[]; day=datetime(2025,1,1,tzinfo=timezone.utc)
    while len(days)<n:
        if day.weekday()<5: days.append(day.date().isoformat())
        day+=timedelta(days=1)
    return {s:[dict(t=d+'T05:00:00Z',o=100+i*.1,c=100+i*.1) for i,d in enumerate(days)] for s in SYMBOLS}


def trial(net=.01, excess=.01, i=0):
    return dict(entry=f'{i:03}',net=net,excess=excess,stress_net=net-.001,gross=1.,drawdown=0.,path=[net])


def test_catalog_is_bounded_unique_and_has_ablation_parents():
    specs=lab.catalog(); keys={s['id'] for s in specs}
    assert len(specs)==len(keys)==36
    assert all(p in keys for s in specs for p in s['parents'])


def test_signal_uses_only_supplied_history_and_filters_risk():
    specs={s['id']:s for s in lab.catalog()}
    h={s:[100+i*.1 for i in range(260)] for s in SYMBOLS}
    w=lab.targets(specs['momentum-20-none'],h,specs)
    assert sum(w.values())==1 and len(w)==2
    assert lab.targets(specs['pullback-3-none'],h,specs)=={}
    h['SPY'][-1]=1
    assert lab.targets(specs['momentum-20-trend200'],h,specs)=={}
    assert lab.targets(specs['momentum-20-calm25'],h,specs)=={}


def test_blend_cannot_create_leverage():
    specs={s['id']:s for s in lab.catalog()}
    blend=dict(family='combination',parents=['momentum-20-none','momentum-60-none'])
    h={s:[100+i*.1 for i in range(260)] for s in SYMBOLS}
    assert sum(lab.targets(blend,h,specs).values())==1


def test_cost_stress_and_intracohort_drawdown_are_recorded():
    data=data_set(); days,index=lab.validate(data)
    index['SPY'][days[-3]]['c']=50
    r=lab.outcome({'SPY':1},index,days[-6],days[-1],days)
    assert r['stress_net']<r['net'] and r['excess']==pytest.approx(0)
    assert r['drawdown']>.5
    assert lab.summary([r])['drawdown']>.5


def test_bad_data_fails_closed():
    data=data_set(); data['QQQ'].pop()
    with pytest.raises(ValueError,match='unaligned'): lab.validate(data)
    data=data_set(); data['SPY'][0]['o']=float('nan')
    with pytest.raises(ValueError,match='Invalid'): lab.validate(data)


def test_flat_price_roundtrip_pays_fees_without_borrowing():
    data=data_set(); days,index=lab.validate(data)
    index['SPY'][days[-1]]['o']=index['SPY'][days[-6]]['o']
    r=lab.outcome({'SPY':1},index,days[-6],days[-1],days)
    cost=lab.POLICY['cost_per_side']
    assert r['net']==pytest.approx((1-cost)/(1+cost)-1)
    assert lab.outcome({},index,days[-6],days[-1],days)['net']==0


def test_no_promotion_from_small_sample_or_failed_parent_comparison():
    rows=[trial(i=i) for i in range(12)]
    assert lab.evaluate(rows[:11],{})['state']=='COLLECTING'
    assert lab.evaluate(rows,{})['state']=='PAPER_QUALIFIED'
    assert lab.evaluate(rows,{'parent':rows})['state']=='RETIRED'
    assert lab.evaluate([trial(net=-.01,i=i) for i in range(12)],{})['state']=='RETIRED'


def test_correlated_models_do_not_multiply_allocation():
    rows=[trial(net=.01+i*.001,i=i) for i in range(12)]
    verdict=lab.evaluate(rows,{})
    specs={'a':{'family':'momentum'},'b':{'family':'breakout'}}
    allocation=lab.select_portfolio({'a':verdict,'b':verdict},specs,{'a':rows,'b':rows})
    assert len(allocation)==1 and sum(allocation.values())==.2


def test_freeze_rejects_changed_results():
    c=sqlite3.connect(':memory:'); c.execute('CREATE TABLE records(kind TEXT,key TEXT,payload TEXT,PRIMARY KEY(kind,key))')
    lab.put(c,'result','a',{'net':-1})
    with pytest.raises(ValueError,match='Frozen'): lab.put(c,'result','a',{'net':1})


def test_promotion_retirement_and_demotion_are_persistent():
    c=sqlite3.connect(':memory:');c.execute('CREATE TABLE records(kind TEXT,key TEXT,payload TEXT,PRIMARY KEY(kind,key))')
    now=datetime.now(timezone.utc)
    good=[trial(i=i) for i in range(12)]; bad=[trial(net=-.02,excess=-.02,i=i+12) for i in range(3)]
    assert lab.adjudicate(c,'a',good,{},now)['state']=='PAPER_QUALIFIED'
    assert lab.adjudicate(c,'a',good+bad,{},now)['state']=='DEMOTED'
    assert lab.adjudicate(c,'a',good,{},now)['state']=='DEMOTED'
    failed=[trial(net=-.01,excess=-.01,i=i) for i in range(12)]
    assert lab.adjudicate(c,'b',failed,{},now)['state']=='RETIRED'
    assert lab.adjudicate(c,'b',good,{},now)['state']=='RETIRED'


def test_allocation_shrinks_when_fresh_performance_deteriorates():
    original=[trial(i=i) for i in range(12)]
    weaker=[trial(net=.004,excess=.004,i=i+12) for i in range(12)]
    v=lab.evaluate(original,{})
    allocation=lab.select_portfolio({'a':v},{'a':{'family':'momentum'}},{'a':original+weaker})
    assert 0<allocation['a']<.2


def test_lifecycle_idempotency_future_entry_and_frozen_outcomes(tmp_path,monkeypatch):
    # Small calendar fixture skips expensive historical windows, preserves real signal/outcome logic.
    data=data_set(310); cal=[dict(date=b['t'][:10],open='09:30',close='16:00') for b in data['SPY']]
    def now(i): return datetime.fromisoformat(cal[i]['date']+'T23:00:00+00:00')
    before={s:rows[:281] for s,rows in data.items()}
    first=lab.run(tmp_path,before,cal,now(280))
    assert first['experiment_count']==42 and first['pending_cohorts']==1 and first['completed_cohorts']==0
    assert lab.run(tmp_path,before,cal,now(280))==first
    c=sqlite3.connect(tmp_path/'lab.sqlite')
    cohort=lab.records(c,'cohort')[0]
    assert cohort['entry']==cal[281]['date'] and cohort['allocation']=={}
    after={s:rows[:287] for s,rows in data.items()}
    second=lab.run(tmp_path,after,cal,now(286))
    assert second['completed_cohorts']==1 and second['pending_cohorts']==1
    frozen=lab.records(c,'cohort_result')
    after['SPY'][281]['o']=50
    lab.run(tmp_path,after,cal,now(286))
    assert lab.records(c,'cohort_result')==frozen
    assert second['virtual_equity']==100000
    c.close()


def test_late_registration_is_not_backfilled(tmp_path):
    data=data_set(290); cal=[dict(date=b['t'][:10],open='09:30',close='16:00') for b in data['SPY']]
    now=datetime.fromisoformat(cal[280]['date']+'T23:00:00+00:00')
    before={s:r[:281] for s,r in data.items()};lab.run(tmp_path,before,cal,now)
    c=sqlite3.connect(tmp_path/'lab.sqlite');cohort=lab.records(c,'cohort')[0]
    cohort['frozen_at']=cohort['entry_at']
    c.execute("UPDATE records SET payload=? WHERE kind='cohort'",(json.dumps(cohort),));c.commit();c.close()
    with pytest.raises(ValueError,match='Late'): lab.run(tmp_path,before,cal,now)
