"""
nwt_agents/backfill_ledger_qty.py
One-off backfill: populate nwt_portfolio_ledger.qty for existing open
positions from Alpaca's filled_qty on each row's alpaca_order_id.

Run once after db/migrate_2026_07_ledger_qty.sql, before the next
recon_agent.py --gate run — otherwise open rows with qty=NULL will
re-trigger the qty_mismatch class recon now checks for.

Usage: python3 backfill_ledger_qty.py
"""

import logging
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv(Path(__file__).parent / ".env")

from shared_context import clean_alpaca_base_url, get_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("backfill_ledger_qty")

ALPACA_BASE_URL = clean_alpaca_base_url(os.environ.get("NWT_ALPACA_BASE_URL", "https://paper-api.alpaca.markets"))
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("NWT_ALPACA_KEY_ID", ""),
    "APCA-API-SECRET-KEY": os.environ.get("NWT_ALPACA_SECRET_KEY", ""),
}


def main() -> None:
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT position_id, asset, alpaca_order_id
                FROM nwt_portfolio_ledger
                WHERE status = 'open' AND qty IS NULL AND alpaca_order_id IS NOT NULL
                """
            )
            rows = cur.fetchall()

        if not rows:
            logger.info("No open rows with qty=NULL — nothing to backfill")
            return

        updated = 0
        skipped = 0
        for row in rows:
            order_id = row["alpaca_order_id"]
            try:
                resp = requests.get(
                    f"{ALPACA_BASE_URL}/v2/orders/{order_id}",
                    headers=ALPACA_HEADERS,
                    timeout=15,
                )
                resp.raise_for_status()
                filled_qty = float(resp.json().get("filled_qty") or 0)
            except Exception as exc:
                logger.warning("Order lookup failed for %s (position %s, %s): %s",
                               order_id, row["position_id"], row["asset"], exc)
                skipped += 1
                continue

            if filled_qty <= 0:
                logger.warning("No filled_qty for order %s (position %s, %s) — skipping",
                               order_id, row["position_id"], row["asset"])
                skipped += 1
                continue

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE nwt_portfolio_ledger SET qty = %s WHERE position_id = %s",
                    (filled_qty, row["position_id"]),
                )
            conn.commit()
            logger.info("Backfilled %s (position %s): qty=%.0f", row["asset"], row["position_id"], filled_qty)
            updated += 1

        logger.info("Backfill complete: %d updated, %d skipped", updated, skipped)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
