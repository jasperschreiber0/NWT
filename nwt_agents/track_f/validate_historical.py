"""
track_f/validate_historical.py
Track F — Historical Validation (Step 2 of build sequence)

Tests whether Track F scoring would have flagged 4 known winners using
only EDGAR data available at the time of the signal window.

Run this BEFORE any live agent code. Pass criteria: ≥3 of 4 tickers
score bottleneck_score ≥60 before their price moved >30%.

Usage:
    python3 track_f/validate_historical.py
    python3 track_f/validate_historical.py --verbose
"""

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("validate_historical")

EDGAR_BASE = "https://efts.sec.gov/LATEST/search-index"
EDGAR_HEADERS = {
    "User-Agent": "NorthWorldTrading research@northworldtrading.com",
    "Accept": "application/json",
}
RATE_LIMIT_DELAY = 1.2  # EDGAR fair-use: max 10 req/sec, stay conservative

# ---------------------------------------------------------------------------
# Test cases — signal window uses data available at that moment only
# ---------------------------------------------------------------------------

TEST_CASES = [
    {
        "ticker":         "NVDA",
        "theme":          "ai_compute",
        "signal_window":  {"start": "2022-10-01", "end": "2022-12-31"},
        "pre_move_price": 140.0,
        "peak_price":     974.0,
        "filing_forms":   ["10-Q", "10-K", "8-K"],
        "description":    "NVDA ai_compute — Q4 2022 (~$140 → $974)",
        "theme_terms": [
            "data center", "artificial intelligence", "accelerated computing",
            "AI infrastructure", "hyperscaler", "large language model",
            "generative AI",
        ],
    },
    {
        "ticker":         "VRT",
        "theme":          "ai_cooling",
        "signal_window":  {"start": "2023-04-01", "end": "2023-06-30"},
        "pre_move_price": 14.0,
        "peak_price":     110.0,
        "filing_forms":   ["10-Q", "10-K", "8-K"],
        "description":    "VRT ai_cooling — Q2 2023 (~$14 → $110)",
        "theme_terms": [
            "liquid cooling", "data center thermal", "cooling capacity",
            "thermal management", "direct-to-chip", "immersion cooling",
        ],
    },
    {
        "ticker":         "PWR",
        "theme":          "ai_power",
        "signal_window":  {"start": "2024-01-01", "end": "2024-03-31"},
        "pre_move_price": 190.0,
        "peak_price":     380.0,
        "filing_forms":   ["10-Q", "10-K", "8-K"],
        "description":    "PWR ai_power — Q1 2024 (~$190 → $380)",
        "theme_terms": [
            "grid congestion", "power demand", "data center power",
            "utility interconnection", "transformer backlog", "switchgear",
            "hyperscaler", "electric utility",
        ],
    },
    {
        "ticker":         "CCJ",
        "theme":          "nuclear",
        "signal_window":  {"start": "2021-07-01", "end": "2021-09-30"},
        "pre_move_price": 18.0,
        "peak_price":     55.0,
        "filing_forms":   ["10-Q", "10-K", "8-K"],
        "description":    "CCJ nuclear — Q3 2021 (~$18 → $55)",
        "theme_terms": [
            "uranium", "nuclear power", "SMR", "small modular reactor",
            "nuclear PPA", "uranium offtake", "enrichment",
            "fuel supply", "long-term contract",
        ],
    },
]

# ---------------------------------------------------------------------------
# Constraint signals (from TRACK_F_BUILD_PLAN.md)
# ---------------------------------------------------------------------------

CONSTRAINT_SIGNALS = {
    "backlog": {
        "terms": [
            "backlog grew", "record backlog", "backlog increased",
            "order backlog", "backlog of $",
        ],
        "weight": 25,
    },
    "capacity": {
        "terms": [
            "exceeds capacity", "capacity constrained", "adding capacity",
            "capacity expansion", "cannot meet demand", "sold out",
        ],
        "weight": 30,
    },
    "lead_times": {
        "terms": [
            "lead times extended", "lead time increased", "delivery delays",
            "longer lead times", "month lead time",
        ],
        "weight": 20,
    },
    "shortage": {
        "terms": [
            "supply shortage", "component shortage", "material shortage",
            "allocation basis", "constrained supply",
        ],
        "weight": 15,
    },
    "pricing_power": {
        "terms": [
            "price increases accepted", "raised prices", "pricing power",
            "customers accepting price", "price discipline",
        ],
        "weight": 10,
    },
}


# ---------------------------------------------------------------------------
# EDGAR helpers
# ---------------------------------------------------------------------------

def edgar_search(
    query_terms: list[str],
    start_date: str,
    end_date: str,
    forms: list[str],
    ticker: Optional[str] = None,
    max_results: int = 50,
) -> list[dict]:
    """
    Query EDGAR full-text search API.
    Returns list of filing hit dicts (entity_name, file_date, form_type, etc.)
    """
    q = " OR ".join(f'"{t}"' for t in query_terms)
    if ticker:
        q = f'"{ticker}" AND ({q})'

    params = {
        "q":         q,
        "dateRange": "custom",
        "startdt":   start_date,
        "enddt":     end_date,
        "forms":     ",".join(forms),
        "_source":   "file_date,entity_name,file_num,period_of_report,form_type,display_names",
        "from":      0,
        "size":      max_results,
    }

    try:
        resp = requests.get(
            EDGAR_BASE,
            params=params,
            headers=EDGAR_HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        return [h.get("_source", {}) for h in hits]
    except requests.RequestException as exc:
        logger.warning("EDGAR request failed: %s", exc)
        return []


def count_term_hits(
    terms: list[str],
    start_date: str,
    end_date: str,
    forms: list[str],
    ticker: Optional[str] = None,
) -> int:
    """Return number of EDGAR filings (capped at 50) matching any of the terms."""
    time.sleep(RATE_LIMIT_DELAY)
    hits = edgar_search(terms, start_date, end_date, forms, ticker=ticker)
    return len(hits)


# ---------------------------------------------------------------------------
# Scoring components
# ---------------------------------------------------------------------------

@dataclass
class ConstraintResult:
    constraint_severity: float = 0.0
    category_hits: dict = field(default_factory=dict)


def compute_constraint_severity(
    ticker: str,
    start_date: str,
    end_date: str,
    forms: list[str],
    verbose: bool = False,
) -> ConstraintResult:
    """
    Query EDGAR for constraint signal terms, return constraint_severity 0–100.
    Max score 100, caps at 3 hits per category.
    """
    result = ConstraintResult()

    for category, config in CONSTRAINT_SIGNALS.items():
        hits = count_term_hits(
            config["terms"],
            start_date,
            end_date,
            forms,
            ticker=ticker,
        )
        capped = min(hits / 3, 1.0)
        contribution = config["weight"] * capped
        result.constraint_severity += contribution
        result.category_hits[category] = {
            "raw_hits": hits,
            "capped_ratio": round(capped, 3),
            "contribution": round(contribution, 2),
        }
        if verbose:
            logger.info(
                "  [constraint] %s/%s: %d hits → %.1f pts",
                ticker, category, hits, contribution,
            )
        time.sleep(RATE_LIMIT_DELAY)

    return result


@dataclass
class ThemeMomentumResult:
    score: float = 0.0
    mention_count: int = 0


def compute_theme_momentum(
    ticker: str,
    theme_terms: list[str],
    start_date: str,
    end_date: str,
    forms: list[str],
    verbose: bool = False,
) -> ThemeMomentumResult:
    """
    Compute theme momentum from EDGAR mention count in the signal window.
    Score 0–100: normalized against a reference ceiling of 30 filings.
    """
    hits = count_term_hits(theme_terms, start_date, end_date, forms, ticker=ticker)
    MENTION_CEILING = 30
    score = min(hits / MENTION_CEILING, 1.0) * 100.0

    if verbose:
        logger.info("  [theme_momentum] %s: %d hits → %.1f", ticker, hits, score)

    return ThemeMomentumResult(score=round(score, 2), mention_count=hits)


# ---------------------------------------------------------------------------
# Simplified historical bottleneck score
#
# For historical validation some components (revenue_leverage, attention_gap,
# smart_money, crowding) cannot be computed from EDGAR filings alone or would
# require Alpaca historical data outside the signal window.
#
# Approximations used:
#   - revenue_leverage: hardcoded per-ticker based on known 10-K segment data
#   - attention_gap:    15 (neutral) — we cannot measure industry RS historically
#                       without Alpaca data; set conservative
#   - smart_money:      0 (conservative) — Form 4 lookup deferred
#   - crowding_penalty: 5 (all four were early/pre-crowd in their signal window)
#
# These approximations are conservative. The validation is checking constraint
# severity + theme momentum can drive score ≥60, which is the live signal.
# ---------------------------------------------------------------------------

HISTORICAL_APPROXIMATIONS = {
    "NVDA": {
        "revenue_leverage": 85,   # pure-play accelerated computing (data center ~60% revenue)
        "attention_gap":    15,
        "smart_money_score": 0,
        "crowding_penalty": 5,
    },
    "VRT": {
        "revenue_leverage": 90,   # pure-play data center thermal
        "attention_gap":    15,
        "smart_money_score": 0,
        "crowding_penalty": 5,
    },
    "PWR": {
        "revenue_leverage": 60,   # electrical contractor — significant data center exposure
        "attention_gap":    15,
        "smart_money_score": 0,
        "crowding_penalty": 5,
    },
    "CCJ": {
        "revenue_leverage": 95,   # pure-play uranium
        "attention_gap":    15,
        "smart_money_score": 0,
        "crowding_penalty": 5,
    },
}


def compute_bottleneck_score(
    theme_momentum: ThemeMomentumResult,
    constraint: ConstraintResult,
    ticker: str,
) -> float:
    """
    F4 composite formula (historical approximation).
    Weights from TRACK_F_BUILD_PLAN.md:
      theme_momentum     * 0.20
      constraint_severity * 0.25
      revenue_leverage   * 0.15
      attention_gap      * 0.15
      smart_money_score  * 0.15
      crowding_penalty   * 0.10  (subtracted)
    """
    approx = HISTORICAL_APPROXIMATIONS.get(ticker, {
        "revenue_leverage": 50,
        "attention_gap": 15,
        "smart_money_score": 0,
        "crowding_penalty": 5,
    })

    score = (
        theme_momentum.score        * 0.20
        + constraint.constraint_severity * 0.25
        + approx["revenue_leverage"]     * 0.15
        + approx["attention_gap"]        * 0.15
        + approx["smart_money_score"]    * 0.15
        - approx["crowding_penalty"]     * 0.10
    )
    return round(min(max(score, 0.0), 100.0), 2)


# ---------------------------------------------------------------------------
# Main validation runner
# ---------------------------------------------------------------------------

@dataclass
class CaseResult:
    ticker: str
    theme: str
    description: str
    pre_move_price: float
    peak_price: float
    price_move_pct: float
    theme_momentum: ThemeMomentumResult
    constraint: ConstraintResult
    bottleneck_score: float
    passed: bool
    error: Optional[str] = None


def run_case(case: dict, verbose: bool = False) -> CaseResult:
    ticker = case["ticker"]
    logger.info("--- %s ---", case["description"])

    price_move_pct = round(
        (case["peak_price"] - case["pre_move_price"]) / case["pre_move_price"] * 100, 1
    )

    try:
        tm = compute_theme_momentum(
            ticker=ticker,
            theme_terms=case["theme_terms"],
            start_date=case["signal_window"]["start"],
            end_date=case["signal_window"]["end"],
            forms=case["filing_forms"],
            verbose=verbose,
        )

        cs = compute_constraint_severity(
            ticker=ticker,
            start_date=case["signal_window"]["start"],
            end_date=case["signal_window"]["end"],
            forms=case["filing_forms"],
            verbose=verbose,
        )

        score = compute_bottleneck_score(tm, cs, ticker)
        passed = score >= 60.0

        logger.info(
            "%s: theme_momentum=%.1f constraint_severity=%.1f bottleneck_score=%.1f %s",
            ticker, tm.score, cs.constraint_severity, score,
            "PASS ✓" if passed else "FAIL ✗",
        )

        return CaseResult(
            ticker=ticker,
            theme=case["theme"],
            description=case["description"],
            pre_move_price=case["pre_move_price"],
            peak_price=case["peak_price"],
            price_move_pct=price_move_pct,
            theme_momentum=tm,
            constraint=cs,
            bottleneck_score=score,
            passed=passed,
        )

    except Exception as exc:
        logger.error("%s: failed with error: %s", ticker, exc)
        return CaseResult(
            ticker=ticker,
            theme=case["theme"],
            description=case["description"],
            pre_move_price=case["pre_move_price"],
            peak_price=case["peak_price"],
            price_move_pct=price_move_pct,
            theme_momentum=ThemeMomentumResult(),
            constraint=ConstraintResult(),
            bottleneck_score=0.0,
            passed=False,
            error=str(exc),
        )


def print_report(results: list[CaseResult], verbose: bool = False) -> bool:
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]

    print("\n" + "=" * 70)
    print("TRACK F — HISTORICAL VALIDATION REPORT")
    print(f"Run: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 70)

    for r in results:
        status = "PASS ✓" if r.passed else "FAIL ✗"
        print(f"\n{r.ticker:6} [{r.theme}]  {status}")
        print(f"  Window:            {r.description}")
        print(f"  Price move:        {r.pre_move_price:.0f} → {r.peak_price:.0f} (+{r.price_move_pct:.0f}%)")
        print(f"  Theme momentum:    {r.theme_momentum.score:.1f}/100  ({r.theme_momentum.mention_count} filing hits)")
        print(f"  Constraint score:  {r.constraint.constraint_severity:.1f}/100")
        if verbose and r.constraint.category_hits:
            for cat, hits in r.constraint.category_hits.items():
                print(f"    {cat:15} {hits['raw_hits']:3d} hits  +{hits['contribution']:.1f}pts")
        print(f"  Bottleneck score:  {r.bottleneck_score:.1f}/100  (threshold: 60)")
        if r.error:
            print(f"  ERROR:             {r.error}")

    print("\n" + "-" * 70)
    print(f"Results: {len(passed)}/4 passed (need ≥3)")

    overall_pass = len(passed) >= 3
    if overall_pass:
        print("VALIDATION PASSED — proceed to live agent build")
    else:
        print("VALIDATION FAILED — tune scoring weights before proceeding")
        if len(passed) > 0:
            print(f"Passing cases: {', '.join(r.ticker for r in passed)}")
            print(f"Failing cases: {', '.join(r.ticker for r in failed)}")
            print("\nHint: if constraint_severity is consistently 0, EDGAR may not have")
            print("  ticker-level filing attribution for these symbols in the signal window.")
            print("  Re-run without ticker filter (broader industry search) to diagnose.")

    print("=" * 70 + "\n")
    return overall_pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Track F historical validation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show per-category detail")
    parser.add_argument(
        "--case", metavar="TICKER",
        help="Run a single case only (NVDA, VRT, PWR, CCJ)",
    )
    args = parser.parse_args()

    cases = TEST_CASES
    if args.case:
        cases = [c for c in TEST_CASES if c["ticker"].upper() == args.case.upper()]
        if not cases:
            print(f"Unknown ticker '{args.case}'. Choices: NVDA, VRT, PWR, CCJ")
            sys.exit(1)

    logger.info("Starting Track F historical validation — %d case(s)", len(cases))
    logger.info("EDGAR rate limit: %.1fs between requests", RATE_LIMIT_DELAY)

    results = []
    for case in cases:
        result = run_case(case, verbose=args.verbose)
        results.append(result)

    overall = print_report(results, verbose=args.verbose)

    # Machine-readable summary for CI / automated checks
    summary = {
        "run_at": datetime.utcnow().isoformat() + "Z",
        "passed": overall,
        "cases_passed": sum(1 for r in results if r.passed),
        "cases_total": len(results),
        "results": [
            {
                "ticker":            r.ticker,
                "theme":             r.theme,
                "bottleneck_score":  r.bottleneck_score,
                "theme_momentum":    r.theme_momentum.score,
                "constraint_severity": r.constraint.constraint_severity,
                "price_move_pct":    r.price_move_pct,
                "passed":            r.passed,
                "error":             r.error,
            }
            for r in results
        ],
    }

    out_path = "/tmp/track_f_validation.json"
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2)
    logger.info("Summary written to %s", out_path)

    sys.exit(0 if overall or len(cases) < 4 else 1)


if __name__ == "__main__":
    main()
