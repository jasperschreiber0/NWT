import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from universe import paged, SYMBOLS, OPTION_UNDERLYINGS
from pattern_lab import evaluate, ingest, initialize


def bars(n=31, start=None):
    start = start or datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
    return [dict(t=(start+timedelta(minutes=i)).strftime('%Y-%m-%dT%H:%M:%SZ'), o=100, h=101, l=99, c=100, v=100) for i in range(n)]


def test_universe_and_pagination_include_later_symbols():
    assert len(SYMBOLS) == len(set(SYMBOLS)) == 40
    assert len(OPTION_UNDERLYINGS) == 12
    calls = []
    def get(url, params):
        calls.append(params)
        return {'bars': {'QQQ': [2]}, 'next_page_token': None} if params.get('page_token') else {'bars': {'AAPL': [1]}, 'next_page_token': 'next'}
    assert paged(get, 'unused', {'feed': 'sip'}, 'bars')['bars'] == {'AAPL': [1], 'QQQ': [2]}
    assert calls[1]['feed'] == 'sip'


def test_partial_or_looping_data_rejected():
    with pytest.raises(RuntimeError): paged(lambda *_: {'error': 'denied'}, '', {}, 'bars')
    with pytest.raises(RuntimeError): paged(lambda *_: {'bars': {}, 'next_page_token': 'same'}, '', {}, 'bars')


def test_patterns_require_contiguous_history_and_aligned_benchmark():
    data = bars(); data[-1].update(c=102,h=102,v=300)
    result = evaluate(data, [])
    assert result['volume_breakout_base'] == 1
    assert result['relative_strength_base'] is None
    assert len(result) == 8
    assert evaluate(data[:10]+data[11:], []) == {}


def test_seed_history_does_not_create_backdated_predictions():
    c=sqlite3.connect(':memory:')
    ingest(c, {'SPY': bars()}, datetime(2026,9,15,18,tzinfo=timezone.utc))
    assert c.execute('SELECT COUNT(*) FROM bars').fetchone()[0] == 31
    assert c.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == 0


def test_prospective_entry_no_lookahead_cost_stress_and_idempotency():
    c=sqlite3.connect(':memory:'); initialize(c)
    c.execute('INSERT INTO observations VALUES (?,?,?,?,?)', ('SPY','2026-09-15T14:30:00Z','test',1,'2026-09-15T14:31:10+00:00'))
    data=bars(31, datetime(2026,9,15,14,32,tzinfo=timezone.utc)); data[-1]['o']=101
    now=datetime(2026,9,15,15,4,tzinfo=timezone.utc)
    ingest(c, {'SPY':data}, now); ingest(c, {'SPY':data}, now)
    rows=c.execute('SELECT entry_t,exit_t,gross,net_10bps,net_30bps FROM outcomes').fetchall()
    assert len(rows)==1
    assert rows[0][:2]==('2026-09-15T14:32:00Z','2026-09-15T15:02:00Z')
    assert rows[0][2:]==pytest.approx((.01,.009,.007))


def test_missing_exit_bar_does_not_generate_an_outcome():
    c=sqlite3.connect(':memory:'); initialize(c)
    c.execute('INSERT INTO observations VALUES (?,?,?,?,?)', ('SPY','2026-09-15T14:30:00Z','test',1,'2026-09-15T14:31:10+00:00'))
    ingest(c, {'SPY':bars(30,datetime(2026,9,15,14,32,tzinfo=timezone.utc))}, datetime(2026,9,15,15,5,tzinfo=timezone.utc))
    assert c.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0]==0
