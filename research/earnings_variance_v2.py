"""Prospective rolling census and signed original inference receipts. No network I/O.

Public keys attest an external capture service, not the model's truthfulness.
Production registration remains blocked until source/recorder identities are verified.
"""
import base64
import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import earnings_variance as baseline_dependency
from earnings_variance import POLICY as ORIGINAL, forecast, stamp, digest

PROSPECTIVE_BLOCKERS = (
    'verified runtime issuer/source mappings and market-data provenance are unconfigured',
    'immutable runtime model and effective settings are unconfigured',
    'trusted runtime recorder identity, custody and adapter enforcement are unconfigured',
)

POLICY = dict(ORIGINAL, version='earnings-variance-rolling-v2.1', cadence_seconds=3600,
              admission_lead_seconds=300, receipt_format='signed-responses-api-v2-attempt-bound')


def now():
    return datetime.now(timezone.utc)


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def exclusive(path,value):
    with Path(path).open('xb') as f:
        f.write(canonical(value));f.flush();os.fsync(f.fileno())
    if os.name=='posix':
        directory=os.open(str(Path(path).parent),os.O_RDONLY | getattr(os,'O_DIRECTORY',0))
        try:os.fsync(directory)
        finally:os.close(directory)


def dependency_hashes():
    """Freeze both admission code and the imported policy/formula implementation."""
    return {name:hashlib.sha256(Path(path).read_text(encoding='utf-8').encode()).hexdigest()
            for name,path in {'rolling_harness':__file__,'baseline_policy_and_forecast':baseline_dependency.__file__}.items()}


def code_hash():
    return digest(dependency_hashes())


def register(folder, config):
    """Source maps and recorder key must be reviewed before any holdout use."""
    if now()>=stamp(POLICY['holdout_start']):raise ValueError('Registration too late')
    required={'sources','calendar','model','settings','recorder_public_key','recorder_identity_reference',
              'source_map_reference','model_revision_reference','extraction_version','evidence_class','recorder_contract'}
    if set(config)!=required or any(config[k] is None for k in required):raise ValueError('Unconfigured registration')
    if config['evidence_class']!='engineering_smoke':
        raise ValueError('Prospective registration disabled: '+'; '.join(PROSPECTIVE_BLOCKERS))
    if set(config['sources'])!=set(POLICY['universe']):raise ValueError('Complete fixed issuer map required')
    for source in config['sources'].values():
        if set(source)!={'cik','sec_url','issuer_url'} or not str(source['cik']).isdigit():raise ValueError('Source map')
        if urlparse(source['sec_url']).hostname!='data.sec.gov':raise ValueError('Official SEC source required')
        if urlparse(source['issuer_url']).scheme!='https':raise ValueError('Official issuer source required')
    if not all(config[k] for k in ('model','settings','recorder_public_key','recorder_identity_reference','source_map_reference','model_revision_reference','extraction_version')):raise ValueError('Missing reviewed identity')
    contract=config['recorder_contract']
    if set(contract)!={'reference','network_attempt_limit','sdk_retries','sign_only_journaled_attempts','record_terminal_failures','timestamp_custody_reference'} or not contract['reference'] or not contract['timestamp_custody_reference'] or contract['network_attempt_limit']!=1 or contract['sdk_retries']!=0 or contract['sign_only_journaled_attempts'] is not True or contract['record_terminal_failures'] is not True:raise ValueError('First-attempt recorder contract required')
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(base64.b64decode(config['recorder_public_key'],validate=True))
    if {'model','input'} & set(config['settings']):raise ValueError('Reserved request settings')
    sessions=config['calendar']['sessions']
    if not config['calendar'].get('source_reference') or len(sessions)<26:raise ValueError('Verified calendar required')
    closes=[stamp(s['close']) for s in sessions]
    if closes!=sorted(set(closes)) or any(stamp(s['open'])>=stamp(s['close']) for s in sessions):raise ValueError('Calendar order')
    if closes[20]>=stamp(POLICY['holdout_start']) or len([c for c in closes if c>=stamp(POLICY['holdout_end'])])<5:raise ValueError('Calendar coverage')
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    record=dict(policy=POLICY,config=config,registered_at=now().isoformat(),code_sha256=code_hash(),dependency_sha256=dependency_hashes(),readiness_blockers=PROSPECTIVE_BLOCKERS)
    exclusive(folder/'registration.json',dict(record,sha256=digest(record)))
    return record


def registration(folder):
    r=json.loads((Path(folder)/'registration.json').read_text())
    checksum=r.pop('sha256')
    if checksum!=digest(r) or r['policy']!=POLICY or r['code_sha256']!=code_hash() or r['dependency_sha256']!=dependency_hashes():raise ValueError('Registration changed')
    if r['config']['evidence_class']!='engineering_smoke':raise ValueError('Prospective registration disabled')
    return r


def journal(folder):
    previous=None;rows=[]
    for i,path in enumerate(sorted(Path(folder).glob('journal-*.json'))):
        row=json.loads(path.read_text());checksum=row.pop('sha256')
        if path.name!=f'journal-{i:08d}.json' or row['previous']!=previous or digest(row)!=checksum:raise ValueError('Journal changed')
        previous=checksum;rows.append(row)
    return rows


def append(folder, action, payload):
    rows=journal(folder)
    if rows and now()<stamp(rows[-1]['at']):raise ValueError('Journal clock moved backwards')
    previous=digest(rows[-1]) if rows else None
    row=dict(previous=previous,at=now().isoformat(),action=action,payload=payload)
    exclusive(Path(folder)/f'journal-{len(rows):08d}.json',dict(row,sha256=digest(row)))


def census(folder):
    events={};skips=[]
    for row in journal(folder):
        if row['action']=='discovery':
            for item in row['payload']['events']:
                if item['status']=='ADMITTED':events[item['event_id']]=item
                else:skips.append(item)
                if item['status']=='INVALIDATED_CORRECTION':events.pop(item['event_id'],None)
    return events,skips


def discover(folder,slot,checks,candidates):
    """Import one original source scan. All candidates and skips are retained."""
    r=registration(folder);at=now();slot=stamp(slot)
    append(folder,'discovery_attempt',dict(slot=slot.isoformat(),checks=checks,candidates=candidates,input_sha256=digest([checks,candidates])))
    if slot.minute or slot.second or slot.microsecond or not slot<=at<slot+timedelta(hours=1):raise ValueError('Invalid discovery slot')
    if not stamp(r['registered_at'])<=slot<stamp(POLICY['holdout_end']):raise ValueError('Outside discovery period')
    rows=journal(folder)
    if sum(x['action']=='discovery_attempt' and x['payload']['slot']==slot.isoformat() for x in rows)!=1:raise ValueError('One discovery attempt per slot; failed coverage cannot be retried')
    if set(checks)!=set(POLICY['universe']):raise ValueError('Every issuer must be checked')
    for symbol,check in checks.items():
        for name in ('sec','issuer'):
            c=check[name]
            if c['url']!=r['config']['sources'][symbol][name+'_url'] or c['status'] not in ('OK','FAILED'):raise ValueError('Source check identity')
            if c['status']=='OK' and not c.get('sha256'):raise ValueError('Missing source receipt')
    canonical_statuses={'ADMITTED','SKIPPED_PRE_REGISTRATION','SKIPPED_OUTSIDE_WINDOW','SKIPPED_LATE_DISCOVERY'}
    seen={e['event_id']:e for row in rows if row['action']=='discovery' for e in row['payload']['events'] if e['status'] in canonical_statuses};items=[]
    # First discovery belongs to a source version, not the latest hourly scan.
    first_seen={(e['event_id'],e.get('accession'),e.get('source_sha256')):e.get('first_seen_at')
                for row in rows if row['action']=='discovery' for e in row['payload']['events']
                if e.get('first_seen_at') is not None}
    sessions=r['config']['calendar']['sessions']
    # Earliest original wins even if multiple releases arrive in one scan.
    ordered=sorted(candidates,key=lambda c:(c['symbol'],c['fiscal_period_end'],stamp(c['release_at']) if c.get('release_at') else at))
    for candidate in ordered:
        symbol=candidate['symbol'];event_id=symbol+':'+candidate['fiscal_period_end']
        item=dict(candidate,event_id=event_id,observed_at=at.isoformat())
        identity=(event_id,candidate.get('accession'),candidate.get('source_sha256'))
        if identity in first_seen and stamp(candidate['first_seen_at'])!=stamp(first_seen[identity]):
            raise ValueError('Immutable first_seen changed for source version')
        if candidate.get('first_seen_at') is not None:first_seen[identity]=candidate['first_seen_at']
        if symbol not in POLICY['universe']:item['status']='SKIPPED_OUTSIDE_UNIVERSE'
        elif candidate.get('form') not in ('8-K','8-K/A') or candidate.get('item')!='2.02':item['status']='SKIPPED_NON_EARNINGS'
        elif not candidate.get('release_at') or not candidate.get('sec_accepted_at') or not candidate.get('timestamp_verified'):item['status']='SKIPPED_UNVERIFIED_TIME'
        else:
            source=r['config']['sources'][symbol]
            if str(candidate.get('cik'))!=str(source['cik']) or urlparse(candidate['release_url']).hostname!=urlparse(source['issuer_url']).hostname or not candidate.get('source_sha256') or not candidate.get('accession'):raise ValueError('Unverified candidate source')
            released=stamp(candidate['release_at'])
            if not max(released,stamp(candidate['sec_accepted_at']))<=stamp(candidate['first_seen_at'])<=at:raise ValueError('Discovery timing')
            future=[i for i,s in enumerate(sessions) if stamp(s['open'])>released]
            if not future:raise ValueError('Calendar exhausted')
            i=future[0];cutoff=sessions[i]['close']
            item.update(cutoff=cutoff,earnings_open_at=sessions[i]['open'],
                        prior_session_dates=[s['close'] for s in sessions[max(0,i-20):i]],
                        prior_price_dates=[s['close'] for s in sessions[max(0,i-21):i]],
                        session_dates=[s['close'] for s in sessions[i+1:i+6]])
            if event_id in seen:
                old=seen[event_id]
                if candidate['source_sha256']==old['source_sha256']:item['status']='DUPLICATE'
                elif candidate['form']=='8-K/A' and candidate.get('correction_kind') in ('financial_figures_changed','official_withdrawal'):
                    item['status']='INVALIDATED_CORRECTION' if at<stamp(old['cutoff']) else 'POST_CUTOFF_AMENDMENT'
                else:item['status']='UNRESOLVED_AMENDMENT'
            elif released<stamp(r['registered_at']):item['status']='SKIPPED_PRE_REGISTRATION'
            elif candidate['form']=='8-K/A':item['status']='SKIPPED_AMENDMENT_WITHOUT_ORIGINAL'
            elif not stamp(POLICY['holdout_start'])<=stamp(cutoff)<stamp(POLICY['holdout_end']):item['status']='SKIPPED_OUTSIDE_WINDOW'
            elif at>stamp(sessions[i]['open'])-timedelta(seconds=300):item['status']='SKIPPED_LATE_DISCOVERY'
            elif len(item['prior_price_dates'])!=21 or len(item['prior_session_dates'])!=20 or len(item['session_dates'])!=5:raise ValueError('Event calendar coverage')
            else:item['status']='ADMITTED';seen[event_id]=item
        if item['status'] in canonical_statuses:seen[event_id]=item
        items.append(item)
    append(folder,'discovery',dict(slot=slot.isoformat(),checks=checks,events=items,input_sha256=digest([checks,candidates])))
    return items


def verified_prediction(r,event,features,receipt,admitted_at,sealed_at):
    event_id=event['event_id']
    if sealed_at>stamp(event['cutoff']):raise ValueError('Late seal')
    body=receipt['body']
    if body.get('features')!=features:raise ValueError('Feature receipt binding')
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    key=Ed25519PublicKey.from_public_bytes(base64.b64decode(r['config']['recorder_public_key'],validate=True))
    key.verify(base64.b64decode(receipt['signature'],validate=True),canonical(body))
    if set(body)!={'event_id','source_text','request_raw','response_raw','requested_at','received_at','features','attempt_id','attempt_number'}:raise ValueError('Receipt schema')
    if body['event_id']!=event_id or hashlib.sha256(body['source_text'].encode()).hexdigest()!=event['source_sha256']:raise ValueError('Source binding')
    if not admitted_at<=stamp(features['features_available_at'])<=stamp(body['requested_at'])<=stamp(body['received_at'])<=sealed_at:raise ValueError('Prospective receipt timing')
    if stamp(body['requested_at'])<stamp(event['earnings_open_at']):raise ValueError('Opening gap not yet known')
    request=json.loads(body['request_raw']);response=json.loads(body['response_raw'])
    expected=dict(r['config']['settings'],model=r['config']['model'],input=POLICY['score_prompt']+'\nSOURCE_TEXT:\n'+body['source_text'])
    if request!=expected or response.get('model')!=r['config']['model'] or response.get('status')!='completed' or not response.get('id'):raise ValueError('Model/request identity')
    created=response.get('created_at')
    if not isinstance(created,(int,float)) or not math.isfinite(created) or not int(stamp(body['requested_at']).timestamp())<=created<=stamp(body['received_at']).timestamp():raise ValueError('Response creation time')
    texts=[c['text'] for out in response['output'] if out['type']=='message' for c in out['content'] if c['type']=='output_text']
    if len(texts)!=1:raise ValueError('Ambiguous response')
    score=json.loads(texts[0])
    if isinstance(score.get('uncertainty_score'),bool) or not isinstance(score.get('uncertainty_score'),(int,float)):raise ValueError('Numeric uncertainty score required')
    if set(score)!={'uncertainty_score','quote'} or not isinstance(score['quote'],str) or len(score['quote'])<10 or score['quote'] not in body['source_text']:raise ValueError('Grounding')
    validate_features(event,features,sealed_at)
    returns=features['prior_returns']
    if len(returns)!=20 or not all(math.isfinite(float(x)) for x in returns) or not math.isclose(sum(float(x)**2 for x in returns)/20,features['prior_variance'],rel_tol=1e-9):raise ValueError('Prior variance')
    cost=features['inference_cost_usd']
    if not features['data_source_reference'] or cost is not None and (not math.isfinite(float(cost)) or float(cost)<0):raise ValueError('Cost/source')
    prediction=forecast(dict(features,uncertainty_score=score['uncertainty_score']))
    return prediction


def _seal(folder,event_id,features,receipt):
    """Only parse scores from the original signed response; never accept a score field."""
    r=registration(folder);events,_=census(folder);event=events[event_id]
    append(folder,'prediction_attempt',dict(event_id=event_id,input_sha256=digest([features,receipt])))
    attempts=[x for x in journal(folder) if x['action']=='prediction_attempt' and x['payload']['event_id']==event_id]
    if len(attempts)!=1:raise ValueError('One attempt only')
    if now()>stamp(event['cutoff']):raise ValueError('Prediction too late')
    if set(features)!={'prior_returns','prior_variance','earnings_gap','features_available_at','data_source_reference','inference_cost_usd','market_prices'}:raise ValueError('Unexpected feature fields')
    admitted=next(x for x in journal(folder) if x['action']=='discovery' and any(e['event_id']==event_id and e['status']=='ADMITTED' for e in x['payload']['events']))
    prediction=verified_prediction(r,event,features,receipt,stamp(admitted['at']),now())
    response_id=json.loads(receipt['body']['response_raw'])['id']
    if any(x['action']=='prediction' and json.loads(x['payload']['receipt']['body']['response_raw'])['id']==response_id for x in journal(folder)):raise ValueError('Reused response identity')
    value=dict(event_id=event_id,features=features,receipt=receipt,receipt_sha256=digest(receipt),prediction=prediction,sealed_at=now().isoformat())
    append(folder,'prediction',value)
    return value


def evaluate(folder,outcomes):
    r=registration(folder);events,skips=census(folder)
    if now()<stamp(POLICY['holdout_end']) or any(stamp(e['session_dates'][-1])>now() for e in events.values()):raise ValueError('Outcomes not mature')
    exclusive(Path(folder)/'evaluation-attempt.json',dict(at=now().isoformat(),sha256=digest(outcomes)))
    rows=journal(folder);scans={x['payload']['slot']:x['payload'] for x in rows if x['action']=='discovery'}
    start=stamp(r['registered_at']);slot=start.replace(minute=0,second=0,microsecond=0)
    if slot<start:slot+=timedelta(hours=1)
    while slot<stamp(POLICY['holdout_end']):
        scan=scans.get(slot.isoformat())
        attempts=[x['payload'] for x in rows if x['action']=='discovery_attempt' and x['payload']['slot']==slot.isoformat()]
        if len(attempts)!=1 or not scan or scan.get('input_sha256')!=attempts[0]['input_sha256'] or attempts[0]['input_sha256']!=digest([attempts[0]['checks'],attempts[0]['candidates']]):raise ValueError('Incomplete discovery attempt coverage')
        if not scan or any(c[n]['status']!='OK' for c in scan['checks'].values() for n in ('sec','issuer')):raise ValueError('Incomplete discovery coverage')
        slot+=timedelta(hours=1)
    if any(e['status']=='UNRESOLVED_AMENDMENT' for e in skips):raise ValueError('Unresolved amendment')
    predictions={x['payload']['event_id']:x['payload'] for x in rows if x['action']=='prediction'}
    by_id={o['event_id']:o for o in outcomes}
    if len(by_id)!=len(outcomes) or set(by_id)!=set(events) or not set(events)<=set(predictions):raise ValueError('Incomplete admitted population')
    inference_summary=inference_accounting(folder,rows)
    scores=[]
    for ident,e in events.items():
        p=predictions[ident];o=by_id[ident]
        admitted=next(x for x in rows if x['action']=='discovery' and any(e['event_id']==ident and e['status']=='ADMITTED' for e in x['payload']['events']))
        validate_attempt(folder,r,e,p['features'],p['receipt'],rows,require_success=True)
        expected=verified_prediction(r,e,p['features'],p['receipt'],stamp(admitted['at']),stamp(p['sealed_at']))
        if expected!=p['prediction'] or digest(p['receipt'])!=p['receipt_sha256']:raise ValueError('Prediction receipt changed')
        if o['price_timestamps']!=[e['cutoff']]+e['session_dates'] or not o.get('source_reference') or o.get('corporate_actions_checked') is not True:raise ValueError('Outcome provenance')
        closes=o['closes']
        if len(closes)!=6 or any(not math.isfinite(float(x)) or float(x)<=0 for x in closes):raise ValueError('Outcome prices')
        target=sum(math.log(float(b)/float(a))**2 for a,b in zip(closes,closes[1:]))/5
        loss={k:(max(target,1e-12)/v)-math.log(max(target,1e-12)/v)-1 for k,v in p['prediction'].items()}
        scores.append(dict(event_id=ident,symbol=e['symbol'],**loss))
    metrics=acceptance_metrics(scores)
    accepted=r['config']['evidence_class']=='prospective_source_verified' and metrics['criteria_met']
    result=dict(accepted=accepted,engineering_criteria_met=metrics['criteria_met'],evidence_class=r['config']['evidence_class'],events=len(scores),issuers=metrics['issuers'],metrics=metrics,inference_attempts=inference_summary,scores=scores,skipped_events=skips,profitability_validated=False,
                inference_costs_complete=all(predictions[k]['features']['inference_cost_usd'] is not None for k in events),operating_costs_complete=False)
    exclusive(Path(folder)/'evaluation.json',result)
    return result


def validate_features(event,features,available_by):
    """Recompute every return and the opening gap from bound timestamped prices."""
    required={'prior_returns','prior_variance','earnings_gap','features_available_at','data_source_reference','inference_cost_usd','market_prices'}
    if set(features)!=required:raise ValueError('Unexpected feature fields')
    prices=features['market_prices'];prior=prices['prior_closes'];opening=prices['opening']
    if set(prices)!={'prior_closes','opening','adjustment','corporate_actions_checked','available_at','source_reference'} or any(set(p)!={'at','price'} for p in prior+[opening]):raise ValueError('Market-price schema')
    if ([p['at'] for p in prior]!=event['prior_price_dates'] or
        [p['at'] for p in prior[1:]]!=event['prior_session_dates'] or
        opening['at']!=event['earnings_open_at']):raise ValueError('Market-price timestamps')
    if prices.get('adjustment')!='split_dividend_consistent' or prices.get('corporate_actions_checked') is not True:raise ValueError('Price adjustment evidence')
    if not prices.get('source_reference') or prices['source_reference']!=features['data_source_reference']:raise ValueError('Price source binding')
    if not stamp(event['earnings_open_at'])<=stamp(prices['available_at'])==stamp(features['features_available_at'])<=available_by:raise ValueError('Price availability')
    values=[p['price'] for p in prior]+[opening['price']]
    if len(prior)!=21 or any(isinstance(v,bool) or not math.isfinite(float(v)) or float(v)<=0 for v in values):raise ValueError('Positive finite market prices required')
    returns=[math.log(float(b['price'])/float(a['price'])) for a,b in zip(prior,prior[1:])]
    variance=sum(v*v for v in returns)/20
    gap=math.log(float(opening['price'])/float(prior[-1]['price']))
    if len(features['prior_returns'])!=20 or any(not math.isclose(a,float(b),rel_tol=1e-9,abs_tol=1e-12) for a,b in zip(returns,features['prior_returns'])):raise ValueError('Price-derived returns mismatch')
    if not math.isclose(variance,float(features['prior_variance']),rel_tol=1e-9,abs_tol=1e-12) or not math.isclose(gap,float(features['earnings_gap']),rel_tol=1e-9,abs_tol=1e-12):raise ValueError('Price-derived variance/gap mismatch')
    return dict(prior_returns=returns,prior_variance=variance,earnings_gap=gap)


def attempt_path(folder,event_id,terminal=False):
    return Path(folder)/(('inference-terminal-' if terminal else 'inference-start-')+digest(event_id)+'.json')


def begin_inference(folder,event_id,features,source_text):
    """Fsync a unique request intent BEFORE transport. An orphan intent blocks retry."""
    r=registration(folder);event=census(folder)[0][event_id]
    if not stamp(event['earnings_open_at'])<=now()<=stamp(event['cutoff']):raise ValueError('Outside inference window')
    validate_features(event,features,now())
    if hashlib.sha256(source_text.encode()).hexdigest()!=event['source_sha256']:raise ValueError('Source binding')
    request=dict(r['config']['settings'],model=r['config']['model'],input=POLICY['score_prompt']+'\nSOURCE_TEXT:\n'+source_text)
    identity=digest(dict(registration=digest(r),event_id=event_id,model=r['config']['model'],ordinal=1))
    intent=dict(attempt_id=identity,event_id=event_id,model=r['config']['model'],attempt_number=1,
                created_at=now().isoformat(),request_raw=canonical(request).decode(),features=features,source_sha256=event['source_sha256'])
    exclusive(attempt_path(folder,event_id),intent)
    append(folder,'inference_started',intent)
    return intent


def fail_inference(folder,event_id,reason):
    """Terminal, append-only failure. No timeout/error permits a second request."""
    registration(folder)
    intent=json.loads(attempt_path(folder,event_id).read_text())
    failure=dict(event_id=event_id,attempt_id=intent['attempt_id'],status='FAILED',at=now().isoformat(),reason=str(reason))
    exclusive(attempt_path(folder,event_id,True),failure)
    append(folder,'inference_failed',failure)
    return failure


def validate_attempt(folder,r,event,features,receipt,rows,require_success=False):
    ident=event['event_id'];intent=json.loads(attempt_path(folder,ident).read_text())
    starts=[x for x in rows if x['action']=='inference_started' and x['payload']['event_id']==ident]
    expected=digest(dict(registration=digest(r),event_id=ident,model=r['config']['model'],ordinal=1))
    body=receipt['body']
    if len(starts)!=1 or starts[0]['payload']!=intent or intent['attempt_id']!=expected:raise ValueError('Inference intent mismatch')
    if body.get('attempt_id')!=expected or body.get('attempt_number')!=1 or body['request_raw']!=intent['request_raw'] or features!=intent['features']:raise ValueError('First inference attempt binding')
    if not stamp(intent['created_at'])<=stamp(body['requested_at']):raise ValueError('Request preceded durable intent')
    terminal=attempt_path(folder,ident,True)
    if require_success:
        state=json.loads(terminal.read_text())
        successes=[x for x in rows if x['action']=='inference_succeeded' and x['payload']['event_id']==ident]
        if state.get('attempt_id')!=expected or state.get('status')!='SUCCEEDED' or state.get('receipt_sha256')!=digest(receipt) or len(successes)!=1 or successes[0]['payload']!=state:raise ValueError('Incomplete inference terminal accounting')
    elif terminal.exists():raise ValueError('Inference attempt already terminal')


def seal(folder,event_id,features,receipt):
    r=registration(folder)
    if attempt_path(folder,event_id,True).exists():raise ValueError('One attempt only; inference already terminal')
    try:
        event=census(folder)[0][event_id]
        validate_attempt(folder,r,event,features,receipt,journal(folder))
        value=_seal(folder,event_id,features,receipt)
        success=dict(event_id=event_id,attempt_id=receipt['body']['attempt_id'],status='SUCCEEDED',at=now().isoformat(),receipt_sha256=digest(receipt))
        exclusive(attempt_path(folder,event_id,True),success)
        append(folder,'inference_succeeded',success)
        return value
    except Exception as exc:
        if attempt_path(folder,event_id).exists() and not attempt_path(folder,event_id,True).exists():
            fail_inference(folder,event_id,type(exc).__name__)
        raise


def execute_inference(folder,event_id,features,source_text,transport,sign):
    """Single-call adapter. Callers supply transport and trusted capture signing.

    No provider client/credentials are included. Tests use synthetic callbacks.
    Runtime recorder policy must restrict signing to this durable attempt channel.
    """
    intent=begin_inference(folder,event_id,features,source_text)
    try:
        requested=now().isoformat()
        raw=transport(intent['attempt_id'],intent['request_raw'])
        body=dict(event_id=event_id,attempt_id=intent['attempt_id'],attempt_number=1,
                  source_text=source_text,features=features,request_raw=intent['request_raw'],response_raw=raw,
                  requested_at=requested,received_at=now().isoformat())
        return seal(folder,event_id,features,dict(body=body,signature=sign(canonical(body))))
    except Exception as exc:
        if not attempt_path(folder,event_id,True).exists():fail_inference(folder,event_id,type(exc).__name__)
        raise



def acceptance_metrics(scores):
    """Fixed numerical criteria; meeting them on synthetic data is not acceptance."""
    issuers={s['symbol'] for s in scores}
    bl=sum(s['baseline'] for s in scores);cl=sum(s['candidate'] for s in scores)
    improved=sum(sum(s['baseline']-s['candidate'] for s in scores if s['symbol']==issuer)>0 for issuer in issuers)
    return dict(issuers=len(issuers),baseline_qlike=bl,candidate_qlike=cl,improved_issuers=improved,
                criteria_met=len(scores)>=POLICY['min_events'] and len(issuers)>=POLICY['min_issuers'] and bl>0 and cl<=.95*bl and improved>=math.ceil(.7*len(issuers)))


def inference_accounting(folder,rows):
    starts=[r['payload'] for r in rows if r['action']=='inference_started']
    files=list(Path(folder).glob('inference-start-*.json'))
    if len(files)!=len(starts) or len({s['event_id'] for s in starts})!=len(starts):raise ValueError('Incomplete inference intent journal')
    counts={'SUCCEEDED':0,'FAILED':0}
    for intent in starts:
        event_id=intent['event_id']
        if json.loads(attempt_path(folder,event_id).read_text())!=intent:raise ValueError('Inference intent changed')
        terminal=attempt_path(folder,event_id,True)
        if not terminal.exists():raise ValueError('Incomplete inference terminal accounting')
        state=json.loads(terminal.read_text());status=state.get('status')
        if status not in counts or state.get('attempt_id')!=intent['attempt_id']:raise ValueError('Invalid inference terminal identity')
        action='inference_succeeded' if status=='SUCCEEDED' else 'inference_failed'
        terminals=[r['payload'] for r in rows if r['action'] in ('inference_succeeded','inference_failed') and r['payload']['event_id']==event_id]
        if len(terminals)!=1 or terminals[0]!=state or not any(r['action']==action and r['payload']==state for r in rows):raise ValueError('Incomplete inference terminal journal')
        counts[status]+=1
    return counts
