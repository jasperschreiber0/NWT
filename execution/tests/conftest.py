"""
execution/engine.py reads its Alpaca/DB config from os.environ at import
time (fails loudly if missing, by design — see CLAUDE.md's Startup
Integrity Gate). Tests never talk to a real broker or DB (everything that
touches either is mocked), so this just supplies harmless dummy values so
the module can be imported at all.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("ALPACA_DATA_URL", "https://data.alpaca.markets")
os.environ.setdefault("NWT_DB_DSN", "postgresql://test:test@localhost/test")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import pytest


@pytest.fixture(autouse=True)
def synthetic_bound_store_for_engine_unit_tests(request, monkeypatch):
    """These four modules mock the DB; real persistence has separate tests."""
    if request.module.__name__ not in {
        'test_close_receipts', 'test_same_cycle_close_barrier',
        'test_engine_equity_close_side', 'test_pending_close',
    }:
        return
    import close_bounds
    rows = {}
    def retain(conn, position, order, observed_at):
        from close_recovery import validate_close_identity
        validate_close_identity(position, order)
        key = (position['position_id'], position.get('close_generation', 0))
        oid, prior = rows.get(key, (order['id'], {}))
        if oid != order['id']:
            raise ValueError('Retained close order identity changed')
        merged = close_bounds.merge_bounds(prior, order, observed_at)
        rows[key] = (oid, merged)
        return close_bounds.bound_sources(oid, merged)
    monkeypatch.setattr(close_bounds, 'retain_close_bounds', retain)
