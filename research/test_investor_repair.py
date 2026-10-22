"""Regression tests for renamed holdings feed and grounded batch classification."""
import json
from datetime import timedelta
from unittest.mock import Mock
import pytest
import investor_core as core
import investor_pipeline as pipeline
from test_investor_backoff import database,Public,NOW
from test_investor_diagnostics import activist,api,answer


def test_passages_preserve_every_character_and_negation():
    text=('This is context. '*100)+'We do NOT propose to acquire the company.'
    passages=core.source_passages(text)
    assert ''.join(p['text'] for p in passages)==text
    p=passages[-1]
    result=core.grounded_catalyst(answer(evidence_id=p['id']),text)
    assert result['evidence_quote']==text[result['evidence_start']:result['evidence_end']]
    assert 'NOT propose' in result['evidence_quote']
    for bad in [True,0,-1,999,'1',None]:
        with pytest.raises(ValueError):core.grounded_catalyst(answer(evidence_id=bad),text)


def test_batch_resolves_four_filings_with_one_billed_call(monkeypatch):
    c=database()
    for key in ['a','b','c','d','e']:activist(c,key,first_seen_at=NOW.isoformat())
    call=api(monkeypatch)
    result=pipeline.classify_activists(c,NOW)
    assert result['classified']==4 and call.call_count==1
    assert pipeline.state(c,'model-budget:'+NOW.date().isoformat())==1
    assert len(pipeline.rows(c,'classification_batch'))==1
    assert pipeline.classification_coverage(c,NOW)['pending']==1
    pipeline.classify_activists(c,NOW)
    assert len(pipeline.rows(c,'classification'))==5 and call.call_count==2
    pipeline.classify_activists(c,NOW);assert call.call_count==2


def test_one_bad_response_does_not_discard_other_filings(monkeypatch):
    c=database();activist(c,'a',first_seen_at=NOW.isoformat());activist(c,'b',first_seen_at=NOW.isoformat())
    call=api(monkeypatch)
    call.side_effect=lambda *a,**k:(dict(classifications=[dict(answer(),event_id='a'),dict(answer(evidence_id=500),event_id='b')]),10,5)
    result=pipeline.classify_activists(c,NOW)
    assert result['classified']==1 and result['status']=='DEGRADED'
    assert pipeline.one(c,'classification','a') and not pipeline.one(c,'classification','b')
    assert pipeline.classification_coverage(c,NOW)['pending']==1


def test_new_filings_precede_retries_and_two_failures_remain_visible(monkeypatch):
    c=database();activist(c,'old',first_seen_at=NOW.isoformat())
    call=api(monkeypatch,error=ValueError('not logged'))
    pipeline.classify_activists(c,NOW)
    activist(c,'new',first_seen_at=(NOW+timedelta(minutes=30)).isoformat())
    monkeypatch.setitem(core.POLICY,'classification_batch_size',1)
    pipeline.classify_activists(c,NOW+timedelta(hours=1))
    assert json.loads(call.call_args.args[0].split('SOURCES:\n')[1])[0]['event_id']=='new'
    pipeline.classify_activists(c,NOW+timedelta(hours=1))
    assert pipeline.classification_coverage(c,NOW+timedelta(hours=1))['quarantined']==1
    assert pipeline.classification_coverage(c,NOW+timedelta(hours=1))['status']=='INCOMPLETE'
    calls=call.call_count
    pipeline.classify_activists(c,NOW+timedelta(hours=1,minutes=5))
    assert call.call_count==calls and len(pipeline.rows(c,'model_error'))==3


def test_batch_source_budget_never_truncates_filings(monkeypatch):
    c=database()
    for key in ['a','b','c']:activist(c,key,purpose='We have no current plans. '*700,first_seen_at=NOW.isoformat())
    call=api(monkeypatch);pipeline.classify_activists(c,NOW)
    sources=json.loads(call.call_args.args[0].split('SOURCES:\n')[1])
    assert len(sources)==1
    assert ''.join(p['text'] for p in sources[0]['passages'])=='We have no current plans. '*700


def test_new_arkw_url_bypasses_cached_failure_and_preserves_history():
    c=database();public=Public();public.days['ARKW']='10/13/2026'
    oldurl='https://assets.ark-funds.com/fund-documents/funds-etf-csv/ARK_NEXT_GENERATION_INTERNET_ETF_ARKW_HOLDINGS.csv'
    pipeline.state(c,'ark-check:ARKW',dict(status='DEGRADED',date='2026-10-06',next_at=(NOW+timedelta(hours=6)).isoformat(),source=dict(url=oldurl),failures=10))
    pipeline.put(c,'ark','ARKW:2026-10-06',dict(fund='ARKW',date='2026-10-06',holdings={},source=dict(sha256='old')))
    result=pipeline.collect_ark(c,public,NOW)['ARKW']
    assert result['status']=='OK' and result['date']=='2026-10-13'
    assert 'TECHNOLOGY' in result['source']['url'] and result['failures']==0
    assert pipeline.one(c,'ark','ARKW:2026-10-06')['source']['sha256']=='old'
    assert pipeline.rows(c,'ark_change')==[]  # Never bridge the missing week with an invented purchase.


def test_request_failure_still_obeys_ark_backoff_without_source_payload():
    c=database();public=Public();public.days['ARKW']=RuntimeError('network unavailable')
    first=pipeline.collect_ark(c,public,NOW)['ARKW'];count=len(public.calls)
    assert first['source'] is None
    assert pipeline.collect_ark(c,public,NOW+timedelta(minutes=5))['ARKW']==first
    assert len(public.calls)==count


def test_legacy_classifier_budget_is_carried_into_new_protocol(monkeypatch):
    c=database();activist(c,first_seen_at=NOW.isoformat());call=api(monkeypatch)
    pipeline.state(c,'model-budget:'+NOW.date().isoformat(),20)
    assert pipeline.classify_activists(c,NOW)['attempts_today']==21
    assert call.call_count==1
