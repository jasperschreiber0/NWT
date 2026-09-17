"""A recovered snapshot cannot erase failures earlier in a trial session."""
import json
import time
from datetime import datetime, timezone


def update_sessions(c, status, trial, day, report=False):
    c.execute('CREATE TABLE IF NOT EXISTS session_incidents(day TEXT, source TEXT, evidence TEXT, PRIMARY KEY(day,source))')
    # Existing receipts and failed runs are retained evidence, including before
    # this upgrade. Do not invent historical healthy monitoring coverage.
    for key, sent in c.execute("SELECT key,sent FROM notifications WHERE key LIKE 'fault:%'").fetchall():
        incident_day = key.split(':', 2)[1]
        if incident_day >= trial['start_date']:
            c.execute('INSERT OR IGNORE INTO session_incidents VALUES (?,?,?)',
                      (incident_day, key, json.dumps({'notification': key, 'sent': sent})))
    for rid, job, started in c.execute("SELECT id,job,started FROM runs WHERE status='failed'").fetchall():
        incident_day = datetime.fromtimestamp(started, timezone.utc).date().isoformat()
        if incident_day >= trial['start_date']:
            c.execute('INSERT OR IGNORE INTO session_incidents VALUES (?,?,?)',
                      (incident_day, 'run:' + str(rid), json.dumps({'job': job, 'run_id': rid})))
    if day >= trial['start_date'] and status.get('issues'):
        c.execute('INSERT OR IGNORE INTO session_incidents VALUES (?,?,?)',
                  (day, 'health:' + '|'.join(status['issues']), json.dumps(status, default=str)))
    c.execute('UPDATE sessions SET passed=0 WHERE day IN (SELECT day FROM session_incidents)')
    if report and status.get('trading_day') and day >= trial['start_date']:
        incident = c.execute('SELECT 1 FROM session_incidents WHERE day=? LIMIT 1', (day,)).fetchone()
        c.execute('INSERT INTO sessions VALUES (?,?,?,?) ON CONFLICT(day) DO UPDATE SET '
                  'passed=MIN(sessions.passed,excluded.passed),evidence=excluded.evidence,checked=excluded.checked',
                  (day, int(not status.get('issues') and not incident), json.dumps(status, default=str), time.time()))
    c.commit()
    return c.execute('SELECT COUNT(*) FROM session_incidents WHERE day=?', (day,)).fetchone()[0]


def unresolved_job_failure(c, name):
    # Starting a retry proves nothing about recovery; require completed success.
    row = c.execute("SELECT status FROM runs WHERE job=? AND status IN ('ok','failed') ORDER BY id DESC LIMIT 1", (name,)).fetchone()
    return bool(row and row[0] == 'failed')
