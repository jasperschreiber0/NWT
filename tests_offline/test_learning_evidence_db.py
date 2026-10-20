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
         CREATE TABLE nwt_portfolio_ledger(position_id uuid PRIMARY KEY,spread_group_id uuid,status text);
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
        q.execute('INSERT INTO nwt_portfolio_ledger VALUES(%s,%s,%s)',(p,group,'closed' if closed else 'open'))
        q.execute('INSERT INTO nwt_trade_outcomes VALUES(%s,%s,10,%s)',(o,p,'entry-evidence-20261020-v1' if verified else None))
        if verified:q.execute('INSERT INTO nwt_entry_evidence(position_id,decision_id,eligible,snapshot) VALUES(%s,%s,true,%s)',(p,decision,json.dumps({'test':'immutable'})))
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
