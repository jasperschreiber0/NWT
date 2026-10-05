from types import SimpleNamespace

import pytest
import requests
import watchdog
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

    def get(url, **kwargs):
        if stage == 'broker.calendar' or url.startswith('http://127.0.0.1:'):
            raise requests.ReadTimeout('private URL and token must not escape')
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: [] if 'calendar?' in url else {'is_open': False})

    monkeypatch.setattr(watchdog.requests, 'get', get)
    with pytest.raises(HealthCheckError) as failure:
        watchdog.inspect()
    assert failure.value.operation == stage
    assert failure.value.error_type == 'ReadTimeout'
    assert 'private' not in str(failure.value)
