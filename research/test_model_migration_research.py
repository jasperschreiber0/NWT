"""Additive migration boundary, retained budgets and old model lineage."""
from datetime import datetime,timezone
from unittest.mock import Mock
import json
import investor_core as core
import investor_pipeline as pipeline
from event_core import classification_lineage
from test_investor_backoff import database
from test_investor_diagnostics import activist,answer,NOW


def test_v4_preserves_all_prior_versions_and_budget(monkeypatch):
    c=database()
    for version in [core.LEGACY_VERSION,core.PREVIOUS_VERSION,core.DIAGNOSTIC_VERSION]:
        pipeline.put(c,'policy',version,dict(version=version,code_sha256=version))
    pipeline.put(c,'classification','old',dict(model='gpt-4.1-mini-2025-04-14'))
    pipeline.state(c,'model-budget:'+NOW.date().isoformat(),20)
    before=c.execute('SELECT * FROM evidence ORDER BY kind,key').fetchall()
    pipeline.register_version(c,dict(core.POLICY,code_sha256='fixture'),NOW)
    assert all(row in c.execute('SELECT * FROM evidence').fetchall() for row in before)
    assert pipeline.state(c,'model-budget:'+NOW.date().isoformat())==20
    assert core.POLICY['maximum_model_attempts_per_day']==20
    assert pipeline.one(c,'version_boundary',core.VERSION)['prior_versions']==sorted([core.LEGACY_VERSION,core.PREVIOUS_VERSION,core.DIAGNOSTIC_VERSION])


def test_investor_usage_and_classification_keep_epoch(monkeypatch):
    import openai_client
    c=database();activist(c)
    def fake(prompt,model,shape,**kwargs):
        kwargs['usage_sink'](dict(model=model,model_epoch=openai_client.MODEL_EPOCH,tokens_used={'openai_cost_usd':.0001}))
        return answer(),10,5
    monkeypatch.setattr(openai_client,'call_json',fake)
    assert pipeline.classify_activists(c,NOW)['status']=='OK'
    assert pipeline.rows(c,'model_usage')[0]['model']=='gpt-6-luna'
    assert pipeline.rows(c,'classification')[0]['model_epoch']==openai_client.MODEL_EPOCH
    assert pipeline.rows(c,'classification')[0]['policy_version']==core.VERSION


def test_old_event_lineage_never_becomes_luna():
    lineage=classification_lineage(dict(version='GLOBAL-EVENT-OBS-v1',model='gpt-4.1-mini-2025-04-14'))
    watch=dict(lineage,version='GLOBAL-EVENT-OBS-v2-luna')
    assert classification_lineage(watch)==lineage
    assert lineage['model_policy'] is None


def test_event_boundary_idempotent_and_preserves_budget():
    import pytest
    from event_core import connect,put,setstate,state,register_model_boundary
    c=connect(":memory:")
    put(c,'classification','old',dict(model='gpt-4.1-mini-2025-04-14'))
    setstate(c,'model_attempts:2026-10-15','20')
    before=list(c.execute("SELECT * FROM records"))
    policy={'model':'gpt-6-luna','client_sha256':'fixture'}
    register_model_boundary(c,'v2',policy,NOW.isoformat())
    register_model_boundary(c,'v2',policy,NOW.isoformat())
    assert all(row in list(c.execute("SELECT * FROM records")) for row in before)
    assert state(c,'model_attempts:2026-10-15')=='20'
    assert c.execute("SELECT COUNT(*) FROM records WHERE kind='model_version_boundary'").fetchone()[0]==1
    with pytest.raises(ValueError):register_model_boundary(c,'v2',dict(policy,model='other'),NOW.isoformat())
