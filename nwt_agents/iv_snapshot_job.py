"""
nwt_agents/iv_snapshot_job.py
Daily IV history snapshot — cron at 14:30 UTC (after US open) weekdays.

For every ticker in the whitelist universe: compute 30/60-DTE ATM IV,
term slope, 25-delta put skew, hv_20d and hv_iv_spread via the IV
pipeline, then upsert into nwt_iv_history.

This is a DATA job: it runs even when no_trade_mode is set (clean history
is the asset; trading halts must not create holes in the dataset).
Exit code 0 if at least one ticker stored, 1 if all failed.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from iv_pipeline.alpaca_provider import AlpacaIVProvider
from iv_pipeline.pipeline import compute_ticker_iv
from iv_pipeline.provider import IVUnavailableError
from iv_pipeline.store import upsert_snapshot
from shared_context import get_db, log_system_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("iv_snapshot_job")

DEFAULT_UNIVERSE = "SPY,QQQ,AAPL,TSLA,NVDA,VGK,FXI,KWEB,MCHI"


def get_universe() -> list[str]:
    raw = os.environ.get("NWT_IV_UNIVERSE", DEFAULT_UNIVERSE)
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def main() -> None:
    conn = get_db()
    provider = AlpacaIVProvider()
    universe = get_universe()
    # Rows are stamped with the US-Eastern trading date — the server runs
    # AEST, so date.today() at the 00:35 AEST cron would label every row
    # one day AFTER its actual US session and corrupt attribution joins.
    today = datetime.now(ZoneInfo("America/New_York")).date()

    stored, failed = [], []
    try:
        for ticker in universe:
            try:
                snap = compute_ticker_iv(provider, ticker, today)
            except IVUnavailableError as exc:
                # Subscription tier problem — surface loudly, do NOT proxy
                logger.error("%s: %s", ticker, exc)
                log_system_event(conn, "ERROR", "iv_snapshot_job",
                                 f"IV unavailable for {ticker} — check Alpaca "
                                 f"options data subscription tier",
                                 {"ticker": ticker, "error": str(exc)})
                failed.append(ticker)
                continue
            except Exception as exc:
                logger.error("%s: snapshot failed: %s", ticker, exc)
                log_system_event(conn, "ERROR", "iv_snapshot_job",
                                 f"IV snapshot failed for {ticker}: {exc}",
                                 {"ticker": ticker})
                failed.append(ticker)
                continue

            if snap["atm_iv_30d"] is None:
                logger.warning("%s: no computable 30-DTE ATM IV — row skipped", ticker)
                log_system_event(conn, "WARNING", "iv_snapshot_job",
                                 f"No computable ATM IV for {ticker}",
                                 {"ticker": ticker, "detail": snap["detail"]})
                failed.append(ticker)
                continue

            upsert_snapshot(
                conn,
                ticker=ticker,
                snapshot_date=today,
                atm_iv_30d=snap["atm_iv_30d"],
                atm_iv_60d=snap["atm_iv_60d"],
                term_slope=snap["term_slope"],
                put_skew_25d=snap["put_skew_25d"],
                hv_20d=snap["hv_20d"],
                hv_iv_spread=snap["hv_iv_spread"],
                source=snap["source"],
            )
            stored.append(ticker)
            logger.info("%s stored: atm_iv_30d=%.4f atm_iv_60d=%s slope=%s "
                        "skew=%s hv=%s spread=%s",
                        ticker, snap["atm_iv_30d"], snap["atm_iv_60d"],
                        snap["term_slope"], snap["put_skew_25d"],
                        snap["hv_20d"], snap["hv_iv_spread"])

        level = "INFO" if not failed else ("WARNING" if stored else "ERROR")
        log_system_event(conn, level, "iv_snapshot_job",
                         f"IV snapshot complete: {len(stored)} stored, {len(failed)} failed",
                         {"stored": stored, "failed": failed, "date": today.isoformat()})
        logger.info("IV snapshot done — stored=%s failed=%s", stored, failed)
    finally:
        conn.close()

    if not stored:
        sys.exit(1)


if __name__ == "__main__":
    main()
