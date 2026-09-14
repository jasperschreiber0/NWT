"""Broad paper-account research feed. Broker access is GET-only."""
import gzip
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from universe import SYMBOLS, OPTION_UNDERLYINGS, VERSION, paged
from pattern_lab import ingest, summary, timestamp

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'research/discovery-evidence'


def atomic(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2))
    os.replace(tmp, path)


def archive(kind, payload, now):
    folder = OUT / now.strftime('%Y-%m-%d')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (now.strftime('%H%M%S%f') + '-' + kind + '.json.gz')
    with gzip.open(path, 'xt') as f:
        json.dump({'observed_at': now.isoformat(), 'version': VERSION, 'data': payload}, f)


def main():
    import requests
    from dotenv import dotenv_values
    v = dotenv_values(ROOT / 'nwt_agents/.env')
    base = v['NWT_ALPACA_BASE_URL'].rstrip('/')
    if base != 'https://paper-api.alpaca.markets': raise RuntimeError('Paper account required')
    headers = {'APCA-API-KEY-ID': v['NWT_ALPACA_KEY_ID'], 'APCA-API-SECRET-KEY': v['NWT_ALPACA_SECRET_KEY']}
    def get(url, params=None):
        response = requests.get(url, headers=headers, params=params, timeout=20)
        if not response.ok: raise RuntimeError('Market request HTTP ' + str(response.status_code))
        return response.json()
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    clock = get(base + '/v2/clock')
    snapshots = get('https://data.alpaca.markets/v2/stocks/snapshots', {'symbols': ','.join(SYMBOLS), 'feed': 'sip'})
    archive('stocks', snapshots, now)
    missing = sorted(set(SYMBOLS) - set(snapshots))
    stale = [s for s in SYMBOLS if s in snapshots and (not snapshots[s].get('latestQuote', {}).get('t') or
             (now-timestamp(snapshots[s]['latestQuote']['t'])).total_seconds() > 180)]
    c = sqlite3.connect(OUT / 'patterns.sqlite', timeout=20)
    from pattern_lab import initialize
    initialize(c)
    statefile = OUT / 'progress.json'
    progress = json.loads(statefile.read_text()) if statefile.exists() else {}
    # Re-read overlap for outages and the initial seed. A failed page never moves
    # this cursor. Bootstrap history is context, not backdated predictions.
    start = max(now-timedelta(days=4), timestamp(progress['bars_through'])-timedelta(minutes=40)) if progress.get('bars_through') else now-timedelta(days=4)
    bars = paged(get, 'https://data.alpaca.markets/v2/stocks/bars',
        {'symbols': ','.join(SYMBOLS), 'timeframe': '1Min', 'start': start.isoformat(),
         'end': (now-timedelta(minutes=1)).isoformat(), 'feed': 'sip', 'adjustment': 'raw', 'limit': 10000}, 'bars')
    archive('bars', bars, datetime.now(timezone.utc))
    ingest(c, bars['bars'], datetime.now(timezone.utc))
    progress['bars_through'] = now.isoformat()
    # Full near-money chain every five minutes, independent of existing tickets.
    chain_due = not progress.get('chains_at') or (now-timestamp(progress['chains_at'])).total_seconds() >= 290
    chains = progress.get('chain_counts', {})
    if chain_due:
        chains = {}
        for symbol in OPTION_UNDERLYINGS:
            snap = snapshots.get(symbol, {})
            price = float((snap.get('latestTrade') or {}).get('p') or (snap.get('dailyBar') or {}).get('c') or 0)
            if price <= 0: raise RuntimeError('Missing underlying price for ' + symbol)
            params = {'feed': 'opra', 'limit': 1000, 'strike_price_gte': round(price*.9, 2),
                      'strike_price_lte': round(price*1.1, 2),
                      'expiration_date_gte': (now+timedelta(days=7)).date().isoformat(),
                      'expiration_date_lte': (now+timedelta(days=60)).date().isoformat()}
            chain = paged(get, 'https://data.alpaca.markets/v1beta1/options/snapshots/' + symbol, params, 'snapshots', 10)
            archive('options-' + symbol, {'parameters': params, **chain}, datetime.now(timezone.utc))
            chains[symbol] = len(chain['snapshots'])
        progress.update(chains_at=now.isoformat(), chain_counts=chains)
    atomic(statefile, progress)
    result = dict(observed_at=datetime.now(timezone.utc).isoformat(), version=VERSION,
                  paper_starting_capital=100000, prospective_live_capital=5000,
                  live_review_date='2026-12-31', live_execution_enabled=False,
                  market_open=clock['is_open'], symbols_requested=len(SYMBOLS), symbols_received=len(snapshots),
                  missing_symbols=missing, stale_quote_symbols=stale, stock_feed='sip', option_feed='opra',
                  option_underlyings=len(chains), option_contracts=sum(chains.values()),
                  chains_observed_at=progress.get('chains_at'), patterns=summary(c))
    c.close()
    # Thinly traded watchlist names may have old quotes; retain that fact without
    # confusing an inactive instrument with a failed market feed.
    result['status'] = 'DEGRADED' if set(missing) & {'SPY', 'QQQ'} or (clock['is_open'] and set(stale) & {'SPY', 'QQQ'}) else 'OK'
    atomic(OUT / 'latest.json', result)
    print(json.dumps(result))
    if result['status'] != 'OK': raise RuntimeError('Discovery market coverage degraded')


if __name__ == '__main__': main()
