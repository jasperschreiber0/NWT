import base64
import hashlib
import json
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
             recorder_identity_reference='TEST-ONLY',source_map_reference='TEST-ONLY',model_revision_reference='TEST-ONLY',extraction_version='test-v1',evidence_class='engineering_smoke')
    ev.register(tmp_path,cfg)
    checks={s:{name:dict(url=v[name+'_url'],status='OK',sha256='fixture-source') for name in ('sec','issuer')} for s,v in cfg['sources'].items()}
    candidate=dict(symbol='AAPL',cik='1',fiscal_period_end='2026-09-30',form='8-K',item='2.02',release_at='2026-11-01T23:00:00Z',
                   first_seen_at=clock[0].isoformat(),timestamp_verified=True,release_url='https://aapl.example.test/releases/1',
                   source_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),accession='fixture-accession')
    return tmp_path,clock,key,cfg,checks,candidate


def admit(x):
    folder,clock,_,_,checks,c=x
    return ev.discover(folder,clock[0].isoformat(),checks,[c])[0]


def prediction(x,event):
    folder,clock,key,cfg,_,_=x
    for hour in range(16):
        clock[0]=datetime(2026,11,2,hour,tzinfo=timezone.utc)
        ev.discover(folder,clock[0].isoformat(),x[4],[])
    clock[0]=datetime(2026,11,2,16,tzinfo=timezone.utc)
    features=dict(prior_returns=[.01]*20,prior_variance=.0001,earnings_gap=.02,features_available_at=clock[0].isoformat(),data_source_reference='fixture-prices',inference_cost_usd=None)
    req=dict(cfg['settings'],model=cfg['model'],input=ev.POLICY['score_prompt']+'\nSOURCE_TEXT:\n'+TEXT)
    response=dict(id='fixture-response-id',created_at=clock[0].timestamp(),model=cfg['model'],status='completed',output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(dict(uncertainty_score=.8,quote='unresolved demand uncertainty')))])])
    body=dict(event_id=event['event_id'],features=features,source_text=TEXT,request_raw=json.dumps(req),response_raw=json.dumps(response),requested_at=clock[0].isoformat(),received_at=clock[0].isoformat())
    receipt=dict(body=body,signature=base64.b64encode(key.sign(ev.canonical(body))).decode())
    return features,receipt


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
