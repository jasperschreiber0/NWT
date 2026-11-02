"""Synthetic SQLite scoring fixtures; no broker, production state or Linux jobs."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from datetime import datetime, timezone
import pytest

sys.path.insert(0,str(Path(__file__).parent))
from trial_evidence import update_sessions, inspection_failure_status
from research_health import VERSION
from health_checks import HealthCheckError

DAY='2026-10-27'
NOW=datetime(2026,10,27,16,tzinfo=timezone.utc)
TRIAL=dict(start_date='2026-09-15',required_sessions=20,failure_drills_passed=True)
POLICY=dict(version=VERSION,effective_at='2026-10-01T00:00:00+00:00')


def database(path=':memory:'):
    c=sqlite3.connect(path)
    c.executescript('''CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,job TEXT,started REAL,status TEXT);
        CREATE TABLE IF NOT EXISTS sessions(day TEXT PRIMARY KEY,passed INTEGER,evidence TEXT,checked REAL);
        CREATE TABLE IF NOT EXISTS notifications(key TEXT PRIMARY KEY,sent REAL,message_id TEXT);''')
    return c


def healthy(**changes):
    return dict(dict(issues=[],trading_day=True,ready_for_entries=True,research_policy=POLICY),**changes)


def failed_run(c,job='investor-research',at=NOW):
    c.execute('INSERT INTO runs(job,started,status) VALUES (?, ?, ?)',(job,at.timestamp(),'failed'));c.commit()


def test_exception_fallback_retains_independent_policy(tmp_path):
    path=tmp_path/'research-policy.json';path.write_text(json.dumps(POLICY))
    status=inspection_failure_status(HealthCheckError('broker.calendar','ReadTimeout',15),path,NOW)
    assert status['research_policy']==POLICY
    assert status['health_failure']['operation']=='broker.calendar'
    assert not status['ready_for_entries']
    c=database();failed_run(c)
    update_sessions(c,status,TRIAL,DAY)
    sources=[r[0] for r in c.execute('SELECT source FROM session_incidents')]
    assert len(sources)==1 and sources[0].startswith('health:Health inspection failed: broker.calendar')


@pytest.mark.parametrize('policy',[None,{},[],{'version':VERSION,'effective_at':'bad'},
    {'version':VERSION,'effective_at':'2026-10-01T00:00:00'}])
def test_missing_or_malformed_policy_does_not_classify_research_or_grant_pass(policy):
    c=database();failed_run(c);status=healthy(research_policy=policy)
    update_sessions(c,status,TRIAL,DAY,report=True)
    assert c.execute('SELECT passed FROM sessions').fetchone()==(0,)
    assert not c.execute("SELECT source FROM session_incidents WHERE source LIKE 'run:%'").fetchall()
    assert 'classification unavailable' in status['issues'][0]
    assert not status['ready_for_entries']


@pytest.mark.parametrize('job,policy,passed',[
    ('investor-research',POLICY,1),('global-events',POLICY,1),('research-hub',POLICY,1),
    ('engine',POLICY,0),('unknown-job',POLICY,0),('engine',{},0),
    ('investor-research',dict(version=VERSION,effective_at='2026-10-28T00:00:00+00:00'),0)])
def test_advisory_and_genuine_failures(job,policy,passed):
    c=database();failed_run(c,job)
    update_sessions(c,healthy(research_policy=policy),TRIAL,DAY,report=True)
    assert c.execute('SELECT passed FROM sessions').fetchone()[0]==passed


def test_703_old_research_failures_never_rewrite_eight_passes_or_old_incidents():
    c=database()
    days=['12','13','14','16','19','20','21','22']
    for d in days:c.execute('INSERT INTO sessions VALUES (?,?,?,?)',('2026-10-'+d,1,'original evidence '+d,int(d)))
    for i in range(703):failed_run(c,at=datetime(2026,10,12+i%11,12,tzinfo=timezone.utc))
    c.execute("INSERT INTO notifications VALUES ('fault:2026-10-12:old finding',1,'old receipt')")
    c.commit()
    update_sessions(c,healthy(),TRIAL,DAY)
    c.execute("INSERT INTO session_incidents VALUES ('2026-10-12','existing','original incident')")
    c.commit()
    before=c.execute('SELECT * FROM sessions ORDER BY day').fetchall()
    old=c.execute("SELECT * FROM session_incidents WHERE day<'2026-10-27'").fetchall()
    for status in [healthy(research_policy={}),healthy(issues=['broker.calendar: ReadTimeout']),healthy()]:
        update_sessions(c,status,TRIAL,DAY,report=True)
    assert c.execute("SELECT * FROM sessions WHERE day<'2026-10-27' ORDER BY day").fetchall()==before
    assert c.execute("SELECT * FROM session_incidents WHERE day<'2026-10-27'").fetchall()==old
    assert c.execute("SELECT passed FROM sessions WHERE day=?",(DAY,)).fetchone()==(0,)
    assert not c.execute("SELECT * FROM session_incidents WHERE source LIKE 'run:%'").fetchall()


def test_same_day_downgrade_has_new_evidence_and_preserves_original(monkeypatch):
    c=database();c.execute('INSERT INTO sessions VALUES (?,?,?,?)',(DAY,1,'original bytes',123))
    monkeypatch.setattr('trial_evidence.time.time',lambda:456)
    failed_run(c,'engine')
    update_sessions(c,healthy(),TRIAL,DAY)
    current=c.execute('SELECT * FROM sessions').fetchone()
    assert current[1]==0 and current[3]==456
    assert json.loads(current[2])['trial_incident_sources']==['run:1']
    original=c.execute("SELECT passed,evidence,checked FROM session_adjudications WHERE reason='preserved_before_same_day_failure'").fetchone()
    assert original==(1,'original bytes',123)
    history=c.execute('SELECT * FROM session_adjudications').fetchall()
    for _ in range(3):update_sessions(c,healthy(),TRIAL,DAY,report=True)
    assert c.execute('SELECT * FROM sessions').fetchone()==current
    assert c.execute('SELECT * FROM session_adjudications').fetchall()==history


def test_date_boundaries_and_pretrial_are_not_backfilled():
    c=database()
    for at in [datetime(2026,10,26,23,59,59,tzinfo=timezone.utc),datetime(2026,10,28,tzinfo=timezone.utc)]:failed_run(c,'engine',at)
    update_sessions(c,healthy(),TRIAL,DAY,report=True)
    assert c.execute('SELECT passed FROM sessions').fetchone()==(1,)
    update_sessions(c,healthy(issues=['failure']),TRIAL,'2026-09-14',report=True)
    assert c.execute('SELECT count(*) FROM sessions').fetchone()==(1,)
    assert c.execute('SELECT count(*) FROM session_incidents').fetchone()==(0,)


@pytest.mark.parametrize('kind',['missing','corrupt'])
def test_policy_file_failure_remains_visible_without_exception_details(tmp_path,kind):
    path=tmp_path/'policy.json'
    if kind=='corrupt':path.write_text('{')
    status=inspection_failure_status(RuntimeError('secret must not escape'),path,NOW)
    assert status['research_policy']=={} and status['issues']==['Health inspection failed: RuntimeError']


@pytest.mark.parametrize('inspection_fails,delivery_ok',[(True,True),(False,False)])
def test_actual_watchdog_main_fallback_and_report_failure_preserve_history(monkeypatch,tmp_path,inspection_fails,delivery_ok):
    # Stub only the platform job-runner boundary, not fcntl or scoring. No jobs
    # execute and no claim is made about Linux locking or production inspection.
    path=tmp_path/'synthetic.sqlite'
    c=database(path);c.execute("INSERT INTO sessions VALUES ('2026-10-12',1,'historic',123)");c.commit();c.close()
    (tmp_path/'trial.json').write_text(json.dumps(TRIAL))
    (tmp_path/'research-policy.json').write_text(json.dumps(POLICY))
    monkeypatch.setitem(sys.modules,'run_job',SimpleNamespace(ROOT=Path(__file__).resolve().parents[1],STATE=tmp_path,db=lambda:database(path)))
    spec=importlib.util.spec_from_file_location('scoring_watchdog_fixture',Path(__file__).with_name('watchdog.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'datetime',SimpleNamespace(now=lambda tz:NOW))
    monkeypatch.setattr(sys,'argv',['watchdog','--report'])
    def inspect():
        if inspection_fails:raise HealthCheckError('broker.calendar','ReadTimeout',15)
        return healthy()
    monkeypatch.setattr(module,'inspect',inspect)
    monkeypatch.setattr(module,'notify',lambda *a:delivery_ok)
    module.main()
    c=sqlite3.connect(path)
    assert c.execute("SELECT * FROM sessions WHERE day='2026-10-12'").fetchone()==('2026-10-12',1,'historic',123)
    status=json.loads((tmp_path/'status.json').read_text())
    assert status['research_policy']==POLICY
    assert not status['ready_for_entries']
    if not inspection_fails:
        assert c.execute('SELECT passed FROM sessions WHERE day=?',(DAY,)).fetchone()==(0,)
        assert c.execute("SELECT count(*) FROM session_adjudications WHERE reason='preserved_before_same_day_failure'").fetchone()==(1,)
    c.close()


@pytest.mark.parametrize('failure_first',[False,True])
def test_two_connections_serialize_both_orderings(tmp_path,failure_first):
    from threading import Event, Thread
    path=tmp_path/'race.sqlite'
    database(path).close()
    reached=Event();release=Event();errors=[]
    first=healthy(issues=['genuine engine failure']) if failure_first else healthy()
    second=healthy() if failure_first else healthy(issues=['genuine engine failure'])
    def writer():
        conn=sqlite3.connect(path,timeout=.1)
        def pause(query):
            if query.startswith('SELECT passed,evidence,checked'):
                reached.set()
                if not release.wait(5):raise AssertionError('test release timeout')
        conn.set_trace_callback(pause)
        try:update_sessions(conn,first,TRIAL,DAY,report=True)
        except BaseException as exc:errors.append(exc)
        finally:conn.close()
    thread=Thread(target=writer);thread.start()
    other=sqlite3.connect(path,timeout=.05)
    try:
        assert reached.wait(5)
        # First scorer holds its write reservation before reading prior state.
        with pytest.raises(sqlite3.OperationalError,match='locked'):
            update_sessions(other,second,TRIAL,DAY,report=True)
        assert not other.in_transaction
    finally:
        release.set();thread.join(5)
    assert not thread.is_alive() and not errors
    update_sessions(other,second,TRIAL,DAY,report=True)
    row=other.execute('SELECT passed,evidence,checked FROM sessions').fetchone()
    assert row[0]==0
    assert json.loads(row[1])['trial_incident_sources']==['health:genuine engine failure']
    history=other.execute('SELECT passed,evidence,checked,reason FROM session_adjudications ORDER BY id').fetchall()
    assert [r[0] for r in history]==([0] if failure_first else [1,1,0])
    if not failure_first:
        assert history[0][:3]==history[1][:3]  # Original successful evidence preserved exactly.
    for _ in range(2):update_sessions(other,healthy(),TRIAL,DAY,report=True)
    assert other.execute('SELECT passed,evidence,checked FROM sessions').fetchone()==row
    assert other.execute('SELECT passed,evidence,checked,reason FROM session_adjudications ORDER BY id').fetchall()==history
    assert other.execute('SELECT COUNT(*) FROM session_incidents').fetchone()==(1,)
    other.close()


def test_database_upsert_cannot_upgrade_or_overwrite_failure_evidence():
    from trial_evidence import SESSION_UPSERT
    c=database();c.execute('INSERT INTO sessions VALUES (?,?,?,?)',(DAY,0,'original failure',123));c.commit()
    assert c.execute(SESSION_UPSERT,(DAY,1,'stale healthy observation',456)).rowcount==0
    c.commit()
    assert c.execute('SELECT passed,evidence,checked FROM sessions').fetchone()==(0,'original failure',123)


def test_scoring_failure_rolls_back_incident_score_and_adjudication_together():
    c=database();update_sessions(c,healthy(),TRIAL,DAY,report=True)
    before=c.execute('SELECT * FROM sessions').fetchall()
    history=c.execute('SELECT * FROM session_adjudications').fetchall()
    c.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON session_adjudications BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END")
    c.commit()
    with pytest.raises(sqlite3.IntegrityError,match='synthetic audit failure'):
        update_sessions(c,healthy(issues=['genuine failure']),TRIAL,DAY)
    assert not c.in_transaction
    assert c.execute('SELECT * FROM sessions').fetchall()==before
    assert c.execute('SELECT * FROM session_adjudications').fetchall()==history
    assert c.execute('SELECT COUNT(*) FROM session_incidents').fetchone()==(0,)
    c.execute('DROP TRIGGER fail_audit');c.commit()
    update_sessions(c,healthy(issues=['genuine failure']),TRIAL,DAY)
    assert c.execute('SELECT passed FROM sessions').fetchone()==(0,)


def test_caller_transaction_is_rejected_without_commit_or_rollback():
    c=database();c.execute('INSERT INTO sessions VALUES (?,?,?,?)',('2026-10-12',1,'caller-owned',123))
    with pytest.raises(ValueError,match='active transaction'):
        update_sessions(c,healthy(),TRIAL,DAY,report=True)
    assert c.in_transaction
    assert c.execute('SELECT evidence FROM sessions').fetchone()==('caller-owned',)
    c.rollback()
    assert c.execute('SELECT COUNT(*) FROM sessions').fetchone()==(0,)
