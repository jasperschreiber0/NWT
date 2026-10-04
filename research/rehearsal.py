"""Independent $5,000 cash-only whole-share rehearsal of frozen research cohorts."""
import json
import math
import sqlite3
from datetime import timedelta
from event_core import sessions,dt

POLICY=dict(version='whole-share-5000-20261005-v1',capital=5000.,position_fraction=.1,invested_fraction=.6,
    cost_per_side=.0015,settlement='Exit proceeds reusable next session only',shorts=False,margin=False,
    fills='Next regular open plus modeled cost; no intraday fill or options approval claims')


def size(weights,prices,cash,equity):
    proposals={};remaining=min(cash,equity*POLICY['invested_fraction'])
    for symbol,weight in sorted(weights.items()):
        if not math.isfinite(weight) or not 0<=weight<=1:raise ValueError('Invalid weight')
        price=float(prices[symbol]);cost=price*(1+POLICY['cost_per_side'])
        if not math.isfinite(price) or price<=0:raise ValueError('Invalid price')
        allocation=min(equity*weight,equity*POLICY['position_fraction'],remaining)
        qty=math.floor(allocation/cost)
        if qty:proposals[symbol]=qty;remaining-=qty*cost
    return proposals


def update(folder,lab_folder,data,calendar,now):
    folder.mkdir(parents=True,exist_ok=True);cal=sessions(calendar);days=[s['date'] for s in cal]
    latest=max(s['date'] for s in cal if dt(s['close'])+timedelta(minutes=15)<=now)
    indexed={s:{b['t'][:10]:b for b in rows} for s,rows in data.items()}
    c=sqlite3.connect(folder/'rehearsal.sqlite')
    c.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,payload TEXT,PRIMARY KEY(kind,key))')
    from strategy_lab import put,get,records
    folders=lab_folder if isinstance(lab_folder,list) else [lab_folder]
    merged={}
    for study_folder in folders:
        source=sqlite3.connect('file:'+str(study_folder/'lab.sqlite')+'?mode=ro',uri=True)
        for raw, in source.execute("SELECT payload FROM records WHERE kind='cohort' ORDER BY key"):
            row=json.loads(raw);entry=row['entry']
            if entry not in merged:merged[entry]=dict(entry=entry,exit=row['exit'],entry_at=row['entry_at'],portfolio_weights={})
            elif merged[entry]['exit']!=row['exit']:raise ValueError('Incompatible rehearsal horizons')
            for symbol,weight in row['portfolio_weights'].items():
                merged[entry]['portfolio_weights'][symbol]=merged[entry]['portfolio_weights'].get(symbol,0)+weight/len(folders)
        source.close()
    cohorts=[merged[k] for k in sorted(merged)]
    with c:
        old=get(c,'policy','policy')
        if old and old!=POLICY:raise ValueError('Rehearsal policy changed')
        put(c,'policy','policy',POLICY)
        cash=POLICY['capital'];settled_after='';errors=[]
        for cohort in cohorts:
            entry=cohort['entry'];end=cohort['exit'];existing=get(c,'roundtrip',entry)
            if existing:
                cash=existing['ending_cash'];settled_after=existing['settled_after'];continue
            if get(c,'missed',entry):continue
            opening=dt(cohort['entry_at']);prepared=get(c,'prepared',entry)
            if not prepared:
                if opening<=now:
                    put(c,'missed',entry,dict(reason='Rehearsal was not registered before entry; no retrospective fills'));continue
                prepared=dict(frozen_at=now.isoformat(),weights=cohort['portfolio_weights'],entry=entry,exit=end)
                put(c,'prepared',entry,prepared)
            if dt(prepared['frozen_at'])>=opening:raise ValueError('Late rehearsal registration')
            if entry>latest:continue
            if entry<settled_after:
                put(c,'missed',entry,dict(reason='Cash unsettled at scheduled entry'));continue
            weights=prepared['weights'];entry_record=get(c,'entry',entry)
            if not entry_record:
                prices={s:float(indexed[s][entry]['o']) for s in weights}
                quantities=size(weights,prices,cash,cash)
                invested=sum(q*prices[s]*(1+POLICY['cost_per_side']) for s,q in quantities.items())
                entry_record=dict(quantities=quantities,entry_prices=prices,starting_cash=cash,cash_remaining=cash-invested)
                put(c,'entry',entry,entry_record)
            if end>latest:
                break
            proceeds=sum(q*float(indexed[s][end]['o'])*(1-POLICY['cost_per_side']) for s,q in entry_record['quantities'].items())
            cash=entry_record['cash_remaining']+proceeds
            index=days.index(end)
            if index+1>=len(days):raise ValueError('Missing settlement session')
            settled_after=days[index+1]
            put(c,'roundtrip',entry,dict(entry=entry,exit=end,ending_cash=cash,net=cash-entry_record['starting_cash'],
                settled_after=settled_after,quantities=entry_record['quantities']))
        closed=records(c,'roundtrip');open_positions=[];equity=cash
        for key,payload in c.execute("SELECT key,payload FROM records WHERE kind='entry'"):
            if get(c,'roundtrip',key):continue
            row=json.loads(payload);equity=row['cash_remaining']
            for s,qty in row['quantities'].items():
                price=float(indexed[s][latest]['c']);equity+=qty*price
                open_positions.append(dict(symbol=s,qty=qty,mark=price))
        result=dict(status='OK',observed_at=now.isoformat(),policy=POLICY,equity=equity,net=equity-POLICY['capital'],
            completed_cohorts=len(closed),missed=records(c,'missed'),open_positions=open_positions,studies=len(folders),
            prepared=len(records(c,'prepared')),limitations=['Daily adjusted prices are modeling inputs; not actual fills',
                'No option positions admitted: contract-unit risk and assignment require separate broker validation'])
    c.close()
    from research_hub import atomic
    atomic(folder/'latest.json',result)
    return {k:result[k] for k in ['status','equity','net','completed_cohorts','prepared','missed']}
