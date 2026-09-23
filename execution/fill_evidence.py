"""Resolve missing partial-fill timestamps from matching account activity."""
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode


def with_fill_timestamp(order,get):
    if order.get('filled_at') or Decimal(str(order.get('filled_qty') or 0))==0:return order
    after=order.get('submitted_at') or order.get('created_at')
    if not after:raise ValueError('Cannot bound fill-activity lookup')
    params={'after':after,'direction':'asc','page_size':100}
    matches={};tokens=set()
    for _ in range(20):
        page=get('/account/activities/FILL?'+urlencode(params))
        if not isinstance(page,list):raise ValueError('Invalid fill activity response')
        for row in page:
            if row.get('order_id')==order['id']:
                if row.get('symbol')!=order['symbol'] or row.get('side')!=order['side']:raise ValueError('Fill activity identity conflict')
                matches[row['id']]=row
        if len(page)<100:break
        token=page[-1]['id']
        if token in tokens:raise ValueError('Repeated fill activity pagination token')
        tokens.add(token);params['page_token']=token
    else:raise ValueError('Incomplete fill activity history')
    qty=sum((Decimal(str(r['qty'])) for r in matches.values()),Decimal(0))
    value=sum((Decimal(str(r['qty']))*Decimal(str(r['price'])) for r in matches.values()),Decimal(0))
    expected=Decimal(str(order['filled_qty']))
    if qty!=expected or qty<=0 or abs(value/qty-Decimal(str(order['filled_avg_price'])))>Decimal('.000001'):
        raise ValueError('Fill activities do not match cumulative order receipt')
    stamp=max(datetime.fromisoformat(r['transaction_time'].replace('Z','+00:00')) for r in matches.values())
    if stamp.tzinfo is None:raise ValueError('Fill activity timestamp lacks timezone')
    return dict(order,filled_at=stamp.isoformat())
