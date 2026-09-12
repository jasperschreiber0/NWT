"""CHINA-SUPPORTED-OBS-v1: supported data, no trade candidates or tickets."""
import json,sys
from pathlib import Path
from datetime import datetime,timedelta,timezone
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'research'))
from event_core import connect,put,quote_check,sessions,dt,stamp
SUPPORTED=['FXI','KWEB','MCHI','BABA']

def assess(bars,quotes,assets,calendar,now,is_open):
    completed=[s['date'] for s in sessions(calendar) if dt(s['close'])<now]
    expected=completed[-1] if completed else None;out={};momentum={}
    for symbol in SUPPORTED:
        b=[x for x in bars.get(symbol,[]) if x['t'][:10] in completed]
        b.sort(key=lambda x:x['t']);status=quote_check(quotes.get(symbol),now)
        history_ok=len(b)>=6 and b[-1]['t'][:10]==expected and all(float(x['c'])>0 for x in b[-6:])
        if history_ok:momentum[symbol]=float(b[-1]['c'])/float(b[-6]['c'])-1
        out[symbol]={'quote_status':status,'history_ok':history_ok,'five_session_return':momentum.get(symbol),
            'tradable':assets.get(symbol,{}).get('tradable') is True,'market_open':is_open,
            'latest_completed_session':b[-1]['t'][:10] if b else None}
    tailwind=all(s in momentum for s in ['FXI','KWEB']) and momentum['FXI']>.02 and momentum['KWEB']>.02
    for symbol,r in out.items():
        r['would_select']=bool(symbol in ['FXI','KWEB','MCHI'] and tailwind and r['history_ok'] and r['tradable'] and is_open and r['quote_status']=='OK')
        r['execution_enabled']=False
    return {'strategy_version':'CHINA-SUPPORTED-OBS-v1','symbols':out,'price_proxy_tailwind':tailwind,
        'broad_stimulus':None,'missing_evidence':{'TCEHY':'UNSUPPORTED_SIP_DATA; dependent broad-stimulus branch unavailable'},
        'actual_policy_announcement_verified':False,'execution_enabled':False}

def main():
    import requests
    from dotenv import dotenv_values
    v=dotenv_values(ROOT/'nwt_agents/.env');base=v['NWT_ALPACA_BASE_URL'].rstrip('/')
    if base!='https://paper-api.alpaca.markets':raise RuntimeError('Paper-only observer')
    h={'APCA-API-KEY-ID':v['NWT_ALPACA_KEY_ID'],'APCA-API-SECRET-KEY':v['NWT_ALPACA_SECRET_KEY']}
    now=datetime.now(timezone.utc);folder=ROOT/'research/event-evidence';folder.mkdir(parents=True,exist_ok=True)
    db=connect(folder/'events.sqlite');bars={};quotes={};assets={};errors={}
    def get(url,params=None):
        r=requests.get(url,headers=h,params=params,timeout=25);r.raise_for_status();return r.json()
    clock=get(base+'/v2/clock')
    calendar=get(base+'/v2/calendar',{'start':(now-timedelta(days=25)).date().isoformat(),'end':now.date().isoformat()})
    for symbol in SUPPORTED:
        try:
            assets[symbol]=get(base+'/v2/assets/'+symbol)
            quotes[symbol]=get('https://data.alpaca.markets/v2/stocks/'+symbol+'/quotes/latest',{'feed':'sip'}).get('quote')
            history=get('https://data.alpaca.markets/v2/stocks/'+symbol+'/bars',{'timeframe':'1Day','start':(now-timedelta(days=25)).isoformat(),'feed':'sip','adjustment':'all','limit':1000})
            if history.get('next_page_token'):raise RuntimeError('Incomplete history')
            bars[symbol]=history.get('bars') or []
        except Exception as exc:errors[symbol]=type(exc).__name__
    result=assess(bars,quotes,assets,calendar,now,clock.get('is_open') is True)
    result['errors']=errors;result['observed_at']=stamp()
    put(db,'china_observation',result['observed_at'],result)
    (folder/'china-latest.json').write_text(json.dumps(result,indent=2));db.close()
    print(json.dumps(result))

if __name__=='__main__':main()
