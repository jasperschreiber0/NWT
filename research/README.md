# NWT research protocol 20260912-v1

Target: 30% annual return after trading costs, not a promised result. Maximum acceptable drawdown is still awaiting the user's choice. Existing execution limits remain unchanged.

`collect.py` is read-only against the trading database and broker. It writes append-only content-addressed observations under `research/evidence/`. Repeated identical source rows preserve first-observed time; changed rows create new records. Old tickets captured now are historical imports, not proof that their market data was available at their original decision time.

The collector archives tickets, decisions, ledger and outcomes, decision inputs, genomes, flags, broker orders and positions, SIP stock snapshots, available selected-contract OPRA snapshots and advisory entry-capacity figures. Five-minute snapshots are not tick-level or exact decision-time quote capture. Unavailable data remains explicitly unavailable. Capacity reports do not reserve capital or replace execution risk checks.

Frozen shadow rules: liquid-ETF close above 200-session average; SPY/QQQ dip below minus one 20-session standard deviation while above the 200-session average; and SAFX's original unfiltered dip hypothesis. None submits trades. Daily observations retain code and rule versions. Subsequent 5/20-session underlying open-to-open returns are labeled fixed-horizon proxies, not full strategy backtests, options P&L or broker fills. Stale-session and retrospectively observed signals are excluded from those forward labels.

The independent five-year study remains under `research/nwt-review-20260912/`. Options-spread promotion requires point-in-time chain/quote coverage, IV/Greeks, realistic fills, expiration/assignment handling and cost stress. No model or strategy parameters are automatically optimized to reach 30%.

Operations: `nwt-research-collector.timer` runs every five minutes during a broad US-session window on weekdays, independently of the entry halt and desktop app. `research/evidence/latest.json` reports the most recent successful collection. Inspect the systemd journal for failures. No outbound user notification is emitted by the collector.

Paper workflow: one-shot Monday September 14, 2026, 13:30:15 UTC. VGK incident recovery -> AAPL reduction to approximately 30% using fresh account/quote data -> clean reconciliation -> one-share SPY long limit entry below $1,000 and immediate close -> final reconciliation -> release validation hold. The long QA direction replaces the earlier short preference once allocation creates long capacity; it is bounded by a buy limit. The test validates equity lifecycle only. QA records are excluded from strategy learning and ordinary equity monitoring. Failure retains an entry hold and durable state; no blind order replacement. Partial allocation fills require review and are not labeled completed.

State: `/var/lib/nwt-vgk-recovery/`, `/var/lib/nwt-paper-allocation/`, `/var/lib/nwt-paper-lifecycle/`. Original AAPL basis uncertainty is preserved; its allocation sale is not manufactured strategy P&L. The workflow uses normal permission and cap checks. Track D protection, mutation freeze, and other risk settings remain intact.
