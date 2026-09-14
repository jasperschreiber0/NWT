"""Server health, verified notifications and evidence-based paper trial accounting."""
import argparse
import html
import json
import os
import sys
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from dotenv import load_dotenv
from run_job import ROOT, STATE, db
sys.path.insert(0, str(ROOT / 'execution'))
from reliability import reconcile_quantities, separate_legacy_expiries


def atomic(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, default=str, indent=2)); os.replace(tmp, path)


def notify(c, key, text):
    if c.execute('SELECT 1 FROM notifications WHERE key=?', (key,)).fetchone(): return True
    token, chat = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat: return False
    try:
        r = requests.post('https://api.telegram.org/bot' + token + '/sendMessage',
                          json={'chat_id': chat, 'text': text[:3900]}, timeout=15)
        r.raise_for_status(); body = r.json()
        if not body.get('ok'): return False
        c.execute('INSERT OR IGNORE INTO notifications VALUES (?,?,?)',
                  (key, time.time(), str(body['result']['message_id'])))
        c.commit(); return True
    except Exception:
        return False  # Never log an exception containing the bot token URL.


def trial_streak(rows):
    streak = 0
    for row in rows:
        streak = streak + 1 if row[1] else 0
    return streak


def inspect(now=None):
    now = now or datetime.now(timezone.utc)
    load_dotenv(ROOT / 'nwt_agents/.env')
    base = os.environ['NWT_ALPACA_BASE_URL'].rstrip('/')
    if base != 'https://paper-api.alpaca.markets': raise RuntimeError('Paper endpoint required')
    h = {'APCA-API-KEY-ID': os.environ['NWT_ALPACA_KEY_ID'], 'APCA-API-SECRET-KEY': os.environ['NWT_ALPACA_SECRET_KEY']}
    def get(path):
        r = requests.get(base + '/v2/' + path, headers=h, timeout=15)
        r.raise_for_status(); return r.json()
    issues = []
    if subprocess.run(['systemctl','is-active','--quiet','cron']).returncode:
        subprocess.run(['systemctl','start','cron'],check=True,timeout=20)
    calendar = get('calendar?start=' + now.date().isoformat() + '&end=' + now.date().isoformat())
    trading_day = bool(calendar)
    clock = get('clock')
    # PM2 already restarts the web process; independently verify authenticated health.
    response = requests.get('http://127.0.0.1:8080/api/health',
                            headers={'Authorization':'Bearer '+os.environ['NWT_DASHBOARD_TOKEN']},timeout=10)
    if response.status_code != 200 or response.json().get('status') != 'ok':
        issues.append('Dashboard health failed')
    broker = get('positions')
    orders = get('orders?status=open&limit=500')
    for order in orders:
        stamp = datetime.fromisoformat(order['created_at'].replace('Z', '+00:00'))
        if (now - stamp).total_seconds() > 900:
            issues.append('Unresolved broker order ' + order['id'])
    conn = psycopg2.connect(os.environ['NWT_DB_DSN'], connect_timeout=10)
    conn.set_session(readonly=True, autocommit=True)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM nwt_portfolio_ledger WHERE status IN ('open','suspect')")
        ledger = list(cur.fetchall())
        ledger, legacy = separate_legacy_expiries(broker, ledger, '2026-09-15')
        mismatch = reconcile_quantities(broker, ledger)
        if mismatch: issues.append('Broker/ledger quantity or direction mismatch')
        if any(p['status'] == 'suspect' for p in ledger): issues.append('Unresolved suspect ledger position')
        cur.execute('SELECT flag,value,reason FROM nwt_system_flags')
        flags = {x['flag']: dict(x) for x in cur.fetchall()}
        if flags.get('no_trade_mode', {}).get('value'): issues.append('Trading hold: ' + str(flags['no_trade_mode']['reason']))
        cur.execute("SELECT COUNT(*) n FROM nwt_decision_inputs d WHERE d.symbol IS NOT NULL AND d.symbol<>'' "
                    "AND NOT EXISTS (SELECT 1 FROM nwt_opportunity_outcomes o WHERE o.source_decision_id=d.id AND o.lane='RAW_SHADOW')")
        missing = cur.fetchone()['n']
        if missing: issues.append('Unrecorded opportunity decisions: ' + str(missing))
        cur.execute("SELECT type,COUNT(*) n FROM nwt_tickets WHERE created_at >= %s GROUP BY type", (now.date(),))
        tickets = {x['type']: x['n'] for x in cur.fetchall()}
        cur.execute("SELECT strategy_id,COUNT(*) n FROM nwt_portfolio_ledger WHERE entry_time >= %s GROUP BY strategy_id", (now.date(),))
        entries = list(cur.fetchall())
        cur.execute("SELECT COUNT(*) n,COALESCE(SUM(pnl_adjusted),0) net FROM nwt_trade_outcomes WHERE exit_time >= %s", (now.date(),))
        outcome = dict(cur.fetchone())
        cur.execute("SELECT decision,COUNT(*) n FROM nwt_ticket_decisions WHERE created_at >= %s GROUP BY decision", (now.date(),))
        decisions = list(cur.fetchall())
        cur.execute("SELECT COUNT(*) n FROM nwt_trade_outcomes")
        learning_n = cur.fetchone()['n']
        cur.execute("SELECT l.position_id FROM nwt_portfolio_ledger l JOIN nwt_tickets t ON t.ticket_id=l.ticket_id "
                    "WHERE l.entry_time >= %s AND l.strategy_id <> 'QA_PAPER_LIFECYCLE' "
                    "AND (t.created_at AT TIME ZONE 'America/New_York')::date <> "
                    "(l.entry_time AT TIME ZONE 'America/New_York')::date", (now.date(),))
        stale_executed = len(cur.fetchall())
        if stale_executed: issues.append('Stale-session entries executed: ' + str(stale_executed))
    conn.close()
    research = {}
    for label, relative, max_age in [('research', 'research/evidence/latest.json', 900), ('events', 'research/event-evidence/latest.json', 1800)]:
        try:
            data = json.loads((ROOT / relative).read_text()); research[label] = data
            age = (now - datetime.fromisoformat(data['observed_at'])).total_seconds()
            expected = label == 'events' or (trading_day and 12 <= now.hour <= 22)
            if expected and age > max_age: issues.append(label + ' collection overdue')
            if label == 'research' and expected and not data.get('market_data_available'): issues.append('Market data unavailable')
            if label == 'events' and data.get('status') != 'OK': issues.append('Event sources degraded')
        except Exception: issues.append(label + ' collection evidence missing')
    c = db(); c.row_factory = __import__('sqlite3').Row
    jobs = json.loads((STATE / 'jobs.json').read_text())
    runs = {}
    for name, config in jobs.items():
        row = c.execute('SELECT * FROM runs WHERE job=? ORDER BY id DESC LIMIT 1', (name,)).fetchone()
        runs[name] = dict(row) if row else None
        if row and row['status'] == 'running' and time.time() - row['started'] > config.get('timeout', 240) + 60:
            issues.append(name + ' job interrupted or stuck')
        if row and row['status'] == 'failed': issues.append(name + ' job failed')
        # Explicit deadlines are only enforced on broker-confirmed trading dates.
        deadline = config.get('deadline_utc')
        if trading_day and deadline and now.strftime('%H:%M') >= deadline:
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            if not row or row['started'] < midnight: issues.append(name + ' daily run missing')
        if config.get('market_interval') and clock.get('is_open'):
            if not row or time.time() - row['started'] > config['market_interval']:
                issues.append(name + ' heartbeat overdue')
    c.close()
    return dict(observed_at=now.isoformat(), trading_day=trading_day, market_open=clock.get('is_open'),
                issues=sorted(set(issues)), ready_for_entries=not issues, broker_positions=len(broker),
                open_orders=len(orders), mismatches=mismatch, flags=flags, tickets=tickets,
                entries=entries, outcomes=outcome, decisions=decisions, learning_outcome_rows=learning_n,
                legacy_attribution=[dict(asset=p['asset'],position_id=str(p['position_id']),status='unresolved historical attribution; absent at broker') for p in legacy],
                research=research, jobs=runs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--activation-report', action='store_true')
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    c = db()
    try:
        status = inspect()
    except Exception as exc:
        status = dict(observed_at=datetime.now(timezone.utc).isoformat(), ready_for_entries=False,
                      issues=['Health inspection failed: ' + type(exc).__name__])
    # A bootstrapping grace only suppresses historical job deadlines, never broker checks.
    trial_path = STATE / 'trial.json'
    trial = json.loads(trial_path.read_text()) if trial_path.exists() else {'start_date': '2026-09-15', 'required_sessions': 20}
    day = datetime.now(timezone.utc).date().isoformat()
    if day < trial['start_date']:
        status['pretrial_findings'] = [x for x in status['issues'] if 'daily run missing' in x or 'Stale-session entries executed' in x]
        status['issues'] = [x for x in status['issues'] if x not in status['pretrial_findings']]
        status['ready_for_entries'] = not status['issues']
    if not trial.get('failure_drills_passed'):
        status['issues'].append('Failure-recovery drills not verified')
        status['ready_for_entries'] = False
    if args.report and status.get('trading_day') and day >= trial['start_date']:
        passed = not status['issues']
        # A failed session can never be overwritten into a pass by a later retry.
        c.execute('INSERT INTO sessions VALUES (?,?,?,?) ON CONFLICT(day) DO UPDATE SET '
                  'passed=MIN(sessions.passed,excluded.passed),evidence=excluded.evidence,checked=excluded.checked',
                  (day, int(passed), json.dumps(status, default=str), time.time()))
        c.commit()
    rows = c.execute('SELECT day,passed FROM sessions ORDER BY day').fetchall()
    trial['consecutive_passes'] = trial_streak(rows)
    trial['sessions_recorded'] = len(rows)
    trial['status'] = 'PASSED' if trial['consecutive_passes'] >= trial['required_sessions'] else 'RUNNING'
    status['trial'] = trial
    previous_path = STATE / 'status.json'
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    signature = '|'.join(status['issues'])
    oldsignature = '|'.join(previous.get('issues', []))
    if signature:
        # Deduplicate an unchanged incident for a day; report recovery once.
        delivered = notify(c, 'fault:' + day + ':' + signature,
                           'NWT needs attention. New entries are held.\n' + '\n'.join(status['issues']))
        status['alert_delivery_confirmed'] = delivered
    elif oldsignature:
        status['alert_delivery_confirmed'] = notify(c, 'recovered:' + day + ':' + oldsignature,
                                                   'NWT recovered: operational checks are healthy. Normal paper risk gates apply.')
    if args.report or args.activation_report:
        text = (('NWT supervision activated — ' if args.activation_report else 'NWT daily paper report — ') + day + '\n' +
                ('Healthy' if not status['issues'] else 'Needs attention') + '\n' +
                'Trial: ' + str(trial['consecutive_passes']) + '/20 consecutive sessions\n' +
                'Paper positions: ' + str(status.get('broker_positions', '?')) + '\n' +
                'Decisions: ' + str(status.get('decisions', [])) + '\n' +
                'Recorded outcomes today: ' + str(status.get('outcomes', {})) + '\n' +
                'Learning outcome rows: ' + str(status.get('learning_outcome_rows', '?')) +
                '\nStrategy promotion stays gated by sample size, regimes, shadow evidence and the trial.\n' +
                ('Action: ' + '; '.join(status['issues']) if status['issues'] else 'No action needed.'))
        status['report_delivery_confirmed'] = notify(c, ('activation:' if args.activation_report else 'daily:') + day, text)
        if not status['report_delivery_confirmed']:
            status['issues'].append('Daily report delivery failed'); status['ready_for_entries'] = False
            c.execute('UPDATE sessions SET passed=0 WHERE day=?', (day,)); c.commit()
            trial['consecutive_passes'] = trial_streak(c.execute('SELECT day,passed FROM sessions ORDER BY day').fetchall())
            trial['status'] = 'RUNNING'
    atomic(trial_path, trial); atomic(previous_path, status)
    print(json.dumps({'issues': status['issues'], 'trial': trial}, default=str))
    c.close()


if __name__ == '__main__': main()
