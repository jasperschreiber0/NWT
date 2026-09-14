# September–December paper research

Use the existing account with $100,000 starting paper capital through December
2026. Do not reset its balance or restrict it to $5,000. The proposed $5,000
live deposit is for a separate end-December readiness review, not an automatic
switch to real money. A 30%+ annual return is a research objective, not a promise.

The existing trading strategies continue on the paper broker. This release
increases research breadth; it does not change execution sizing, remove loss
guards, or give untested hypotheses order authority.

## Data and experiments

- 40 stocks and ETFs: SIP snapshots and one-minute bars every minute during
  the 12:00–22:59 UTC weekday collection window, covering US regular sessions
  across daylight-saving changes. Daily history is paginated across all names.
- 12 option underlyings: OPRA chains every five minutes, calls and puts with
  7–60 calendar days to expiry and strikes within 10% of underlying price.
  Store quotes, trades and available IV/Greeks exactly as returned. Closed-market
  quotes may be old; observation time does not imply executable freshness.
- Four hypotheses with two thresholds each: volume breakout, failed breakout,
  relative strength against SPY, and dip recovery. All eight variants record
  qualifying observations, including no-signal decisions.
- Record predictions only on fresh, contiguous regular-session bars. Bootstrap
  history cannot become backdated predictions. Measure 30- and 120-minute
  horizons from the next minute open strictly after the prediction was observed.
- Outcomes include 10- and 30-basis-point round-trip cost assumptions. They are
  underlying-price proxies, not actual fills or option strategy returns.
  Overlapping symbols, variants and horizons are correlated evidence, not
  independent trades. Short proxies omit borrow costs; neither cost scenario
  models market impact or guarantees execution.

The research scoreboard is in `research/discovery-evidence/latest.json` and the
daily operations report includes coverage and outcome counts. Compressed raw
observations and a separate SQLite database retain evidence for later review.
They do not pollute the broker ledger or mutate production genomes. The
supervisor records failures, retries and stale collection. Repeated timer ticks
cannot run overlapping collectors. No additional subscription was purchased.

Review robustness across sessions and regimes before promoting a hypothesis.
The existing learning jobs remain responsible for broker trade outcomes;
the new scoreboard supplies separate prospective pattern evidence.
