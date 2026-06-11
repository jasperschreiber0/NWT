"""
iv_pipeline/store.py
nwt_iv_history read/write (Postgres, database nwt_agents).

One row per (ticker, date). The daily snapshot job upserts; layer0 and the
rank signals read the series back. Schema in db/migrate_iv_history.sql.
"""

import logging
from datetime import date
from typing import Optional

logger = logging.getLogger("iv_pipeline.store")

UPSERT_SQL = """
INSERT INTO nwt_iv_history
  (ticker, date, atm_iv_30d, atm_iv_60d, term_slope, put_skew_25d,
   hv_20d, hv_iv_spread, source, fetched_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
ON CONFLICT (ticker, date) DO UPDATE SET
  atm_iv_30d = EXCLUDED.atm_iv_30d,
  atm_iv_60d = EXCLUDED.atm_iv_60d,
  term_slope = EXCLUDED.term_slope,
  put_skew_25d = EXCLUDED.put_skew_25d,
  hv_20d = EXCLUDED.hv_20d,
  hv_iv_spread = EXCLUDED.hv_iv_spread,
  source = EXCLUDED.source,
  fetched_at = NOW()
"""


def upsert_snapshot(
    conn,
    ticker: str,
    snapshot_date: date,
    atm_iv_30d: Optional[float],
    atm_iv_60d: Optional[float],
    term_slope: Optional[float],
    put_skew_25d: Optional[float],
    hv_20d: Optional[float],
    hv_iv_spread: Optional[float],
    source: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(UPSERT_SQL, (
            ticker, snapshot_date, atm_iv_30d, atm_iv_60d, term_slope,
            put_skew_25d, hv_20d, hv_iv_spread, source,
        ))
    conn.commit()
    logger.info("nwt_iv_history upsert %s %s: atm_iv_30d=%s", ticker,
                snapshot_date, atm_iv_30d)


def get_iv_series(conn, ticker: str, max_days: int = 252) -> list[float]:
    """atm_iv_30d series for a ticker, oldest first, most recent max_days rows."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT atm_iv_30d FROM (
                SELECT date, atm_iv_30d FROM nwt_iv_history
                WHERE ticker = %s AND atm_iv_30d IS NOT NULL
                ORDER BY date DESC LIMIT %s
            ) recent ORDER BY date ASC
            """,
            (ticker, max_days),
        )
        rows = cur.fetchall()
    return [float(r[0]) for r in rows]


def get_latest_snapshot(conn, ticker: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticker, date, atm_iv_30d, atm_iv_60d, term_slope,
                   put_skew_25d, hv_20d, hv_iv_spread, source, fetched_at
            FROM nwt_iv_history WHERE ticker = %s
            ORDER BY date DESC LIMIT 1
            """,
            (ticker,),
        )
        row = cur.fetchone()
    if not row:
        return None
    keys = ("ticker", "date", "atm_iv_30d", "atm_iv_60d", "term_slope",
            "put_skew_25d", "hv_20d", "hv_iv_spread", "source", "fetched_at")
    return dict(zip(keys, row))
