import json,sqlite3
from datetime import datetime,timedelta,timezone
import pytest
from daily_comparison import update, target, POLICY


def inputs(n=220):
    dates=[];day=datetime(2025,9,1,tzinfo=timezone.utc)
    while len(dates)<n:
        if day.weekday()<5:dates.append(day.date().isoformat())
        day+=timedelta(days=1)
    cal=[dict(date=d,open='09:30',close='16:00') for d in dates]
    bars=[dict(t=d+'T04:00:00Z',o=100+i,c=100+i) for i,d in enumerate(dates)]
    return cal,{'SPY':bars,'QQQ':bars}


def evening(day):return datetime.fromisoformat(day+'T22:00:00+00:00')


def test_only_future_open_executes_and_retries_do_not_duplicate(tmp_path):
    cal,bars=inputs();first=evening(cal[205]['date'])
    a=update(tmp_path,bars,cal,first)
    assert all(p['sessions']==0 and p['equity']==1 and p['invested']==0 for p in a['portfolios'])
    b=update(tmp_path,bars,cal,evening(cal[206]['date']))
    assert all(p['sessions']==1 and p['invested']==1 for p in b['portfolios'])
    assert all(p['equity']==pytest.approx(.9985) for p in b['portfolios'])
    again=update(tmp_path,bars,cal,evening(cal[206]['date']))
    assert again['portfolios']==b['portfolios']


def test_future_prices_cannot_influence_frozen_target(tmp_path):
    cal,bars=inputs()
    for symbol in bars:
        bars[symbol]=[dict(b) for b in bars[symbol]]
        for b in bars[symbol][206:]:b['c']=.01
    a=update(tmp_path,bars,cal,evening(cal[205]['date']))
    assert all(p['pending']['target']==1 for p in a['portfolios'])


def test_missing_bar_does_not_invent_fill_or_erase_pending(tmp_path):
    cal,bars=inputs();update(tmp_path,bars,cal,evening(cal[205]['date']))
    missing={s:[b for b in rows if b['t'][:10]!=cal[206]['date']] for s,rows in bars.items()}
    result=update(tmp_path,missing,cal,evening(cal[207]['date']))
    assert result['status']=='DEGRADED'
    assert all(p['sessions']==0 and p['pending']['day']==cal[206]['date'] for p in result['portfolios'])


def test_breakout_holds_ten_sessions_then_exits_with_cost(tmp_path):
    cal,bars=inputs();update(tmp_path,bars,cal,evening(cal[205]['date']))
    for i in range(206,217): result=update(tmp_path,bars,cal,evening(cal[i]['date']))
    p=next(p for p in result['portfolios'] if p['symbol']=='SPY' and p['rule']=='breakout20_hold10')
    assert p['completed_trades']==1 and p['invested']==0
    assert p['equity']==pytest.approx((316/306)*.9985**2)


def test_mutating_frozen_policy_is_rejected(tmp_path,monkeypatch):
    cal,bars=inputs();update(tmp_path,bars,cal,evening(cal[205]['date']))
    monkeypatch.setitem(POLICY,'cost_per_side',0)
    with pytest.raises(RuntimeError,match='Frozen'):update(tmp_path,bars,cal,evening(cal[206]['date']))
