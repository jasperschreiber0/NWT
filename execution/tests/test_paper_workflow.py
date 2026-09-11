import sys,json,importlib.util
from pathlib import Path
from unittest.mock import MagicMock
import pytest
import engine

ROOT=Path(__file__).resolve().parents[2]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

@pytest.mark.parametrize('budget,qty,symbol,base,direction',[(1000,1,'SPY','paper','long'),(800,2,'SPY','paper','long'),(800,1,'AAPL','paper','long'),(800,1,'SPY','live','long'),(800,1,'SPY','paper','short')])
def test_qa_rejects_outside_bounds(monkeypatch,budget,qty,symbol,base,direction):
    monkeypatch.setattr(engine,'ALPACA_BASE_URL','https://paper-api.alpaca.markets' if base=='paper' else 'https://api.alpaca.markets')
    monkeypatch.setattr(engine,'get_current_price',lambda s:760)
    post=MagicMock();monkeypatch.setattr(engine,'alpaca_post',post)
    with pytest.raises(ValueError):engine.place_equity_order(dict(symbol=symbol,qty=qty,direction=direction,sized_notional=budget,time_in_force='day',strategy_id='QA_PAPER_LIFECYCLE',client_order_id='nwt-qa-entry-test'))
    post.assert_not_called()

def test_qa_exact_share_price_limit(monkeypatch):
    monkeypatch.setattr(engine,'ALPACA_BASE_URL','https://paper-api.alpaca.markets')
    monkeypatch.setattr(engine,'get_current_price',lambda s:760)
    post=MagicMock();monkeypatch.setattr(engine,'alpaca_post',post)
    engine.place_equity_order(dict(symbol='SPY',qty=1,direction='long',sized_notional=765,time_in_force='day',strategy_id='QA_PAPER_LIFECYCLE',client_order_id='nwt-qa-entry-test'))
    body=post.call_args.args[1];assert body['qty']=='1' and body['type']=='limit' and float(body['limit_price'])==765

def test_validation_holds_normal_entries():
    c=MagicMock();c.cursor.return_value.__enter__.return_value.fetchone.return_value=(True,)
    veto,reason=engine.synchronous_risk_veto(c,dict(strategy_id='C1'))
    assert veto and 'validation' in reason

def test_allocation_reduces_and_preserves_headroom():
    m=module('allocation','ops/allocate_aapl.py');sold,remain=m.size(100000,333)
    assert sold+remain==286 and remain*333<=30000 and sold>0
    assert m.size(1000000,333)==(0,286)
    with pytest.raises(ValueError):m.size(100000,0)

def test_research_archive_is_immutable(tmp_path):
    m=module('collect','research/collect.py');m.OUT=tmp_path
    digest=m.archive('test','one',{'status':'new'});p=tmp_path/'test'/(digest+'.json');first=p.read_bytes()
    assert m.archive('test','one',{'status':'new'})==digest and p.read_bytes()==first
    assert m.archive('test','one',{'status':'filled'})!=digest

def test_research_never_enables_orders():
    m=module('collect','research/collect.py')
    assert m.fixed_signals([100]*199,'SPY')['status']=='INSUFFICIENT_HISTORY'
    assert m.fixed_signals([100]*200,'SPY')['execution_enabled'] is False

@pytest.mark.parametrize('observed,expected',[('2026-01-01T22:00:00+00:00',1),('2026-01-03T22:00:00+00:00',0)])
def test_forward_labels_require_prior_observation(tmp_path,observed,expected):
    m=module('collect','research/collect.py');m.OUT=tmp_path
    folder=tmp_path/'frozen_signals';folder.mkdir()
    (folder/'signal.json').write_text(json.dumps({'key':'2026-01-01:SAFX','observed_at':observed,
       'payload':{'bar_timestamp':'2026-01-01T04:00:00Z','stale_session':False,'safx_bounce20_v1':True}}))
    bars=[{'t':f'2026-01-{d:02}T04:00:00Z','o':100} for d in range(1,10)]
    m.evaluate_observed_signals({'bars':{'SAFX':bars}})
    assert len(list((tmp_path/'forward_underlying_outcomes').glob('*.json')))==expected

@pytest.mark.parametrize('fail_allocation',[False,True])
def test_ordered_workflow_stops_before_qa_on_failure(monkeypatch,fail_allocation):
    m=module('workflow','ops/paper_workflow.py');events=[]
    recovery=MagicMock();recovery.main.side_effect=lambda:events.append('recovery')
    allocation=MagicMock()
    def allocate():
        events.append('allocation')
        if fail_allocation:raise RuntimeError('not filled')
    allocation.main.side_effect=allocate
    qa=MagicMock();qa.STATE.exists.return_value=False;qa.main.side_effect=lambda:events.append('qa')
    for name,obj in [('recover_vgk',recovery),('allocate_aapl',allocation),('paper_lifecycle',qa)]:monkeypatch.setitem(sys.modules,name,obj)
    recon=MagicMock();recon.run_recon.side_effect=lambda *a:True;monkeypatch.setitem(sys.modules,'recon_agent',recon)
    context=MagicMock();context.clear_no_trade_mode.side_effect=lambda *a:events.append('qa_only_release');monkeypatch.setitem(sys.modules,'shared_context',context)
    c=MagicMock();c.cursor.return_value.__enter__.return_value.fetchone.side_effect=[(True,),(True,'Recon critical mismatch: 1 untracked/qty-mismatch positions','recon_agent')]
    monkeypatch.setattr(engine,'get_db',lambda:c);monkeypatch.setattr(engine,'ALPACA_BASE_URL','https://paper-api.alpaca.markets');monkeypatch.setattr(engine,'log_system_event',MagicMock())
    if fail_allocation:
        with pytest.raises(RuntimeError):m.main()
        assert events==['recovery','allocation']
    else:
        m.main();assert events==['recovery','allocation','qa_only_release','qa']
