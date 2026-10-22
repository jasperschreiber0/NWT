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
  One bounded AI call per run, at most 40 attempts per UTC day and four filings per call, classifies concrete action
  versus hypothetical or negated intent. Numbered passage IDs resolve to exact supporting text in the source.
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

## Diagnostic boundary: investor-observation-20261014-v3

This additive version accepts the frozen v1/v2 policy records and appends its own
boundary on the first actual run. Earlier evidence and pending forecast lineage are
not rewritten. The diagnostic module is included in the frozen code hash. Publishing
or merging this code does not authorize server deployment or a forced research run.

Classification failures now include allowlisted `stage`, `reason`, and `error`
category fields in new `model_error` records and the latest report. Validation reasons
distinguish schema, exact source-quote and action-consistency failures. Option entry
diagnostics distinguish quote validation, repeated pagination and chain-budget limits;
other failures retain their stage and a bounded generic category. Exception text,
dynamic exception class names, provider responses, prompts and credentials are never
copied into these diagnostics. An unknown ValueError remains `INVALID_VALUE`; it is
not assigned a speculative cause. SQLite failures still fail the cycle.

`classification_coverage` reports classified and unclassified totals, current pending
work, cooldown, ready, budget-blocked, expired, missing-purpose, historical-exclusion
and unresolved-with-error counts. `budget_blocked` can overlap `cooldown`; these are
constraints, not additive buckets. `EXCLUDED_ONLY` is explicit when all remaining
unclassified records are outside the existing eligibility rules. No exclusion is
converted into a classification or verified outcome. Old type-only error records
remain usable for unresolved counts without modifying their payloads.

Current pending classifications add an issue and make the report `DEGRADED`, even if
the cycle exits zero because only scheduled/cooldown/budget-limited work remains.
Coverage alone does not request whole-job retries. Actual classification, market and
option failures still request retries and exit nonzero. The daily 20-attempt limit,
one-hour filing cooldown, freshness and grounding gates, option request/window limits,
paper-only controls and trial adjudication policy are unchanged. No model switch or
extra API request is introduced by computing coverage.

Regressions: `research/test_investor_diagnostics.py` covers the observed ValueError
classes, secret-bearing exceptions, persisted unresolved work, the last budget attempt,
midnight cooldown, option recovery and unknown outcomes, v1/v2 preservation, and complete
cycles whose exit status differs from research-coverage health. Run alongside the
existing investor/backoff suites and the full Linux/PostgreSQL CI suite.

## Repair boundary: investor-observation-20261023-v4

ARKW was renamed to ARK Next Generation Technology ETF. The former INTERNET filename
still serves a valid CSV dated 6 October; the official TECHNOLOGY filename was verified
on 23 October Sydney time with 22 October holdings. The configured URL is now updated.
Source changes bypass a cached failure for the old URL; errors for the new URL still
use persistent bounded backoff. Stale dates, future dates and same-date revisions remain
ineligible. A missing week is not bridged into a fabricated purchase signal.
Official reference: https://www.ark-funds.com/funds/arkw

The model now selects an integer passage ID instead of reproducing a quotation. All
source characters and surrounding context are supplied in lossless slices. Stored
quotations and offsets are reconstructed locally from the immutable filing; unknown IDs,
invalid types, duplicate/missing response IDs and inconsistent action fields are rejected.
This verifies provenance, not the semantic correctness of the AI hypothesis.

Each call handles up to four filings and 30,000 source characters, with one call per run,
40 calls per UTC day and two attempts per filing under this protocol. Existing daily
spend counters are carried forward. First attempts precede retries; older fresh filings
are prioritised. A one-hour cooldown still applies. Successful members of a partly bad
batch are retained. Twice-failed active filings stay visibly quarantined and cannot signal;
expired filings remain excluded. Classification failures do not trigger whole-job reruns.
Token usage is recorded once per batch, with classifications referencing that batch.

v4 appends a boundary alongside v1/v2/v3 and preserves existing evidence, forecasts and
results. It changes collection/classification, not trade execution or entry risk limits.
The release test database explicitly uses UTF-8 to support the current schema comments.
