"""Real Postgres acceptance tests, isolated schema; never production."""
import json
import os
import uuid
from pathlib import Path
import psycopg2
import pytest

MIGRATION=Path(__file__).resolve().parents[1]/'db/migrate_2026_10_20_learning_evidence.sql'


@pytest.fixture
def db():
    dsn=os.environ.get('NWT_TEST_DB_DSN')
    if not dsn:pytest.skip('Isolated PostgreSQL required')
    c=psycopg2.connect(dsn); schema='evidence_'+uuid.uuid4().hex
    with c.cursor() as q:
        q.execute('CREATE SCHEMA '+schema)
        q.execute('SET search_path TO '+schema)
        q.execute('''CREATE TABLE nwt_decision_inputs(id uuid PRIMARY KEY);
         CREATE TABLE nwt_portfolio_ledger(position_id uuid PRIMARY KEY,spread_group_id uuid,status text,asset text DEFAULT 'SPY');
         CREATE TABLE nwt_trade_outcomes(id uuid PRIMARY KEY,position_id uuid,pnl numeric);''')
        q.execute(MIGRATION.read_text(encoding='utf-8'))
        q.execute(MIGRATION.read_text(encoding='utf-8'))
    c.commit()
    yield c
    c.rollback()
    with c.cursor() as q:q.execute('DROP SCHEMA '+schema+' CASCADE')
    c.commit();c.close()


def add(c,group,decision,*,closed=True,verified=True):
    p=str(uuid.uuid4());o=str(uuid.uuid4())
    with c.cursor() as q:
        q.execute('INSERT INTO nwt_portfolio_ledger(position_id,spread_group_id,status) VALUES(%s,%s,%s)',(p,group,'closed' if closed else 'open'))
        q.execute('INSERT INTO nwt_trade_outcomes VALUES(%s,%s,10,%s)',(o,p,'entry-evidence-20261020-v1' if verified else None))
        if verified:q.execute('INSERT INTO nwt_entry_evidence(position_id,decision_id,eligible,snapshot) VALUES(%s,%s,true,%s)',(p,decision,json.dumps({'test':'immutable','expected_assets':['SPY']})))
    c.commit();return p


def test_no_legacy_leakage_and_complete_group_gate(db):
    d=str(uuid.uuid4());g=str(uuid.uuid4())
    with db.cursor() as q:q.execute('INSERT INTO nwt_decision_inputs VALUES(%s)',(d,))
    p=add(db,g,d);other=add(db,g,d,closed=False)
    with db.cursor() as q:
        q.execute('SELECT count(*) FROM nwt_verified_learning_outcomes');assert q.fetchone()[0]==0
        q.execute("UPDATE nwt_portfolio_ledger SET status='closed' WHERE position_id=%s",(other,))
        q.execute('SELECT count(*),sum(pnl) FROM nwt_verified_learning_outcomes');assert q.fetchone()==(2,20)
    add(db,None,d,verified=False)
    with db.cursor() as q:
        q.execute('SELECT count(*) FROM nwt_trade_outcomes');assert q.fetchone()[0]==3
        q.execute('SELECT count(*) FROM nwt_verified_learning_outcomes');assert q.fetchone()[0]==2
        q.execute('INSERT INTO nwt_trade_outcomes VALUES(%s,%s,10,%s)',(str(uuid.uuid4()),p,'entry-evidence-20261020-v1'))
        q.execute('SELECT count(*) FROM nwt_verified_learning_outcomes');assert q.fetchone()[0]==0


def test_snapshot_cannot_be_changed_or_deleted(db):
    d=str(uuid.uuid4())
    with db.cursor() as q:q.execute('INSERT INTO nwt_decision_inputs VALUES(%s)',(d,))
    p=add(db,None,d)
    for sql in ['UPDATE nwt_entry_evidence SET eligible=false WHERE position_id=%s','DELETE FROM nwt_entry_evidence WHERE position_id=%s']:
        with pytest.raises(psycopg2.Error):
            with db.cursor() as q:q.execute(sql,(p,))
        db.rollback()


def test_exact_capture_and_ambiguous_decisions_fail_closed(db):
    import sys
    sys.path.insert(0,str(MIGRATION.parents[1]/'nwt_agents'))
    from entry_evidence import capture, load
    tid=str(uuid.uuid4());p=str(uuid.uuid4());d=str(uuid.uuid4())
    with db.cursor() as q:
        q.execute('''CREATE TABLE nwt_tickets(ticket_id uuid,created_at timestamptz,payload jsonb);
          ALTER TABLE nwt_decision_inputs ADD ticket_id uuid, ADD created_at timestamptz,
          ADD run_at timestamptz, ADD strategy_id text, ADD symbol text, ADD regime jsonb;''')
        q.execute("INSERT INTO nwt_tickets VALUES(%s,'2026-10-20T10:00:00Z',%s)",(tid,json.dumps({'symbol':'SPY'})))
        q.execute("INSERT INTO nwt_decision_inputs VALUES(%s,%s,'2026-10-20T09:00:00Z','2026-10-20T09:00:00Z','A','SPY',%s)",(d,tid,json.dumps({'primary_regime':'range'})))
        q.execute("INSERT INTO nwt_portfolio_ledger(position_id,status) VALUES(%s,'open')",(p,))
    data=dict(ticket_id=tid,asset='SPY',entry_time='2026-10-20T12:00:00Z',strategy_id='A')
    capture(db,p,data);db.commit()
    assert load(db,p)['eligible'] and load(db,p)['decision_id']==d
    second=str(uuid.uuid4())
    with db.cursor() as q:
        q.execute('INSERT INTO nwt_decision_inputs SELECT %s,ticket_id,created_at,run_at,strategy_id,symbol,regime FROM nwt_decision_inputs WHERE id=%s',(str(uuid.uuid4()),d))
        q.execute("INSERT INTO nwt_portfolio_ledger(position_id,status) VALUES(%s,'open')",(second,))
    capture(db,second,data);db.commit()
    assert not load(db,second)['eligible']
    assert load(db,p)['decision_id']==d  # The later ambiguity cannot rewrite entry evidence.


def test_edge_query_counts_one_complete_spread_not_legs(db):
    import sys
    sys.path.insert(0,str(MIGRATION.parents[1]/'nwt_agents'))
    from learning_agent import compute_edge_summary
    d=str(uuid.uuid4());g=str(uuid.uuid4())
    with db.cursor() as q:
        q.execute('''ALTER TABLE nwt_decision_inputs ADD outcome_reason text, ADD strategy_id text,
          ADD genome_version integer, ADD regime jsonb, ADD direction text, ADD signal_strength numeric,
          ADD shadow_pnl_pct numeric, ADD shadow_evaluated_at timestamptz;
          ALTER TABLE nwt_trade_outcomes ADD strategy_id text DEFAULT 'A', ADD pnl_adjusted numeric;''')
        q.execute("INSERT INTO nwt_decision_inputs(id,outcome_reason) VALUES(%s,'EXECUTED')",(d,))
        # Recreate view to include the outcome columns added for this fixture.
        q.execute(MIGRATION.read_text(encoding='utf-8'))
    for _ in range(2):
        p=str(uuid.uuid4())
        with db.cursor() as q:
            q.execute("INSERT INTO nwt_portfolio_ledger VALUES(%s,%s,'closed','SPY')",(p,g))
            q.execute("INSERT INTO nwt_trade_outcomes(id,position_id,pnl,evidence_version) VALUES(%s,%s,10,'entry-evidence-20261020-v1')",(str(uuid.uuid4()),p))
            context={'expected_assets':['SPY'],'decision_context':{'genome_version':1,'direction':'long','signal_strength':2,'regime':{'primary_regime':'range'}}}
            q.execute('INSERT INTO nwt_entry_evidence(position_id,decision_id,eligible,snapshot) VALUES(%s,%s,true,%s)',(p,d,json.dumps(context)))
    rows=compute_edge_summary(db)
    assert len(rows)==1 and rows[0]['n']==1 and rows[0]['avg_pnl']==20
