"""Record cumulative verified closes and resume canceled residual quantities."""
import json
import uuid
from datetime import datetime, timezone
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
    if not isinstance(order, dict) or not isinstance(order.get('id'), str) or not order['id'].strip():
        raise ValueError('Close receipt lacks order identity')
    side='buy' if position['direction']=='short' else 'sell'
    if (order.get('client_order_id')!=close_client_id(position) or order.get('symbol')!=position['asset']
            or order.get('side')!=side or Decimal(str(order.get('qty') or 0))!=Decimal(str(requested if requested is not None else position['qty']))):
        raise ValueError('Close receipt does not match ledger identity, direction and quantity')


def verified_close_time(position, order, *, observed_at=None):
    """Synchronous closes need the same exact receipt evidence as recovery."""
    validate_close_identity(position, order)
    qty=Decimal(str(order.get('filled_qty') or 0))
    price=Decimal(str(order.get('filled_avg_price') or 0))
    if (not order.get('id') or order.get('status')!='filled' or not qty.is_finite()
            or qty<=0 or qty!=Decimal(str(position['qty']))
            or not price.is_finite() or price<=0):
        raise ValueError('Incomplete close fill evidence')
    from fill_evidence import verified_fill_time
    return verified_fill_time(order,order['filled_at'],observed_at or datetime.now(timezone.utc),
                              entry_time=position['entry_time'])


def pending_close_receipt(position, order, now=None, requested=None, retained_bounds=()):
    """Only a validated, recent working receipt is ordinary asynchronous progress."""
    if order.get('status') not in ('new', 'accepted', 'pending_new', 'partially_filled', 'pending_cancel', 'accepted_for_bidding'):
        return False
    validate_close_identity(position, order, requested)
    qty = Decimal(str(requested if requested is not None else position['qty']))
    filled = Decimal(str(order.get('filled_qty') or 0))
    if not qty.is_finite() or not filled.is_finite() or not 0 <= filled < qty:
        raise ValueError('Invalid pending close quantity')
    if filled:
        price = Decimal(str(order.get('filled_avg_price') or 0))
        if not price.is_finite() or price <= 0:
            raise ValueError('Invalid partial close price')
    value = order.get('submitted_at') or order.get('created_at')
    if not value and retained_bounds:
        from close_bounds import aware
        value = max((source['submitted_at'] for source in retained_bounds), key=aware)
    submitted = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    now = now or datetime.now(timezone.utc)
    if submitted.tzinfo is None or not -60 <= (now-submitted).total_seconds() <= 900:
        raise ValueError('Pending close missing valid timestamp or older than 15 minutes')
    return True


def recover_closes(conn,get,now=None):
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
        from fill_evidence import with_fill_timestamp, verified_fill_time
        observed_at=now or datetime.now(timezone.utc)
        from close_bounds import retain_close_bounds, load_bounds, merge_bounds, bound_sources
        retain_close_bounds(conn,position,order,observed_at)
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
                bounds=merge_bounds(load_bounds(q,position,order),order,observed_at)
                order=with_fill_timestamp(order,get,observed_at=observed_at,
                                          additional_bounds=bound_sources(order['id'],bounds),
                                          entry_time=position['entry_time'])
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
                    stamp=verified_fill_time(order,order['filled_at'],observed_at,
                                             entry_time=position['entry_time'])
                    if delta==remaining:
                        close_position(conn,pid,increment_price,0,'verified_delayed_close',exit_time=stamp,commit=False)
                        q.execute("SELECT ticket_id FROM nwt_tickets WHERE type IN ('CLOSE_REQUEST','FORCE_CLOSE') AND payload->>'position_id'=%s",(pid,))
                        for ticket in q.fetchall():write_execution_decision(conn,str(ticket['ticket_id']),'EXECUTED','Verified close fill '+order['id'])
                    else:
                        # Closed segments and the remaining root share a group;
                        # complete-trade reports wait for every segment to close.
                        group=position.get('spread_group_id') or pid
                        part=dict(position,entry_evidence_parent_id=pid,alpaca_order_id=None,qty=delta,notional_risk=Decimal(str(position['notional_risk']))*delta/remaining,spread_group_id=group)
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
                    elif now is not None:
                        try:
                            if not pending_close_receipt(position,order,now,requested,bound_sources(order['id'],bounds)):
                                raise ValueError('Unexpected unresolved close status')
                        except (ValueError, ArithmeticError) as exc:
                            result['blocked'].append(pid+': '+str(exc))
            conn.commit()
        except Exception:
            conn.rollback();raise
    return result
