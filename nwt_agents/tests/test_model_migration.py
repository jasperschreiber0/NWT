"""Migration contract tests use synthetic responses; no inference or broker calls."""
import json
import sys
from pathlib import Path
from unittest.mock import Mock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openai_client as client
import prescreener
import conviction_engine


def reply(text='[]', status='completed', **usage):
    return Mock(status_code=200, json=lambda: dict(id='synthetic',model='gpt-6-luna',status=status,
        usage=dict(input_tokens=1000,output_tokens=100,**usage),
        output=[dict(type='reasoning',summary=[]),dict(type='message',content=[dict(type='output_text',text=text)])]))


def test_luna_contract_and_attribution(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test-only')
    post=Mock(return_value=reply());monkeypatch.setattr(client.requests,'post',post)
    sink=Mock();data,ti,to=client.call_json('JSON fixture',client.DEFAULT_MODEL,list,usage_sink=sink)
    request=post.call_args.kwargs['json']
    assert request==dict(model='gpt-6-luna',input='JSON fixture',max_output_tokens=2048,store=False,
        reasoning={'effort':'none'},service_tier='default',prompt_cache_options={'mode':'explicit'})
    assert data==[] and (ti,to)==(1000,100)
    evidence=sink.call_args.args[0]
    assert evidence['model_epoch']==client.MODEL_EPOCH
    assert evidence['requested_model']==evidence['model']=='gpt-6-luna'
    assert evidence['tokens_used']['openai_cost_usd']==pytest.approx(.00015)
    assert 'JSON fixture' not in json.dumps(evidence)
    assert len(evidence['prompt_sha256'])==64


@pytest.mark.parametrize('ti,cache,writes,to,expected',[
    (1000,200,300,100,.0001395),
    (272000,0,0,2048,.028224),
    (272001,0,0,2048,.0559362),
    (1000,0,1000,100,.000175),
])
def test_luna_cache_write_and_long_context_rates(ti,cache,writes,to,expected):
    assert client.usage_cost('gpt-6-luna',dict(input_tokens=ti,output_tokens=to,
        input_tokens_details=dict(cached_tokens=cache,cache_write_tokens=writes)))==pytest.approx(expected)


@pytest.mark.parametrize('details',[dict(cached_tokens=1001),dict(cache_write_tokens=-1),dict(cached_tokens=600,cache_write_tokens=600)])
def test_invalid_usage_not_silently_underbilled(details):
    with pytest.raises(ValueError):client.usage_cost('gpt-6-luna',dict(input_tokens=1000,output_tokens=100,input_tokens_details=details))


@pytest.mark.parametrize('text,status,shape',[('','incomplete',list),('not JSON','completed',dict),('{}','completed',list),('[1]','completed',list)])
def test_luna_failure_still_records_usage(monkeypatch,text,status,shape):
    monkeypatch.setenv('OPENAI_API_KEY','test-only')
    monkeypatch.setattr(client.requests,'post',Mock(return_value=reply(text,status)))
    sink=Mock()
    with pytest.raises((ValueError,RuntimeError)):client.call_json('fixture','gpt-6-luna',shape,usage_sink=sink)
    assert sink.call_count==1


def test_refusal_is_rejected_and_recorded(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test-only')
    body=reply().json();body['output']=[dict(type='message',content=[dict(type='refusal',refusal='no')])]
    monkeypatch.setattr(client.requests,'post',Mock(return_value=Mock(status_code=200,json=lambda:body)))
    sink=Mock()
    with pytest.raises(RuntimeError,match='refused'):client.call_json('fixture','gpt-6-luna',dict,usage_sink=sink)
    assert sink.call_count==1


def test_unknown_model_fails_before_paid_call(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test-only');post=Mock();monkeypatch.setattr(client.requests,'post',post)
    with pytest.raises(ValueError):client.call_json('fixture','unverified',dict)
    post.assert_not_called()


def test_defaults_and_legacy_override_policy():
    assert prescreener.PRESCREENER_MODEL==conviction_engine.CONVICTION_MODEL=='gpt-6-luna'
    assert client.model_policy('gpt-4.1-2025-04-14')['model_epoch']=='legacy-gpt41'
    assert client.usage_cost('gpt-4.1-mini',dict(input_tokens=1000,output_tokens=100,input_tokens_details=dict(cached_tokens=200)))==pytest.approx(.0005)
