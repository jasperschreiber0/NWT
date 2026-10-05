import pytest
import requests
import health_checks as health


def test_timeout_context_preserves_operation_and_duration_without_secrets(monkeypatch):
    ticks = iter([10.0, 25.125])
    monkeypatch.setattr(health.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(health.HealthCheckError) as failure:
        with health.health_operation('broker.positions'):
            raise requests.ReadTimeout('https://secret-host/?token=DO_NOT_LOG')
    assert failure.value.details() == dict(operation='broker.positions',
        error_type='ReadTimeout', elapsed_seconds=15.125)
    assert str(failure.value) == 'broker.positions: ReadTimeout'
    assert 'DO_NOT_LOG' not in str(failure.value)
    assert failure.value.__suppress_context__


def test_successful_read_is_unchanged():
    with health.health_operation('dashboard.health'):
        result = {'status': 'ok'}
    assert result == {'status': 'ok'}


def test_invalid_response_retains_failure_type_without_body():
    with pytest.raises(health.HealthCheckError, match='broker.clock: ValueError'):
        with health.health_operation('broker.clock'):
            raise ValueError('private response body')


def test_duration_does_not_change_notification_identity():
    assert str(health.HealthCheckError('broker.clock', 'ReadTimeout', 15)) == str(
        health.HealthCheckError('broker.clock', 'ReadTimeout', 16))
