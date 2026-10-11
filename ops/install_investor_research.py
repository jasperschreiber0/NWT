"""Idempotent observation-only investor schedule; existing trading jobs are preserved."""
import json,subprocess
from paper_io import STATE,atomic
LINE='1-56/5 * * * * /usr/bin/python3 /home/northworld/trading/ops/run_job.py investor-research >> /var/log/nwt/ops-runner.log 2>&1 # nwt-investor-research'
def schedule(text):
    return '\n'.join([r for r in text.splitlines() if '# nwt-investor-research' not in r]+[LINE])+'\n'
def configuration(jobs):
    jobs['investor-research']=dict(cwd='.',args=['research/investor_pipeline.py'],env=['nwt_agents/.env'],
        retry_safe=True,timeout=420,market_interval=1500)
    return jobs
def main():
    atomic(STATE/'jobs.json',configuration(json.loads((STATE/'jobs.json').read_text())))
    old=subprocess.check_output(['crontab','-l'],text=True)
    subprocess.run(['crontab','-'],input=schedule(old),text=True,check=True)
if __name__=='__main__':main()
