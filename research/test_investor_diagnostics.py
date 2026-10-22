"""Offline regressions for the Oct 14 failures; no paid calls or relaxed gates."""
import json
import sqlite3
import sys
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock

import pytest
import investor_core as core
import investor_pipeline as pipeline
import investor_options as options
import investor_diagnostics as diagnostics
from test_investor_backoff import database

NOW=datetime(2026,10,14,13,36,tzinfo=timezone.utc)
PURPOSE='We have no current plans to change the board.'
SECRET='sk-secret-test Authorization=private raw-provider-payload'


def activist(c,key='event',**overrides):
    value=dict(event_id=key,baseline=False,purpose=PURPOSE,first_seen_at=NOW.isoformat(),
        source=dict(sha256='source'),changed=True,symbol='TEST',reporters=['1'])
    value.update(overrides);pipeline.put(c,'activist',key,value)
    return value


def answer(**overrides):
    value=dict(action_present=False,action='none',direction='uncertain',evidence_id=1)
    value.update(overrides);return value


def api(monkeypatch,*,value=None,error=None):
    # This module does not call the API at import. Network is also blocked by conftest.
    sys.path.insert(0,str(pipeline.ROOT/'nwt_agents'))
    import openai_client
    def respond(prompt,*args,**kwargs):
        if error:raise error
        sources=json.loads(prompt.split('SOURCES:\n',1)[1])
        return dict(classifications=[dict(value if value is not None else answer(),event_id=p['event_id']) for p in sources]),10,5
    call=Mock(side_effect=respond)
    monkeypatch.setattr(openai_client,'call_json',call)
    return call


@pytest.mark.parametrize('value,reason',[
    (answer(action_present='true'),'CLASSIFICATION_SCHEMA'),
    (answer(action='invented'),'CLASSIFICATION_SCHEMA'),
    (answer(evidence_id=SECRET),'CLASSIFICATION_SOURCE_QUOTE'),
    (answer(action_present=True),'CLASSIFICATION_ACTION'),
])
def test_classification_validation_patterns_are_safe_and_stay_failed(monkeypatch,value,reason):
    c=database();activist(c);call=api(monkeypatch,value=value)
    result=pipeline.classify_activists(c,NOW)
    assert result['status']=='DEGRADED' and result['reason']==reason
    assert result['stage']=='classification_validation' and call.call_count==1
    assert pipeline.rows(c,'classification')==[] and pipeline.candidates(c,NOW)==[]
    errors=pipeline.rows(c,'model_error')
    assert errors[0]['reason']==reason and SECRET not in json.dumps([result,errors])
    assert pipeline.classification_coverage(c,NOW)['unresolved_with_errors']==1
    # Existing one-hour cooldown still blocks a repeat call for this filing.
    pipeline.classify_activists(c,NOW+timedelta(minutes=5))
    assert call.call_count==1
    assert pipeline.classification_coverage(c,NOW+timedelta(minutes=5))['cooldown']==1
    pipeline.classify_activists(c,NOW+timedelta(hours=1))
    assert call.call_count==2 and len(pipeline.rows(c,'model_error'))==2


@pytest.mark.parametrize('error,reason',[
    (ValueError(SECRET),'INVALID_VALUE'),
    (json.JSONDecodeError(SECRET,SECRET,0),'RESPONSE_JSON_INVALID'),
    (TimeoutError(SECRET),'DEPENDENCY_TIMEOUT'),
    (RuntimeError(SECRET),'DEPENDENCY_OR_RUNTIME_FAILURE'),
    (type('SecretClass'+SECRET,(Exception,),{})(SECRET),'UNEXPECTED_FAILURE'),
])
def test_model_request_errors_never_echo_exception_or_response(monkeypatch,error,reason):
    c=database();activist(c);api(monkeypatch,error=error)
    result=pipeline.classify_activists(c,NOW)
    assert result['stage']=='classification_request' and result['reason']==reason
    assert SECRET not in json.dumps([result,pipeline.rows(c,'model_error')])


def test_budget_exhaustion_keeps_unresolved_coverage_without_spending(monkeypatch):
    c=database();activist(c);call=api(monkeypatch)
    pipeline.state(c,'model-budget:'+NOW.date().isoformat(),core.POLICY['maximum_model_attempts_per_day'])
    pipeline.put(c,'model_error','event:old',dict(error='ValueError',attempt=1))
    old=c.execute('SELECT * FROM evidence').fetchall()
    assert pipeline.classify_activists(c,NOW)['status']=='DAILY_BUDGET_REACHED'
    coverage=pipeline.classification_coverage(c,NOW)
    assert coverage['status']=='INCOMPLETE' and coverage['pending']==coverage['budget_blocked']==1
    assert coverage['unresolved_with_errors']==1 and coverage['attempt_limit']==core.POLICY['maximum_model_attempts_per_day']
    call.assert_not_called();assert c.execute('SELECT * FROM evidence').fetchall()==old


def test_final_budget_attempt_and_midnight_do_not_bypass_cooldown(monkeypatch):
    monkeypatch.setitem(core.POLICY,'classification_batch_size',1)
    c=database();activist(c);activist(c,'second');call=api(monkeypatch,error=ValueError(SECRET))
    late=NOW.replace(hour=23,minute=55)
    pipeline.state(c,'model-budget:'+late.date().isoformat(),core.POLICY['maximum_model_attempts_per_day']-1)
    assert pipeline.classify_activists(c,late)['attempts_today']==core.POLICY['maximum_model_attempts_per_day']
    assert pipeline.classify_activists(c,late)['status']=='DAILY_BUDGET_REACHED'
    assert call.call_count==1
    pipeline.classify_activists(c,late+timedelta(minutes=10))
    assert call.call_count==2  # The other filing only; first remains on cooldown.
    assert pipeline.state(c,'classification-attempt:event')==late.isoformat()


def test_coverage_distinguishes_exclusions_and_success_without_changing_evidence(monkeypatch):
    c=database();activist(c);activist(c,'old',first_seen_at=(NOW-timedelta(days=3)).isoformat())
    activist(c,'empty',purpose='');activist(c,'history',baseline=True)
    pipeline.state(c,'model-baseline-sample','prior')
    call=api(monkeypatch);pipeline.classify_activists(c,NOW)
    coverage=pipeline.classification_coverage(c,NOW)
    assert coverage['classified']==1 and coverage['pending']==0 and coverage['status']=='EXCLUDED_ONLY'
    assert coverage['expired']==coverage['missing_purpose']==coverage['historical_excluded']==1
    assert call.call_count==1 and len(pipeline.rows(c,'activist'))==4


def test_classification_storage_failure_is_not_success(monkeypatch):
    c=database();activist(c);api(monkeypatch)
    original=pipeline.put
    def put(conn,kind,key,value):
        if kind=='classification':raise sqlite3.OperationalError(SECRET)
        return original(conn,kind,key,value)
    monkeypatch.setattr(pipeline,'put',put)
    with pytest.raises(sqlite3.Error):pipeline.classify_activists(c,NOW)


def forecast(c):
    pipeline.put(c,'forecast','f',dict(key='f',symbol='TEST',entry='2026-10-14',exit='2026-10-21',
        entry_at='2026-10-14T13:30:00+00:00',exit_at='2026-10-21T13:30:00+00:00'))


def option_run(c,paper,now=NOW):return options.run(c,paper,now,pipeline.rows,pipeline.one,pipeline.put)


def quote(now=NOW):return dict(bp=100,ap=100.1,bs=10,**{'as':10},t=now.isoformat())


@pytest.fixture
def option_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW
    monkeypatch.setattr(options,'datetime',Clock)


@pytest.mark.parametrize('mode,stage,reason',[
    ('stale','option_selection','EQUITY_QUOTE_UNTRADEABLE'),
    ('repeat','option_chain','OPTION_PAGINATION_REPEATED'),
    ('budget','option_chain','OPTION_CHAIN_BUDGET'),
    ('value','option_equity_quote','INVALID_VALUE'),
])
def test_option_failures_report_safe_stage_without_accepting_bad_book(monkeypatch,option_clock,mode,stage,reason):
    c=database();forecast(c);paper=Mock();pages=[]
    def get(path,*a,**kw):
        if mode=='value':raise ValueError(SECRET)
        if '/stocks/' in path:return dict(quote=quote(NOW-timedelta(minutes=5) if mode=='stale' else NOW))
        pages.append(path)
        return dict(snapshots={},next_page_token='repeat' if mode=='repeat' else (str(len(pages)) if mode=='budget' else None))
    paper.get.side_effect=get
    result=option_run(c,paper);error=result['current_errors'][0]
    assert error['stage']==stage and error['reason']==reason and SECRET not in json.dumps(result)
    assert pipeline.rows(c,'option_book')==[] and pipeline.rows(c,'option_result')==[]
    assert len(pages)<=4  # Existing chain request budget.
    paper.get.side_effect=lambda path,*a,**kw:dict(quote=quote()) if '/stocks/' in path else dict(snapshots={})
    assert option_run(c,paper)['current_errors']==[]
    assert pipeline.one(c,'option_book','f')['option_availability']=='NO_ELIGIBLE_SPREAD'
    assert pipeline.rows(c,'option_result')==[]  # No invented completed outcome.


def test_option_missed_window_does_not_fetch_or_invent_result(option_clock):
    c=database();forecast(c);paper=Mock()
    result=option_run(c,paper,NOW+timedelta(minutes=21))
    paper.get.assert_not_called()
    assert result['missed_entries']==1 and result['completed']==0


def test_option_storage_failure_propagates(option_clock,monkeypatch):
    c=database();forecast(c);paper=Mock()
    paper.get.side_effect=lambda path,*a,**kw:dict(quote=quote()) if '/stocks/' in path else dict(snapshots={})
    with pytest.raises(sqlite3.OperationalError):
        options.run(c,paper,NOW,pipeline.rows,pipeline.one,Mock(side_effect=sqlite3.OperationalError(SECRET)))


def test_v3_boundary_preserves_v1_v2_and_pending_forecast_lineage():
    c=database()
    for version in [core.LEGACY_VERSION,core.PREVIOUS_VERSION,core.DIAGNOSTICS_VERSION]:
        pipeline.put(c,'policy',version,dict(version=version,code_sha256=version))
    pipeline.put(c,'forecast','pending',dict(policy_version=core.PREVIOUS_VERSION))
    before=c.execute('SELECT * FROM evidence ORDER BY kind,key').fetchall()
    policy=dict(core.POLICY,code_sha256='v3')
    pipeline.register_version(c,policy,NOW);pipeline.register_version(c,policy,NOW)
    assert all(row in c.execute('SELECT * FROM evidence').fetchall() for row in before)
    assert len(pipeline.rows(c,'version_boundary'))==1
    with pytest.raises(ValueError):pipeline.register_version(c,dict(policy,code_sha256='changed'),NOW)


@pytest.mark.parametrize('mode',['budget','cooldown','failure','storage','option','partial_success'])
def test_full_cycle_reports_incomplete_coverage_without_retries_or_false_success(monkeypatch,tmp_path,mode):
    out=tmp_path/'evidence';out.mkdir();c=sqlite3.connect(out/'investors.sqlite')
    c.execute('CREATE TABLE evidence(kind TEXT,key TEXT,payload TEXT,PRIMARY KEY(kind,key))')
    c.execute('CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT)')
    activist(c);pipeline.state(c,'tickers',dict(at=NOW.isoformat(),mapping={}))
    if mode=='partial_success':
        activist(c,'second')
        monkeypatch.setitem(core.POLICY,'classification_batch_size',1)
    if mode=='budget':pipeline.state(c,'model-budget:'+NOW.date().isoformat(),core.POLICY['maximum_model_attempts_per_day'])
    if mode=='cooldown':pipeline.state(c,'classification-attempt:event',NOW.isoformat())
    c.close();call=api(monkeypatch,error=ValueError(SECRET) if mode=='failure' else None)
    monkeypatch.setattr(pipeline.fcntl,'flock',lambda *a:None)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW
    monkeypatch.setattr(pipeline,'datetime',Clock)
    monkeypatch.setattr(pipeline,'Public',lambda *a:Mock())
    monkeypatch.setattr(pipeline,'collect_feed',lambda *a:dict(status='OK'))
    monkeypatch.setattr(pipeline,'parse_filings',lambda *a:dict(parsed=0,backlog=0,errors=[]))
    monkeypatch.setattr(pipeline,'collect_managers',lambda *a:{})
    monkeypatch.setattr(pipeline,'collect_ark',lambda *a:{})
    monkeypatch.setattr(pipeline,'eligible_ark',lambda *a:[])
    monkeypatch.setattr(pipeline,'market_context',lambda *a:{})
    monkeypatch.setattr(pipeline,'propose',lambda *a:0)
    monkeypatch.setattr(pipeline,'evaluate',lambda *a:dict(completed=0,unresolved=0))
    monkeypatch.setattr(options,'run',lambda *a:dict(current_errors=[dict(reason='INVALID_VALUE')]) if mode=='option' else {})
    paper=Mock();paper.get.return_value=[]
    monkeypatch.setattr(pipeline,'Paper',lambda:paper)
    monkeypatch.setattr(sys,'argv',['investor_pipeline','--output',str(out)])
    if mode=='storage':monkeypatch.setattr(pipeline,'classify_activists',Mock(side_effect=sqlite3.OperationalError(SECRET)))
    retryable=mode in ['storage','option']
    if retryable:
        with pytest.raises(SystemExit) as exc:pipeline.main()
        assert exc.value.code==1
    else:pipeline.main()
    report=json.loads((out/'latest.json').read_text())
    assert report['status']=='DEGRADED'
    assert report['classification_coverage']['pending']==(0 if mode=='option' else 1)
    assert report['retryable']==retryable
    assert report['execution_enabled'] is False and report['scored'].get('completed',0)==0
    assert SECRET not in json.dumps(report)
    assert call.call_count==(1 if mode in ['failure','option','partial_success'] else 0)
