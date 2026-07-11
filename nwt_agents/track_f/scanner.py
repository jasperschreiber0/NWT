"""
nwt_agents/track_f/scanner.py
Track F — production bottleneck/thematic scanner. Runs once daily (SEC
filings don't change intraday, unlike the options conviction stack).

Reuses the exact live-computable scoring method track_f/validate_historical.py
already backtested against 4 known historical winners (NVDA/VRT/PWR/CCJ — 3/4
scored >=60 before their moves; CCJ is a documented EDGAR EFTS limitation,
not a method failure — see themes.py). This is not a new, unvalidated
heuristic invented for this scanner.

WHAT'S LIVE vs CURATED in compute_bottleneck_score's four inputs:
  - theme_momentum, constraint_severity: computed live from EDGAR's current
    full-text search results, identical method to validate_historical.py.
  - revenue_leverage, attention_gap, smart_money_score, crowding_penalty:
    curated per-ticker in themes.py (TICKER_APPROXIMATIONS / a conservative
    default), NOT live. Live institutional-flow (13F/Form4) and analyst-
    coverage data are a real Phase-2 extension — not fabricated here.
    This scanner does NOT reuse validate_historical.compute_bottleneck_score
    directly: that function's own HISTORICAL_APPROXIMATIONS lookup only
    covers the 4 backtest tickers and would silently default every other
    ticker, so the formula is reimplemented here against themes.py's
    per-ticker approximations instead.

Writes:
  - nwt_bottleneck_scores  — one row per (ticker, theme) per run
  - nwt_track_f_candidates — confirmed-theme tickers scoring >= threshold,
                             for human review via the dashboard
  - nwt_emerging_themes    — candidate (speculative) themes whose aggregate
                             momentum crosses a threshold, for human approval

No order authority. CLAUDE.md defines no sizing/risk/isolation rules for
Track F, so this stays a research signal surface — it never writes a
TRADE_PROPOSAL or touches nwt_tickets.
"""

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

_here = Path(__file__).parent
sys.path.insert(0, str(_here))            # themes.py, validate_historical.py
sys.path.insert(0, str(_here.parent))     # shared_context.py

load_dotenv(_here.parent / ".env")

from shared_context import check_no_trade_mode, get_db, log_system_event, upsert_agent_state  # noqa: E402
from themes import (  # noqa: E402
    BOTTLENECK_SCORE_CANDIDATE_THRESHOLD,
    CANDIDATE_THEMES,
    CONFIRMED_THEMES,
    TICKER_ENTITY,
    get_approximation,
    get_forms,
)
from validate_historical import compute_constraint_severity, compute_theme_momentum  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("track_f_scanner")

SCAN_WINDOW_DAYS = 30
MOMENTUM_BASELINE_SCANS = 3
EMERGING_THEME_MOMENTUM_THRESHOLD = 10.0  # points vs trailing baseline


def compute_bottleneck_score(tm_score: float, constraint_severity: float, approx: dict) -> float:
    """Same weighted composite validate_historical.py backtested (see that file's compute_bottleneck_score)."""
    score = (
        tm_score * 0.20
        + constraint_severity * 0.25
        + approx["revenue_leverage"] * 0.15
        + approx["attention_gap"] * 0.15
        + approx["smart_money_score"] * 0.15
        - approx["crowding_penalty"] * 0.10
    )
    return round(min(max(score, 0.0), 100.0), 2)


def score_ticker(ticker: str, theme_terms: list, constraint_cache: dict) -> tuple:
    """Returns (bottleneck_score, mention_count, evidence)."""
    entity = TICKER_ENTITY.get(ticker, ticker)
    forms = get_forms(ticker)
    end = date.today()
    start = end - timedelta(days=SCAN_WINDOW_DAYS)

    tm = compute_theme_momentum(
        theme_terms=theme_terms, start_date=start.isoformat(), end_date=end.isoformat(),
        forms=forms, entity=entity,
    )

    if ticker in constraint_cache:
        cs = constraint_cache[ticker]
    else:
        cs = compute_constraint_severity(
            entity=entity, start_date=start.isoformat(), end_date=end.isoformat(), forms=forms,
        )
        constraint_cache[ticker] = cs

    approx = get_approximation(ticker)
    score = compute_bottleneck_score(tm.score, cs.constraint_severity, approx)
    evidence = {
        "theme_momentum": tm.score, "mention_count": tm.mention_count,
        "constraint_severity": round(cs.constraint_severity, 2),
        "constraint_categories": cs.category_hits, "approximation": approx,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
    }
    return score, tm.mention_count, evidence


def fetch_baseline_score(conn, ticker: str, theme: str) -> float:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bottleneck_score FROM nwt_bottleneck_scores
            WHERE ticker = %s AND theme = %s
            ORDER BY scored_at DESC LIMIT %s
            """,
            (ticker, theme, MOMENTUM_BASELINE_SCANS),
        )
        rows = [r[0] for r in cur.fetchall()]
    return sum(float(r) for r in rows) / len(rows) if rows else None


def write_bottleneck_score(conn, ticker: str, theme: str, score: float, mentions: int,
                           momentum, evidence: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_bottleneck_scores (ticker, theme, bottleneck_score, mention_count, momentum, evidence)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (ticker, theme, score, mentions, momentum, __import__("json").dumps(evidence, default=str)),
        )
    conn.commit()


def surface_candidate(conn, ticker: str, theme: str, score: float, evidence: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM nwt_track_f_candidates WHERE ticker=%s AND theme=%s AND status IN ('pending','approved')",
            (ticker, theme),
        )
        if cur.fetchone()[0] > 0:
            return  # already surfaced, awaiting/has had human review
        cur.execute(
            """
            INSERT INTO nwt_track_f_candidates (ticker, theme, bottleneck_score, rationale, status)
            VALUES (%s, %s, %s, %s, 'pending')
            """,
            (ticker, theme, score,
             f"bottleneck_score={score} (>= {BOTTLENECK_SCORE_CANDIDATE_THRESHOLD}) — "
             f"{evidence['mention_count']} theme mentions, constraint_severity={evidence['constraint_severity']}"),
        )
    conn.commit()
    logger.info("Candidate surfaced: %s / %s (score=%.1f)", ticker, theme, score)


def upsert_emerging_theme(conn, theme: str, tickers: list, momentum: float, evidence: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO nwt_emerging_themes (candidate_theme, tickers, momentum, evidence)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (candidate_theme) DO UPDATE
              SET tickers = EXCLUDED.tickers, momentum = EXCLUDED.momentum, evidence = EXCLUDED.evidence
            """,
            (theme, tickers, momentum, __import__("json").dumps(evidence, default=str)),
        )
    conn.commit()
    logger.info("Emerging theme updated: %s (momentum=%.1f)", theme, momentum)


def run_scan(conn) -> dict:
    counts = {"scored": 0, "candidates_surfaced": 0, "emerging_themes_updated": 0}
    constraint_cache: dict = {}

    # --- Confirmed themes: score, surface candidates ---
    for theme, cfg in CONFIRMED_THEMES.items():
        for ticker in cfg["tickers"]:
            try:
                score, mentions, evidence = score_ticker(ticker, cfg["terms"], constraint_cache)
            except Exception as exc:
                logger.warning("Scoring failed for %s / %s: %s", ticker, theme, exc)
                continue

            baseline = fetch_baseline_score(conn, ticker, theme)
            momentum = round(score - baseline, 2) if baseline is not None else None
            write_bottleneck_score(conn, ticker, theme, score, mentions, momentum, evidence)
            counts["scored"] += 1

            if score >= BOTTLENECK_SCORE_CANDIDATE_THRESHOLD:
                surface_candidate(conn, ticker, theme, score, evidence)
                counts["candidates_surfaced"] += 1

    # --- Candidate (speculative) themes: score, track momentum, surface for approval ---
    for theme, cfg in CANDIDATE_THEMES.items():
        theme_scores = []
        theme_evidence = {}
        for ticker in cfg["tickers"]:
            try:
                score, mentions, evidence = score_ticker(ticker, cfg["terms"], constraint_cache)
            except Exception as exc:
                logger.warning("Scoring failed for %s / %s: %s", ticker, theme, exc)
                continue
            baseline = fetch_baseline_score(conn, ticker, theme)
            momentum = round(score - baseline, 2) if baseline is not None else None
            write_bottleneck_score(conn, ticker, theme, score, mentions, momentum, evidence)
            counts["scored"] += 1
            theme_scores.append(score)
            theme_evidence[ticker] = {"score": score, "mentions": mentions}

        if not theme_scores:
            continue

        avg_score = sum(theme_scores) / len(theme_scores)
        baseline_avg = fetch_baseline_score(conn, cfg["tickers"][0], theme)  # proxy: lead ticker's trend
        theme_momentum = round(avg_score - baseline_avg, 2) if baseline_avg is not None else 0.0

        if theme_momentum >= EMERGING_THEME_MOMENTUM_THRESHOLD or avg_score >= BOTTLENECK_SCORE_CANDIDATE_THRESHOLD:
            upsert_emerging_theme(conn, theme, cfg["tickers"], theme_momentum,
                                  {"avg_score": round(avg_score, 2), "per_ticker": theme_evidence})
            counts["emerging_themes_updated"] += 1

    return counts


def main() -> None:
    conn = get_db()
    try:
        halted, halt_reason = check_no_trade_mode(conn)
        if halted:
            logger.info("no_trade_mode SET — scanning anyway (read-only research, no trade authority): %s", halt_reason)

        counts = run_scan(conn)
        log_system_event(conn, "INFO", "track_f_scanner", f"Scan complete: {counts}", counts)
        upsert_agent_state(conn, "track_f_scanner", "ok", counts)
        logger.info("Track F scan done — %s", counts)
    except Exception as exc:
        logger.error("Track F scan failed: %s", exc, exc_info=True)
        log_system_event(conn, "ERROR", "track_f_scanner", f"Scan failed: {exc}")
        upsert_agent_state(conn, "track_f_scanner", "error", {"error": str(exc)})
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
