"""Install existing-runner jobs; preserve all prior schedules and cost uncertainty."""
import json
import subprocess
from paper_io import STATE,atomic

SCHEDULES={'paper-bridge':'2-57/5 * * * *','unified-performance':'35 22 * * 1-5','autonomous-recovery':'7,22,37,52 * * * *'}


def schedule(text):
    retained=[line for line in text.splitlines() if '# nwt-autonomy-' not in line]
    for name,when in SCHEDULES.items():
        retained.append(when+' /usr/bin/python3 /home/northworld/trading/ops/run_job.py '+name+' >> /var/log/nwt/ops-runner.log 2>&1 # nwt-autonomy-'+name)
    return '\n'.join(retained)+'\n'


def configuration(jobs):
    jobs['paper-bridge']=dict(cwd='.',args=['research/paper_bridge.py'],env=['nwt_agents/.env'],retry_safe=True,
        timeout=180,lock='broker',market_interval=900)
    jobs['unified-performance']=dict(cwd='.',args=['ops/unified_performance.py'],env=['nwt_agents/.env'],retry_safe=True,
        timeout=600,deadline_utc='23:00')
    jobs['autonomous-recovery']=dict(cwd='.',args=['ops/autonomous_recovery.py'],env=['nwt_agents/.env'],retry_safe=False,timeout=3300)
    return jobs


def main():
    atomic(STATE/'jobs.json',configuration(json.loads((STATE/'jobs.json').read_text())))
    old=subprocess.check_output(['crontab','-l'],text=True)
    subprocess.run(['crontab','-'],input=schedule(old),text=True,check=True)
    costs=STATE/'operating-costs.json'
    if not costs.exists():atomic(costs,{s:{'monthly_usd':None,'status':'Awaiting confirmed billing amount and currency'} for s in ['alpaca','hetzner','railway','supabase']})


if __name__=='__main__':main()
