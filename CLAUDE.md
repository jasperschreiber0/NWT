# NORTHWORLD TRADING
## CLAUDE.md — System Architecture & Build Reference
### Full rebuild after credential leak — May 2026

> Read this before touching anything.

---

## Current Status

> **NOTE** Rows below marked *(repo)* describe what's in this repository and can be verified by reading it. Rows marked *(server)* describe live infrastructure state this repo cannot see — re-verify those with the Session Startup Checklist below before trusting them; they were last confirmed on the date shown and may be stale.

| Check | Status | Date |
|---|---|---|
| VPS online *(server)* | Last confirmed intact | 2026-05-18 |
| Alpaca keys *(server)* | Last confirmed same (PA3844MEHFIO) | 2026-05-18 |
| Anthropic API key *(server)* | Last rotated | 2026-05-18 |
| Discord webhook | Dead, not replaced — Telegram (`nwt_agents/notifier.py`) is the live alerting channel instead | 2026-07-11 |
| Postgres nwt_agents DB *(server)* | Last confirmed state: wiped for rebuild | 2026-05-18 |
| PM2 stack *(repo)* | `ecosystem.config.cjs` defines all Track A bots + dashboard, `time_zone: 'UTC'` explicit on every app | 2026-07-11 |
| nwt_agents cron *(repo)* | `crontab.txt` defines the full conviction stack + risk/execution/learning/recon schedule, `SHELL=/bin/bash` first line, confirmed UTC | 2026-07-11 |
| db/schema.sql + migrate_*.sql *(repo)* | Present; apply in filename/date order — schema.sql alone is the Day-1 baseline only | 2026-07-11 |
| recon_agent.py *(repo)* | Built; `--gate` now auto-runs cold-start import first; `--clear-if-clean` added for human-acknowledged recovery | 2026-07-11 |
| no_trade_mode wiring *(repo)* | Built; `clear_no_trade_mode()` now reachable via `recon_agent.py --clear-if-clean` | 2026-07-11 |
| Heartbeat (engine↔risk) *(repo)* | Built, end-to-end wired (`execution/engine.py` writes, `risk_agent.py`/`integrity_gate.py` read) | 2026-07-11 |
| Directional cap (60%) *(repo)* | Built in `execution/engine.py` (`DIRECTIONAL_CAP_PCT`); distinct from `master/strategist.py`'s `PER_BOT_WEIGHT_CEILING` (0.65) — see Stack 3 | 2026-07-11 |
| Risk Agent sizing-reduction rules (3, 7) *(repo)* | Built — `sizing_multiplier` on `nwt_ticket_decisions`, applied by `execution_agent.py`; used to only log a warning | 2026-07-11 |
| Exit lifecycle (equity monitor + options close) *(repo)* | Built; equity monitor now prefers the ticket's own `stop_pct`/`target_pct` (persisted on the ledger row) over genome/hardcoded defaults | 2026-07-11 |
| Inactivity ticket taxonomy *(repo)* | Built; session scorecard now counts both Track A's `nwt_inactivity_log` and Track C/D/E's `nwt_tickets(type='inactivity')` | 2026-07-11 |
| Same-regime 5+ sessions rule *(repo)* | Built — `nwt_regime_history` + `regime_classifier.py`'s session-persistence check | 2026-07-11 |
| Server hardening *(server)* | Not verified in this pass — re-run the deploy steps in Server Hardening below and confirm with `sshd -T` | 2026-05-18 |

---

## What Changed in Rebuild vs Prior Architecture

- CEO layer removed (unnecessary complexity, token cost)
- Force trade mandate removed — inactivity is a valid logged state
- China bot added (FXI/KWEB/MCHI, policy/event driven)
- EU bot refocused on mean reversion (not just ETF proxies)
- AUS bot: dividend/momentum only, no options (ASX liquidity too thin)
- Discord alerting: dead for now, logging to Postgres only
- US bot loses execution authority — rewrote as pure signal generator; Portfolio Brain sizes and approves all trades
- Single Execution Engine — one service handles all order placement across all bots
- Internal Portfolio Ledger — single source of truth for all positions (equity + options), replaces reading Alpaca directly
- Startup Integrity Gate — system refuses to trade if any critical check fails
- Strategy genome is runtime — no agent may use hardcoded parameters; all agents query `nwt_strategy_genome` at startup
- Learning Agent split — Data Layer always active; Mutation Layer shadow mode from 30 trades, promotion only after 100+ trades across multiple regimes
- All prior cron jobs, PM2 config, and agent code must be redeployed from scratch

---

## The Goal

60-day paper trading window (started April 2026). Success = Learning Agent producing meaningful win-rate feedback per strategy (Track C/D/E).

> **NOTE** The 60-day window is not about making paper money. It is about generating a dataset clean enough to learn from. Schema completeness and attribution logging take priority over strategy sophistication.

---

## Full Roadmap

1. **Now** — Rebuild infrastructure. Get 4 bots + nwt_agents Track C/D/E paper trading options daily, generating decisions logged to Postgres.
2. **Next** — Learning Agent matching buy/sell pairs, computing per-strategy win rates with full attribution. Signal quality scored separately from PnL quality.
3. **Then** — Go live: options win rate >55%, profit factor >1.5, max single-day drawdown <3%, best strategy win rate >65%, consecutive losses <4.

---

## Core Philosophy

This system is a **learning and attribution engine first**, trading engine second. The real long-term asset is not the current strategy or alpha — it is the dataset, the attribution layer, the regime understanding, and the allocator intelligence built over time.

**What That Means in Practice**

- Smooth compounding beats unstable spikes. Optimise for survivability.
- Every component has a single defined role. No component may exceed it.
- Inactivity is a first-class state. "No edge present" is a valid logged outcome.
- LLM dependence reduces over time. As the system matures, classification and execution logic should migrate toward deterministic components. LLMs are strongest at synthesis and anomaly interpretation — weakest at consistent probabilistic calibration.
- Most catastrophic losses happen because systems continue operating while degraded. The Risk Agent is the single most authoritarian component.

---

## Infrastructure

| Item | Value |
|---|---|
| Server | root@62.238.12.201 (Hetzner CPX22, Ubuntu 24) |
| Base path | /home/northworld/trading/ |
| Shared dir | /home/northworld/trading/shared/ |
| Entity | Builda AI ABN 41 615 978 808 |
| Broker | Alpaca Paper — PA3844MEHFIO (~$97k equity, ~$83k options BP) |
| Alerting | Postgres logging (all agents) + Telegram (`nwt_agents/notifier.py` — kill switch, no_trade_mode, heartbeat loss, recon critical, daily digest). Discord dead, not replaced by Discord. |

---

## Session Startup Checklist

Run these before diagnosing anything:

**1. PM2 health**
```bash
pm2 list
```

**2. Did Track A bots produce candidates?**
```bash
cat /home/northworld/trading/shared/us-candidates.json
cat /home/northworld/trading/shared/eu-candidates.json
cat /home/northworld/trading/shared/aus-candidates.json
cat /home/northworld/trading/shared/china-candidates.json
```

**3. Did master-directives fire?**
```bash
cat /home/northworld/trading/shared/master-directives.json
```

**4. Did nwt_agents fire today?**
```bash
cd /home/northworld/trading/nwt_agents
set -a && source .env && set +a
psql "$NWT_DB_DSN" -c "SELECT created_at, from_agent, to_agent, type FROM nwt_tickets ORDER BY created_at DESC LIMIT 20;"
```

**5. Portfolio ledger state**
```bash
psql "$NWT_DB_DSN" -c "SELECT bot_source, asset, asset_type, status, entry_time FROM nwt_portfolio_ledger WHERE status='open' ORDER BY entry_time DESC;"
```

**6. Performance**
```bash
cat /home/northworld/trading/performance/trades.json | tail -20
cat /home/northworld/trading/performance/summary.json
```

> ⚠ Do not suggest fixes until you know what is actually broken.

---

## System Architecture

5-stack architecture. No component may exceed its role.

| Component | Role | Order Authority |
|---|---|---|
| Portfolio Brain | Reads ledger, classifies regime with confidence score, sizes/approves trades, outputs master-directives.json | None — allocator only |
| 4 Signal Bots (Track A) | US/EU/AUS/China — each with distinct alpha source, outputs candidates JSON only | Zero |
| Execution Engine | Lifecycle service — validates, places, tracks fills, writes ledger | Executes only what Brain approves |
| Internal Portfolio Ledger | Single truth source for all positions, all bots, all tracks | N/A |
| nwt_agents Options Stack | Track C/D/E conviction -> risk -> execution -> learning, runs via crontab | Via Execution Engine only |

**Mental Model**
- **Portfolio Brain** = fund manager (mathematical, not discretionary). Reads ledger. Computes risk. Allocates capital. Approves trades. Does NOT generate signals.
- **Signal Bots** = specialist PMs. Generate candidate trades only. Zero order authority.
- **Execution Engine** = broker desk. Zero opinion on whether to trade.
- **Internal Portfolio Ledger** = single source of truth for all open/closed positions across all bots and all tracks.

---

## Stack 1 — PM2 Equity Bots (Track A)

Config file: `/home/northworld/trading/ecosystem.config.cjs`
Process manager: PM2 — all processes must be in this file to survive reboot.

### Bot Architecture (4 bots, genuinely uncorrelated)

| Bot | Market | Alpha Source | Instruments | Holding Period |
|---|---|---|---|---|
| US Flow Bot | United States | Options flow, ORB, momentum | SPY, QQQ, AAPL, TSLA, NVDA | Intraday -> 5 days |
| EU Mean Reversion Bot | Europe | Mean reversion, ECB lag | VGK, EWU, FEZ | 2-20 days |
| AUS Dividend/Momentum Bot | Australia | Dividend capture, trend following | EWA, BHP, RIO | 1-8 weeks |
| China Policy/Event Bot | China/HK | Stimulus reaction, regulatory easing | FXI, KWEB, MCHI, BABA, TCEHY | 1 day -> 3 weeks |

### Capital Allocation (~$97k)

- US bot: $35k
- EU bot: $20k
- AUS bot: $20k
- China bot: $15k
- Cash reserve: $10k (never fully deploy)

### PM2 Schedule (UTC)

> **NOTE** Server timezone is confirmed UTC via `timedatectl` (see `crontab.txt`'s own header comment). `ecosystem.config.cjs` sets `time_zone: 'UTC'` explicitly on every app so this can't silently drift again — this file previously assumed AEST (UTC+10) for PM2's `cron_restart` while `crontab.txt` used literal UTC for the same server, meaning every job below could have been firing ~10 hours off depending on which assumption was actually correct. If in doubt, re-verify with `timedatectl` before trusting either file.

| PM2 Name | Schedule (UTC) | Path |
|---|---|---|
| master-strategist | 21:30 (after US close) | master/ |
| asx-strategist | 09:00 | asx/ |
| asx-executor | 09:30 | asx/ |
| ukeu-strategist | 09:30 | ukeu/ |
| ukeu-executor | 10:00 | ukeu/ |
| us-nightly | 10:30 | us/ |
| us-trader | 18:05 (14:05 ET ORB) | us/ |
| perf-tracker | 00:00 | performance/ |
| nwt-dashboard | always-on (FastAPI, port 8080) | dashboard/ |

China bot (`china-strategist`, `china-executor`) is **not** a PM2 app — it is event-triggered (fires after ADR liquidity confirmation post US open), so it runs from `crontab.txt`'s polling window (`0,30 14-18 * * 1-5` UTC) instead of PM2's `cron_restart`. Do not add PM2 entries for it: crontab already supervises it, and a second supervisor running the same scripts would trip the Startup Integrity Gate's duplicate-runner check.

### Bot Isolation Rules

| Bot | Allowed | Disallowed |
|---|---|---|
| US | Price + options flow + vol | Macro narrative weighting |
| EU | Mean reversion signals, ECB lag | US momentum triggers |
| AUS | 1-8 week trend + dividends | Intraday signals |
| China | Policy + liquidity + ADR spreads | US technical overlays |

> ⚠ Violation of isolation rules = silent architecture failure. Enforce in code, not convention. Bots silently converging to US macro proxy is the single most dangerous failure mode.

### US Bot — IMPORTANT

Script: `us/workspace-northworldtrading/bot/trade_1400_with_brackets.py`

- This script must be rewritten as a signal generator. It must **NOT** place orders.
- Output: `shared/us-candidates.json` only
- ORB scoring: SPY>=4, QQQ>=3, AAPL>=3, TSLA>=4, NVDA>=3
- Fire time: 18:05 UTC (14:05 ET) — NOT 18:00 (SIP data not ready at exactly 14:00 ET)

> **CRITICAL** Any version of this script that calls Alpaca order endpoints is wrong.

---

## Stack 2 — Portfolio Brain

Path: `/home/northworld/trading/master/`
Fires: 21:30 UTC daily (after US close)

### What Portfolio Brain Does (NOT a trader)

1. Reads `nwt_portfolio_ledger` for current exposure across all bots
2. Reads global market data (SPY, VIX, DXY, sector ETFs via Alpaca)
3. Reads market internals: breadth, put/call skew, sector dispersion, realized vs implied vol spread (see Market Internals section)
4. Estimates net delta, net vega, correlation clusters
5. Classifies regime with confidence score and transition risk (see Regime Model section)
6. Caps single-direction exposure explicitly when correlation risk is elevated
7. Outputs `master-directives.json`

### Regime Model

Regimes are not categorical. Markets transition between states and the transition phases are the most dangerous periods — not stable risk-on or stable risk-off.

**Regime Schema**
```json
{
  "primary_regime": "risk_on",
  "confidence": 0.62,
  "secondary_regime": "fragile_liquidity",
  "transition_risk": 0.41
}
```

Regime states: `risk_on | risk_off | inflation_concern | recession_fear | geopolitical_stress | fragile_liquidity | neutral`

**Regime Rules**
- `confidence < 0.5` → allocator becomes more conservative across all bots
- `transition_risk > 0.5` → reduce all sizing by 50%, flag for manual review
- If SPY above 5-day-ago price → `primary_regime` cannot be `risk_off`
- Same regime 5+ sessions → must cite price evidence or reclassify to `neutral`
- Kill switch: drawdown >8% OR VIX >40

### Market Internals

Portfolio Brain must detect fragility, compression, liquidity withdrawal, and crowded positioning before price fully reflects stress. Most blowups are visible in internals before they appear in price.

| Internal | Source | Priority |
|---|---|---|
| VIX | Alpaca feed (treat 0 as missing, not signal) | Day 1 |
| DXY | Alpaca feed | Day 1 |
| SPY / QQQ breadth | Alpaca data | Day 1 |
| Put/call skew | Options chain data | Day 1 |
| Sector dispersion | Sector ETF spread | Day 2 |
| Realized vs implied vol spread | Computed from price history + IV | Day 2 |
| MOVE index | External feed — add to roadmap | Phase 2 |
| Credit spreads (HYG/LQD) | Alpaca feed | Phase 2 |
| Correlation index | Computed from bot positions | Phase 2 |
| Liquidity metrics | Order book depth / bid-ask spread | Phase 2 |

### master-directives.json Schema

```json
{
  "date": "2026-05-20",
  "regime": {
    "primary_regime": "risk_on",
    "confidence": 0.62,
    "secondary_regime": "fragile_liquidity",
    "transition_risk": 0.41
  },
  "vix": 18.5,
  "global_kill_switch": false,
  "net_delta_estimate": 0.34,
  "net_vega_estimate": 0.12,
  "bot_permissions": {
    "us": { "status": "active", "capital_weight": 0.60, "size_cap": 1.0 },
    "eu": { "status": "reduced", "capital_weight": 0.10, "size_cap": 0.5 },
    "aus": { "status": "paused", "capital_weight": 0.00, "size_cap": 0.0 },
    "china": { "status": "active", "capital_weight": 0.30, "size_cap": 1.0 }
  },
  "conflict_notes": "...",
  "reasoning": "..."
}
```

> **CRITICAL** Portfolio Brain must NOT generate trade ideas, generate signals, or override strategy logic. Only allocation, throttling, and risk gating.

---

## Stack 3 — Execution Engine

Path: `/home/northworld/trading/execution/`
This is NOT a script. It is a lifecycle service.

### Interface Contracts

**Signal → Brain (bot output, pre-approval)**
```json
{
  "bot": "us",
  "symbol": "SPY",
  "direction": "long",
  "confidence": 0.78,
  "strategy_id": "US-ORB-001",
  "signal_quality": {
    "entry_timing_score": 0.82,
    "thesis_validity": "ORB confirmed above VWAP",
    "expected_move_capture": 0.75
  },
  "expected_payoff": { "target_pct": 1.2, "stop_pct": -0.6 },
  "rationale": "ORB breakout above VWAP, score 5/5"
}
```

**Brain → Execution Engine (post-approval, sized)**
```json
{
  "approved": true,
  "bot_source": "US_BOT",
  "symbol": "SPY",
  "direction": "long",
  "strategy_id": "US-ORB-001",
  "sized_notional": 7000,
  "capital_weight": 0.60,
  "size_cap": 1.0,
  "asset_type": "equity",
  "time_in_force": "day",
  "stop_pct": -0.006,
  "target_pct": 0.012
}
```

**Execution → Ledger (post-fill)**
```json
{
  "bot_source": "US_BOT",
  "asset": "SPY",
  "asset_type": "equity",
  "direction": "long",
  "delta_exposure": 1.0,
  "notional_risk": 7000,
  "entry_price": 512.40,
  "entry_time": "2026-05-20T18:07:22Z",
  "alpaca_order_id": "abc123",
  "slippage": 0.0003
}
```

---

## Stack 4 — Internal Portfolio Ledger

Single source of truth for all positions across all bots and all tracks.

> **CRITICAL** Portfolio Brain reads `nwt_portfolio_ledger` — NOT the Alpaca positions API. Alpaca is execution-only.

---

## Stack 5 — nwt_agents Options Bot

Path: `/home/northworld/trading/nwt_agents/`
DB: Postgres, database `nwt_agents`
Runs separately from PM2 via crontab.

### Agent Hierarchy (CEO removed)

- Track C (premium-seller, C1-C12)
- Track D (aggressive directional, D1-D12)
- Track E (vol desk / stat-arb, E1-E12; requires `quantitative_edge` field)
- Risk Agent (13 veto rules, no LLM — fires every 5 min)
- Execution Agent (places Alpaca options orders via Execution Engine)
- Learning Agent (daily 21:00 UTC — win rates per strategy)

### Cron Schedule (weekdays, UTC)

| Time UTC | What fires |
|---|---|
| 13:00-13:45 | Conviction stack (layer0 -> prescreener -> engine -> summary) |
| 14:00 | Track C + D decide |
| 14:30 | Track E decides |
| 14:00-18:00 (every 30min) | China strategist + executor (event-triggered, self-gates on ADR liquidity) |
| 13:00-20:00 | Risk Agent every 5min |
| 13:00-20:00 | Execution Agent every 5min |
| 13:00-20:00 | Execution Engine every 5min |
| 13:00-20:00 | Snapshot writer every 15min |
| 21:00 | Learning Agent + cost agent |
| 21:15 | Session scorecard (green/red) |
| 21:20 | Shadow decision evaluator |
| 21:30 | Morning triage digest (read-only, no trade authority) |
| 22:30 | DB backup (local dump, 7-day rotation) |
| 23:00 | Recon nightly (`recon_agent.py --nightly`) |

See `crontab.txt` for the authoritative, current schedule — this table is kept in sync with it, but the file is the source of truth.

> **CRITICAL** `SHELL=/bin/bash` must be the first line of crontab. `source` is bash-only — silently fails under `/bin/sh`. This caused 373 dead tickets in the prior deployment.
> Verify: `crontab -l | head -1`

### Conviction Stack

```
layer0_builder.py -> prescreener.py (Haiku) -> conviction_engine.py (Sonnet) -> conviction_summary_writer.py
```

- Data: Alpaca data API (not Polygon), Nasdaq API for earnings (not FMP)
- Output: `conviction_tickets.json`, `conviction_summary.txt`
- Authority chain: master-directives → conviction_tickets → track decisions
- All agents share `shared_context.py` (single import: regime + conviction + final_sizing)

### Options Strategy Rules

| Regime + IV | Strategy |
|---|---|
| risk_on + IV<30 | Long calls SPY/QQQ |
| risk_on + IV>50 | Bull call spread |
| risk_off + IV<30 | Long puts |
| risk_off + IV>50 | Bear put spread |
| neutral | Iron condor |
| geopolitical + IV>40 | VIX calls |
| recession + IV<35 | Long puts SPY |

Skip if: IV>80, signal<0.5, earnings within 5d, VIX>40, regime confidence <0.5.

### Options Trade Parameters

- **Size:** 2% account per trade
- **Expiry:** 7-21 DTE (spreads), 21-45 DTE (long options)
- **Strike:** ATM long / 1 OTM spreads
- **Profit target:** 50% max profit (spreads) / 100% gain (long)
- **Stop:** 50% premium paid
- **Hard close:** all positions by 15:45 ET, no exceptions

### Track E Requirement

> **CRITICAL** `quantitative_edge` field mandatory on every proposed trade. Missing = auto-rejected before Risk Agent. No exceptions.

### Strategy Genome — Runtime Rule

No agent may use hardcoded strategy parameters. At startup, every track agent must:

```python
genome = db.query("SELECT * FROM nwt_strategy_genome WHERE strategy_id = %s", strategy_id)
if not genome:
    raise RuntimeError(f"No genome row found for {strategy_id} — refusing to run")
```

> **CRITICAL** Hardcoded parameters = learning system does not exist.

---

## Risk Agent — Authority Model

The Risk Agent is the single most authoritarian component in the system. It has more effective power than every other component combined.

**What the Risk Agent Can Do**
- Reduce all sizing across all tracks
- Disable individual tracks
- Force liquidation of open positions
- Freeze mutation promotion (block Learning Agent from promoting any strategy change)
- Trigger cooling-off periods after consecutive losses or execution anomalies

### Risk Agent Escalation Triggers

| Condition | Response |
|---|---|
| VIX > 40 | Global kill switch — no new positions |
| Drawdown > 8% | Global kill switch — no new positions |
| Slippage expansion (>2x baseline) | Reduce all sizing 50%, flag for review |
| Consecutive losses >= 4 (same track) | Disable track, trigger cooling-off |
| Correlation spike (cross-bot) | Cap single-direction exposure |
| Regime confidence < 0.4 | Reduce all sizing 50% |
| Regime transition_risk > 0.6 | Pause new entries, hold existing |
| API anomaly detected | Pause execution, alert via Postgres log |
| Spread widening (>3x normal) | Pause execution for affected symbols |
| Execution engine unresponsive | NO-TRADE MODE — log and exit |

> **NOTE** 13 veto rules are enforced in code. No LLM. No discretion. The Risk Agent never trades through failures.

---

## Learning System

The system is NOT a logging system. It is a closed-loop learning system: observation → attribution → action change.

> ⚠ The mutation engine is the most dangerous component in the system. Most quant systems fail here. Shadow mode is mandatory before any promotion.

### Two Active Layers (build first week)

**Layer A — Trade Outcome Learner (always active)**

Logs every trade with full context. NEVER suggests changes. Only answers: what actually happened?

- Entry and exit price, time, slippage
- PnL, PnL pct, slippage-adjusted efficiency
- IV at entry and exit
- Regime at entry and exit (full regime object including confidence)
- DTE at entry, strategy ID
- Signal quality scores (entry timing, exit timing, thesis validity, expected vs realized move capture)

> **NOTE** Signal quality and PnL quality are scored separately. A correct signal with bad execution is not the same as a profitable random trade. Conflating them contaminates learning.

**Layer B — Attribution Engine (schema built week 1, logic after 30 trades)**

Factor-based decomposition only. No narrative.
- WRONG: "market was volatile"
- RIGHT: "vega exposure +42% correlated with loss cluster in IV>50 regime"

### Two Deferred Layers

**Layer C — Strategy Mutator (shadow from 30 trades, promote after 100+)**

Bounded mutations only: adjust DTE range, tighten IV filter, change entry threshold, adjust stop loss, reduce frequency per regime.

> ⚠ Mutation floor is 100+ trades per strategy bucket, across multiple volatility regimes, with frozen baselines and shadow-mode testing first. 30 trades is enough to observe, not enough to mutate. Promoting changes on 30 trades risks learning random variance, regime chasing, and overfitting to transient volatility structures.

Shadow mode means: the mutated strategy runs alongside the baseline, its hypothetical outcomes recorded, but no capital is allocated to it until it passes the learning gate.

**Layer D — Portfolio Allocator (after meaningful trade history per bot)**

Learns which bot works in which regime. Answers "where should capital go?" — NOT "what should we trade?"

### The Learning Gate

A strategy mutation is only promoted if:

- 100+ trades in sample (per regime bucket)
- Trades span at least 2 distinct volatility regimes
- Statistically meaningful edge shift (not noise)
- Improvement in at least ONE of: profit factor, drawdown reduction, win rate stability
- No degradation in tail risk
- Shadow-mode results consistent with backtest

### Strategy Decay Tracking

The most important early warning system is not "is this strategy profitable?" but "is the edge deteriorating?"

| Decay Signal | How to detect |
|---|---|
| Rolling expectancy decay | Compare 20-trade rolling expectancy to baseline |
| Diminishing payoff asymmetry | Win/loss ratio compressing over time |
| Increased false positives | Signal fires more often, edge per signal falls |
| Longer recovery times | Time between drawdown and new high increasing |
| Signal crowding | Multiple strategies converging on same entry conditions |

Good strategies die slowly before they die suddenly. The auto-retire trigger at 45% win rate is a lagging indicator. Decay tracking is the leading indicator.

### Success Metrics

**Primary metrics:**
Win rate, profit factor, max drawdown (go-live thresholds)

**Stability metrics (equally important):**
- Variance of returns — smooth compounding beats unstable spikes
- Volatility-adjusted expectancy
- Recovery speed — how long from drawdown to new high
- Drawdown duration — not just depth
- Regime adaptability — does the strategy degrade in specific regimes?
- Tail dependency — correlation with worst market outcomes
- Execution consistency — slippage stability over time

### Do Nothing as a First-Class State

Inactivity must be explicitly logged as a valid decision outcome. Learning Agent must distinguish:
- No edge present (correct inaction)
- Signal missed (execution failure)
- Regime mismatch (strategy skip)

---

## Stress Simulations (Pre-Go-Live Requirement)

Before go-live, the system must be able to answer "what breaks?" under historical stress scenarios. These are not for prediction — they are for survivability testing.

### Required Scenarios

| Scenario | Key stress | What to test |
|---|---|---|
| 2022 inflation shock | Rising rates, multiple regime shifts | Regime detection lag, correlation between bots |
| COVID volatility expansion (Mar 2020) | VIX >80, liquidity withdrawal | Kill switch timing, sizing behaviour at extremes |
| 2018 vol crash (Feb) | Short-vol blowup, rapid spike then recovery | Risk Agent response speed, slippage assumptions |
| China crackdown (2021) | Sector-specific, ADR discount widening | China bot isolation, portfolio brain correlation cap |
| Rapid rate repricing | Duration shock, credit spread widening | EU bot behaviour, cross-bot correlation |
| Liquidity air pocket | Wide bid-ask, partial fills, high slippage | Execution engine handling, ledger consistency |

### Questions to Answer

- What breaks first?
- Where does cross-bot correlation spike unexpectedly?
- Where does sizing logic fail?
- Which regime assumptions collapse?
- Does the Risk Agent respond before or after the damage?

---

## Reconciliation — Ledger vs Alpaca

The ledger is the source of truth for DECISIONS. Alpaca is the source of truth for FILLS. These can diverge (crash between fill and ledger write, manual intervention, wiped DB with live positions). Divergence is detected, never assumed away.

Recon agent: `nwt_agents/recon_agent.py`
Runs: (a) as Integrity Gate step 7 before every session, (b) nightly 23:00 UTC.

Mismatch classes:
- `in_alpaca_not_ledger` → CRITICAL: set no_trade_mode, write ticket. Untracked risk.
- `in_ledger_not_alpaca` → mark ledger row status='suspect', write ticket.
- `qty_mismatch` → CRITICAL: set no_trade_mode, write ticket.

Clean recon writes a ticket type='recon_ok' (so absence of recon is itself detectable).

**COLD START IS AN IMPORT, NOT AN ASSUMPTION.** On first run against an empty ledger, recon_agent imports live Alpaca positions into the ledger with bot_source='UNATTRIBUTED'. "Assume zero exposure" applies only when Alpaca confirms zero positions.

---

## no_trade_mode

Stored in `nwt_system_flags` table (flag='no_trade_mode'). When TRUE:
- Every trading agent (Track C/D/E, execution_agent, execution_engine) checks at run start and exits immediately.
- Only humans (or a clean recon gate after human acknowledgement) clear it.
- Set by: recon_agent (critical mismatch), risk_agent (kill switch, heartbeat lost).

---

## Startup Integrity Gate

Before ANY trading session begins, system must verify:

1. No duplicate runners (`ps aux | grep python3`)
2. DB connectivity (`psql "$NWT_DB_DSN" -c "\dt"`)
3. Alpaca connectivity (`GET /v2/account`)
4. Options chains accessible (`GET /v2/options/contracts?underlying_symbols=SPY`)
5. Ledger writable (test insert + rollback)
6. Execution engine live (heartbeat row fresh, or outside market hours)
7. Recon clean (`recon_agent.py --gate` exits 0)

> **CRITICAL** If ANY check fails → system enters NO-TRADE MODE, logs reason, exits. Does not attempt to trade through failures.

---

## Postgres Schema

### Core Tables (Day 1)

```sql
CREATE TABLE nwt_tickets (
  ticket_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  from_agent TEXT NOT NULL,
  to_agent TEXT NOT NULL,
  type TEXT NOT NULL,
  payload JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE nwt_ticket_decisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id UUID REFERENCES nwt_tickets(ticket_id),
  decision TEXT NOT NULL,
  reasoning TEXT,
  decided_by TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Enforce append-only on nwt_tickets
CREATE RULE no_update_tickets AS ON UPDATE TO nwt_tickets DO INSTEAD NOTHING;
CREATE RULE no_delete_tickets AS ON DELETE TO nwt_tickets DO INSTEAD NOTHING;
```

### Portfolio Ledger (Day 1)

```sql
CREATE TABLE nwt_portfolio_ledger (
  position_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bot_source TEXT NOT NULL,
  asset TEXT NOT NULL,
  asset_type TEXT NOT NULL,   -- 'equity', 'option'
  direction TEXT,             -- 'long', 'short'
  delta_exposure NUMERIC,
  notional_risk NUMERIC,
  entry_price NUMERIC,
  entry_time TIMESTAMPTZ,
  exit_price NUMERIC,
  exit_time TIMESTAMPTZ,
  realized_slippage NUMERIC,
  status TEXT DEFAULT 'open', -- 'open', 'closed'
  alpaca_order_id TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### Learning System Tables (Day 1 schema, data accumulates)

```sql
CREATE TABLE nwt_trade_outcomes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id TEXT NOT NULL,
  symbol TEXT, direction TEXT,
  entry_price NUMERIC, entry_time TIMESTAMPTZ,
  exit_price NUMERIC, exit_time TIMESTAMPTZ,
  pnl NUMERIC, pnl_pct NUMERIC,
  iv_at_entry NUMERIC, iv_at_exit NUMERIC,
  regime_at_entry JSONB,      -- full regime object {primary, confidence, secondary, transition_risk}
  regime_at_exit JSONB,
  dte_at_entry INTEGER,
  slippage NUMERIC,
  slippage_adjusted_efficiency NUMERIC,
  entry_timing_score NUMERIC, -- signal quality, separate from PnL
  exit_timing_score NUMERIC,
  thesis_validity TEXT,
  expected_move_capture NUMERIC,
  realized_move_capture NUMERIC,
  closed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE nwt_strategy_decay (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id TEXT NOT NULL,
  computed_at TIMESTAMPTZ DEFAULT NOW(),
  rolling_expectancy_20 NUMERIC,
  baseline_expectancy NUMERIC,
  expectancy_delta NUMERIC,
  win_loss_ratio_trend TEXT,  -- 'compressing', 'stable', 'expanding'
  false_positive_rate NUMERIC,
  avg_recovery_days NUMERIC,
  decay_flag BOOLEAN DEFAULT FALSE
);

CREATE TABLE nwt_strategy_genome (
  strategy_id TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  track TEXT NOT NULL,
  asset_universe TEXT[],
  dte_min INTEGER, dte_max INTEGER,
  iv_filter_max NUMERIC, entry_threshold NUMERIC,
  stop_loss_pct NUMERIC, profit_target_pct NUMERIC,
  regime TEXT,
  active BOOLEAN DEFAULT TRUE,
  parent_version INTEGER,            -- lineage for mutations
  created_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (strategy_id, version)
);
-- Exactly one active version per strategy
CREATE UNIQUE INDEX one_active_genome ON nwt_strategy_genome (strategy_id) WHERE active;

CREATE TABLE nwt_system_flags (
  flag TEXT PRIMARY KEY,
  value BOOLEAN NOT NULL DEFAULT FALSE,
  reason TEXT, set_by TEXT,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO nwt_system_flags (flag, value) VALUES ('no_trade_mode', FALSE);

CREATE TABLE nwt_heartbeat (
  service TEXT PRIMARY KEY,
  last_beat TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  status TEXT DEFAULT 'ok'
);

CREATE TABLE nwt_equity_curve (
  date DATE PRIMARY KEY,
  equity NUMERIC NOT NULL,
  source TEXT DEFAULT 'alpaca',
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### Schema Notes

- `nwt_tickets` columns: ticket_id, from_agent, to_agent, type, payload, created_at
- Decisions: INSERT into `nwt_ticket_decisions` — NOT update `nwt_tickets.decision` (append-only, enforced via Postgres rule)
- Track C/D/E data lives in Postgres, not trades.json
- Portfolio Brain reads `nwt_portfolio_ledger`, NOT Alpaca positions API
- `regime_at_entry` and `regime_at_exit` store full JSONB objects — not just a string label
- signal quality fields (`entry_timing_score`, `exit_timing_score`, etc.) populated at close, not at entry

---

## Credentials — Where Things Live

> **CRITICAL** NEVER paste `northworld-system.docx` in chat — contains live API credentials.

| Credential | Location |
|---|---|
| Alpaca API key/secret (ASX) | /home/northworld/trading/asx/.env |
| Alpaca API key/secret (UKEU) | /home/northworld/trading/ukeu/.env |
| Alpaca API key/secret (US) | /home/northworld/trading/us/.env |
| Alpaca API key/secret (master) | /home/northworld/trading/master/.env |
| Alpaca API key/secret (China) | /home/northworld/trading/china/.env |
| nwt_agents DB + Alpaca + Claude | /home/northworld/trading/nwt_agents/.env |

Key vars in `nwt_agents/.env`: `NWT_DB_DSN`, `NWT_ALPACA_BASE_URL`, `ANTHROPIC_API_KEY`

> ⚠ `NWT_ALPACA_BASE_URL` must NOT have a trailing `/v2` — causes double `/v2/v2/` in API calls → 404.

---

## Models in Use

| Model | Role |
|---|---|
| Claude Haiku | Classification / prescreener |
| Claude Sonnet | High-stakes decisions (Track C/D/E) |

Current model string: `claude-sonnet-4-6`
All model env vars overridable via `nwt_agents/.env`

> **NOTE** LLM dependence should reduce over time. As the system matures, classification logic migrates toward deterministic components. LLMs handle synthesis and anomaly interpretation. Stable statistical optimisation and deterministic execution belong in code.

---

## Claude Behaviour Rules

- **File edits:** Always ask before editing any file on the server — code, config, .env, or ecosystem files. No exceptions.
- **Restarts:** Check `pm2 list` first. Never restart without confirming it is actually down or erroring.
- **Bug found mid-session:** Fix it, then report exactly what changed and verify the fix worked. Do not claim fixed until verified.
- Never say "it will run tomorrow" or "should be fixed now" without evidence.
- Health checks must be outcome-based: "Did candidates.json update in the last 24h?" — not "files are fresh."

---

## Known Gotchas

| Gotcha | Detail |
|---|---|
| SHELL=/bin/bash | Must be first line of crontab — silently fails otherwise |
| NWT_ALPACA_BASE_URL trailing /v2 | Causes double /v2/v2/ → 404 |
| GTC orders | Required for ASX/UKEU; day orders for US only |
| ASX options | Not traded — liquidity too thin |
| VIX feed returns 0 | Treat as missing data, not a signal — do not use 0 |
| ORB timing | Fire at 18:05 UTC not 18:00 — SIP data not ready at exactly 14:00 ET |
| AEST vs UTC | Server is UTC, all cron times are UTC |
| Append-only tickets | UPDATE/DELETE on nwt_tickets raises an exception (trigger-enforced) — always INSERT to nwt_ticket_decisions |
| no_trade_mode flag | Checked by every trading agent at run start — TRUE means halt immediately, no exceptions |
| 15:45 ET hard close | Computed from ET with DST awareness (zoneinfo), never a fixed UTC time |
| Genome PK is (strategy_id, version) | Query uses AND active=TRUE — one active version per strategy enforced by partial unique index |
| nwt_trade_outcomes.position_id | Mandatory on every new row — fuzzy symbol/time attribution is a bug |
| Performance gates read pnl_adjusted | Not raw pnl — paper fills are fantasy, evaluate conservatively |
| Recon must pass before any session | in_alpaca_not_ledger is untracked risk — halt immediately |
| Cold start = Alpaca import, not zero-assumption | zero only when Alpaca confirms zero |
| Track C shares Track A Alpaca account | Ledger handles attribution |
| China bot cron | Placeholder only — actual trigger is ADR liquidity confirmation post US open |
| US bot order placement | Any version calling Alpaca order endpoints is wrong |
| Portfolio Brain data source | Reads nwt_portfolio_ledger — NOT Alpaca positions API directly |
| Strategy genome | Agents that do not query nwt_strategy_genome at startup are non-compliant — treat as a bug |
| Cold start | System assumes zero exposure until ledger populates — explicit, not an error |
| Regime is now an object | regime_at_entry/exit are JSONB — not plain text strings. Any agent reading regime as a string is non-compliant. |
| Mutation shadow mode | No strategy mutation may be promoted without shadow-mode results. Observing from 30 trades, promoting after 100+. |

---

## Remaining Architectural Risks

**Risk 1 — Cross-bot correlation under stress**
Lightweight net delta + net vega estimation built into Portfolio Brain on Day 2. Full cross-bot correlation classifier deferred. During global liquidity tightening, all 4 bots may still converge directionally — Portfolio Brain must cap single-direction exposure explicitly.

**Risk 2 — Strategy purity not enforced**
Hard isolation rules are specified per bot. Must be enforced in code. Bots silently converging to US macro proxy is the silent killer — isolation rules must be checked in each bot's decision layer, not just documented.

**Risk 3 — No capital reallocation engine**
Static allocation is fine for Phase 1. After Learning Agent produces rolling Sharpe per bot (requires meaningful trade history), capital shifts dynamically. Weak bot gets reduced. Strong bot gets scaled.

**Risk 4 — Market internals coverage incomplete**
Day 1 internals (VIX, DXY, breadth, put/call skew) are a minimum viable set. Phase 2 adds MOVE index, credit spreads, correlation index, and liquidity metrics. Until those are live, Portfolio Brain has incomplete fragility detection.

**Risk 5 — No survivability floor before go-live**
Stress simulations (see Stress Simulations section) must be run before go-live. The system has not been tested against 2022 inflation shock, COVID vol expansion, or the 2018 vol crash. These are required, not optional.

---

## Performance & Go-Live Thresholds

### Go-Live Thresholds (options)

- Win rate >55%
- Profit factor >1.5
- Max single-day drawdown <3%
- Best strategy win rate >65% — with ≥20 trades for that strategy (36 strategies on small samples: one WILL hit 65% by luck)
- Consecutive losses <4
- Stress simulations passed for all 6 required scenarios

### Phase 1 Thresholds (before options bot)

- Win rate >52%
- Profit factor >1.3
- Regime accuracy >55%
- Trade count 60+

### Strategy Retirement (auto-disable if)

- 20-trade rolling win rate <45%
- Drawdown >8%
- Sharpe collapse
- Regime mismatch
- Decay flag set in `nwt_strategy_decay` — leading indicator, check before lagging triggers

### Return Target Calibration

- 12-25% annualised: strong
- 25-35%: excellent
- 35-50%: rare, usually involves hidden tail risk

The system is optimised for survivability and medium-high compounding. Reaching 40%+ consistently requires leverage, concentration, or short-vol bias — all intentionally avoided in current design.

---

## Server Hardening (post-leak baseline)

- SSH: `PasswordAuthentication no`, key-only. `PermitRootLogin prohibit-password`.
- fail2ban active on sshd.
- Postgres listens on localhost only (`listen_addresses = 'localhost'`).
- .env files: `chmod 600`, owned by service user.
- Verify after any infra change: `sshd -T | grep -E 'passwordauthentication|permitrootlogin'`

**Deploy steps (run once, in a second SSH session before applying):**
```bash
# Confirm key auth works FIRST in a second session before disabling password auth
grep -E "PasswordAuthentication|PermitRootLogin" /etc/ssh/sshd_config
# Edit: PasswordAuthentication no, PermitRootLogin prohibit-password
# systemctl reload sshd
apt install fail2ban
# Postgres: edit /etc/postgresql/*/main/postgresql.conf → listen_addresses = 'localhost'
chmod 600 /home/northworld/trading/*/.env
```

> ⚠ Do NOT disable password auth until you have confirmed key-based login works in a live second session.
