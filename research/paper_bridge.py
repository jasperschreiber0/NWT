"""Durable qualified-research proposals; only execution/engine.py sends orders."""
import hashlib
import json
import math
import sqlite3
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ops'))
from paper_io import Paper, STATE, atomic, stamp

LAB=ROOT/'research/hub-evidence/strategy-lab'
LABS={'core':LAB,'stocks':ROOT/'research/hub-evidence/broader/stocks','macro':ROOT/'research/hub-evidence/broader/macro'}
LABS['theories']=ROOT/'research/hub-evidence/theories'
SOURCE='NWT_RESEARCH_BRIDGE'
POLICY=dict(version='paper-bridge-20261005-v1',entry_cap=500.,strategy_cap=1000.,total_cap=3000.,
    loss_budget=100.,stop=.03,entry_window_minutes=30,max_quote_age=90,max_spread=.005,
    promotion='Qualified prospective model AND completed operational trial; paper equities only')


def identity(experiment):
    return 'LAB-'+hashlib.sha256(experiment.encode()).hexdigest()[:16]


def ticket_id(experiment,day,symbol):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,'nwt-paper-lab:'+experiment+':'+day+':'+symbol))


def evidence(now,folder=LAB):
    report=json.loads((folder/'latest.json').read_text())
    age=(now-stamp(report['observed_at'])).total_seconds()
    if not 0<=age<=30*3600 or report.get('status')!='OK' or report.get('execution_enabled') is not False:
        raise ValueError('Research evidence stale or invalid')
    c=sqlite3.connect('file:'+str(folder/'lab.sqlite')+'?mode=ro',uri=True)
    cohorts=[json.loads(x[0]) for x in c.execute("SELECT payload FROM records WHERE kind='cohort' ORDER BY key")]
    verdicts={k:json.loads(p) for k,p in c.execute("SELECT key,payload FROM records WHERE kind='verdict'")}
    demoted={k for k, in c.execute("SELECT key FROM records WHERE kind='demotion'")}
    c.close()
    return report,cohorts,verdicts,demoted


def eligible(experiment,cohort,report,verdicts,demoted,now):
    if experiment in demoted or verdicts.get(experiment,{}).get('state')!='PAPER_QUALIFIED':return False
    if report.get('hypotheses',{}).get(experiment,{}).get('verdict',{}).get('state')!='PAPER_QUALIFIED':return False
    if not 0<cohort.get('allocation',{}).get(experiment,0)<=.2:return False
    if not 0<report.get('allocation',{}).get(experiment,0)<=.2:return False
    opening=stamp(cohort['entry_at'])
    return stamp(cohort['frozen_at'])<opening and opening<=now<opening+timedelta(minutes=POLICY['entry_window_minutes'])


def quote_order(quote,budget,now):
    bid=float(quote['bp']);ask=float(quote['ap']);age=(now-stamp(quote['t'])).total_seconds()
    if not all(math.isfinite(x) for x in [bid,ask,budget]) or not 0<bid<=ask or not 0<budget<=POLICY['entry_cap']:
        raise ValueError('Invalid quote or budget')
    if not 0<=age<=POLICY['max_quote_age'] or (ask-bid)/ask>POLICY['max_spread']:
        raise ValueError('Stale, future or wide quote')
    if float(quote.get('bs',0))<1 or float(quote.get('as',0))<1:raise ValueError('Quote has no displayed size')
    limit=math.ceil(ask*1.001*100)/100;qty=math.floor(budget/limit)
    if qty<1:raise ValueError('Whole share exceeds paper entry budget')
    return qty,limit


def feedback(rows):
    """Only complete broker-linked outcomes adjust an allocation; missing data blocks."""
    if any(r.get('status')=='closed' and r.get('pnl_adjusted') is None for r in rows):
        return dict(scale=0.,reason='Missing realized attribution')
    closed=[r for r in rows if r.get('status')=='closed']
    net=sum(float(r['pnl_adjusted']) for r in closed)
    if net<=-POLICY['loss_budget']:return dict(scale=0.,reason='Paper loss budget exhausted',net=net)
    if len(closed)<5:return dict(scale=1.,reason='Initial bounded paper validation',net=net)
    recent=sorted(closed,key=lambda r:str(r.get('exit_time')))[-10:]
    net_recent=sum(float(r['pnl_adjusted']) for r in recent)
    scale=max(0.,min(1.,1+net_recent/POLICY['loss_budget']))
    return dict(scale=scale,reason='Recent actual adjusted fills',net=net,recent_net=net_recent)


def ledger_rows(conn,strategy=None):
    from psycopg2.extras import RealDictCursor
    with conn.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("SELECT l.*,o.pnl_adjusted FROM nwt_portfolio_ledger l LEFT JOIN nwt_trade_outcomes o ON o.position_id=l.position_id "
            "WHERE l.bot_source='RESEARCH_LAB'"+(' AND l.strategy_id=%s' if strategy else ''),(strategy,) if strategy else ())
        return [dict(r) for r in q.fetchall()]


def exposure(conn,exclude=None):
    from psycopg2.extras import RealDictCursor
    rows=ledger_rows(conn); amounts={}
    for r in rows:
        if r['status'] in ('open','suspect'):
            amounts[r['strategy_id']]=amounts.get(r['strategy_id'],0)+float(r['qty'])*float(r['entry_price'])
    with conn.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("SELECT t.ticket_id,t.payload FROM nwt_tickets t WHERE t.from_agent=%s AND t.type='TRADE_REQUEST' "
            "AND NOT EXISTS (SELECT 1 FROM nwt_ticket_decisions d WHERE d.ticket_id=t.ticket_id AND d.decided_by='EXECUTION_ENGINE' "
            "AND d.decision IN ('REJECTED','EXECUTED','FAILED','STRUCTURALLY_IMPOSSIBLE')) "
            "AND NOT EXISTS (SELECT 1 FROM nwt_portfolio_ledger l WHERE l.ticket_id=t.ticket_id)",(SOURCE,))
        for r in q.fetchall():
            if str(r['ticket_id'])==exclude:continue
            p=r['payload'];amounts[p['strategy_id']]=amounts.get(p['strategy_id'],0)+float(p['sized_notional'])
    return amounts


def symbol_in_use(conn,symbol,exclude):
    # Do not stack independently selected studies onto the same broker position.
    with conn.cursor() as q:
        q.execute("SELECT 1 FROM nwt_portfolio_ledger WHERE asset=%s AND status IN ('open','suspect') LIMIT 1",(symbol,))
        if q.fetchone():return True
        q.execute("SELECT 1 FROM nwt_tickets t WHERE t.from_agent=%s AND t.type='TRADE_REQUEST' AND t.payload->>'symbol'=%s "
            "AND t.ticket_id<>%s AND NOT EXISTS (SELECT 1 FROM nwt_ticket_decisions d WHERE d.ticket_id=t.ticket_id "
            "AND d.decided_by='EXECUTION_ENGINE' AND d.decision IN ('REJECTED','EXECUTED','FAILED','STRUCTURALLY_IMPOSSIBLE')) LIMIT 1",
            (SOURCE,symbol,exclude))
        return bool(q.fetchone())


def gate(conn,payload,base,now=None):
    """Independent final gate in the engine; returned settings cannot exceed source cohort."""
    now=now or datetime.now(timezone.utc)
    if base!='https://paper-api.alpaca.markets':raise ValueError('Research orders require paper endpoint')
    if payload.get('bot_source')!='RESEARCH_LAB' or payload.get('asset_type')!='equity' or payload.get('direction')!='long':
        raise ValueError('Only long research equities supported')
    trial=json.loads((STATE/'trial.json').read_text())
    if trial.get('status')!='PASSED':raise ValueError('Operational trial not passed')
    meta=payload['research'];key=meta['experiment'];symbol=payload['symbol'];study=meta['study']
    report,cohorts,verdicts,demoted=evidence(now,LABS[study])
    cohort=next(x for x in cohorts if x['entry']==meta['entry'])
    if not eligible(key,cohort,report,verdicts,demoted,now):raise ValueError('Research qualification or entry window unavailable')
    strategy=identity(study+':'+key)
    if (payload['strategy_id']!=strategy or meta['exit']!=cohort['exit'] or meta['policy']!=POLICY['version']
            or meta['source_hash']!=cohort['data_sha256']):
        raise ValueError('Research identity mismatch')
    amount=float(payload['sized_notional']);w=float(cohort['targets'][key].get(symbol,0))
    reduction=min(cohort['allocation'][key],report['allocation'][key])/.2
    scale=feedback(ledger_rows(conn,strategy))['scale']
    maximum=min(POLICY['entry_cap'],POLICY['strategy_cap']*w)*reduction*scale
    if not math.isfinite(amount) or not 0<amount<=maximum+.000001:raise ValueError('Research allocation exceeds current evidence')
    booked=exposure(conn,ticket_id(study+':'+key,cohort['entry'],symbol))
    if symbol_in_use(conn,symbol,ticket_id(study+':'+key,cohort['entry'],symbol)):
        raise ValueError('Symbol already held or reserved; cross-study concentration blocked')
    if booked.get(strategy,0)+amount>POLICY['strategy_cap'] or sum(booked.values())+amount>POLICY['total_cap']:
        raise ValueError('Research aggregate budget exceeded')
    if payload.get('time_in_force')!='day' or float(payload.get('stop_pct',0))!=POLICY['stop']:
        raise ValueError('Research lifecycle controls missing')
    return True


def insert_ticket(conn,identifier,kind,payload):
    with conn.cursor() as q:
        q.execute("INSERT INTO nwt_tickets(ticket_id,from_agent,to_agent,type,payload) VALUES (%s,%s,'EXECUTION_ENGINE',%s,%s::jsonb) "
                  "ON CONFLICT DO NOTHING RETURNING ticket_id",(identifier,SOURCE,kind,json.dumps(payload)))
        row=q.fetchone()
        if row and kind=='TRADE_REQUEST':
            decision=str(uuid.uuid5(uuid.NAMESPACE_URL,'nwt-research-decision:'+identifier))
            q.execute("INSERT INTO nwt_decision_inputs(id,run_date,symbol,strategy_id,track,ticket_id,decision,layer0_signals) "
                "VALUES (%s,CURRENT_DATE,%s,%s,'LAB',%s,'TRADE_PROPOSED',%s::jsonb) ON CONFLICT DO NOTHING",
                (decision,payload['symbol'],payload['strategy_id'],identifier,json.dumps(payload['research'])))
            sys.path.insert(0,str(ROOT/'nwt_agents'))
            from opportunity_outcomes import record_decision_outcome
            record_decision_outcome(conn,decision,commit=False)
    conn.commit();return bool(row)


def main():
    paper=Paper();now=datetime.now(timezone.utc);conn=paper.db();created=0;closures=0;notes=[]
    try:
        clock=paper.get('/v2/clock');studies={}
        for name,path in LABS.items():
            try:studies[name]=evidence(now,path)
            except (ValueError,OSError,sqlite3.Error):notes.append(name+': research evidence unavailable; entries held')
        rows=ledger_rows(conn)
        # Exits remain due even when research qualification is lost or entry gates fail.
        from psycopg2.extras import RealDictCursor
        for row in rows:
            if row['status']!='open':continue
            with conn.cursor(cursor_factory=RealDictCursor) as q:
                q.execute('SELECT payload FROM nwt_tickets WHERE ticket_id=%s',(str(row['ticket_id']),));source=q.fetchone()
            meta=source['payload']['research'];key=meta['experiment']
            due=now.astimezone(ZoneInfo('America/New_York')).date().isoformat()>=meta['exit']
            demoted=studies.get(meta.get('study','core'),({},[],{},set()))[3]
            suspended=key in demoted or feedback([r for r in rows if r['strategy_id']==row['strategy_id']])['scale']==0
            if due or suspended:
                identifier=str(uuid.uuid5(uuid.NAMESPACE_URL,'nwt-lab-close:'+str(row['position_id'])))
                closures+=insert_ticket(conn,identifier,'CLOSE_REQUEST',dict(position_id=str(row['position_id']),asset_type='equity',
                    symbol=row['asset'],strategy_id=row['strategy_id'],exit_reason='lab_time_exit' if due else 'lab_suspended'))
        if not clock.get('is_open'):notes.append('Market closed')
        elif json.loads((STATE/'trial.json').read_text()).get('status')!='PASSED':notes.append('Operational trial pending')
        else:
            account=paper.get('/v2/account');cash=float(account['cash'])
            if account.get('trading_blocked') or account.get('account_blocked'):raise ValueError('Broker blocks trading')
            for study,(report,cohorts,verdicts,demoted) in studies.items():
              for cohort in cohorts:
                for key in cohort['allocation']:
                    if not eligible(key,cohort,report,verdicts,demoted,now):continue
                    strategy=identity(study+':'+key);adjustment=feedback([r for r in rows if r['strategy_id']==strategy])
                    for symbol,w in cohort['targets'][key].items():
                        budget=round(min(POLICY['entry_cap'],POLICY['strategy_cap']*w)*adjustment['scale']*
                            min(cohort['allocation'][key],report['allocation'][key])/.2,2)
                        if budget<=0 or budget>cash:continue
                        p=dict(approved=True,bot_source='RESEARCH_LAB',strategy_id=strategy,symbol=symbol,direction='long',
                            sized_notional=budget,asset_type='equity',time_in_force='day',stop_pct=POLICY['stop'],target_pct=10.,
                            research=dict(study=study,experiment=key,entry=cohort['entry'],exit=cohort['exit'],policy=POLICY['version'],
                                source_hash=cohort['data_sha256'],hypothesis=report['hypotheses'][key]['spec']))
                        try:
                            gate(conn,p,paper.base,now)
                            quote=paper.get('/v2/stocks/'+symbol+'/quotes/latest',{'feed':'sip'},data=True)['quote']
                            qty,limit=quote_order(quote,budget,now)
                        except (ValueError,KeyError,StopIteration) as exc:
                            notes.append(symbol+': '+str(exc));continue
                        p['research'].update(decision_quote=quote,estimated_qty=qty,estimated_limit=limit)
                        if insert_ticket(conn,ticket_id(study+':'+key,cohort['entry'],symbol),'TRADE_REQUEST',p):created+=1;cash-=budget
        atomic(STATE/'paper-bridge.json',dict(observed_at=now.isoformat(),status='OK',created=created,closures=closures,
            notes=notes,policy=POLICY,qualified=sum(len(s[0]['allocation']) for s in studies.values()),paper_only=True))
        print(json.dumps(dict(created=created,closures=closures,notes=notes)))
    finally:conn.close()


if __name__=='__main__':main()
