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
        CREATE TEMP TABLE nwt_tickets(ticket_id uuid PRIMARY KEY,payload jsonb,created_at timestamptz DEFAULT now());
        CREATE TEMP TABLE nwt_entry_intents(ticket_id uuid);
        CREATE TEMP TABLE nwt_ticket_decisions(ticket_id uuid,decision text,reasoning text,decided_by text);
        CREATE TEMP TABLE nwt_decision_inputs(ticket_id uuid,outcome_reason text,stage_reached text);
        CREATE TEMP TABLE nwt_system_log(level text,component text,message text,payload jsonb);
        CREATE TEMP TABLE nwt_portfolio_ledger(position_id uuid DEFAULT gen_random_uuid(),bot_source text,strategy_id text,
          asset text,asset_type text,direction text,delta_exposure numeric,notional_risk numeric,qty numeric,entry_price numeric,
          entry_time timestamptz,entry_bid numeric,entry_ask numeric,alpaca_order_id uuid,stop_pct numeric,target_pct numeric,
          spread_group_id uuid,ticket_id uuid,status text);
        ''')
        q.execute('INSERT INTO nwt_tickets(ticket_id,payload) VALUES(%s,%s)',(TID,json.dumps(PAYLOAD)))
        q.execute('INSERT INTO nwt_entry_intents VALUES(%s)',(TID,))
        q.execute("INSERT INTO nwt_decision_inputs VALUES(%s,'EXECUTION_FAILED','EXECUTION')",(TID,))
    conn.commit()
    yield conn
    conn.close()


def test_late_fill_recovers_once_with_exact_broker_evidence(recovery_db):
    conn=recovery_db
    assert len(recover_entries(conn,lambda _:deepcopy(ORDER))['recorded'])==1
    assert recover_entries(conn,lambda _:pytest.fail('must not fetch completed order'))['recorded']==[]
    with conn.cursor() as q:
        q.execute('SELECT qty,entry_price,notional_risk,stop_pct,target_pct FROM nwt_portfolio_ledger')
        assert q.fetchall()==[(Decimal(32),Decimal('89.66375'),Decimal('2869.24000'),Decimal('-.015'),Decimal('.03'))]
        q.execute('SELECT outcome_reason FROM nwt_decision_inputs')
        assert q.fetchone()[0]=='EXECUTED'


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
