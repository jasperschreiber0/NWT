"""Public investor evidence parsers. No order authority or inferred execution times."""
import csv,io,hashlib,json,math,re,statistics
from datetime import datetime,timedelta,timezone
from urllib.parse import urljoin,urlparse
from xml.etree import ElementTree as ET
from investor_diagnostics import ResearchValidationError

UTC=timezone.utc
LEGACY_VERSION='investor-observation-20261011-v1'
PREVIOUS_VERSION='investor-observation-20261013-v2'
DIAGNOSTICS_VERSION='investor-observation-20261014-v3'
VERSION='investor-observation-20261023-v4'
POLICY=dict(version=VERSION,minimum_purchase=25000,cluster_total=100000,cluster_owners=2,
    cluster_days=7,hold_sessions=[5,20],cost_per_side=.0015,stress_per_side=.003,
    minimum_stock_price=5,minimum_average_dollar_volume20=5000000,agreement_max_last_session_move=.03,
    activist_model='gpt-4.1-mini-2025-04-14',maximum_model_attempts_per_day=40,
    classification_protocol='numbered-source-passages-v1',classification_batch_size=4,
    classification_source_char_limit=30000,classification_attempts_per_filing=2,
    promotion='RESEARCH_ONLY; no automatic broker qualification',
    limitations=['13F positions are delayed and incomplete portfolios',
        'ARK holdings changes are flow-adjusted proxies, not verified transactions',
        'Public disclosure does not reveal an investor complete hedge or entry price'])

def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def stamp(v):return datetime.fromisoformat(v.replace('Z','+00:00')).astimezone(UTC)
def number(v):
    n=float(re.sub(r'[$,%\s]','',str(v)))
    if not math.isfinite(n):raise ValueError('Nonfinite number')
    return n
def xml(blob):
    if re.search(br'<!\s*(DOCTYPE|ENTITY)',blob,re.I):raise ValueError('XML declarations forbidden')
    r=ET.fromstring(blob)
    for e in r.iter():e.tag=e.tag.split('}')[-1]
    return r
def txt(root,path):
    e=root.find(path)
    return ''.join(e.itertext()).strip() if e is not None else ''

def feed(blob):
    r=xml(blob)
    if r.tag!='feed':raise ValueError('Invalid SEC feed')
    result={}
    for e in r.findall('entry'):
        link=e.find('link');category=e.find('category');ident=txt(e,'id')
        accession=re.search(r'\d{10}-\d{2}-\d{6}',ident)
        if link is None or category is None or not accession:raise ValueError('Incomplete filing metadata')
        url=link.attrib['href'];published=txt(e,'updated')
        if urlparse(url).hostname!='www.sec.gov' or '/Archives/edgar/data/' not in url:raise ValueError('Non-SEC filing link')
        stamp(published)
        result[accession.group()]=dict(accession=accession.group(),url=url,form=category.attrib['term'],
            published_at=stamp(published).isoformat(),title=txt(e,'title'))
    return list(result.values())

def form4(blob):
    r=xml(blob)
    if r.tag!='ownershipDocument':raise ValueError('Not ownership XML')
    owners=[]
    for e in r.findall('reportingOwner'):
        if txt(e,'reportingOwnerRelationship/isOfficer') in ('1','true') or txt(e,'reportingOwnerRelationship/isDirector') in ('1','true'):
            cik=txt(e,'reportingOwnerId/rptOwnerCik').lstrip('0')
            if cik.isdigit():owners.append(cik)
    owners=sorted(set(owners));symbol=txt(r,'issuer/issuerTradingSymbol').strip().upper()
    footnotes={e.attrib.get('id'):''.join(e.itertext()).strip() for e in r.findall('footnotes/footnote')}
    planned=txt(r,'aff10b5One') in ('1','true')
    results=[]
    for i,e in enumerate(r.findall('nonDerivativeTable/nonDerivativeTransaction')):
        code=txt(e,'transactionCoding/transactionCode')
        refs=[f.attrib.get('id') for f in e.iter('footnoteId')]
        note=' '.join(footnotes.get(k,'') for k in refs)+' '+txt(r,'remarks')
        reasons=[]
        if any(k not in footnotes for k in refs):reasons.append('Referenced footnote missing')
        if txt(r,'documentType')!='4':reasons.append('Amendment: no new purchase signal')
        if code!='P' or txt(e,'transactionAmounts/transactionAcquiredDisposedCode/value')!='A':reasons.append('Not a P-coded acquisition')
        if not owners:reasons.append('No executive/director owner')
        if planned or re.search(r'10b5[\s-]*1|purchase plan|private|placement|subscription|directed.share|employee.stock',note,re.I):reasons.append('Planned/private/employee transaction or ambiguous footnote')
        if txt(e,'ownershipNature/directOrIndirectOwnership/value')!='D':reasons.append('Indirect ownership excluded')
        try:
            qty=number(txt(e,'transactionAmounts/transactionShares/value'));price=number(txt(e,'transactionAmounts/transactionPricePerShare/value'))
            if qty<=0 or price<=0:raise ValueError('Invalid purchase')
            amount=qty*price
        except ValueError:qty=price=amount=0.;reasons.append('Price or quantity unavailable')
        day=txt(e,'transactionDate/value')
        try:datetime.strptime(day,'%Y-%m-%d')
        except ValueError:reasons.append('Invalid transaction date')
        if amount<POLICY['minimum_purchase']:reasons.append('Below purchase threshold')
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}',symbol):reasons.append('No supported ticker')
        results.append(dict(row=i,symbol=symbol,issuer_cik=txt(r,'issuer/issuerCik'),owners=owners,
            transaction_date=day,code=code,shares=qty,price=price,dollars=amount,
            eligible=not reasons,reasons=reasons,footnote_excerpt=note[:1200],
            venue='P code covers open-market OR private purchases; explicit private/plan evidence excluded, venue not independently verified'))
    return results

def activist(blob):
    r=xml(blob)
    if r.tag!='edgarSubmission':raise ValueError('Not structured 13D XML')
    # Structured Schedule 13D schemas retain Item 4 even when rendered HTML differs.
    items=[e for e in r.iter() if e.tag.lower() in ('item4','purposeoftransaction')]
    body=' '.join(' '.join(e.itertext()) for e in items)
    body=re.sub(r'\s+',' ',body).strip()
    tags={
        'board_change':r'board|director|nomina',
        'capital_return':r'repurchase|buyback|dividend|return.of.capital',
        'restructuring':r'restructur|spin.off|divest|sale.of|strategic.alternative',
        'transaction':r'merger|acquisition|tender.offer|take.private',
    }
    evidence={}
    for label,pattern in tags.items():
        m=re.search(pattern,body,re.I)
        if m:evidence[label]=body[max(0,m.start()-100):min(len(body),m.end()+250)]
    def first(names):
        return next((''.join(e.itertext()).strip() for e in r.iter() if e.tag.lower() in names),'')
    return dict(issuer_cik=first({'issuercik'}),issuer=first({'issuername'}),cusip=first({'cusipnumber','issuercusipnumber'}),
        item4_present=bool(body),
        purpose=body[:30000],purpose_sha256=hashlib.sha256(body.encode()).hexdigest(),
        action_evidence=evidence,classification='KEYWORD_HYPOTHESIS; conditional/negated intent requires review',
        reporters=sorted(set(''.join(e.itertext()).strip().lstrip('0') for e in r.iter() if e.tag.lower() in {'reportingpersoncik','reportingpersonciknumber'})))

def holdings13f(blob):
    r=xml(blob)
    if r.tag.lower()!='informationtable':raise ValueError('Not 13F information table')
    positions=[]
    for e in r.findall('infoTable'):
        positions.append(dict(issuer=txt(e,'nameOfIssuer'),cusip=txt(e,'cusip'),class_title=txt(e,'titleOfClass'),
            shares=number(txt(e,'shrsOrPrnAmt/sshPrnamt')),unit=txt(e,'shrsOrPrnAmt/sshPrnamtType'),
            put_call=txt(e,'putCall'),reported_value=number(txt(e,'value'))))
    if not positions:raise ValueError('Empty 13F information table')
    return positions

def validate_catalyst(value,purpose):
    actions=['board_change','capital_return','restructuring','transaction','none']
    if not isinstance(value,dict) or type(value.get('action_present')) is not bool or value.get('action') not in actions or value.get('direction') not in ['up','down','uncertain']:
        raise ResearchValidationError('CLASSIFICATION_SCHEMA')
    quote=value.get('evidence_quote','')
    if not isinstance(quote,str) or not 10<=len(quote)<=1200 or quote not in purpose:
        raise ResearchValidationError('CLASSIFICATION_SOURCE_QUOTE')
    if value['action_present'] and value['action']=='none':raise ResearchValidationError('CLASSIFICATION_ACTION')
    return dict(action_present=value['action_present'],action=value['action'],direction=value['direction'],
        evidence_quote=quote,label='GROUNDED_MODEL_HYPOTHESIS_NOT_VERIFIED_OUTCOME')

def source_passages(purpose):
    """Lossless source slices: the model selects an ID rather than rewriting text."""
    result=[];start=0
    while start<len(purpose):
        end=min(start+1000,len(purpose))
        if end<len(purpose):
            boundary=purpose.rfind(' ',start+500,end)
            if boundary>start:end=boundary+1
        result.append(dict(id=len(result)+1,start=start,end=end,text=purpose[start:end]))
        start=end
    return result


def grounded_catalyst(answer,purpose):
    if not isinstance(answer,dict):raise ResearchValidationError('CLASSIFICATION_SCHEMA')
    ident=answer.get('evidence_id');passages=source_passages(purpose)
    if type(ident) is not int or not 1<=ident<=len(passages):
        raise ResearchValidationError('CLASSIFICATION_SOURCE_QUOTE')
    passage=passages[ident-1]
    # Evidence text is constructed exclusively from the immutable filing.
    result=validate_catalyst(dict(answer,evidence_quote=passage['text']),purpose)
    if not result['action_present'] and result['action']!='none':
        raise ResearchValidationError('CLASSIFICATION_ACTION')
    return dict(result,evidence_id=ident,evidence_start=passage['start'],evidence_end=passage['end'])


def ark_csv(blob,fund):
    rows=list(csv.DictReader(io.StringIO(blob.decode('utf-8-sig'))));holdings={};dates=set()
    if not rows or not {'date','fund','ticker','shares','market value ($)'}.issubset(rows[0]):raise ValueError('ARK schema changed')
    for r in rows:
        if r.get('fund')!=fund:continue
        day=datetime.strptime(r['date'],'%m/%d/%Y').date().isoformat();dates.add(day)
        ticker=r.get('ticker','').strip();cusip=r.get('cusip','').strip()
        if not ticker or not cusip:continue
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}',ticker):continue
        key=cusip+':'+ticker
        if key in holdings:raise ValueError('Duplicate ARK security')
        holdings[key]=dict(symbol=ticker,cusip=cusip,shares=number(r['shares']),value=number(r['market value ($)']))
    if len(dates)!=1 or len(holdings)<10:raise ValueError('Incomplete ARK snapshot')
    return dict(fund=fund,date=dates.pop(),holdings=holdings)

def ark_changes(before,after):
    if before['fund']!=after['fund'] or not before['date']<after['date']:return []
    a=before['holdings'];b=after['holdings'];common=set(a)&set(b)
    ratios=[b[k]['shares']/a[k]['shares'] for k in common if a[k]['shares']>0 and b[k]['shares']>0]
    if len(ratios)<20:return []
    scale=statistics.median(ratios)
    if not .8<=scale<=1.2:return []
    result=[]
    for k in sorted(common):
        if a[k]['shares']<=0 or b[k]['shares']<=0:continue
        ratio=b[k]['shares']/a[k]['shares'];relative=ratio/scale-1
        # Large discontinuities need verified corporate-action data, not guessed adjustment.
        if .8<=ratio<=1.2 and .02<=relative<=.15:
            result.append(dict(symbol=b[k]['symbol'],cusip=b[k]['cusip'],relative_change=relative,
                fund_flow_scale_estimate=scale,kind='RELATIVE_HOLDINGS_INCREASE_PROXY',verified_purchase=False,
                corporate_actions_verified=False))
    return result

def cluster(purchases,now):
    qualifying=[p for p in purchases if p.get('eligible') and not p.get('baseline') and
        0<=(now-stamp(p['first_seen_at'])).total_seconds()<=POLICY['cluster_days']*86400 and
        0<=(now.date()-datetime.fromisoformat(p['transaction_date']).date()).days<=POLICY['cluster_days']]
    by_symbol={}
    for p in qualifying:by_symbol.setdefault(p['symbol'],[]).append(p)
    result=[]
    for symbol,rows in by_symbol.items():
        owners=set();groups=[]
        for p in sorted(rows,key=lambda p:(-p['dollars'],p['event_id'])):
            group=set(p['owners'])
            if group and not group&owners:groups.append(p);owners.update(group)
        if len(groups)>=2 and sum(p['dollars'] for p in groups)>=POLICY['cluster_total']:
            result.append(dict(symbol=symbol,events=sorted(p['event_id'] for p in groups),
                actors=sorted('cik:'+owner for owner in owners),dollars=sum(p['dollars'] for p in groups)))
    return result

def forward_score(pred,bars,now):
    if stamp(pred['frozen_at'])>=stamp(pred['entry_at']):raise ValueError('Late forecast')
    if now<stamp(pred['exit_at'])+timedelta(minutes=15):return None
    en,ex=pred['entry'],pred['exit'];symbol=pred['symbol']
    if any(d not in bars.get(s,{}) for s in [symbol,'SPY'] for d in [en,ex]):return None
    def net(s,cost):
        a=number(bars[s][en]['o']);b=number(bars[s][ex]['o'])
        if a<=0 or b<=0:raise ValueError('Invalid forward price')
        return b/a*(1-cost)/(1+cost)-1
    value=net(symbol,.0015);benchmark=net('SPY',.0015)
    return dict(net=value,stress_net=net(symbol,.003),benchmark=benchmark,excess=value-benchmark,
        entry=en,exit=ex,label='MODELLED_UNDERLYING_RETURN_NOT_BROKER_PROFIT',
        source_sha256=digest({s:{d:bars[s][d] for d in [en,ex]} for s in {symbol,'SPY'}}))
