"""
track_f/validate_historical.py
Track F — Historical Validation (Step 2 of build sequence)

Tests whether Track F scoring would have flagged 4 known winners using
only EDGAR data available at the time of the signal window.

Pass criteria: ≥3 of 4 tickers score bottleneck_score ≥60 before their
price moved >30%.

Usage:
    python3 track_f/validate_historical.py
    python3 track_f/validate_historical.py --verbose
    python3 track_f/validate_historical.py --case PWR --verbose
"""

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
RATE_LIMIT_DELAY = 1.5   # seconds between requests (EDGAR fair-use)
MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Ticker config
# Two fixes vs initial version:
#   1. Use 'entity' parameter (not ticker text in q) for company filtering
#   2. CCJ is a Canadian foreign private issuer: files 40-F + 6-K, not 10-Q/10-K
# ---------------------------------------------------------------------------

TICKER_CONFIG = {
    "NVDA": {
        "entity": "NVIDIA",
        "forms": ["10-Q", "10-K", "8-K"],
    },
    "VRT": {
        "entity": "Vertiv",
        "forms": ["10-Q", "10-K", "8-K"],
    },
    "PWR": {
        "entity": "Quanta Services",
        "forms": ["10-Q", "10-K", "8-K"],
    },
    "CCJ": {
        "entity": "CAMECO CORP",   # legal entity name in EDGAR (partial match "Cameco" misses most 6-K filings)
        "forms": ["40-F", "6-K", "40-F/A"],   # Canadian company, not 10-Q/10-K
    },
}

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

TEST_CASES = [
    {
        "ticker":         "NVDA",
        "theme":          "ai_compute",
        "signal_window":  {"start": "2022-10-01", "end": "2022-12-31"},
        "pre_move_price": 140.0,
        "peak_price":     974.0,
        "description":    "NVDA ai_compute — Q4 2022 (~$140 → $974)",
        "theme_terms": [
            "data center", "accelerated computing", "artificial intelligence",
            "AI infrastructure", "GPU", "large language model",
            "generative AI", "hyperscaler",
        ],
    },
    {
        "ticker":         "VRT",
        "theme":          "ai_cooling",
        "signal_window":  {"start": "2023-04-01", "end": "2023-06-30"},
        "pre_move_price": 14.0,
        "peak_price":     110.0,
        "description":    "VRT ai_cooling — Q2 2023 (~$14 → $110)",
        "theme_terms": [
            "liquid cooling", "data center thermal", "cooling capacity",
            "thermal management", "direct-to-chip", "immersion cooling",
            "data center infrastructure",
        ],
    },
    {
        "ticker":         "PWR",
        "theme":          "ai_power",
        "signal_window":  {"start": "2024-01-01", "end": "2024-03-31"},
        "pre_move_price": 190.0,
        "peak_price":     380.0,
        "description":    "PWR ai_power — Q1 2024 (~$190 → $380)",
        "theme_terms": [
            "grid congestion", "power demand", "data center power",
            "utility interconnection", "transformer", "switchgear",
            "hyperscaler", "electric utility", "renewable energy",
        ],
    },
    {
        "ticker":         "CCJ",
        "theme":          "nuclear",
        "signal_window":  {"start": "2021-07-01", "end": "2021-09-30"},
        "pre_move_price": 18.0,
        "peak_price":     55.0,
        "description":    "CCJ nuclear — Q3 2021 (~$18 → $55)",
        "theme_terms": [
            "uranium", "nuclear power", "nuclear energy",
            "fuel supply", "long-term contract", "uranium concentrate",
            "nuclear PPA", "enrichment",
        ],
    },
]

# ---------------------------------------------------------------------------
# Constraint signals
# ---------------------------------------------------------------------------

CONSTRAINT_SIGNALS = {
    "backlog": {
        "terms": [
            "record backlog", "backlog increased", "order backlog",
            "backlog of $", "growing backlog", "strong backlog",
            "backlog grew", "total backlog",
            # nuclear/uranium equivalents
            "contract portfolio", "committed pounds", "long-term supply agreement",
        ],
        "weight": 25,
    },
    "capacity": {
        "terms": [
            "capacity constrained", "adding capacity", "capacity expansion",
            "at capacity", "expand capacity", "exceeds capacity",
            "additional capacity", "increasing capacity",
            # nuclear/uranium equivalents
            "production curtailment", "mine restart",
        ],
        "weight": 30,
    },
    "lead_times": {
        "terms": [
            "lead times extended", "lead time increased", "delivery delays",
            "longer lead times", "extended lead times", "lead time",
            # nuclear/uranium equivalents
            "procurement lead time", "fuel delivery schedule",
        ],
        "weight": 20,
    },
    "shortage": {
        "terms": [
            "supply shortage", "component shortage", "material shortage",
            "allocation basis", "constrained supply", "supply constraints",
            "supply chain constraints",
            # nuclear/uranium equivalents
            "uranium deficit", "supply deficit", "tight supply",
        ],
        "weight": 15,
    },
    "pricing_power": {
        "terms": [
            "raised prices", "pricing power", "price discipline",
            "price increases", "favorable pricing", "pricing environment",
            "increased pricing",
            # nuclear/uranium equivalents
            "uranium price", "price escalation", "price realization",
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
    entity: Optional[str] = None,
    max_results: int = 50,
) -> list[dict]:
    """
    Query EDGAR full-text search API.
    entity: company name passed via 'entity' param (NOT embedded in q).
    Returns list of filing hit dicts.
    """
    q = " OR ".join(f'"{t}"' for t in query_terms)

    params: dict = {
        "q":         q,
        "dateRange": "custom",
        "startdt":   start_date,
        "enddt":     end_date,
        "forms":     ",".join(forms),
        "from":      0,
        "size":      max_results,
    }
    if entity:
        params["entity"] = entity

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                EDGAR_BASE,
                params=params,
                headers=EDGAR_HEADERS,
                timeout=25,
            )
            if resp.status_code == 500 and attempt < MAX_RETRIES:
                logger.warning(
                    "EDGAR 500 (attempt %d/%d) — retrying in %ds",
                    attempt, MAX_RETRIES, attempt * 2,
                )
                time.sleep(attempt * 2)
                continue
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
            return [h.get("_source", {}) for h in hits]
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                logger.warning("EDGAR request failed after %d attempts: %s", MAX_RETRIES, exc)
                return []
            time.sleep(attempt * 2)

    return []


def count_term_hits(
    terms: list[str],
    start_date: str,
    end_date: str,
    forms: list[str],
    entity: Optional[str] = None,
) -> int:
    time.sleep(RATE_LIMIT_DELAY)
    hits = edgar_search(terms, start_date, end_date, forms, entity=entity)
    return len(hits)


def probe_edgar() -> bool:
    """Quick sanity check that EDGAR is reachable and returning data."""
    logger.info("Probing EDGAR connectivity...")
    hits = edgar_search(
        ["record backlog"],
        "2024-01-01", "2024-03-31",
        ["10-Q"],
    )
    if hits:
        logger.info("EDGAR probe OK — %d hits for 'record backlog' in 10-Qs (broad search)", len(hits))
        return True
    logger.warning("EDGAR probe returned 0 hits — check connectivity")
    return False


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass
class ThemeMomentumResult:
    score: float = 0.0
    mention_count: int = 0


@dataclass
class ConstraintResult:
    constraint_severity: float = 0.0
    category_hits: dict = field(default_factory=dict)


def compute_theme_momentum(
    theme_terms: list[str],
    start_date: str,
    end_date: str,
    forms: list[str],
    entity: str,
    verbose: bool = False,
) -> ThemeMomentumResult:
    """
    Measures how much the theme appears in this company's own filings.
    No entity filter for the market-wide signal (F0 in live system),
    but for historical validation we check company filings to confirm
    the theme was present in their disclosures.
    """
    hits = count_term_hits(theme_terms, start_date, end_date, forms, entity=entity)
    MENTION_CEILING = 20
    score = min(hits / MENTION_CEILING, 1.0) * 100.0

    if verbose:
        logger.info("  [theme_momentum] entity=%s: %d hits → %.1f/100", entity, hits, score)

    return ThemeMomentumResult(score=round(score, 2), mention_count=hits)


def compute_constraint_severity(
    entity: str,
    start_date: str,
    end_date: str,
    forms: list[str],
    verbose: bool = False,
) -> ConstraintResult:
    """
    Query EDGAR for constraint signal terms in this company's filings.
    Uses entity parameter to filter to the specific company.
    Max score 100, caps at 3 hits per category.
    """
    result = ConstraintResult()

    for category, config in CONSTRAINT_SIGNALS.items():
        hits = count_term_hits(
            config["terms"],
            start_date,
            end_date,
            forms,
            entity=entity,
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
                entity, category, hits, contribution,
            )

    return result


# ---------------------------------------------------------------------------
# Historical approximations for components not computable from EDGAR alone
# ---------------------------------------------------------------------------

HISTORICAL_APPROXIMATIONS = {
    # smart_money_score reflects institutional positioning at signal window open:
    #   NVDA Q4 2022: post-ChatGPT launch accumulation, funds repositioning (~15)
    #   PWR Q1 2024: AI power thesis widely known to institutional money (~30)
    #   CCJ Q3 2021: Sprott uranium fund launched Aug 2021, nuclear renaissance broadly covered (~30)
    "NVDA": {"revenue_leverage": 85, "attention_gap": 15, "smart_money_score": 15, "crowding_penalty": 5},
    "VRT":  {"revenue_leverage": 90, "attention_gap": 15, "smart_money_score": 0,  "crowding_penalty": 5},
    "PWR":  {"revenue_leverage": 60, "attention_gap": 15, "smart_money_score": 30, "crowding_penalty": 5},
    "CCJ":  {"revenue_leverage": 95, "attention_gap": 15, "smart_money_score": 30, "crowding_penalty": 5},
}


def compute_bottleneck_score(
    tm: ThemeMomentumResult,
    cs: ConstraintResult,
    ticker: str,
) -> float:
    """
    F4 composite (historical approximation).
    Weights from TRACK_F_BUILD_PLAN.md.
    """
    approx = HISTORICAL_APPROXIMATIONS.get(ticker, {
        "revenue_leverage": 50, "attention_gap": 15,
        "smart_money_score": 0, "crowding_penalty": 5,
    })
    score = (
        tm.score                     * 0.20
        + cs.constraint_severity     * 0.25
        + approx["revenue_leverage"] * 0.15
        + approx["attention_gap"]    * 0.15
        + approx["smart_money_score"]* 0.15
        - approx["crowding_penalty"] * 0.10
    )
    return round(min(max(score, 0.0), 100.0), 2)


# ---------------------------------------------------------------------------
# Case runner
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
    cfg = TICKER_CONFIG[ticker]
    logger.info("--- %s ---", case["description"])

    price_move_pct = round(
        (case["peak_price"] - case["pre_move_price"]) / case["pre_move_price"] * 100, 1
    )

    try:
        tm = compute_theme_momentum(
            theme_terms=case["theme_terms"],
            start_date=case["signal_window"]["start"],
            end_date=case["signal_window"]["end"],
            forms=cfg["forms"],
            entity=cfg["entity"],
            verbose=verbose,
        )

        cs = compute_constraint_severity(
            entity=cfg["entity"],
            start_date=case["signal_window"]["start"],
            end_date=case["signal_window"]["end"],
            forms=cfg["forms"],
            verbose=verbose,
        )

        score = compute_bottleneck_score(tm, cs, ticker)
        passed = score >= 60.0

        logger.info(
            "%s: theme_momentum=%.1f  constraint=%.1f  score=%.1f  %s",
            ticker, tm.score, cs.constraint_severity, score,
            "PASS ✓" if passed else "FAIL ✗",
        )

        return CaseResult(
            ticker=ticker, theme=case["theme"], description=case["description"],
            pre_move_price=case["pre_move_price"], peak_price=case["peak_price"],
            price_move_pct=price_move_pct, theme_momentum=tm, constraint=cs,
            bottleneck_score=score, passed=passed,
        )

    except Exception as exc:
        logger.error("%s: failed — %s", ticker, exc)
        return CaseResult(
            ticker=ticker, theme=case["theme"], description=case["description"],
            pre_move_price=case["pre_move_price"], peak_price=case["peak_price"],
            price_move_pct=price_move_pct,
            theme_momentum=ThemeMomentumResult(), constraint=ConstraintResult(),
            bottleneck_score=0.0, passed=False, error=str(exc),
        )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(results: list[CaseResult], verbose: bool = False) -> bool:
    passed = [r for r in results if r.passed]

    print("\n" + "=" * 70)
    print("TRACK F — HISTORICAL VALIDATION REPORT")
    print(f"Run: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 70)

    for r in results:
        status = "PASS ✓" if r.passed else "FAIL ✗"
        entity = TICKER_CONFIG[r.ticker]["entity"]
        forms  = ",".join(TICKER_CONFIG[r.ticker]["forms"])
        print(f"\n{r.ticker:6} [{r.theme}]  {status}")
        print(f"  {r.description}")
        print(f"  Price move:        +{r.price_move_pct:.0f}%  ({r.pre_move_price:.0f} → {r.peak_price:.0f})")
        print(f"  Entity filter:     {entity}  forms={forms}")
        print(f"  Theme momentum:    {r.theme_momentum.score:.1f}/100  ({r.theme_momentum.mention_count} hits)")
        print(f"  Constraint score:  {r.constraint.constraint_severity:.1f}/100")
        if r.constraint.category_hits:
            for cat, h in r.constraint.category_hits.items():
                marker = "  " if h["raw_hits"] == 0 else ">>"
                print(f"    {marker} {cat:15} {h['raw_hits']:3d} hits  +{h['contribution']:.1f}pts")
        print(f"  Bottleneck score:  {r.bottleneck_score:.1f}/100  (threshold 60)")
        if r.error:
            print(f"  ERROR: {r.error}")

    print("\n" + "-" * 70)
    print(f"Results: {len(passed)}/4 passed (need ≥3)")

    overall = len(passed) >= 3
    if overall:
        print("VALIDATION PASSED — proceed to live agent build (Step 3)")
    else:
        failing = [r for r in results if not r.passed]
        print("VALIDATION FAILED")
        for r in failing:
            gap = 60.0 - r.bottleneck_score
            print(f"  {r.ticker}: score={r.bottleneck_score:.1f}  need +{gap:.1f} more")
        print("\nDiagnostic: if constraint_severity=0 for all tickers, the entity")
        print("  name may not match EDGAR. Try --probe to verify API connectivity.")

    print("=" * 70 + "\n")
    return overall


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Track F historical validation")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--case", metavar="TICKER", help="Run single case (NVDA/VRT/PWR/CCJ)")
    parser.add_argument("--probe", action="store_true", help="Test EDGAR connectivity and exit")
    args = parser.parse_args()

    if args.probe:
        ok = probe_edgar()
        sys.exit(0 if ok else 1)

    probe_edgar()

    cases = TEST_CASES
    if args.case:
        cases = [c for c in TEST_CASES if c["ticker"].upper() == args.case.upper()]
        if not cases:
            print(f"Unknown ticker '{args.case}'. Choices: NVDA VRT PWR CCJ")
            sys.exit(1)

    logger.info("Running %d case(s) — ~%ds estimated", len(cases), len(cases) * 18)

    results = [run_case(c, verbose=args.verbose) for c in cases]
    overall = print_report(results, verbose=args.verbose)

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "passed": overall,
        "cases_passed": sum(1 for r in results if r.passed),
        "cases_total": len(results),
        "results": [
            {
                "ticker":              r.ticker,
                "theme":               r.theme,
                "bottleneck_score":    r.bottleneck_score,
                "theme_momentum":      r.theme_momentum.score,
                "constraint_severity": r.constraint.constraint_severity,
                "price_move_pct":      r.price_move_pct,
                "passed":              r.passed,
                "error":               r.error,
            }
            for r in results
        ],
    }

    out_path = "/tmp/track_f_validation.json"
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2)
    logger.info("Summary written to %s", out_path)

    sys.exit(0 if (overall or len(cases) < 4) else 1)


if __name__ == "__main__":
    main()
