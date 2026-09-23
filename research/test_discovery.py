import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from universe import paged, SYMBOLS, OPTION_UNDERLYINGS
from pattern_lab import evaluate, ingest, initialize


def bars(n=31, start=None):
    start = start or datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
    return [dict(t=(start+timedelta(minutes=i)).strftime('%Y-%m-%dT%H:%M:%SZ'), o=100, h=101, l=99, c=100, v=100) for i in range(n)]


def test_universe_and_pagination_include_later_symbols():
    assert len(SYMBOLS) == len(set(SYMBOLS)) == 41
    assert len(OPTION_UNDERLYINGS) == 12
    calls = []
    def get(url, params):
        calls.append(params)
        return {'bars': {'QQQ': [2]}, 'next_page_token': None} if params.get('page_token') else {'bars': {'AAPL': [1]}, 'next_page_token': 'next'}
    assert paged(get, 'unused', {'feed': 'sip'}, 'bars')['bars'] == {'AAPL': [1], 'QQQ': [2]}
    assert calls[1]['feed'] == 'sip'


def test_partial_or_looping_data_rejected():
    with pytest.raises(RuntimeError): paged(lambda *_: {'error': 'denied'}, '', {}, 'bars')
    with pytest.raises(RuntimeError): paged(lambda *_: {'bars': {}, 'next_page_token': 'same'}, '', {}, 'bars')


def test_patterns_require_contiguous_history_and_aligned_benchmark():
    data = bars(); data[-1].update(c=102,h=102,v=300)
    result = evaluate(data, [])
    assert result['volume_breakout_base'] == 1
    assert result['relative_strength_base'] is None
    assert len(result) == 8
    assert evaluate(data[:10]+data[11:], []) == {}


def test_seed_history_does_not_create_backdated_predictions():
    c=sqlite3.connect(':memory:')
    ingest(c, {'SPY': bars()}, datetime(2026,9,15,18,tzinfo=timezone.utc))
    assert c.execute('SELECT COUNT(*) FROM bars').fetchone()[0] == 31
    assert c.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == 0


def test_prospective_entry_no_lookahead_cost_stress_and_idempotency():
    c=sqlite3.connect(':memory:'); initialize(c)
    c.execute('INSERT INTO observations VALUES (?,?,?,?,?)', ('SPY','2026-09-15T14:30:00Z','test',1,'2026-09-15T14:31:10+00:00'))
    data=bars(31, datetime(2026,9,15,14,32,tzinfo=timezone.utc)); data[-1]['o']=101
    now=datetime(2026,9,15,15,4,tzinfo=timezone.utc)
    ingest(c, {'SPY':data}, now); ingest(c, {'SPY':data}, now)
    rows=c.execute('SELECT entry_t,exit_t,gross,net_10bps,net_30bps FROM outcomes').fetchall()
    assert len(rows)==1
    assert rows[0][:2]==('2026-09-15T14:32:00Z','2026-09-15T15:02:00Z')
    assert rows[0][2:]==pytest.approx((.01,.009,.007))


def test_missing_exit_bar_does_not_generate_an_outcome():
    c=sqlite3.connect(':memory:'); initialize(c)
    c.execute('INSERT INTO observations VALUES (?,?,?,?,?)', ('SPY','2026-09-15T14:30:00Z','test',1,'2026-09-15T14:31:10+00:00'))
    ingest(c, {'SPY':bars(30,datetime(2026,9,15,14,32,tzinfo=timezone.utc))}, datetime(2026,9,15,15,5,tzinfo=timezone.utc))
    assert c.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0]==0



def test_experiment_spreads_reject_stale_and_crossed_quotes():
    from experiment_lab import spread_fraction
    now=datetime(2026,9,24,14,30,tzinfo=timezone.utc)
    assert spread_fraction({'latestQuote':{'bp':100,'ap':100.02,'t':now.isoformat()}},now)==pytest.approx(.02/100.01)
    assert spread_fraction({'latestQuote':{'bp':100,'ap':99,'t':now.isoformat()}},now) is None
    assert spread_fraction({'latestQuote':{'bp':100,'ap':101,'t':'2026-09-24T12:00:00Z'}},now) is None


def test_experiments_freeze_policy_and_start_without_historical_predictions():
    import experiment_lab as lab
    c=sqlite3.connect(':memory:');initialize(c);lab.initialize(c)
    report=lab.run(c,{},datetime(2026,9,24,14,30,tzinfo=timezone.utc))
    assert report['sessions']==0 and not report['execution_enabled']
    c.execute("UPDATE experiment_versions SET policy='changed'")
    with pytest.raises(ValueError,match='Frozen'):lab.initialize(c)


def test_distinct_experiment_hypotheses_require_context():
    from experiment_lab import hypotheses
    data=bars(31,datetime(2026,9,24,13,30,tzinfo=timezone.utc));data[-1]['c']=101
    snap={'prevDailyBar':{'c':99},'dailyBar':{'o':100,'t':'2026-09-24T04:00:00Z'}}
    result=hypotheses(data,bars(31,datetime(2026,9,24,13,30,tzinfo=timezone.utc)),snap,[{'c':97},{'c':98},{'c':100}])
    assert result['opening_gap_follow'][0]==1
    assert result['sector_relative_strength'][0]==1
    assert result['three_day_trend_confirmation'][0]==1
    assert hypotheses(data,{}, {},{}) .get('sector_relative_strength') is None


def test_research_review_cannot_promote_one_good_observation():
    import experiment_lab as lab
    c=sqlite3.connect(':memory:');lab.initialize(c)
    c.execute("INSERT INTO experiment_observations(version,rule,symbol,t,observed_at,direction,horizon,regime,spread,gross,net,stressed,benchmark,entry_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(lab.VERSION,'test','SPY','2026-09-24T14:00:00Z','2026-09-24T14:01:00Z',1,30,'trend',.001,.1,.09,.08,0,100))
    r=lab.review(c,'2026-09-24')
    assert r['rules'][0]['state']=='DISCOVERY'
    assert r['rules'][0]['validation_observations']==0



def test_new_experiment_scores_only_future_complete_horizon():
    import experiment_lab as lab
    import json
    c=sqlite3.connect(':memory:');initialize(c)
    now=datetime(2026,9,24,14,1,10,tzinfo=timezone.utc)
    for symbol in ['AAPL','XLK','SPY']:
        data=bars(31,datetime(2026,9,24,13,30,tzinfo=timezone.utc))
        if symbol=='AAPL':data[-1]['c']=101
        for b in data:c.execute('INSERT INTO bars VALUES (?,?,?)',(symbol,b['t'],json.dumps(b)))
    snaps={s:{'latestQuote':{'bp':100,'ap':100.02,'t':now.isoformat()}} for s in ['AAPL','XLK','SPY']}
    lab.run(c,snaps,now)
    assert c.execute("SELECT COUNT(*) FROM experiment_observations WHERE rule='sector_relative_strength'").fetchone()[0]==1
    assert c.execute('SELECT COUNT(*) FROM experiment_observations WHERE gross IS NOT NULL').fetchone()[0]==0
    for symbol in ['AAPL','SPY']:
        data=bars(121,datetime(2026,9,24,14,2,tzinfo=timezone.utc))
        if symbol=='AAPL':data[-1]['o']=102
        for b in data:c.execute('INSERT OR IGNORE INTO bars VALUES (?,?,?)',(symbol,b['t'],json.dumps(b)))
    later=datetime(2026,9,24,16,4,tzinfo=timezone.utc)
    lab.run(c,snaps,later);lab.run(c,snaps,later)
    row=c.execute("SELECT entry_t,exit_t,gross,stressed FROM experiment_observations WHERE rule='sector_relative_strength'").fetchone()
    assert row[:2]==('2026-09-24T14:02:00Z','2026-09-24T16:02:00Z')
    assert row[2:]==pytest.approx((.02,.017))



def test_small_account_distinguishes_debit_spread_and_naked_call():
    from small_account import assess
    long=dict(asset_type='option',asset='SPY260925C00759000',direction='long',qty=8,entry_price=2)
    short=dict(long,asset='SPY260925C00760000',direction='short',entry_price=1.5)
    result=assess([long,short])
    assert result['minimum_structure_expiry_loss']==50 and result['eligible']
    assert not assess([short])['eligible']
    assert not assess([dict(long,entry_price=10)])['eligible']
