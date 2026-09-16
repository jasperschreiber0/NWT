"""Install approved server scheduling; preserves original configuration in release backup."""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path('/home/northworld/trading')


def main():
    Path('/var/lib/nwt-ops').mkdir(parents=True, exist_ok=True)
    cron = subprocess.check_output(['crontab', '-l'], text=True)
    if 'NWT_RECORDED_JOBS_V1' in cron:
        raise RuntimeError('Already installed; update configuration deliberately, do not duplicate schedules')
    jobs = {}
    output = ['# NWT_RECORDED_JOBS_V1']
    safe = {'snapshot_writer', 'cost_agent', 'recon_agent', 'shadow_decision_evaluator'}
    for line in cron.splitlines():
        if line.lstrip().startswith('#') or 'python3 ' not in line:
            output.append(line); continue
        match = re.match(r'((?:\S+\s+){5}).*?cd ([^ &]+).*?python3 ([^>]+?)\s*>>', line)
        if not match: raise RuntimeError('Unrecognized schedule: ' + line)
        schedule, folder, command = match.groups()
        folder = folder.replace('$NWT_DIR', str(ROOT / 'nwt_agents'))
        relative = str(Path(folder).relative_to(ROOT))
        args = command.strip().split()
        name = Path(args[0]).stem + ('_' + args[1].lstrip('-').replace('-', '_') if len(args)>1 else '')
        env = str((Path(relative) / ('../.env' if 'source ../.env' in line else '.env')))
        jobs[name] = dict(cwd=relative, args=args, env=[env], retry_safe=Path(args[0]).stem in safe, timeout=240)
        if relative == 'execution' or name in ('execution_agent', 'risk_agent') or name.startswith('recon_agent'):
            jobs[name]['lock'] = 'broker'
        if name in ('engine', 'execution_agent', 'risk_agent'): jobs[name]['market_interval'] = 900
        minute, hour, *_ = schedule.split()
        if minute.isdigit() and hour.isdigit():
            total = int(hour)*60+int(minute)+20
            jobs[name]['deadline_utc'] = f'{total//60:02}:{total%60:02}'
        output.append(schedule + f'/usr/bin/python3 {ROOT}/ops/run_job.py {name} >> /var/log/nwt/ops-runner.log 2>&1')
    # Replace PM2 scheduled batch jobs, keeping the dashboard under PM2.
    batch = [
      ('master-strategist','30 21 * * 1-5','master','strategist.py','22:00'),
      ('asx-strategist','0 9 * * 1-5','asx','strategist.py','09:20'),
      ('asx-executor','30 9 * * 1-5','asx','executor.py','09:50'),
      ('ukeu-strategist','30 9 * * 1-5','ukeu','strategist.py','09:50'),
      ('ukeu-executor','0 10 * * 1-5','ukeu','executor.py','10:20'),
      ('us-nightly','30 10 * * 1-5','us','nightly.py','11:00'),
      ('us-trader','5 18 * * 1-5','us','workspace-northworldtrading/bot/trade_1400_with_brackets.py','18:30'),
      ('perf-tracker','5 21 * * 1-5','performance','tracker.py','21:25'),
    ]
    for name,schedule,folder,script,deadline in batch:
        jobs[name]=dict(cwd=folder,args=[script],env=[folder+'/.env'],retry_safe=False,timeout=600,deadline_utc=deadline)
        output.append(schedule + f' /usr/bin/python3 {ROOT}/ops/run_job.py {name} >> /var/log/nwt/ops-runner.log 2>&1')
    jobs['research-collector']=dict(cwd='.',args=['research/collect.py'],env=['nwt_agents/.env'],retry_safe=True,timeout=240,market_interval=900)
    jobs['global-events']=dict(cwd='.',args=['research/event_pipeline.py'],env=['nwt_agents/.env'],retry_safe=True,timeout=600)
    jobs['opportunity-replay']=dict(cwd='.',args=['ops/replay_opportunities.py'],env=['nwt_agents/.env'],retry_safe=True,timeout=240,deadline_utc='21:50')
    output.append(f'40 21 * * 1-5 /usr/bin/python3 {ROOT}/ops/run_job.py opportunity-replay >> /var/log/nwt/ops-runner.log 2>&1')
    jobs['learning-review']=dict(cwd='.',args=['ops/learning_review.py'],env=['nwt_agents/.env'],retry_safe=True,timeout=240,deadline_utc='22:10')
    output.append(f'50 21 * * 1-5 /usr/bin/python3 {ROOT}/ops/run_job.py learning-review >> /var/log/nwt/ops-runner.log 2>&1')
    from repair_job_config import repair
    jobs = repair(jobs)
    (Path('/var/lib/nwt-ops')/'jobs.json').write_text(json.dumps(jobs,indent=2))
    # Validate every entry before installing schedules or removing old PM2 jobs.
    for name,j in jobs.items():
        if not (ROOT/j['cwd']/j['args'][0]).is_file(): raise RuntimeError('Missing job: '+name)
    for unit,job in [('nwt-research-collector','research-collector'),('nwt-global-events','global-events')]:
        drop=Path('/etc/systemd/system')/(unit+'.service.d');drop.mkdir(exist_ok=True)
        (drop/'supervision.conf').write_text('[Service]\nExecStart=\nExecStart=/usr/bin/python3 '+str(ROOT)+'/ops/run_job.py '+job+'\nTimeoutStartSec=35min\n')
    for name,*_ in batch:
        subprocess.run(['pm2','delete',name],check=True,stdout=subprocess.DEVNULL)
    subprocess.run(['pm2','save'],check=True,stdout=subprocess.DEVNULL)
    subprocess.run(['crontab','-'],input='\n'.join(output)+'\n',text=True,check=True)
    for name,command,calendar in [('watchdog','', '*-*-* *:0/5:30 UTC'),('daily-report',' --report','Mon..Fri *-*-* 23:20:00 UTC')]:
        stem='nwt-'+name
        Path('/etc/systemd/system/'+stem+'.service').write_text('[Unit]\nDescription=NWT '+name+'\nAfter=network-online.target\n[Service]\nType=oneshot\nWorkingDirectory='+str(ROOT)+'\nExecStart=/usr/bin/python3 '+str(ROOT)+'/ops/watchdog.py'+command+'\nTimeoutStartSec=4min\nUMask=0077\n')
        Path('/etc/systemd/system/'+stem+'.timer').write_text('[Unit]\nDescription=NWT '+name+' schedule\n[Timer]\nOnCalendar='+calendar+'\nPersistent=true\n[Install]\nWantedBy=timers.target\n')
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now','nwt-watchdog.timer','nwt-daily-report.timer'],check=True)
    print('Installed',len(jobs),'recorded server jobs; dashboard PM2 retained')


if __name__=='__main__': main()
