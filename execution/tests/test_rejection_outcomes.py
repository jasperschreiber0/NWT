import os
from datetime import datetime, timezone
from unittest.mock import Mock
import psycopg2
import pytest
import engine

TID='11111111-1111-1111-1111-111111111111'
PID='22222222-2222-2222-2222-222222222222'


@pytest.mark.parametrize('proposal',[False,True])
def test_budget_rejection_retains_detail_and_records_constrained_outcome(monkeypatch,proposal):
    conn=psycopg2.connect(os.environ['NWT_TEST_DB_DSN'])
    try:
        with conn.cursor() as q:
            q.execute('''CREATE TEMP TABLE nwt_decision_inputs(ticket_id uuid,outcome_reason text
              CHECK(outcome_reason IN ('NO_EDGE','BELOW_THRESHOLD','RISK_VETOED','EXECUTION_FAILED',
              'STRUCTURALLY_IMPOSSIBLE','DUPLICATE_POSITION','EXECUTED')));
              CREATE TEMP TABLE nwt_ticket_decisions(ticket_id uuid,decision text,reasoning text,
                decided_by text,created_at timestamptz DEFAULT now());
              CREATE UNIQUE INDEX rejection_decision_unique ON nwt_ticket_decisions(ticket_id,decided_by)
                WHERE created_at >= TIMESTAMPTZ '2026-07-24 00:00:00+00';''')
            q.execute('INSERT INTO nwt_decision_inputs VALUES(%s,NULL)',(PID if proposal else TID,))
        conn.commit()
        monkeypatch.setattr(engine,'require_operations_health',lambda:None)
        monkeypatch.setattr(engine,'synchronous_risk_veto',lambda *a:(False,''))
        monkeypatch.setattr('strategy_review.entry_gate',lambda *a:'LIMITED_EXPERIMENT: entry budget exceeds $500')
        reserve=Mock();post=Mock();monkeypatch.setattr(engine,'reserve',reserve);monkeypatch.setattr(engine,'alpaca_post',post)
        payload=dict(approved=True,bot_source='AUS_BOT',strategy_id='AUS-DIV-001',symbol='EWA',direction='long',asset_type='equity',sized_notional=1600,time_in_force='day')
        if proposal:payload['source_proposal_ticket_id']=PID
        ticket=dict(ticket_id=TID,created_at=datetime.now(timezone.utc),payload=payload)
        engine.process_ticket(conn,ticket,{})
        engine.mark_decision_outcome(conn,PID if proposal else TID,'EXECUTED')
        with conn.cursor() as q:
            q.execute('SELECT outcome_reason FROM nwt_decision_inputs');assert q.fetchone()==('RISK_VETOED',)
            q.execute('SELECT decision,reasoning FROM nwt_ticket_decisions');assert q.fetchone()==('REJECTED','STRATEGY_BUDGET: LIMITED_EXPERIMENT: entry budget exceeds $500')
        reserve.assert_not_called();post.assert_not_called()
    finally:conn.close()
