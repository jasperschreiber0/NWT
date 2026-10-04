"""Install one recorded, observation-only nightly research job, idempotently."""
import json,subprocess
from pathlib import Path

LINE='15 22 * * 1-5 /usr/bin/python3 /home/northworld/trading/ops/run_job.py research-hub >> /var/log/nwt/ops-runner.log 2>&1 # nwt-research-hub'


def schedule(text):
    return '\n'.join([line for line in text.splitlines() if '# nwt-research-hub' not in line]+[LINE])+'\n'


def main():
    p=Path('/var/lib/nwt-ops/jobs.json');jobs=json.loads(p.read_text())
    jobs['research-hub']=dict(cwd='.',args=['research/research_hub.py'],env=['nwt_agents/.env'],retry_safe=True,timeout=900,deadline_utc='23:00')
    temp=p.with_suffix('.tmp');temp.write_text(json.dumps(jobs,indent=2));temp.replace(p)
    old=subprocess.check_output(['crontab','-l'],text=True)
    subprocess.run(['crontab','-'],input=schedule(old),text=True,check=True)


if __name__=='__main__':main()
