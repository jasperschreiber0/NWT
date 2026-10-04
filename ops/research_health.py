"""Explicit isolation for the observation-only event lane; unknown jobs stay critical."""
from datetime import datetime, timezone

VERSION = 'event-research-isolation-v1'


def policy_active(policy, now):
    try:
        return (policy['version'] == VERSION and
                datetime.fromisoformat(policy['effective_at']) <= now)
    except (KeyError, TypeError, ValueError):
        return False


def observation_job(name, config):
    commands={'global-events':'research/event_pipeline.py','research-hub':'research/research_hub.py'}
    return (name in commands and config.get('cwd') == '.' and
            config.get('args') == [commands[name]])


def persistent_warnings(previous, warnings, now, delay=1800):
    """Track each warning separately so changing failures cannot reset another's age."""
    current = {w: previous.get(w, now.timestamp()) for w in warnings}
    due = sorted(w for w, since in current.items() if now.timestamp() - since >= delay)
    return current, due


def research_run_after_policy(job, started, policy):
    try:
        return (job in ('global-events','research-hub') and policy['version'] == VERSION and
                started >= datetime.fromisoformat(policy['effective_at']).timestamp())
    except (KeyError, TypeError, ValueError):
        return False
