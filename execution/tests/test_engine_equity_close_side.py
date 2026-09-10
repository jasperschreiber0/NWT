"""Regression coverage for delayed fills and duplicate equity exits."""
from unittest.mock import MagicMock
import pytest
import requests
import engine


def position(direction='short'):
    return dict(position_id='5d8ecdde-7a46-493c-adad-08320fd28745',
                asset='VGK', direction=direction, qty=196, status='open')


def missing():
    response = requests.Response()
    response.status_code = 404
    return requests.HTTPError(response=response)


@pytest.mark.parametrize('direction,side', [('short', 'buy'), ('long', 'sell')])
def test_close_side_and_stable_identity(monkeypatch, direction, side):
    get = MagicMock(side_effect=[missing(), {'is_open': True},
                                {'side': direction, 'qty': '196'}, []])
    post = MagicMock(return_value={'id': 'first'})
    monkeypatch.setattr(engine, 'alpaca_get', get)
    monkeypatch.setattr(engine, 'alpaca_post', post)
    engine.equity_close_order(position(direction))
    body = post.call_args.args[1]
    assert body['side'] == side and body['qty'] == '196'
    assert body['client_order_id'] == 'nwt-close-' + position()['position_id']
    assert body['position_intent'] == side + '_to_close'


@pytest.mark.parametrize('status', ['new', 'partially_filled', 'filled', 'canceled'])
def test_retry_reuses_order_without_submission(monkeypatch, status):
    order = dict(id='original', status=status, symbol='VGK', side='buy', qty='196')
    monkeypatch.setattr(engine, 'alpaca_get', MagicMock(return_value=order))
    post = MagicMock(); monkeypatch.setattr(engine, 'alpaca_post', post)
    assert engine.equity_close_order(position()) is order
    post.assert_not_called()


def test_ambiguous_lookup_does_not_submit(monkeypatch):
    monkeypatch.setattr(engine, 'alpaca_get', MagicMock(side_effect=requests.Timeout()))
    post = MagicMock(); monkeypatch.setattr(engine, 'alpaca_post', post)
    with pytest.raises(requests.Timeout): engine.equity_close_order(position())
    post.assert_not_called()


@pytest.mark.parametrize('broker', [{'side': 'long', 'qty': '196'}, {'side': 'short', 'qty': '100'}])
def test_mismatched_broker_exposure_blocks_close(monkeypatch, broker):
    monkeypatch.setattr(engine, 'alpaca_get', MagicMock(side_effect=[missing(), {'is_open': True}, broker]))
    post = MagicMock(); monkeypatch.setattr(engine, 'alpaca_post', post)
    with pytest.raises(ValueError): engine.equity_close_order(position())
    post.assert_not_called()


@pytest.mark.parametrize('status,qty,price,closed', [
    ('new', '0', None, False), ('partially_filled', '100', '89.82', False),
    ('filled', '100', '89.82', False), ('filled', '196', '0', False),
    ('filled', '196', '89.82', True)])
def test_only_complete_fill_closes_ledger(monkeypatch, status, qty, price, closed):
    pos = position()
    monkeypatch.setattr(engine, 'get_ledger_position', lambda *a: pos)
    monkeypatch.setattr(engine, 'equity_close_order', lambda *a: {'id': 'original'})
    monkeypatch.setattr(engine, 'poll_order_until_filled', lambda *a: dict(status=status, filled_qty=qty, filled_avg_price=price))
    close = MagicMock(); monkeypatch.setattr(engine, 'close_position', close)
    monkeypatch.setattr(engine, 'log_system_event', MagicMock())
    engine._close_equity_position(MagicMock(), pos, 89.82, pos['position_id'], 'VGK', 18074, 92.21, 'target')
    assert close.called is closed


def test_missing_direction_is_rejected(monkeypatch):
    pos = position(); del pos['direction']
    post = MagicMock(); monkeypatch.setattr(engine, 'alpaca_post', post)
    with pytest.raises(KeyError): engine.equity_close_order(pos)
    post.assert_not_called()
