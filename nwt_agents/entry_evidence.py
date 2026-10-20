"""Exact, point-in-time learning provenance. No environment or network access."""
import hashlib
import json
from datetime import datetime, timezone

VERSION = 'entry-evidence-20261020-v1'


def stamp(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Timezone-aware timestamp required')
    return value.astimezone(timezone.utc)


def snapshot(position, ticket, proposal, decisions):
    """Do not infer provenance from symbols, order proximity, or current context.

    Decision context is labelled as decision-time, never as measured fill-time IV.
    A source proposal is followed only by its explicit ID. Ambiguity fails closed.
    """
    reasons = []
    context = None
    decision = None
    try:
        entry = stamp(position.get('entry_time'))
        if not ticket or str(ticket['ticket_id']) != str(position.get('ticket_id')):
            raise ValueError('missing_exact_ticket')
        if stamp(ticket['created_at']) > entry:
            raise ValueError('ticket_after_entry')
        payload = ticket.get('payload') or {}
        origin = payload.get('source_proposal_ticket_id') or ticket['ticket_id']
        if payload.get('source_proposal_ticket_id'):
            if not proposal or str(proposal['ticket_id']) != str(origin):
                raise ValueError('missing_exact_proposal')
            if stamp(proposal['created_at']) > stamp(ticket['created_at']):
                raise ValueError('proposal_after_request')
        matches = [d for d in decisions if str(d.get('ticket_id')) == str(origin)]
        if len(matches) != 1:
            raise ValueError('decision_missing_or_ambiguous')
        decision = matches[0]
        if max(stamp(decision['run_at']), stamp(decision['created_at'])) > entry:
            raise ValueError('decision_after_entry')
        if not position.get('strategy_id') or decision.get('strategy_id') != position['strategy_id']:
            raise ValueError('strategy_mismatch')
        regime = decision.get('regime')
        if not isinstance(regime, dict) or not regime or regime.get('primary_regime') in (None, 'unknown'):
            raise ValueError('unknown_decision_regime')
        context = {k: decision.get(k) for k in ('id', 'ticket_id', 'strategy_id', 'symbol',
                   'run_at', 'created_at', 'regime', 'iv_at_decision', 'dte_at_decision',
                   'genome_version', 'signal_strength', 'direction', 'archetype')}
    except (ValueError, TypeError, KeyError) as exc:
        reasons.append(str(exc))
    result = dict(version=VERSION, ticket_id=str(position.get('ticket_id') or '') or None,
                  decision_id=str(decision['id']) if decision and not reasons else None,
                  eligible=not reasons, reasons=reasons, decision_context=context,
                  entry_time=position.get('entry_time'), iv_at_entry=None,
                  regime_at_entry={'status': 'unknown', 'reason': 'No measured fill-time regime snapshot'},
                  unknowns=['fill_time_iv', 'fill_time_regime'])
    encoded = json.dumps(result, sort_keys=True, default=str, separators=(',', ':'))
    return dict(json.loads(encoded), sha256=hashlib.sha256(encoded.encode()).hexdigest())


def capture(conn, position_id, data):
    """Called in the ledger insert transaction; one immutable snapshot per leg.

    Subsequent cumulative partial fills update quantities, not this first-entry
    context. Missing evidence is recorded explicitly instead of reconstructed.
    """
    from psycopg2.extras import RealDictCursor
    with conn.cursor(cursor_factory=RealDictCursor) as q:
        q.execute('SELECT * FROM nwt_tickets WHERE ticket_id=%s', (data.get('ticket_id'),))
        ticket = q.fetchone()
        proposal = None
        origin = (ticket.get('payload') or {}).get('source_proposal_ticket_id') if ticket else None
        if origin:
            q.execute('SELECT * FROM nwt_tickets WHERE ticket_id=%s', (origin,))
            proposal = q.fetchone()
        q.execute('SELECT * FROM nwt_decision_inputs WHERE ticket_id=%s',
                  (origin or data.get('ticket_id'),))
        evidence = snapshot(data, ticket, proposal, q.fetchall())
        q.execute('INSERT INTO nwt_entry_evidence(position_id,decision_id,eligible,snapshot) '
                  'VALUES (%s,%s,%s,%s)',
                  (position_id, evidence['decision_id'], evidence['eligible'], json.dumps(evidence, default=str)))


def load(conn, position_id):
    with conn.cursor() as q:
        q.execute('SELECT snapshot FROM nwt_entry_evidence WHERE position_id=%s', (position_id,))
        row = q.fetchone()
    return row[0] if row else {'eligible': False, 'reasons': ['missing_entry_snapshot']}
