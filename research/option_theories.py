"""Prospective $5k-sized vertical-spread research; no brokerage order authority."""
import hashlib,json,sqlite3,statistics
from pathlib import Path
from datetime import datetime,timedelta
from event_core import sessions,dt
from research_engine import quote
from data_atlas import atomic
import strategy_lab as lab

VERSION='small-account-option-theories-20261005-v1'
STRUCTURES=['bull_put','bear_call','bull_call','bear_put']


def value(pair,chain,observed,entry=True):
    a=quote(chain.get(pair['short'],{}),observed);b=quote(chain.get(pair['long'],{}),observed)
    if not a or not b:return None
    # Positive cashflow means credit. Two cents adverse spread execution beyond BBO.
    cash=a[0]-b[1]-.02 if entry else b[0]-a[1]-.02
    if abs(cash)>=pair['width']:return None
    if entry:
        if pair['credit'] and cash<=0:return None
        if not pair['credit'] and cash>=0:return None
        risk=((pair['width']-cash) if pair['credit'] else -cash)*100+2.60
        if not 0<risk<=100:return None
        return dict(cashflow=cash,max_loss_with_assumed_fees=risk)
    return dict(cashflow=cash)


def choose(structure,width,frame):
    chain=frame['chain'];spot=frame['spot'];observed=frame['observed'];choices=[]
    kind='P' if structure in ['bull_put','bear_put'] else 'C'
    credit=structure in ['bull_put','bear_call']
    for symbol in chain:
        try:
            if symbol[-9]!=kind:continue
            expiry=datetime.strptime(symbol[-15:-9],'%y%m%d').date();dte=(expiry-observed.date()).days
            if not 21<=dte<=45:continue
            strike=int(symbol[-8:])/1000
            if credit:
                if kind=='P' and strike>spot*.98:continue
                if kind=='C' and strike<spot*1.02:continue
                other=strike-width if kind=='P' else strike+width
                pair=dict(short=symbol,long=symbol[:-8]+f'{round(other*1000):08d}',width=width,credit=True)
                distance=abs(strike-spot*(.98 if kind=='P' else 1.02))
            else:
                if abs(strike/spot-1)>.01:continue
                other=strike+width if kind=='C' else strike-width
                pair=dict(long=symbol,short=symbol[:-8]+f'{round(other*1000):08d}',width=width,credit=False)
                distance=abs(strike-spot)
            if value(pair,chain,observed):choices.append((abs(dte-30),distance,symbol,pair))
        except (ValueError,IndexError):continue
    return min(choices,key=lambda r:r[:3])[-1] if choices else None


def run(folder,frames,data,calendar,now):
    folder.mkdir(parents=True,exist_ok=True);c=sqlite3.connect(folder/'options.sqlite')
    c.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,payload TEXT,PRIMARY KEY(kind,key))')
    policy=dict(version=VERSION,risk_cap=100,reference_capital=5000,widths=[1,2],structures=STRUCTURES,
        code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),hold_sessions=5,
        costs='Executable-side BBO plus $0.02 adverse per spread side and $2.60 assumed roundtrip fees',
        limits='Shadow estimates only; no assignment, early exercise, legging or guaranteed fill simulation',
        promotion='No automatic broker admission for these options until option-specific execution validation')
    cal=sessions(calendar);results=[];pending=[];skipped=[];unresolved=[]
    with c:
        old=lab.get(c,'policy',VERSION)
        if old and old!=policy:raise ValueError('Frozen option protocol changed')
        lab.put(c,'policy',VERSION,policy)
        for proposal in lab.records(c,'proposal'):
            key=proposal['key'];outcome=lab.get(c,'outcome',key)
            if outcome:
                (results if outcome['state']=='COMPLETE' else skipped).append(outcome);continue
            symbol=proposal['symbol'];entry=frames.get((proposal['entry'],symbol));end=frames.get((proposal['exit'],symbol))
            entry_at=dt(proposal['entry_at'])
            if dt(proposal['frozen_at'])>=entry_at:raise ValueError('Late option registration')
            if entry is None or not entry_at<=entry['observed']<=entry_at+timedelta(minutes=15):
                if now>entry_at+timedelta(hours=2):
                    out=dict(key=key,state='SKIPPED',reason='Entry BBO frame missing')
                    lab.put(c,'outcome',key,out);skipped.append(out)
                else:pending.append(key)
                continue
            initial=value(proposal['pair'],entry['chain'],entry['observed'])
            if not initial:
                out=dict(key=key,state='SKIPPED',reason='Entry not tradeable within $100 risk budget')
                lab.put(c,'outcome',key,out);skipped.append(out);continue
            final=value(proposal['pair'],end['chain'],end['observed'],False) if end else None
            if final:
                out=dict(key=key,state='COMPLETE',entry=proposal['entry'],exit=proposal['exit'],
                    net_model_dollars=(initial['cashflow']+final['cashflow'])*100-2.60,
                    maximum_loss=initial['max_loss_with_assumed_fees'],entry_sha256=entry['sha256'],exit_sha256=end['sha256'])
                lab.put(c,'outcome',key,out);results.append(out)
            else:
                pending.append(key)
                if proposal['exit']<now.date().isoformat():unresolved.append(key)
        future=[s for s in cal if dt(s['close'])-timedelta(hours=1)>now+timedelta(minutes=2)]
        if future:
            first=future[0];i=cal.index(first)
            if i+5<len(cal):
                for symbol in ['SPY','QQQ']:
                    candidates=[(d,f) for (d,s),f in frames.items() if s==symbol and f['observed']<now]
                    if not candidates:continue
                    day,frame=max(candidates,key=lambda p:p[0])
                    # Do not register against a stale session.
                    complete=[s['date'] for s in cal if dt(s['close'])+timedelta(minutes=15)<=now]
                    if not complete or day!=complete[-1]:continue
                    closes=[float(b['c']) for b in data.get(symbol,[]) if b['t'][:10]<=day]
                    if len(closes)<200:continue
                    bullish=closes[-1]>statistics.mean(closes[-200:])
                    for structure in STRUCTURES:
                        for width in [1,2]:
                            variant=f'{symbol}:{structure}:{width}';key=variant+':'+first['date']
                            previous=[p for p in lab.records(c,'proposal') if p['variant']==variant]
                            if any(p['key'] in pending or p['exit']>first['date'] for p in previous):continue
                            if lab.get(c,'proposal',key):continue
                            if structure.startswith('bull')!=bullish:continue
                            pair=choose(structure,width,frame)
                            if pair:lab.put(c,'proposal',key,dict(key=key,variant=variant,symbol=symbol,pair=pair,
                                frozen_at=now.isoformat(),entry=first['date'],exit=cal[i+5]['date'],
                                entry_at=(dt(first['close'])-timedelta(hours=1)).isoformat(),source_sha256=frame['sha256']))
        report=dict(version=VERSION,observed_at=now.isoformat(),status='DEGRADED' if unresolved else 'OK',execution_enabled=False,
            variants=16,registered=len(lab.records(c,'proposal')),completed=len(results),
            pending=len(lab.records(c,'proposal'))-len(results)-len(skipped),skipped=len(skipped),
            net_model_dollars=sum(r['net_model_dollars'] for r in results),results=results,
            unresolved_exit_quotes=unresolved,verdict='COLLECTING_PROSPECTIVE_BBO_EVIDENCE',policy=policy)
    c.close();atomic(folder/'latest.json',report);return report
