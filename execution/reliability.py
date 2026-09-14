"""Fail-closed entry policy and durable, broker-independent intent reservation."""
import hashlib
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')


def entry_rejection(ticket, now=None):
    now = now or datetime.now(timezone.utc)
    created = ticket.get('created_at')
    if isinstance(created, str):
        created = datetime.fromisoformat(created.replace('Z', '+00:00'))
    if not isinstance(created, datetime) or created.tzinfo is None:
        return 'ENTRY_TIMESTAMP_MISSING'
    age = (now - created).total_seconds()
    if age < -60 or created.astimezone(ET).date() != now.astimezone(ET).date():
        return 'ENTRY_SESSION_EXPIRED'
    # Regional candidates are deliberately generated before the US session.
    max_age = 6 * 3600 if ticket.get('from_agent') in ('AUS_EXECUTOR', 'EU_EXECUTOR') else 1800
    if age > max_age:
        return 'ENTRY_AGE_EXPIRED'
    return None


def intent_key(ticket, now=None):
    p = ticket['payload']
    session = (now or datetime.now(timezone.utc)).astimezone(ET).date().isoformat()
    # One strategy/instrument/direction entry per session, even after a close.
    parts = [session, p.get('bot_source'), p.get('strategy_id'), p.get('symbol'),
             p.get('direction'), p.get('option_symbol'),
             sorted((x.get('option_symbol'), x.get('side')) for x in p.get('legs', []))]
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def reserve(conn, ticket):
    with conn.cursor() as cur:
        cur.execute('INSERT INTO nwt_entry_intents(intent_key,ticket_id) VALUES (%s,%s) '
                    'ON CONFLICT DO NOTHING RETURNING ticket_id',
                    (intent_key(ticket), str(ticket['ticket_id'])))
        row = cur.fetchone()
    conn.commit()
    return bool(row)


def reconcile_quantities(broker, ledger):
    expected = {}
    for row in ledger:
        qty = float(row.get('qty') or 0) * (-1 if row.get('direction') == 'short' else 1)
        expected[row['asset']] = expected.get(row['asset'], 0) + qty
    actual = {p['symbol']: float(p['qty']) for p in broker}
    return [{'symbol': s, 'broker': actual.get(s, 0), 'ledger': expected.get(s, 0)}
            for s in sorted(set(actual) | set(expected))
            if abs(actual.get(s, 0) - expected.get(s, 0)) > 0.000001]
