"""One-shot paper incident recovery. No strategy entries or broad engine runs."""
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path('/home/northworld/trading')
POSITION = '39c0682e-cfb9-5e93-9587-c08dffb2a0c0'
OPEN_ORDER = '9a254ebb-7b99-4e74-b116-52bd97166cfa'
CLIENT = 'nwt-close-' + POSITION
STATE = Path('/var/lib/nwt-vgk-recovery/state.json')


def validate_position(pos):
    assert pos and str(pos['position_id']) == POSITION
    assert pos['asset'] == 'VGK' and pos['direction'] == 'long'
    assert Decimal(str(pos['qty'])) == 196
    assert str(pos['alpaca_order_id']) == OPEN_ORDER
    assert pos['strategy_id'] == 'EXECUTION_INCIDENT'


def complete_fill(order):
    return (order.get('status') == 'filled'
            and order.get('symbol') == 'VGK' and order.get('side') == 'sell'
            and order.get('client_order_id') == CLIENT
            and Decimal(str(order.get('filled_qty') or 0)) == 196
            and Decimal(str(order.get('filled_avg_price') or 0)) > 0
            and bool(order.get('filled_at')))


def save(stage, **data):
    STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    old = json.loads(STATE.read_text()) if STATE.exists() else {}
    old.update(stage=stage, updated_at=datetime.now(timezone.utc).isoformat(), **data)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(old, default=str, indent=2)); os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)
    print(stage, flush=True)


def main(check=False):
    from dotenv import dotenv_values
    import requests
    sys.path.insert(0, str(ROOT / 'execution'))
    import engine
    values = dotenv_values(ROOT / 'nwt_agents/.env')
    assert engine.ALPACA_BASE_URL == values['NWT_ALPACA_BASE_URL'].rstrip('/') == 'https://paper-api.alpaca.markets'
    account = engine.alpaca_get('/account')
    response = requests.get(engine.ALPACA_BASE_URL + '/v2/account', headers={
        'APCA-API-KEY-ID': values['NWT_ALPACA_KEY_ID'],
        'APCA-API-SECRET-KEY': values['NWT_ALPACA_SECRET_KEY']}, timeout=15)
    response.raise_for_status(); assert response.json()['id'] == account['id']
    assert not account.get('trading_blocked') and not account.get('account_blocked')
    c = engine.get_db()
    try:
        with c.cursor() as q:
            q.execute('SELECT pg_try_advisory_lock(9141330196)'); assert q.fetchone()[0]
        pos = engine.get_ledger_position(c, POSITION); validate_position(pos)
        with c.cursor() as q:
            q.execute("SELECT flag,value,reason,set_by FROM nwt_system_flags WHERE flag IN ('no_trade_mode','mutation_frozen')")
            flags = {row[0]: row[1:] for row in q.fetchall()}
        clock = engine.alpaca_get('/clock')
        if check:
            print(json.dumps(dict(paper_account_verified=True, position_status=pos['status'],
                                 is_open=clock['is_open'], flags=flags), default=str)); return
        if STATE.exists() and json.loads(STATE.read_text()).get('stage') == 'COMPLETE':
            print('Already completed; no actions'); return
        now = datetime.now(timezone.utc)
        assert now.date().isoformat() == '2026-09-14', 'Outside authorized one-shot date'
        assert clock['is_open'], 'Regular market closed'
        assert (datetime.fromisoformat(clock['next_close']) - now).total_seconds() >= 900
        assert flags['mutation_frozen'][0] is True
        assert flags['no_trade_mode'][0] is True
        assert flags['no_trade_mode'][1] == 'Recon critical mismatch: 1 untracked/qty-mismatch positions'
        assert flags['no_trade_mode'][2] == 'recon_agent'
        positions = engine.alpaca_get('/positions')
        unrelated = {p['symbol']: (p['side'], str(p['qty'])) for p in positions if p['symbol'] != 'VGK'}
        assert unrelated == {'AAPL': ('long', '286')}, 'Unrelated baseline changed'
        orders = engine.alpaca_get('/orders?status=open')
        assert all(o.get('client_order_id') == CLIENT for o in orders), 'Conflicting order'
        save('PREFLIGHT', baseline=unrelated, position_id=POSITION)
        # Reuse the broker's durable order identity after any restart or timeout.
        order = engine.equity_close_order(pos)
        save('SUBMITTED', order_id=order['id'])
        deadline = time.monotonic() + 600
        while not complete_fill(order):
            if order.get('status') in ('canceled', 'expired', 'rejected'):
                raise RuntimeError('Close ended without a complete fill; hold retained')
            if time.monotonic() >= deadline:
                # Cancel only this known recovery order; do not replace or oversell.
                response = requests.delete(engine.ALPACA_BASE_URL + '/v2/orders/' + order['id'],
                                           headers=engine.ALPACA_HEADERS, timeout=15)
                if response.status_code not in (204, 422): response.raise_for_status()
                order = engine.alpaca_get('/orders/' + order['id'])
                if complete_fill(order): break
                save('NEEDS_ATTENTION', order=order)
                raise RuntimeError('Timed out; cancellation attempted, inspect remaining order/exposure')
            time.sleep(3)
            order = engine.alpaca_get('/orders/' + order['id'])
        save('FILLED', order=order)
        engine.close_position(c, POSITION, float(order['filled_avg_price']), 0, 'incident_recovery')
        with c.cursor() as q:
            q.execute('UPDATE nwt_portfolio_ledger SET exit_time=%s WHERE position_id=%s',
                      (order['filled_at'], POSITION))
        c.commit()
        actual = engine.alpaca_get('/positions')
        assert {p['symbol']: (p['side'], str(p['qty'])) for p in actual} == unrelated
        assert not engine.alpaca_get('/orders?status=open')
        sys.path.insert(0, str(ROOT / 'nwt_agents'))
        from recon_agent import run_recon
        from shared_context import clear_no_trade_mode
        assert run_recon(c, 'authorized_vgk_recovery'), 'Reconciliation failed; hold retained'
        # Lock and recheck the exact incident hold so unrelated pauses cannot be cleared.
        with c.cursor() as q:
            q.execute("SELECT value,reason,set_by FROM nwt_system_flags WHERE flag='no_trade_mode' FOR UPDATE")
            assert tuple(q.fetchone()) == tuple(flags['no_trade_mode'])
            if os.environ.get('NWT_DEFER_RECOVERY_RELEASE') != '1':
                clear_no_trade_mode(c, 'authorized_vgk_recovery')
        engine.log_system_event(c, 'INFO', 'incident_repair',
                                'VGK accidental paper exposure resolved; release controlled by workflow',
                                {'position_id': POSITION, 'close_order': order,
                                 'gross_pnl': str((Decimal(order['filled_avg_price']) - Decimal(str(pos['entry_price']))) * 196)})
        save('COMPLETE', order=order, no_trade_mode=os.environ.get('NWT_DEFER_RECOVERY_RELEASE') == '1')
    finally:
        c.close()


if __name__ == '__main__':
    try:
        main('--check' in sys.argv)
    except Exception as exc:
        if '--check' not in sys.argv: save('NEEDS_ATTENTION', error=str(exc))
        raise
