"""Regression suites must never contact a broker or external service."""
import pytest
import requests


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Real network request blocked in test; provide an explicit mock')
    monkeypatch.setattr(requests.sessions.Session, 'send', blocked)
