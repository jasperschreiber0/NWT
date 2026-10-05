"""A close submitted during this run must block conflicting new entries."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
import engine


@pytest.mark.parametrize('orders,allowed', [([], True), ([{'id': 'close', 'status': 'new'}], False),
                                         (None, False), ({}, False),
                                         (requests.Timeout('offline simulated timeout'), False)])
def test_new_entries_wait_for_broker_orders_after_close_processing(monkeypatch, orders, allowed):
    conn = Mock()
    monkeypatch.setattr(engine, 'get_db', lambda: conn)
    monkeypatch.setattr(engine, 'upsert_heartbeat', lambda _: None)
    monkeypatch.setitem(sys.modules, 'fill_recovery', SimpleNamespace(
        recover_entries=lambda *a: {'recorded': [], 'pending': []}))
    monkeypatch.setitem(sys.modules, 'close_recovery', SimpleNamespace(
        recover_closes=lambda *a: {'recorded': [], 'pending': [], 'blocked': []}))
    monkeypatch.setattr(engine, 'check_no_trade_mode', lambda _: (False, ''))
    monkeypatch.setattr(engine, 'load_master_directives', lambda: {})
    monkeypatch.setattr(engine, 'get_open_positions', lambda _: [])
    events = []
    monkeypatch.setattr(engine, 'run_equity_position_monitor', lambda _: events.append('close_phase'))
    monkeypatch.setattr(engine, 'fetch_force_close_tickets', lambda _: [])
    fetch = Mock(return_value=[{'ticket_id': 'entry'}])
    process = Mock()
    monkeypatch.setattr(engine, 'fetch_pending_tickets', fetch)
    monkeypatch.setattr(engine, 'process_ticket', process)

    def broker(path):
        if path == '/clock': return {'is_open': True}
        assert path == '/orders?status=open&limit=1'
        assert events == ['close_phase']
        events.append('fresh_broker_check')
        if isinstance(orders, Exception): raise orders
        return orders

    monkeypatch.setattr(engine, 'alpaca_get', broker)
    if isinstance(orders, requests.Timeout):
        with pytest.raises(requests.Timeout, match='offline simulated timeout'):
            engine.main()
    elif not isinstance(orders, list):
        with pytest.raises(ValueError, match='Invalid broker open-order response'):
            engine.main()
    else:
        engine.main()
    assert events == ['close_phase', 'fresh_broker_check']
    assert process.call_count == int(allowed)
    assert fetch.call_count == int(allowed)
    conn.close.assert_called_once()
