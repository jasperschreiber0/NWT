"""Resolve missing partial-fill timestamps from matching account activity."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode


CLOCK_SKEW = timedelta(seconds=5)


def verified_fill_time(order, value, observed_at, *, entry_time=None):
    """Broker times stay unchanged. Allow at most five seconds ahead of receipt.

    Broker submitted_at (or created_at if absent) is the lower bound when
    available; it is not synthesized from local processing time.
    """
    def aware(value):
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('Evidence timestamp requires timezone')
        return stamp
    observed_at = aware(observed_at)
    stamp = aware(value)
    if stamp > observed_at + CLOCK_SKEW:
        raise ValueError('Fill timestamp exceeds observed receipt time plus clock skew')
    submitted = order.get('submitted_at') or order.get('created_at')
    if submitted is not None:
        submitted = aware(submitted)
        if submitted > observed_at + CLOCK_SKEW or stamp < submitted:
            raise ValueError('Fill timestamp precedes submission or submission is in future')
    if entry_time is not None and stamp < aware(entry_time):
        raise ValueError('Fill timestamp precedes entry')
    return stamp


def with_fill_timestamp(order,get,*,observed_at=None,additional_bounds=(),entry_time=None):
    observed_at = observed_at or datetime.now(timezone.utc)
    def validate_time(value):
        # Every contributing execution must respect every retained identified
        # receipt's lower bound, including metadata absent from a later poll.
        stamp = verified_fill_time(order, value, observed_at, entry_time=entry_time)
        for source in additional_bounds:
            if not order.get('id') or source.get('id') != order['id']:
                raise ValueError('Timestamp bound belongs to a different order')
            verified_fill_time(source, value, observed_at)
        return stamp
    if order.get('filled_at'):
        validate_time(order['filled_at'])
        return order
    if Decimal(str(order.get('filled_qty') or 0))==0:return order
    after=order.get('submitted_at') or order.get('created_at')
    if not after:
        candidates=[source.get('submitted_at') or source.get('created_at') for source in additional_bounds]
        candidates=[value for value in candidates if value is not None]
        if candidates:
            from close_bounds import aware
            after=min(candidates,key=aware)
    if not after:raise ValueError('Cannot bound fill-activity lookup')
    params={'after':after,'direction':'asc','page_size':100}
    matches={};tokens=set()
    for _ in range(20):
        page=get('/account/activities/FILL?'+urlencode(params))
        if not isinstance(page,list):raise ValueError('Invalid fill activity response')
        for row in page:
            if row.get('order_id')==order['id']:
                if row.get('symbol')!=order['symbol'] or row.get('side')!=order['side']:raise ValueError('Fill activity identity conflict')
                if not row.get('id'):raise ValueError('Missing fill activity identity')
                if row['id'] in matches and matches[row['id']]!=row:
                    raise ValueError('Conflicting duplicate fill activity')
                qty,price=Decimal(str(row['qty'])),Decimal(str(row['price']))
                if not qty.is_finite() or not price.is_finite() or qty<=0 or price<=0:
                    raise ValueError('Invalid fill activity quantity or price')
                validate_time(row['transaction_time'])
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
    stamp=max(validate_time(r['transaction_time']) for r in matches.values())
    return dict(order,filled_at=stamp.isoformat())
