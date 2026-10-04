import io,sys,zipfile,json,sqlite3
from pathlib import Path
from datetime import datetime,timezone,timedelta
import pytest
sys.path.insert(0,str(Path(__file__).parent))
import data_atlas as atlas
import theory_lab as theory
import option_theories as options

def zipped(text):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:z.writestr('factors.csv',text)
    return b.getvalue()

def test_factor_units_missing_and_footer():
    rows=atlas.factors(zipped('Notes\n,Mkt-RF,RF\n19260701,1.25,0.01\n19260702,-99.99,0.01\n19260706,-2,0.02\nAnnual returns\n1926,12,2'))
    assert len(rows)==2 and rows[0]['Mkt-RF']==.0125 and rows[-1]['RF']==.0002
    assert atlas.factor_summary(rows)['first']=='1926-07-01'

def test_reject_duplicate_and_nonfinite_public_data():
    for text in [',Mom\n19260701,1\n19260701,2',',Mom\n19260701,nan']:
        with pytest.raises(ValueError):atlas.factors(zipped(text))

def test_next_open_costs_and_missing_price():
    days=['2026-10-'+str(n).zfill(2) for n in range(1,7)]
    indexed={s:{d:dict(o=100,c=100) for d in days} for s in ['AAPL','SPY']}
    r=theory.sample(indexed,'AAPL',days,0,True)
    assert r['net']==pytest.approx(.5*(.9985/1.0015-1))
    assert r['stress_net']<r['net']<0
    del indexed['AAPL'][days[3]]
    assert theory.sample(indexed,'AAPL',days,0,True) is None

def test_rules_need_history_and_compare_known_conditions():
    assert not theory.active('passive',[100.]*252)
    assert theory.active('passive',[100.]*253)
    prices=[100+i*.2 for i in range(253)]
    assert theory.active('quiet_momentum',prices)
    assert not theory.active('trend_pullback',prices)
    prices[-1]=prices[-6]-.1
    assert theory.active('trend_pullback',prices)

def test_option_credit_debit_cashflows_and_budget():
    now=datetime(2026,10,5,tzinfo=timezone.utc)
    def q(b,a):return {'latestQuote':dict(bp=b,ap=a,bs=10,**{'as':10},t=now.isoformat())}
    chain={'short':q(.8,.85),'long':q(.2,.25)}
    pair=dict(short='short',long='long',width=1,credit=True)
    entry=options.value(pair,chain,now)
    exit_=options.value(pair,chain,now,False)
    assert entry['max_loss_with_assumed_fees']==pytest.approx(49.60)
    assert (entry['cashflow']+exit_['cashflow'])*100-2.60==pytest.approx(-16.60)
    assert options.value(dict(pair,width=2),chain,now) is None
    debit=dict(short='long',long='short',width=1,credit=False)
    assert options.value(debit,chain,now)['max_loss_with_assumed_fees']==pytest.approx(69.60)
    assert options.value(pair,chain,now+timedelta(minutes=5)) is None

def test_short_listing_excluded_and_protocol_frozen(tmp_path,monkeypatch):
    monkeypatch.setattr(theory,'SYMBOLS',['SPY','NEW'])
    days=[]; d=datetime(2025,1,1,tzinfo=timezone.utc)
    while len(days)<270:
        if d.weekday()<5:days.append(d.date().isoformat())
        d+=timedelta(days=1)
    now=datetime.fromisoformat(days[-10]+'T23:00:00+00:00')
    data={s:[dict(t=x+'T00:00:00Z',o=100+i,c=100+i) for i,x in enumerate(days[:-9]) if s=='SPY' or i>200] for s in theory.SYMBOLS}
    cal=[dict(date=x,open='09:30',close='16:00') for x in days[-20:]]
    report=theory.run(tmp_path,data,cal,now,{'public':{}})
    assert report['eligible_symbols']==1 and report['pending_cohorts']==1
    with sqlite3.connect(tmp_path/'lab.sqlite') as c:
        cohort=json.loads(c.execute("SELECT payload FROM records WHERE kind='cohort'").fetchone()[0])
        assert all(k.startswith('SPY:') for k in cohort['eligible'])
        assert all(not v for k,v in cohort['targets'].items() if k.startswith('NEW:'))
    again=theory.run(tmp_path,data,cal,now,{'public':{}})
    assert again['pending_cohorts']==1
    later=datetime.fromisoformat(days[-3]+'T23:00:00+00:00')
    extended={s:[dict(t=x+'T00:00:00Z',o=100+i,c=100+i) for i,x in enumerate(days[:-2]) if s=='SPY' or i>200] for s in theory.SYMBOLS}
    finished=theory.run(tmp_path,extended,cal,later,{'public':{}})
    assert finished['completed_cohorts']==1 and finished['states']['PAPER_QUALIFIED']==0
    with sqlite3.connect(tmp_path/'lab.sqlite') as c:
        outcome=json.loads(c.execute("SELECT payload FROM records WHERE kind='cohort_result'").fetchone()[0])
        assert 'NEW:passive' not in outcome['results']
        assert outcome['results']['SPY:passive']['entry']==cohort['entry']
    monkeypatch.setattr(theory,'RULES',dict(theory.RULES,unexpected='Changed after registration'))
    with pytest.raises(ValueError,match='Frozen'):theory.run(tmp_path,data,cal,now,{'public':{}})
