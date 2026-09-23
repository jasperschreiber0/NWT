import json
import os
from copy import deepcopy
from decimal import Decimal
import psycopg2
import pytest
from fill_recovery import recover_entries, fill_data

TID='11111111-1111-1111-1111-111111111111'
OID='22222222-2222-2222-2222-222222222222'
PAYLOAD=dict(asset_type='equity',bot_source='EU_BOT',strategy_id='EU-MR-001',symbol='VGK',direction='long',expected_payoff={'stop_pct':-.015,'target_pct':.03})
ORDER=dict(id=OID,client_order_id='nwt-entry-'+TID,symbol='VGK',side='buy',qty='32',filled_qty='32',filled_avg_price='89.66375',filled_at='2026-09-17T13:32:36.842520907Z',status='filled')


@pytest.fixture
def recovery_db():
    conn=psycopg2.connect(os.environ['NWT_TEST_DB_DSN'])
    with conn.cursor() as q:
        q.execute('''
        CREATE TEMP TABLE nwt_tickets(ticket_id uuid PRIMARY KEY,type text,payload jsonb,created_at timestamptz DEFAULT now());
        CREATE TEMP TABLE nwt_entry_intents(ticket_id uuid);
        CREATE TEMP TABLE nwt_ticket_decisions(ticket_id uuid,decision text,reasoning text,decided_by text,created_at timestamptz DEFAULT now());
        CREATE UNIQUE INDEX recovery_partial_decision ON nwt_ticket_decisions(ticket_id,decided_by)
          WHERE created_at >= TIMESTAMPTZ '2026-07-24 00:00:00+00';
        CREATE TEMP TABLE nwt_decision_inputs(ticket_id uuid,outcome_reason text,stage_reached text);
        CREATE TEMP TABLE nwt_system_log(level text,component text,message text,payload jsonb);
        CREATE TEMP TABLE nwt_portfolio_ledger(position_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),bot_source text,strategy_id text,
          asset text,asset_type text,direction text,delta_exposure numeric,notional_risk numeric,qty numeric,entry_price numeric,
          entry_time timestamptz,entry_bid numeric,entry_ask numeric,alpaca_order_id uuid,stop_pct numeric,target_pct numeric,
          spread_group_id uuid,ticket_id uuid,status text,lifecycle_state text,exit_price numeric,exit_time timestamptz,realized_slippage numeric,exit_reason text,exit_bid numeric,exit_ask numeric,close_generation integer DEFAULT 0,close_requested boolean DEFAULT false);
        CREATE UNIQUE INDEX recovery_order_asset ON nwt_portfolio_ledger(alpaca_order_id,asset) WHERE alpaca_order_id IS NOT NULL;
        CREATE TEMP TABLE nwt_close_progress(position_id uuid,generation integer,order_id uuid,requested_qty numeric,recorded_qty numeric,recorded_value numeric,PRIMARY KEY(position_id,generation));
        ''')
        q.execute('INSERT INTO nwt_tickets(ticket_id,payload) VALUES(%s,%s)',(TID,json.dumps(PAYLOAD)))
        q.execute('INSERT INTO nwt_entry_intents VALUES(%s)',(TID,))
        q.execute("INSERT INTO nwt_decision_inputs VALUES(%s,'EXECUTION_FAILED','EXECUTION')",(TID,))
    conn.commit()
    yield conn
    conn.close()


def test_late_fill_recovers_once_with_exact_broker_evidence(recovery_db):
    conn=recovery_db
    with conn.cursor() as q:
        q.execute("INSERT INTO nwt_ticket_decisions(ticket_id,decision,reasoning,decided_by) VALUES(%s,'FAILED','Order did not fill','EXECUTION_ENGINE')",(TID,))
    conn.commit()
    assert len(recover_entries(conn,lambda _:deepcopy(ORDER))['recorded'])==1
    assert recover_entries(conn,lambda _:pytest.fail('must not fetch completed order'))['recorded']==[]
    with conn.cursor() as q:
        q.execute('SELECT qty,entry_price,notional_risk,stop_pct,target_pct FROM nwt_portfolio_ledger')
        assert q.fetchall()==[(Decimal(32),Decimal('89.66375'),Decimal('2869.24000'),Decimal('-.015'),Decimal('.03'))]
        q.execute('SELECT outcome_reason FROM nwt_decision_inputs')
        assert q.fetchone()[0]=='EXECUTED'
        q.execute("SELECT payload->'previous_decision'->>'decision' FROM nwt_system_log")
        assert q.fetchone()[0]=='FAILED'


def test_submitted_decision_can_transition_to_executed(recovery_db):
    import engine
    engine.insert_decision(recovery_db,TID,'SUBMITTED','Accepted')
    engine.insert_decision(recovery_db,TID,'EXECUTED','Filled')
    with recovery_db.cursor() as q:
        q.execute('SELECT decision FROM nwt_ticket_decisions')
        assert q.fetchall()==[('EXECUTED',)]


def test_pending_order_is_followed_until_filled(recovery_db):
    pending=dict(ORDER,status='new',filled_qty='0',filled_avg_price=None,filled_at=None)
    assert recover_entries(recovery_db,lambda _:pending)['pending']==[TID]
    assert len(recover_entries(recovery_db,lambda _:ORDER)['recorded'])==1


def test_ledger_and_execution_audit_are_atomic(recovery_db):
    with recovery_db.cursor() as q:q.execute("ALTER TABLE nwt_system_log ADD CONSTRAINT injected_failure CHECK(level='NEVER')")
    recovery_db.commit()
    with pytest.raises(psycopg2.IntegrityError):recover_entries(recovery_db,lambda _:ORDER)
    with recovery_db.cursor() as q:
        q.execute('SELECT COUNT(*) FROM nwt_portfolio_ledger');assert q.fetchone()[0]==0
        q.execute('SELECT COUNT(*) FROM nwt_ticket_decisions');assert q.fetchone()[0]==0


@pytest.mark.parametrize('change',[{'client_order_id':'wrong'},{'side':'sell'},{'symbol':'FEZ'},{'filled_qty':'33'},{'filled_avg_price':'NaN'},{'filled_at':None}])
def test_unverified_fills_are_rejected(change):
    with pytest.raises(ValueError):fill_data({'ticket_id':TID,'payload':PAYLOAD},dict(ORDER,**change))


def test_terminal_partial_fill_uses_actual_filled_quantity():
    data=fill_data({'ticket_id':TID,'payload':PAYLOAD},dict(ORDER,status='canceled',filled_qty='12'))
    assert data['qty']==12


@pytest.fixture
def close_db(recovery_db):
    recover_entries(recovery_db,lambda _:ORDER)
    with recovery_db.cursor() as q:
        q.execute("UPDATE nwt_portfolio_ledger SET status='open' RETURNING position_id")
        pid=str(q.fetchone()[0])
    recovery_db.commit()
    return recovery_db,pid


def close_receipt(pid, **changes):
    return dict(dict(ORDER,client_order_id='nwt-close-'+pid,side='sell',
                     filled_at='2026-09-17T15:00:00Z'),**changes)


def test_delayed_close_receipt_is_atomic_and_idempotent(close_db):
    from close_recovery import recover_closes
    conn,pid=close_db
    with conn.cursor() as q:
        q.execute("ALTER TABLE nwt_system_log ADD CONSTRAINT fail_close_audit CHECK(component <> 'close_recovery')")
    conn.commit()
    with pytest.raises(psycopg2.IntegrityError):recover_closes(conn,lambda _:close_receipt(pid))
    with conn.cursor() as q:
        q.execute('SELECT status FROM nwt_portfolio_ledger');assert q.fetchone()[0]=='open'
        q.execute('ALTER TABLE nwt_system_log DROP CONSTRAINT fail_close_audit')
    conn.commit()
    assert recover_closes(conn,lambda _:close_receipt(pid))['recorded']==[pid]
    assert recover_closes(conn,lambda _:pytest.fail('Already closed'))['recorded']==[]
    with conn.cursor() as q:
        q.execute('SELECT status,exit_price,exit_time FROM nwt_portfolio_ledger')
        row=q.fetchone();assert row[:2]==('closed',Decimal('89.66375'))
        assert row[2].isoformat()=='2026-09-17T15:00:00+00:00'


@pytest.mark.parametrize('change',[{'symbol':'EWA'},{'side':'buy'},{'qty':'33'},{'filled_qty':'31'}, {'filled_avg_price':'NaN'}, {'filled_at':'2026-09-16T15:00:00Z'}])
def test_close_receipt_conflict_leaves_ledger_open(close_db,change):
    from close_recovery import recover_closes
    conn,pid=close_db
    with pytest.raises(ValueError):recover_closes(conn,lambda _:close_receipt(pid,**change))
    with conn.cursor() as q:
        q.execute('SELECT status FROM nwt_portfolio_ledger');assert q.fetchone()[0]=='open'


def test_partial_close_is_not_mistaken_for_full_close(close_db):
    from close_recovery import recover_closes
    conn,pid=close_db
    result=recover_closes(conn,lambda _:close_receipt(pid,status='canceled',filled_qty='12'))
    assert result['pending']==[pid] and result['residual_retries']==[pid]
    with conn.cursor() as q:
        q.execute("SELECT qty,close_generation FROM nwt_portfolio_ledger WHERE status='open'")
        assert q.fetchone()==(Decimal(20),1)
        q.execute("SELECT qty FROM nwt_portfolio_ledger WHERE status='closed'");assert q.fetchone()[0]==12


def test_short_option_delayed_close_and_ticket_attribution(close_db):
    from close_recovery import recover_closes
    conn,pid=close_db
    with conn.cursor() as q:
        q.execute("UPDATE nwt_portfolio_ledger SET direction='short',asset_type='option'")
        q.execute("INSERT INTO nwt_tickets(ticket_id,type,payload) VALUES(%s,'CLOSE_REQUEST',%s)",
                  (OID,json.dumps({'position_id':pid})))
    conn.commit()
    assert recover_closes(conn,lambda _:close_receipt(pid,side='buy'))['recorded']==[pid]
    with conn.cursor() as q:
        q.execute('SELECT decision FROM nwt_ticket_decisions WHERE ticket_id=%s',(OID,))
        assert q.fetchone()[0]=='EXECUTED'



def test_partial_entry_grows_without_duplicate_position(recovery_db):
    first=dict(ORDER,status='partially_filled',filled_qty='12')
    assert recover_entries(recovery_db,lambda _:first)['pending']==[TID]
    recover_entries(recovery_db,lambda _:first)
    recover_entries(recovery_db,lambda _:ORDER)
    with recovery_db.cursor() as q:
        q.execute('SELECT qty FROM nwt_portfolio_ledger');assert q.fetchall()==[(Decimal(32),)]
        q.execute('SELECT decision FROM nwt_ticket_decisions');assert q.fetchone()[0]=='EXECUTED'


def test_spread_receipts_require_every_price_and_write_atomically(recovery_db):
    p=dict(PAYLOAD,asset_type='option',qty=32,legs=[{'option_symbol':'AAA','side':'buy'},{'option_symbol':'BBB','side':'sell'}])
    parent=dict(ORDER,order_class='mleg',legs=[dict(ORDER,symbol='AAA',side='buy'),dict(ORDER,symbol='BBB',side='sell',filled_avg_price='80')])
    with recovery_db.cursor() as q:q.execute('UPDATE nwt_tickets SET payload=%s',(json.dumps(p),))
    recovery_db.commit()
    bad=deepcopy(parent);bad['legs'][1]['filled_avg_price']=None
    with pytest.raises(ValueError):recover_entries(recovery_db,lambda _:bad)
    with recovery_db.cursor() as q:
        q.execute('SELECT COUNT(*) FROM nwt_portfolio_ledger');assert q.fetchone()[0]==0
    result=recover_entries(recovery_db,lambda _:parent)
    assert len(result['recorded'])==2
    with recovery_db.cursor() as q:
        q.execute('SELECT COUNT(DISTINCT spread_group_id),COUNT(*) FROM nwt_portfolio_ledger');assert q.fetchone()==(1,2)



def test_partial_close_restart_records_only_incremental_fills(close_db):
    from close_recovery import recover_closes
    conn,pid=close_db
    first=close_receipt(pid,status='partially_filled',filled_qty='12',filled_avg_price='90')
    recover_closes(conn,lambda _:first);recover_closes(conn,lambda _:first)
    recover_closes(conn,lambda _:close_receipt(pid,filled_avg_price='91'))
    with conn.cursor() as q:
        q.execute("SELECT SUM(qty),SUM(qty*exit_price),COUNT(*) FROM nwt_portfolio_ledger WHERE status='closed'")
        qty,value,count=q.fetchone();assert qty==32 and value==32*91 and count==2
        q.execute('SELECT COUNT(DISTINCT spread_group_id) FROM nwt_portfolio_ledger');assert q.fetchone()[0]==1


def test_canceled_residual_uses_new_deterministic_identity(close_db):
    from close_recovery import recover_closes,close_client_id
    conn,pid=close_db
    recover_closes(conn,lambda _:close_receipt(pid,status='canceled',filled_qty='12'))
    client=close_client_id({'position_id':pid,'close_generation':1})
    assert len(client)<=48 and client!='nwt-close-'+pid
    order=close_receipt(pid,client_order_id=client,qty='20',filled_qty='20')
    assert recover_closes(conn,lambda _:order)['recorded']==[pid]
    with conn.cursor() as q:
        q.execute("SELECT SUM(qty) FROM nwt_portfolio_ledger WHERE status='closed'");assert q.fetchone()[0]==32



def test_partial_timestamp_requires_matching_broker_fill_activities():
    from fill_evidence import with_fill_timestamp
    order=dict(ORDER,filled_at=None,status='partially_filled',submitted_at='2026-09-17T13:30:00Z')
    activity=dict(id='fill-1',order_id=OID,symbol='VGK',side='buy',qty='32',price=ORDER['filled_avg_price'],transaction_time=ORDER['filled_at'])
    assert with_fill_timestamp(order,lambda _:[activity])['filled_at'].startswith('2026-09-17T13:32:36')
    with pytest.raises(ValueError):with_fill_timestamp(order,lambda _:[dict(activity,qty='31')])



def test_partial_close_retry_limit_preserves_verified_fill(close_db):
    from close_recovery import recover_closes,close_client_id
    conn,pid=close_db
    with conn.cursor() as q:q.execute('UPDATE nwt_portfolio_ledger SET close_generation=3')
    conn.commit()
    client=close_client_id({'position_id':pid,'close_generation':3})
    result=recover_closes(conn,lambda _:close_receipt(pid,client_order_id=client,status='canceled',filled_qty='12'))
    assert result['blocked'] and not result['residual_retries']
    with conn.cursor() as q:
        q.execute("SELECT qty FROM nwt_portfolio_ledger WHERE status='open'");assert q.fetchone()[0]==20
