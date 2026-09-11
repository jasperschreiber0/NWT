"""Ordered incident recovery, allocation and lifecycle validation."""
import os,sys,json
from pathlib import Path
ROOT=Path('/home/northworld/trading')

def main(check=False):
    import recover_vgk,allocate_aapl,paper_lifecycle
    if check:
        recover_vgk.main(True);allocate_aapl.main(True);paper_lifecycle.main(True);return
    sys.path.insert(0,str(ROOT/'execution'));import engine
    c=engine.get_db()
    try:
        with c.cursor() as q:
            q.execute('SELECT pg_try_advisory_lock(9141330199)');assert q.fetchone()[0]
        if paper_lifecycle.STATE.exists() and json.loads(paper_lifecycle.STATE.read_text()).get('stage')=='COMPLETE':
            sys.path.insert(0,str(ROOT/'nwt_agents'));from recon_agent import run_recon
            assert not engine.check_no_trade_mode(c)[0]
            assert run_recon(c,'paper_workflow_release_resume')
            with c.cursor() as q:
                q.execute("UPDATE nwt_system_flags SET value=FALSE,reason='Paper lifecycle completed',updated_at=NOW() WHERE flag='qa_validation_in_progress' AND set_by='paper_workflow'")
            c.commit();print('Workflow already complete; no new orders');return
        assert engine.ALPACA_BASE_URL=='https://paper-api.alpaca.markets'
        with c.cursor() as q:
            q.execute("INSERT INTO nwt_system_flags(flag,value,reason,set_by) VALUES('qa_validation_in_progress',TRUE,'Authorized paper workflow','paper_workflow') ON CONFLICT(flag) DO UPDATE SET value=TRUE,reason=EXCLUDED.reason,set_by=EXCLUDED.set_by,updated_at=NOW()")
        c.commit()
        os.environ['NWT_DEFER_RECOVERY_RELEASE']='1'
        recover_vgk.main();allocate_aapl.main()
        sys.path.insert(0,str(ROOT/'nwt_agents'))
        from recon_agent import run_recon
        from shared_context import clear_no_trade_mode
        assert run_recon(c,'paper_workflow_before_qa')
        with c.cursor() as q:
            q.execute("SELECT value,reason,set_by FROM nwt_system_flags WHERE flag='no_trade_mode' FOR UPDATE")
            assert q.fetchone()==(True,'Recon critical mismatch: 1 untracked/qty-mismatch positions','recon_agent')
            clear_no_trade_mode(c,'paper_workflow_qa_only')
        # The separate validation flag still blocks all normal strategy entries.
        paper_lifecycle.main()
        assert run_recon(c,'paper_workflow_completed')
        with c.cursor() as q:
            q.execute("UPDATE nwt_system_flags SET value=FALSE,reason='Paper lifecycle completed',updated_at=NOW() WHERE flag='qa_validation_in_progress' AND set_by='paper_workflow'")
        c.commit();engine.log_system_event(c,'INFO','paper_workflow','Recovery, allocation and paper lifecycle complete; normal gates remain active')
    finally:c.close()

if __name__=='__main__':main('--check' in sys.argv)
