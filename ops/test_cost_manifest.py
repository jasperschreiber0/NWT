from prepare_cost_manifest import prepare
from unified_performance import expenses

def test_legacy_monthly_claim_is_preserved_but_never_assumed_usd():
    old={'alpaca':{'monthly_usd':None}}
    config=prepare(old)
    assert config['legacy_notes']==old and prepare(config)==config
    result=expenses(config,start='2026-10-01T00:00:00Z',end='2026-11-01T00:00:00Z')
    assert not result['complete'] and result['accrued_usd'] is None
    assert len(result['missing'])==5 and result['known_accrued_usd']==0

def test_existing_cost_evidence_is_not_overwritten():
    config={'records':[{'source_id':'invoice'}],'coverage':{'alpaca':{'reconciled':True}}}
    assert prepare(config)==config
