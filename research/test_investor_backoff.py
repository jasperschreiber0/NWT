"""ARK source failures cannot weaken freshness or replay the whole collector."""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import investor_core as core
import investor_pipeline as pipeline

NOW = datetime(2026, 10, 13, 22, tzinfo=timezone.utc)
CALENDAR = [dict(date=d, open='09:30', close='16:00')
            for d in ['2026-10-09', '2026-10-12', '2026-10-13', '2026-10-14']]


def database():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE evidence(kind TEXT,key TEXT,payload TEXT,PRIMARY KEY(kind,key))')
    c.execute('CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT)')
    return c


def csv_blob(fund, day, shares=100):
    return ('date,fund,ticker,cusip,shares,market value ($)\n' + ''.join(
        f'{day},{fund},S{i},CUSIP{i},{shares},1000\n' for i in range(25))).encode()


class Public:
    def __init__(self):
        self.days = dict(ARKK='10/13/2026', ARKQ='10/13/2026', ARKW='10/06/2026')
        self.calls = []
        self.shares = 100

    def get(self, url):
        fund = next(f for f in pipeline.ARK if f + '_HOLDINGS' in url)
        self.calls.append(fund)
        value = self.days[fund]
        if isinstance(value, Exception):
            raise value
        blob = csv_blob(fund, value, self.shares)
        return blob, dict(url=url, sha256=hashlib.sha256(blob).hexdigest(), observed_at=NOW.isoformat())


@pytest.mark.parametrize('day', ['10/06/2026', '10/14/2026'])
def test_invalid_dates_are_cached_and_cannot_signal(day):
    c = database(); public = Public(); public.days['ARKW'] = day
    health = pipeline.collect_ark(c, public, NOW)
    bad = health['ARKW']
    assert bad['status'] == 'DEGRADED' and bad['date'] == datetime.strptime(day, '%m/%d/%Y').date().isoformat()
    assert bad['source']['sha256'] and bad['next_at'] == (NOW + timedelta(hours=1)).isoformat()
    assert pipeline.rows(c, 'ark') and not pipeline.one(c, 'ark', 'ARKW:' + bad['date'])
    pipeline.eligible_ark(c, health, CALENDAR, NOW)
    assert pipeline.state(c, 'fresh-ark-funds') == ['ARKK', 'ARKQ']
    calls = public.calls[:]
    again = pipeline.collect_ark(c, public, NOW + timedelta(minutes=5))
    assert again['ARKW'] == bad and public.calls == calls


def test_backoff_is_persisted_capped_and_recovers_on_fresh_snapshot():
    c = database(); public = Public(); now = NOW
    for hours in [1, 2, 4, 6, 6]:
        health = pipeline.collect_ark(c, public, now)
        next_at = core.stamp(health['ARKW']['next_at'])
        assert next_at - now == timedelta(hours=hours)
        calls = public.calls.count('ARKW')
        pipeline.collect_ark(c, public, next_at - timedelta(seconds=1))
        assert public.calls.count('ARKW') == calls
        now = next_at
    public.days['ARKW'] = now.strftime('%m/%d/%Y')
    fresh = pipeline.collect_ark(c, public, now)['ARKW']
    assert fresh['status'] == 'OK' and fresh['failures'] == 0 and 'error' not in fresh


def test_transport_failure_does_not_starve_other_funds():
    c = database(); public = Public(); public.days['ARKK'] = TimeoutError('timeout')
    health = pipeline.collect_ark(c, public, NOW)
    assert health['ARKK']['status'] == 'DEGRADED'
    assert health['ARKQ']['status'] == 'OK'
    assert public.calls == ['ARKK', 'ARKQ', 'ARKW']


def test_same_date_revision_stays_quarantined_even_if_original_returns():
    c = database(); public = Public(); public.days['ARKW'] = '10/13/2026'
    pipeline.collect_ark(c, public, NOW)
    original = pipeline.one(c, 'ark', 'ARKW:2026-10-13')
    public.shares = 101
    assert pipeline.collect_ark(c, public, NOW + timedelta(hours=1))['ARKW']['status'] == 'DEGRADED'
    assert pipeline.one(c, 'ark', 'ARKW:2026-10-13') == original
    public.shares = 100
    assert pipeline.collect_ark(c, public, NOW + timedelta(hours=2))['ARKW']['status'] == 'DEGRADED'
    assert len(pipeline.rows(c, 'ark_revision')) == 3


@pytest.mark.parametrize('now,day,expected', [
    ('2026-10-12T00:05:00+00:00', '2026-10-09', True),  # weekend remains Friday
    ('2026-10-13T20:14:59+00:00', '2026-10-12', True),
    ('2026-10-13T20:15:00+00:00', '2026-10-12', False),
    ('2026-10-13T20:14:59+00:00', '2026-10-13', False),
    ('2026-10-13T20:15:00+00:00', '2026-10-13', True),
    ('2026-10-14T00:05:00+00:00', '2026-10-13', True),
])
def test_cached_health_rechecks_exchange_session_boundary(now, day, expected):
    c = database(); health = {'ARKW': dict(status='OK', date=day)}
    pipeline.eligible_ark(c, health, CALENDAR, core.stamp(now))
    assert ('ARKW' in pipeline.state(c, 'fresh-ark-funds')) == expected


def test_utc_date_guard_rechecks_cached_success_at_midnight():
    c = database(); public = Public(); public.days['ARKW'] = '10/08/2026'
    now = datetime(2026, 10, 13, 23, 50, tzinfo=timezone.utc)
    assert pipeline.collect_ark(c, public, now)['ARKW']['status'] == 'OK'
    assert pipeline.collect_ark(c, public, now + timedelta(minutes=20))['ARKW']['status'] == 'DEGRADED'
    assert pipeline.ark_date_valid('2026-10-13', core.stamp('2026-10-14T01:00:00+02:00'))


def test_holiday_and_early_close_use_exchange_calendar_with_dst():
    c = database()
    calendar = [dict(date='2026-11-25', open='09:30', close='16:00'),
                dict(date='2026-11-27', open='09:30', close='13:00')]
    for moment, day in [('2026-11-26T22:00:00Z', '2026-11-25'),
                        ('2026-11-27T18:14:59Z', '2026-11-25'),
                        ('2026-11-27T18:15:00Z', '2026-11-27')]:
        health = {fund: dict(status='OK', date=day) for fund in pipeline.ARK}
        pipeline.eligible_ark(c, health, calendar, core.stamp(moment))
        assert pipeline.state(c, 'fresh-ark-funds') == list(pipeline.ARK)


def test_stale_fund_cannot_propose_while_healthy_fund_still_can():
    c = database(); public = Public()
    for fund, symbol in [('ARKW', 'STALE'), ('ARKK', 'FRESH')]:
        for day in ['2026-10-09', '2026-10-12', '2026-10-13']:
            pipeline.put(c, 'ark_change', fund + day, dict(fund=fund, symbol=symbol,
                date=day, first_seen_at=NOW.isoformat(), source=dict(sha256=fund + day)))
    health = pipeline.collect_ark(c, public, NOW)
    pipeline.eligible_ark(c, health, CALENDAR, NOW)
    calendar = CALENDAR + [dict(date=f'2026-11-{i:02d}', open='09:30', close='16:00')
                           for i in range(2, 30)]
    context = {s: dict(eligible=True, last_session_return=0) for s in ['STALE', 'FRESH']}
    assert pipeline.propose(c, NOW, calendar, set(context), context) == 2
    forecasts = pipeline.rows(c, 'forecast')
    assert {p['symbol'] for p in forecasts} == {'FRESH'}
    assert all(p['policy_version'] == core.VERSION for p in forecasts)


def test_database_errors_are_not_reported_as_cached_source_failure(monkeypatch):
    c = database()
    monkeypatch.setattr(pipeline, 'one', Mock(side_effect=sqlite3.OperationalError('disk unavailable')))
    with pytest.raises(sqlite3.OperationalError):
        pipeline.collect_ark(c, Public(), NOW)


def test_boundary_is_append_only_idempotent_and_detects_changed_code():
    c = database()
    pipeline.put(c, 'policy', core.LEGACY_VERSION, dict(version=core.LEGACY_VERSION, code_sha256='old'))
    pipeline.put(c, 'forecast', 'pending', dict(exit='2026-10-20', frozen_at=NOW.isoformat()))
    old = c.execute('SELECT * FROM evidence ORDER BY kind,key').fetchall()
    policy = dict(core.POLICY, code_sha256='new')
    pipeline.register_version(c, policy, NOW)
    pipeline.register_version(c, policy, NOW + timedelta(hours=1))
    assert all(row in c.execute('SELECT * FROM evidence').fetchall() for row in old)
    assert pipeline.one(c, 'version_boundary', core.VERSION)['effective_at'] == NOW.isoformat()
    assert len(pipeline.rows(c, 'version_boundary')) == 1
    with pytest.raises(ValueError, match='Frozen'):
        pipeline.register_version(c, dict(policy, code_sha256='changed'), NOW)


def test_legacy_forecast_outcome_continues_with_original_lineage():
    c = database()
    forecast = dict(key='old', symbol='TEST', entry='2026-10-05', exit='2026-10-12',
        entry_at='2026-10-05T13:30:00+00:00', exit_at='2026-10-12T13:30:00+00:00',
        frozen_at='2026-10-04T22:00:00+00:00', rule='insider_single', horizon=5)
    pipeline.put(c, 'forecast', 'old', forecast)
    pipeline.register_version(c, dict(core.POLICY, code_sha256='new'), NOW)
    paper = Mock()
    paper.get.return_value = dict(bars={s: [dict(t='2026-10-05T13:30:00Z', o=100),
        dict(t='2026-10-12T13:30:00Z', o=101)] for s in ['TEST', 'SPY']})
    assert pipeline.evaluate(c, paper, NOW)['completed'] == 1
    assert pipeline.one(c, 'outcome', 'old')['policy_version'] == core.LEGACY_VERSION
    assert pipeline.one(c, 'forecast', 'old') == forecast
    assert pipeline.evaluate(c, paper, NOW)['completed'] == 0


def test_stale_ark_cycle_evaluates_outcomes_and_finishes_without_retry(monkeypatch, tmp_path):
    import investor_options
    import run_job
    import sys
    public = Public(); output = tmp_path / 'evidence'
    # Exercise main twice through the real scheduler with only its child process mocked.
    monkeypatch.setattr(pipeline.fcntl, 'flock', lambda *a: None)
    monkeypatch.setattr(pipeline, 'Public', lambda out: public)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return NOW
    monkeypatch.setattr(pipeline, 'datetime', Clock)
    output.mkdir(); c = sqlite3.connect(output / 'investors.sqlite')
    c.execute('CREATE TABLE evidence(kind TEXT,key TEXT,payload TEXT,PRIMARY KEY(kind,key))')
    c.execute('CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT)')
    pipeline.state(c, 'tickers', dict(at=NOW.isoformat(), mapping={}))
    c.close()
    monkeypatch.setattr(pipeline, 'collect_feed', lambda *a: dict(status='OK'))
    monkeypatch.setattr(pipeline, 'parse_filings', lambda *a: dict(parsed=0, backlog=0, errors=[]))
    monkeypatch.setattr(pipeline, 'collect_managers', lambda *a: {})
    monkeypatch.setattr(pipeline, 'classify_activists', lambda *a: dict(status='OK'))
    paper = Mock(); paper.get.side_effect = lambda path, params: CALENDAR if 'calendar' in path else []
    monkeypatch.setattr(pipeline, 'Paper', lambda: paper)
    monkeypatch.setattr(pipeline, 'market_context', lambda *a: {})
    evaluate = Mock(return_value=dict(completed=0, unresolved=0))
    monkeypatch.setattr(pipeline, 'evaluate', evaluate)
    monkeypatch.setattr(investor_options, 'run', lambda *a: {})
    monkeypatch.setattr(sys, 'argv', ['investor_pipeline', '--output', str(output)])
    monkeypatch.setattr(run_job, 'STATE', tmp_path)
    monkeypatch.setattr(run_job, 'ROOT', tmp_path)
    (tmp_path / 'jobs.json').write_text(json.dumps({'investor-research':
        dict(cwd='.', args=['research/investor_pipeline.py'], retry_safe=True)}))
    class Process:
        def wait(self, **kwargs):
            pipeline.main()
            return 0
    popen = Mock(return_value=Process())
    monkeypatch.setattr(run_job.subprocess, 'Popen', popen)
    for _ in range(2): assert run_job.run('investor-research') == 0
    assert popen.call_count == evaluate.call_count == 2
    assert public.calls == ['ARKK', 'ARKQ', 'ARKW']
    report = json.loads((output / 'latest.json').read_text())
    assert report['status'] == 'DEGRADED' and report['retryable'] is False
    assert report['sources']['ARKW']['status'] == 'DEGRADED'
    c = sqlite3.connect(output / 'investors.sqlite')
    assert pipeline.state(c, 'fresh-ark-funds') == ['ARKK', 'ARKQ']
    assert pipeline.rows(c, 'forecast') == []
    c.close()
    c = run_job.db()
    assert c.execute('SELECT status,attempts FROM runs').fetchall() == [('ok', 1), ('ok', 1)]
    c.close()


def test_retryable_failure_still_exits_nonzero(tmp_path):
    c = database()
    with pytest.raises(SystemExit) as result:
        pipeline.finish_report(c, tmp_path, dict(status='DEGRADED', issues=['Market evaluation unavailable']), True)
    assert result.value.code == 1
    assert json.loads((tmp_path / 'latest.json').read_text())['retryable'] is True
