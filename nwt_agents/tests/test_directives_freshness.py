"""
nwt_agents/tests/test_directives_freshness.py

New test for directives_is_stale's trading-day-aware freshness window (Bug 1:
Monday false stale-directives halt). Pure-function test, no DB/network
required — directives_is_stale takes now_utc as an explicit parameter.

Covers both duplicated copies (nwt_agents/shared_context.py and
execution/engine.py — the two are kept in sync deliberately, not via
import, so both need independent proof).

Run: python3 nwt_agents/tests/test_directives_freshness.py
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from shared_context import directives_is_stale as shared_directives_is_stale  # noqa: E402

# execution/engine.py needs these env vars just to import (module-level reads)
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("NWT_DB_DSN", "postgresql://test")
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "execution"))
from engine import directives_is_stale as engine_directives_is_stale  # noqa: E402

CASES = [
    # (label, weekday_of_"today", today_date, directive_date, expect_stale)
    ("Monday accepts Friday's directive",
     datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc), "2026-08-14", False),  # Mon 8/17, Fri 8/14
    ("Tuesday accepts Monday's directive",
     datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc), "2026-08-17", False),
    ("Wednesday rejects Monday's directive (too old)",
     datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc), "2026-08-17", True),
    ("Friday accepts Thursday's directive",
     datetime(2026, 8, 14, 14, 0, tzinfo=timezone.utc), "2026-08-13", False),
    ("Monday rejects Thursday's directive (too old)",
     datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc), "2026-08-13", True),
    ("Genuinely old directive (two weeks) is stale",
     datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc), "2026-08-03", True),
    ("Same-day directive is never stale",
     datetime(2026, 8, 18, 22, 0, tzinfo=timezone.utc), "2026-08-18", False),
    ("Missing date field is stale",
     datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc), None, True),
]


def run(fn, label_suffix):
    failures = []
    for label, now_utc, directive_date, expect_stale in CASES:
        directives = {} if directive_date is None else {"date": directive_date}
        stale, reason = fn(directives, now_utc)
        status = "PASS" if stale == expect_stale else "FAIL"
        print(f"[{status}] {label} ({label_suffix}): stale={stale} (expected {expect_stale})"
              + (f" — {reason}" if stale else ""))
        if status == "FAIL":
            failures.append(label)
    return failures


if __name__ == "__main__":
    all_failures = []
    all_failures += run(shared_directives_is_stale, "shared_context.py")
    all_failures += run(engine_directives_is_stale, "execution/engine.py")

    if all_failures:
        print(f"\n{len(all_failures)} TEST(S) FAILED: {all_failures}")
        sys.exit(1)
    print("\nALL TESTS PASSED")
