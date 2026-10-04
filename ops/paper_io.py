"""Shared read-only paper data access; never logs credentials or response bodies."""
import json
import os
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/var/lib/nwt-ops')


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, default=str, indent=2, allow_nan=False)); tmp.replace(path)


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class Paper:
    def __init__(self):
        from dotenv import load_dotenv
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        load_dotenv(ROOT/'nwt_agents/.env')
        self.base = os.environ['NWT_ALPACA_BASE_URL'].rstrip('/')
        if self.base != 'https://paper-api.alpaca.markets': raise ValueError('Paper endpoint required')
        self.session = requests.Session()
        self.session.headers.update({'APCA-API-KEY-ID':os.environ['NWT_ALPACA_KEY_ID'],
            'APCA-API-SECRET-KEY':os.environ['NWT_ALPACA_SECRET_KEY']})
        self.session.mount('https://', HTTPAdapter(max_retries=Retry(total=2, backoff_factor=1,
            status_forcelist=[429,500,502,503,504], allowed_methods=['GET'],respect_retry_after_header=False)))

    def get(self, path, params=None, data=False):
        base = 'https://data.alpaca.markets' if data else self.base
        response = self.session.get(base+path, params=params, timeout=20)
        if not response.ok: raise RuntimeError('Paper read failed: HTTP '+str(response.status_code))
        return response.json()

    def db(self):
        import psycopg2
        return psycopg2.connect(os.environ['NWT_DB_DSN'], connect_timeout=10,options='-c statement_timeout=30000')
