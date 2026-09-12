import unittest,tempfile,json,sys,ast
from pathlib import Path
from datetime import datetime,timedelta,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'china'))
from event_core import *
from observation import assess

class EventTests(unittest.TestCase):
    def test_first_seen_cannot_be_rewritten(self):
        with tempfile.TemporaryDirectory() as p:
            c=connect(Path(p)/'test.sqlite')
            self.assertTrue(put(c,'event','a',{'v':1},'2026-01-01T00:00:00+00:00'))
            self.assertFalse(put(c,'event','a',{'v':2},'2027-01-01T00:00:00+00:00'))
            self.assertEqual(json.loads(c.execute('SELECT payload FROM records').fetchone()[0]),{'v':1});c.close()

    def test_baseline_old_unknown_and_future_are_not_prospective(self):
        now=datetime(2026,9,12,tzinfo=UTC)
        self.assertFalse(fresh_event({'published_raw':(now-timedelta(hours=1)).isoformat()},now,True))
        for value in [None,'bad','2026-09-09T00:00:00Z','2026-09-13T00:00:00Z']:
            self.assertFalse(fresh_event({'published_raw':value},now,False))
        self.assertTrue(fresh_event({'published_raw':(now-timedelta(hours=1)).isoformat()},now,False))
        self.assertTrue(fresh_event({'published_date':'2026-09-12'},now,False))
        self.assertFalse(fresh_event({'published_date':'2026-09-12'},now,True))
        self.assertFalse(fresh_event({'published_date':'2026-09-01'},now,False))
        self.assertFalse(fresh_event({'published_date':'2026-09-13'},now,False))

    def test_no_fabricated_evidence_or_consensus(self):
        answer={'event_type':'monetary_policy','impacts':[{'symbol':'SPY','direction':'up','evidence_quote':'cut rates'}],'consensus':'made up'}
        with self.assertRaises(ValueError):validate_classification(answer,'held rates')
        self.assertIsNone(validate_classification(answer,'cut rates')['consensus'])

    def test_quotes_reject_bad_or_stale_values(self):
        now=datetime.now(UTC);q={'bp':100,'ap':101,'t':now.isoformat()}
        self.assertEqual(quote_check(q,now),'OK')
        for change in [{'ap':99},{'bp':0},{'ap':200},{'ap':float('nan')},{'t':(now-timedelta(minutes=3)).isoformat()},{'t':(now+timedelta(seconds=1)).isoformat()}]:
            self.assertNotEqual(quote_check(dict(q,**change),now),'OK')
        self.assertEqual(quote_check(None,now),'MISSING_QUOTE')

    def test_freeze_before_real_market_open_and_signed_costs(self):
        now=datetime(2026,9,14,13,0,tzinfo=UTC)
        entry={'date':'2026-09-14','open':'2026-09-14T13:30:00Z'};exit_={'date':'2026-09-15','open':'2026-09-15T13:30:00Z'}
        self.assertIsNone(make_prediction('e','SPY','r',1,entry,exit_,now+timedelta(hours=1)))
        p=make_prediction('e','SPY','r',1,entry,exit_,now)
        b={'2026-09-14':{'o':100},'2026-09-15':{'o':110}}
        self.assertIsNone(score(p,b,b,now))
        result=score(p,b,b,datetime(2026,9,15,14,tzinfo=UTC))
        self.assertAlmostEqual(result['return_pct'],9.895)
        self.assertAlmostEqual(result['excess_pct'],0)
        p['direction']=-1
        self.assertAlmostEqual(score(p,b,b,datetime(2026,9,15,14,tzinfo=UTC))['return_pct'],-10.105)

    def test_rss_and_atom(self):
        self.assertEqual(parse_feed('<rss><channel><item><title>A</title><link>https://x</link><pubDate>Fri, 11 Sep 2026 12:00:00 GMT</pubDate></item></channel></rss>')[0]['title'],'A')
        self.assertEqual(parse_feed('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>B</title><link href="https://x"/><updated>2026-09-11T12:00:00Z</updated></entry></feed>')[0]['url'],'https://x')
        rdf='<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><item><title>C</title><dc:date>2026-09-11T12:00:00Z</dc:date></item></rss>'
        self.assertEqual(parse_feed(rdf)[0]['published_raw'],'2026-09-11T12:00:00Z')
        with self.assertRaises(ValueError):parse_feed('<html/>')

    def test_calendar_observes_daylight_saving(self):
        result=sessions([{'date':'2026-09-14','open':'09:30','close':'16:00'}, {'date':'2026-12-14','open':'09:30','close':'16:00'}])
        self.assertEqual(dt(result[0]['open']).hour,13)
        self.assertEqual(dt(result[1]['open']).hour,14)

    def test_china_supported_data_survives_missing_tencent(self):
        now=datetime(2026,9,11,15,tzinfo=UTC)
        calendar=[{'date':f'2026-09-{i:02d}','open':'09:30','close':'16:00'} for i in range(4,12)]
        bars={s:[{'t':f'2026-09-{i:02d}T04:00:00Z','c':100+i} for i in range(4,11)] for s in ['FXI','KWEB','MCHI','BABA']}
        quotes={s:{'bp':100,'ap':101,'t':now.isoformat()} for s in bars}
        assets={s:{'tradable':True} for s in bars}
        r=assess(bars,quotes,assets,calendar,now,True)
        self.assertTrue(r['symbols']['FXI']['would_select']);self.assertIsNone(r['broad_stimulus'])
        self.assertFalse(r['execution_enabled']);self.assertIn('TCEHY',r['missing_evidence'])
        quotes.pop('MCHI');r=assess(bars,quotes,assets,calendar,now,True)
        self.assertFalse(r['symbols']['MCHI']['would_select']);self.assertTrue(r['symbols']['FXI']['would_select'])
        self.assertFalse(assess(bars,quotes,assets,calendar,now,False)['symbols']['FXI']['would_select'])

    def test_china_executor_returns_before_any_legacy_calls(self):
        source=(Path(__file__).resolve().parents[1]/'china/executor.py').read_text()
        node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='main')
        # The only preceding statement is an informational log, not a DB or broker call.
        self.assertIsInstance(node.body[1],ast.Return)

if __name__=='__main__':unittest.main()
