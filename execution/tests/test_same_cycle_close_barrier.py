"""A close submitted during this run must block conflicting new entries."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import MagicMock

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


@pytest.mark.parametrize('phase',['monitor','ticket'])
def test_close_evidence_error_blocks_new_entries_even_if_broker_order_finished(monkeypatch,phase):
    conn=Mock()
    monkeypatch.setattr(engine,'get_db',lambda:conn)
    monkeypatch.setattr(engine,'upsert_heartbeat',lambda _:None)
    monkeypatch.setitem(sys.modules,'fill_recovery',SimpleNamespace(
        recover_entries=lambda *a:dict(recorded=[],pending=[])))
    monkeypatch.setitem(sys.modules,'close_recovery',SimpleNamespace(
        recover_closes=lambda *a:dict(recorded=[],pending=[],blocked=[])))
    monkeypatch.setattr(engine,'check_no_trade_mode',lambda _:(False,''))
    monkeypatch.setattr(engine,'load_master_directives',lambda:{})
    monkeypatch.setattr(engine,'get_open_positions',lambda _:[])
    monkeypatch.setattr(engine,'alpaca_get',lambda path:{'is_open':True} if path=='/clock' else [])
    monkeypatch.setattr(engine,'log_system_event',Mock())
    fail=Mock(side_effect=engine.CloseReceiptError('missing broker timestamp'))
    monkeypatch.setattr(engine,'run_equity_position_monitor',fail if phase=='monitor' else Mock())
    monkeypatch.setattr(engine,'fetch_force_close_tickets',lambda _:[dict(type='FORCE_CLOSE',ticket_id='close')])
    monkeypatch.setattr(engine,'process_force_close',fail)
    entries=Mock();monkeypatch.setattr(engine,'fetch_pending_tickets',entries)
    with pytest.raises(engine.CloseReceiptError):engine.main()
    entries.assert_not_called();conn.close.assert_called_once()


@pytest.mark.parametrize('phase',['equity_monitor','close_request','force_close'])
@pytest.mark.parametrize('failure',['recent_pending','recent_partial','missing_id','timeout_audit_failure','success_audit_failure',
                                    'ledger_failure','pending_empty_orders','bound_storage_audit_failure'])
def test_real_close_handlers_block_main_on_unresolved_close(monkeypatch,phase,failure):
    """Real main/handlers/receipt verifier; mock broker, DB and scheduling inputs."""
    import close_recovery
    import fill_recovery
    from test_close_receipts import position, receipt
    pos=dict(position(),asset_type='equity' if phase=='equity_monitor' else 'option',status='open')
    conn=MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value=pos
    monkeypatch.setattr(engine,'get_db',lambda:conn)
    monkeypatch.setattr(engine,'upsert_heartbeat',Mock())
    monkeypatch.setattr(fill_recovery,'recover_entries',lambda *a:dict(recorded=[],pending=[]))
    monkeypatch.setattr(close_recovery,'recover_closes',lambda *a:dict(recorded=[],pending=[],blocked=[]))
    monkeypatch.setattr(engine,'check_no_trade_mode',lambda _:(False,''))
    monkeypatch.setattr(engine,'load_master_directives',lambda:{})
    monkeypatch.setattr(engine,'get_open_positions',lambda _:[])
    monkeypatch.setattr(engine,'get_ledger_position',lambda *a:pos)
    monkeypatch.setattr(engine,'get_latest_quote',lambda *a:(80,80))
    broker=Mock(side_effect=lambda path:{'is_open':True} if path=='/clock' else [])
    monkeypatch.setattr(engine,'alpaca_get',broker)
    submit=Mock(return_value={} if failure=='missing_id' else receipt())
    monkeypatch.setattr(engine,'equity_close_order',submit)
    monkeypatch.setattr(engine,'option_close_order',submit)
    poll=Mock(return_value=receipt(status='new',filled_qty='0') if failure=='pending_empty_orders' else receipt())
    if failure in ('recent_pending','recent_partial'):
        from datetime import datetime, timezone
        now=datetime.now(timezone.utc).isoformat()
        poll.return_value=receipt(status='new' if failure=='recent_pending' else 'partially_filled',
                                  filled_qty='0' if failure=='recent_pending' else '1',
                                  submitted_at=now,filled_at=None if failure=='recent_pending' else now)
    if failure=='timeout_audit_failure':poll.side_effect=requests.Timeout('synthetic timeout')
    monkeypatch.setattr(engine,'poll_order_until_filled',poll)
    audit=Mock(side_effect=RuntimeError('synthetic audit storage failure') if 'audit_failure' in failure else None)
    monkeypatch.setattr(engine,'log_system_event',audit)
    monkeypatch.setattr(engine,'insert_decision',Mock())
    if failure=='bound_storage_audit_failure':
        import close_bounds
        monkeypatch.setattr(close_bounds,'retain_close_bounds',Mock(side_effect=RuntimeError('bounds storage unavailable')))
    persist=Mock(side_effect=RuntimeError('synthetic ledger failure') if failure=='ledger_failure' else None)
    monkeypatch.setattr(engine,'close_position',persist)
    def monitor(_):
        if phase=='equity_monitor':
            engine._close_equity_position(conn,pos,80,pos['position_id'],'VGK',160,80,'target')
    monkeypatch.setattr(engine,'run_equity_position_monitor',monitor)
    ticket=dict(type='FORCE_CLOSE' if phase=='force_close' else 'CLOSE_REQUEST',ticket_id='close',
                payload=dict(position_id=pos['position_id'],asset_type=pos['asset_type']))
    monkeypatch.setattr(engine,'fetch_force_close_tickets',lambda _:[] if phase=='equity_monitor' else [ticket])
    entries=Mock(return_value=[dict(ticket_id='entry')]);process=Mock()
    monkeypatch.setattr(engine,'fetch_pending_tickets',entries)
    monkeypatch.setattr(engine,'process_ticket',process)
    if failure in ('recent_pending','recent_partial'):
        engine.main()
        audit.assert_not_called()
    else:
        with pytest.raises(engine.CloseReceiptError):engine.main()
    submit.assert_called_once()
    entries.assert_not_called();process.assert_not_called();conn.close.assert_called_once()
    assert persist.call_count==int(failure=='ledger_failure')
    if failure in ('missing_id','bound_storage_audit_failure'):poll.assert_not_called()


@pytest.mark.parametrize('case',['contradictory','omitted','consistent_early','consistent_valid'])
def test_every_activity_respects_initial_submission_in_real_main(monkeypatch,case):
    import close_recovery
    import fill_recovery
    from datetime import datetime, timezone
    from copy import deepcopy
    from test_close_receipts import position, receipt
    pos=dict(position(),asset_type='equity',status='open')
    initial=receipt(submitted_at='2026-10-23T13:30:00Z')
    polled=receipt(filled_at=None,submitted_at='2026-10-23T13:20:00Z' if case=='contradictory'
                   else '2026-10-23T13:30:00Z')
    if case=='omitted':
        polled.pop('submitted_at')
        polled['created_at']='2026-10-23T13:20:00Z'
    activity=dict(id='execution-1',order_id='close-order',symbol='VGK',side='sell',qty='1',price='80',
                  transaction_time='2026-10-23T13:30:00Z' if case=='consistent_valid' else '2026-10-23T13:29:59Z')
    activities=[activity,dict(activity,id='execution-2',transaction_time='2026-10-23T13:35:00Z')]
    originals=deepcopy((initial,polled,activities))
    conn=MagicMock()
    monkeypatch.setattr(engine,'get_db',lambda:conn)
    monkeypatch.setattr(engine,'upsert_heartbeat',Mock())
    monkeypatch.setattr(fill_recovery,'recover_entries',lambda *a:dict(recorded=[],pending=[]))
    monkeypatch.setattr(close_recovery,'recover_closes',lambda *a:dict(recorded=[],pending=[],blocked=[]))
    monkeypatch.setattr(engine,'check_no_trade_mode',lambda _:(False,''))
    monkeypatch.setattr(engine,'load_master_directives',lambda:{})
    monkeypatch.setattr(engine,'get_open_positions',lambda _:[])
    monkeypatch.setattr(engine,'get_ledger_position',lambda *a:pos)
    def broker(path):
        if path=='/clock':return dict(is_open=True)
        if '/activities/' in path:return activities
        assert path=='/orders?status=open&limit=1'
        return []
    monkeypatch.setattr(engine,'alpaca_get',broker)
    monkeypatch.setattr(engine,'equity_close_order',lambda _:initial)
    monkeypatch.setattr(engine,'poll_order_until_filled',lambda _:polled)
    monkeypatch.setattr(engine,'log_system_event',Mock())
    persist=Mock();monkeypatch.setattr(engine,'close_position',persist)
    monkeypatch.setattr(engine,'run_equity_position_monitor',lambda _:
        engine._close_equity_position(conn,pos,80,pos['position_id'],'VGK',160,80,'target'))
    monkeypatch.setattr(engine,'fetch_force_close_tickets',lambda _:[])
    entries=Mock(return_value=[dict(ticket_id='new-entry')]);process=Mock()
    monkeypatch.setattr(engine,'fetch_pending_tickets',entries)
    monkeypatch.setattr(engine,'process_ticket',process)
    if case=='consistent_valid':
        engine.main()
        persist.assert_called_once();entries.assert_called_once();process.assert_called_once()
        assert persist.call_args.kwargs['exit_time']==datetime(2026,10,23,13,35,tzinfo=timezone.utc)
    else:
        with pytest.raises(engine.CloseReceiptError):engine.main()
        persist.assert_not_called();entries.assert_not_called();process.assert_not_called()
    assert (initial,polled,activities)==originals
    conn.close.assert_called_once()
