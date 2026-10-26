from datetime import timedelta
from unittest.mock import Mock
import sqlite3
import pytest
import investor_pipeline as pipeline
import investor_core as core
import investor_options as options
from test_investor_backoff import database,NOW

def test_feed_backoff_survives_calls_and_does_not_clear_warning(monkeypatch):
    c=database();fetch=Mock(side_effect=TimeoutError('private data'))
    monkeypatch.setattr(pipeline,'collect_feed',fetch)
    first=pipeline.collect_feed_with_backoff(c,None,'4',NOW)
    assert first['status']=='DEGRADED' and first['error']=='DEPENDENCY_TIMEOUT'
    assert pipeline.collect_feed_with_backoff(c,None,'4',NOW+timedelta(minutes=1))==first
    assert fetch.call_count==1
    second=pipeline.collect_feed_with_backoff(c,None,'4',NOW+timedelta(minutes=5))
    assert second['next_at']==(NOW+timedelta(minutes=15)).isoformat()
    fetch.side_effect=None;fetch.return_value=dict(status='OK')
    recovered=pipeline.collect_feed_with_backoff(c,None,'4',NOW+timedelta(minutes=15))
    assert recovered['failures']==0 and 'error' not in recovered
    assert recovered['last_success_at']==(NOW+timedelta(minutes=15)).isoformat()

def test_feed_database_failure_remains_fatal(monkeypatch):
    monkeypatch.setattr(pipeline,'collect_feed',Mock(side_effect=sqlite3.OperationalError('disk')))
    with pytest.raises(sqlite3.Error):pipeline.collect_feed_with_backoff(database(),None,'4',NOW)

def test_failed_quotes_rotate_and_expired_windows_remain_recorded():
    c=database();paper=Mock();paper.get.side_effect=TimeoutError()
    for i in range(7):
        pipeline.put(c,'forecast',str(i),dict(key=str(i),symbol='TEST',entry=NOW.date().isoformat(),entry_at=NOW.isoformat()))
    run=lambda at:options.run(c,paper,at,pipeline.rows,pipeline.one,pipeline.put)
    assert [e['forecast'] for e in run(NOW)['current_errors']]==['0','1','2']
    assert [e['forecast'] for e in run(NOW+timedelta(minutes=5))['current_errors']]==['3','4','5']
    assert run(NOW+timedelta(minutes=10))['current_errors'][0]['forecast']=='6'
    before=paper.get.call_count
    result=run(NOW+timedelta(minutes=21))
    assert result['missed_entries']==7 and result['coverage_status']=='INCOMPLETE'
    assert paper.get.call_count==before and result['completed']==0

def test_v4_policy_is_preserved_when_registering_v5():
    c=database();old=dict(version=core.CLASSIFICATION_VERSION,code_sha256='old')
    pipeline.put(c,'policy',core.CLASSIFICATION_VERSION,old)
    pipeline.register_version(c,core.POLICY,NOW)
    assert pipeline.one(c,'policy',core.CLASSIFICATION_VERSION)==old
    assert pipeline.one(c,'version_boundary',core.VERSION)['prior_versions']==[core.CLASSIFICATION_VERSION]
