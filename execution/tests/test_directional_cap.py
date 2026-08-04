"""
execution/tests/test_directional_cap.py
Regression test for the UNATTRIBUTED-exposure directional cap fix.

Proven live 2026-08-03/08-04: a single UNATTRIBUTED legacy position
($90,805 AAPL) consumed the entire directional budget and blocked every
subsequent, correctly-sized bot trade regardless of direction. Fixed by
excluding bot_source='UNATTRIBUTED' from check_directional_cap()'s
existing-exposure sum.

Run against a throwaway Postgres (NWT_TEST_DB_DSN), never production:
    NWT_TEST_DB_DSN=postgresql://nwt_test:nwt_test_pw@localhost/nwt_directional_cap_test \
        pytest execution/tests/test_directional_cap.py -v
"""
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine import DIRECTIONAL_CAP_PCT, check_directional_cap  # noqa: E402

TEST_DSN = os.environ.get("NWT_TEST_DB_DSN")


@pytest.fixture
def conn():
    if not TEST_DSN:
        pytest.skip("NWT_TEST_DB_DSN not set — run against a throwaway Postgres, never production")
    c = psycopg2.connect(TEST_DSN)
    yield c
    with c.cursor() as cur:
        cur.execute("TRUNCATE nwt_portfolio_ledger")
    c.commit()
    c.close()


def _insert_position(conn, bot_source, direction, notional_risk, status="open"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_portfolio_ledger
                (position_id, bot_source, asset, asset_type, direction, notional_risk, status)
            VALUES (%s, %s, 'TESTSYM', 'equity', %s, %s, %s)
            """,
            (str(uuid.uuid4()), bot_source, direction, notional_risk, status),
        )
    conn.commit()


ACCOUNT_EQUITY = 94_000.0


@patch("engine.get_alpaca_account_equity", return_value=ACCOUNT_EQUITY)
def test_unattributed_exposure_excluded_from_cap(mock_equity, conn):
    """
    Reproduces the exact live scenario: one huge UNATTRIBUTED long position
    plus a small, correctly-sized new bot trade. Before the fix, the
    UNATTRIBUTED notional alone (already >60% of equity) would block the
    new trade outright. After the fix, only real bot-attributed exposure
    counts, so the small trade is correctly allowed through.
    """
    _insert_position(conn, "UNATTRIBUTED", "long", 90_805.0)

    small_new_trade = 2_000.0
    cap_exceeded, total, cap = check_directional_cap(conn, "long", small_new_trade)

    assert cap == pytest.approx(ACCOUNT_EQUITY * DIRECTIONAL_CAP_PCT)
    assert total == pytest.approx(small_new_trade), (
        f"UNATTRIBUTED exposure leaked into the cap sum: total={total}, expected={small_new_trade}"
    )
    assert not cap_exceeded, "A small trade was blocked by legacy UNATTRIBUTED exposure it can't manage"


@patch("engine.get_alpaca_account_equity", return_value=ACCOUNT_EQUITY)
def test_attributed_exposure_still_counts(mock_equity, conn):
    """The fix must not accidentally exclude real bot exposure too — only UNATTRIBUTED."""
    _insert_position(conn, "AUS_BOT", "long", 30_000.0)

    incoming = 30_000.0  # 30k + 30k = 60k, exactly at 60% of 94k (56,400) -> should exceed
    cap_exceeded, total, cap = check_directional_cap(conn, "long", incoming)

    assert total == pytest.approx(60_000.0)
    assert cap_exceeded, "Real bot-attributed exposure must still count toward the cap"


@patch("engine.get_alpaca_account_equity", return_value=ACCOUNT_EQUITY)
def test_directional_cap_pct_unchanged(mock_equity, conn):
    """Explicit guard: this fix must not have touched the cap percentage itself."""
    assert DIRECTIONAL_CAP_PCT == 0.60


@patch("engine.get_alpaca_account_equity", return_value=ACCOUNT_EQUITY)
def test_short_direction_unaffected(mock_equity, conn):
    """UNATTRIBUTED exclusion applies symmetrically — sanity check on the short side."""
    _insert_position(conn, "UNATTRIBUTED", "short", 50_000.0)
    _insert_position(conn, "US_BOT", "short", 10_000.0)

    cap_exceeded, total, cap = check_directional_cap(conn, "short", 5_000.0)

    assert total == pytest.approx(15_000.0), "UNATTRIBUTED short exposure should also be excluded"
    assert not cap_exceeded
