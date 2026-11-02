"""Real persistence/recovery functions with a synthetic SQL adapter, not PostgreSQL."""
import json
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock
import pytest
import close_bounds as bounds
import close_recovery as recovery
import engine

NOW = datetime(2026, 10, 27, 0, 5, tzinfo=timezone.utc)
POS = dict(position_id='11111111-1111-1111-1111-111111111111', qty=2,
           asset='VGK', direction='long', entry_time='2026-10-26T22:00:00Z',
           status='open', close_generation=0, notional_risk=160)


def order(**changes):
    return dict(dict(id='receipt',client_order_id=recovery.close_client_id(POS),
                     symbol='VGK',side='sell',qty='2',filled_qty='0',status='new',
                     submitted_at='2026-10-27T00:00:00Z'), **changes)


class Store:
    """JSON round trips represent a fresh process; no SQL engine is claimed."""
    def __init__(self, saved=None):
        self.data=json.loads(saved) if saved else dict(position=deepcopy(POS),bounds={},progress=None)
        self.committed=deepcopy(self.data)
        self.result=None
        self.fail_write=False
        self.fail_commit=False
    def cursor(self, **kwargs):return self
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def commit(self):
        if self.fail_commit:raise RuntimeError('synthetic commit failure')
        self.committed=deepcopy(self.data)
    def rollback(self):self.data=deepcopy(self.committed)
    def saved(self):return json.dumps(self.committed)
    def execute(self, query, args=()):
        self.result=None
        if query.startswith('SELECT pg_advisory'):return
        if query.startswith('SELECT * FROM nwt_portfolio_ledger'):
            self.result=self.data['position'];return
        if query.startswith('SELECT requested_qty') or query.startswith('SELECT * FROM nwt_close_progress'):
            self.result=self.data['progress'];return
        if query.startswith('SELECT order_id,bounds'):
            self.result=self.data['bounds'].get(str(args[1]));return
        if query.startswith('INSERT INTO nwt_close_receipt_bounds'):
            if self.fail_write:raise RuntimeError('synthetic bounds write failure')
            self.data['bounds'][str(args[1])]=dict(order_id=args[2],bounds=json.loads(args[3]));return
        if query.startswith('INSERT INTO nwt_close_progress'):
            self.data['progress']=dict(order_id=args[2],requested_qty=str(args[3]),recorded_qty=str(args[4]),recorded_value=str(args[5]));return
        if query.startswith('SELECT ticket_id'):self.result=[];return
        if query.startswith('INSERT INTO nwt_system_log'):return
        raise AssertionError('Unexpected SQL: '+query)
    def fetchone(self):return deepcopy(self.result)
    def fetchall(self):return deepcopy(self.result if isinstance(self.result,list) else [self.result])


@pytest.mark.parametrize('filltime,accepted', [('2026-10-26T23:00:00Z',False),('2026-10-27T00:01:00Z',True)])
def test_committed_bound_survives_restart_and_recovery(monkeypatch,filltime,accepted):
    conn=Store()
    bounds.retain_close_bounds(conn,POS,order(),NOW)
    assert conn.data['progress'] is None  # Does not suppress a residual retry.
    restarted=Store(conn.saved())
    receipt=order(status='filled',filled_qty='2',filled_avg_price='80',filled_at=filltime)
    receipt.pop('submitted_at')
    original=deepcopy(receipt)
    close=Mock();monkeypatch.setattr(recovery,'close_position',close)
    if accepted:
        assert recovery.recover_closes(restarted,lambda _:receipt,NOW)['recorded']==[POS['position_id']]
        close.assert_called_once()
    else:
        from fill_evidence import with_fill_timestamp
        # The reviewer's original hole: this receipt alone has no lower bound.
        assert with_fill_timestamp(receipt,lambda _:pytest.fail('no lookup'),observed_at=NOW)==receipt
        with pytest.raises(ValueError,match='precedes submission'):
            recovery.recover_closes(restarted,lambda _:receipt,NOW)
        close.assert_not_called()
        assert restarted.data['progress'] is None
    assert receipt==original
    assert restarted.data['bounds']['0']['bounds']['submitted_at']=='2026-10-27T00:00:00Z'


@pytest.mark.parametrize('change',[dict(submitted_at='2026-10-26T22:30:00Z'),dict(submitted_at='bad'),dict(created_at='2026-10-27T00:01:00Z')])
def test_conflict_or_malformed_bounds_never_replace_prior(change):
    conn=Store();bounds.retain_close_bounds(conn,POS,order(),NOW);before=conn.saved()
    with pytest.raises(ValueError):bounds.retain_close_bounds(conn,POS,order(**change),NOW)
    assert conn.saved()==before


def test_retry_generation_has_independent_bound_and_identity():
    conn=Store();bounds.retain_close_bounds(conn,POS,order(),NOW)
    conn.data['position']['close_generation']=1;conn.commit()
    pos=conn.data['position'];next_order=order(id='retry',client_order_id=recovery.close_client_id(pos),submitted_at='2026-10-27T00:04:00Z')
    bounds.retain_close_bounds(conn,pos,next_order,NOW)
    assert set(conn.data['bounds'])=={'0','1'}
    with pytest.raises(ValueError,match='identity'):
        bounds.retain_close_bounds(conn,pos,dict(next_order,id='replacement'),NOW)


@pytest.mark.parametrize('failure',['write','commit'])
def test_persistence_failure_blocks_before_poll(monkeypatch,failure):
    conn=Store();setattr(conn,'fail_'+failure,True)
    poll=Mock();monkeypatch.setattr(engine,'poll_order_until_filled',poll)
    monkeypatch.setattr(engine,'log_system_event',Mock(side_effect=RuntimeError('audit also failed')))
    with pytest.raises(engine.CloseReceiptError):engine.poll_close_receipt(conn,POS,order())
    poll.assert_not_called()


def test_initial_bound_committed_before_poll_crash(monkeypatch):
    conn=Store()
    monkeypatch.setattr(engine,'poll_order_until_filled',Mock(side_effect=RuntimeError('crash')))
    monkeypatch.setattr(engine,'log_system_event',Mock())
    with pytest.raises(engine.CloseReceiptError):engine.poll_close_receipt(conn,POS,order())
    assert Store(conn.saved()).data['bounds']['0']['bounds']=={'submitted_at':'2026-10-27T00:00:00Z'}


def test_each_activity_respects_durable_bound_without_mutating_receipt():
    from fill_evidence import with_fill_timestamp
    conn=Store();sources=bounds.retain_close_bounds(conn,POS,order(),NOW)
    receipt=order(filled_qty='2',filled_avg_price='80',status='filled');receipt.pop('submitted_at')
    rows=[dict(id=str(i),order_id='receipt',symbol='VGK',side='sell',qty='1',price='80',transaction_time=t)
          for i,t in enumerate(['2026-10-26T23:59:59Z','2026-10-27T00:01:00Z'])]
    with pytest.raises(ValueError,match='precedes submission'):
        with_fill_timestamp(receipt,lambda _:rows,observed_at=NOW,additional_bounds=sources)
    rows[0]['transaction_time']='2026-10-27T00:00:00Z'
    assert with_fill_timestamp(receipt,lambda _:rows,observed_at=NOW,additional_bounds=sources)['filled_at']=='2026-10-27T00:01:00+00:00'
    assert 'submitted_at' not in receipt


def test_legacy_missing_bound_is_not_invented_and_null_cannot_erase():
    assert bounds.merge_bounds({},order(submitted_at=None),NOW)=={}
    old={'submitted_at':'2026-10-27T00:00:00Z'}
    assert bounds.merge_bounds(old,order(submitted_at=None),NOW)==old
    assert bounds.merge_bounds(old,order(submitted_at='2026-10-27T00:00:00+00:00'),NOW)==old


def test_pending_missing_submission_uses_retained_age_without_rewriting():
    receipt=order();receipt.pop('submitted_at')
    sources=bounds.bound_sources('receipt',{'submitted_at':'2026-10-27T00:00:00Z'})
    assert recovery.pending_close_receipt(POS,receipt,NOW,retained_bounds=sources)
    assert 'submitted_at' not in receipt
    with pytest.raises(ValueError,match='older than 15 minutes'):
        recovery.pending_close_receipt(POS,receipt,datetime(2026,10,27,0,16,tzinfo=timezone.utc),retained_bounds=sources)


@pytest.mark.parametrize('prior',[None,{'submitted_at':'bad'},{'unexpected':'value'}])
def test_malformed_retained_state_blocks(prior):
    with pytest.raises(ValueError):bounds.merge_bounds(prior,order(submitted_at=None),NOW)
