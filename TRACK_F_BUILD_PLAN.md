# TRACK_F_BUILD_PLAN.md
# NorthWorld Trading — Track F: Bottleneck Discovery Agent Stack
# Drop this file at: /home/northworld/trading/TRACK_F_BUILD_PLAN.md
# Read alongside: NWT_BUILD_PLAN.md and CLAUDE.md

---

## What Track F Is

A research-first, slow-burn agent stack that identifies structural bottleneck companies
before the market fully prices them in. It discovers picks-and-shovels opportunities
using SEC EDGAR filings + Alpaca price data.

Output: `track_f_candidates.json` → Portfolio Brain → Execution Engine → Ledger.

This is NOT a signal bot. It is a dependency-chain discovery and scoring system.
Trade candidates are a byproduct of research, not the starting point.

---

## How It Fits Into NWT

- Lives in: `/home/northworld/trading/nwt_agents/track_f/`
- Runs via: same crontab as Track C/D/E (SHELL=/bin/bash line 1 — mandatory)
- DB: same `nwt_agents` Postgres database
- Credentials: same `nwt_agents/.env`
- Execution: candidates flow to Portfolio Brain via `shared/track_f_candidates.json`
- Portfolio Brain approves → Execution Engine places → Ledger records
- Strategy IDs: all prefixed `TF-` (e.g. `TF-ai_power-POWL-001`)
- Learning Agent: outcomes logged to `nwt_trade_outcomes` with TF- strategy_id

---

## Agent Stack (Full)

```
WEEKLY RESEARCH LAYER (Sunday UTC)
  F0   — Theme Monitor         (known themes, EDGAR term frequency)
  F0B  — Emerging Cluster Detector  (unknown themes, NLP acceleration → human gate)
  F1   — Dependency Mapper     (approved theme → ticker graph)

WEEKLY EXPOSURE LAYER (Monday UTC)
  F3   — Institutional Flow Reader  (13F, Form 4 — lag-aware)
  F3B  — Crowding Agent        (ETF concentration, news volume, analyst coverage)

DAILY SCORING LAYER (weekdays, post-US-close, 21:30–22:15 UTC)
  F3.5 — Revenue Exposure Agent     (% revenue leveraged to theme — Sonnet, weekly)
  F2   — Divergence Scanner    (industry-relative RS, not SPY)
  F2.5 — Constraint Detector   (backlog, lead times, capacity language — highest priority)
  F4   — Bottleneck Scorer     (composite formula, deterministic, no LLM)
  F5   — Conviction Engine     (Sonnet — thesis + invalidation; vehicle = deterministic)

SHARED (existing)
  Risk Agent     (13 veto rules, every 5 min)
  Execution Engine
  Learning Agent (21:00 UTC)
```

---

## Pre-Build Requirement: Historical Validation

**Build this FIRST before any live agent code.**

File: `track_f/validate_historical.py`

Test whether Track F scoring would have flagged these before their major move,
using only EDGAR data available at the time:

| Ticker | Theme         | Signal Window      | Pre-move price | Peak   |
|--------|---------------|--------------------|----------------|--------|
| NVDA   | ai_compute    | Q4 2022            | ~$140          | $974   |
| VRT    | ai_cooling    | Q2 2023            | ~$14           | $110   |
| PWR    | ai_power      | Q1 2024            | ~$190          | $380   |
| CCJ    | nuclear       | Q3 2021            | ~$18           | $55    |

Pass criteria: ≥3 of 4 tickers score bottleneck_score ≥60 before price moves >30%.
If fewer than 3 pass → tune scoring weights before proceeding.

EDGAR historical filings: https://efts.sec.gov/LATEST/search-index?q=%22backlog%22&dateRange=custom&startdt=2022-10-01&enddt=2022-12-31&forms=10-Q

---

## Build Sequence

Build in this exact order. Do not skip steps.

```
Step 1  — Postgres schema
Step 2  — validate_historical.py (offline backtest)
Step 3  — F0  theme_monitor.py
Step 4  — F0B cluster_detector.py + approval gate
Step 5  — F1  dependency_mapper.py
Step 6  — F2.5 constraint_detector.py  ← highest priority live agent
Step 7  — F2  divergence_scanner.py (industry-relative)
Step 8  — F3  institutional_flow.py
Step 9  — F3B crowding_agent.py
Step 10 — F3.5 revenue_exposure.py (Sonnet, weekly)
Step 11 — F4  bottleneck_scorer.py (deterministic)
Step 12 — F5  conviction_engine.py (Sonnet — thesis only, not vehicle)
Step 13 — Cron entries + verify SHELL=/bin/bash line 1
Step 14 — Strategy genome rows for TF- strategies
Step 15 — End-to-end dry run: verify track_f_candidates.json produced
Step 16 — Portfolio Brain: add theme exposure cap logic
```

---

## Postgres Schema

Run this against the `nwt_agents` database. All tables are additive to existing schema.

```sql
-- F0B output: emerging theme queue awaiting human approval
CREATE TABLE nwt_emerging_themes (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_theme TEXT NOT NULL,
  top_phrases     TEXT[],
  mention_growth_pct NUMERIC,
  momentum        NUMERIC,
  status          TEXT DEFAULT 'pending',  -- 'pending', 'approved', 'rejected'
  detected_at     TIMESTAMPTZ DEFAULT NOW(),
  approved_at     TIMESTAMPTZ
);

-- F2.5 output: constraint signals per ticker
CREATE TABLE nwt_constraint_signals (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticker              TEXT NOT NULL,
  constraint_severity NUMERIC,
  evidence            JSONB,
  filing_date         DATE,
  filing_type         TEXT,
  scored_at           TIMESTAMPTZ DEFAULT NOW()
);

-- F3.5 output: revenue exposure per ticker/theme
CREATE TABLE nwt_revenue_exposure (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticker           TEXT NOT NULL,
  theme            TEXT NOT NULL,
  revenue_leverage NUMERIC,
  reasoning        TEXT,
  scored_at        TIMESTAMPTZ DEFAULT NOW()
);

-- F3B output: crowding scores
CREATE TABLE nwt_crowding_scores (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticker                      TEXT NOT NULL,
  crowding_score              NUMERIC,
  etf_count                   INTEGER,
  institutional_growth_qoq_pct NUMERIC,
  news_volume_zscore          NUMERIC,
  scored_at                   TIMESTAMPTZ DEFAULT NOW()
);

-- F0 output: theme momentum scores
CREATE TABLE nwt_theme_momentum (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  theme          TEXT NOT NULL,
  score          NUMERIC NOT NULL,
  mention_count  INTEGER,
  qoq_delta      NUMERIC,
  source_filings INTEGER,
  scored_at      TIMESTAMPTZ DEFAULT NOW()
);

-- F4 output: composite bottleneck scores
CREATE TABLE nwt_bottleneck_scores (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticker              TEXT NOT NULL,
  theme               TEXT NOT NULL,
  bottleneck_score    NUMERIC NOT NULL,
  theme_momentum      NUMERIC,
  constraint_severity NUMERIC,
  revenue_leverage    NUMERIC,
  attention_gap       NUMERIC,
  smart_money_score   NUMERIC,
  crowding_penalty    NUMERIC,
  scored_at           TIMESTAMPTZ DEFAULT NOW()
);

-- F5 output: candidates for Portfolio Brain
CREATE TABLE nwt_track_f_candidates (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id       TEXT NOT NULL,
  ticker            TEXT NOT NULL,
  theme             TEXT NOT NULL,
  bottleneck_score  NUMERIC,
  conviction        NUMERIC,
  vehicle           TEXT,         -- 'LEAPS', 'bull_call_spread', 'equity'
  direction         TEXT,
  time_horizon_days INTEGER,
  thesis            TEXT,
  invalidation      TEXT,
  gap_explained     BOOLEAN,
  status            TEXT DEFAULT 'pending',  -- 'pending','approved','rejected','open','closed'
  created_at        TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Agent Specifications

### F0 — Theme Monitor
**File:** `track_f/f0_theme_monitor.py`
**Schedule:** Sunday 20:00 UTC
**Source:** SEC EDGAR full-text search API (no API key required)
**Base URL:** `https://efts.sec.gov/LATEST/search-index`

Known theme dictionary (seed — will grow via F0B approvals):
```python
THEME_TERMS = {
    "ai_power": [
        "grid congestion", "power demand", "data center power",
        "utility interconnection", "transformer backlog", "switchgear"
    ],
    "ai_networking": [
        "InfiniBand", "400G", "800G", "spine-leaf", "network fabric",
        "ethernet switching"
    ],
    "ai_cooling": [
        "liquid cooling", "direct-to-chip cooling", "immersion cooling",
        "thermal management", "cooling capacity"
    ],
    "nuclear": [
        "SMR", "small modular reactor", "nuclear PPA",
        "uranium offtake", "nuclear power purchase"
    ],
    "robotics": [
        "humanoid", "autonomous mobile robot", "AMR",
        "end effector", "collaborative robot"
    ],
    "copper_constraint": [
        "copper shortage", "copper lead time", "copper allocation",
        "wire and cable", "copper backlog"
    ],
}
```

For each theme:
- Query EDGAR for mentions in 10-K, 10-Q, 8-K over last 90 days
- Compare to prior 90-day window (rolling)
- Compute momentum score 0–100 (mention velocity)
- Write to `nwt_theme_momentum`
- Write `shared/theme_momentum.json`

---

### F0B — Emerging Cluster Detector
**File:** `track_f/f0b_cluster_detector.py`
**Schedule:** Sunday 20:30 UTC (after F0)
**Dependencies:** `pip install spacy && python -m spacy download en_core_web_sm`

Method:
1. Pull last 90 days of 8-K/10-Q/10-K filings from EDGAR (limit to 500 filings)
2. Extract noun phrases using spaCy `en_core_web_sm`
3. Compute TF-IDF or simple count per phrase
4. Compare phrase frequency to prior 90-day window
5. Group semantically similar phrases (cosine similarity on char n-grams — no embeddings API needed)
6. Flag clusters with >150% QoQ growth NOT already in `THEME_TERMS`
7. Write to `nwt_emerging_themes` with `status = 'pending'`

DO NOT auto-approve. Human approves via:
```sql
UPDATE nwt_emerging_themes SET status = 'approved', approved_at = NOW()
WHERE candidate_theme = 'advanced_cooling';
```

F1 only processes themes with `status = 'approved'` in `nwt_emerging_themes`
OR themes in the hardcoded `THEME_TERMS` dict.

Output per candidate:
```json
{
  "candidate_theme": "advanced_cooling",
  "top_phrases": ["liquid cooling", "direct-to-chip", "immersion cooling"],
  "mention_growth_pct": 240,
  "momentum": 78,
  "status": "pending"
}
```

Weekly query to surface pending themes (add to session startup checklist):
```sql
SELECT candidate_theme, momentum, top_phrases, detected_at
FROM nwt_emerging_themes
WHERE status = 'pending'
ORDER BY momentum DESC;
```

---

### F1 — Dependency Mapper
**File:** `track_f/f1_dependency_mapper.py`
**Schedule:** Sunday 21:00 UTC (after F0B)

Seed graph (expand as F0B approves new themes):
```python
DEPENDENCY_GRAPH = {
    "ai_power": {
        "tickers": ["ETN", "PWR", "VRT", "POWL", "EMR"],
        "constraint_type": "manufacturing_capacity",
        "switchability": "low",
    },
    "ai_networking": {
        "tickers": ["ANET", "AVGO", "CSCO"],
        "constraint_type": "design_monopoly",
        "switchability": "medium",
    },
    "ai_cooling": {
        "tickers": ["VRT", "TT", "GNRC"],
        "constraint_type": "manufacturing_capacity",
        "switchability": "medium",
    },
    "nuclear": {
        "tickers": ["CCJ", "NNE", "LEU"],
        "constraint_type": "resource_scarcity",
        "switchability": "very_low",
    },
    "robotics": {
        "tickers": ["TDY", "ISRG", "ONTO"],
        "constraint_type": "ip_monopoly",
        "switchability": "low",
    },
    "copper_constraint": {
        "tickers": ["FCX", "SCCO", "WIRE"],
        "constraint_type": "resource_scarcity",
        "switchability": "low",
    },
}
```

Output: `shared/bottleneck_candidates.json`
Ranked by: theme momentum score × switchability_inverse (very_low=1.0, low=0.8, medium=0.5, high=0.2)

---

### F2.5 — Constraint Detector ← HIGHEST PRIORITY LIVE AGENT
**File:** `track_f/f2_5_constraint_detector.py`
**Schedule:** Daily 21:50 UTC (weekdays)
**Source:** EDGAR full-text search on 8-K, 10-Q, 10-K for each candidate ticker

Constraint term dictionary:
```python
CONSTRAINT_SIGNALS = {
    "backlog": {
        "terms": ["backlog grew", "record backlog", "backlog increased",
                  "order backlog", "backlog of $"],
        "weight": 25
    },
    "capacity": {
        "terms": ["exceeds capacity", "capacity constrained", "adding capacity",
                  "capacity expansion", "cannot meet demand", "sold out"],
        "weight": 30
    },
    "lead_times": {
        "terms": ["lead times extended", "lead time increased", "delivery delays",
                  "longer lead times", "month lead time"],
        "weight": 20
    },
    "shortage": {
        "terms": ["supply shortage", "component shortage", "material shortage",
                  "allocation basis", "constrained supply"],
        "weight": 15
    },
    "pricing_power": {
        "terms": ["price increases accepted", "raised prices", "pricing power",
                  "customers accepting price", "price discipline"],
        "weight": 10
    },
}
```

Scoring:
```python
constraint_severity = sum(
    cat["weight"] * min(mention_count_for_category / 3, 1.0)
    for cat in CONSTRAINT_SIGNALS.values()
)
# Max score: 100. Caps at 3 mentions per category to prevent gaming.
```

Store evidence (filing snippets, dates, filing type) in JSONB column.
Write to `nwt_constraint_signals`.

---

### F2 — Divergence Scanner (Industry-Relative)
**File:** `track_f/f2_divergence_scanner.py`
**Schedule:** Daily 21:45 UTC (weekdays)
**Source:** Alpaca data API (already connected)

**DO NOT compare to SPY. Compare to industry peers.**

```python
# Peer basket = other tickers in same dependency graph theme node
def industry_relative_strength(ticker: str, theme: str, lookback_weeks: int = 13) -> float:
    peers = [t for t in DEPENDENCY_GRAPH[theme]["tickers"] if t != ticker]
    peer_rs_scores = [rs_score(p, lookback_weeks) for p in peers]
    peer_median = statistics.median(peer_rs_scores)
    return rs_score(ticker, lookback_weeks) - peer_median

# attention_gap: score rising faster than peers = genuine divergence
# attention_gap: peers also lagging = sector-wide weakness, not a signal
attention_gap = bottleneck_score_delta - industry_rs_delta
```

Output: `shared/attention_gaps.json`
Flag tickers where `attention_gap > 20` and gap has been open > 5 trading days.

---

### F3 — Institutional Flow Reader
**File:** `track_f/f3_institutional_flow.py`
**Schedule:** Monday 21:00 UTC (lag-aware: 13F filed 45 days after quarter end)
**Source:** SEC EDGAR 13F filings + Form 4 insider filings

Scoring:
```python
smart_money_score = 0
# New position opened by ≥3 distinct funds this quarter
if new_fund_count >= 3:
    smart_money_score += 20
# Existing position increased >25% by major fund (>$1B AUM)
if large_fund_increase_pct >= 25:
    smart_money_score += 15
# Insider Form 4 purchase (not option exercise)
if insider_open_market_buy:
    smart_money_score += 25
# Analyst estimate revision (upward, last 30 days — proxy via 8-K guidance language)
if earnings_guidance_raised:
    smart_money_score += 10
```

13F EDGAR endpoint: `https://data.sec.gov/submissions/CIK{cik}.json`
Form 4 search: `https://efts.sec.gov/LATEST/search-index?q={ticker}&forms=4`

Write to `nwt_agent_state` (reuse existing table, strategy_id = `TF-flow-{ticker}`)
and `shared/smart_money.json`.

---

### F3B — Crowding Agent
**File:** `track_f/f3b_crowding_agent.py`
**Schedule:** Monday 21:30 UTC
**Source:** EDGAR 13F (ETF holdings), Alpaca news feed

```python
crowding_score = (
    etf_ownership_pct      * 0.35   # how many ETFs hold this ticker
  + inst_growth_rate_qoq   * 0.25   # fast-growing institutional ownership
  + news_volume_zscore     * 0.25   # 30-day rolling news volume vs 1yr baseline
  + analyst_coverage_zscore* 0.15   # proxy: EDGAR 8-K mention count by analysts
)
# 0–100. High = crowded. Applied as penalty in F4, not disqualifier.
```

Write to `nwt_crowding_scores`.

---

### F3.5 — Revenue Exposure Agent
**File:** `track_f/f3_5_revenue_exposure.py`
**Schedule:** Monday 21:30 UTC (weekly — Sonnet call, cost-controlled)
**Model:** claude-sonnet-4-6

For each candidate ticker, extract revenue segment data from most recent 10-K (EDGAR).
Sonnet prompt:

```
Ticker: {ticker}
Theme: {theme}
Revenue segments from 10-K: {segments_text}

If the "{theme}" theme grows 2x over 3 years, what percentage of this
company's current revenue directly benefits?

Score 0-100:
100 = pure-play (nearly all revenue exposed)
50  = material segment, not dominant
10  = minor exposure, large conglomerate

Return JSON only, no preamble:
{"ticker": "X", "revenue_leverage": 72, "reasoning": "one sentence"}
```

Without this:
- VRT scores ~92 (pure-play data center thermal)
- ETN scores ~45 (electrical is large but ETN is diversified)
- MSFT scores ~12 (Azure grows but MSFT is too large)

Mega-caps are correctly penalised. Write to `nwt_revenue_exposure`.

---

### F4 — Bottleneck Scorer
**File:** `track_f/f4_bottleneck_scorer.py`
**Schedule:** Daily 22:00 UTC (weekdays)
**Model:** None — fully deterministic. No LLM.

```python
def compute_bottleneck_score(ticker: str, theme: str) -> float:
    tm  = get_latest(nwt_theme_momentum, theme)           # F0 output
    cs  = get_latest(nwt_constraint_signals, ticker)      # F2.5 output
    rl  = get_latest(nwt_revenue_exposure, ticker, theme) # F3.5 output
    ag  = get_latest(attention_gaps, ticker)              # F2 output
    sm  = get_latest(smart_money, ticker)                 # F3 output
    cr  = get_latest(nwt_crowding_scores, ticker)         # F3B output

    score = (
        tm.score             * 0.20
      + cs.constraint_severity * 0.25   # highest weight — this is what market underprices
      + rl.revenue_leverage  * 0.15
      + ag.attention_gap     * 0.15
      + sm.smart_money_score * 0.15
      - cr.crowding_score    * 0.10     # penalty, not disqualifier
    )
    return min(max(score, 0), 100)

# Threshold for F5 review: score >= 70
```

Write all scores to `nwt_bottleneck_scores`.
Write `shared/track_f_scores.json` (all candidates ranked).

---

### F5 — Track F Conviction Engine
**File:** `track_f/f5_conviction_engine.py`
**Schedule:** Daily 22:15 UTC (weekdays)
**Model:** claude-sonnet-4-6
**Input:** tickers from `nwt_bottleneck_scores` where `bottleneck_score >= 70`
          AND `attention_gap > 0`

**Vehicle selection is DETERMINISTIC. Sonnet does NOT choose the vehicle.**

```python
def select_vehicle(iv_percentile: float) -> str:
    if iv_percentile < 40:
        return "LEAPS"          # cheap optionality on multi-month thesis
    elif iv_percentile < 70:
        return "bull_call_spread"
    else:
        return "equity"         # options too expensive; smaller equity position
```

Get IV percentile from Alpaca options chain before calling Sonnet.

Strategy genome check (mandatory — same as all other tracks):
```python
genome = db.query(
    "SELECT * FROM nwt_strategy_genome WHERE strategy_id = %s",
    strategy_id
)
if not genome:
    raise RuntimeError(f"No genome row for {strategy_id} — refusing to run")
```

Sonnet prompt:
```
You are a bottleneck investment analyst for NorthWorldTrading Track F.

Ticker: {ticker}
Theme: {theme}
Bottleneck Score: {score}/100
Score Components:
  - Theme Momentum: {theme_momentum}
  - Constraint Severity: {constraint_severity} (backlog/lead time evidence: {evidence_summary})
  - Revenue Leverage: {revenue_leverage}
  - Attention Gap: {attention_gap} (score accelerating vs peers)
  - Smart Money: {smart_money_score}
  - Crowding Penalty: {crowding_score}
Current Regime: {regime}
Vehicle (already determined): {vehicle}
IV Percentile: {iv_pct}

Assess:
1. Is this a genuine structural bottleneck or a crowded narrative?
2. Is the attention gap real or explained by a known fundamental (earnings miss, guidance cut)?
   Set gap_explained: true if price weakness has a fundamental explanation.
3. What is the appropriate time horizon: 90 / 180 / 270 days?
4. Write the thesis in one paragraph (what is the bottleneck, why now, why this company).
5. Write a single sentence invalidation condition.

Return JSON only, no preamble or markdown:
{
  "strategy_id": "TF-{theme}-{ticker}-001",
  "ticker": "{ticker}",
  "theme": "{theme}",
  "bottleneck_score": {score},
  "conviction": 0.0-1.0,
  "vehicle": "{vehicle}",
  "vehicle_rationale": "one sentence explaining why IV percentile justifies vehicle",
  "direction": "long",
  "time_horizon_days": 90 or 180 or 270,
  "thesis": "...",
  "invalidation": "...",
  "gap_explained": false
}
```

Skip if `gap_explained = true` — Sonnet is flagging that the attention gap has a
fundamental explanation (price dropped on news, not overlooked). Do not generate candidate.

Write output to `nwt_track_f_candidates` and `shared/track_f_candidates.json`.

---

## Options Trade Parameters (Track F)

Track F uses LEAPS or bull call spreads only. Longer time horizons than C/D/E.

| Parameter         | Value                                      |
|-------------------|--------------------------------------------|
| Size              | 2% account per trade (same as C/D/E)       |
| Expiry (LEAPS)    | 180–365 DTE                                |
| Expiry (spread)   | 60–120 DTE                                 |
| Strike (LEAPS)    | 10–15 delta ITM (not ATM — thesis has time)|
| Strike (spread)   | ATM long / 1 strike OTM short              |
| Profit target     | 100% gain (LEAPS) / 50% max profit (spread)|
| Stop              | 40% premium paid (wider than C/D/E — time) |
| Hard close        | No intraday close — thesis-based exit only |
| Equity position   | 1–2% notional, no options layer            |

---

## Portfolio Brain — Theme Exposure Cap

Add to Portfolio Brain logic (master/portfolio_brain.js or equivalent):

```python
THEME_AGGREGATION = {
    "ai_power":      ["ETN", "PWR", "VRT", "POWL", "EMR"],
    "ai_networking": ["ANET", "AVGO", "CSCO"],
    "ai_cooling":    ["VRT", "TT", "GNRC"],
    "nuclear":       ["CCJ", "NNE", "LEU"],
    "robotics":      ["TDY", "ISRG", "ONTO"],
}

MAX_THEME_EXPOSURE_PCT = 0.15  # no more than 15% of portfolio in one theme
MAX_SINGLE_TICKER_PCT  = 0.05  # no more than 5% in one ticker

for theme, tickers in THEME_AGGREGATION.items():
    theme_notional = sum(ledger.open_notional(t) for t in tickers)
    theme_pct = theme_notional / total_portfolio_value
    if theme_pct >= MAX_THEME_EXPOSURE_PCT:
        reject(candidate, reason=f"theme_cap_reached:{theme} at {theme_pct:.1%}")
```

Add `theme_aggregation` block to `master-directives.json` output schema:
```json
"track_f_limits": {
  "max_theme_exposure_pct": 0.15,
  "max_single_ticker_pct": 0.05,
  "current_theme_exposures": {
    "ai_power": 0.08,
    "nuclear": 0.03
  }
}
```

---

## Cron Entries

Append to existing nwt_agents crontab. SHELL=/bin/bash must remain line 1.

```bash
# Track F — weekly research (Sunday UTC)
0 20 * * 0    cd /home/northworld/trading/nwt_agents && source .env && python3 track_f/f0_theme_monitor.py >> /tmp/f0.log 2>&1
30 20 * * 0   cd /home/northworld/trading/nwt_agents && source .env && python3 track_f/f0b_cluster_detector.py >> /tmp/f0b.log 2>&1
0 21 * * 0    cd /home/northworld/trading/nwt_agents && source .env && python3 track_f/f1_dependency_mapper.py >> /tmp/f1.log 2>&1

# Track F — weekly exposure (Monday UTC)
0 21 * * 1    cd /home/northworld/trading/nwt_agents && source .env && python3 track_f/f3_institutional_flow.py >> /tmp/f3.log 2>&1
30 21 * * 1   cd /home/northworld/trading/nwt_agents && source .env && python3 track_f/f3b_crowding_agent.py >> /tmp/f3b.log 2>&1
30 21 * * 1   cd /home/northworld/trading/nwt_agents && source .env && python3 track_f/f3_5_revenue_exposure.py >> /tmp/f3_5.log 2>&1

# Track F — daily scoring (weekdays, post US close)
45 21 * * 1-5  cd /home/northworld/trading/nwt_agents && source .env && python3 track_f/f2_divergence_scanner.py >> /tmp/f2.log 2>&1
50 21 * * 1-5  cd /home/northworld/trading/nwt_agents && source .env && python3 track_f/f2_5_constraint_detector.py >> /tmp/f2_5.log 2>&1
0 22 * * 1-5   cd /home/northworld/trading/nwt_agents && source .env && python3 track_f/f4_bottleneck_scorer.py >> /tmp/f4.log 2>&1
15 22 * * 1-5  cd /home/northworld/trading/nwt_agents && source .env && python3 track_f/f5_conviction_engine.py >> /tmp/f5.log 2>&1
```

---

## Strategy Genome Rows (insert before first run)

```sql
INSERT INTO nwt_strategy_genome (strategy_id, track, asset_universe, dte_min, dte_max,
  iv_filter_max, entry_threshold, stop_loss_pct, profit_target_pct, regime, version, active)
VALUES
  ('TF-ai_power-LEAPS',     'F', ARRAY['ETN','PWR','VRT','POWL','EMR'], 180, 365, 60, 0.70, 0.40, 1.00, 'risk_on', 1, true),
  ('TF-ai_power-SPREAD',    'F', ARRAY['ETN','PWR','VRT','POWL','EMR'],  60, 120, 80, 0.70, 0.50, 0.50, 'risk_on', 1, true),
  ('TF-ai_cooling-LEAPS',   'F', ARRAY['VRT','TT','GNRC'],              180, 365, 60, 0.70, 0.40, 1.00, 'risk_on', 1, true),
  ('TF-nuclear-LEAPS',      'F', ARRAY['CCJ','NNE','LEU'],              180, 365, 60, 0.70, 0.40, 1.00, 'neutral',  1, true),
  ('TF-robotics-LEAPS',     'F', ARRAY['TDY','ISRG','ONTO'],            180, 365, 60, 0.70, 0.40, 1.00, 'risk_on', 1, true),
  ('TF-copper-LEAPS',       'F', ARRAY['FCX','SCCO','WIRE'],            180, 365, 60, 0.70, 0.40, 1.00, 'risk_on', 1, true);
```

---

## Session Startup Checklist Addition

Add to CLAUDE.md session startup block:

```bash
# Track F health
cat /home/northworld/trading/shared/track_f_scores.json | python3 -c "
import json,sys; d=json.load(sys.stdin)
top = sorted(d, key=lambda x: x['bottleneck_score'], reverse=True)[:5]
for t in top: print(f\"{t['ticker']:6} {t['theme']:20} score={t['bottleneck_score']:.0f}\")
"

# Pending emerging themes (approve or reject weekly)
psql "$NWT_DB_DSN" -c "SELECT candidate_theme, momentum, detected_at FROM nwt_emerging_themes WHERE status='pending' ORDER BY momentum DESC;"

# Track F candidates pending Portfolio Brain approval
psql "$NWT_DB_DSN" -c "SELECT ticker, theme, conviction, vehicle, time_horizon_days, created_at FROM nwt_track_f_candidates WHERE status='pending' ORDER BY created_at DESC LIMIT 10;"
```

---

## Key Constraints

- F5 must query `nwt_strategy_genome` at startup — same rule as all tracks
- Vehicle selection is deterministic (IV percentile rule) — Sonnet explains, never decides
- Human approval required before any F0B theme enters the dependency graph
- Portfolio Brain caps theme exposure at 15% — PWR + ETN + VRT + POWL are one trade
- Historical validation must pass (≥3 of 4 tickers) before paper trading begins
- Track F candidates use longer DTE than C/D/E — do not apply intraday close rules
- `gap_explained = true` from F5 = skip candidate — price weakness has a fundamental cause
- Inactivity is valid — log to `nwt_tickets` with reason if no candidates scored ≥70

---

## Files To Create

```
/home/northworld/trading/nwt_agents/track_f/
  __init__.py
  shared_context_f.py          # imports regime + theme state
  f0_theme_monitor.py
  f0b_cluster_detector.py
  f1_dependency_mapper.py
  f2_divergence_scanner.py
  f2_5_constraint_detector.py
  f3_institutional_flow.py
  f3b_crowding_agent.py
  f3_5_revenue_exposure.py
  f4_bottleneck_scorer.py
  f5_conviction_engine.py
  validate_historical.py       # build and run FIRST
```

---

## What Success Looks Like

**Week 1:** validate_historical.py flags 3/4 historical winners with score ≥60 pre-move.
**Week 2:** F0–F2.5 running. `track_f_scores.json` produced daily. Constraint severity scores visible.
**Week 3:** F3–F5 running. First `track_f_candidates.json` produced. Portfolio Brain reviewing.
**Month 2:** First Track F positions open. Learning Agent logging TF- outcomes.
**Month 3:** Attribution data accumulating. Scoring weights tunable against outcomes.

---

*Last updated: 2026-06-04*
*Architecture finalised in Claude.ai before this build plan was produced.*
*Do not modify scoring weights without running validate_historical.py first.*
