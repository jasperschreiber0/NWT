"""Bounded, idempotent analytics repair every five minutes, including overnight."""
import subprocess


def updated_cron(cron):
    lines=[]; found=0
    for line in cron.splitlines():
        if not line.lstrip().startswith('#') and '/ops/run_job.py opportunity-replay ' in line:
            line='*/5 * * * * '+line.split(None,5)[5]
            found+=1
        lines.append(line)
    if found!=1:raise RuntimeError('Expected exactly one existing replay job')
    return '\n'.join(lines)+'\n'


if __name__=='__main__':
    existing=subprocess.check_output(['crontab','-l'],text=True)
    subprocess.run(['crontab','-'],input=updated_cron(existing),text=True,check=True)
