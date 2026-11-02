"""Synthetic provider failures only; no requests or billed inference."""
import json
import sys
from pathlib import Path
from unittest.mock import Mock
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import openai_client as client
import prescreener
from test_openai_provider import response

SECRET='secret-key prompt account-id private-provider-message'


def failure(code=None,kind=None,status=429,headers=None):
    return Mock(status_code=status,headers=headers or {},json=lambda:dict(error=dict(code=code,type=kind,message=SECRET)))


@pytest.fixture(autouse=True)
def synthetic(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-key')
    monkeypatch.setattr(client.time,'sleep',Mock())
    monkeypatch.setattr(client.random,'random',lambda:.5)


@pytest.mark.parametrize('code',sorted(client.QUOTA_CODES))
def test_quota_never_retries_or_logs_provider_content(monkeypatch,code):
    post=Mock(return_value=failure(code,kind='rate_limit_exceeded'))
    monkeypatch.setattr(client.requests,'post',post)
    audit=Mock();monkeypatch.setattr('shared_context.log_system_event',audit)
    with pytest.raises(client.OpenAIRequestError,match='quota_exhausted') as exc:
        prescreener.call_prescreener(SECRET,conn=object())
    assert post.call_count==1
    client.time.sleep.assert_not_called()
    assert SECRET not in str(exc.value) and SECRET not in repr(audit.call_args_list)


@pytest.mark.parametrize('body',[[],None,{}, {'error':[]},{'error':{'code':'unknown','type':'rate_limit_exceeded'}}, {'error':{'code':['rate_limit_exceeded']}}])
def test_ambiguous_429_never_retries(monkeypatch,body):
    bad=failure();bad.json=lambda:body
    post=Mock(return_value=bad);monkeypatch.setattr(client.requests,'post',post)
    with pytest.raises(client.OpenAIRequestError,match='unknown_429'):prescreener.call_prescreener(SECRET)
    assert post.call_count==1


def test_invalid_json_and_transport_are_redacted_and_not_retried(monkeypatch):
    bad=failure();bad.json=Mock(side_effect=ValueError(SECRET))
    post=Mock(return_value=bad);monkeypatch.setattr(client.requests,'post',post)
    with pytest.raises(client.OpenAIRequestError,match='unknown_429'):prescreener.call_prescreener(SECRET)
    post.side_effect=client.requests.Timeout(SECRET);post.reset_mock()
    with pytest.raises(client.OpenAIRequestError,match='transport_failure') as exc:prescreener.call_prescreener(SECRET)
    assert post.call_count==1 and SECRET not in str(exc.value)
    assert exc.value.__suppress_context__


@pytest.mark.parametrize('code',['rate_limit_exceeded','slow_down'])
def test_temporary_limit_recovers_once_without_changing_success_or_accounting(monkeypatch,code):
    post=Mock(side_effect=[failure(code,headers={'Retry-After':'3'}),response()])
    monkeypatch.setattr(client.requests,'post',post)
    audit=Mock();monkeypatch.setattr('shared_context.log_system_event',audit)
    result=prescreener.call_prescreener(SECRET,conn=object())
    assert result[1:]==(1000,100) and result[0][0]['symbol']=='SPY'
    assert post.call_count==2
    assert post.call_args_list[0]==post.call_args_list[1]
    client.time.sleep.assert_called_once_with(3)
    usage=[c for c in audit.call_args_list if c.args[3]=='OpenAI API usage']
    assert len(usage)==1
    assert SECRET not in repr(audit.call_args_list)


def test_attempt_limit_and_other_callers_remain_single_attempt(monkeypatch):
    post=Mock(return_value=failure('rate_limit_exceeded'));monkeypatch.setattr(client.requests,'post',post)
    with pytest.raises(client.OpenAIRequestError,match='attempts=2'):prescreener.call_prescreener('synthetic')
    assert post.call_count==2;client.time.sleep.assert_called_once_with(1.5)
    post.reset_mock();client.time.sleep.reset_mock()
    with pytest.raises(client.OpenAIRequestError):client.call_json('synthetic','gpt-4.1-mini',list)
    assert post.call_count==1;client.time.sleep.assert_not_called()


@pytest.mark.parametrize('value',['11','NaN','inf','-1','bad','x'*129])
def test_invalid_or_excessive_retry_after_does_not_retry_early(monkeypatch,value):
    post=Mock(return_value=failure('rate_limit_exceeded',headers={'Retry-After':value}));monkeypatch.setattr(client.requests,'post',post)
    with pytest.raises(client.OpenAIRequestError):prescreener.call_prescreener('synthetic')
    assert post.call_count==1;client.time.sleep.assert_not_called()


def test_http_date_and_explicit_no_retry(monkeypatch):
    monkeypatch.setattr(client.time,'time',lambda:0)
    assert client.retry_delay({'Retry-After':'Thu, 01 Jan 1970 00:00:04 GMT'})==4
    assert client.retry_delay({'Retry-After':'Thu, 01 Jan 1970 00:00:40 GMT'}) is None
    assert client.retry_delay({'x-should-retry':'false'}) is None


@pytest.mark.parametrize('code',['insufficient_quota','rate_limit_exceeded',None])
def test_failure_clears_candidates_and_logs_only_safe_reason(monkeypatch,tmp_path,caplog,code):
    target=tmp_path/'prescreened_symbols.json';target.write_text('[{"symbol":"STALE"}]')
    monkeypatch.setattr(prescreener,'AGENTS_DIR',tmp_path)
    monkeypatch.setattr(prescreener,'get_db',lambda:Mock())
    monkeypatch.setattr(prescreener,'load_layer0_data',lambda:{'symbols':{'SPY':{}}})
    monkeypatch.setattr(prescreener,'load_master_directives',lambda:{})
    monkeypatch.setattr(prescreener,'apply_hard_filters',lambda *a:(['SPY'],[]))
    monkeypatch.setattr(prescreener,'build_prescreener_prompt',lambda *a:SECRET)
    monkeypatch.setattr(client.requests,'post',Mock(return_value=failure(code)))
    audit=Mock();monkeypatch.setattr(prescreener,'log_system_event',audit)
    monkeypatch.setattr('shared_context.log_system_event',Mock(side_effect=RuntimeError(SECRET)))
    with pytest.raises(SystemExit):prescreener.main()
    assert json.loads(target.read_text())==[]
    assert SECRET not in caplog.text and SECRET not in repr(audit.call_args_list)


def test_prescreener_final_audit_failure_cannot_replace_sanitized_exit(monkeypatch,tmp_path,caplog):
    import traceback
    target=tmp_path/'prescreened_symbols.json';target.write_text('[{"symbol":"STALE"}]')
    conn=Mock()
    monkeypatch.setattr(prescreener,'AGENTS_DIR',tmp_path)
    monkeypatch.setattr(prescreener,'get_db',lambda:conn)
    monkeypatch.setattr(prescreener,'load_layer0_data',lambda:{'symbols':{'SPY':{}}})
    monkeypatch.setattr(prescreener,'load_master_directives',lambda:{})
    monkeypatch.setattr(prescreener,'apply_hard_filters',lambda *a:(['SPY'],[]))
    monkeypatch.setattr(prescreener,'build_prescreener_prompt',lambda *a:SECRET)
    post=Mock(return_value=failure('insufficient_quota'))
    monkeypatch.setattr(client.requests,'post',post)
    monkeypatch.setattr('shared_context.log_system_event',Mock())
    audit=Mock(side_effect=RuntimeError(SECRET))
    monkeypatch.setattr(prescreener,'log_system_event',audit)
    with pytest.raises(SystemExit) as caught:prescreener.main()
    assert caught.value.code==1 and caught.value.__suppress_context__
    assert SECRET not in ''.join(traceback.format_exception(caught.value))
    assert SECRET not in caplog.text
    assert 'quota_exhausted' in caplog.text and 'failure audit unavailable' in caplog.text
    assert json.loads(target.read_text())==[]
    assert post.call_count==1
    audit.assert_called_once();conn.close.assert_called_once()
