from types import SimpleNamespace

import pytest
import requests
import watchdog
import calendar_evidence
from health_checks import HealthCheckError


@pytest.mark.parametrize('stage', ['broker.calendar', 'dashboard.health'])
def test_inspection_failure_identifies_read_without_exposing_request(monkeypatch, tmp_path, stage):
    monkeypatch.setattr(watchdog, 'STATE', tmp_path)
    monkeypatch.setattr(watchdog, 'load_dotenv', lambda *a: None)
    for key, value in {'NWT_ALPACA_BASE_URL': 'https://paper-api.alpaca.markets',
                       'NWT_ALPACA_KEY_ID': 'fake', 'NWT_ALPACA_SECRET_KEY': 'fake',
                       'NWT_DASHBOARD_TOKEN': 'fake'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(watchdog.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    retained = []
    def retain(state, receipt, result):
        retained.append((state, receipt, result))
        return 'synthetic-receipt'
    # This test isolates read-error identity from storage permissions. Actual
    # POSIX persistence is separately exercised with an owned private fixture.
    monkeypatch.setattr(calendar_evidence, 'retain_calendar', retain)

    def get(url, **kwargs):
        if stage == 'broker.calendar' or url.startswith('http://127.0.0.1:'):
            raise requests.ReadTimeout('private URL and token must not escape')
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: [] if '/calendar' in url else {'is_open': False})

    monkeypatch.setattr(watchdog.requests, 'get', get)
    with pytest.raises(HealthCheckError) as failure:
        watchdog.inspect()
    assert failure.value.operation == stage
    assert failure.value.error_type == 'ReadTimeout'
    assert 'private' not in str(failure.value)
    if stage == 'broker.calendar':
        assert len(retained) == 1
        assert retained[0][0] == tmp_path
        assert retained[0][1]['operation'] == 'broker.calendar'
        assert retained[0][1]['error_type'] == 'ReadTimeout'
        assert retained[0][1]['attempts'] == 1
        assert retained[0][2] == 'failed'
    else:
        assert retained == []


def test_inspection_persistence_failure_blocks_before_later_checks(monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog, 'STATE', tmp_path)
    monkeypatch.setattr(watchdog, 'load_dotenv', lambda *a: None)
    for key, value in {'NWT_ALPACA_BASE_URL': 'https://paper-api.alpaca.markets',
                       'NWT_ALPACA_KEY_ID': 'fake', 'NWT_ALPACA_SECRET_KEY': 'fake'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(watchdog.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        assert url.endswith('/v2/calendar')
        raise requests.ReadTimeout('private request detail')
    def fail_retention(*args):
        raise PermissionError('private storage detail')
    monkeypatch.setattr(watchdog.requests, 'get', get)
    monkeypatch.setattr(calendar_evidence, 'retain_calendar', fail_retention)
    with pytest.raises(HealthCheckError) as failure:
        watchdog.inspect()
    assert len(calls) == 1  # No clock/dashboard/positions or database inspection.
    assert failure.value.operation == 'calendar.evidence'
    assert failure.value.error_type == 'PersistenceFailed'
    assert failure.value.calendar_failure['operation'] == 'broker.calendar'
    assert failure.value.calendar_failure['error_type'] == 'ReadTimeout'
    assert failure.value.calendar_failure['attempts'] == 1
    assert str(failure.value) == 'calendar.evidence: PersistenceFailed'
    assert 'private' not in repr(failure.value.calendar_failure)
    assert failure.value.__suppress_context__
