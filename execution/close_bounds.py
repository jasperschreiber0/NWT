"""Durable broker submission evidence, separate from accounted fill progress."""
import json
from datetime import datetime, timedelta
from psycopg2.extras import RealDictCursor


def aware(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('Malformed close submission bound')
    return stamp


def merge_bounds(prior, order, observed_at):
    """Keep original strings; omitted fields cannot erase earlier observations."""
    if not isinstance(prior, dict) or set(prior) - {'submitted_at', 'created_at'}:
        raise ValueError('Malformed retained close bounds')
    merged = dict(prior)
    for key in ('submitted_at', 'created_at'):
        if key in prior:
            aware(prior[key])
        if key not in order or order[key] is None:
            continue
        stamp = aware(order[key])
        # Preserve the existing pending submission clock-skew allowance.
        if stamp > aware(observed_at) + timedelta(seconds=60):
            raise ValueError('Close submission bound is in future')
        if key in prior and stamp != aware(prior[key]):
            raise ValueError('Conflicting retained close submission bound')
        merged.setdefault(key, order[key])
    if 'submitted_at' in merged and 'created_at' in merged:
        if aware(merged['created_at']) > aware(merged['submitted_at']):
            raise ValueError('Close creation follows submission')
    return merged


def bound_sources(order_id, bounds):
    # Check both independently, even if a later receipt drops submitted_at.
    return tuple(dict(id=order_id, submitted_at=value) for value in bounds.values())


def load_bounds(cursor, position, order):
    cursor.execute('SELECT order_id,bounds FROM nwt_close_receipt_bounds WHERE position_id=%s AND generation=%s FOR UPDATE',
                   (str(position['position_id']), int(position.get('close_generation') or 0)))
    row = cursor.fetchone()
    if row is None:
        return {}
    if str(row['order_id']) != order['id']:
        raise ValueError('Retained close order identity changed')
    return row['bounds']


def retain_close_bounds(conn, position, order, observed_at):
    """Commit evidence before polling/accounting; failure must block the cycle.

    No fill-progress row is created here: that row also controls residual retries.
    Missing migration raises normally; there is no in-memory fallback.
    """
    from close_recovery import validate_close_identity
    pid = str(position['position_id'])
    generation = int(position.get('close_generation') or 0)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as q:
            q.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (pid,))
            q.execute('SELECT * FROM nwt_portfolio_ledger WHERE position_id=%s FOR UPDATE', (pid,))
            current = q.fetchone()
            if current is None or current['status'] != 'open' or int(current.get('close_generation') or 0) != generation:
                raise ValueError('Close position changed before evidence persistence')
            q.execute('SELECT requested_qty FROM nwt_close_progress WHERE position_id=%s AND generation=%s', (pid, generation))
            progress = q.fetchone()
            validate_close_identity(current, order, progress['requested_qty'] if progress else None)
            prior = load_bounds(q, current, order)
            merged = merge_bounds(prior, order, observed_at)
            q.execute('INSERT INTO nwt_close_receipt_bounds(position_id,generation,order_id,bounds) VALUES (%s,%s,%s,%s::jsonb) '
                      'ON CONFLICT(position_id,generation) DO UPDATE SET bounds=excluded.bounds',
                      (pid, generation, order['id'], json.dumps(merged)))
        conn.commit()
        return bound_sources(order['id'], merged)
    except Exception:
        conn.rollback()
        raise
