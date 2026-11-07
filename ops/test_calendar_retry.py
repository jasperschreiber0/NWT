import ast
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
import requests
from health_checks import calendar_read, HealthCheckError


class CalendarTests(unittest.TestCase):
    def test_recovered_connection_timeout_visible(self):
        calls = []
        def fetch(timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise requests.ConnectTimeout('SECRET')
            return []
        value, receipt = calendar_read(fetch, lambda: 0)
        self.assertEqual(value, [])
        self.assertEqual(calls, [15, 15])
        self.assertEqual(receipt['recovered_errors'], ['ConnectTimeout'])
        self.assertEqual(receipt['attempts'], 2)

    def test_persistent_failure_blocks_and_redacts(self):
        calls = []
        def fetch(timeout):
            calls.append(timeout)
            raise requests.ConnectTimeout('SECRET')
        with self.assertRaises(HealthCheckError) as caught:
            calendar_read(fetch, lambda: 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(caught.exception.details()['attempts'], 2)
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_other_errors_not_retried(self):
        for error in [requests.ReadTimeout, requests.HTTPError, requests.ConnectionError, ValueError]:
            calls = []
            def fetch(timeout):
                calls.append(timeout)
                raise error('SECRET')
            with self.subTest(error=error), self.assertRaises(HealthCheckError):
                calendar_read(fetch, lambda: 0)
            self.assertEqual(len(calls), 1)

    def test_no_stale_cache(self):
        calendar_read(lambda timeout: [{'date':'2026-11-03'}], lambda: 0)
        def fail(timeout):
            raise requests.ConnectTimeout()
        with self.assertRaises(HealthCheckError):
            calendar_read(fail, lambda: 0)

    def test_late_response_rejected(self):
        ticks = iter([0, 0, 35])
        with self.assertRaisesRegex(HealthCheckError, 'DeadlineExceeded'):
            calendar_read(lambda timeout: [], lambda: next(ticks))

    def test_deadline_prevents_retry(self):
        ticks = iter([0, 0, 36, 36])
        calls = []
        def fail(timeout):
            calls.append(timeout)
            raise requests.ConnectTimeout()
        with self.assertRaisesRegex(HealthCheckError, 'DeadlineExceeded'):
            calendar_read(fail, lambda: next(ticks))
        self.assertEqual(len(calls), 1)

    def test_concurrent_calls_have_independent_receipts(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda n: calendar_read(lambda timeout: [n], lambda: 0), [1,2]))
        self.assertEqual([x[0] for x in results], [[1],[2]])
        self.assertTrue(all(x[1]['attempts']==1 for x in results))

    def fetch_function(self, data, status=200):
        # Actual nested function body, isolated from watchdog imports/environment.
        tree = ast.parse(Path(__file__).with_name('watchdog.py').read_text())
        fn = next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='fetch_calendar')
        calls=[]
        def get(*args, **kwargs):
            calls.append((args,kwargs))
            return SimpleNamespace(status_code=status, raise_for_status=lambda: None, json=lambda:data)
        ns={'requests':SimpleNamespace(get=get),'base':'https://paper-api.alpaca.markets',
            'h':{'synthetic':'only'},'datetime':datetime,'now':datetime(2026,11,27,tzinfo=timezone.utc)}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<actual-calendar-body>','exec'),ns)
        return ns['fetch_calendar'], calls

    def test_holiday_and_early_close(self):
        for data in [[],[{'date':'2026-11-27','open':'09:30','close':'13:00'}]]:
            fn,calls=self.fetch_function(data)
            self.assertEqual(fn(15),data)
            self.assertEqual(calls[0][1]['params'],{'start':'2026-11-27','end':'2026-11-27'})
            self.assertFalse(calls[0][1]['allow_redirects'])

    def test_stale_malformed_and_invalid_hours_block(self):
        for data in [{},[{'date':'2026-11-26'}],[{'date':'2026-11-27','open':'16:00','close':'09:30'}],
                     [{'date':'2026-11-27','open':'bad','close':'16:00'}],[{},{}]]:
            fn,_=self.fetch_function(data)
            with self.subTest(data=data), self.assertRaises(HealthCheckError):
                calendar_read(fn,lambda:0)

    def test_redirect_not_a_holiday(self):
        fn,_=self.fetch_function([],302)
        with self.assertRaises(HealthCheckError):
            calendar_read(fn,lambda:0)

if __name__=='__main__':
    unittest.main()
