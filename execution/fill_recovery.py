"""Record exact delayed entry fills. This module has no broker write operation."""
import json
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode
import requests
from psycopg2.extras import RealDictCursor
from ledger import insert_position
from decision_store import write_execution_decision

TERMINAL = {'filled', 'canceled', 'expired', 'rejected'}


def fill_data(ticket, order):
    p=ticket['payload']; ident=str(ticket['ticket_id'])
    if order.get('client_order_id') != 'nwt-entry-'+ident:
        raise ValueError('Broker client order identity mismatch')
    if p.get('legs') or order.get('order_class')=='mleg':
        raise ValueError('Multi-leg fill recovery needs per-leg attribution')
    asset_type=p.get('asset_type')
    if asset_type not in ('equity','option'):raise ValueError('Unknown asset type')
    symbol=p.get('option_symbol',p.get('symbol')) if asset_type=='option' else p.get('symbol')
    side='buy' if asset_type=='option' or p.get('direction')=='long' else 'sell'
    if order.get('symbol')!=symbol or order.get('side')!=side:
        raise ValueError('Order instrument or side does not match original ticket')
    qty=Decimal(str(order.get('filled_qty') or 0)); requested=Decimal(str(order.get('qty') or 0))
    price=Decimal(str(order.get('filled_avg_price') or 0))
    if not all(x.is_finite() for x in (qty,requested,price)) or not 0<qty<=requested or price<=0:
        raise ValueError('Invalid broker fill quantities or price')
    if order['status']=='filled' and qty!=requested:raise ValueError('Incomplete filled order')
    if not order.get('filled_at'):raise ValueError('Broker fill timestamp unavailable')
    stamp=datetime.fromisoformat(order['filled_at'].replace('Z','+00:00'))
    if stamp.tzinfo is None:raise ValueError('Fill timestamp needs timezone')
    direction='long' if side=='buy' else 'short'
    return dict(bot_source=p['bot_source'],strategy_id=p['strategy_id'],ticket_id=ident,
        asset=symbol,asset_type=asset_type,direction=direction,
        delta_exposure=(.5 if p.get('option_type','call')=='call' else -.5) if asset_type=='option' else (1 if direction=='long' else -1),
        notional_risk=qty*price*(100 if asset_type=='option' else 1),qty=qty,entry_price=price,
        entry_time=stamp,alpaca_order_id=order['id'],
        stop_pct=p.get('stop_pct',(p.get('expected_payoff') or {}).get('stop_pct')),
        target_pct=p.get('target_pct',(p.get('expected_payoff') or {}).get('target_pct')))


def recover_entries(conn, get, *, apply=True):
    with conn.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("SELECT t.* FROM nwt_entry_intents i JOIN nwt_tickets t ON t.ticket_id=i.ticket_id "
                  "WHERE NOT EXISTS(SELECT 1 FROM nwt_ticket_decisions d WHERE d.ticket_id=t.ticket_id "
                  "AND d.decided_by='EXECUTION_ENGINE' AND (d.decision='EXECUTED' OR d.reasoning='BROKER_TERMINAL_NO_FILL')) ORDER BY t.created_at")
        tickets=q.fetchall()
    result={'recorded':[], 'pending':[], 'absent':0}
    for ticket in tickets:
        tid=str(ticket['ticket_id'])
        try:order=get('/orders:by_client_order_id?'+urlencode({'client_order_id':'nwt-entry-'+tid}))
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code==404:
                result['absent']+=1;continue
            raise
        if ticket['payload'].get('legs') and not order.get('legs'):
            order=get('/orders/'+order['id']+'?nested=true')
        if order.get('client_order_id') != 'nwt-entry-'+tid:
            raise ValueError('Broker client order identity mismatch')
        terminal = order.get('status') in TERMINAL
        if not terminal:
            result['pending'].append(tid)
            if not any(Decimal(str(x.get('filled_qty') or 0)) > 0 for x in (order.get('legs') or [order])):
                continue
        if not any(Decimal(str(x.get('filled_qty') or 0)) > 0 for x in (order.get('legs') or [order])):
            if apply:
                write_execution_decision(conn,tid,'FAILED','BROKER_TERMINAL_NO_FILL')
                conn.commit()
            continue
        from fill_evidence import with_fill_timestamp
        if order.get('legs'):
            order=dict(order,legs=[with_fill_timestamp(x,get) for x in order['legs']])
        else:
            order=with_fill_timestamp(order,get)
        from verified_fills import entry_rows
        records=entry_rows(ticket,order)
        if apply:
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as q:
                    q.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(tid,))
                    q.execute('SELECT * FROM nwt_portfolio_ledger WHERE ticket_id=%s OR alpaca_order_id=%s',(tid,order['id']))
                    existing=q.fetchall()
                    groups={str(x['spread_group_id']) for x in existing if x.get('spread_group_id')}
                    if len(groups)>1:raise ValueError('Conflicting spread group attribution')
                    if groups:
                        for data in records:data['spread_group_id']=next(iter(groups))
                    for data in records:
                        matched=[x for x in existing if x['asset']==data['asset']]
                        if len(matched)>1:
                            raise ValueError('Duplicate existing leg attribution')
                        if matched:
                            old=matched[0]
                            if str(old['ticket_id'])!=tid or str(old['alpaca_order_id'])!=order['id'] or old['direction']!=data['direction']:
                                raise ValueError('Existing ledger attribution conflicts with fill')
                            if Decimal(str(old['qty']))==data['qty'] and Decimal(str(old['entry_price']))==data['entry_price']:
                                continue
                            if old['status']!='open' or Decimal(str(old['qty']))>data['qty']:
                                raise ValueError('Cannot overwrite closed or reduced entry quantity')
                            q.execute('UPDATE nwt_portfolio_ledger SET qty=%s,entry_price=%s,notional_risk=%s WHERE position_id=%s',
                                      (data['qty'],data['entry_price'],data['notional_risk'],old['position_id']))
                        else:
                            insert_position(conn,data,commit=False)
                    if set(x['asset'] for x in existing)-set(x['asset'] for x in records):
                        raise ValueError('Existing ledger has unverified legs')
                    q.execute("SELECT decision,reasoning FROM nwt_ticket_decisions WHERE ticket_id=%s AND decided_by='EXECUTION_ENGINE'",(tid,))
                    previous=q.fetchone()
                    write_execution_decision(conn,tid,'EXECUTED' if terminal else 'SUBMITTED','Verified cumulative broker fill '+order['id'])
                    origin=ticket['payload'].get('source_proposal_ticket_id') or tid
                    if terminal: q.execute("UPDATE nwt_decision_inputs SET outcome_reason='EXECUTED',stage_reached='EXECUTION' WHERE ticket_id=%s AND (outcome_reason IS NULL OR outcome_reason='EXECUTION_FAILED')",(str(origin),))
                    q.execute("INSERT INTO nwt_system_log(level,component,message,payload) VALUES ('INFO','fill_recovery','Recorded verified delayed fill',%s)",
                              (json.dumps({'ticket_id':tid,'order_id':order['id'],'qty':str(data['qty']),'price':str(data['entry_price']),'filled_at':order.get('filled_at'),'legs':[{'asset':x['asset'],'qty':str(x['qty']),'price':str(x['entry_price'])} for x in records],'previous_decision':dict(previous) if previous else None}),))
                conn.commit()
            except Exception:
                conn.rollback();raise
        result['recorded'].extend({'ticket_id':tid,'order_id':order['id'],'symbol':data['asset'],'qty':str(data['qty']),'price':str(data['entry_price'])} for data in records)
    return result


if __name__=='__main__':
    import argparse,os,psycopg2
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    base=os.environ['NWT_ALPACA_BASE_URL'].rstrip('/')
    if base!='https://paper-api.alpaca.markets':raise RuntimeError('Paper account required')
    headers={'APCA-API-KEY-ID':os.environ['NWT_ALPACA_KEY_ID'],'APCA-API-SECRET-KEY':os.environ['NWT_ALPACA_SECRET_KEY']}
    def get(path):
        response=requests.get(base+'/v2'+path,headers=headers,timeout=20);response.raise_for_status();return response.json()
    with psycopg2.connect(os.environ['NWT_DB_DSN']) as conn:
        print(json.dumps(recover_entries(conn,get,apply=args.apply)))
