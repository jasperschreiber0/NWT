"""Offline persistence-path supplement; no database or provider connection."""
import json
from unittest.mock import Mock
import pytest
import openai_client as client
import shared_context


@pytest.mark.parametrize('audit_fails', [False, True])
def test_real_warning_logging_serializes_only_safe_subtypes(monkeypatch, audit_fails):
    secret = 'SYNTHETIC_PRIVATE_MARKER_DO_NOT_RETAIN'
    monkeypatch.setenv('OPENAI_API_KEY', secret)
    response = Mock(status_code=429, headers={'x-private': secret})
    response.json.return_value = {'error': {
        'code': 'project_spend_limit_exceeded', 'type': 'insufficient_quota',
        'message': secret, 'project': secret}}
    post = Mock(return_value=response)
    monkeypatch.setattr(client.requests, 'post', post)
    conn = Mock()
    cursor = Mock()
    conn.cursor.return_value.__enter__ = Mock(return_value=cursor)
    conn.cursor.return_value.__exit__ = Mock(return_value=False)
    if audit_fails:
        cursor.execute.side_effect = RuntimeError(secret)
    # Use actual client.error_event -> shared_context.log_system_event -> SQL serialization.
    assert shared_context.log_system_event.__module__ == 'shared_context'
    with pytest.raises(client.OpenAIRequestError) as caught:
        client.call_json('synthetic prompt', 'gpt-4.1-mini', list,
                         conn=conn, retry_rate_limit=True)
    sql, params = cursor.execute.call_args.args
    assert 'INSERT INTO nwt_system_log' in sql
    assert params[:3] == ('WARNING', 'openai_client', 'OpenAI request failure')
    payload = json.loads(params[3])
    assert payload['quota_code'] == 'project_spend_limit_exceeded'
    assert payload['quota_type'] == 'insufficient_quota'
    assert payload['category'] == 'quota_exhausted'
    assert payload['attempts'] == 1 and payload['retry_scheduled'] is False
    assert secret not in repr(params) + str(caught.value)
    assert 'quota_code' not in str(caught.value)  # Terminal message intentionally generic.
    assert post.call_count == 1
    if audit_fails:
        conn.commit.assert_not_called()  # No retention claim when audit persistence fails.
    else:
        conn.commit.assert_called_once_with()
