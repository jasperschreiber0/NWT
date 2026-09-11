"""Append-only research evidence. Never submits orders or changes trading flags."""
import hashlib,json,os,statistics,subprocess
from pathlib import Path
from datetime import datetime,timedelta,timezone

ROOT=Path('/home/northworld/trading')
OUT=ROOT/'research/evidence'
SYMBOLS=['SPY','QQQ','GLD','TLT','XLE','VGK','SAFX']

def encode(value):return json.dumps(value,sort_keys=True,default=str,separators=(',',':'))

def archive(kind,key,payload):
    record={'kind':kind,'key':str(key),'payload':payload}
    digest=hashlib.sha256(encode(record).encode()).hexdigest()
    folder=OUT/kind;folder.mkdir(parents=True,exist_ok=True)
    path=folder/(digest+'.json')
    # Exclusive creation preserves the first observation time on repeated sweeps.
    try:
        with path.open('x') as f:
            json.dump(dict(record,observed_at=datetime.now(timezone.utc).isoformat()),f,default=str)
    except FileExistsError:pass
    return digest

def fixed_signals(closes,symbol):
    if len(closes)<200:return {'status':'INSUFFICIENT_HISTORY'}
    mean=statistics.mean(closes[-20:]);sd=statistics.pstdev(closes[-20:])
    z=(closes[-1]-mean)/sd if sd else 0
    trend=closes[-1]>statistics.mean(closes[-200:])
    return {'status':'SHADOW_ONLY','trend200_v1':trend,
            'etf_dip_trend_v1':bool(symbol in ('SPY','QQQ') and z < -1 and trend),
            'safx_bounce20_v1':bool(symbol=='SAFX' and z < -1),
            'z20':z,'close':closes[-1],
            'options_spread_v1':'NOT_ELIGIBLE_WITHOUT_POINT_IN_TIME_CHAIN_AND_COST_TEST',
            'execution_enabled':False}

def evaluate_observed_signals(data):
    """Forward underlying returns only; never labels these as option or broker P&L."""
    for path in (OUT/'frozen_signals').glob('*.json'):
        record=json.loads(path.read_text());p=record['payload']
        if p.get('stale_session') or not p.get('bar_timestamp'):continue
        symbol=record['key'].split(':')[-1];bars=data['bars'].get(symbol,[])
        dates=[b['t'][:10] for b in bars]
        if p['bar_timestamp'][:10] not in dates:continue
        entry=dates.index(p['bar_timestamp'][:10])+1
        if entry>=len(bars) or record['observed_at']>=bars[entry]['t']:continue
        for rule,horizon in [('trend200_v1',20),('etf_dip_trend_v1',5),('safx_bounce20_v1',5)]:
            if not p.get(rule) or entry+horizon>=len(bars):continue
            a=float(bars[entry]['o']);b=float(bars[entry+horizon]['o']);cost=.005 if symbol=='SAFX' else .0005
            archive('forward_underlying_outcomes',path.stem+':'+rule,dict(
                signal_id=path.stem,symbol=symbol,rule=rule,horizon_sessions=horizon,
                entry_session=dates[entry],exit_session=dates[entry+horizon],entry_open=a,exit_open=b,
                assumed_cost_per_side=cost,return_pct=100*((b/a)*(1-cost)**2-1),
                label='FIXED_HORIZON_UNDERLYING_PROXY_NOT_FULL_STRATEGY_OR_OPTIONS_PNL',broker_fills=False))

def main():
    from dotenv import dotenv_values
    import requests,psycopg2
    from psycopg2.extras import RealDictCursor
    now=datetime.now(timezone.utc);v=dotenv_values(ROOT/'nwt_agents/.env')
    h={'APCA-API-KEY-ID':v['NWT_ALPACA_KEY_ID'],'APCA-API-SECRET-KEY':v['NWT_ALPACA_SECRET_KEY']}
    version=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    c=psycopg2.connect(v['NWT_DB_DSN']);c.set_session(readonly=True)
    counts={};contracts=set();ledger_rows=[]
    try:
        with c.cursor(cursor_factory=RealDictCursor) as q:
            queries={
                'tickets':("SELECT * FROM nwt_tickets WHERE created_at >= %s",(now-timedelta(days=14),)),
                'decisions':("SELECT * FROM nwt_ticket_decisions WHERE created_at >= %s",(now-timedelta(days=14),)),
                'ledger':('SELECT * FROM nwt_portfolio_ledger',()),
                'outcomes':('SELECT * FROM nwt_trade_outcomes',()),
                'decision_inputs':('SELECT * FROM nwt_decision_inputs',()),
                'genomes':('SELECT * FROM nwt_strategy_genome',()),
                'flags':('SELECT * FROM nwt_system_flags',()),
            }
            for kind,(sql,args) in queries.items():
                q.execute(sql,args);rows=[dict(r) for r in q.fetchall()];counts[kind]=len(rows)
                if kind=='ledger':ledger_rows=rows
                for row in rows:
                    key=row.get('id') or row.get('ticket_id') or row.get('position_id') or row.get('strategy_id') or row.get('flag')
                    archive(kind,key,row)
                    if kind=='tickets':
                        p=row.get('payload') or {}
                        if isinstance(p,dict):
                            for candidate in [p.get('option_symbol')]+[x.get('option_symbol') for x in p.get('legs',[]) if isinstance(x,dict)]:
                                if candidate:contracts.add(candidate)
    finally:c.close()
    def get(url,params):
        x=requests.get(url,headers=h,params=params,timeout=30)
        if not x.ok:return {'http_status':x.status_code,'available':False}
        return x.json()
    market=get('https://data.alpaca.markets/v2/stocks/snapshots',{'symbols':','.join(SYMBOLS),'feed':'sip'})
    archive('stock_snapshots',now.isoformat(),{'feed_requested':'sip','data':market,'code_version':version})
    broker_base=v['NWT_ALPACA_BASE_URL'].rstrip('/')
    if broker_base!='https://paper-api.alpaca.markets':raise RuntimeError('Collector configured for paper account only')
    orders=get(broker_base+'/v2/orders',{'status':'all','after':(now-timedelta(days=14)).isoformat(),'limit':500,'nested':'true'})
    archive('broker_orders',now.isoformat(),{'data':orders,'possibly_truncated':isinstance(orders,list) and len(orders)==500})
    archive('broker_positions',now.isoformat(),get(broker_base+'/v2/positions',{}))
    account=get(broker_base+'/v2/account',{})
    if 'equity' in account:
        equity=float(account['equity'])
        exposure={side:sum(float(p.get('notional_risk') or 0) for p in ledger_rows
                          if p['status']=='open' and p.get('direction')==side) for side in ('long','short')}
        archive('entry_capacity',now.isoformat(),dict(equity=equity,cash=account.get('cash'),
            directional_cap=.6*equity,ledger_exposure=exposure,
            remaining_before_pending_orders={side:max(0,.6*equity-value) for side,value in exposure.items()},
            advisory_only=True,reserves_capital=False))
    # Never substitute indicative quotes for an unavailable OPRA feed.
    if contracts:
        chain=get('https://data.alpaca.markets/v1beta1/options/snapshots',{'symbols':','.join(sorted(contracts)[:100]),'feed':'opra'})
        archive('option_snapshots',now.isoformat(),{'feed_requested':'opra','contracts_requested':sorted(contracts)[:100],
                'truncated':len(contracts)>100,'data':chain,'code_version':version})
    # One full daily dataset per UTC day after the regular close, independent of no_trade_mode.
    marker=OUT/('daily-'+now.date().isoformat()+'.json')
    if now.hour>=21 and not marker.exists():
        params={'symbols':','.join(SYMBOLS),'timeframe':'1Day','start':(now-timedelta(days=400)).isoformat(),
                'end':now.isoformat(),'feed':'sip','adjustment':'all','limit':10000}
        data=get('https://data.alpaca.markets/v2/stocks/bars',params)
        if 'bars' not in data or data.get('next_page_token'):
            raise RuntimeError('Daily history unavailable or incomplete; no new research signal recorded')
        archive('daily_bars',now.date(),dict(parameters=params,data=data))
        evaluate_observed_signals(data)
        signals={}
        for symbol in SYMBOLS:
            bars=data['bars'].get(symbol,[])
            signals[symbol]=fixed_signals([float(b['c']) for b in bars],symbol)
            signals[symbol]['bar_timestamp']=bars[-1]['t'] if bars else None
            signals[symbol]['stale_session']=not bars or bars[-1]['t'][:10]!=now.date().isoformat()
            archive('frozen_signals',str(now.date())+':'+symbol,dict(signals[symbol],code_version=version,
                research_only=True,prospective_execution=False,rule_version='20260912-v1'))
        marker.parent.mkdir(parents=True,exist_ok=True)
        with marker.open('x') as f:json.dump(signals,f)
    summary={'observed_at':now.isoformat(),'counts':counts,'code_version':version,
             'market_data_available':'available' not in market,'research_execution_enabled':False}
    OUT.mkdir(parents=True,exist_ok=True);tmp=OUT/'latest.tmp';tmp.write_text(json.dumps(summary,indent=2));os.replace(tmp,OUT/'latest.json')
    print(json.dumps(summary))

if __name__=='__main__':main()
