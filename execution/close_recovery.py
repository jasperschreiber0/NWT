"""Reconcile existing full close receipts without submitting broker orders."""
import json
from datetime import datetime
from decimal import Decimal
import requests
from psycopg2.extras import RealDictCursor
from ledger import close_position
from decision_store import write_execution_decision


def validate_close_identity(position, order):
    side = 'buy' if position['direction'] == 'short' else 'sell'
    if (order.get('client_order_id') != 'nwt-close-' + str(position['position_id'])
            or order.get('symbol') != position['asset'] or order.get('side') != side
            or Decimal(str(order.get('qty') or 0)) != Decimal(str(position['qty']))):
        raise ValueError('Close receipt does not match ledger identity, direction and quantity')


def recover_closes(conn, get):
    with conn.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("SELECT * FROM nwt_portfolio_ledger WHERE status='open'")
        positions = q.fetchall()
    result = {'recorded': [], 'pending': []}
    for position in positions:
        pid = str(position['position_id'])
        try:
            order = get('/orders:by_client_order_id?client_order_id=nwt-close-' + pid)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                continue
            raise
        validate_close_identity(position, order)
        if order.get('status') != 'filled':
            # Partial and canceled closes need explicit quantity attribution.
            # Preserve evidence and entry hold; never resubmit another close.
            result['pending'].append(pid)
            continue
        qty = Decimal(str(order.get('filled_qty') or 0))
        price = Decimal(str(order.get('filled_avg_price') or 0))
        if not qty.is_finite() or not price.is_finite() or qty != Decimal(str(position['qty'])) or qty <= 0 or price <= 0:
            raise ValueError('Invalid full close receipt')
        stamp = datetime.fromisoformat(order['filled_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp < position['entry_time']:
            raise ValueError('Invalid broker close timestamp')
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as q:
                q.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (pid,))
                q.execute('SELECT status FROM nwt_portfolio_ledger WHERE position_id=%s FOR UPDATE', (pid,))
                if q.fetchone()['status'] != 'open':
                    conn.rollback()
                    continue
                close_position(conn, pid, price, 0, 'verified_delayed_close', exit_time=stamp, commit=False)
                q.execute("SELECT ticket_id FROM nwt_tickets WHERE type IN ('CLOSE_REQUEST','FORCE_CLOSE') AND payload->>'position_id'=%s", (pid,))
                for ticket in q.fetchall():
                    write_execution_decision(conn, str(ticket['ticket_id']), 'EXECUTED', 'Verified existing close fill ' + order['id'])
                q.execute("INSERT INTO nwt_system_log(level,component,message,payload) VALUES ('INFO','close_recovery','Recorded verified delayed close',%s)",
                          (json.dumps({'position_id': pid, 'broker_receipt': order}),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        result['recorded'].append(pid)
    return result
