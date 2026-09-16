import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock
import pytest
import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ops'))
from repair_job_config import repair


def module(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);sys.modules[name]=value;spec.loader.exec_module(value)
    return value


def test_job_settings_restore_dependencies_without_retrying_scanner_writes():
    jobs=repair({'perf-tracker':{},'scanner':{'retry_safe':False}})
    assert jobs['perf-tracker']['env'][-1]=='nwt_agents/.env'
    assert jobs['perf-tracker']['retry_safe']
    assert jobs['scanner']['timeout']==1800
    assert not jobs['scanner']['retry_safe']


def test_equity_uses_canonical_paper_credentials_and_failure_is_visible(monkeypatch):
    tracker=module('repair_tracker',ROOT/'performance/tracker.py')
    monkeypatch.setenv('NWT_ALPACA_BASE_URL','https://paper-api.alpaca.markets')
    monkeypatch.setenv('NWT_ALPACA_KEY_ID','paper-key')
    monkeypatch.setenv('NWT_ALPACA_SECRET_KEY','paper-secret')
    monkeypatch.setenv('ALPACA_BASE_URL','https://api.alpaca.markets')
    def failed(url,**kwargs):
        assert url=='https://paper-api.alpaca.markets/v2/account'
        assert kwargs['headers']['APCA-API-KEY-ID']=='paper-key'
        raise requests.Timeout('test')
    monkeypatch.setattr(requests,'get',failed)
    conn=Mock()
    with pytest.raises(RuntimeError,match='Equity curve update failed'):tracker.write_equity_curve(conn)
    conn.rollback.assert_called_once()


def test_edgar_failure_is_not_scored_as_zero_mentions(monkeypatch):
    edgar=module('repair_edgar',ROOT/'nwt_agents/track_f/validate_historical.py')
    monkeypatch.setattr(edgar.time,'sleep',lambda *_:None)
    monkeypatch.setattr(edgar.requests,'get',Mock(side_effect=requests.Timeout('test')))
    with pytest.raises(RuntimeError,match='missing evidence is not zero hits'):
        edgar.edgar_search(['grid'],'2026-09-01','2026-09-16',['10-K'],'Example')


def test_account_drawdown_includes_capital_and_requires_history():
    from unittest.mock import MagicMock
    tracker=module('drawdown_tracker',ROOT/'performance/tracker.py')
    conn=MagicMock()
    cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value=[('a',100000),('b',110000),('c',99000)]
    assert tracker.equity_drawdown(conn)==(.1,3)
    cur.fetchall.return_value=[('a',100000)]
    assert tracker.equity_drawdown(conn)==(None,1)
