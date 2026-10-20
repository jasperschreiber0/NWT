"""Bounded classification repair; synthetic responses, never paid requests."""
import json
from datetime import timedelta
from unittest.mock import Mock

import pytest
import investor_core as core
import investor_pipeline as pipeline
from test_investor_backoff import database
from test_investor_diagnostics import NOW, activist, answer, api


def test_unattempted_event_precedes_repeated_lexicographic_failure(monkeypatch):
    c=database();activist(c,'aaa');activist(c,'zzz')
    pipeline.put(c,'model_error','aaa:old',dict(reason='CLASSIFICATION_SOURCE_QUOTE'))
    pipeline.state(c,'classification-attempt:aaa',(NOW-timedelta(hours=2)).isoformat())
    call=api(monkeypatch)
    assert pipeline.classify_activists(c,NOW)['status']=='CLASSIFIED'
    assert pipeline.one(c,'classification','zzz') and not pipeline.one(c,'classification','aaa')
    assert call.call_count==1


def test_legacy_errors_cap_survives_midnight_and_keeps_pending(monkeypatch):
    c=database();activist(c)
    for n in range(3):pipeline.put(c,'model_error','event:'+str(n),dict(reason='CLASSIFICATION_SOURCE_QUOTE'))
    before=c.execute('SELECT * FROM evidence').fetchall();call=api(monkeypatch)
    for when in [NOW,NOW+timedelta(days=1)]:
        assert pipeline.classify_activists(c,when)['status']=='UNRESOLVED_DEFERRED'
        v=pipeline.classification_coverage(c,when)
        assert v['pending']==v['attempt_limit_blocked']==1 and v['ready']==0
    call.assert_not_called();assert c.execute('SELECT * FROM evidence').fetchall()==before


def test_failed_intent_counts_once_and_unfinished_intent_counts(monkeypatch):
    c=database();activist(c);api(monkeypatch,value=answer(evidence_quote='Invented words here.'))
    assert pipeline.classify_activists(c,NOW)['retryable'] is False
    assert pipeline.attempt_count(c,'event')==1
    pipeline.put(c,'classification_request','event:crash',dict(event_id='event',attempt_key='event:crash'))
    assert pipeline.attempt_count(c,'event')==2


@pytest.mark.parametrize('text,status,http',[
    ('not json','completed',200),('{}','completed',200),
    (json.dumps(answer(evidence_quote='not in the source')),'completed',200),
    (json.dumps(answer()),'incomplete',200),('refused','completed',403),
])
def test_original_response_and_request_survive_every_rejection(monkeypatch,text,status,http):
    import openai_client
    c=database();activist(c);monkeypatch.setenv('OPENAI_API_KEY','test-key-only')
    body=dict(id='response-test',status=status,usage={},output=[dict(type='message',content=[dict(type='output_text',text=text)])])
    raw=json.dumps(body);post=Mock(return_value=Mock(status_code=http,text=raw,json=lambda:body))
    monkeypatch.setattr(openai_client.requests,'post',post)
    result=pipeline.classify_activists(c,NOW)
    assert result['status']=='DEGRADED' and pipeline.rows(c,'classification')==[]
    assert result['retryable'] is False
    request=pipeline.rows(c,'classification_request')[0];receipt=pipeline.rows(c,'classification_response')[0]
    assert receipt['response_raw']==raw and receipt['attempt_key']==request['attempt_key']
    assert receipt['request']==post.call_args.kwargs['json']
    assert receipt['request']['text']['format']['strict'] is True
    assert pipeline.state(c,'model-budget:'+NOW.date().isoformat())==1
    assert raw not in json.dumps([result,pipeline.rows(c,'model_error')])
    assert post.call_count==1


def test_schema_opt_in_and_receipt_before_parser(monkeypatch):
    import openai_client
    monkeypatch.setenv('OPENAI_API_KEY','test-only')
    post=Mock(return_value=Mock(status_code=200,text='broken',json=Mock(side_effect=ValueError('parse'))))
    monkeypatch.setattr(openai_client.requests,'post',post);saved=[]
    with pytest.raises(ValueError):openai_client.call_json('prompt','gpt-4.1-mini',dict,capture=saved.append)
    assert 'text' not in post.call_args.kwargs['json'] and saved[0]['response_raw']=='broken'


def test_transport_error_retains_intent_and_remains_retryable(monkeypatch):
    c=database();activist(c);api(monkeypatch,error=TimeoutError('private'))
    result=pipeline.classify_activists(c,NOW)
    assert result['retryable'] and result['reason']=='DEPENDENCY_TIMEOUT'
    assert pipeline.rows(c,'classification_request') and not pipeline.rows(c,'classification_response')


def test_capture_storage_failure_cannot_accept_or_retry_unreserved(monkeypatch):
    import openai_client,sqlite3
    c=database();activist(c);monkeypatch.setenv('OPENAI_API_KEY','test-only')
    body=dict(status='completed',usage={},output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(answer()))])])
    monkeypatch.setattr(openai_client.requests,'post',Mock(return_value=Mock(status_code=200,text=json.dumps(body),json=lambda:body)))
    original=pipeline.put
    def put(conn,kind,key,value):
        if kind=='classification_response':raise sqlite3.OperationalError('disk')
        return original(conn,kind,key,value)
    monkeypatch.setattr(pipeline,'put',put)
    with pytest.raises(sqlite3.Error):pipeline.classify_activists(c,NOW)
    assert pipeline.attempt_count(c,'event')==1 and not pipeline.rows(c,'classification')


def test_exact_source_text_is_not_repaired_or_paraphrased():
    purpose='The investor?s letter says: "No current plans to change the board."'
    assert core.validate_catalyst(answer(evidence_quote=purpose),purpose)
    for quote in [purpose.replace('?',"'"),purpose.replace('No current plans','Plans'),purpose+'...']:
        with pytest.raises(ValueError):core.validate_catalyst(answer(evidence_quote=quote),purpose)


def test_v4_boundary_retains_all_three_prior_versions():
    c=database()
    for version in [core.LEGACY_VERSION,core.PREVIOUS_VERSION,core.DIAGNOSTIC_VERSION]:
        pipeline.put(c,'policy',version,dict(version=version,code_sha256=version))
    old=c.execute('SELECT * FROM evidence').fetchall()
    pipeline.register_version(c,dict(core.POLICY,code_sha256='new'),NOW)
    assert all(row in c.execute('SELECT * FROM evidence').fetchall() for row in old)
    assert len(pipeline.rows(c,'policy'))==4

@pytest.mark.parametrize('http,part,expected_retry',[
    (200,dict(type='refusal',refusal='Cannot classify'),False),
    (429,dict(type='output_text',text='{}'),True),
    (500,dict(type='output_text',text='{}'),True),
])
def test_refusal_and_transient_provider_status_keep_original(monkeypatch,http,part,expected_retry):
    import openai_client
    c=database();activist(c);monkeypatch.setenv('OPENAI_API_KEY','test-only')
    body=dict(id='original',status='completed',usage={},output=[dict(type='message',content=[part])])
    raw=json.dumps(body);post=Mock(return_value=Mock(status_code=http,text=raw,json=lambda:body))
    monkeypatch.setattr(openai_client.requests,'post',post)
    result=pipeline.classify_activists(c,NOW)
    assert result['status']=='DEGRADED' and result['retryable']==expected_retry
    assert pipeline.rows(c,'classification_response')[0]['response_raw']==raw
    assert not pipeline.rows(c,'classification') and post.call_count==1


def test_no_work_is_distinct_from_a_pending_budget_block(monkeypatch):
    c=database();activist(c,first_seen_at=(NOW-timedelta(days=3)).isoformat())
    pipeline.state(c,'model-budget:'+NOW.date().isoformat(),20);call=api(monkeypatch)
    assert pipeline.classify_activists(c,NOW)['status']=='NO_ELIGIBLE_WORK'
    call.assert_not_called()
