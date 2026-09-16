"""Correct tracker credentials and allow the sequential EDGAR scan to finish."""
import json
from pathlib import Path


def repair(jobs):
    jobs['perf-tracker']['env'] = ['performance/.env', 'nwt_agents/.env']
    jobs['perf-tracker']['retry_safe'] = True
    jobs['scanner']['timeout'] = 1800
    jobs['scanner']['deadline_utc'] = '12:40'
    return jobs


if __name__ == '__main__':
    path = Path('/var/lib/nwt-ops/jobs.json')
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(repair(json.loads(path.read_text())), indent=2))
    tmp.replace(path)
