from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import pytest
import engine
from close_recovery import pending_close_receipt, close_client_id

NOW=datetime(2026,10,27,14,tzinfo=timezone.utc)
POS=dict(position_id='11111111-1111-1111-1111-111111111111',asset='EWA',direction='long',qty=10,status='open')

def receipt(**changes):
    return dict(dict(id='order',client_order_id=close_client_id(POS),symbol='EWA',side='sell',qty='10',filled_qty='0',status='new',submitted_at=NOW.isoformat()),**changes)

def test_recent_pending_and_partial_are_progress():
    assert pending_close_receipt(POS,receipt(),NOW+timedelta(minutes=4))
    assert pending_close_receipt(POS,receipt(status='partially_filled',filled_qty='2',filled_avg_price='25'),NOW)

@pytest.mark.parametrize('changes',[dict(side='buy'),dict(qty='11'),dict(filled_qty='NaN'),dict(filled_qty='11'),dict(submitted_at='bad'),dict(submitted_at=(NOW-timedelta(minutes=16)).isoformat()),dict(status='partially_filled',filled_qty='2',filled_avg_price='0')])
def test_pending_does_not_hide_invalid_or_stuck_orders(changes):
    with pytest.raises((ValueError,ArithmeticError)):pending_close_receipt(POS,receipt(**changes),NOW)

def test_terminal_receipt_is_not_pending():
    for status in ['rejected','canceled','expired','filled','unknown']:
        assert not pending_close_receipt(POS,receipt(status=status),NOW)

def test_engine_pending_close_is_not_failed_or_recorded_as_fill(monkeypatch):
    order=receipt(submitted_at=datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(engine,'get_ledger_position',lambda *a:POS)
    monkeypatch.setattr(engine,'equity_close_order',lambda *a:order)
    monkeypatch.setattr(engine,'poll_order_until_filled',lambda *a:order)
    close=Mock();event=Mock();logger=Mock()
    monkeypatch.setattr(engine,'close_position',close)
    monkeypatch.setattr(engine,'log_system_event',event)
    monkeypatch.setattr(engine,'logger',logger)
    engine._close_equity_position(None,POS,25,POS['position_id'],'EWA',250,25,'max_hold')
    close.assert_not_called();event.assert_not_called();logger.error.assert_not_called()
