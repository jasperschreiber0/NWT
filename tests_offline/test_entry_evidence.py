import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'nwt_agents'))
from entry_evidence import snapshot


def facts():
    return ({'ticket_id':'request','entry_time':'2026-10-20T12:00:00Z','strategy_id':'A','qty':2},
            {'ticket_id':'request','created_at':'2026-10-20T11:00:00Z','payload':{'source_proposal_ticket_id':'proposal'}},
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


@pytest.mark.parametrize('failure', ['missing_ticket','missing_source','two_decisions','late_decision','late_source','late_ticket','wrong_strategy','missing_regime'])
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
