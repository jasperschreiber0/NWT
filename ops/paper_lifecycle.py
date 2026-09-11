"""One-shot, one-share paper SPY lifecycle under ordinary execution gates."""
import json,os,sys,time,uuid
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path('/home/northworld/trading')
STATE=Path('/var/lib/nwt-paper-lifecycle/state.json')
TICKET=str(uuid.uuid5(uuid.NAMESPACE_URL,'nwt-qa-entry-20260914'))
CLIENT='nwt-qa-entry-'+TICKET

def save(stage,**data):
    STATE.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    old=json.loads(STATE.read_text()) if STATE.exists() else {}
    old.update(stage=stage,updated_at=datetime.now(timezone.utc).isoformat(),**data)
    tmp=STATE.with_suffix('.tmp');tmp.write_text(json.dumps(old,default=str,indent=2));os.chmod(tmp,0o600);os.replace(tmp,STATE)
    print(stage,flush=True)

def lookup(engine):
    import requests
    try:return engine.alpaca_get('/orders:by_client_order_id?client_order_id='+CLIENT)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code==404:return None
        raise

def main(check=False):
    import requests
    from dotenv import dotenv_values
    sys.path.insert(0,str(ROOT/'execution'));import engine
    v=dotenv_values(ROOT/'nwt_agents/.env')
    assert engine.ALPACA_BASE_URL==v['NWT_ALPACA_BASE_URL'].rstrip('/')=='https://paper-api.alpaca.markets'
    c=engine.get_db()
    try:
        with c.cursor() as q:
            q.execute('SELECT pg_try_advisory_lock(9141330197)');assert q.fetchone()[0]
            q.execute("SELECT value FROM nwt_system_flags WHERE flag='mutation_frozen'");assert q.fetchone()[0]
        prior=json.loads(STATE.read_text()) if STATE.exists() else {}
        if prior.get('stage')=='COMPLETE':print('Already complete');return
        clock=engine.alpaca_get('/clock');halted,reason=engine.check_no_trade_mode(c)
        directives=engine.load_master_directives()
        with c.cursor() as q:
            q.execute('SELECT * FROM nwt_portfolio_ledger WHERE ticket_id=%s',(TICKET,));existing=q.fetchall()
        order=lookup(engine)
        if check:
            print(json.dumps(dict(is_open=clock['is_open'],halted=halted,reason=reason,
                permission=directives.get('bot_permissions',{}).get('us'),existing_order=bool(order),existing_ledger_rows=len(existing))));return
        now=datetime.now(timezone.utc)
        assert now.date().isoformat()=='2026-09-14' and clock['is_open']
        assert (datetime.fromisoformat(clock['next_close'])-now).total_seconds()>900
        assert not halted and not directives.get('global_kill_switch')
        # Pending attempts are recovered rather than submitted again.
        if not prior:
            assert not order and not existing
            positions=engine.alpaca_get('/positions')
            assert all(p['symbol']!='SPY' for p in positions)
            assert not engine.alpaca_get('/orders?status=open')
            sys.path.insert(0,str(ROOT/'nwt_agents'));from recon_agent import run_recon
            assert run_recon(c,'qa_preflight')
            response=requests.get(engine.ALPACA_DATA_URL+'/v2/stocks/SPY/quotes/latest',headers=engine.ALPACA_HEADERS,timeout=15)
            response.raise_for_status();quote=response.json()['quote']
            assert 0 <= (datetime.now(timezone.utc)-datetime.fromisoformat(quote['t'].replace('Z','+00:00'))).total_seconds()<60
            bid,ask=float(quote['bp']),float(quote['ap']);assert 0<bid<=ask<990 and (ask-bid)/ask<.002
            budget=round(ask*1.001,2);assert budget<1000
            payload=dict(approved=True,bot_source='US_BOT',symbol='SPY',direction='long',strategy_id='QA_PAPER_LIFECYCLE',
                sized_notional=budget,asset_type='equity',time_in_force='day',qty=1,client_order_id=CLIENT)
            veto,why=engine.synchronous_risk_veto(c,payload);assert not veto,why
            exceeded,_,_=engine.check_directional_cap(c,'long',budget);assert not exceeded,'Long allocation still exceeds cap'
            save('ENTRY_INTENT',baseline=[{k:p[k] for k in ('symbol','side','qty')} for p in positions],payload=payload,quote=quote)
            with c.cursor() as q:
                q.execute("INSERT INTO nwt_tickets(ticket_id,from_agent,to_agent,type,payload) VALUES(%s,'QA_SUPERVISOR','QA_SUPERVISOR','PAPER_LIFECYCLE_TEST',%s) ON CONFLICT DO NOTHING",(TICKET,json.dumps(payload)))
            c.commit()
            assert not engine.check_no_trade_mode(c)[0]
            engine.POLL_MAX=200
            engine.process_ticket(c,dict(ticket_id=TICKET,payload=payload),directives)
            order=lookup(engine)
        if not order:raise RuntimeError('Prior intent has no resolved broker order; no retry submitted')
        save('ENTRY_OBSERVED',entry_order=order)
        if order['status']!='filled':
            response=requests.delete(engine.ALPACA_BASE_URL+'/v2/orders/'+order['id'],headers=engine.ALPACA_HEADERS,timeout=15)
            if response.status_code not in (204,422):response.raise_for_status()
            for _ in range(10):
                order=engine.alpaca_get('/orders/'+order['id'])
                if order['status'] in ('filled','canceled','expired','rejected'):break
                time.sleep(3)
        if float(order.get('filled_qty') or 0)==0:raise RuntimeError('Entry did not fill; inspect cancellation state')
        assert order['symbol']=='SPY' and order['side']=='buy' and float(order['filled_qty'])==1 and order['status']=='filled'
        assert float(order['filled_avg_price'])<1000
        with c.cursor() as q:
            q.execute('SELECT position_id FROM nwt_portfolio_ledger WHERE ticket_id=%s AND alpaca_order_id=%s',(TICKET,order['id']));rows=q.fetchall()
        normal_entry=len(rows)==1
        if not normal_entry:
            # Exact broker evidence permits cleanup, but does not turn a failed entry path into a passed test.
            assert not rows
            posid=engine.insert_position(c,dict(bot_source='US_BOT',strategy_id='QA_PAPER_LIFECYCLE',ticket_id=TICKET,
                asset='SPY',asset_type='equity',direction='long',qty=1,entry_price=float(order['filled_avg_price']),
                notional_risk=float(order['filled_avg_price']),alpaca_order_id=order['id'],entry_time=order['filled_at']))
        else:posid=str(rows[0][0])
        save('ENTRY_LINKED',position_id=posid,normal_entry=normal_entry,entry_order=order)
        closeid=str(uuid.uuid5(uuid.NAMESPACE_URL,'nwt-qa-close-20260914'))
        close_payload=dict(position_id=posid,symbol='SPY',asset_type='equity',qty=1,exit_reason='qa_lifecycle')
        with c.cursor() as q:
            q.execute("INSERT INTO nwt_tickets(ticket_id,from_agent,to_agent,type,payload) VALUES(%s,'QA_SUPERVISOR','QA_SUPERVISOR','PAPER_LIFECYCLE_CLOSE',%s) ON CONFLICT DO NOTHING",(closeid,json.dumps(close_payload)))
        c.commit();engine.POLL_MAX=200
        engine.process_close_ticket(c,dict(ticket_id=closeid,payload=close_payload))
        closed=engine.alpaca_get('/orders:by_client_order_id?client_order_id=nwt-close-'+posid)
        assert closed['status']=='filled' and float(closed['filled_qty'])==1 and closed['side']=='sell'
        pos=engine.get_ledger_position(c,posid);assert pos['status']=='closed' and pos['lifecycle_state']=='CLOSED'
        current=[{k:p[k] for k in ('symbol','side','qty')} for p in engine.alpaca_get('/positions')]
        baseline=json.loads(STATE.read_text())['baseline']
        assert sorted(current,key=lambda p:p['symbol'])==sorted(baseline,key=lambda p:p['symbol'])
        assert not engine.alpaca_get('/orders?status=open')
        with c.cursor() as q:
            q.execute('UPDATE nwt_portfolio_ledger SET entry_time=%s,exit_time=%s WHERE position_id=%s',(order['filled_at'],closed['filled_at'],posid))
            q.execute("SELECT count(*) FROM nwt_trade_outcomes WHERE position_id=%s",(posid,));assert q.fetchone()[0]==0
        c.commit()
        assert normal_entry,'Exposure cleaned up, but entry-to-ledger path failed'
        save('COMPLETE',entry_order=order,close_order=closed,position_id=posid,
             gross_pnl=float(closed['filled_avg_price'])-float(order['filled_avg_price']))
    except Exception as exc:
        c.rollback()
        if not check:
            save('NEEDS_ATTENTION',error=str(exc))
            with c.cursor() as q:
                q.execute("UPDATE nwt_system_flags SET value=TRUE,reason='Paper QA requires review',set_by='qa_supervisor',updated_at=NOW() WHERE flag='no_trade_mode' AND value=FALSE")
            c.commit()
        raise
    finally:c.close()

if __name__=='__main__':main('--check' in sys.argv)
