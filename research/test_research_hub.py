import json,sqlite3
from datetime import datetime,timedelta,timezone
from pathlib import Path
import pytest
from research_engine import SYMBOLS,backtest,weights,quote,spread_choice,spread_entry,spread_exit
from research_hub import archive,options_review,ai_review,forward_equities,assess


def dataset(n=400):
    days=[];day=datetime(2025,1,1,tzinfo=timezone.utc)
    while len(days)<n:
        if day.weekday()<5:days.append(day.date().isoformat())
        day+=timedelta(days=1)
    return {s:[dict(t=d+'T04:00:00Z',o=100.,c=100.) for d in days] for s in SYMBOLS}


def journal():
    c=sqlite3.connect(':memory:');c.execute('CREATE TABLE records(kind TEXT,key TEXT,observed TEXT,payload TEXT,PRIMARY KEY(kind,key))');return c


def test_passive_costs_and_cash_no_future_close_signal():
    data=dataset();start=data['SPY'][253]['t'][:10];end=data['SPY'][-1]['t'][:10]
    r=backtest(data,'passive_spy',start,end)
    assert r['total_return']==pytest.approx(.9985**2-1)
    assert backtest(data,'dual_trend200',start,end)['total_return']==0
    data['SPY'][-1]['c']=200;data['QQQ'][-1]['c']=200
    assert backtest(data,'dual_trend200',end,end)['total_return']==0


def test_data_gaps_and_duplicates_fail_closed():
    data=dataset();data['QQQ'].pop()
    with pytest.raises(ValueError,match='Unaligned'):backtest(data,'passive_spy','2026','2027')
    data=dataset();data['QQQ'].append(data['QQQ'][-1])
    with pytest.raises(ValueError,match='Duplicate'):backtest(data,'passive_spy','2026','2027')


def snapshot(bid,ask,now):return {'latestQuote':dict(bp=bid,ap=ask,bs=10,**{'as':10},t=now.isoformat())}


def test_spread_uses_executable_sides_fees_and_stale_quote_rejection():
    now=datetime(2026,9,15,19,tzinfo=timezone.utc)
    short='SPY261016P00740000';long='SPY261016P00735000'
    chain={short:snapshot(2,2.1,now),long:snapshot(1,1.1,now)}
    pair=spread_choice(chain,760,now);assert pair['short']==short
    entry=spread_entry(pair,chain,now)
    assert entry['credit']==pytest.approx(.88)
    exit_=spread_exit(pair,entry,chain,now)
    assert exit_['net_dollars']==pytest.approx(-26.6)
    assert quote(chain[short],now+timedelta(minutes=4)) is None
    assert quote(chain[short],now-timedelta(seconds=1)) is None
    assert not entry['small_account_2pct_risk_feasible']


def test_missing_exit_is_unresolved_not_a_profitable_expiry():
    c=journal();days=['2026-09-'+str(d) for d in range(15,23)]
    now=datetime(2026,9,15,19,tzinfo=timezone.utc)
    chain={'SPY261016P00740000':snapshot(2,2.1,now),'SPY261016P00735000':snapshot(1,1.1,now)}
    frames={}
    for i in range(2):
        t=now+timedelta(days=i)
        frames[(days[i],'SPY')]=dict(observed=t,spot=760,source='test',sha256='test',chain={s:snapshot(v['latestQuote']['bp'],v['latestQuote']['ap'],t) for s,v in chain.items()})
    result=options_review(c,frames,[dict(date=d) for d in days])
    assert result['completed']==[] and len(result['unresolved'])==1
    assert result['net_model_dollars']==0


def test_forward_targets_are_frozen_and_do_not_overlap():
    c=journal();data=dataset(400)
    cal=[dict(date=b['t'][:10],open='09:30',close='16:00') for b in data['SPY']]
    now=datetime.fromisoformat(cal[300]['date']+'T22:00:00+00:00')
    before={s:rows[:301] for s,rows in data.items()}
    forward_equities(c,before,cal,now)
    count=c.execute("SELECT count(*) FROM records WHERE kind='equity_target'").fetchone()[0]
    assert count==3
    forward_equities(c,before,cal,now+timedelta(minutes=1))
    assert c.execute("SELECT count(*) FROM records WHERE kind='equity_target'").fetchone()[0]==3


def test_ai_no_evidence_is_not_zero_performance(tmp_path):
    result=ai_review(tmp_path/'missing.sqlite')
    assert result['status']=='MISSING_EVENT_ARCHIVE' and result['pairs']==0


def test_weekly_job_install_keeps_existing_cron():
    from install_research_hub import schedule
    original='SHELL=/bin/bash\n0 9 * * * existing-job\n'
    text=schedule(original)
    assert original in text and schedule(text)==text


def test_failed_assessment_cannot_be_retested_into_success():
    c=journal()
    assert assess(c,'test',[('1',1)],3,3)['verdict']=='INSUFFICIENT_FRESH_EVIDENCE'
    bad=[(str(i),-.01) for i in range(3)]
    first=assess(c,'test',bad,3,3)
    assert first['verdict']=='RETIRED_FROM_SHORTLIST'
    assert assess(c,'test',[(str(i),1) for i in range(5)],3,3)==first
