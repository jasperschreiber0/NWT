import base64
import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime,timedelta,timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
import earnings_variance_v2 as ev

TEXT='Management reports unresolved demand uncertainty.'


@pytest.fixture
def experiment(tmp_path,monkeypatch):
    # Short synthetic clock window exercises mechanics, never acceptance.
    policy=dict(ev.POLICY,holdout_start='2026-11-02T00:00:00Z',holdout_end='2026-11-03T00:00:00Z')
    monkeypatch.setattr(ev,'POLICY',policy)
    clock=[datetime(2026,11,1,23,tzinfo=timezone.utc)]
    monkeypatch.setattr(ev,'now',lambda:clock[0])
    key=Ed25519PrivateKey.generate()  # ephemeral fixture key; no service credential
    sessions=[];d=datetime(2026,10,1,tzinfo=timezone.utc)
    while d<datetime(2026,12,1,tzinfo=timezone.utc):
        if d.weekday()<5:sessions.append(dict(open=d.replace(hour=14,minute=30).isoformat(),close=d.replace(hour=21).isoformat()))
        d+=timedelta(days=1)
    cfg=dict(sources={s:dict(cik=str(i+1),sec_url=f'https://data.sec.gov/submissions/CIK{i+1:010d}.json',issuer_url=f'https://{s.lower()}.example.test/releases') for i,s in enumerate(policy['universe'])},
             calendar=dict(source_reference='SYNTHETIC-SMOKE',sessions=sessions),model='synthetic-revision-1',settings={'temperature':0},
             recorder_public_key=base64.b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode(),
             recorder_identity_reference='TEST-ONLY',source_map_reference='TEST-ONLY',model_revision_reference='TEST-ONLY',extraction_version='test-v1',evidence_class='engineering_smoke',
             recorder_contract=dict(reference='TEST-ONLY',network_attempt_limit=1,sdk_retries=0,sign_only_journaled_attempts=True,record_terminal_failures=True,timestamp_custody_reference='TEST-CLOCK'))
    ev.register(tmp_path,cfg)
    checks={s:{name:dict(url=v[name+'_url'],status='OK',sha256='fixture-source') for name in ('sec','issuer')} for s,v in cfg['sources'].items()}
    candidate=dict(symbol='AAPL',cik='1',fiscal_period_end='2026-09-30',form='8-K',item='2.02',release_at='2026-11-01T23:00:00Z',sec_accepted_at='2026-11-01T23:00:00Z',
                   first_seen_at=clock[0].isoformat(),timestamp_verified=True,release_url='https://aapl.example.test/releases/1',
                   source_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),accession='fixture-accession')
    return tmp_path,clock,key,cfg,checks,candidate


def admit(x):
    folder,clock,_,_,checks,c=x
    return ev.discover(folder,clock[0].isoformat(),checks,[c])[0]


def market_features(event,at):
    prior=[dict(at=t,price=100*math.exp(.01*i)) for i,t in enumerate(event['prior_price_dates'])]
    market=dict(prior_closes=prior,opening=dict(at=event['earnings_open_at'],price=prior[-1]['price']*math.exp(.02)),
                adjustment='split_dividend_consistent',corporate_actions_checked=True,available_at=at.isoformat(),source_reference='fixture-prices')
    return dict(prior_returns=[.01]*20,prior_variance=.0001,earnings_gap=.02,features_available_at=at.isoformat(),
                data_source_reference='fixture-prices',inference_cost_usd=None,market_prices=market)


def signed_response(x,event,features,intent,score=.8):
    _,clock,key,cfg,_,_=x
    response=dict(id='fixture-response-'+event['event_id'],created_at=clock[0].timestamp(),model=cfg['model'],status='completed',
                  output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(dict(uncertainty_score=score,quote='unresolved demand uncertainty')))])])
    body=dict(event_id=event['event_id'],attempt_id=intent['attempt_id'],attempt_number=1,features=features,source_text=TEXT,
              request_raw=intent['request_raw'],response_raw=json.dumps(response),requested_at=clock[0].isoformat(),received_at=clock[0].isoformat())
    return dict(body=body,signature=base64.b64encode(key.sign(ev.canonical(body))).decode())


def prediction(x,event):
    folder,clock,_,_,_,_=x
    for hour in range(16):
        clock[0]=datetime(2026,11,2,hour,tzinfo=timezone.utc)
        ev.discover(folder,clock[0].isoformat(),x[4],[])
    clock[0]=datetime(2026,11,2,16,tzinfo=timezone.utc)
    features=market_features(event,clock[0])
    intent=ev.begin_inference(folder,event['event_id'],features,TEXT)
    return features,signed_response(x,event,features,intent)



def complete_discovery(x):
    folder,clock,_,_,checks,_=x
    for hour in range(16,24):
        clock[0]=datetime(2026,11,2,hour,tzinfo=timezone.utc)
        ev.discover(folder,clock[0].isoformat(),checks,[])


def outcome(event):
    return dict(event_id=event['event_id'],price_timestamps=[event['cutoff']]+event['session_dates'],closes=[100,101,100,101,100,101],source_reference='fixture-outcomes',corporate_actions_checked=True)


def test_complete_population_and_signed_original_receipt(experiment):
    x=experiment;e=admit(x);f,r=prediction(x,e)
    ev.seal(x[0],e['event_id'],f,r)
    complete_discovery(x);x[1][0]=datetime(2026,11,10,tzinfo=timezone.utc)
    result=ev.evaluate(x[0],[outcome(e)])
    assert result['events']==1 and not result['accepted'] and not result['profitability_validated']
    with pytest.raises(FileExistsError):ev.evaluate(x[0],[outcome(e)])


@pytest.mark.parametrize('hours,status',[(14,'ADMITTED'),(15,'SKIPPED_LATE_DISCOVERY')])
def test_admission_deadline_and_discovery_lag(experiment,hours,status):
    x=experiment;x[1][0]=datetime(2026,11,2,hours,tzinfo=timezone.utc);x[5]['first_seen_at']=x[1][0].isoformat()
    assert admit(x)['status']==status


def test_duplicate_and_correction_do_not_resurrect_event(experiment):
    x=experiment;admit(x);x[1][0]+=timedelta(hours=1)
    assert admit(x)['status']=='DUPLICATE'
    x[1][0]+=timedelta(hours=1);x[5].update(first_seen_at=x[1][0].isoformat(),source_sha256='amended',correction_kind='financial_figures_changed',form='8-K/A')
    assert admit(x)['status']=='INVALIDATED_CORRECTION'
    assert not ev.census(x[0])[0]
    x[1][0]+=timedelta(hours=1)
    assert admit(x)['status']=='INVALIDATED_CORRECTION'
    assert not ev.census(x[0])[0] and len(ev.census(x[0])[1])==3


def test_skip_and_missing_issuer_check_are_audited(experiment):
    x=experiment;x[5]['timestamp_verified']=False
    assert admit(x)['status']=='SKIPPED_UNVERIFIED_TIME'
    x[1][0]+=timedelta(hours=1);del x[4]['MSFT']
    with pytest.raises(ValueError,match='Every issuer'):admit(x)
    assert ev.journal(x[0])[-1]['action']=='discovery_attempt'


@pytest.mark.parametrize('tamper',['response','request','features','signature','model'])
def test_tampered_receipts_and_mismatched_requests_fail_closed(experiment,tamper):
    x=experiment;e=admit(x);f,r=prediction(x,e)
    if tamper=='response':r['body']['response_raw']=r['body']['response_raw'].replace('0.8','0.9')
    if tamper=='request':r['body']['request_raw']='{}'
    if tamper=='features':f=deepcopy(f);f['earnings_gap']=9
    if tamper=='signature':r['signature']=base64.b64encode(b'x'*64).decode()
    if tamper=='model':
        raw=json.loads(r['body']['response_raw']);raw['model']='wrong';r['body']['response_raw']=json.dumps(raw)
        r['signature']=base64.b64encode(x[2].sign(ev.canonical(r['body']))).decode()
    with pytest.raises(Exception):ev.seal(x[0],e['event_id'],f,r)
    assert not any(a['action']=='prediction' for a in ev.journal(x[0]))
    with pytest.raises(ValueError,match='One attempt'):ev.seal(x[0],e['event_id'],f,r)


@pytest.mark.parametrize('failure',['coverage','outcome','prediction'])
def test_incomplete_population_blocks_evaluation(experiment,failure):
    x=experiment;e=admit(x);f,r=prediction(x,e)
    if failure!='prediction':ev.seal(x[0],e['event_id'],f,r)
    if failure!='coverage':complete_discovery(x)
    x[1][0]=datetime(2026,11,10,tzinfo=timezone.utc)
    with pytest.raises(ValueError,match='Incomplete'):ev.evaluate(x[0],[] if failure=='outcome' else [outcome(e)])


def test_journal_tampering_detected(experiment):
    x=experiment;admit(x);path=x[0]/'journal-00000001.json'
    data=json.loads(path.read_text());data['payload']['events'][0]['status']='DUPLICATE';path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='Journal changed'):ev.census(x[0])


def test_unconfigured_registration_rejected(experiment,tmp_path):
    cfg=deepcopy(experiment[3]);cfg['model']=None
    with pytest.raises(ValueError,match='Unconfigured'):ev.register(tmp_path/'blocked',cfg)


def test_post_cutoff_amendment_stays_in_population(experiment):
    x=experiment;e=admit(x);x[1][0]=datetime(2026,11,2,22,tzinfo=timezone.utc)
    x[5].update(first_seen_at=x[1][0].isoformat(),source_sha256='amended',correction_kind='official_withdrawal',form='8-K/A')
    assert admit(x)['status']=='POST_CUTOFF_AMENDMENT'
    assert e['event_id'] in ev.census(x[0])[0]


def test_rehashed_prediction_tampering_still_fails_receipt_binding(experiment):
    x=experiment;e=admit(x);f,r=prediction(x,e);ev.seal(x[0],e['event_id'],f,r);complete_discovery(x)
    previous=None
    for path in sorted(x[0].glob('journal-*.json')):
        row=json.loads(path.read_text());row.pop('sha256')
        if row['action']=='prediction':row['payload']['prediction']['candidate']=99
        row['previous']=previous;previous=ev.digest(row)
        path.write_text(json.dumps(dict(row,sha256=previous)))
    x[1][0]=datetime(2026,11,10,tzinfo=timezone.utc)
    with pytest.raises(ValueError,match='Prediction receipt changed'):ev.evaluate(x[0],[outcome(e)])



def test_pre_registration_source_is_not_prospective(experiment):
    x=experiment;x[5]['release_at']='2026-11-01T22:59:59Z'
    assert admit(x)['status']=='SKIPPED_PRE_REGISTRATION'
    assert not ev.census(x[0])[0]


def test_repeated_scan_preserves_first_seen_and_advances_observation(experiment):
    x=experiment;first=admit(x)
    x[1][0]+=timedelta(hours=1)
    again=admit(x)
    assert again['status']=='DUPLICATE'
    assert again['first_seen_at']==first['first_seen_at']
    assert ev.stamp(again['observed_at'])>ev.stamp(first['observed_at'])


def test_repeated_source_cannot_rewrite_first_seen(experiment):
    x=experiment;admit(x);x[1][0]+=timedelta(hours=1)
    x[5]['first_seen_at']=x[1][0].isoformat()
    with pytest.raises(ValueError,match='Immutable first_seen'):admit(x)


def test_skipped_original_remains_canonical_for_same_period(experiment):
    x=experiment;x[5]['release_at']='2026-11-01T22:59:59Z'
    assert admit(x)['status']=='SKIPPED_PRE_REGISTRATION'
    x[1][0]+=timedelta(hours=1)
    x[5].update(release_at='2026-11-01T23:30:00Z',first_seen_at=x[1][0].isoformat(),source_sha256='different-original',accession='different-accession')
    assert admit(x)['status']=='UNRESOLVED_AMENDMENT'
    assert not ev.census(x[0])[0]


def test_failed_scan_cannot_retry_with_candidate_removed(experiment):
    x=experiment;x[5]['release_url']='https://wrong.example.test/release'
    with pytest.raises(ValueError,match='Unverified candidate'):admit(x)
    with pytest.raises(ValueError,match='attempt'):
        ev.discover(x[0],x[1][0].isoformat(),x[4],[])
    attempts=[r for r in ev.journal(x[0]) if r['action']=='discovery_attempt']
    assert attempts[0]['payload']['candidates'][0]['release_url']==x[5]['release_url']
    assert not any(r['action']=='discovery' for r in ev.journal(x[0]))


def test_skipped_duplicate_and_objective_amendment_never_admit(experiment):
    x=experiment;x[5]['release_at']='2026-11-01T22:59:59Z';admit(x)
    x[1][0]+=timedelta(hours=1)
    assert admit(x)['status']=='DUPLICATE'
    x[1][0]+=timedelta(hours=1)
    x[5].update(form='8-K/A',source_sha256='corrected',first_seen_at=x[1][0].isoformat(),correction_kind='financial_figures_changed')
    assert admit(x)['status']=='INVALIDATED_CORRECTION'
    assert not ev.census(x[0])[0]


def test_canonical_order_does_not_depend_on_batch_order(experiment):
    x=experiment;early=deepcopy(x[5]);early['release_at']='2026-11-01T22:59:59Z'
    later=dict(x[5],source_sha256='later-original',accession='later-accession')
    rows=ev.discover(x[0],x[1][0].isoformat(),x[4],[later,early])
    assert [r['status'] for r in rows]==['SKIPPED_PRE_REGISTRATION','UNRESOLVED_AMENDMENT']
    assert not ev.census(x[0])[0]


def test_failed_attempt_blocks_final_coverage_despite_empty_success_record(experiment):
    x=experiment;x[5]['release_url']='https://wrong.example.test/release'
    with pytest.raises(ValueError):admit(x)
    with pytest.raises(ValueError):ev.discover(x[0],x[1][0].isoformat(),x[4],[])
    # Simulate an erroneous importer appending a success after those attempts.
    ev.append(x[0],'discovery',dict(slot=x[1][0].isoformat(),checks=x[4],events=[],input_sha256=ev.digest([x[4],[]])))
    for hour in range(24):
        x[1][0]=datetime(2026,11,2,hour,tzinfo=timezone.utc)
        ev.discover(x[0],x[1][0].isoformat(),x[4],[])
    x[1][0]=datetime(2026,11,10,tzinfo=timezone.utc)
    with pytest.raises(ValueError,match='attempt coverage'):ev.evaluate(x[0],[])



def test_dependency_only_change_invalidates_registration(experiment,monkeypatch,tmp_path):
    from pathlib import Path
    dependency=tmp_path/'changed_baseline.py'
    dependency.write_text(Path(ev.baseline_dependency.__file__).read_text()+'\n# changed formula dependency\n')
    monkeypatch.setattr(ev.baseline_dependency,'__file__',str(dependency))
    with pytest.raises(ValueError,match='Registration changed'):ev.registration(experiment[0])


def test_real_registration_disabled_even_with_populated_config(experiment,tmp_path):
    cfg=deepcopy(experiment[3]);cfg['evidence_class']='prospective_source_verified'
    with pytest.raises(ValueError,match='Prospective registration disabled'):
        ev.register(tmp_path/'not_ready',cfg)
    assert not (tmp_path/'not_ready/registration.json').exists()


def test_adapter_journals_before_transport_and_never_retries_failure(experiment):
    x=experiment;e=admit(x);x[1][0]=datetime(2026,11,2,16,tzinfo=timezone.utc)
    f=market_features(e,x[1][0]);calls=[]
    def transport(attempt_id,request_raw):
        intent=json.loads(ev.attempt_path(x[0],e['event_id']).read_text())
        assert intent['attempt_id']==attempt_id and intent['request_raw']==request_raw
        assert ev.journal(x[0])[-1]['action']=='inference_started'
        calls.append(attempt_id)
        raise TimeoutError('synthetic transport failure')
    with pytest.raises(TimeoutError):ev.execute_inference(x[0],e['event_id'],f,TEXT,transport,lambda _:pytest.fail('must not sign failure'))
    assert json.loads(ev.attempt_path(x[0],e['event_id'],True).read_text())['status']=='FAILED'
    with pytest.raises(FileExistsError):ev.execute_inference(x[0],e['event_id'],f,TEXT,transport,lambda _:None)
    assert len(calls)==1 and ev.journal(x[0])[-1]['action']=='inference_failed'


def test_adapter_success_has_one_transport_and_terminal_receipt(experiment):
    x=experiment;e=admit(x);x[1][0]=datetime(2026,11,2,16,tzinfo=timezone.utc)
    f=market_features(e,x[1][0]);calls=[]
    def transport(attempt_id,raw):
        calls.append(attempt_id)
        intent=json.loads(ev.attempt_path(x[0],e['event_id']).read_text())
        return signed_response(x,e,f,intent)['body']['response_raw']
    sign=lambda body:base64.b64encode(x[2].sign(body)).decode()
    p=ev.execute_inference(x[0],e['event_id'],f,TEXT,transport,sign)
    assert p['receipt']['body']['attempt_number']==1
    assert json.loads(ev.attempt_path(x[0],e['event_id'],True).read_text())['status']=='SUCCEEDED'
    with pytest.raises(FileExistsError):ev.execute_inference(x[0],e['event_id'],f,TEXT,transport,sign)
    assert len(calls)==1


def test_orphan_durable_intent_blocks_resubmission(experiment,monkeypatch):
    x=experiment;e=admit(x);x[1][0]=datetime(2026,11,2,16,tzinfo=timezone.utc);f=market_features(e,x[1][0])
    def interrupted(*a):raise RuntimeError('synthetic crash after fsync')
    monkeypatch.setattr(ev,'append',interrupted)
    with pytest.raises(RuntimeError):ev.begin_inference(x[0],e['event_id'],f,TEXT)
    assert ev.attempt_path(x[0],e['event_id']).exists()
    with pytest.raises(FileExistsError):ev.begin_inference(x[0],e['event_id'],f,TEXT)


@pytest.mark.parametrize('change',['attempt_id','attempt_number','requested_at'])
def test_receipt_cannot_relabel_actual_attempt(experiment,change):
    x=experiment;e=admit(x);f,r=prediction(x,e)
    r['body'][change]={'attempt_id':'invented','attempt_number':2,'requested_at':'2026-11-02T15:59:59Z'}[change]
    r['signature']=base64.b64encode(x[2].sign(ev.canonical(r['body']))).decode()
    with pytest.raises(ValueError):ev.seal(x[0],e['event_id'],f,r)
    assert json.loads(ev.attempt_path(x[0],e['event_id'],True).read_text())['status']=='FAILED'


@pytest.mark.parametrize('change',['prior_timestamp','opening_timestamp','previous_close','opening_price','return','gap','availability'])
def test_price_features_are_recomputed_before_any_request(experiment,change):
    x=experiment;e=admit(x);x[1][0]=datetime(2026,11,2,16,tzinfo=timezone.utc);f=market_features(e,x[1][0])
    market=f['market_prices']
    if change=='prior_timestamp':market['prior_closes'][-1]['at']='2026-10-29T21:00:00Z'
    if change=='opening_timestamp':market['opening']['at']='2026-11-02T15:30:00Z'
    if change=='previous_close':market['prior_closes'][-1]['price']*=2
    if change=='opening_price':market['opening']['price']*=2
    if change=='return':f['prior_returns'][0]=.1
    if change=='gap':f['earnings_gap']=.5
    if change=='availability':market['available_at']=f['features_available_at']='2026-11-02T13:00:00Z'
    with pytest.raises(ValueError):ev.begin_inference(x[0],e['event_id'],f,TEXT)
    assert not ev.attempt_path(x[0],e['event_id']).exists()


def test_sec_acceptance_lag_misses_admission_deadline(experiment):
    x=experiment;x[1][0]=datetime(2026,11,2,15,tzinfo=timezone.utc)
    x[5].update(sec_accepted_at=x[1][0].isoformat(),first_seen_at=x[1][0].isoformat())
    assert admit(x)['status']=='SKIPPED_LATE_DISCOVERY'
    assert not ev.census(x[0])[0]


def test_holiday_and_early_close_use_frozen_exchange_calendar(experiment,monkeypatch):
    from pathlib import Path
    x=experiment;cfg=deepcopy(x[3])
    cfg['calendar']=json.loads((Path(ev.__file__).parent/'experiments/earnings-v2-calendar-reference.json').read_text())
    monkeypatch.setattr(ev,'POLICY',dict(ev.POLICY,holdout_end='2026-11-28T00:00:00Z'))
    folder=x[0]/'holiday';ev.register(folder,cfg)
    x[1][0]=datetime(2026,11,26,12,tzinfo=timezone.utc)
    candidate=dict(x[5],release_at='2026-11-25T22:00:00Z',sec_accepted_at='2026-11-26T11:00:00Z',first_seen_at=x[1][0].isoformat())
    e=ev.discover(folder,x[1][0].isoformat(),x[4],[candidate])[0]
    assert e['status']=='ADMITTED'
    assert ev.stamp(e['earnings_open_at'])==ev.stamp('2026-11-27T14:30:00Z')
    assert ev.stamp(e['cutoff'])==ev.stamp('2026-11-27T18:00:00Z')
    assert ev.stamp(e['prior_price_dates'][-1])==ev.stamp('2026-11-25T21:00:00Z')
    assert ev.stamp(e['session_dates'][0])==ev.stamp('2026-11-30T21:00:00Z')
    x[1][0]=datetime(2026,11,27,17,tzinfo=timezone.utc)
    f=market_features(e,x[1][0]);intent=ev.begin_inference(folder,e['event_id'],f,TEXT)
    p=ev.seal(folder,e['event_id'],f,signed_response(x,e,f,intent))
    assert ev.stamp(p['sealed_at'])<ev.stamp(e['cutoff'])
    x[1][0]=datetime(2026,11,27,18,1,tzinfo=timezone.utc)
    with pytest.raises(ValueError,match='Outside inference window'):
        ev.begin_inference(folder,e['event_id'],market_features(e,x[1][0]),TEXT)


def test_complete_thirty_event_five_issuer_engineering_cohort(experiment):
    x=experiment;candidates=[]
    # Deliberately synthetic simultaneous reporting of six fiscal periods.
    # Tests population arithmetic, not plausible trading opportunities.
    for symbol in ev.POLICY['universe'][:5]:
        source=x[3]['sources'][symbol]
        for period in ('2025-03-31','2025-06-30','2025-09-30','2025-12-31','2026-03-31','2026-06-30'):
            candidates.append(dict(x[5],symbol=symbol,cik=source['cik'],fiscal_period_end=period,
                accession=symbol+period,release_url=source['issuer_url']+'/cohort'))
    events=ev.discover(x[0],x[1][0].isoformat(),x[4],candidates)
    assert len(events)==30 and all(e['status']=='ADMITTED' for e in events)
    for hour in range(16):
        x[1][0]=datetime(2026,11,2,hour,tzinfo=timezone.utc);ev.discover(x[0],x[1][0].isoformat(),x[4],[])
    x[1][0]=datetime(2026,11,2,16,tzinfo=timezone.utc);outcomes=[]
    for e in events:
        f=market_features(e,x[1][0]);intent=ev.begin_inference(x[0],e['event_id'],f,TEXT)
        p=ev.seal(x[0],e['event_id'],f,signed_response(x,e,f,intent))
        o=outcome(e);sigma=math.sqrt(p['prediction']['candidate'])
        o['closes']=[100*math.exp(sigma*i) for i in range(6)];outcomes.append(o)
    complete_discovery(x);x[1][0]=datetime(2026,11,10,tzinfo=timezone.utc)
    result=ev.evaluate(x[0],outcomes)
    assert result['events']==30 and result['issuers']==5 and result['engineering_criteria_met'] is True
    assert result['accepted'] is False and result['evidence_class']=='engineering_smoke'
    assert result['profitability_validated'] is False
    assert result['inference_attempts']=={'SUCCEEDED':30,'FAILED':0}


@pytest.mark.parametrize('n,issuers,gain,positive,expected',[(30,5,.1,5,True),(29,5,.1,5,False),(30,4,.1,4,False),(30,5,.03,5,False),(30,5,.5,3,False)])
def test_engineering_acceptance_threshold_boundaries(n,issuers,gain,positive,expected):
    rows=[dict(symbol=str(i%issuers),baseline=10,candidate=10*(1-gain) if i%issuers<positive else 11) for i in range(n)]
    assert ev.acceptance_metrics(rows)['criteria_met'] is expected


def test_terminal_accounting_includes_failed_and_unfinished_requests(experiment):
    x=experiment;e=admit(x);f,_=prediction(x,e)
    with pytest.raises(ValueError,match='Incomplete inference terminal'):ev.inference_accounting(x[0],ev.journal(x[0]))
    ev.fail_inference(x[0],e['event_id'],'synthetic timeout')
    assert ev.inference_accounting(x[0],ev.journal(x[0]))=={'SUCCEEDED':0,'FAILED':1}
    with pytest.raises(FileExistsError):ev.begin_inference(x[0],e['event_id'],f,TEXT)


def test_amendment_after_request_produces_terminal_failure(experiment):
    x=experiment;e=admit(x);f,r=prediction(x,e)
    x[1][0]=datetime(2026,11,2,17,tzinfo=timezone.utc)
    corrected=dict(x[5],form='8-K/A',source_sha256='correction',first_seen_at=x[1][0].isoformat(),correction_kind='official_withdrawal')
    ev.discover(x[0],x[1][0].isoformat(),x[4],[corrected])
    with pytest.raises(KeyError):ev.seal(x[0],e['event_id'],f,r)
    assert ev.inference_accounting(x[0],ev.journal(x[0]))=={'SUCCEEDED':0,'FAILED':1}


@pytest.mark.parametrize('field,value',[('network_attempt_limit',2),('sdk_retries',1),('sign_only_journaled_attempts',False),('record_terminal_failures',False),('timestamp_custody_reference','')])
def test_registration_requires_fixed_first_attempt_recorder_contract(experiment,tmp_path,field,value):
    cfg=deepcopy(experiment[3]);cfg['recorder_contract'][field]=value
    with pytest.raises(ValueError,match='recorder contract'):ev.register(tmp_path/'bad-contract',cfg)



def test_signer_failure_retains_original_response_and_blocks_retry(experiment):
    x=experiment;e=admit(x);x[1][0]=datetime(2026,11,2,16,tzinfo=timezone.utc);f=market_features(e,x[1][0])
    def sign(_):raise RuntimeError('synthetic signing outage')
    with pytest.raises(RuntimeError):ev.execute_inference(x[0],e['event_id'],f,TEXT,lambda *_:'original raw response',sign)
    raw=json.loads((x[0]/('inference-response-'+ev.digest(e['event_id'])+'.json')).read_text())
    assert raw['response_raw']=='original raw response'
    assert ev.inference_accounting(x[0],ev.journal(x[0]))=={'SUCCEEDED':0,'FAILED':1}


def test_elapsed_deadline_after_durable_intent_never_calls_transport(experiment,monkeypatch):
    x=experiment;e=admit(x);x[1][0]=datetime(2026,11,2,16,tzinfo=timezone.utc);f=market_features(e,x[1][0])
    begin=ev.begin_inference
    def delayed(*args):
        intent=begin(*args);x[1][0]=datetime(2026,11,2,21,1,tzinfo=timezone.utc);return intent
    monkeypatch.setattr(ev,'begin_inference',delayed)
    with pytest.raises(ValueError,match='deadline elapsed'):
        ev.execute_inference(x[0],e['event_id'],f,TEXT,lambda *_:pytest.fail('must not send after deadline'),lambda _:None)
    assert ev.inference_accounting(x[0],ev.journal(x[0]))=={'SUCCEEDED':0,'FAILED':1}
