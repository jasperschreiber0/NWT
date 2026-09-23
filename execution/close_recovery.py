"""Record cumulative verified closes and resume canceled residual quantities."""
import json
import uuid
from datetime import datetime
from decimal import Decimal
import requests
from psycopg2.extras import RealDictCursor
from ledger import close_position, insert_position
from decision_store import write_execution_decision


def close_client_id(position):
    generation=int(position.get('close_generation') or 0)
    pid=str(position['position_id'])
    return 'nwt-close-'+pid if generation==0 else 'nwt-c-'+str(uuid.uuid5(uuid.NAMESPACE_URL,pid+':'+str(generation)))


def validate_close_identity(position, order, requested=None):
    side='buy' if position['direction']=='short' else 'sell'
    if (order.get('client_order_id')!=close_client_id(position) or order.get('symbol')!=position['asset']
            or order.get('side')!=side or Decimal(str(order.get('qty') or 0))!=Decimal(str(requested if requested is not None else position['qty']))):
        raise ValueError('Close receipt does not match ledger identity, direction and quantity')


def recover_closes(conn,get):
    with conn.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("SELECT * FROM nwt_portfolio_ledger WHERE status='open'")
        positions=q.fetchall()
    result={'recorded':[],'pending':[],'residual_retries':[],'blocked':[]}
    for position in positions:
        pid=str(position['position_id']);generation=int(position.get('close_generation') or 0)
        try:order=get('/orders:by_client_order_id?client_order_id='+close_client_id(position))
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code==404:
                if position.get('close_requested'):result['pending'].append(pid)
                continue
            raise
        from fill_evidence import with_fill_timestamp
        order=with_fill_timestamp(order,get)
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as q:
                q.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(pid,))
                q.execute('SELECT * FROM nwt_portfolio_ledger WHERE position_id=%s FOR UPDATE',(pid,))
                position=dict(q.fetchone())
                if position['status']!='open' or int(position.get('close_generation') or 0)!=generation:
                    conn.rollback();continue
                q.execute('SELECT * FROM nwt_close_progress WHERE position_id=%s AND generation=%s FOR UPDATE',(pid,generation))
                progress=q.fetchone()
                requested=Decimal(str(progress['requested_qty'] if progress else position['qty']))
                validate_close_identity(position,order,requested)
                if progress and str(progress['order_id'])!=order['id']:raise ValueError('Close order changed identity')
                total=Decimal(str(order.get('filled_qty') or 0));seen=Decimal(str(progress['recorded_qty'])) if progress else Decimal(0)
                seen_value=Decimal(str(progress['recorded_value'])) if progress else Decimal(0)
                if not total.is_finite() or not seen<=total<=requested:raise ValueError('Invalid cumulative close quantity')
                if order.get('status')=='filled' and total!=requested:raise ValueError('Invalid full close receipt')
                delta=total-seen;remaining=Decimal(str(position['qty']))
                if delta>remaining:raise ValueError('Close receipt exceeds remaining ledger quantity')
                value=seen_value
                if delta:
                    price=Decimal(str(order.get('filled_avg_price') or 0))
                    if not price.is_finite() or price<=0:raise ValueError('Invalid close price')
                    value=price*total;increment_price=(value-seen_value)/delta
                    if increment_price<=0:raise ValueError('Invalid incremental close price')
                    stamp=datetime.fromisoformat(order['filled_at'].replace('Z','+00:00'))
                    if stamp.tzinfo is None or stamp<position['entry_time']:raise ValueError('Invalid broker close timestamp')
                    if delta==remaining:
                        close_position(conn,pid,increment_price,0,'verified_delayed_close',exit_time=stamp,commit=False)
                        q.execute("SELECT ticket_id FROM nwt_tickets WHERE type IN ('CLOSE_REQUEST','FORCE_CLOSE') AND payload->>'position_id'=%s",(pid,))
                        for ticket in q.fetchall():write_execution_decision(conn,str(ticket['ticket_id']),'EXECUTED','Verified close fill '+order['id'])
                    else:
                        # Closed segments and the remaining root share a group;
                        # complete-trade reports wait for every segment to close.
                        group=position.get('spread_group_id') or pid
                        part=dict(position,alpaca_order_id=None,qty=delta,notional_risk=Decimal(str(position['notional_risk']))*delta/remaining,spread_group_id=group)
                        child=insert_position(conn,part,commit=False)
                        close_position(conn,child,increment_price,0,'verified_partial_close',exit_time=stamp,commit=False)
                        q.execute('UPDATE nwt_portfolio_ledger SET qty=%s,notional_risk=%s,spread_group_id=%s,close_requested=true WHERE position_id=%s',
                                  (remaining-delta,Decimal(str(position['notional_risk']))*(remaining-delta)/remaining,group,pid))
                    q.execute("INSERT INTO nwt_system_log(level,component,message,payload) VALUES ('INFO','close_recovery','Recorded verified close quantity',%s)",
                              (json.dumps({'position_id':pid,'entry_order_id':str(position.get('alpaca_order_id')),'delta':str(delta),'increment_price':str(increment_price),'broker_receipt':order}),))
                    result['recorded'].append(pid)
                q.execute('INSERT INTO nwt_close_progress VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(position_id,generation) DO UPDATE SET recorded_qty=excluded.recorded_qty,recorded_value=excluded.recorded_value',
                          (pid,generation,order['id'],requested,total,value))
                if remaining-delta>0:
                    result['pending'].append(pid)
                    q.execute('UPDATE nwt_portfolio_ledger SET close_requested=true WHERE position_id=%s',(pid,))
                    if order.get('status') in ('canceled','expired'):
                        if generation>=3:
                            result['blocked'].append(pid+': residual close retry limit reached')
                        else:
                            q.execute('UPDATE nwt_portfolio_ledger SET close_generation=close_generation+1,close_requested=true WHERE position_id=%s',(pid,))
                            result['residual_retries'].append(pid)
                    elif order.get('status')=='rejected':result['blocked'].append(pid+': broker rejected close')
                    else:q.execute('UPDATE nwt_portfolio_ledger SET close_requested=true WHERE position_id=%s',(pid,))
            conn.commit()
        except Exception:
            conn.rollback();raise
    return result
