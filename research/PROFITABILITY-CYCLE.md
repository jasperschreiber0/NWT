# Prospective profitability research cycle — 24 September 2026

The paper account retains its $100,000 starting-capital basis. No live-money
activation or increase in trading size is part of this release.

## Running automatically

The discovery collector now includes XLC (41 symbols) and runs a separately
versioned experimental cohort. New rules investigate opening-gap continuation
and reversal, sector-relative strength, and three-day trend confirmation.
The last uses multi-day context with a 120-minute forward evaluation, not a
multi-day holding simulation. Existing event research continues testing delayed
reaction, reversal and persistent repricing with its own prospective records.

Each new observation freezes direction, regime, quote spread and observation
time before its next-minute entry. Contiguous future bars are required. A
symbol/rule cannot launch overlapping active horizons. SPY comparison and
instrument-specific observed entry spread plus 10 bps extra slippage are
recorded alongside a minimum 30 bps stress. These are underlying proxies, not
executable option profits. Short borrowing, exit spread changes and market
impact are not fully modeled.

The first 20 observed sessions are discovery. Later observations form a
separate validation period. At least 20 validation days, 100 observations and
two regimes are required before review eligibility. A positive conservative
day-level stressed-return bound is required. Weak completed candidates leave
the shortlist; their records and future observations remain available. Rules
and policy are hashed and versioned. Changes require a new cohort rather than
rewriting past predictions. Review status never grants order authority.

Nightly learning review writes strategy-level complete-trade attribution,
gross versus adjusted results, entry-hour and exit-reason buckets, holding
durations and exclusions for open/incomplete groups. Equity benchmark figures
are long buy-and-hold over the same holding intervals, not a whole-period
passive portfolio or evidence of alpha. Broker receipt measurements report fill
delay; decision-price slippage stays unknown without a linked quote.

An illustrative $5,000 check evaluates one whole share and minimum option
structures. It uses 10% position cost and 2% expiry-loss budgets as research
assumptions only. Assignment, commissions, broker permissions and buying power
remain separate requirements. The check does not change current sizing.

## Execution and operational changes

* SEC scoring retries only the read-only failed operation before database
  writes. The complete scanner is not blindly replayed inside the job runner.
* Partial entries update the same open ledger quantity and weighted entry
  price from cumulative broker evidence. Unresolved entries defer broker
  actions until quantities settle.
* Multi-leg recovery validates every leg, requires actual fill prices and
  timestamps, and commits all verified legs plus execution decisions together.
  The synchronous spread path shares this writer. Quote-price substitution is
  removed.
* Ambiguous or absent broker evidence still causes a hold. Partial/canceled
  closes remain explicitly held for residual-quantity attribution; this release
  does not introduce automatic replacement close orders. Full delayed-close
  recovery remains active.

Profitability is an outcome to measure, not a claim made by these changes.
These bounded recovery paths reduce intervention; unknown failures and missing broker evidence still require diagnosis.
