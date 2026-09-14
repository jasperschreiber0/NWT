# NWT autonomous paper operations

Approved scope: existing paper strategies and risk limits, recorded native jobs,
bounded recovery, verified reporting, and a 20-session operational acceptance trial.
No new paid service and no live-money trading are introduced.

## Operations

`run_job.py` records starts, completion, exit status and attempts in
`/var/lib/nwt-ops/operations.sqlite`. Broker jobs share an exclusion lock. Explicitly
safe jobs retry at most three times; trading jobs never get automatic replay from
the wrapper. Internal errors in otherwise successful processes are failures.
The existing cron schedules and research/event systemd timers use this runner.
PM2 supervises only the dashboard. Cron is started again if found inactive.

The watchdog runs every five minutes, checks broker calendar, signed aggregate
positions, outstanding orders, job deadlines, source freshness, and attribution.
It holds new entries when its result is missing, older than 15 minutes, or degraded.
An operational hold does not bypass or clear the existing risk/reconciliation flags.
Safe transient faults recover when fresh checks pass. Uncertain orders and quantity
mismatches require investigation and are never resolved by guessing fills.
Existing risk holds may also stop exit jobs; this release does not weaken them.

Alpaca entries use stable client IDs and durable once-per-session reservations.
Requests from an earlier US session are rejected. Regional premarket requests have
a six-hour age limit; other entries have a 30-minute limit. Reservations survive
failure and are not automatically released. Option closes use per-position IDs and
exact filled-quantity checks. A canceled/partial order is never blindly replaced.

## Learning and visibility

Canonical UUID attribution is repaired without discarding any old integer values.
Exact canonical decisions are replayed with `reconstructed_at`, never relabeled as
prospective observations. Daily learning/decay, shadow evaluation, and mutation
proposal jobs continue automatically. Frozen candidates now receive an evidence
review instead of silently skipping evaluation. Underlying-return proxies cannot
authorize options-genome promotion; execution-grade options evidence is still
required. Mutation freeze and existing sample/regime/risk gates remain in force.

`/api/operations` and the dashboard show collection timestamps, issues and trial
progress. Telegram accepts a single consolidated daily report at 23:20 UTC on
weekdays, after nightly reconciliation. Delivery is recorded only after Telegram
returns success. Fault alerts are deduplicated for an unchanged incident per day;
recovery is reported once. Existing critical risk alerts remain available.

## Trial

First eligible date is recorded in `/var/lib/nwt-ops/trial.json`; no historical
sessions are credited. Broker-confirmed trading dates count. Twenty consecutive
sessions must finish with successful required jobs, complete opportunity logging,
fresh feeds, clean signed reconciliation, no stale-session executions, no overdue
orders, and successful daily report delivery. Failure resets the streak. Simulated
failure-drill evidence is required before a session can pass. A later retry cannot
turn an already-failed session into a pass. State survives server/application restarts.

This trial validates operations, not profitable strategy performance. One clean
equity QA cycle and mocked option-close tests do not validate every broker scenario.
The current research population is too small to justify automatic option promotion.
Host-wide/network outages cannot be reported by a watchdog on the same host; an
independent external availability monitor remains a separate infrastructure layer.

## Recovery and inspection

- `systemctl status nwt-watchdog.timer nwt-daily-report.timer`
- `/var/lib/nwt-ops/status.json`, `trial.json`, `learning.json`, `logs/`
- `python3 ops/run_job.py opportunity-replay` safely repairs exact missing links.
- Never rerun an order worker merely because its status is uncertain. Inspect its
  stable Alpaca order and ledger first. Never clear a reconciliation hold automatically.
- Production release backups retain checkout, database, cron, PM2 dump and systemd
  configuration. Scheduler configuration is generated into the protected state dir.
