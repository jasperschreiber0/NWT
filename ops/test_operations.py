import json
import os
from pathlib import Path
import sys
import sqlite3
from unittest.mock import Mock
import pytest
sys.path.insert(0,str(Path(__file__).parent))
import run_job
import watchdog
from install_opportunity_recovery import updated_cron


def test_replay_schedule_is_frequent_and_install_is_idempotent():
    original='SHELL=/bin/bash\n40 21 * * 1-5 /usr/bin/python3 /home/northworld/trading/ops/run_job.py opportunity-replay >> /var/log/nwt/ops-runner.log 2>&1\n'
    updated=updated_cron(original)
    assert '*/5 * * * * /usr/bin/python3' in updated
    assert updated_cron(updated)==updated
    with pytest.raises(RuntimeError):updated_cron('')


def test_trial_requires_consecutive_sessions():
    assert watchdog.trial_streak([('a',1),('b',1),('c',0),('d',1)])==1
    assert watchdog.trial_streak([])==0


def test_notification_rejection_is_not_recorded(monkeypatch,tmp_path):
    monkeypatch.setattr(run_job,'STATE',tmp_path)
    c=run_job.db()
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','fake');monkeypatch.setenv('TELEGRAM_CHAT_ID','fake')
    response=Mock();response.json.return_value={'ok':False}
    monkeypatch.setattr(watchdog.requests,'post',lambda *a,**k:response)
    assert not watchdog.notify(c,'key','test')
    assert not c.execute('SELECT * FROM notifications').fetchall()


def test_safe_job_retries_but_order_job_does_not(monkeypatch,tmp_path):
    monkeypatch.setattr(run_job,'STATE',tmp_path)
    monkeypatch.setattr(run_job,'ROOT',tmp_path)
    monkeypatch.setattr(run_job.time,'sleep',lambda _:None)
    p=Mock();p.wait.side_effect=[1,1,0,1]
    popen=Mock(return_value=p);monkeypatch.setattr(run_job.subprocess,'Popen',popen)
    jobs={name:dict(cwd='.',args=['fake.py'],retry_safe=safe) for name,safe in [('collector',True),('engine',False)]}
    (tmp_path/'jobs.json').write_text(json.dumps(jobs))
    assert run_job.run('collector')==0
    assert run_job.run('engine')==1
    assert popen.call_count==4
    c=run_job.db()
    assert c.execute('SELECT job,status,attempts FROM runs ORDER BY id').fetchall()==[
        ('collector','ok',3),('engine','failed',1)]


def test_broker_job_waits_for_shared_lock(monkeypatch,tmp_path):
    monkeypatch.setattr(run_job,'STATE',tmp_path)
    monkeypatch.setattr(run_job,'ROOT',tmp_path)
    jobs={'engine':dict(cwd='.',args=['fake.py'],lock='broker',retry_safe=False)}
    (tmp_path/'jobs.json').write_text(json.dumps(jobs))
    calls=[]
    def acquire(file,flags):
        calls.append(file.name)
        if len(calls)==2:raise BlockingIOError()
    monkeypatch.setattr(run_job.fcntl,'flock',acquire)
    monkeypatch.setattr(run_job.time,'sleep',lambda _:None)
    process=Mock();process.wait.return_value=0
    monkeypatch.setattr(run_job.subprocess,'Popen',lambda *a,**k:process)
    assert run_job.run('engine')==0
    assert len(calls)==3
    assert calls[1]==calls[2]



def test_intraday_failure_cannot_be_replaced_by_healthy_report(monkeypatch,tmp_path):
    from trial_evidence import update_sessions, unresolved_job_failure
    monkeypatch.setattr(run_job,'STATE',tmp_path)
    c=run_job.db();trial={'start_date':'2026-09-15'}
    update_sessions(c,{'issues':['mismatch'],'trading_day':True},trial,'2026-09-17')
    update_sessions(c,{'issues':[],'trading_day':True},trial,'2026-09-17',report=True)
    assert c.execute('SELECT passed FROM sessions').fetchone()[0]==0
    c.execute("INSERT INTO runs(job,started,status) VALUES ('engine',1789680000,'failed')")
    c.execute("INSERT INTO runs(job,started,status) VALUES ('engine',1789680001,'running')")
    assert unresolved_job_failure(c,'engine')
    c.execute("INSERT INTO runs(job,started,status) VALUES ('engine',1789680002,'ok')")
    assert not unresolved_job_failure(c,'engine')


def test_historical_fault_receipts_correct_false_trial_credit(monkeypatch,tmp_path):
    from trial_evidence import update_sessions
    monkeypatch.setattr(run_job,'STATE',tmp_path)
    c=run_job.db()
    c.execute("INSERT INTO sessions VALUES ('2026-09-16',1,'{}',0)")
    c.execute("INSERT INTO notifications VALUES ('fault:2026-09-16:engine job failed',0,'receipt')")
    update_sessions(c,{'issues':[],'trading_day':True},{'start_date':'2026-09-15'},'2026-09-17',report=True)
    assert c.execute('SELECT day,passed FROM sessions ORDER BY day').fetchall()==[('2026-09-16',0),('2026-09-17',1)]
