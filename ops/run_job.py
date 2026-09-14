"""Recorded, non-overlapping server jobs. Only explicitly safe jobs retry."""
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.getenv('NWT_OPS_STATE', '/var/lib/nwt-ops'))


def db():
    STATE.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(STATE / 'operations.sqlite', timeout=20)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,job TEXT,started REAL,finished REAL,status TEXT,attempts INTEGER,exit_code INTEGER,log TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS sessions(day TEXT PRIMARY KEY,passed INTEGER,evidence TEXT,checked REAL)')
    c.execute('CREATE TABLE IF NOT EXISTS notifications(key TEXT PRIMARY KEY,sent REAL,message_id TEXT)')
    c.commit()
    return c


def run(name):
    config = json.loads((STATE / 'jobs.json').read_text())[name]
    STATE.mkdir(parents=True, exist_ok=True)
    # All order/reconciliation jobs share one lock. Other jobs use their own.
    own_lock = (STATE / (name + '.job.lock')).open('a')
    try:
        fcntl.flock(own_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    lock = (STATE / (config.get('lock', name) + '.lock')).open('a')
    # Different broker jobs share scheduled minutes. Serialize them rather than
    # dropping the jobs that arrive second; duplicate instances still skip above.
    until = time.monotonic() + 180
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= until:
                return 75
            time.sleep(1)
    c = db()
    now = time.time()
    log = STATE / 'logs' / (name + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '.log')
    log.parent.mkdir(exist_ok=True)
    rid = c.execute('INSERT INTO runs(job,started,status,log) VALUES (?,?,?,?)',
                    (name, now, 'running', str(log))).lastrowid
    c.commit()
    env = os.environ.copy()
    for envfile in config.get('env', []):
        env.update({k: v for k, v in dotenv_values(ROOT / envfile).items() if v is not None})
    env['SHARED_DIR'] = str(ROOT / 'shared')
    # Consolidate normal digests into the operations report, retaining critical alerts.
    env['NWT_OPS_DIGEST'] = '1'
    attempts = 0
    code = 1
    with log.open('w') as out:
        for attempt in range(3 if config.get('retry_safe') else 1):
            attempts += 1
            offset = out.tell()
            try:
                p = subprocess.Popen([sys.executable, *config['args']], cwd=ROOT / config['cwd'],
                                     env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    code = p.wait(timeout=config.get('timeout', 240))
                except subprocess.TimeoutExpired:
                    import signal
                    os.killpg(p.pid, signal.SIGTERM)
                    try: p.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(p.pid, signal.SIGKILL); p.wait()
                    code = 124
            except Exception as exc:
                out.write(type(exc).__name__ + '\n'); code = 125
            out.flush()
            with log.open() as observed:
                observed.seek(offset); segment = observed.read()
            if code == 0 and any(marker in segment for marker in ('Traceback (most recent call last)', '[ERROR]', '[INTEGRITY GATE FAIL]', 'Raw opportunity write failed')):
                code = 126
            if code == 0: break
            if attempt < (2 if config.get('retry_safe') else 0): time.sleep(5 * (attempt + 1))
    c.execute('UPDATE runs SET finished=?,status=?,attempts=?,exit_code=? WHERE id=?',
              (time.time(), 'ok' if code == 0 else 'failed', attempts, code, rid))
    c.commit(); c.close()
    return code


if __name__ == '__main__':
    sys.exit(run(sys.argv[1]))
