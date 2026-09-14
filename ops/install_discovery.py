"""Idempotently install the approved wider paper research schedule."""
import json
import subprocess
from pathlib import Path

ROOT = Path('/home/northworld/trading')


def main():
    p = Path('/var/lib/nwt-ops/jobs.json')
    jobs = json.loads(p.read_text())
    jobs['paper-discovery'] = dict(cwd='.', args=['research/discovery.py'],
        env=['nwt_agents/.env'], retry_safe=True, timeout=240, market_interval=600)
    tmp = p.with_suffix('.tmp'); tmp.write_text(json.dumps(jobs, indent=2)); tmp.replace(p)
    unit = Path('/etc/systemd/system/nwt-paper-discovery.service')
    unit.write_text('[Unit]\nDescription=NWT broad paper research\nAfter=network-online.target\n'
        '[Service]\nType=oneshot\nWorkingDirectory='+str(ROOT)+'\nExecStart=/usr/bin/python3 '+str(ROOT)+
        '/ops/run_job.py paper-discovery\nTimeoutStartSec=15min\nUMask=0077\n')
    unit.with_suffix('.timer').write_text('[Unit]\nDescription=NWT minute research collection\n[Timer]\n'
        'OnCalendar=Mon..Fri *-*-* 12..22:*:10 UTC\nAccuracySec=1s\nPersistent=false\n[Install]\nWantedBy=timers.target\n')
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', 'nwt-paper-discovery.timer'], check=True)
    print('Installed 40-symbol paper research; existing broker capital and risk configuration retained')


if __name__ == '__main__': main()
