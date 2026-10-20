import sys
from pathlib import Path
from unittest.mock import MagicMock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'nwt_agents'))
from entry_evidence import snapshot


def facts():
    return ({'ticket_id':'request','entry_time':'2026-10-20T12:00:00Z','strategy_id':'A','qty':2,'asset':'SPY'},
            {'ticket_id':'request','created_at':'2026-10-20T11:00:00Z','payload':{'source_proposal_ticket_id':'proposal','symbol':'SPY'}},
            {'ticket_id':'proposal','created_at':'2026-10-20T10:00:00Z'},
            [{'id':'d1','ticket_id':'proposal','created_at':'2026-10-20T10:00:00Z',
              'run_at':'2026-10-20T10:00:00Z','strategy_id':'A','symbol':'SPY',
              'regime':{'primary_regime':'range'},'iv_at_decision':.2}])


def test_exact_origin_ignores_reused_symbol_and_freezes_context():
    p,t,source,ds=facts()
    ds.append(dict(ds[0],id='newer',ticket_id='other',run_at='2026-10-21T10:00:00Z'))
    result=snapshot(p,t,source,ds)
    assert result['eligible'] and result['decision_id']=='d1'
    assert result['iv_at_entry'] is None
    assert result['decision_context']['iv_at_decision']==.2
    ds[0]['regime']['primary_regime']='changed'
    assert result['decision_context']['regime']['primary_regime']=='range'


@pytest.mark.parametrize('failure', ['missing_ticket','missing_source','two_decisions','late_decision','late_source','late_ticket','wrong_strategy','missing_regime','wrong_asset','wrong_decision_symbol'])
def test_fail_closed(failure):
    p,t,source,ds=facts()
    if failure=='missing_ticket': t=None
    if failure=='missing_source': source=None
    if failure=='two_decisions': ds.append(dict(ds[0],id='d2'))
    if failure=='late_decision': ds[0]['created_at']='2026-10-21T00:00:00Z'
    if failure=='late_source': source['created_at']='2026-10-20T11:30:00Z'
    if failure=='late_ticket': t['created_at']='2026-10-21T00:00:00Z'
    if failure=='wrong_strategy': ds[0]['strategy_id']='B'
    if failure=='missing_regime': ds[0]['regime']=None
    if failure=='wrong_asset': p['asset']='QQQ'
    if failure=='wrong_decision_symbol': ds[0]['symbol']='QQQ'
    result=snapshot(p,t,source,ds)
    assert not result['eligible'] and result['decision_id'] is None
    assert result['iv_at_entry'] is None


def test_partial_fill_quantity_does_not_change_decision_snapshot():
    p,t,source,ds=facts()
    first=snapshot(p,t,source,ds)
    p['qty']=5
    assert snapshot(p,t,source,ds)==first


def test_ticket_lookup_does_not_fall_back_to_symbol():
    from learning_agent import find_original_ticket
    conn=MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value=None
    assert find_original_ticket(conn,'missing') is None
    sql,args=conn.cursor.return_value.__enter__.return_value.execute.call_args.args
    assert args==('missing',) and 'symbol' not in sql and 'ORDER BY' not in sql


def test_learning_keeps_missing_metrics_unknown_and_retries_idempotently(monkeypatch):
    import learning_agent as learning
    conn=MagicMock();q=conn.cursor.return_value.__enter__.return_value
    state={'query':'','saved':None}
    def execute(sql,args=None):
        state['query']=sql
        if 'INSERT INTO nwt_trade_outcomes' in sql:
            assert state['saved'] is None
            columns=sql.split('(',1)[1].split(')',1)[0].replace('\n','').split(',')
            state['saved']=dict(zip((x.strip() for x in columns),args))
    def fetchone():
        if 'INSERT INTO nwt_trade_outcomes' in state['query']:return ('outcome',)
        return (1,) if state['saved'] is not None else None
    q.execute.side_effect=execute;q.fetchone.side_effect=fetchone;q.fetchall.return_value=[]
    monkeypatch.setattr(learning,'get_db',lambda:conn)
    monkeypatch.setattr(learning,'fetch_unprocessed_closed_positions',lambda c:[dict(position_id='p',asset='SPY',
        asset_type='equity',strategy_id='A',entry_price=100,exit_price=101,qty=2,direction='long')])
    monkeypatch.setattr(learning,'load_entry_evidence',lambda *a:{'eligible':False})
    monkeypatch.setattr(learning,'compute_edge_summary',lambda c:[])
    monkeypatch.setattr(learning,'log_system_event',lambda *a:None)
    learning.main();learning.main()
    outcome=state['saved']
    assert outcome['pnl']==2 and outcome['evidence_version']=='raw-fill-unverified-20261020-v1'
    for name in ['iv_at_entry','iv_at_exit','dte_at_entry','slippage','entry_timing_score',
                 'exit_timing_score','expected_move_capture','realized_move_capture','slippage_adjusted_efficiency']:
        assert outcome[name] is None

