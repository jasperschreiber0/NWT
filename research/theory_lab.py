"""Versioned all-universe hypotheses with registered, non-overlapping future tests."""
import bisect, hashlib, json, math, sqlite3, statistics as st
from datetime import timedelta
from pathlib import Path
from event_core import sessions, dt
from universe import SYMBOLS
import strategy_lab as lab
from data_atlas import atomic

RULES = {
    'passive': 'Same-asset buy/hold control at matched fixed 50% exposure',
    'trend_pullback': 'Five-day decline inside a rising 200-session trend',
    'quiet_momentum': 'Positive 120-session momentum excluding latest 20 sessions; realized volatility below 25%',
    'vol_breakout': 'New 60-session high after 20-session volatility falls below prior 60-session volatility',
    'oversold_rebound': 'Three-day decline exceeds two recent daily standard deviations while above 200-session trend',
    'defensive_trend': 'Positive 200-session trend with 20-session volatility below 15%',
}
VERSION = 'all-universe-theories-20261005-v1'


def active(rule, prices):
    if len(prices) < 253: return False
    if rule == 'passive': return True
    p = prices[-253:]; returns = [p[i]/p[i-1]-1 for i in range(1,len(p))]
    sd = st.stdev(returns[-20:]); trend = p[-1] > st.mean(p[-200:])
    if rule == 'trend_pullback': return trend and p[-1] < p[-6]
    if rule == 'quiet_momentum': return p[-21] > p[-121] and sd*252**.5 < .25
    if rule == 'vol_breakout': return p[-1] > max(p[-61:-1]) and sd < st.stdev(returns[-80:-20])
    if rule == 'oversold_rebound': return trend and p[-1]/p[-4]-1 < -2*sd
    if rule == 'defensive_trend': return trend and sd*252**.5 < .15
    raise ValueError('Unknown rule')


def sample(indexed, symbol, days, i, engaged):
    interval = days[i:i+6]
    if len(interval) != 6 or any(d not in indexed[s] for s in {symbol,'SPY'} for d in interval):
        return None  # Missing prices are neither a flat return nor a fabricated fill.
    entry, end = interval[0], interval[-1]; gross = .5 if engaged else 0.
    def net(s, cost): return float(indexed[s][end]['o'])/float(indexed[s][entry]['o'])*(1-cost)/(1+cost)-1
    value = gross*net(symbol,.0015); stress = gross*net(symbol,.003)
    path = [gross*(float(indexed[symbol][d][p])/float(indexed[symbol][entry]['o'])/1.0015-1)
            for d in interval[:-1] for p in ('o','c')] if engaged else [0.]
    path.append(value); peak = 1.; dd = 0.
    for v in path: peak=max(peak,1+v); dd=max(dd,1-(1+v)/peak)
    return dict(entry=entry, exit=end, net=value, stress_net=stress, benchmark=gross*net('SPY',.0015),
        excess=value-gross*net('SPY',.0015), gross=gross, path=path, drawdown=dd)


def validate(data):
    indexed = {}
    for s in SYMBOLS:
        rows = data.get(s, []); days = [b['t'][:10] for b in rows]
        if days != sorted(set(days)): raise ValueError('Unordered prices: '+s)
        if any(not math.isfinite(float(b[k])) or float(b[k])<=0 for b in rows for k in ('o','c')):
            raise ValueError('Invalid price: '+s)
        indexed[s] = dict(zip(days,rows))
    if len(indexed['SPY']) < 253: raise ValueError('Insufficient benchmark history')
    return list(indexed['SPY']), indexed


def run(folder, data, calendar, now, atlas):
    folder.mkdir(parents=True, exist_ok=True); days,indexed=validate(data); cal=sessions(calendar)
    complete=[s['date'] for s in cal if dt(s['close'])+timedelta(minutes=15)<=now]
    if not complete or days[-1]!=complete[-1]: raise ValueError('Latest complete session missing')
    policy=dict(version=VERSION, symbols=SYMBOLS, rules=RULES, hold_sessions=5,
        cost_per_side=.0015, stress_per_side=.003, review_cohorts=12,
        code_sha256=hashlib.sha256(Path(__file__).read_bytes()+Path(lab.__file__).read_bytes()).hexdigest(),
        source_use='Long-run factor evidence motivates momentum/reversal/regime hypotheses; never used as real-time prices',
        limits='Survivor universe; retrospective inspected history; 246 dependent tests; four-SE hurdle is heuristic, not a calibrated probability',
        admission='Same operational and broker gates as existing paper bridge; no live authority')
    c=sqlite3.connect(folder/'lab.sqlite',timeout=20)
    c.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,payload TEXT,PRIMARY KEY(kind,key))')
    keys={s:sorted(indexed[s]) for s in SYMBOLS}
    prices={s:[float(indexed[s][d]['c']) for d in keys[s]] for s in SYMBOLS}
    specs={s+':'+r:dict(id=s+':'+r,symbol=s,rule=r,family=r,parents=[] if r=='passive' else [s+':passive'],
        rationale=reason) for s in SYMBOLS for r,reason in RULES.items()}
    def signal(spec, before):
        s=spec['symbol']; end=bisect.bisect_left(keys[s],before)
        # Reject stale/gapped lookbacks, including assets too recently listed.
        spy_end=bisect.bisect_left(days,before)
        if end<253 or keys[s][end-253:end]!=days[spy_end-253:spy_end]: return False
        return active(spec['rule'],prices[s][end-253:end])
    with c:
        old=lab.get(c,'policy',VERSION)
        if old and old['policy']!=policy: raise ValueError('Frozen theory protocol changed')
        if not old:
            lab.put(c,'policy',VERSION,dict(policy=policy,frozen_at=now.isoformat(),data_sha256=lab.digest(data),
                context_sources=atlas.get('public',{})))
            for key,spec in specs.items(): lab.put(c,'spec',key,spec)
            # All hypotheses fixed before reading outcomes. Inception gaps are explicit exclusions.
            for key,spec in specs.items():
                history={}
                for period,(start,end) in lab.POLICY['historical_periods'].items():
                    rows=[]; omitted=0
                    eligible=[i for i,d in enumerate(days) if start<=d<=end and i>=253]
                    for i in eligible[::5]:
                        if i+5>=len(days) or days[i+5]>end: continue
                        j=bisect.bisect_left(keys[spec['symbol']],days[i])
                        if j<253 or keys[spec['symbol']][j-253:j]!=days[i-253:i]: omitted+=1;continue
                        result=sample(indexed,spec['symbol'],days,i,signal(spec,days[i]))
                        if result is None: omitted+=1
                        else: rows.append(result)
                    history[period]=dict(**lab.summary(rows),excluded_missing_or_short_history=omitted)
                lab.put(c,'history',key,history)
        errors=[]
        for cohort in lab.records(c,'cohort'):
            if lab.get(c,'cohort_result',cohort['entry']) or cohort['exit']>days[-1]: continue
            if dt(cohort['frozen_at'])>=dt(cohort['entry_at']): raise ValueError('Late registration')
            expected=[s['date'] for s in cal if cohort['entry']<=s['date']<=cohort['exit']]
            if len(expected)!=6 or cohort['entry'] not in days: raise ValueError('Missing calendar')
            i=days.index(cohort['entry'])
            if days[i:i+6]!=expected: raise ValueError('Missing benchmark session')
            results={k:sample(indexed,specs[k]['symbol'],days,i,bool(w)) for k,w in cohort['targets'].items()}
            # Excluded assets remain excluded for the whole cohort, never backfilled into success.
            results={k:v for k,v in results.items() if k in cohort['eligible'] and v is not None}
            missing=set(cohort['eligible'])-set(results)
            if missing: errors.append('Missing cohort outcomes: '+','.join(sorted(missing)));continue
            lab.put(c,'cohort_result',cohort['entry'],dict(entry=cohort['entry'],results=results,
                source_sha256=lab.digest(data),recorded_at=now.isoformat()))
        outcomes=lab.records(c,'cohort_result')
        histories={k:[r['results'][k] for r in outcomes if k in r['results']] for k in specs}
        verdicts={k:lab.adjudicate(c,k,histories[k],{p:histories[p] for p in spec['parents']},now)
                  for k,spec in specs.items()}
        # Controls establish comparison; they are not new strategy discoveries.
        qualified={k:v for k,v in verdicts.items() if specs[k]['rule']!='passive'}
        allocation=lab.select_portfolio(qualified,specs,histories)
        future=[s for s in cal if dt(s['open'])>now+timedelta(minutes=2)]
        cohorts=lab.records(c,'cohort'); last_exit=max((r['exit'] for r in cohorts),default='')
        if future and future[0]['date']>=last_exit and not errors:
            first=future[0]; pos=cal.index(first)
            if pos+5<len(cal) and not lab.get(c,'cohort',first['date']):
                eligible=[k for k,spec in specs.items() if len(keys[spec['symbol']])>=253 and
                    keys[spec['symbol']][-253:]==days[-253:]]
                targets={k:({spec['symbol']:.5} if k in eligible and signal(spec,first['date']) else {})
                         for k,spec in specs.items()}
                weights={}
                for k,w in allocation.items():
                    for s,v in targets[k].items(): weights[s]=weights.get(s,0)+w*v
                lab.put(c,'cohort',first['date'],dict(entry=first['date'],exit=cal[pos+5]['date'],entry_at=first['open'],
                    frozen_at=now.isoformat(),signal_day=days[-1],eligible=eligible,
                    data_sha256=lab.digest(data),targets=targets,allocation=allocation,portfolio_weights=weights))
        evidence={k:dict(spec=spec,history=lab.get(c,'history',k),verdict=verdicts[k]) for k,spec in specs.items()}
        report=dict(version=VERSION,observed_at=now.isoformat(),status='DEGRADED' if errors else 'OK',
            execution_enabled=False,experiment_count=len(specs),allocation=allocation,hypotheses=evidence,
            completed_cohorts=len(outcomes),pending_cohorts=len(lab.records(c,'cohort'))-len(outcomes),
            states={s:sum(v['state']==s for v in verdicts.values()) for s in ['COLLECTING','PAPER_QUALIFIED','RETIRED','DEMOTED']},
            virtual_cash_weight=1-sum(allocation.values()),errors=errors,policy=policy,
            eligible_symbols=sum(keys[s][-253:]==days[-253:] and len(keys[s])>=253 for s in SYMBOLS),
            source_issues=atlas.get('issues',[]))
    c.close(); atomic(folder/'latest.json',report)
    return {k:report[k] for k in ['version','observed_at','status','experiment_count','states','eligible_symbols',
        'completed_cohorts','pending_cohorts','errors']}
