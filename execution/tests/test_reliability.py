from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
import pytest
import requests
import engine
from reliability import entry_rejection, intent_key, reconcile_quantities, separate_legacy_expiries

NOW = datetime(2026,9,14,14,tzinfo=timezone.utc)


def test_closed_market_defers_all_orders_without_consuming_tickets(monkeypatch):
    conn=MagicMock()
    monkeypatch.setattr(engine,'get_db',lambda:conn)
    heartbeat=MagicMock();monkeypatch.setattr(engine,'upsert_heartbeat',heartbeat)
    monkeypatch.setattr(engine,'alpaca_get',lambda path:{'is_open':False})
    for name in ['run_equity_position_monitor','fetch_force_close_tickets','fetch_pending_tickets']:
        monkeypatch.setattr(engine,name,MagicMock(side_effect=AssertionError('must remain deferred')))
    engine.main()
    heartbeat.assert_called_once_with(conn)
    conn.close.assert_called_once()


def ticket(age=0, sender='NWT_EXECUTION_AGENT'):
    return {'ticket_id':'11111111-1111-1111-1111-111111111111', 'from_agent':sender,
            'created_at':NOW-timedelta(seconds=age), 'payload':dict(strategy_id='C1',symbol='QQQ',direction='short')}


def test_friday_ticket_cannot_trade_monday():
    assert entry_rejection(ticket(3*86400,'AUS_EXECUTOR'),NOW)=='ENTRY_SESSION_EXPIRED'


def test_session_age_and_missing_timestamp():
    assert entry_rejection(ticket(1801),NOW)=='ENTRY_AGE_EXPIRED'
    assert entry_rejection(ticket(4*3600,'AUS_EXECUTOR'),NOW) is None
    assert entry_rejection({'payload':{}},NOW)=='ENTRY_TIMESTAMP_MISSING'
    assert entry_rejection(ticket(-300),NOW)=='ENTRY_SESSION_EXPIRED'


def test_two_requests_same_intent_cannot_create_two_identities():
    a=ticket(); b=ticket(); b['ticket_id']='22222222-2222-2222-2222-222222222222'
    assert intent_key(a,NOW)==intent_key(b,NOW)
    b['payload']['direction']='long'
    assert intent_key(a,NOW)!=intent_key(b,NOW)


def test_signed_aggregate_reconciliation_detects_equity_errors():
    rows=[dict(asset='EWA',qty=24,direction='long'),dict(asset='EWA',qty=24,direction='long')]
    assert not reconcile_quantities([dict(symbol='EWA',qty='48')],rows)
    assert reconcile_quantities([dict(symbol='EWA',qty='24')],rows)
    assert reconcile_quantities([dict(symbol='EWA',qty='-48')],rows)


def test_only_absent_pretrial_expired_suspects_are_historical():
    old=dict(asset='AAPL260717C00312500',asset_type='option',status='suspect')
    current=dict(asset='QQQ260921P00706000',asset_type='option',status='suspect')
    assert separate_legacy_expiries([], [old,current], '2026-09-15')==([current],[old])
    assert separate_legacy_expiries([dict(symbol=old['asset'])],[old],'2026-09-15')==([old],[])


@pytest.mark.parametrize('status',['new','partially_filled','filled','canceled'])
def test_entry_uses_existing_order_for_every_status(monkeypatch,status):
    prior=dict(id='original',status=status)
    monkeypatch.setattr(engine,'alpaca_get',lambda path:prior)
    post=MagicMock();monkeypatch.setattr(engine,'alpaca_post',post)
    assert engine.submit_identified_order(dict(client_order_id='nwt-entry-test'))==prior
    post.assert_not_called()


def test_uncertain_lookup_never_submits(monkeypatch):
    monkeypatch.setattr(engine,'alpaca_get',MagicMock(side_effect=requests.Timeout()))
    post=MagicMock();monkeypatch.setattr(engine,'alpaca_post',post)
    with pytest.raises(requests.Timeout):engine.submit_identified_order(dict(client_order_id='nwt-entry-test'))
    post.assert_not_called()


def test_option_close_uses_previous_identity(monkeypatch):
    order=dict(id='original',status='partially_filled')
    monkeypatch.setattr(engine,'alpaca_get',lambda _:order)
    post=MagicMock();monkeypatch.setattr(engine,'alpaca_post',post)
    pos=dict(status='open',asset_type='option',qty=3,position_id='id',asset='QQQ260921P00706000',direction='long')
    assert engine.option_close_order(pos)==order
    post.assert_not_called()
