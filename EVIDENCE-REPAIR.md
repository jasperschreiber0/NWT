# Evidence repairs — review candidate, not deployed

Base: main `46dd22de17a20763ec460fc99aeff5bd0e9c817e`. Separate from PR14; no model switch, paid inference, execution authority, deployment or trial reset.

## Learning

Ledger insertion now captures an immutable per-position evidence row in the same transaction. Exact ledger ticket and explicit proposal IDs must resolve to exactly one decision created before entry, with matching strategy and known decision regime. Missing/ambiguous/late evidence is recorded as ineligible. Symbol reuse never selects another ticket. Partial fills preserve the first entry context while existing recovery continues updating cumulative fill quantities.

Decision-time IV/regime stays labelled as decision-time. Fill-time IV, regime, exit IV/regime and DTE remain explicitly unknown without actual point-in-time measurements. There is no fallback to current market data or current genome archetype. New P&L uses actual cumulative filled quantity rather than requested risk budget. Existing outcomes are not rewritten.

The additive migration creates immutable snapshots and a verified-learning view. Only outcomes written under this evidence version are eligible. Every leg must be closed, unique and verified against the same decision before a spread enters learning. The snapshot join supports multiple outcomes per decision; the old scalar outcome link cannot represent a spread. Learning, mutation sample gates and allocator inputs use verified data. Risk's consecutive-loss query explicitly retains raw historical losses. Reporting raw accounting history remains possible.

Read-only production audit: 104 positions, 98 closed, 82 outcomes, 665 decisions; 2 outcomes lack position IDs and 24 closed positions lack ticket IDs. Across all positions: 26 missing exact tickets, 23 missing/ambiguous decisions, 55 candidate exact contexts. No historical position has this new immutable capture: **zero historical outcomes are automatically promoted**. The local reconstruction proposal preserves raw input, including explicit option-leg identities, and SHA256 `a698c80643ca9be4a697b754b7387c8fc79c3b7612f94470d7d65e6e2a74556f`. It is not a production migration or validation of old entry features.

Migration review: back up and test on an isolated DB copy; apply `db/migrate_2026_10_20_learning_evidence.sql` twice; verify raw row counts unchanged and historical learning eligibility zero; test a new synthetic complete spread. Deployment must apply the migration before this code. No backfill is supplied: historical correction requires a separately reviewed append-only correction record with original IDs/hashes and verifiable pre-entry sources. Do not remove the version guard or update raw outcomes to make old history eligible.

## Cost coverage

`ops/cost_accounting.py` accepts identified incremental execution receipts, explicit leg/fill/fee manifests and USD amounts. Replays deduplicate by source identity; conflicting records fail. Unbalanced partial fills cannot claim complete-trade net. Fill prices already include spread/slippage; actual fees are deducted once, stress haircuts reported separately. Fully loaded trade net remains unknown without operating-expense allocation.

Operating records need gross including tax, ISO currency, service period, source reference, resource mapping and explicit NWT allocation. UTC overlap accrual is linear; all five service categories need reconciled coverage or evidenced zero/non-applicability. Estimates never establish completeness. Unified performance now uses these records; a guessed monthly-cost dictionary cannot establish net profit. External cash is excluded from trading P&L, and broker fees already reflected in account equity are not deducted twice.

Read-only usage audit from October 4 23:06:39.318494 UTC through October 20 00:59:48.544622 UTC found 306 logged OpenAI requests, 83,286 input tokens, 18,605 output tokens, zero recorded cached tokens and USD 0.1242808 recorded **pretax estimate**. This is not provider-reconciled project coverage; research calls without unified usage capture may be absent. No new requests were made.

Missing evidence: complete OpenAI project/request export, prices/tax and research usage coverage; October Hetzner resource-level usage and workload allocation; market-data subscription period and account mapping; Railway/Supabase NWT allocation or verified non-applicability; complete per-trade execution fee manifests. No unrelated Kaspr/Worka invoice is allocated. September Hetzner invoice 089001253362 has USD 10.09 net, 1.01 tax, 11.10 gross for September, not October, and no established invoice-line/resource allocation or payment confirmation. The September Alpaca failed subscription charge is not a paid October expense. Unknown remains unknown, never zero.

## One preregistered hypothesis

`research/earnings_variance.py` fixes a 25-issuer universe and November 2, 2026–March 31, 2027 chronological holdout. No fitted coefficients: baseline is prior-20-session mean squared log return plus squared earnings opening gap / 5; candidate multiplies baseline by `1 + 0.5 * unresolved_uncertainty_score`. Outcome is next-five-session mean squared close-to-close log return. Acceptance requires 30 events, 5 issuers, >=5% QLIKE loss reduction and improvement in >=70% of issuers. This predicts variance, not executable returns.

Registration freezes code/policy and the separately reviewed issuer-event/exchange-calendar manifest. Without that manifest the harness refuses real predictions/evaluation. Every forecast uses a prospective existing model output with source publication/output timestamps, exact quote, prior prices and frozen future session dates. Exclusive-create receipts prevent replacement; submission attempts are retained. A single final evaluation requires the entire frozen population and mature outcomes. Missing events block acceptance. Historical model knowledge is not independent holdout evidence. Synthetic tests are explicitly engineering smoke and cannot pass research acceptance.

Status: policy/code preregistration is saved in `research/experiments/earnings-variance-v1/registration.json`, with a null input manifest; no real predictions/outcomes scored. Smallest blockers are a reviewed frozen event/calendar manifest and prospectively sealed uncertainty outputs plus their public sources/price evidence. No suitable independent output set has been established. This blocked registration cannot be overwritten: before the holdout starts, a new auditable registration must bind the reviewed manifest to the same policy, preserving this earlier record. No paid calls are authorized by this branch, and no collection job is installed. Policy registration without data is a blocked experiment, not evidence of an edge. Costs and accuracy remain separate; no profitability claim.

## Validation

Focused command: `python -m pytest -q tests_offline/test_entry_evidence.py tests_offline/test_learning_evidence_db.py ops/test_cost_accounting.py research/test_earnings_variance.py`.

Full command: `python -m pytest -q tests_offline nwt_agents/tests execution/tests master/tests ukeu/tests ops research` with isolated PostgreSQL via `NWT_TEST_DB_DSN`. Tests block external requests. The DB suite tests migration reapplication, immutable snapshots, legacy exclusion, complete spreads and duplicate outcomes. Existing recovery/aggregation fixtures explicitly model the new evidence boundary.

Windows full run before final integration: 294 passed, 56 skipped; 26 setup errors and 2 failures require PostgreSQL, one failure requires real Unix file locking. These are not a complete suite pass. Linux/PostgreSQL CI must pass before review completion. No repository lint/type-check configuration exists; compilation and whitespace checks are required, with focused static checks reported separately if available.

Initial Linux/PostgreSQL CI at `0ce2375` passed 381 full-suite tests and 24 focused execution tests (run 39351587673). Subsequent review adds instrument/expected-leg checks, preservation of the raw drawdown fallback, concurrent learning-job serialization, and more DB integration tests; the final revision requires its own CI pass. Local focused tests currently pass 23 cases; PostgreSQL tests run in CI. Ruff F/E9 checks on new modules/tests, whole-repository undefined-name/syntax checks and mypy (`--check-untyped-defs --follow-imports skip --ignore-missing-imports`) on the three new core modules pass. Type checks are focused, not a claim of fully typed application coverage.
