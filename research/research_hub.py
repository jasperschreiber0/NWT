"""Nightly reproducible research inventory and verdict. GET-only broker access."""
import argparse,hashlib,json,os,sqlite3,gzip,sys,statistics
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from research_engine import SYMBOLS,RULES,weights,backtest,spread_choice,spread_entry,spread_exit

ROOT=Path(__file__).resolve().parents[1]
POLICY={'version':'research-expansion-20261005-v1','symbols':SYMBOLS,'equity_rules':RULES,
    'periods':{'development':['2017-01-01','2019-12-31'],'validation':['2020-01-01','2022-12-31'],
        'later_exploratory':['2023-01-01','2025-12-31'],'recent_exploratory':['2026-01-01','2026-10-02']},
    'history_status':'ALL_RETROSPECTIVE; these date ranges have previously been inspected; not a pristine holdout',
    'selection':'Highest development CAGR after 30bps roundtrip, positive validation excess over passive SPY, drawdown <=20%; fresh prospective validation still required',
    'options':'SPY/QQQ 2% OTM 5-wide bull put, nearest 30DTE in 21..45, select at 15:00 ET; next session 15:00 entry; five-session hold; one open spread per underlying',
    'ai':'Matched event cohort: frozen AI directional hypothesis versus same-asset long and no-trade; no retrospective LLM calls',
    'promotion':'RESEARCH_REVIEW_ONLY; no broker orders or automatic capital promotion',
    'assessment':'One frozen review per variant: 30 option groups across 15 entry dates; 12 nonoverlapping equity cohorts; 30 AI events. Cluster mean minus three standard errors must exceed zero. Heuristic shortlist, not statistical proof.',
    'limitations':['Fixed survivor-selected ETFs, not a delisting-complete stock universe','No verified 40-year history',
        'Equity model uses adjusted price ratios and fractional allocations; not whole-share fills',
        'Stock returns cannot reconstruct historical option quotes','Operating bills not confirmed; infrastructure excluded']}


def atomic(path,data):
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data,default=str,indent=2));temp.replace(path)


def archive(c,kind,key,data):
    c.execute('INSERT OR IGNORE INTO records VALUES (?,?,?,?)',(kind,key,datetime.now(timezone.utc).isoformat(),json.dumps(data,default=str)))
    c.commit()


def assess(c,name,observations,minimum,groups_min):
    key=POLICY['version']+':'+name
    old=c.execute("SELECT payload FROM records WHERE kind='verdict' AND key=?",(key,)).fetchone()
    if old:return json.loads(old[0])
    groups={}
    for group,value in observations:groups.setdefault(group,[]).append(value)
    result=dict(name=name,observations=len(observations),groups=len(groups),verdict='INSUFFICIENT_FRESH_EVIDENCE')
    if len(observations)<minimum or len(groups)<groups_min:return result
    values=[statistics.mean(v) for v in groups.values()]
    lower=statistics.mean(values)-3*statistics.stdev(values)/len(values)**.5
    result.update(lower_heuristic=lower,verdict='REVIEW_CANDIDATE' if lower>0 else 'RETIRED_FROM_SHORTLIST')
    archive(c,'verdict',key,result)
    return result


def equity_review(data):
    results={r:{p:backtest(data,r,*dates) for p,dates in POLICY['periods'].items()} for r in RULES}
    candidates=[r for r in RULES if r!='passive_spy' and results[r]['development']]
    chosen=max(candidates,key=lambda r:results[r]['development']['cagr']) if candidates else None
    passed=False
    if chosen:
        v=results[chosen]['validation'];benchmark=results['passive_spy']['validation']
        passed=bool(v and v['total_return']>benchmark['total_return'] and v['max_daily_drawdown']<=.2)
    return dict(results=results,development_selected=chosen,passes_historical_screen=passed,
        verdict='FRESH_PAPER_VALIDATION_REQUIRED' if passed else 'NO_HISTORICAL_QUALIFIER',
        history_status=POLICY['history_status'])


def option_frames(root):
    """At most one bounded timestamped frame per symbol/session; reject stale legs later."""
    frames={};missing=[]
    for folder in sorted(root.glob('20??-??-??')):
        for symbol in ['SPY','QQQ']:
            selected=None
            for path in sorted(folder.glob('*-options-'+symbol+'.json.gz')):
                # Filename UTC hour avoids decompressing the full archive.
                if not '18'<=path.name[:2]<='20':continue
                with gzip.open(path,'rt') as f:record=json.load(f)
                if record['data'].get('parameters',{}).get('feed')!='opra':continue
                observed=datetime.fromisoformat(record['observed_at'])
                local=observed.astimezone(ZoneInfo('America/New_York'))
                if local.hour==15 and local.minute<=15:selected=(path,record,observed);break
            if not selected:continue
            path,record,observed=selected
            stocks=[p for p in folder.glob('*-stocks.json.gz') if p.name[:12]<=path.name[:12]]
            if not stocks:continue
            with gzip.open(max(stocks),'rt') as f:stock=json.load(f)
            if not 0<=(observed-datetime.fromisoformat(stock['observed_at'])).total_seconds()<=180:continue
            trade=stock['data'].get(symbol,{}).get('latestTrade',{})
            if not trade.get('t') or not 0<=(observed-datetime.fromisoformat(trade['t'].replace('Z','+00:00'))).total_seconds()<=180:continue
            spot=float(trade.get('p') or 0)
            if spot<=0:continue
            frames[(folder.name,symbol)]={'chain':record['data']['snapshots'],'observed':observed,'spot':spot,
                'source':str(path.relative_to(root)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    return frames


def options_review(c,frames,calendar,data=None):
    days=[s['date'] for s in calendar]; completed=[];unresolved=[];missing=0
    for symbol in ['SPY','QQQ']:
        busy_until=''
        for day in days:
            if day<=busy_until or (day,symbol) not in frames:continue
            frame=frames[(day,symbol)];index=days.index(day)
            if index+6>=len(days):continue
            key=POLICY['version']+':'+symbol+':'+day
            row=c.execute("SELECT observed,payload FROM records WHERE kind='spread_decision' AND key=?",(key,)).fetchone()
            if row:decision=json.loads(row[1]);frozen=row[0]
            else:
                pair=spread_choice(frame['chain'],frame['spot'],frame['observed'])
                decision=dict(pair=pair,signal_day=day,entry_day=days[index+1],exit_day=days[index+6],
                    source=frame['source'],sha256=frame['sha256'])
                archive(c,'spread_decision',key,decision)
                frozen=c.execute("SELECT observed FROM records WHERE kind='spread_decision' AND key=?",(key,)).fetchone()[0]
            if decision['sha256']!=frame['sha256']:raise ValueError('Frozen option source changed')
            saved=c.execute("SELECT payload FROM records WHERE kind='spread_outcome' AND key=?",(key,)).fetchone()
            if saved:
                completed.append(json.loads(saved[0]));busy_until=decision['exit_day'];continue
            if not decision['pair']:continue
            entry_frame=frames.get((decision['entry_day'],symbol));exit_frame=frames.get((decision['exit_day'],symbol))
            if not entry_frame:
                missing+=1;continue
            entry=spread_entry(decision['pair'],entry_frame['chain'],entry_frame['observed'])
            if not entry:missing+=1;continue
            busy_until=decision['exit_day']
            prospective=datetime.fromisoformat(frozen)<entry_frame['observed']
            exit_=spread_exit(decision['pair'],entry,exit_frame['chain'],exit_frame['observed']) if exit_frame else None
            if not exit_:
                unresolved.append(dict(symbol=symbol,entry_day=decision['entry_day'],exit_day=decision['exit_day']))
                # Do not simulate another position while an exit remains unpriced.
                break
            result=dict(symbol=symbol,**decision,**entry,**exit_,prospective=prospective,
                entry_sha256=entry_frame.get('sha256'),exit_sha256=exit_frame.get('sha256'),
                matched_underlying_return=exit_frame['spot']/entry_frame['spot']-1,
                capital_risk_return=exit_['net_dollars']/entry['max_expiry_risk'])
            history=[float(b['c']) for b in (data or {}).get(symbol,[]) if b['t'][:10]<day]
            trend=len(history)>=200 and history[-1]>statistics.mean(history[-200:])
            returns=[history[j]/history[j-1]-1 for j in range(max(1,len(history)-20),len(history))]
            vol=statistics.stdev(returns)*252**.5 if len(returns)==20 else None
            result.update(trend_above_200=trend,realized_vol20=vol,
                fixed_trend_calm_filter=trend and vol is not None and vol<.25)
            archive(c,'spread_outcome',key,result);completed.append(result)
    return dict(completed=completed,unresolved=unresolved,missing_or_untradeable_entries=missing,
        fixed_filtered_trades=sum(t['fixed_trend_calm_filter'] for t in completed),
        fixed_filtered_net=sum(t['net_dollars'] for t in completed if t['fixed_trend_calm_filter']),
        prospective_completed=sum(t['prospective'] for t in completed),net_model_dollars=sum(t['net_dollars'] for t in completed),
        underlyings=2,verdict='INSUFFICIENT_PROSPECTIVE_EVIDENCE',
        historical_quote_api='Unavailable in access probe; archive replay only',
        risk_note='SPY and QQQ are correlated. Risk-normalized option returns are not account returns; underlying comparison has different exposure.')


def ai_review(path):
    if not path.exists():return dict(status='MISSING_EVENT_ARCHIVE',pairs=0)
    e=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
    pairs=[];excluded=0
    for key,payload in e.execute("SELECT key,payload FROM records WHERE kind='outcome'"):
        result=json.loads(payload);pred=result.get('prediction',{})
        if pred.get('rule')!='delayed_reaction_v1':continue
        event=pred.get('event_id');row=e.execute("SELECT observed_at,payload FROM records WHERE kind='classification' AND key=?",(event,)).fetchone()
        if not row:excluded+=1;continue
        classification=json.loads(row[1])
        if classification.get('historical_context_only') or row[0]>=pred.get('entry_open_at',''):
            excluded+=1;continue
        ratio=float(result['exit_open'])/float(result['entry_open'])
        baseline=(ratio-1)-.0005*(1+ratio)
        ai=float(result['return_pct'])/100
        pairs.append(dict(key=key,event_id=event,ai_return=ai,long_baseline=baseline,no_trade=0.,
            incremental_return=ai-baseline,short_borrow_missing=pred.get('direction',1)<0))
    e.close()
    return dict(status='PAIRED_OBSERVATION_ONLY',pairs=len(pairs),unique_events=len(set(p['event_id'] for p in pairs)),
        mean_incremental_return=statistics.mean(p['incremental_return'] for p in pairs) if pairs else None,
        observations=pairs,excluded=excluded,verdict='AI_VALUE_NOT_ESTABLISHED',
        limitation='Same selected-event cohort only; cannot establish event-selection value or historical model knowledge contamination. Short borrow omitted.')


def forward_equities(c,data,calendar,now):
    from event_core import sessions,dt
    cal=sessions(calendar);days=[s['date'] for s in cal];indexed={s:{b['t'][:10]:b for b in rows} for s,rows in data.items()}
    scored=[]
    for key,observed,payload in c.execute("SELECT key,observed,payload FROM records WHERE kind='equity_target'").fetchall():
        pred=json.loads(payload);entry=pred['entry_open_at'][:10]
        if observed>=pred['entry_open_at'] or entry not in days:continue
        i=days.index(entry)
        if i+20>=len(days) or dt(cal[i+20]['open'])+timedelta(minutes=15)>now:continue
        end=days[i+20]
        if any(entry not in indexed[s] or end not in indexed[s] for s in set(pred['weights'])|{'SPY'}):continue
        def net(s):
            ratio=float(indexed[s][end]['o'])/float(indexed[s][entry]['o'])
            return ratio-1-.0015*(1+ratio)
        saved=c.execute("SELECT payload FROM records WHERE kind='equity_forward_outcome' AND key=?",(key,)).fetchone()
        if saved:scored.append(json.loads(saved[0]));continue
        result=dict(rule=pred['rule'],entry=entry,exit=end,net=sum(w*net(s) for s,w in pred['weights'].items()),benchmark=net('SPY'),
            label='20-session fixed-weight prospective proxy, not full strategy or broker P&L')
        archive(c,'equity_forward_outcome',key,result);scored.append(result)
    future=[s for s in cal if dt(s['open'])>now+timedelta(minutes=2)]
    if future:
        history={s:[float(b['c']) for b in data[s]] for s in SYMBOLS}
        for rule in RULES:
            existing=[json.loads(p) for p, in c.execute("SELECT payload FROM records WHERE kind='equity_target'") if json.loads(p)['rule']==rule]
            last=max((p['entry_open_at'][:10] for p in existing),default=None)
            if last and last in days and days.index(future[0]['date'])-days.index(last)<20:continue
            archive(c,'equity_target',POLICY['version']+':'+rule+':'+future[0]['date'],dict(rule=rule,
                entry_open_at=future[0]['open'],weights=weights(rule,history)))
    return dict(completed=scored,verdict='INSUFFICIENT_FRESH_EVIDENCE',nonoverlap_sessions=20)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output');args=parser.parse_args()
    out=Path(args.output) if args.output else ROOT/'research/hub-evidence';out.mkdir(parents=True,exist_ok=True)
    import fcntl
    lock=(out/'run.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    c=sqlite3.connect(out/'journal.sqlite')
    c.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,observed TEXT,payload TEXT,PRIMARY KEY(kind,key))')
    frozen=json.dumps(POLICY,sort_keys=True)
    old=c.execute("SELECT payload FROM records WHERE kind='policy' AND key=?",(POLICY['version'],)).fetchone()
    if old and json.loads(old[0])!=POLICY:raise RuntimeError('Frozen protocol changed without version bump')
    archive(c,'policy',POLICY['version'],POLICY)
    from dotenv import dotenv_values
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from universe import paged
    v=dotenv_values(Path('/home/northworld/trading/nwt_agents/.env'))
    if v['NWT_ALPACA_BASE_URL'].rstrip('/')!='https://paper-api.alpaca.markets':raise RuntimeError('Paper-only research')
    headers={'APCA-API-KEY-ID':v['NWT_ALPACA_KEY_ID'],'APCA-API-SECRET-KEY':v['NWT_ALPACA_SECRET_KEY']}
    session=requests.Session();session.mount('https://',HTTPAdapter(max_retries=Retry(total=2,backoff_factor=1,status_forcelist=[429,500,502,503,504],allowed_methods=['GET'],respect_retry_after_header=False)))
    def get(url,params):
        r=session.get(url,params=params,headers=headers,timeout=25);r.raise_for_status();return r.json()
    now=datetime.now(timezone.utc);day=now.date().isoformat()
    calendar=get('https://paper-api.alpaca.markets/v2/calendar',dict(start='2026-09-01',end=(now+timedelta(days=45)).date().isoformat()))
    from event_core import sessions,dt
    completed=[s['date'] for s in sessions(calendar) if dt(s['close'])+timedelta(minutes=15)<=now]
    last_complete=completed[-1]
    cache=out/('bars-'+day+'.json')
    data=json.loads(cache.read_text()) if cache.exists() else {}
    if not data or any(not data.get(s) or data[s][-1]['t'][:10]!=last_complete for s in SYMBOLS):
        data=paged(get,'https://data.alpaca.markets/v2/stocks/bars',dict(symbols=','.join(SYMBOLS),timeframe='1Day',start='1993-01-01',end=now.isoformat(),feed='sip',adjustment='all',limit=10000),'bars',30)['bars']
        data={s:[b for b in rows if b['t'][:10]<=last_complete] for s,rows in data.items()}
        if any(not data.get(s) or data[s][-1]['t'][:10]!=last_complete for s in SYMBOLS):raise ValueError('Latest complete session missing')
        atomic(cache,data)
    probe_key=now.strftime('%G-W%V')
    prior_probe=c.execute("SELECT payload FROM records WHERE kind='access_probe' AND key=?",(probe_key,)).fetchone()
    if prior_probe:probe=json.loads(prior_probe[0])
    else:
        response=session.get('https://data.alpaca.markets/v1beta1/options/quotes',headers=headers,
            params=dict(symbols='SPY261016P00750000',start='2026-10-02T14:00:00Z',end='2026-10-02T14:01:00Z',feed='opra',limit=1),timeout=25)
        probe=dict(endpoint='/v1beta1/options/quotes',http_status=response.status_code,observed_at=now.isoformat(),
            interpretation='Endpoint unavailable' if response.status_code==404 else 'Further coverage audit required; access is not complete historical coverage')
        archive(c,'access_probe',probe_key,probe)
    inventory=dict(observed_at=now.isoformat(),stock_coverage={s:dict(rows=len(data[s]),first=data[s][0]['t'],last=data[s][-1]['t']) for s in SYMBOLS},
        requested_start='1993-01-01',delisting_complete_universe=False,historical_event_consensus=False,
        historical_option_quote_probe=probe,
        sources=['https://docs.alpaca.markets/us/docs/historical-option-data'],additional_purchases=0)
    frames=option_frames(Path('/home/northworld/trading/research/discovery-evidence'))
    inventory['option_archive_frames']=len(frames)
    equities=equity_review(data);options=options_review(c,frames,calendar,data)
    ai=ai_review(Path('/home/northworld/trading/research/event-evidence/events.sqlite'))
    forward=forward_equities(c,data,calendar,now)
    from strategy_lab import run as run_lab
    lab=run_lab(out/'strategy-lab',data,calendar,now)
    from broader_research import run as run_broader
    broader=run_broader(out/'broader',get,calendar,now,last_complete)
    from rehearsal import update as rehearse
    broader_bars=json.loads((out/'broader'/('bars-'+day+'.json')).read_text())
    rehearsal=rehearse(out/'rehearsal',[out/'strategy-lab',out/'broader/stocks',out/'broader/macro'],dict(data,**broader_bars),calendar,now)
    assessments=[assess(c,'bull_put',[(t['entry_day'],t['capital_risk_return']) for t in options['completed'] if t['prospective']],30,15)]
    assessments.extend(assess(c,r,[(t['entry'],t['net']-t['benchmark']) for t in forward['completed'] if t['rule']==r],12,12) for r in RULES if r!='passive_spy')
    assessments.append(assess(c,'ai_increment',[(t['event_id'],t['incremental_return']) for t in ai.get('observations',[]) if not t['short_borrow_missing']],30,30))
    report=dict(observed_at=now.isoformat(),version=POLICY['version'],status='OK',execution_enabled=False,assessments=assessments,
        inventory=inventory,equities=equities,bull_put=options,ai=ai,forward_equities=forward,strategy_lab=lab,
        broader=broader,rehearsal=rehearsal,verdict='NO_STRATEGY_APPROVED_FOR_LIVE_CAPITAL',
        prospective_start=c.execute("SELECT observed FROM records WHERE kind='policy' AND key=?",(POLICY['version'],)).fetchone()[0],policy=POLICY,data_sha256=hashlib.sha256(cache.read_bytes()).hexdigest(),
        code_sha256=hashlib.sha256((ROOT/'research/research_engine.py').read_bytes()).hexdigest())
    archive(c,'review',now.isoformat(),report);atomic(out/'latest.json',report)
    if now.weekday()==4:atomic(out/'weekly-latest.json',report)
    c.close()
    print(json.dumps(dict(status=report['status'],inventory=inventory,equities=equities,
        option_completed=len(options['completed']),option_net=options['net_model_dollars'],ai_pairs=ai['pairs'],verdict=report['verdict'])))


if __name__=='__main__':main()
