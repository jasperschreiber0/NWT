"""Versioned prospective experiments. No order authority or automatic live promotion."""
import hashlib
import json
import math
import statistics
from datetime import timedelta
from zoneinfo import ZoneInfo
from pattern_lab import timestamp, regular

VERSION = '20260924-v1'
POLICY = {'version': VERSION, 'discovery_sessions': 20, 'validation_sessions': 20,
          'minimum_validation_observations': 100, 'minimum_regimes': 2,
          'stress_roundtrip': .003, 'extra_slippage_roundtrip': .001,
          'live_capital': 5000, 'max_position_fraction': .1,
          'promotion': 'REVIEW_ONLY_NO_ORDER_AUTHORITY'}
SECTORS = {'AAPL':'XLK','MSFT':'XLK','NVDA':'SMH','AMD':'SMH','META':'XLC',
           'GOOGL':'XLC','AMZN':'XLY','TSLA':'XLY','COIN':'XLF','PLTR':'XLK'}


def initialize(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS experiment_versions(version TEXT PRIMARY KEY,policy TEXT,hash TEXT);
    CREATE TABLE IF NOT EXISTS experiment_observations(
      version TEXT,rule TEXT,symbol TEXT,t TEXT,observed_at TEXT,direction INTEGER,
      horizon INTEGER,regime TEXT,spread REAL,entry_t TEXT,exit_t TEXT,
      gross REAL,net REAL,stressed REAL,benchmark REAL,entry_price REAL,
      PRIMARY KEY(version,rule,symbol,t));
    CREATE TABLE IF NOT EXISTS experiment_reviews(version TEXT,day TEXT,payload TEXT,
      PRIMARY KEY(version,day));
    ''')
    import inspect
    policy=json.dumps(POLICY,sort_keys=True)
    digest=hashlib.sha256((policy+inspect.getsource(hypotheses)).encode()).hexdigest()
    old=c.execute('SELECT policy,hash FROM experiment_versions WHERE version=?',(VERSION,)).fetchone()
    if old and (old[0]!=policy or old[1]!=digest):raise ValueError('Frozen experiment policy changed without a version bump')
    c.execute('INSERT OR IGNORE INTO experiment_versions VALUES (?,?,?)',
              (VERSION,policy,digest))


def spread_fraction(snapshot,now):
    q=(snapshot or {}).get('latestQuote') or {}
    try:
        bid,ask=float(q['bp']),float(q['ap'])
        age=(now-timestamp(q['t'])).total_seconds()
        if not all(math.isfinite(x) for x in (bid,ask)) or not 0<bid<=ask or not 0<=age<=120:return None
        return (ask-bid)/((ask+bid)/2)
    except (KeyError,TypeError,ValueError):return None


def hypotheses(bars, sector, snapshot, history):
    """Rules use only completed bars available at observation time."""
    if len(bars)<31:return {}
    recent=bars[-31:]
    if any((timestamp(b['t'])-timestamp(a['t'])).total_seconds()!=60 for a,b in zip(recent,recent[1:])):return {}
    last=recent[-1];price=float(last['c']);ret=price/float(recent[0]['c'])-1
    vol=statistics.pstdev([float(b['c'])/float(a['c'])-1 for a,b in zip(recent,recent[1:])])
    regime=('trend' if abs(ret)>.003 else 'range')+('_highvol' if vol>.001 else '_lowvol')
    out={}
    minute=timestamp(last['t']).astimezone(ZoneInfo('America/New_York'))
    previous=(snapshot or {}).get('prevDailyBar') or {}
    today=(snapshot or {}).get('dailyBar') or {}
    if previous.get('c') and today.get('o') and today.get('t') and timestamp(today['t']).date()==timestamp(last['t']).date() and minute.hour==10 and minute.minute<=30:
        gap=float(today['o'])/float(previous['c'])-1
        if abs(gap)>=.005:
            sign=1 if gap>0 else -1
            out['opening_gap_follow']=(sign if sign*ret>0 else 0,30)
            out['opening_gap_fade']=(-sign if sign*ret<0 else 0,30)
    ref={b['t']:b for b in sector}
    if recent[0]['t'] in ref and last['t'] in ref:
        excess=ret-(float(ref[last['t']]['c'])/float(ref[recent[0]['t']]['c'])-1)
        out['sector_relative_strength']=(1 if excess>.002 else -1 if excess<-.002 else 0,120)
    if len(history)>=3:
        move=float(history[-1]['c'])/float(history[-3]['c'])-1
        out['three_day_trend_confirmation']=(1 if move>.01 and ret>0 else -1 if move<-.01 and ret<0 else 0,120)
    return {rule:(direction,horizon,regime) for rule,(direction,horizon) in out.items()}


def run(c,snapshots,now):
    initialize(c)
    loaded={s:[json.loads(r[0]) for r in c.execute('SELECT payload FROM bars WHERE symbol=? ORDER BY t DESC LIMIT 90',(s,))][::-1] for s in snapshots}
    for symbol,bars in loaded.items():
        if not bars or not regular(bars[-1]['t']) or timestamp(bars[-1]['t']).minute%5:continue
        if not 60<=(now-timestamp(bars[-1]['t'])).total_seconds()<=180:continue
        spread=spread_fraction(snapshots[symbol],now)
        if spread is None:continue
        # Daily close context requires a recorded 15:59 ET minute, never a
        # partial day mislabeled as a daily close. Raw prices retain split risk.
        prior=[]
        for t,payload in c.execute('SELECT t,payload FROM bars WHERE symbol=? AND t<? ORDER BY t DESC',(symbol,bars[-1]['t'][:10])):
            dt=timestamp(t).astimezone(ZoneInfo('America/New_York'))
            if (dt.hour,dt.minute)==(15,59):prior.append(json.loads(payload))
            if len(prior)==3:break
        rules=hypotheses(bars,loaded.get(SECTORS.get(symbol,''),[]),snapshots[symbol],prior[::-1])
        for rule,(side,horizon,regime) in rules.items():
            # One active horizon per rule/symbol: do not inflate samples with
            # overlapping predictions. Cross-symbol dependence still exists.
            last=c.execute('SELECT observed_at,horizon FROM experiment_observations WHERE version=? AND rule=? AND symbol=? AND direction<>0 ORDER BY observed_at DESC LIMIT 1',(VERSION,rule,symbol)).fetchone()
            if side and last and now<=timestamp(last[0])+timedelta(minutes=last[1]+1):continue
            c.execute('INSERT OR IGNORE INTO experiment_observations(version,rule,symbol,t,observed_at,direction,horizon,regime,spread) VALUES (?,?,?,?,?,?,?,?,?)',
                      (VERSION,rule,symbol,bars[-1]['t'],now.isoformat(),side,horizon,regime,spread))
    for rule,symbol,t,observed,side,horizon,spread in c.execute('SELECT rule,symbol,t,observed_at,direction,horizon,spread FROM experiment_observations WHERE version=? AND direction<>0 AND gross IS NULL',(VERSION,)).fetchall():
        entry=timestamp(observed).replace(second=0,microsecond=0)+timedelta(minutes=1);end=entry+timedelta(minutes=horizon)
        if end>now or not regular(entry.isoformat()) or not regular(end.isoformat()):continue
        a,b=entry.strftime('%Y-%m-%dT%H:%M:%SZ'),end.strftime('%Y-%m-%dT%H:%M:%SZ')
        rows=[json.loads(r[0]) for r in c.execute('SELECT payload FROM bars WHERE symbol=? AND t>=? AND t<=? ORDER BY t',(symbol,a,b))]
        if len(rows)!=horizon+1 or rows[0]['t']!=a or rows[-1]['t']!=b:continue
        ref=[json.loads(r[0]) for r in c.execute('SELECT payload FROM bars WHERE symbol=? AND t IN (?,?) ORDER BY t',('SPY',a,b))]
        if len(ref)!=2:continue
        price=float(rows[0]['o']);gross=side*(float(rows[-1]['o'])/price-1)
        cost=spread+POLICY['extra_slippage_roundtrip']
        c.execute('UPDATE experiment_observations SET entry_t=?,exit_t=?,gross=?,net=?,stressed=?,benchmark=?,entry_price=? WHERE version=? AND rule=? AND symbol=? AND t=?',
                  (a,b,gross,gross-cost,gross-max(cost,POLICY['stress_roundtrip']),float(ref[-1]['o'])/float(ref[0]['o'])-1,price,VERSION,rule,symbol,t))
    c.commit()
    return review(c,now.date().isoformat())


def review(c,day):
    days=[r[0] for r in c.execute('SELECT DISTINCT substr(t,1,10) FROM experiment_observations WHERE version=? ORDER BY 1',(VERSION,))]
    boundary=days[POLICY['discovery_sessions']] if len(days)>POLICY['discovery_sessions'] else '9999'
    results=[]
    for rule, in c.execute('SELECT DISTINCT rule FROM experiment_observations WHERE version=?',(VERSION,)):
        rows=c.execute('SELECT substr(t,1,10),regime,stressed,net,benchmark,entry_price,direction FROM experiment_observations WHERE version=? AND rule=? AND gross IS NOT NULL',(VERSION,rule)).fetchall()
        validation=[r for r in rows if r[0]>=boundary]
        vd=len(set(r[0] for r in validation));regimes=len(set(r[1] for r in validation))
        means={d:statistics.mean(r[2] for r in validation if r[0]==d) for d in sorted(set(r[0] for r in validation))}
        daily=list(means.values());avg=statistics.mean(daily) if daily else None
        # Day-level dispersion avoids treating correlated symbols as independent.
        lower=avg-3*statistics.stdev(daily)/math.sqrt(len(daily)) if len(daily)>1 else None
        enough=vd>=POLICY['validation_sessions'] and len(validation)>=POLICY['minimum_validation_observations'] and regimes>=POLICY['minimum_regimes']
        state='DISCOVERY' if boundary=='9999' else 'VALIDATING'
        if enough:state='REVIEW_CANDIDATE' if lower is not None and lower>0 else 'RETIRED_FROM_SHORTLIST'
        results.append({'rule':rule,'state':state,'observations':len(rows),'validation_observations':len(validation),
          'validation_days':vd,'validation_regimes':regimes,'validation_daily_mean_stressed':avg,
          'conservative_daily_bound':lower,'mean_stressed':statistics.mean(r[2] for r in rows) if rows else None,
          'mean_excess_over_spy':statistics.mean(r[3]-r[4] for r in rows) if rows else None,
          'small_account_long_share_candidates':sum(r[5]<=500 and r[6]==1 for r in rows),
          'small_account_note':'One-share affordability at 10% of $5,000 only; short margin/options feasibility not established'})
    report={'version':VERSION,'sessions':len(days),'validation_starts':None if boundary=='9999' else boundary,
            'rules':results,'policy':POLICY,'execution_enabled':False,
            'limitations':['Day clustering reduces, but does not eliminate, dependence.',
              'Quotes estimate roundtrip spread; exit spread and real fills can differ.',
              'Raw-price corporate actions can distort multi-day context.',
              'Review candidates require independent execution-grade validation; no automatic trading promotion.']}
    c.execute('INSERT OR REPLACE INTO experiment_reviews VALUES (?,?,?)',(VERSION,day,json.dumps(report)));c.commit()
    return report
