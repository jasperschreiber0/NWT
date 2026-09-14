"""Replay exact canonical decisions; reconstructed rows retain that designation."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'nwt_agents'))
from shared_context import get_db
from opportunity_outcomes import opportunity_id_for, upsert_outcome
from psycopg2.extras import RealDictCursor


def main():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT d.* FROM nwt_decision_inputs d WHERE d.symbol IS NOT NULL '
                        "AND d.symbol <> '' AND NOT EXISTS (SELECT 1 FROM nwt_opportunity_outcomes o "
                        "WHERE o.source_decision_id=d.id AND o.lane='RAW_SHADOW') ORDER BY d.created_at")
            rows = cur.fetchall()
        for row in rows:
            oid = opportunity_id_for(row['strategy_id'], row['symbol'], row['run_date'],
                                     row['track'], row.get('genome_version'), row.get('poll_slot', ''))
            upsert_outcome(conn, oid, 'RAW_SHADOW', dict(
                strategy_id=row['strategy_id'], symbol=row['symbol'], direction=row.get('direction'),
                track=row['track'], regime=row.get('regime'), entry_price=row.get('entry_price_ref'),
                decision=row.get('decision'), decision_reason=row.get('rejection_reason') or row.get('outcome_reason'),
                source_ticket_id=str(row['ticket_id']) if row.get('ticket_id') else None,
                source_decision_id=str(row['id'])))
            with conn.cursor() as cur:
                cur.execute("UPDATE nwt_opportunity_outcomes SET reconstructed_at=COALESCE(reconstructed_at,NOW()) "
                            "WHERE opportunity_id=%s AND lane='RAW_SHADOW'", (oid,))
            conn.commit()
        print('Reconstructed canonical decisions:', len(rows))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
