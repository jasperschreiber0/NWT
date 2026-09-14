"""Frozen prospective hypotheses, including no-signal observations and cost stress.

Outcomes are underlying-price research proxies, never simulated broker fills.
No orders, risk changes, or automatic strategy promotion occur here.
"""
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

RULES = ('volume_breakout', 'failed_breakout', 'relative_strength', 'dip_recovery')


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def regular(value):
    t = timestamp(value).astimezone(ZoneInfo('America/New_York'))
    return t.weekday() < 5 and (9, 30) <= (t.hour, t.minute) < (16, 0)


def evaluate(bars, benchmark):
    if len(bars) < 31: return {}
    bars = bars[-31:]
    if any((timestamp(b['t']) - timestamp(a['t'])).total_seconds() != 60 for a, b in zip(bars, bars[1:])):
        return {}
    closes = [float(b['c']) for b in bars]
    if any(not math.isfinite(c) or c <= 0 for c in closes): return {}
    last, previous = bars[-1], bars[-21:-1]
    high, low = max(float(b['h']) for b in previous), min(float(b['l']) for b in previous)
    volume = statistics.mean(float(b['v']) for b in previous)
    ratio = float(last['v']) / volume if volume else 0
    momentum = closes[-1] / closes[-6] - 1
    mean, sd = statistics.mean(closes[-21:-1]), statistics.pstdev(closes[-21:-1])
    z = (closes[-2] - mean) / sd if sd else 0
    # Benchmark timestamps must align exactly; missing data is not zero return.
    ref = {b['t']: b for b in benchmark}
    aligned = bars[-1]['t'] in ref and bars[-16]['t'] in ref
    excess = (closes[-1] / closes[-16] - float(ref[bars[-1]['t']]['c']) / float(ref[bars[-16]['t']]['c'])) if aligned else None
    result = {}
    for level, vol, edge in [('base', 1.5, .002), ('fast', 1.2, .001)]:
        result['volume_breakout_' + level] = 1 if closes[-1] > high and ratio >= vol else -1 if closes[-1] < low and ratio >= vol else 0
        result['failed_breakout_' + level] = -1 if float(last['h']) > high and closes[-1] < high and momentum < -edge else 1 if float(last['l']) < low and closes[-1] > low and momentum > edge else 0
        result['relative_strength_' + level] = None if excess is None else 1 if excess > edge and momentum > 0 else -1 if excess < -edge and momentum < 0 else 0
        result['dip_recovery_' + level] = 1 if z < (-1.5 if level == 'base' else -1) and closes[-1] > closes[-2] else -1 if z > (1.5 if level == 'base' else 1) and closes[-1] < closes[-2] else 0
    return result


def initialize(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS bars(symbol TEXT,t TEXT,payload TEXT,PRIMARY KEY(symbol,t));
    CREATE TABLE IF NOT EXISTS observations(symbol TEXT,t TEXT,rule TEXT,direction INTEGER,observed_at TEXT,PRIMARY KEY(symbol,t,rule));
    CREATE TABLE IF NOT EXISTS outcomes(symbol TEXT,t TEXT,rule TEXT,horizon INTEGER,entry_t TEXT,exit_t TEXT,gross REAL,net_10bps REAL,net_30bps REAL,PRIMARY KEY(symbol,t,rule,horizon));
    CREATE INDEX IF NOT EXISTS bars_time ON bars(t);
    CREATE INDEX IF NOT EXISTS observations_time ON observations(observed_at);
    ''')


def ingest(c, data, now):
    initialize(c)
    for symbol, bars in data.items():
        for bar in bars:
            if timestamp(bar['t']) + timedelta(minutes=1) <= now and regular(bar['t']):
                bar = dict(bar, t=timestamp(bar['t']).strftime('%Y-%m-%dT%H:%M:%SZ'))
                # Preserve the original observed bar; later vendor revisions cannot
                # retroactively improve an experiment's history.
                c.execute('INSERT OR IGNORE INTO bars VALUES (?,?,?)', (symbol, bar['t'], json.dumps(bar)))
    loaded = {}
    for symbol in data:
        loaded[symbol] = [json.loads(r[0]) for r in c.execute('SELECT payload FROM bars WHERE symbol=? ORDER BY t DESC LIMIT 90', (symbol,))][::-1]
    for symbol, bars in loaded.items():
        if not bars or not 60 <= (now - timestamp(bars[-1]['t'])).total_seconds() <= 180: continue
        if timestamp(bars[-1]['t']).minute % 5: continue
        for rule, direction in evaluate(bars, loaded.get('SPY', [])).items():
            if direction is None: continue
            c.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?)',
                      (symbol, bars[-1]['t'], rule, direction, now.isoformat()))
    # Entry is a subsequent minute OPEN, strictly after the prediction was saved.
    # Missing minute bars or overnight gaps invalidate a horizon, rather than
    # silently turning a 30-minute experiment into a multi-day position.
    for symbol, t, rule, direction, observed in c.execute('SELECT * FROM observations WHERE direction<>0 AND observed_at>=?', ((now-timedelta(days=7)).isoformat(),)).fetchall():
        entry = timestamp(observed).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for horizon in (30, 120):
            if c.execute('SELECT 1 FROM outcomes WHERE symbol=? AND t=? AND rule=? AND horizon=?', (symbol,t,rule,horizon)).fetchone(): continue
            end = entry + timedelta(minutes=horizon)
            if end > now or not regular(entry.isoformat()) or not regular(end.isoformat()): continue
            rows = [json.loads(r[0]) for r in c.execute('SELECT payload FROM bars WHERE symbol=? AND t>=? AND t<=? ORDER BY t',
                    (symbol, entry.strftime('%Y-%m-%dT%H:%M:%SZ'), end.strftime('%Y-%m-%dT%H:%M:%SZ')))]
            if len(rows) != horizon+1 or timestamp(rows[0]['t']) != entry or timestamp(rows[-1]['t']) != end: continue
            gross = direction * (float(rows[-1]['o']) / float(rows[0]['o']) - 1)
            c.execute('INSERT OR IGNORE INTO outcomes VALUES (?,?,?,?,?,?,?,?,?)',
                      (symbol,t,rule,horizon,rows[0]['t'],rows[-1]['t'],gross,gross-.001,gross-.003))
    c.commit()


def summary(c):
    return {'observations': c.execute('SELECT COUNT(*) FROM observations').fetchone()[0],
            'triggered_signals': c.execute('SELECT COUNT(*) FROM observations WHERE direction<>0').fetchone()[0],
            'minute_bars': c.execute('SELECT COUNT(*) FROM bars').fetchone()[0],
            'outcomes': c.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0],
            'scoreboard': [dict(zip(('rule','horizon_minutes','samples','mean_after_10bps','mean_after_30bps'), r)) for r in c.execute('SELECT rule,horizon,COUNT(*),AVG(net_10bps),AVG(net_30bps) FROM outcomes GROUP BY rule,horizon')],
            'label': 'PROSPECTIVE_UNDERLYING_PROXY_NOT_BROKER_PNL', 'automatic_promotion': False}
