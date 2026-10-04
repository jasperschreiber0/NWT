import json
import sys
from pathlib import Path
from datetime import datetime,timezone,timedelta
from unittest.mock import MagicMock

import pytest
import engine
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'research'))
import paper_bridge as bridge


def prepare_case(monkeypatch,tmp_path):
    now=datetime.now(timezone.utc);key='test-rule';day=now.date().isoformat()
    cohort=dict(entry=day,exit=(now+timedelta(days=7)).date().isoformat(),entry_at=(now-timedelta(minutes=3)).isoformat(),
        frozen_at=(now-timedelta(hours=10)).isoformat(),allocation={key:.2},targets={key:{'XLK':.5}},data_sha256='source')
    report=dict(allocation={key:.2},hypotheses={key:{'verdict':{'state':'PAPER_QUALIFIED'}}})
    monkeypatch.setattr(bridge,'STATE',tmp_path);(tmp_path/'trial.json').write_text(json.dumps({'status':'PASSED'}))
    monkeypatch.setattr(bridge,'evidence',lambda *a:(report,[cohort],{key:{'state':'PAPER_QUALIFIED'}},set()))
    monkeypatch.setattr(bridge,'ledger_rows',lambda *a:[]);monkeypatch.setattr(bridge,'exposure',lambda *a:{})
    monkeypatch.setattr(bridge,'symbol_in_use',lambda *a:False)
    p=dict(approved=True,bot_source='RESEARCH_LAB',strategy_id=bridge.identity('core:'+key),symbol='XLK',direction='long',
        sized_notional=500,asset_type='equity',time_in_force='day',stop_pct=.03,target_pct=10.,
        research=dict(study='core',experiment=key,entry=day,exit=cohort['exit'],policy=bridge.POLICY['version'],source_hash='source'))
    ticket=dict(ticket_id=bridge.ticket_id('core:'+key,day,'XLK'),from_agent=bridge.SOURCE,created_at=now,payload=p)
    monkeypatch.setattr(engine,'require_operations_health',lambda:None)
    monkeypatch.setattr(engine,'synchronous_risk_veto',lambda *a:(False,''))
    monkeypatch.setattr(engine,'check_directional_cap',lambda *a:(False,0,10000))
    import strategy_review
    monkeypatch.setattr(strategy_review,'entry_gate',lambda *a:None)
    monkeypatch.setattr(engine,'reserve',MagicMock(return_value=True))
    monkeypatch.setattr(engine,'insert_decision',MagicMock());monkeypatch.setattr(engine,'mark_decision_outcome',MagicMock())
    monkeypatch.setattr(engine,'log_system_event',MagicMock())
    monkeypatch.setattr(engine,'get_current_price',lambda *a:50.)
    monkeypatch.setattr(engine,'get_latest_quote',lambda *a:(49.99,50))
    response=MagicMock();response.json.return_value={'quote':{'bp':49.99,'ap':50,'bs':10,'as':10,'t':now.isoformat()}}
    monkeypatch.setattr(engine.requests,'get',lambda *a,**k:response)
    monkeypatch.setattr(engine,'alpaca_get',lambda *a:{'cash':'10000'})
    monkeypatch.setattr(engine,'submit_identified_order',MagicMock(return_value={'id':'paper-order'}))
    monkeypatch.setattr(engine,'poll_order_until_filled',lambda *a:{'status':'filled','filled_avg_price':'50','filled_qty':'9'})
    monkeypatch.setattr(engine,'insert_position',MagicMock(return_value='position'))
    return ticket


def test_qualified_proposal_reaches_existing_fill_ledger(monkeypatch,tmp_path):
    ticket=prepare_case(monkeypatch,tmp_path)
    engine.process_ticket(MagicMock(),ticket,{})
    order=engine.submit_identified_order.call_args[0][0]
    assert order['type']=='limit' and int(order['qty'])*float(order['limit_price'])<=500
    assert order['client_order_id']=='nwt-entry-'+ticket['ticket_id']
    recorded=engine.insert_position.call_args[0][1]
    assert recorded['ticket_id']==ticket['ticket_id'] and recorded['strategy_id']==ticket['payload']['strategy_id']
    assert recorded['qty']==9 and recorded['alpaca_order_id']=='paper-order'


def test_failed_trial_cannot_reach_broker(monkeypatch,tmp_path):
    ticket=prepare_case(monkeypatch,tmp_path)
    (tmp_path/'trial.json').write_text(json.dumps({'status':'RUNNING'}))
    engine.process_ticket(MagicMock(),ticket,{})
    engine.submit_identified_order.assert_not_called();engine.reserve.assert_not_called()


def test_forged_source_cannot_reach_broker(monkeypatch,tmp_path):
    ticket=prepare_case(monkeypatch,tmp_path);ticket['from_agent']='UNKNOWN'
    engine.process_ticket(MagicMock(),ticket,{})
    engine.submit_identified_order.assert_not_called()


def test_live_endpoint_cannot_reach_broker(monkeypatch,tmp_path):
    ticket=prepare_case(monkeypatch,tmp_path);monkeypatch.setattr(engine,'ALPACA_BASE_URL','https://api.alpaca.markets')
    engine.process_ticket(MagicMock(),ticket,{})
    engine.submit_identified_order.assert_not_called()
