# Paper autonomy amendment — 18 September 2026

The $100,000 starting paper research plan continues through December. This
release does not enable live trading or claim the research has found an edge.

## Changes verified in this release

* Known single-leg delayed entry fills already recover by original client ID.
* Full delayed closes now recover by position-specific client ID before market
  hours and trading-hold checks. Recovery makes GET requests only. It validates
  symbol, side, requested/filled quantity, price and fill timestamp, then writes
  ledger, related close-ticket decisions and the broker receipt atomically.
  Learning ingestion subsequently picks up closed rows by position ID.
* A retry starting no longer clears a previous failed-job health condition.
  A completed successful run is required.
* Trial failures persist across intraday recovery. Existing failed-run and
  delivered-fault evidence also invalidates prior false clean-day credit.
* Daily research reports show observed sessions, counts of positive stressed
  means, the strongest stressed mean and its sample count, and explicit
  overlapping-observation and live-readiness limitations.

## Lifecycle audit and remaining engineering work

| Case | Current handling / remaining limitation |
|---|---|
| Accepted entry, delayed full fill, restart | Existing client ID; verified receipt recovery; no repost |
| Entry ledger succeeds, decision write fails | Recovery checks existing attribution before decision repair |
| Terminal partial single-leg entry | Exact filled quantity only when a broker fill timestamp exists |
| Nonterminal partial entry | Cumulative broker quantities and prices update the same ledger row; actions deferred while settling |
| Delayed multi-leg entry | Every leg validated and committed atomically using actual broker fills |
| Delayed full close, restart | Receipt recovery added; audit-write failure rolls back ledger and decisions |
| Partial or canceled close | Incremental closed segments and remaining quantity recorded atomically; canceled/expired remainder uses deterministic replacement identity, maximum three retries |
| Broker/ledger conflict | Trading hold remains; this release does not bypass reconciliation |
| Synchronous close accounting | Existing paths still need consolidation around the same atomic receipt writer |
| Hold and risk-reducing exits | Existing hold policy still blocks new broker actions; receipt recording continues |
| Unknown software failure | Requires diagnosis; watchdog retries do not create code fixes |
| Trial completeness | Failure accounting improved; full-session monitoring-gap qualification remains open |

These are engineering gaps, not reasons to increase order size. Research
counts are neither independent trades nor proof of live profitability. More
independent sessions and execution-quality evidence are needed before strategy
promotion. The report now states this instead of only advertising record counts.
