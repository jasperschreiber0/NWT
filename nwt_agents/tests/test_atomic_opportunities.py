from datetime import date
import pytest
import opportunity_outcomes
from test_statistical_validity import conn, asx_strategist, ukeu_strategist, china_strategist, us_strategist
from shared_context import log_decision_input


def write(kind, conn, price=100):
    args=dict(run_date=date(2026,9,17),symbol='SPY',genome_version=1,regime={},
              signal_strength=.8,direction='long',entry_price_ref=price,target_pct=.03,stop_pct=-.01)
    if kind=='shared':
        return log_decision_input(conn,**args,strategy_id='C1',track='C',archetype='test',is_winner=True,decision='CANDIDATE')
    module={'aus':asx_strategist,'eu':ukeu_strategist,'china':china_strategist,'us':us_strategist}[kind]
    if kind!='us':args['strategy_id']='TEST-'+kind
    if kind=='china':args['poll_slot']='14:00'
    return module.log_decision_input(conn,**args)


@pytest.mark.parametrize('kind',['shared','aus','eu','china','us'])
def test_each_writer_persists_both_records_and_retries_preserve_evidence(conn,kind):
    ident=write(kind,conn)
    assert ident
    assert write(kind,conn,price=999)==ident
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*),MIN(entry_price) FROM nwt_opportunity_outcomes WHERE source_decision_id=%s AND lane='RAW_SHADOW'",(ident,))
        assert cur.fetchone()==(1,100)


@pytest.mark.parametrize('kind',['shared','aus','eu','china','us'])
def test_secondary_write_failure_cannot_commit_an_unpaired_decision(conn,monkeypatch,kind):
    def fail(*args,**kwargs):raise RuntimeError('injected analytics failure')
    monkeypatch.setattr(opportunity_outcomes,'record_decision_outcome',fail)
    assert write(kind,conn) is None
    with conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM nwt_decision_inputs')
        assert cur.fetchone()[0]==0
