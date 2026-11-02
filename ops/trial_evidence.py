"""A recovered snapshot cannot erase failures earlier in a trial session."""
import json
import time
from datetime import datetime, timezone, timedelta
from research_health import research_run_after_policy, VERSION
from health_checks import HealthCheckError


def inspection_failure_status(exc, policy_path, now):
    """A failed broker read does not remove independent research policy context."""
    try:
        policy = json.loads(policy_path.read_text())
    except (OSError, ValueError):
        policy = {}
    status = dict(observed_at=now.isoformat(), ready_for_entries=False,
                  research_policy=policy,
                  issues=['Health inspection failed: ' +
                          (str(exc) if isinstance(exc, HealthCheckError) else type(exc).__name__)])
    if isinstance(exc, HealthCheckError):
        status['health_failure'] = exc.details()
    return status


def policy_known(policy):
    try:
        stamp = datetime.fromisoformat(policy['effective_at'])
        return policy['version'] == VERSION and stamp.tzinfo is not None and stamp.utcoffset() is not None
    except (KeyError, TypeError, ValueError):
        return False


SESSION_UPSERT = (
    'INSERT INTO sessions VALUES (?,?,?,?) ON CONFLICT(day) DO UPDATE SET '
    'passed=MIN(sessions.passed,excluded.passed),evidence=excluded.evidence,checked=excluded.checked '
    'WHERE sessions.passed>excluded.passed'
)


def update_sessions(c, status, trial, day, report=False):
    """Own one serialized transaction, including reads, evidence and score writes.

    A caller-owned transaction is rejected without committing or rolling it back.
    Lock acquisition failure propagates; it must never produce a healthy score.
    """
    if c.in_transaction:
        raise ValueError('Scoring requires a connection without an active transaction')
    c.execute('BEGIN IMMEDIATE')
    try:
        result = _update_sessions_locked(c, status, trial, day, report)
        c.commit()
        return result
    except BaseException:
        c.rollback()
        raise


def _update_sessions_locked(c, status, trial, day, report):
    c.execute('CREATE TABLE IF NOT EXISTS session_incidents(day TEXT, source TEXT, evidence TEXT, PRIMARY KEY(day,source))')
    c.execute('CREATE TABLE IF NOT EXISTS session_adjudications('
              'id INTEGER PRIMARY KEY,day TEXT,passed INTEGER,evidence TEXT,checked REAL,recorded REAL,reason TEXT)')
    if day < trial['start_date']:
        return 0
    # Routine health checks adjudicate only their UTC day. Historical repair is
    # a separate reviewed operation, never an implicit backfill on inspection.
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    for key, sent in c.execute("SELECT key,sent FROM notifications WHERE key LIKE ?", ('fault:' + day + ':%',)).fetchall():
        incident_day = key.split(':', 2)[1]
        if incident_day == day:
            c.execute('INSERT OR IGNORE INTO session_incidents VALUES (?,?,?)',
                      (incident_day, key, json.dumps({'notification': key, 'sent': sent})))
    for rid, job, started in c.execute("SELECT id,job,started FROM runs WHERE status='failed' AND started>=? AND started<?",
                                      (start.timestamp(), (start+timedelta(days=1)).timestamp())).fetchall():
        # Only new observation-lane failures are advisory. Existing incidents and
        # all pre-policy failures retain their original trial consequences.
        policy = status.get('research_policy', {})
        if job in ('global-events', 'research-hub', 'investor-research') and not policy_known(policy):
            # Unknown classification cannot create a pass or turn research into
            # a retrospective operational failure. Record the uncertainty now.
            issue = 'Research failure classification unavailable: missing or invalid policy'
            if issue not in status.setdefault('issues', []): status['issues'].append(issue)
            status['ready_for_entries'] = False
            continue
        if research_run_after_policy(job, started, policy): continue
        incident_day = datetime.fromtimestamp(started, timezone.utc).date().isoformat()
        if incident_day == day:
            c.execute('INSERT OR IGNORE INTO session_incidents VALUES (?,?,?)',
                      (incident_day, 'run:' + str(rid), json.dumps({'job': job, 'run_id': rid})))
    if day >= trial['start_date'] and status.get('issues'):
        c.execute('INSERT OR IGNORE INTO session_incidents VALUES (?,?,?)',
                  (day, 'health:' + '|'.join(status['issues']), json.dumps(status, default=str)))
    sources = [row[0] for row in c.execute('SELECT source FROM session_incidents WHERE day=? ORDER BY source', (day,))]
    prior = c.execute('SELECT passed,evidence,checked FROM sessions WHERE day=?', (day,)).fetchone()
    # A same-day failure can downgrade a prior pass immediately. A recovery
    # cannot upgrade it. Repeated checks do not overwrite original evidence.
    passed = int(not status.get('issues') and not sources)
    if (prior and prior[0] and not passed) or (prior is None and report and status.get('trading_day')):
        checked = time.time()
        evidence = json.dumps(dict(status, trial_incident_sources=sources), default=str)
        changed = c.execute(SESSION_UPSERT, (day, passed, evidence, checked)).rowcount
        if changed:
            if prior:
                c.execute('INSERT INTO session_adjudications(day,passed,evidence,checked,recorded,reason) VALUES (?,?,?,?,?,?)',
                          (day, prior[0], prior[1], prior[2], checked, 'preserved_before_same_day_failure'))
            c.execute('INSERT INTO session_adjudications(day,passed,evidence,checked,recorded,reason) VALUES (?,?,?,?,?,?)',
                      (day, passed, evidence, checked, checked, 'same_day_failure' if prior else 'initial_report'))
    return c.execute('SELECT COUNT(*) FROM session_incidents WHERE day=?', (day,)).fetchone()[0]


def unresolved_job_failure(c, name):
    # Starting a retry proves nothing about recovery; require completed success.
    row = c.execute("SELECT status FROM runs WHERE job=? AND status IN ('ok','failed') ORDER BY id DESC LIMIT 1", (name,)).fetchone()
    return bool(row and row[0] == 'failed')
