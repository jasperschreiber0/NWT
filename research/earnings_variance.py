"""One prospective variance hypothesis. Offline only; never invokes a model.

Registration and prediction receipts are exclusive-create files. Evaluation is
single-use and journals failures as well as success; no historical independence
is inferred from the date on an old issuer document.
"""
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

POLICY = dict(version='earnings-variance-20261020-v1',
    hypothesis='Unresolved earnings uncertainty increases next-five-session realised variance',
    universe=['AAPL','MSFT','AMZN','GOOGL','META','NVDA','TSLA','AMD','INTC','CRM',
              'ADBE','ORCL','NFLX','JPM','BAC','GS','WMT','COST','HD','KO','PEP','XOM','CVX','UNH','JNJ'],
    holdout_start='2026-11-02T00:00:00+00:00',holdout_end='2027-04-01T00:00:00+00:00',
    horizon_sessions=5,min_events=30,min_issuers=5,
    baseline='prior_20_session_mean_squared_log_return + earnings_open_gap_squared / 5',
    candidate='baseline * (1 + 0.5 * uncertainty_score)',
    score_prompt='Using only supplied issuer text, score unresolved forward earnings uncertainty from 0 to 1; return score and exact supporting quote. Do not use outside knowledge.',
    outcome='mean squared close-to-close log return over next five exchange sessions after prediction cutoff',
    acceptance='At least 5% lower mean QLIKE; positive issuer-level improvement in >=70% of issuers; at least 30 events and 5 issuers; no missing eligible outcomes',
    cost_policy='Prediction accuracy only, no executable profit claim; incremental inference budget zero. Record incurred inference cost or explicit unknown.',
    historical_policy='Historical model outputs are engineering smoke evidence only; prospective timestamped predictions required',
    trading_authority=False)


def stamp(s):
    d=datetime.fromisoformat(s.replace('Z','+00:00')) if isinstance(s,str) else s
    if not isinstance(d,datetime) or d.tzinfo is None:raise ValueError('Timezone required')
    return d.astimezone(timezone.utc)


def digest(x):
    return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def write_once(path, value):
    with Path(path).open('x',encoding='utf-8') as f:
        json.dump(value,f,sort_keys=True,indent=2)


def register(folder, input_manifest=None):
    """Must happen before holdout. No fitting or selection on holdout data."""
    now=datetime.now(timezone.utc)
    if now>=stamp(POLICY['holdout_start']):raise ValueError('Registration must precede holdout')
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    value=dict(policy=POLICY,policy_sha256=digest(POLICY),registered_at=now.isoformat(),
               input_manifest=input_manifest, input_manifest_sha256=digest(input_manifest),
               code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    write_once(folder/'registration.json',value)
    return value


def registration(folder):
    r=json.loads((Path(folder)/'registration.json').read_text(encoding='utf-8'))
    if r['policy_sha256']!=digest(POLICY) or r['code_sha256']!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError('Frozen policy or harness changed; new experiment required')
    if r['input_manifest_sha256']!=digest(r['input_manifest']):raise ValueError('Input manifest changed')
    return r


def forecast(features):
    """Fixed coefficients; no historical or holdout fitting."""
    prior=float(features['prior_variance']);gap=float(features['earnings_gap']);u=float(features['uncertainty_score'])
    if not all(math.isfinite(x) for x in (prior,gap,u)) or prior<=0 or not 0<=u<=1:
        raise ValueError('Invalid features')
    base=prior+gap*gap/5
    return {'baseline':base,'candidate':base*(1+.5*u)}


def seal_prediction(folder, features):
    """Public cached source text and existing output only. No inference calls."""
    folder=Path(folder);r=registration(folder);now=datetime.now(timezone.utc)
    # Every submission attempt gets a receipt, including rejected submissions.
    attempt=folder/('attempt-'+now.strftime('%Y%m%dT%H%M%S%f')+'.json')
    write_once(attempt,dict(at=now.isoformat(),action='seal',input_sha256=digest(features)))
    manifest=r['input_manifest']
    if not manifest or not manifest.get('source_reference'):raise ValueError('Frozen issuer-event and exchange-calendar manifest required')
    event=manifest['events'].get(features.get('event_id'))
    if not event or any(features.get(k)!=event[k] for k in ('symbol','cutoff','earnings_open_at','prior_session_dates','session_dates')):
        raise ValueError('Event or session selection differs from frozen manifest')
    allowed={'event_id','symbol','cutoff','source_text','source_reference','source_published_at',
             'features_available_at','model_output_at','model','prior_variance','earnings_gap',
             'uncertainty_score','quote','inference_cost_usd','prior_returns','prior_session_dates',
             'earnings_open_at','session_dates','data_source_reference'}
    if set(features)-allowed:raise ValueError('Unexpected fields (possible label leakage)')
    cutoff=stamp(features['cutoff'])
    if not stamp(r['policy']['holdout_start'])<=cutoff<stamp(r['policy']['holdout_end']):raise ValueError('Outside holdout')
    if not stamp(r['registered_at'])<=now<=cutoff:raise ValueError('Prediction must be sealed before cutoff')
    for name in ('source_published_at','features_available_at','model_output_at'):
        if not stamp(r['registered_at'])<=stamp(features[name])<=now:raise ValueError('Not independently prospective: '+name)
    if features['symbol'] not in POLICY['universe']:raise ValueError('Outside fixed universe')
    if not features.get('source_reference') or not features.get('data_source_reference') or not features.get('model'):
        raise ValueError('Missing source/model identity')
    quote=features.get('quote')
    if not isinstance(quote,str) or len(quote)<10 or quote not in features['source_text']:raise ValueError('Ungrounded quote')
    returns=features['prior_returns']
    if len(returns)!=20 or any(not math.isfinite(float(x)) for x in returns):raise ValueError('20 prior returns required')
    if not math.isclose(sum(float(x)**2 for x in returns)/20,float(features['prior_variance']),rel_tol=1e-9):raise ValueError('Prior variance mismatch')
    prior_dates=features['prior_session_dates']
    if (len(prior_dates)!=20 or len(set(prior_dates))!=20 or prior_dates!=sorted(prior_dates) or
        stamp(prior_dates[-1])>=stamp(features['earnings_open_at']) or
        stamp(features['earnings_open_at'])>now or stamp(features['source_published_at'])>stamp(features['model_output_at'])):
        raise ValueError('Prior prices or earnings/model timing invalid')
    dates=features['session_dates']
    if len(dates)!=5 or len(set(dates))!=5 or dates!=sorted(dates) or stamp(dates[0])<=cutoff:
        raise ValueError('Five future ordered exchange-session closes required')
    cost=features.get('inference_cost_usd')
    if cost is not None and (not math.isfinite(float(cost)) or float(cost)<0):raise ValueError('Invalid inference cost')
    value=dict(features=features,sealed_at=now.isoformat(),policy_sha256=r['policy_sha256'],
               prediction=forecast(features),evidence_class='prospective',source_sha256=digest(features['source_text']))
    # Event identity prevents repeated predictions/selection; never overwrite.
    write_once(folder/('prediction-'+digest(features['event_id'])+'.json'),value)
    return value


def evaluate(folder, outcomes):
    """Single predeclared final read; missing observations block acceptance."""
    folder=Path(folder);r=registration(folder);now=datetime.now(timezone.utc)
    if now<stamp(POLICY['holdout_end']):raise ValueError('Holdout not complete')
    write_once(folder/'evaluation-attempt.json',dict(at=now.isoformat(),outcomes_sha256=digest(outcomes)))
    predictions=[json.loads(p.read_text(encoding='utf-8')) for p in sorted(folder.glob('prediction-*.json'))]
    ids=[x['event_id'] for x in outcomes]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate outcomes')
    by_id={x['event_id']:x for x in outcomes};rows=[]
    manifest=r['input_manifest']
    if not manifest or set(by_id)!=set(manifest['events']):raise ValueError('Incomplete frozen event population')
    if set(by_id)!={p['features']['event_id'] for p in predictions}:raise ValueError('Missing or extraneous holdout outcomes')
    for p in predictions:
        f=p['features'];o=by_id[f['event_id']]
        if (p['policy_sha256']!=r['policy_sha256'] or p['prediction']!=forecast(f) or
            p['source_sha256']!=digest(f['source_text']) or stamp(p['sealed_at'])>stamp(f['cutoff'])):
            raise ValueError('Prediction receipt mismatch')
        if o['session_dates']!=f['session_dates'] or stamp(o['session_dates'][-1])>now:raise ValueError('Outcome session mismatch/not mature')
        closes=o['closes']
        if len(closes)!=6 or any(not math.isfinite(float(x)) or float(x)<=0 for x in closes):raise ValueError('Six verified adjusted closes required')
        if not o.get('source_reference') or o.get('corporate_actions_checked') is not True:raise ValueError('Unverified price/corporate-action evidence')
        target=sum(math.log(float(b)/float(a))**2 for a,b in zip(closes,closes[1:]))/5
        scores={name:math.log(v)+target/v for name,v in p['prediction'].items()}
        rows.append(dict(event_id=f['event_id'],symbol=f['symbol'],target=target,**scores))
    issuers=sorted({x['symbol'] for x in rows})
    # QLIKE relative improvement uses nonnegative normalized loss against realised
    # variance (floor avoids undefined log at exact zero).
    def loss(row,key):
        p=next(p for p in predictions if p['features']['event_id']==row['event_id'])['prediction'][key]
        ratio=max(row['target'],1e-12)/p
        return ratio-math.log(ratio)-1
    bl=sum(loss(x,'baseline') for x in rows);cl=sum(loss(x,'candidate') for x in rows)
    improved=sum(sum(x['baseline']-x['candidate'] for x in rows if x['symbol']==s)>0 for s in issuers)
    accepted=manifest.get('evidence_class')=='prospective_source_verified' and len(rows)>=POLICY['min_events'] and len(issuers)>=POLICY['min_issuers'] and bl>0 and cl<=.95*bl and improved>=math.ceil(.7*len(issuers))
    result=dict(events=len(rows),issuers=len(issuers),baseline_qlike=bl,candidate_qlike=cl,accepted=accepted,
                rows=rows,profitability_validated=False,policy_sha256=r['policy_sha256'],
                costs_complete=all(p['features'].get('inference_cost_usd') is not None for p in predictions))
    write_once(folder/'evaluation.json',result)
    return result
