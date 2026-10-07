import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import psycopg2
import pytest

MASTER = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('allocator_under_test', MASTER / 'allocator.py')
allocator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(allocator)
BASE = {'us': .36, 'eu': .21, 'aus': .20, 'china': .15}


@pytest.fixture
def allocator_db():
    conn = psycopg2.connect(os.environ['NWT_TEST_DB_DSN'])
    with conn.cursor() as q:
        q.execute('''
          CREATE TEMP TABLE nwt_portfolio_ledger(position_id uuid PRIMARY KEY, bot_source text);
          CREATE TEMP TABLE nwt_trade_outcomes(position_id uuid, pnl numeric, pnl_adjusted numeric,
            regime_at_entry jsonb, closed_at timestamptz);
          CREATE TEMP TABLE nwt_allocator_history(bot text, regime text, baseline_weight numeric,
            dynamic_weight numeric, sample_trades integer, rolling_expectancy numeric, sharpe_proxy numeric, note text);
          INSERT INTO nwt_portfolio_ledger VALUES
            ('11111111-1111-1111-1111-111111111111','EU_BOT'),
            ('22222222-2222-2222-2222-222222222222','AUS_BOT');
          INSERT INTO nwt_trade_outcomes
            SELECT position_id, 999, CASE WHEN bot_source='EU_BOT' THEN -20 ELSE 30 END,
              '{"primary_regime":"risk_on"}'::jsonb, now() FROM nwt_portfolio_ledger CROSS JOIN generate_series(1,20);
        ''')
    conn.commit()
    yield conn
    conn.close()


def test_native_uuid_join_produces_shadow_but_never_changes_effective_weights(allocator_db):
    effective, notes = allocator.compute_dynamic_weights(allocator_db, {'primary_regime':'risk_on'}, BASE)
    assert effective == BASE and effective is not BASE
    assert any('SHADOW_ONLY' in n for n in notes)
    with allocator_db.cursor() as q:
        q.execute('SELECT bot,dynamic_weight,note FROM nwt_allocator_history')
        rows = q.fetchall()
    assert len(rows) == 4
    assert all(float(weight)==BASE[bot] for bot,weight,_ in rows)
    shadow = {bot:json.loads(note) for bot,_,note in rows}
    assert shadow['eu']['candidate_weight'] < BASE['eu']
    assert shadow['aus']['candidate_weight'] > BASE['aus']
    assert all(v['mode']=='shadow_only' for v in shadow.values())


@pytest.mark.parametrize('stage',['score','history'])
def test_database_failure_is_visible_and_connection_recovers(allocator_db, stage):
    with allocator_db.cursor() as q:
        if stage=='score':
            q.execute('ALTER TABLE nwt_trade_outcomes RENAME COLUMN pnl_adjusted TO unavailable')
        else:
            q.execute("ALTER TABLE nwt_allocator_history ADD CHECK(bot='impossible')")
    allocator_db.commit()
    with pytest.raises(RuntimeError, match='Allocator'):
        allocator.compute_dynamic_weights(allocator_db, {}, BASE)
    with allocator_db.cursor() as q:
        q.execute('SELECT 1'); assert q.fetchone()==(1,)
        q.execute('SELECT count(*) FROM nwt_allocator_history'); assert q.fetchone()==(0,)


@pytest.mark.parametrize('failed',[False,True])
def test_strategist_reports_allocator_failure_without_activating_shadow(monkeypatch, failed):
    with patch.dict(sys.modules, {'allocator':allocator, 'market_internals':Mock(), 'regime_classifier':Mock()}):
        spec = importlib.util.spec_from_file_location('master_strategist_under_test', MASTER/'strategist.py')
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    for key in ['NWT_DB_DSN','ALPACA_API_KEY','ALPACA_SECRET_KEY','ALPACA_DATA_URL']:
        monkeypatch.setenv(key,'test-only')
    monkeypatch.setenv('ALPACA_BASE_URL','https://paper-api.alpaca.markets')
    conn=Mock();monkeypatch.setattr(m,'run_integrity_checks',lambda **kw:conn)
    monkeypatch.setattr(m,'fetch_market_internals',lambda **kw:{'vix':12})
    for name,value in [('read_open_positions',[]),('compute_drawdown',0),('fetch_recent_regime_history',[])]:
        monkeypatch.setattr(m,name,lambda *a,v=value:v)
    monkeypatch.setattr(m,'record_regime_history',Mock())
    monkeypatch.setattr(m,'classify_regime',lambda *a,**kw:dict(primary_regime='risk_on',confidence=.95,transition_risk=0))
    allocation=Mock(return_value=(dict(BASE),['shadow only']))
    if failed:allocation.side_effect=RuntimeError('injected allocator failure')
    monkeypatch.setattr(m,'compute_dynamic_weights',allocation)
    write=Mock();state=Mock()
    monkeypatch.setattr(m,'write_directives',write);monkeypatch.setattr(m,'upsert_agent_state',state)
    monkeypatch.setattr(m,'log_to_postgres',Mock())
    assert m.main()==(3 if failed else 0)
    directives=write.call_args.args[0]
    assert {k:v['capital_weight'] for k,v in directives['bot_permissions'].items()}==BASE
    assert directives['allocator_mode']=='shadow_only'
    assert directives['allocator_status']==('failed' if failed else 'ok')
    assert state.call_args.args[2]==('degraded' if failed else 'ok')
    if failed:conn.rollback.assert_called()
