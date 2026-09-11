"""User-authorized paper AAPL reduction to <=30%; never increases exposure."""
import json,os,sys,time,math
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path('/home/northworld/trading');PID='09a114df-e09b-40ee-b0ba-960ce0883087'
CLIENT='nwt-aapl-rebalance-20260914';STATE=Path('/var/lib/nwt-paper-allocation/state.json')

def size(equity,price,held=286):
    if equity<=0 or price<=0:raise ValueError('Invalid account/quote')
    remaining=min(held,math.floor(.30*equity/price))
    return held-remaining,remaining

def save(stage,**data):
    STATE.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    old=json.loads(STATE.read_text()) if STATE.exists() else {};old.update(stage=stage,**data)
    tmp=STATE.with_suffix('.tmp');tmp.write_text(json.dumps(old,default=str,indent=2));os.chmod(tmp,0o600);os.replace(tmp,STATE)
    print(stage,flush=True)

def main(check=False):
    import requests
    sys.path.insert(0,str(ROOT/'execution'));import engine
    assert engine.ALPACA_BASE_URL=='https://paper-api.alpaca.markets'
    c=engine.get_db()
    try:
        with c.cursor() as q:q.execute('SELECT pg_try_advisory_lock(9141330198)');assert q.fetchone()[0]
        account=engine.alpaca_get('/account');pos=engine.get_ledger_position(c,PID)
        assert pos['asset']=='AAPL' and pos['direction']=='long' and pos['status']=='open'
        broker=engine.alpaca_get('/positions/AAPL');assert broker['side']=='long'
        price=float(broker['current_price']);qty,remaining=size(float(account['equity']),price,int(pos['qty']))
        if check:print(json.dumps(dict(target_weight=.3,sell_qty=qty,remaining_qty=remaining,market_open=engine.alpaca_get('/clock')['is_open'])));return
        prior=json.loads(STATE.read_text()) if STATE.exists() else {}
        if prior.get('stage')=='COMPLETE':print('Already complete');return
        assert datetime.now(timezone.utc).date().isoformat()=='2026-09-14'
        clock=engine.alpaca_get('/clock');assert clock['is_open']
        assert (datetime.fromisoformat(clock['next_close'])-datetime.now(timezone.utc)).total_seconds()>1200
        assert engine.check_no_trade_mode(c)[0], 'Allocation must run while entries paused'
        if not prior:
            assert int(pos['qty'])==286 and float(broker['qty'])==286
            assert not engine.alpaca_get('/orders?status=open')
            assert all(p['symbol']=='AAPL' for p in engine.alpaca_get('/positions'))
            bid,ask=engine.get_latest_quote('AAPL','equity');assert bid and ask and 0<bid<=ask and (ask-bid)/ask<.002
            qty,remaining=size(float(account['equity']),ask)
            assert 0<qty<286
            save('PLANNED',qty=qty,remaining=remaining,original=dict(pos),equity=account['equity'],reference_ask=ask,limit_price=round(bid*.999,2))
            prior=json.loads(STATE.read_text())
        try:order=engine.alpaca_get('/orders:by_client_order_id?client_order_id='+CLIENT)
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code!=404:raise
            order=None
        if order is None:
            if prior['stage']!='PLANNED':raise RuntimeError('Ambiguous prior submission; manual review required')
            save('SUBMITTING')
            order=engine.alpaca_post('/orders',dict(symbol='AAPL',qty=str(prior['qty']),side='sell',type='limit',
                 limit_price=str(prior['limit_price']),time_in_force='day',position_intent='sell_to_close',client_order_id=CLIENT))
        assert order['symbol']=='AAPL' and order['side']=='sell' and int(order['qty'])==prior['qty']
        save('SUBMITTED',order=order)
        for _ in range(200):
            order=engine.alpaca_get('/orders/'+order['id'])
            if order['status'] in ('filled','canceled','rejected','expired'):break
            time.sleep(3)
        if order['status']!='filled':
            response=requests.delete(engine.ALPACA_BASE_URL+'/v2/orders/'+order['id'],headers=engine.ALPACA_HEADERS,timeout=15)
            if response.status_code not in (204,422):response.raise_for_status()
            save('NEEDS_ATTENTION',order=engine.alpaca_get('/orders/'+order['id']))
            raise RuntimeError('Allocation not fully filled; pause retained, no replacement')
        assert float(order['filled_qty'])==prior['qty']
        broker=engine.alpaca_get('/positions/AAPL');assert float(broker['qty'])==prior['remaining'] and broker['side']=='long'
        # Preserve uncertain historical cost basis; record the sale as allocation activity,
        # not a strategy win or a manufactured historical learning outcome.
        with c.cursor() as q:
            q.execute('SELECT qty FROM nwt_portfolio_ledger WHERE position_id=%s FOR UPDATE',(PID,));oldqty=int(q.fetchone()[0])
            assert oldqty in (286,prior['remaining'])
            if oldqty==286:
                q.execute('UPDATE nwt_portfolio_ledger SET qty=%s,notional_risk=entry_price*%s WHERE position_id=%s',(prior['remaining'],prior['remaining'],PID))
                q.execute("UPDATE nwt_position_attribution SET evidence=evidence || %s::jsonb WHERE position_id=%s",(json.dumps({'allocation_20260914':{'order':order,'original_qty':286,'remaining_qty':prior['remaining'],'cost_basis_still_unverified':True}}),PID))
                q.execute("INSERT INTO nwt_system_log(level,component,message,payload) VALUES('INFO','paper_allocation','User-authorized AAPL reduction; no strategy P&L assigned',%s)",(json.dumps(dict(position_id=PID,order=order,remaining=prior['remaining'],original=prior['original']),default=str),))
        c.commit()
        assert not engine.alpaca_get('/orders?status=open')
        save('COMPLETE',order=order,realized_strategy_pnl=None)
    finally:c.close()

if __name__=='__main__':main('--check' in sys.argv)
