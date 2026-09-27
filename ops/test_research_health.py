from datetime import datetime, timezone, timedelta
import json
from research_health import policy_active, observation_job, persistent_warnings, VERSION
from trial_evidence import update_sessions
import run_job


def test_policy_is_explicit_and_unknown_job_remains_critical():
    now=datetime(2026,9,27,tzinfo=timezone.utc)
    assert not policy_active({},now)
    assert not policy_active({'version':VERSION,'effective_at':(now+timedelta(days=1)).isoformat()},now)
    assert observation_job('global-events',{'cwd':'.','args':['research/event_pipeline.py']})
    assert not observation_job('global-events',{'cwd':'.','args':['execution/engine.py']})
    assert not observation_job('engine',{'cwd':'.','args':['research/event_pipeline.py']})


def test_research_notifications_wait_and_do_not_reset_persistent_failure():
    now=datetime(2026,9,27,tzinfo=timezone.utc)
    state,due=persistent_warnings({},['feed'],now)
    assert not due
    state,due=persistent_warnings(state,['feed','daily'],now+timedelta(minutes=31))
    assert due==['feed']
    state,due=persistent_warnings(state,['daily'],now+timedelta(minutes=32))
    assert not due and 'feed' not in state


def test_trial_preserves_old_failures_but_isolates_new_event_failures(monkeypatch,tmp_path):
    monkeypatch.setattr(run_job,'STATE',tmp_path);c=run_job.db()
    cutoff=datetime(2026,9,27,tzinfo=timezone.utc)
    policy={'version':VERSION,'effective_at':cutoff.isoformat()}
    c.execute("INSERT INTO runs(job,started,status) VALUES ('global-events',?,'failed')",(cutoff.timestamp()-86400,))
    c.execute("INSERT INTO runs(job,started,status) VALUES ('global-events',?,'failed')",(cutoff.timestamp()+86400,))
    status={'issues':[],'trading_day':True,'research_policy':policy,'research_warnings':['Event sources degraded']}
    update_sessions(c,status,{'start_date':'2026-09-15'},'2026-09-28',report=True)
    assert c.execute("SELECT passed FROM sessions WHERE day='2026-09-28'").fetchone()[0]==1
    assert c.execute("SELECT COUNT(*) FROM session_incidents WHERE day='2026-09-26'").fetchone()[0]==1
    c.execute("INSERT INTO runs(job,started,status) VALUES ('engine',?,'failed')",(cutoff.timestamp()+86400,))
    update_sessions(c,status,{'start_date':'2026-09-15'},'2026-09-28',report=True)
    assert c.execute("SELECT passed FROM sessions WHERE day='2026-09-28'").fetchone()[0]==0


def test_report_does_not_call_degraded_research_healthy():
    from report_format import format_report
    result=format_report({'issues':[],'research_warnings':['Event sources degraded']},{},'2026-09-28')
    assert 'research needs attention' in result
    assert 'No action needed' not in result
