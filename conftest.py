"""Regression suites must never contact a broker or external service."""
import sys
from pathlib import Path

import pytest
import requests

# These services use sibling imports when launched as scripts. Make their
# modules available before collection, including focused execution tests that
# patch strategy_review or load research/collect.py by filename. Do not depend
# on another test module importing paper_bridge first and modifying sys.path.
ROOT = Path(__file__).resolve().parent
for directory in ("ops", "research"):
    sys.path.insert(0, str(ROOT / directory))


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Real network request blocked in test; provide an explicit mock')
    monkeypatch.setattr(requests.sessions.Session, 'send', blocked)
