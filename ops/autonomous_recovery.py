"""Bounded repair of explicitly replay-safe jobs, never blind replay of order jobs."""
import fcntl
import json
import sqlite3
import subprocess
import sys
from datetime import datetime,timezone
from paper_io import Paper,STATE,ROOT,atomic

# Exact commands prevent a changed job configuration inheriting replay permission.
SAFE={'research-hub':('.', ['research/research_hub.py']),
      'unified-performance':('.', ['ops/unified_performance.py']),
      'perf-tracker':('performance',['tracker.py']),
      'opportunity-replay':('.',['ops/replay_opportunities.py']),
      'research-collector':('.',['research/collect.py']),
      'paper-discovery':('.',['research/discovery.py']),
      'learning-review':('.',['ops/learning_review.py'])}


def candidates(jobs,latest,attempts,now,trading_day,market_open=False):
    chosen=[];blocked=[]
    midnight=now.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    for name,(cwd,args) in SAFE.items():
        job=jobs.get(name,{})
        if job.get('cwd')!=cwd or job.get('args')!=args or not job.get('retry_safe'):continue
        row=latest.get(name);deadline=job.get('deadline_utc')
        missing=trading_day and deadline and now.strftime('%H:%M')>=deadline and (not row or row['started']<midnight)
        failed=row and row['status']=='failed'
        overdue=market_open and job.get('market_interval') and (not row or now.timestamp()-row['started']>job['market_interval'])
        if not (missing or failed or overdue) or (row and row['status']=='running'):continue
        tries=[a for a in attempts if a['job']==name and a['started']>=midnight]
        if len(tries)>=2:blocked.append(name+': retry budget exhausted');continue
        if tries and now.timestamp()-max(a['started'] for a in tries)<1800:continue
        chosen.append(name)
    return chosen[:1],blocked


def main():
    now=datetime.now(timezone.utc);paper=Paper()
    trading=bool(paper.get('/v2/calendar',dict(start=now.date().isoformat(),end=now.date().isoformat())))
    jobs=json.loads((STATE/'jobs.json').read_text());c=sqlite3.connect(STATE/'operations.sqlite',timeout=20)
    c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS recovery_attempts(id INTEGER PRIMARY KEY,job TEXT,started REAL,finished REAL,exit_code INTEGER)')
    latest={}
    for name in SAFE:
        row=c.execute('SELECT * FROM runs WHERE job=? ORDER BY id DESC LIMIT 1',(name,)).fetchone()
        if row and row['status']=='running':
            with (STATE/(name+'.job.lock')).open('a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:pass
                else:
                    c.execute("UPDATE runs SET status='failed',finished=?,exit_code=125 WHERE id=? AND status='running'",(now.timestamp(),row['id']))
                    c.commit();row=c.execute('SELECT * FROM runs WHERE id=?',(row['id'],)).fetchone()
        latest[name]=dict(row) if row else None
    choices,blocked=candidates(jobs,latest,[dict(x) for x in c.execute('SELECT * FROM recovery_attempts')],now,trading,paper.get('/v2/clock').get('is_open',False))
    repaired=[]
    for name in choices:
        rid=c.execute('INSERT INTO recovery_attempts(job,started) VALUES (?,?)',(name,now.timestamp())).lastrowid;c.commit()
        # Never kill the lock-owning runner and leave its child free to duplicate.
        process=subprocess.Popen([sys.executable,str(ROOT/'ops/run_job.py'),name])
        try:code=process.wait(timeout=3000)
        except subprocess.TimeoutExpired:code=124
        c.execute('UPDATE recovery_attempts SET finished=?,exit_code=? WHERE id=?',(datetime.now(timezone.utc).timestamp(),code,rid));c.commit()
        repaired.append(dict(job=name,exit_code=code))
    c.close()
    atomic(STATE/'recovery.json',dict(observed_at=now.isoformat(),status='ATTENTION' if blocked else 'OK',attempts=repaired,blocked=blocked,
        policy='Exact allowlist, at most two extra retries/job/day with 30-minute spacing; order and position jobs keep their existing idempotent recovery'))
    print(json.dumps(dict(attempts=repaired,blocked=blocked)))


if __name__=='__main__':main()
