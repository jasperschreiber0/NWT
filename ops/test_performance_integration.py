import json
import sqlite3
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import unified_performance as report


class Cursor:
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def execute(self,*a):pass
    def fetchall(self):return []


class Database:
    def cursor(self,**k):return Cursor()
    def set_session(self,**k):pass
    def close(self):pass


class Paper:
    def get(self,path,*a):
        if path=='/v2/account':return {'equity':'1000','cash':'1000'}
        return []
    def db(self):return Database()


def test_first_baseline_and_next_report_decimal_serialization(tmp_path,monkeypatch,capsys):
    class Clock(datetime):
        current=datetime(2026,10,20,tzinfo=timezone.utc)
        @classmethod
        def now(cls,tz=None):return cls.current
    monkeypatch.setattr(report,'Paper',Paper)
    monkeypatch.setattr(report,'STATE',tmp_path)
    monkeypatch.setattr(report,'datetime',Clock)
    report.main()
    initial=json.loads(capsys.readouterr().out)
    assert initial['operating_costs']['complete'] is False
    assert initial['operating_costs']['missing']==['initial_baseline_no_elapsed_window']
    with sqlite3.connect(tmp_path/'performance/snapshots.sqlite') as db:
        assert db.execute('SELECT count(*) FROM snapshots').fetchone()==(1,)
    Clock.current+=timedelta(days=1)
    # Exercise real reporting/serialization with exact accounting output.
    monkeypatch.setattr(report,'expenses',lambda *a,**k:dict(complete=False,accrued_usd=Decimal('0.123456789123456789'),missing=['invoice']))
    report.main()
    stdout=json.loads(capsys.readouterr().out)
    disk=json.loads((tmp_path/'unified-performance.json').read_text())
    with sqlite3.connect(tmp_path/'performance/snapshots.sqlite') as db:
        stored=json.loads(db.execute('SELECT payload FROM snapshots ORDER BY observed DESC LIMIT 1').fetchone()[0])
    for output in (stdout,disk,stored):
        assert output['operating_costs']['accrued_usd']=='0.123456789123456789'
