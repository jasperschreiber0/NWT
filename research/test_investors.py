import sys,json,sqlite3
from pathlib import Path
from datetime import datetime,timedelta,timezone
import pytest
sys.path.insert(0,str(Path(__file__).parent))
import investor_core as core
import investor_pipeline as pipeline
import investor_options
sys.path.insert(0,str(Path(__file__).parents[1]/'ops'))
import install_investor_research as installer
from research_health import observation_job

def ownership(code='P',footnote='',planned='0',owner='123',price='50',form='4'):
    return f'''<ownershipDocument><documentType>{form}</documentType><aff10b5One>{planned}</aff10b5One>
    <issuer><issuerCik>1234</issuerCik><issuerTradingSymbol>TEST</issuerTradingSymbol></issuer>
    <reportingOwner><reportingOwnerId><rptOwnerCik>{owner}</rptOwnerCik></reportingOwnerId><reportingOwnerRelationship><isOfficer>1</isOfficer></reportingOwnerRelationship></reportingOwner>
    <nonDerivativeTable><nonDerivativeTransaction><transactionDate><value>2026-10-09</value></transactionDate>
    <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding><transactionAmounts>
    <transactionShares><value>1500</value></transactionShares><transactionPricePerShare><value>{price}</value><footnoteId id="F1"/></transactionPricePerShare>
    <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
    <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></nonDerivativeTransaction></nonDerivativeTable>
    <footnotes><footnote id="F1">{footnote}</footnote></footnotes></ownershipDocument>'''.encode()

def db():
    c=sqlite3.connect(':memory:');c.execute('CREATE TABLE evidence(kind TEXT,key TEXT,payload TEXT,PRIMARY KEY(kind,key))')
    c.execute('CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT)');return c

def test_real_purchase_and_exclusions():
    assert core.form4(ownership())[0]['eligible']
    for kwargs in [dict(code='A'),dict(planned='1'),dict(footnote='Private placement'),dict(footnote='10b5-1 plan'),dict(price='NaN'),dict(form='4/A')]:
        assert not core.form4(ownership(**kwargs))[0]['eligible']
    with pytest.raises(ValueError):core.xml(b'<!DOCTYPE x [<!ENTITY a "bad">]><x/>')

def test_feed_dedup_empty_and_external_url_rejected():
    entry='<entry><title>4 test</title><link href="https://www.sec.gov/Archives/edgar/data/1/a-index.htm"/><id>0000000001-26-000001</id><updated>2026-10-09T17:00:00-04:00</updated><category term="4"/></entry>'
    assert len(core.feed(('<feed>'+entry+entry+'</feed>').encode()))==1
    assert core.feed(b'<feed/>')==[]
    with pytest.raises(ValueError):core.feed(('<feed>'+entry.replace('www.sec.gov','evil.test')+'</feed>').encode())

def test_joint_owners_duplicate_and_baseline_do_not_create_cluster():
    now=datetime(2026,10,10,tzinfo=timezone.utc)
    def p(key,owners,baseline=False):return dict(eligible=True,event_id=key,owners=owners,baseline=baseline,
        symbol='TEST',dollars=75000,transaction_date='2026-10-09',first_seen_at=now.isoformat())
    assert core.cluster([p('a',['1','2']),p('b',['2'])],now)==[]
    assert core.cluster([p('a',['1']),p('b',['2'],True)],now)==[]
    assert len(core.cluster([p('a',['1']),p('b',['2'])],now))==1

def test_flow_adjustment_and_split_exclusion():
    a={'fund':'ARKK','date':'2026-10-08','holdings':{str(i):dict(symbol='S'+str(i),cusip=str(i),shares=100,value=1000) for i in range(25)}}
    b=json.loads(json.dumps(a));b['date']='2026-10-09'
    for v in b['holdings'].values():v['shares']=110
    assert core.ark_changes(a,b)==[]
    b['holdings']['1']['shares']=115;b['holdings']['2']['shares']=200
    changes=core.ark_changes(a,b)
    assert len(changes)==1 and changes[0]['symbol']=='S1' and changes[0]['verified_purchase'] is False

def test_13f_and_activist_evidence():
    b=b'<informationTable><infoTable><nameOfIssuer>A</nameOfIssuer><cusip>123456789</cusip><value>300</value><shrsOrPrnAmt><sshPrnamt>20</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt><putCall>PUT</putCall></infoTable></informationTable>'
    assert core.holdings13f(b)[0]['put_call']=='PUT'
    a=core.activist(b'<edgarSubmission><issuerCik>123</issuerCik><item4>We may nominate a director and request a dividend.</item4></edgarSubmission>')
    assert set(a['action_evidence'])=={'board_change','capital_return'}
    assert a['issuer_cik']=='123' and a['classification'].startswith('KEYWORD')
    amendment=core.activist(b'<edgarSubmission><issuerCik>123</issuerCik><item5>Changed ownership</item5></edgarSubmission>')
    assert amendment['item4_present'] is False and amendment['action_evidence']=={}

def test_forecasts_wait_for_future_open_dedup_and_liquidity():
    c=db();now=datetime(2026,10,9,15,tzinfo=timezone.utc)
    p=dict(core.form4(ownership())[0],event_id='a',baseline=False,first_seen_at=now.isoformat())
    pipeline.put(c,'purchase','a',p)
    calendar=[];d=datetime(2026,10,9)
    while len(calendar)<30:
        if d.weekday()<5:calendar.append(dict(date=d.date().isoformat(),open='09:30',close='16:00'))
        d+=timedelta(days=1)
    assert pipeline.propose(c,now,calendar,{'TEST'},{})==0
    context={'TEST':dict(eligible=True,last_session_return=.01)}
    assert pipeline.propose(c,now,calendar,{'TEST'},context)==2
    assert pipeline.propose(c,now,calendar,{'TEST'},context)==0
    pred=pipeline.rows(c,'forecast')[0]
    assert pred['entry']=='2026-10-12' and core.stamp(pred['frozen_at'])<core.stamp(pred['entry_at'])
    data={s:{pred['entry']:{'o':100},pred['exit']:{'o':110}} for s in ['TEST','SPY']}
    assert core.forward_score(pred,data,now) is None
    result=core.forward_score(pred,data,core.stamp(pred['exit_at'])+timedelta(minutes=20))
    assert result['net']<.10 and result['stress_net']<result['net'] and result['excess']==0
    pred['frozen_at']=pred['entry_at']
    with pytest.raises(ValueError):core.forward_score(pred,data,now)

def test_installer_and_research_isolation():
    old='0 * * * * untouched\n';new=installer.schedule(old)
    assert installer.schedule(new)==new and new.startswith(old)
    jobs=installer.configuration({'engine':{'keep':True}})
    assert jobs['engine']=={'keep':True} and observation_job('investor-research',jobs['investor-research'])
    assert not observation_job('investor-research',dict(cwd='.',args=['execution/engine.py']))

def test_model_requires_exact_evidence_and_handles_no_action():
    purpose='We have no current plans to change the board.'
    value=dict(action_present=False,action='none',direction='uncertain',evidence_quote=purpose)
    assert core.validate_catalyst(value,purpose)['action_present'] is False
    with pytest.raises(ValueError):core.validate_catalyst(dict(value,evidence_quote='We will replace the board immediately.'),purpose)
    with pytest.raises(ValueError):core.validate_catalyst(dict(value,action_present='true'),purpose)

def test_option_comparison_costs_and_expiry_buffer():
    now=datetime(2026,10,12,13,35,tzinfo=timezone.utc)
    def q(b,a):return dict(bp=b,ap=a,bs=10,**{'as':10},t=now.isoformat())
    chain={'TEST261113P00098000':{'latestQuote':q(.8,.85)},'TEST261113P00097000':{'latestQuote':q(.2,.25)}}
    pred=dict(key='x',exit='2026-10-19')
    book=investor_options.choose_book(chain,q(99.99,100.01),pred,now)
    assert len(book['spreads'])==1 and book['spreads'][0]['entry']['max_loss_with_assumed_fees']<100
    closed=investor_options.close_book(book,chain,q(99.99,100.01),now)
    assert closed['equity_model_dollars']<0 and closed['spreads'][0]['net_model_dollars']<0
    book=investor_options.choose_book(chain,q(99.99,100.01),dict(pred,exit='2026-11-10'),now)
    assert book['spreads']==[]


def test_bootstrap_feed_is_context_and_later_filing_is_fresh():
    c=db();now=datetime(2026,10,12,12,tzinfo=timezone.utc)
    class Public:
        accession='0000000001-26-000001'
        def get(self,url):
            return ('<feed><entry><title>4 test</title><link href="https://www.sec.gov/Archives/edgar/data/1/a-index.htm"/>'
                '<id>'+self.accession+'</id><updated>2026-10-12T11:00:00Z</updated><category term="4"/></entry></feed>').encode(),dict(observed_at=now.isoformat())
    public=Public();pipeline.collect_feed(c,public,'4',now);pipeline.collect_feed(c,public,'4',now)
    assert len(pipeline.rows(c,'filing'))==1 and pipeline.rows(c,'filing')[0]['baseline'] is True
    public.accession='0000000001-26-000002';pipeline.collect_feed(c,public,'4',now)
    assert pipeline.one(c,'filing',public.accession)['baseline'] is False


def test_same_person_across_sources_is_not_independent_agreement():
    c=db();now=datetime(2026,10,10,tzinfo=timezone.utc)
    for key,owner in [('a','1'),('b','2')]:
        p=dict(core.form4(ownership(owner=owner))[0],event_id=key,baseline=False,first_seen_at=now.isoformat())
        pipeline.put(c,'purchase',key,p)
    pipeline.put(c,'activist','x',dict(event_id='x',baseline=False,changed=True,symbol='TEST',reporters=['1'],first_seen_at=now.isoformat()))
    pipeline.put(c,'classification','x',dict(action_present=True,direction='up',historical_context_only=False))
    assert 'independent_agreement' not in [p['rule'] for p in pipeline.candidates(c,now)]
    pipeline.put(c,'activist','y',dict(event_id='y',baseline=False,changed=True,symbol='TEST',reporters=['3'],first_seen_at=now.isoformat()))
    pipeline.put(c,'classification','y',dict(action_present=True,direction='up',historical_context_only=False))
    assert 'independent_agreement' in [p['rule'] for p in pipeline.candidates(c,now)]


def test_document_failure_stays_visible_during_backoff():
    c=db();now=datetime(2026,10,12,12,tzinfo=timezone.utc)
    pipeline.put(c,'filing','x',dict(accession='x',form='4',baseline=False,first_seen_at=now.isoformat(),url='https://www.sec.gov/Archives/edgar/data/x'))
    class Failed:
        calls=0
        def get(self,url):self.calls+=1;raise RuntimeError('Source unavailable')
    public=Failed();a=pipeline.parse_filings(c,public,now,{})
    b=pipeline.parse_filings(c,public,now+timedelta(minutes=5),{})
    assert a['errors']==b['errors'] and len(b['errors'])==1 and public.calls==1


def test_options_observation_roundtrip_is_get_only_and_idempotent(monkeypatch):
    c=db();now=datetime(2026,10,12,13,35,tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return now
    monkeypatch.setattr(investor_options,'datetime',Clock)
    def q(b,a):return dict(bp=b,ap=a,bs=10,**{'as':10},t=now.isoformat())
    chain=lambda:{'TEST261113P00098000':{'latestQuote':q(.8,.85)},'TEST261113P00097000':{'latestQuote':q(.2,.25)}}
    class ReadOnly:
        calls=[]
        def get(self,path,params,data=False):
            self.calls.append(path)
            if '/stocks/' in path:return dict(quote=q(99.99,100.01))
            if '/snapshots/' in path:return dict(snapshots=chain())
            return dict(quotes={s:v['latestQuote'] for s,v in chain().items()})
    paper=ReadOnly()
    pipeline.put(c,'forecast','x',dict(key='x',symbol='TEST',entry='2026-10-12',exit='2026-10-19',
        entry_at='2026-10-12T13:30:00+00:00',exit_at='2026-10-19T13:30:00+00:00'))
    run=lambda:investor_options.run(c,paper,now,pipeline.rows,pipeline.one,pipeline.put)
    assert run()['books_with_options']==1
    calls=len(paper.calls);run();assert len(paper.calls)==calls
    now=datetime(2026,10,19,13,35,tzinfo=timezone.utc)
    assert run()['completed']==1
    evidence=pipeline.one(c,'option_exit_quote_evidence','x')
    assert pipeline.one(c,'option_result','x')['source_sha256']==core.digest(dict(chain=evidence['chain'],quote=evidence['quote']))
    calls=len(paper.calls);run();assert len(paper.calls)==calls
