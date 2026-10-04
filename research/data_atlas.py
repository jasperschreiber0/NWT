"""Auditable accessible-data inventory. Public research returns are not fill prices."""
import csv, hashlib, io, json, math, re, statistics, zipfile
from datetime import datetime, timezone
from pathlib import Path
from universe import SYMBOLS, paged

PUBLIC = {
    'ff3': 'F-F_Research_Data_Factors_daily_CSV.zip',
    'ff5': 'F-F_Research_Data_5_Factors_2x3_daily_CSV.zip',
    'momentum': 'F-F_Momentum_Factor_daily_CSV.zip',
    'reversal': 'F-F_ST_Reversal_Factor_daily_CSV.zip',
}
PUBLIC_BASE = 'https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/'


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp'); tmp.write_text(json.dumps(value, indent=2, allow_nan=False)); tmp.replace(path)


def factors(blob):
    """Read exactly the daily table; convert published percent to return fractions."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        members = [i for i in z.infolist() if i.filename.lower().endswith('.csv')]
        if len(members) != 1 or members[0].file_size > 20_000_000:
            raise ValueError('Unexpected public archive')
        text = z.read(members[0]).decode('utf-8-sig')
    columns = None; rows = []; started = False
    for fields in csv.reader(text.splitlines()):
        if not fields: continue
        fields = [x.strip() for x in fields]
        if re.fullmatch(r'\d{8}', fields[0]):
            if not columns or len(fields) != len(columns)+1: raise ValueError('Invalid daily table')
            day = datetime.strptime(fields[0], '%Y%m%d').date().isoformat()
            values = [float(v) for v in fields[1:]]
            if not all(math.isfinite(v) for v in values): raise ValueError('Nonfinite factor')
            # Missing values are not returns; reject incomplete rows, retain count in manifest.
            if any(v in (-99.99, -999.) for v in values): continue
            rows.append(dict(date=day, **dict(zip(columns, [v/100 for v in values])))); started = True
        elif started: break
        elif fields[0] == '' and len(fields) > 1: columns = fields[1:]
    dates = [r['date'] for r in rows]
    if not rows or dates != sorted(set(dates)): raise ValueError('Missing or unordered daily table')
    return rows


def factor_summary(rows):
    columns = [c for c in rows[0] if c != 'date']
    decades = {}
    for r in rows: decades.setdefault(str(int(r['date'][:4])//10*10), []).append(r)
    return dict(first=rows[0]['date'], last=rows[-1]['date'], rows=len(rows),
        decades={d: {c: dict(observations=len(group), daily_mean=statistics.mean(r[c] for r in group))
                     for c in columns} for d, group in decades.items()},
        interpretation='Revised research portfolio returns, not executable prices or point-in-time fundamentals. '
                       'Factor means motivate hypotheses; they do not prove an implementable edge.')


def collect(out, get, now, latest, public_get=None):
    out.mkdir(parents=True, exist_ok=True); issues = []
    cache = out/('stocks-'+latest+'.json')
    if cache.exists(): data = json.loads(cache.read_text())
    else:
        data = paged(get, 'https://data.alpaca.markets/v2/stocks/bars',
            dict(symbols=','.join(SYMBOLS), timeframe='1Day', start='1990-01-01', end=now.isoformat(),
                 feed='sip', adjustment='all', limit=10000), 'bars', 50)['bars']
        data = {s: [b for b in data.get(s, []) if b['t'][:10] <= latest] for s in SYMBOLS}
        atomic(cache, data)
    coverage = {}
    for symbol in SYMBOLS:
        rows = data.get(symbol, [])
        coverage[symbol] = dict(rows=len(rows), first=rows[0]['t'][:10] if rows else None,
            last=rows[-1]['t'][:10] if rows else None, current=bool(rows and rows[-1]['t'][:10] == latest))
        if not coverage[symbol]['current']: issues.append('Latest stock session unavailable: '+symbol)
    # No brokerage headers are ever sent to a public-data provider.
    if public_get is None:
        import requests
        public_get = requests.get
    public = {}
    month = now.strftime('%Y-%m')
    for name, filename in PUBLIC.items():
        try:
            dest = out/(month+'-'+filename)
            if not dest.exists():
                response = public_get(PUBLIC_BASE+filename, timeout=30); response.raise_for_status()
                if len(response.content) > 10_000_000: raise ValueError('Archive too large')
                factors(response.content)  # Validate before publishing cache.
                temp = dest.with_suffix('.tmp'); temp.write_bytes(response.content); temp.replace(dest)
            raw = dest.read_bytes(); rows = factors(raw)
            public[name] = dict(status='OK', url=PUBLIC_BASE+filename, sha256=hashlib.sha256(raw).hexdigest(),
                archive=dest.name, fetched_at=datetime.fromtimestamp(dest.stat().st_mtime, timezone.utc).isoformat(),
                **factor_summary(rows))
        except Exception as e:
            public[name] = dict(status='UNAVAILABLE', error_type=type(e).__name__)
            issues.append('Public research source unavailable: '+name)
    probes = {}
    for route in ['bars', 'trades']:
        try:
            # Historical bars/trades do NOT accept the latest-quotes feed parameter.
            params = dict(symbols='SPY240315P00500000', start='2024-03-01', end='2024-03-02', limit=10000)
            if route == 'bars': params['timeframe'] = '1Day'
            result = paged(get, 'https://data.alpaca.markets/v1beta1/options/'+route, params, route, 30)[route]
            sample = dict(parameters=params, data=result)
            atomic(out/('options-probe-'+route+'.json'), sample)
            probes[route] = dict(status='ACCESSIBLE', rows=sum(len(v) for v in result.values()),
                sha256=hashlib.sha256(json.dumps(sample,sort_keys=True).encode()).hexdigest(),
                coverage='One expired contract/session probe only; not a complete options history',
                fill_evidence=False)
        except Exception as e:
            probes[route] = dict(status='UNAVAILABLE', error_type=type(e).__name__)
    report = dict(observed_at=now.isoformat(), status='DEGRADED' if issues else 'OK', stocks=coverage,
        stock_sha256=hashlib.sha256(cache.read_bytes()).hexdigest(), public=public, options=probes,
        issues=issues, purchased_data=False,
        limitations=['Surviving 41-symbol universe, not all securities or delisted stocks',
            'Trade prints and bars cannot substitute for contemporaneous option bid/ask spreads',
            'Public factor history is revised and has publication lag; hypothesis context only'])
    atomic(out/'latest.json', report)
    return report, data
