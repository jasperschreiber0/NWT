"""Deterministic research comparisons. All returns are modeled; no order methods."""
import math, statistics

SYMBOLS=['SPY','QQQ','XLK','XLF','XLE','XLV','XLI','XLP']
SECTORS=SYMBOLS[2:]
RULES=['passive_spy','dual_trend200','sector_momentum252']


def weights(rule,history):
    if rule=='passive_spy':return {'SPY':1.}
    if rule=='dual_trend200':
        return {s:.5 for s in ['SPY','QQQ'] if len(history[s])>=200 and history[s][-1]>statistics.mean(history[s][-200:])}
    if len(history['SPY'])<253 or history['SPY'][-1]<=statistics.mean(history['SPY'][-200:]):return {}
    ranks=sorted(((history[s][-1]/history[s][-253]-1,s) for s in SECTORS if len(history[s])>=253),reverse=True)
    return {s:1/3 for ret,s in ranks[:3] if ret>0}


def backtest(data,rule,start,end,cost=.0015):
    indexed={s:{b['t'][:10]:b for b in data[s]} for s in SYMBOLS}
    dates=sorted(indexed['SPY'])
    if any(set(indexed[s])!=set(dates) for s in SYMBOLS):raise ValueError('Unaligned equity history')
    closes={s:[float(indexed[s][d]['c']) for d in dates] for s in SYMBOLS}
    if any(len(indexed[s])!=len(data[s]) for s in SYMBOLS):raise ValueError('Duplicate daily bar')
    if any(not math.isfinite(x) or x<=0 for values in closes.values() for x in values):raise ValueError('Invalid closing price')
    cash=1.;holdings={};peak=1.;dd=0.;turnover=0.;roundtrips=0;curve=[];last_targets=None;previous_month=None
    for i,d in enumerate(dates):
        if d<start or d>end or i<253:continue
        opening={s:float(indexed[s][d]['o']) for s in SYMBOLS}
        if any(not math.isfinite(v) or v<=0 for v in opening.values()):raise ValueError('Invalid price')
        for s in holdings:holdings[s]*=opening[s]/closes[s][i-1]
        target=weights(rule,{s:closes[s][:i] for s in SYMBOLS})
        monthly=rule=='sector_momentum252'
        rebalance=(previous_month!=d[:7]) if monthly else target!=last_targets
        if rebalance:
            equity=cash+sum(holdings.values())
            desired={s:equity*w for s,w in target.items()}
            traded=sum(abs(desired.get(s,0)-holdings.get(s,0)) for s in set(desired)|set(holdings))
            fee=traded*cost;turnover+=traded/equity
            roundtrips+=sum(s not in desired for s in holdings)
            # Pay fees proportionally from new allocation; no negative cash or leverage.
            holdings={s:(equity-fee)*w for s,w in target.items()};cash=(equity-fee)*(1-sum(target.values()))
            last_targets=target;previous_month=d[:7]
        for s in holdings:holdings[s]*=closes[s][i]/opening[s]
        equity=cash+sum(holdings.values());peak=max(peak,equity);dd=max(dd,1-equity/peak)
        curve.append((d,equity))
    if not curve:return None
    final=cash+sum(holdings.values())*(1-cost);roundtrips+=len(holdings)
    dd=max(dd,1-final/peak)
    from datetime import date
    years=(date.fromisoformat(curve[-1][0])-date.fromisoformat(curve[0][0])).days/365.25
    return dict(total_return=final-1,cagr=final**(1/years)-1 if years>=1 else None,
        max_daily_drawdown=dd,completed_asset_roundtrips=roundtrips,turnover=turnover,
        sessions=len(curve),first=curve[0][0],last=curve[-1][0],final_liquidation_included=True)


def quote(snapshot,observed,max_age=180):
    from datetime import datetime
    q=snapshot.get('latestQuote') or {}
    try:
        bid,ask=float(q['bp']),float(q['ap']);t=datetime.fromisoformat(q['t'].replace('Z','+00:00'))
        if not all(math.isfinite(x) for x in [bid,ask]) or not 0<=bid<=ask or ask<=0:return None
        if not 0<=(observed-t).total_seconds()<=max_age:return None
        if float(q.get('bs',0))<1 or float(q.get('as',0))<1:return None
        return bid,ask
    except (KeyError,ValueError,TypeError):return None


def spread_choice(chain,spot,observed):
    """Fixed 2% OTM, closest 30-day expiry, $5-wide puts; no delta hindsight."""
    from datetime import datetime
    candidates=[]
    for symbol,snapshot in chain.items():
        try:
            if symbol[-9]!='P':continue
            expiry=datetime.strptime(symbol[-15:-9],'%y%m%d').date()
            dte=(expiry-observed.date()).days;strike=int(symbol[-8:])/1000
            if not 21<=dte<=45 or strike>spot*.98:continue
            long=symbol[:-8]+f'{round((strike-5)*1000):08d}'
            shortq=quote(snapshot,observed);longq=quote(chain.get(long,{}),observed)
            if not shortq or not longq:continue
            credit=shortq[0]-longq[1]
            if not .10<=credit<5:continue
            candidates.append((abs(dte-30),-strike,symbol,long))
        except (ValueError,IndexError):continue
    if not candidates:return None
    _,_,short,long=min(candidates)
    return dict(short=short,long=long,width=5.)


def spread_entry(pair,chain,observed):
    a=quote(chain.get(pair['short'],{}),observed);b=quote(chain.get(pair['long'],{}),observed)
    if not a or not b:return None
    credit=a[0]-b[1]-.02 # one cent per contract adverse execution beyond BBO
    if not 0<credit<pair['width']:return None
    return dict(credit=credit,max_expiry_risk=(pair['width']-credit)*100+2.60,
        small_account_2pct_risk_feasible=(pair['width']-credit)*100+2.60<=100)


def spread_exit(pair,entry,chain,observed):
    a=quote(chain.get(pair['short'],{}),observed);b=quote(chain.get(pair['long'],{}),observed)
    if not a or not b:return None
    debit=a[1]-b[0]+.02
    if debit<0:return None
    return dict(net_dollars=(entry['credit']-debit)*100-2.60,exit_debit=debit,
        label='BBO model, $0.65/contract/side assumed fees; not fills; assignment/legging not simulated')
