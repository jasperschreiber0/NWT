from unittest.mock import Mock
import pytest
import openai_client as client

@pytest.mark.parametrize('field',['code','type'])
@pytest.mark.parametrize('value',sorted(client.QUOTA_CODES))
def test_exact_quota_subtype_retained_without_retry(monkeypatch,field,value):
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-only')
    body={'error':{field:value,'message':'PRIVATE SYNTHETIC CONTENT'}}
    response=Mock(status_code=429,headers={},json=lambda:body)
    post=Mock(return_value=response);audit=Mock();sleep=Mock()
    monkeypatch.setattr(client.requests,'post',post)
    monkeypatch.setattr(client,'error_event',audit)
    monkeypatch.setattr(client.time,'sleep',sleep)
    with pytest.raises(client.OpenAIRequestError) as caught:
        client.call_json('synthetic','gpt-4.1-mini',list,conn=object(),retry_rate_limit=True)
    assert caught.value.details['quota_'+field]==value
    assert audit.call_args.args[-1]['quota_'+field]==value
    assert audit.call_args.args[-1]['retry_scheduled'] is False
    assert post.call_count==1
    sleep.assert_not_called()
    assert 'PRIVATE' not in repr(caught.value.details)+str(caught.value)+repr(audit.call_args)

def test_conflicting_recognized_fields_preserved_separately():
    response=Mock(status_code=429,json=lambda:{'error':{'code':'project_spend_limit_exceeded','type':'insufficient_quota'}})
    assert client.error_metadata(response)==dict(category='quota_exhausted',quota_code='project_spend_limit_exceeded',quota_type='insufficient_quota')

@pytest.mark.parametrize('value',['sk-private','insufficient_quota PRIVATE',[],{},None,99])
def test_untrusted_code_is_not_persisted_even_with_known_type(value):
    response=Mock(status_code=429,json=lambda:{'error':{'code':value,'type':'insufficient_quota'}})
    result=client.error_metadata(response)
    error=client.OpenAIRequestError(429,result['category'],1,result['quota_code'],result['quota_type'])
    assert error.details==dict(http_status=429,category='quota_exhausted',attempts=1,quota_type='insufficient_quota')

def test_constructor_rechecks_allowlist():
    error=client.OpenAIRequestError(429,'quota_exhausted',1,'PRIVATE',['PRIVATE'])
    assert set(error.details)=={'http_status','category','attempts'}

def test_body_parsed_once(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-only')
    response=Mock(status_code=429,headers={})
    response.json.side_effect=[{'error':{'code':'insufficient_quota'}},AssertionError('second parse')]
    monkeypatch.setattr(client.requests,'post',Mock(return_value=response))
    with pytest.raises(client.OpenAIRequestError):client.call_json('synthetic','gpt-4.1-mini',list)
    assert response.json.call_count==1
