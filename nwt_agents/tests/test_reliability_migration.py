import os
from pathlib import Path
import pytest
import psycopg2


def test_real_uuid_attribution_and_repeatable_migration():
    dsn=os.getenv('NWT_TEST_DB_DSN')
    if not dsn:pytest.skip('isolated Postgres required')
    root=Path(__file__).resolve().parents[2]
    with psycopg2.connect(dsn) as c:
        with c.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS nwt_entry_intents,nwt_opportunity_outcomes CASCADE')
            cur.execute('CREATE TABLE IF NOT EXISTS nwt_tickets(ticket_id UUID PRIMARY KEY)')
            cur.execute((root/'db/migrate_2026_09_opportunity_outcomes.sql').read_text())
            cur.execute((root/'db/migrate_2026_09_reliability_20260915.sql').read_text())
            cur.execute((root/'db/migrate_2026_09_reliability_20260915.sql').read_text())
        from opportunity_outcomes import upsert_outcome
        upsert_outcome(c,'11111111-1111-1111-1111-111111111111','RAW_SHADOW',dict(
            strategy_id='C1',symbol='QQQ',source_decision_id='22222222-2222-2222-2222-222222222222'))
        with c.cursor() as cur:
            cur.execute('SELECT source_decision_id::text FROM nwt_opportunity_outcomes')
            assert cur.fetchone()[0]=='22222222-2222-2222-2222-222222222222'
