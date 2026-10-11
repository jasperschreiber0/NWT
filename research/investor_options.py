"""Contemporaneous equity/vertical-spread comparisons for frozen investor forecasts.

Models only. Broker GET access; no tickets, orders, positions or cash mutation.
"""
import math
from datetime import datetime,timedelta,timezone
from option_theories import choose,value
from event_core import quote_check
from investor_core import stamp,digest

VERSION='investor-option-comparison-v1'

def choose_book(chain,quote,forecast,observed):
    if quote_check(quote,observed,max_age=120,max_spread=.01)!='OK':raise ValueError('Equity quote unavailable or untradeable')
    spot=(float(quote['bp'])+float(quote['ap']))/2
    minimum=datetime.fromisoformat(forecast['exit']).date()+timedelta(days=7)
    filtered={}
    for symbol,snapshot in chain.items():
        try:expiry=datetime.strptime(symbol[-15:-9],'%y%m%d').date()
        except ValueError:continue
        if expiry>=minimum:filtered[symbol]=snapshot
    frame=dict(chain=filtered,spot=spot,observed=observed);spreads=[]
    for structure in ['bull_put','bull_call']:
        for width in [1,2]:
            pair=choose(structure,width,frame)
            if pair:
                initial=value(pair,filtered,observed)
                spreads.append(dict(structure=structure,width=width,pair=pair,entry=initial))
    ask=float(quote['ap']);qty=math.floor(500/(ask*1.0015))
    return dict(forecast_key=forecast['key'],entry_observed_at=observed.isoformat(),spreads=spreads,
        option_availability='ELIGIBLE_SPREADS' if spreads else 'NO_ELIGIBLE_SPREAD',
        equity=dict(ask=ask,quantity=qty,capital=qty*ask*1.0015),
        exposure_note='Equity <=$500 entry cost; each spread <=$100 contractual max-loss estimate plus assumed fees. Different exposure; no account-return comparison.',
        source_sha256=digest(dict(chain=filtered,quote=quote)))

def close_book(book,chain,quote,observed):
    if quote_check(quote,observed,max_age=120,max_spread=.01)!='OK':return None
    results=[]
    for spread in book['spreads']:
        final=value(spread['pair'],chain,observed,False)
        if final is None:return None
        results.append(dict(structure=spread['structure'],width=spread['width'],
            net_model_dollars=(spread['entry']['cashflow']+final['cashflow'])*100-2.60,
            max_loss=spread['entry']['max_loss_with_assumed_fees']))
    eq=book['equity'];net=eq['quantity']*(float(quote['bp'])*.9985-eq['ask']*1.0015)
    return dict(exit_observed_at=observed.isoformat(),spreads=results,equity_model_dollars=net,
        equity_quantity=eq['quantity'],source_sha256=digest(dict(chain=chain,quote=quote)),
        label='Contemporaneous BBO estimates, not fills. Assignment/early exercise not modeled. No proven option advantage.')

def run(c,paper,now,rows,one,put):
    forecasts=rows(c,'forecast');processed=0;errors=[]
    for pred in sorted(forecasts,key=lambda p:(p['entry'],p['key'])):
        key=pred['key'];book=one(c,'option_book',key);ending=one(c,'option_result',key)
        if ending:continue
        at=stamp(pred['exit_at'] if book else pred['entry_at'])
        if now<at:continue
        if now>at+timedelta(minutes=20):
            put(c,'option_result',key,dict(state='UNPRICED_EXIT' if book else 'MISSED_ENTRY',
                reason='No contemporaneous comparison within the frozen 20-minute observation window',observed_at=now.isoformat()))
            continue
        if processed>=3:break
        processed+=1
        try:
            q=paper.get('/v2/stocks/'+pred['symbol']+'/quotes/latest',dict(feed='sip'),data=True)['quote']
            params=dict(feed='opra',limit=1000,root_symbol=pred['symbol'])
            if book:
                contracts=sorted({s['pair'][leg] for s in book['spreads'] for leg in ['short','long']})
                chain={}
                if contracts:
                    response=paper.get('/v1beta1/options/quotes/latest',dict(symbols=','.join(contracts),feed='opra'),data=True)
                    chain={s:dict(latestQuote=v) for s,v in response.get('quotes',{}).items()}
            else:
                params.update(expiration_date_gte=(datetime.fromisoformat(pred['exit']).date()+timedelta(days=7)).isoformat(),
                    expiration_date_lte=(now.date()+timedelta(days=45)).isoformat(),
                    strike_price_gte=str(round(float(q['bp'])*.94,2)),strike_price_lte=str(round(float(q['ap'])*1.02,2)))
                chain={};token=None;seen=set()
                for _ in range(4):
                    if token:params['page_token']=token
                    response=paper.get('/v1beta1/options/snapshots/'+pred['symbol'],params,data=True)
                    chain.update(response.get('snapshots') or {});token=response.get('next_page_token')
                    if not token:break
                    if token in seen:raise ValueError('Repeated option pagination')
                    seen.add(token)
                if token:raise ValueError('Option chain exceeds research budget')
            observed=datetime.now(timezone.utc)
            if observed>at+timedelta(minutes=20):continue
            if book:
                closed=close_book(book,chain,q,observed)
                if closed:
                    put(c,'option_exit_quote_evidence',key,dict(chain=chain,quote=q,observed_at=observed.isoformat()))
                    put(c,'option_result',key,dict(closed,state='COMPLETE'))
            else:
                entry=choose_book(chain,q,pred,observed)
                put(c,'option_quote_evidence',key,dict(chain=chain,quote=q,observed_at=observed.isoformat()))
                put(c,'option_book',key,dict(entry,version=VERSION))
        except Exception as e:
            errors.append(dict(forecast=key,error=type(e).__name__))
    result=rows(c,'option_result')
    books=rows(c,'option_book')
    return dict(version=VERSION,books=len(books),books_with_options=sum(bool(b['spreads']) for b in books),
        no_eligible_spread=sum(not b['spreads'] for b in books),completed=sum(r['state']=='COMPLETE' for r in result),
        missed_entries=sum(r['state']=='MISSED_ENTRY' for r in result),unpriced_exits=sum(r['state']=='UNPRICED_EXIT' for r in result),
        current_errors=errors,execution_enabled=False)
