# Investor research, 11 October 2026

The `investor-research` recorded job collects public evidence every five minutes.
It has no order authority. Existing paper execution remains with the execution engine.
Run it through `python3 ops/run_job.py investor-research` after installing with
`python3 ops/install_investor_research.py`. The installer preserves other jobs and
cron entries. Retries, overdue detection and research warnings use existing operations controls.

## Sources and hypotheses

- SEC Form 4: direct, P-coded executive/director acquisitions of at least $25,000.
  Explicit plans, grants, exercises, amendments and private-placement evidence are excluded.
  P codes can also describe private purchases, so remaining transactions are not labelled
  independently verified open-market purchases. Two disjoint owner groups totalling
  $100,000 within seven days form a cluster. Single purchases are a control.
- Structured Schedule 13D: Item 4 changes, resolved to a unique issuer ticker.
  One bounded AI call per run, at most 20 attempts per UTC day, classifies concrete action
  versus hypothetical or negated intent. Exact supporting text must occur in the source.
  AI direction remains a hypothesis. Amendments without Item 4 do not imply a new catalyst.
- ARKK, ARKQ and ARKW: official daily holdings. Median common-holding share changes
  estimate fund-wide flows. Three distinct dates with relative increases produce a proxy
  hypothesis. These are not confirmed trades; corporate actions are not independently
  verified. Delayed files remain context only and are disclosed in the daily report.
- Berkshire, Pershing Square and Duquesne: latest two original quarterly 13F filings,
  retained as delayed context. Related Pershing entities are not independent investors.
  No real-time trade inference or automatic portfolio mirroring.
- Independent agreement: at least two different signal families with disjoint recorded
  actor identities. This is an operational identity check, not proof of economic independence.

Initial imports are historical context, never fresh signals. Publication, first observation,
parsing and forecast-freeze times are retained. SEC feed pagination gaps are recorded;
the five-minute cadence does not guarantee complete coverage or speed of execution.

## Evaluation

Eligible active US equities need price >= $5 and 20-session average dollar volume >= $5m.
Forecasts freeze before a future session open and run for 5 or 20 sessions. Same-rule,
same-symbol, same-horizon outcomes cannot overlap. SPY is the benchmark; modeled costs
are 15 bps per side with 30 bps per-side stress. Source rankings, matched controls and
cohort comparisons are evidence only. No automatic capital weighting or qualification.

Stock-versus-options comparisons collect contemporaneous quotes within 20 minutes of
the frozen entry and exit opens. Bull put and bull call verticals use $1/$2 widths,
21–45 days to expiry, expiry at least seven days beyond the forecast exit, and <=$100
modeled maximum loss including assumed fees. Equity comparisons use <=$500 entry cost.
Different exposures are explicit; these are BBO estimates, not fills or account returns.
Missing quotes, infeasible spreads and missed windows are counted, never filled from
future data. Assignment, early exercise and guaranteed execution are not modeled.

Evidence lives in `research/investor-evidence/`: immutable SQLite records, hashed raw
public documents and `latest.json`. Policy and new research code are frozen by hash.
Changes require a new evidence version/directory; do not overwrite the old experiment.
The daily report includes counts and coverage limitations. Research-source failures are
isolated from unrelated paper trading, while broker reconciliation remains mandatory.

## Source backoff boundary: investor-observation-20261013-v2

This version appends a policy and `version_boundary` record in the existing SQLite
store, in one transaction. Existing v1 policies, source evidence, first-seen times,
forecasts and outcomes remain unchanged. New forecasts carry `policy_version`; pending
v1 forecasts continue evaluation with v1 lineage. Signal and scoring rules are unchanged.
The first actual v2 run records the boundary time. A different code hash under the same
v2 version still fails closed. Do not delete history or change its frozen policy to upgrade.

Each ARK fund persists its latest check, observed date, source hash, failure count and
`next_at`. Failed checks back off for 1, 2, 4, then at most 6 hours; healthy checks refresh
hourly. Failure state remains visible during backoff. Cached successes are rechecked at
UTC date boundaries, and every cycle rechecks the exchange-calendar session gate.
Snapshots older than five calendar days or in the future remain excluded. Only holdings
from the latest completed exchange session (close plus 15 minutes) can generate signals.
Same-date revisions remain quarantined even if the original bytes later reappear.

A completed cycle with only ARK-source failures writes `status=DEGRADED` and
`retryable=false`, then exits zero: the scheduler records completed work without replaying
the entire collector three times. The watchdog still warns on the degraded report.
Healthy funds, other sources and pending outcome evaluation continue. Storage failures,
market-evaluation failures and other retryable failures still exit nonzero. This does not
change the scheduler, trading controls, source cadence for other sources, or model budgets.

Validation adds `research/test_investor_backoff.py`, including a real SQLite boundary,
legacy-outcome continuation, per-fund cache recovery and a mocked-child scheduler test.
Publication does not activate this version; deployment requires separate authorization.

Validation: `python3 -m pytest -q research/test_investors.py`; the release validation
script also runs the existing trading and operations regressions against an isolated DB.
