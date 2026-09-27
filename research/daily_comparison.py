"""Prospective, unlevered virtual equity portfolios. No broker order capability."""
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

POLICY = {'version': 'daily-comparison-20260927-v1', 'symbols': ['SPY', 'QQQ'],
          'rules': ['buy_hold', 'trend200', 'breakout20_hold10'],
          'cost_per_side': .0015, 'execution_enabled': False,
          'signal': 'completed daily close; next scheduled regular-session open',
          'benchmark': 'buy_hold in the same symbol, started at the same prospective open',
          'allocation': 'one unlevered virtual portfolio per symbol/rule; normalized capital',
          'data': 'adjusted daily bars; no idle-cash interest; modeled fills, not broker P&L'}


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def calendar_rows(calendar):
    result = []
    for s in calendar:
        result.append({'date': s['date'], **{k: datetime.fromisoformat(s['date']+'T'+s[k]).replace(
            tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc) for k in ('open', 'close')}})
    return sorted(result, key=lambda s: s['date'])


def target(rule, closes, state):
    trend = closes[-1] > sum(closes[-200:]) / 200
    if rule == 'buy_hold': return 1
    if rule == 'trend200': return int(trend)
    if state['invested']: return int(state['held'] < 10)
    return int(trend and closes[-1] > max(closes[-21:-1]))


def update(folder, bars_by_symbol, calendar, now):
    """Only frozen targets can execute; never synthesize past daily decisions."""
    folder.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(folder/'comparison.sqlite')
    c.execute('CREATE TABLE IF NOT EXISTS policy(version TEXT PRIMARY KEY,payload TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS portfolios(key TEXT PRIMARY KEY,payload TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS observations(key TEXT,day TEXT,payload TEXT,PRIMARY KEY(key,day))')
    encoded = json.dumps(POLICY, sort_keys=True)
    old = c.execute('SELECT payload FROM policy WHERE version=?', (POLICY['version'],)).fetchone()
    if old and old[0] != encoded: raise RuntimeError('Frozen daily policy changed; new version required')
    c.execute('INSERT OR IGNORE INTO policy VALUES (?,?)', (POLICY['version'], encoded))
    sessions = calendar_rows(calendar)
    complete = [s for s in sessions if s['close'] + timedelta(minutes=15) <= now]
    if not complete: raise ValueError('No completed session calendar')
    latest = complete[-1]['date']; errors = []; portfolios = []
    with c:
        for symbol in POLICY['symbols']:
            bars = {b['t'][:10]: b for b in bars_by_symbol.get(symbol, [])}
            if latest not in bars:
                errors.append(symbol+': latest completed bar missing'); continue
            for rule in POLICY['rules']:
                key = POLICY['version']+':'+symbol+':'+rule
                row = c.execute('SELECT payload FROM portfolios WHERE key=?', (key,)).fetchone()
                state = json.loads(row[0]) if row else dict(day=latest, equity=1., invested=0,
                    held=0, peak=1., drawdown=0., completed_trades=0, sessions=0, pending=None,
                    first_observed_at=now.isoformat(), entry_equity=None)
                blocked = False
                for session in complete:
                    day = session['date']
                    if day <= state['day']: continue
                    previous = bars.get(state['day']); bar = bars.get(day)
                    if not previous or not bar:
                        errors.append(symbol+': missing bar '+day); blocked = True; break
                    values = [float(previous['c']), float(bar['o']), float(bar['c'])]
                    if not all(math.isfinite(v) and v > 0 for v in values):
                        raise ValueError('Invalid daily prices')
                    prev, opening, closing = values
                    # Relative adjusted-price returns avoid mixing share quantities
                    # from different corporate-action adjustment vintages.
                    if state['invested']: state['equity'] *= opening/prev
                    pending = state['pending']
                    if pending and pending['day'] == day:
                        if stamp(pending['frozen_at']) >= session['open']:
                            raise ValueError('Target was not frozen before entry open')
                        desired = pending['target']
                        if desired != state['invested']:
                            state['equity'] *= 1-POLICY['cost_per_side']
                            if desired:
                                state['held'] = 0; state['entry_equity'] = state['equity']
                            else: state['completed_trades'] += 1
                            state['invested'] = desired
                        state['pending'] = None
                    elif pending and pending['day'] < day:
                        raise ValueError('Missing target session in calendar; refusing inferred fill')
                    if state['invested']:
                        state['equity'] *= closing/opening; state['held'] += 1
                    state['day'] = day; state['sessions'] += 1
                    state['peak'] = max(state['peak'], state['equity'])
                    state['drawdown'] = max(state['drawdown'], 1-state['equity']/state['peak'])
                    # A gap in observation never creates retroactive signals.
                    c.execute('INSERT INTO observations VALUES (?,?,?)', (key, day,
                        json.dumps(dict(state, observed_at=now.isoformat()))))
                if not blocked and state['day'] == latest and state['pending'] is None:
                    history = [float(bars[d]['c']) for d in sorted(bars) if d <= latest]
                    future = [s for s in sessions if s['date'] > latest]
                    if len(history) < 200: errors.append(symbol+': insufficient history')
                    elif not future or future[0]['open'] <= now:
                        errors.append(symbol+': next open missed or calendar unavailable')
                    else:
                        state['pending'] = dict(day=future[0]['date'], target=target(rule, history, state),
                            frozen_at=now.isoformat(), signal_session=latest)
                c.execute('INSERT INTO portfolios VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
                          (key, json.dumps(state)))
                portfolios.append(dict(symbol=symbol, rule=rule, **state))
    c.close()
    result = dict(version=POLICY['version'], policy_hash=hashlib.sha256(encoded.encode()).hexdigest(),
        observed_at=now.isoformat(), status='DEGRADED' if errors else 'OK', errors=sorted(set(errors)),
        execution_enabled=False, portfolios=portfolios,
        note='Prospective modeled equity returns after 30bps round-trip costs; open exposure is marked, not liquidated. No strategy promotion.')
    temp = folder/'latest.tmp'; temp.write_text(json.dumps(result, indent=2)); temp.replace(folder/'latest.json')
    return result


def main():
    """Read-only bootstrap/recovery using the same frozen policy as collection."""
    from pathlib import Path
    from dotenv import dotenv_values
    from universe import paged
    import requests
    root=Path(__file__).resolve().parents[1]; v=dotenv_values(root/'nwt_agents/.env')
    if v['NWT_ALPACA_BASE_URL'].rstrip('/')!='https://paper-api.alpaca.markets':
        raise RuntimeError('Paper-only research')
    now=datetime.now(timezone.utc)
    headers={'APCA-API-KEY-ID':v['NWT_ALPACA_KEY_ID'],'APCA-API-SECRET-KEY':v['NWT_ALPACA_SECRET_KEY']}
    def get(url,params):
        response=requests.get(url,headers=headers,params=params,timeout=30)
        response.raise_for_status();return response.json()
    start=(now-timedelta(days=400)).date().isoformat()
    data=paged(get,'https://data.alpaca.markets/v2/stocks/bars',dict(symbols=','.join(POLICY['symbols']),
        timeframe='1Day',start=start,end=now.isoformat(),adjustment='all',feed='sip',limit=10000),'bars')
    calendar=get('https://paper-api.alpaca.markets/v2/calendar',dict(start=start,end=(now+timedelta(days=14)).date().isoformat()))
    result=update(root/'research/daily-comparison',data['bars'],calendar,now)
    print(json.dumps(result))
    if result['status']!='OK':raise RuntimeError('Daily comparison degraded')


if __name__=='__main__':main()
