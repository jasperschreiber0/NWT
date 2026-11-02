"""Synthetic broker receipts only; no orders or external calls."""
from datetime import datetime, timezone
from unittest.mock import Mock, MagicMock

import pytest
import requests
import engine
from close_recovery import close_client_id, verified_close_time
from fill_evidence import with_fill_timestamp


def position():
    return dict(position_id='11111111-1111-1111-1111-111111111111',
                asset='VGK', direction='long', qty=2,
                entry_time=datetime(2026, 10, 1, tzinfo=timezone.utc))


def receipt(**changes):
    return dict(dict(id='close-order', client_order_id=close_client_id(position()),
                     symbol='VGK', side='sell', qty='2', filled_qty='2',
                     filled_avg_price='80', status='filled',
                     filled_at='2026-10-23T13:35:00Z'), **changes)


@pytest.mark.parametrize('changes', [dict(filled_at=None), dict(filled_at='2026-10-23T13:35:00'),
    dict(filled_at='2026-09-01T00:00:00Z'), dict(filled_qty='1'), dict(filled_avg_price='NaN'),
    dict(client_order_id='wrong'), dict(symbol='EWA'), dict(side='buy'), dict(qty='3')])
def test_incomplete_or_mismatched_receipt_cannot_verify(changes):
    with pytest.raises((ValueError, TypeError, AttributeError)):
        verified_close_time(position(), receipt(**changes))


def test_poll_timeout_pending_then_recovery_retains_error_receipts(monkeypatch):
    saved=[]
    monkeypatch.setattr(engine, 'log_system_event', lambda c,level,component,msg,payload:
                        saved.append((level, dict(payload))))
    poll=Mock(side_effect=[requests.Timeout('synthetic'), receipt(status='new',filled_qty='0'), receipt()])
    monkeypatch.setattr(engine,'poll_order_until_filled',poll)
    with pytest.raises(engine.CloseReceiptError):engine.poll_close_receipt(None,position(),receipt())
    with pytest.raises(engine.CloseReceiptError):engine.poll_close_receipt(None,position(),receipt())
    result=engine.poll_close_receipt(None,position(),receipt())
    assert result['filled_at']=='2026-10-23T13:35:00Z'
    assert [x[0] for x in saved]==['ERROR','ERROR','INFO']
    assert [x[1]['outcome'] for x in saved]==[
        'poll_or_evidence_error','poll_or_evidence_error','verified_fill']
    assert all(x[1]['order_id']=='close-order' for x in saved)


def test_poll_window_is_bounded_and_pending_is_not_filled(monkeypatch):
    get=Mock(return_value=receipt(status='new',filled_qty='0'))
    monkeypatch.setattr(engine,'alpaca_get',get)
    monkeypatch.setattr(engine,'POLL_MAX',2)
    monkeypatch.setattr(engine.time,'sleep',lambda _:None)
    assert engine.poll_order_until_filled('close-order')['status']=='new'
    assert get.call_count==3


def test_fill_replay_deduplicates_but_conflicts_fail():
    row=dict(id='execution-1',order_id='close-order',symbol='VGK',side='sell',
             qty='2',price='80',transaction_time='2026-10-23T13:35:00Z')
    order=receipt(filled_at=None,submitted_at='2026-10-23T13:30:00Z')
    assert with_fill_timestamp(order,lambda _: [row,row])['filled_at']=='2026-10-23T13:35:00+00:00'
    with pytest.raises(ValueError,match='Conflicting duplicate'):
        with_fill_timestamp(order,lambda _: [row,dict(row,price='81')])
    for field,value in [('qty','-2'),('price','NaN'),('id','')]:
        with pytest.raises(ValueError):with_fill_timestamp(order,lambda _: [dict(row,**{field:value})])


def test_polled_order_identity_and_missing_evidence_fail_closed(monkeypatch):
    log=Mock();monkeypatch.setattr(engine,'log_system_event',log)
    for bad in [receipt(id='different'),receipt(filled_at=None)]:
        monkeypatch.setattr(engine,'poll_order_until_filled',lambda _,value=bad:value)
        with pytest.raises(ValueError):engine.poll_close_receipt(None,position(),receipt())
    assert all(c.args[1]=='ERROR' for c in log.call_args_list)


@pytest.mark.parametrize('handler',[engine.process_close_ticket,engine.process_force_close])
def test_ticket_close_writers_use_verified_broker_time(monkeypatch,handler):
    pos=dict(position(),asset_type='option',status='open')
    conn=MagicMock();conn.cursor.return_value.__enter__.return_value.fetchone.return_value=pos
    monkeypatch.setattr(engine,'get_ledger_position',lambda *a:pos)
    monkeypatch.setattr(engine,'option_close_order',lambda *a:receipt())
    monkeypatch.setattr(engine,'get_latest_quote',lambda *a:(80,80))
    monkeypatch.setattr(engine,'poll_order_until_filled',lambda *a:receipt())
    monkeypatch.setattr(engine,'insert_decision',Mock())
    monkeypatch.setattr(engine,'log_system_event',Mock())
    close=Mock();monkeypatch.setattr(engine,'close_position',close)
    handler(conn,dict(ticket_id='ticket',payload=dict(position_id=pos['position_id'],asset_type='option')))
    close.assert_called_once()
    assert close.call_args.kwargs['exit_time']==datetime(2026,10,23,13,35,tzinfo=timezone.utc)


@pytest.mark.parametrize('activity',[False,True])
@pytest.mark.parametrize('stamp', ['2099-01-01T00:00:00Z','2026-10-23T13:29:59Z',
                                  '2026-10-23T13:35:06Z'])
def test_future_and_presubmission_evidence_rejected(activity,stamp):
    observed=datetime(2026,10,23,13,35,tzinfo=timezone.utc)
    order=receipt(submitted_at='2026-10-23T13:30:00Z',filled_at=None if activity else stamp)
    row=dict(id='execution-1',order_id='close-order',symbol='VGK',side='sell',
             qty='2',price='80',transaction_time=stamp)
    with pytest.raises(ValueError):
        with_fill_timestamp(order,lambda _:[row],observed_at=observed)
    if not activity:
        with pytest.raises(ValueError):verified_close_time(position(),order,observed_at=observed)


def test_clock_skew_boundary_is_bounded_and_timestamp_never_rewritten():
    observed=datetime(2026,10,23,13,35,tzinfo=timezone.utc)
    order=receipt(filled_at='2026-10-23T13:35:05Z',submitted_at='2026-10-23T13:30:00Z')
    assert with_fill_timestamp(order,Mock(),observed_at=observed) is order
    assert verified_close_time(position(),order,observed_at=observed).second==5


def test_submission_response_time_is_not_lost_when_poll_omits_it(monkeypatch):
    monkeypatch.setattr(engine,'poll_order_until_filled',lambda _:receipt())
    monkeypatch.setattr(engine,'log_system_event',Mock())
    submitted=receipt(submitted_at='2026-10-23T13:36:00Z')
    with pytest.raises(engine.CloseReceiptError):engine.poll_close_receipt(None,position(),submitted)


def test_every_activity_time_is_validated_not_only_latest():
    order=receipt(filled_at=None,submitted_at='2026-10-23T13:30:00Z')
    row=dict(id='a',order_id='close-order',symbol='VGK',side='sell',qty='1',price='80',
             transaction_time='2026-10-23T13:29:59Z')
    with pytest.raises(ValueError):
        with_fill_timestamp(order,lambda _:[row,dict(row,id='b',transaction_time='2026-10-23T13:35:00Z')],
                            observed_at=datetime(2026,10,23,13,35,tzinfo=timezone.utc))


@pytest.mark.parametrize('activity',[False,True])
@pytest.mark.parametrize('stamp',['2099-01-01T00:00:00Z','2026-10-23T13:29:59Z'])
def test_delayed_recovery_rejects_time_evidence_before_ledger_mutation(activity,stamp):
    import close_recovery
    conn=MagicMock()
    cursor=conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value=[dict(position(),status='open')]
    cursor.fetchone.side_effect=[dict(position(),status='open'),None,None]
    order=receipt(submitted_at='2026-10-23T13:30:00Z',filled_at=None if activity else stamp)
    row=dict(id='execution-1',order_id='close-order',symbol='VGK',side='sell',
             qty='2',price='80',transaction_time=stamp)
    with pytest.raises(ValueError):
        close_recovery.recover_closes(conn,lambda path:[row] if '/activities/' in path else order)
    conn.commit.assert_not_called()
    assert all(not call.args[0].startswith(('INSERT','UPDATE','DELETE')) for call in cursor.execute.call_args_list)
