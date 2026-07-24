# NORTHWORLD — Strategy Discovery Platform
## Architecture Evolution Beyond CLAUDE.md v1 (Rebuild, May 2026)

> **Status: DESIGN DOCUMENT — not yet built.** Nothing in this file is implemented. It describes the target architecture for evolving NWT from "4 signal bots + one options stack" into a Strategy Factory capable of continuously discovering, validating, deploying, and retiring strategies. Every principle in `CLAUDE.md` (append-only tickets, genome-driven params, no_trade_mode, Risk Agent authority, ledger-as-truth, shadow-mode-before-promotion) is inherited, not replaced. Where this document adds a new authority boundary, it says so explicitly.
>
> This is a companion to `CLAUDE.md`, not a replacement. `CLAUDE.md` remains the source of truth for what is *currently deployed*. This file is the source of truth for what is *next*, in priority order, with the reasoning for why each piece exists and what breaks if it's skipped.

---

## 0. Why Evolve, Not Replace

The existing 5-stack architecture (Portfolio Brain / Signal Bots / Execution Engine / Ledger / nwt_agents Options Stack) already encodes the two hardest lessons in systematic trading: **strategies must be isolated from each other**, and **nothing gets promoted without evidence**. Track C/D/E's genome + shadow-mode + Learning Gate machinery is, in miniature, exactly what a Strategy Factory needs — it's just currently scoped to 36 strategies inside one options stack, sharing one Risk Agent, one Execution Engine, and no formal capital-competition layer above the bot-level allocator.

The evolution is: **generalize what already works, and add the layer that's missing** — a portfolio-of-strategies allocator that treats every strategy (equity or options, Track A or Track C/D/E or a future Track G) as a competing unit, and a research warehouse rich enough that an AI Research Agent (advisory only, never executing) can mine it for hypotheses instead of a human eyeballing `trades.json`.

Nothing here proposes removing the Risk Agent's authority, the append-only ticket ledger, the genome-versioning rule, or the shadow-mode gate. It proposes making all four apply uniformly across a much larger and more heterogeneous strategy population.

---

## 1. Strategy Factory

### 1.1 The core abstraction: `Strategy`

Today, "strategy" is implicit — a strategy is whatever `nwt_strategy_genome.strategy_id` names, and its code lives wherever `track_c.py`/`track_d.py`/`track_e.py` happen to branch on regime/track. That's fine at 36 strategies. It stops being fine at hundreds, across multiple tracks and asset classes, some equity, some options, some running on Track A's PM2 cadence and some on nwt_agents' cron cadence.

The Strategy Factory formalizes a strategy as a **self-contained, versioned, config-driven unit** with no shared mutable state with any other strategy. Concretely, every strategy is:

1. A row in `nwt_strategy_registry` (identity + lifecycle stage — see §2) joined to
2. A row in `nwt_strategy_genome` (the existing table, extended — see §4) for the currently-active parameter set, joined to
3. A **strategy spec file** (`strategy_specs/<strategy_id>.yaml` or `.py` depending on track) that is pure declarative/functional logic: given market state + genome parameters, emit a candidate signal or `None`. It may read genome and market data. It may not read another strategy's state, another strategy's open positions, or global mutable variables. It may not place orders — no strategy, in any track, has execution authority. That has been true for Track A since the rebuild and is now true universally.

This is a strict generalization of the existing rule ("no agent may use hardcoded parameters, all agents query nwt_strategy_genome at startup") — it just also forbids strategies from touching each other's state, which was never a risk at 4 tracks-worth of bots but becomes the primary risk at scale (see Risk 2 in `CLAUDE.md`: "bots silently converging to a shared proxy is the silent killer").

### 1.2 Required fields per strategy (registry + genome, combined view)

| Field | Table | Notes |
|---|---|---|
| `strategy_id` | registry | Immutable, human-readable (`US-ORB-001`, `OPT-C-IRONCONDOR-014`) |
| `hypothesis` | registry | Free text, one paragraph. Mandatory at creation — no strategy without a stated thesis. |
| `market_assumptions` | registry | Structured JSONB: regime dependency, vol regime, liquidity assumption |
| `entry_rules` / `exit_rules` | genome (versioned) | Already exists for options tracks (`entry_threshold`, `stop_loss_pct`, `profit_target_pct`); generalized to equity strategies |
| `volatility_assumptions` | genome | IV regime this strategy is designed for; used by the allocator to reject in wrong-vol-regime |
| `risk_limits` | registry | Max position size, max concurrent positions, max daily loss — hard caps checked by Risk Agent, not self-reported |
| `sizing_model` | genome | Reference to a sizing function (fixed-fraction, vol-targeted, Kelly-fraction-capped) — see §9 |
| `instruments_traded` | registry | Explicit allow-list; anything else is a Risk Agent veto |
| `capital_allocation` | allocator output (not self-declared) | Strategies request, they do not set; see §3 |
| `lifecycle_stage` | registry | Enum, see §2 |
| `confidence_score` | computed | Rolling, from Learning Gate statistics — not hand-set |
| `track` / `asset_class` | registry | equity / option; A / C / D / E / future |
| `parent_strategy_id` | registry | Set when a strategy originates from a mutation or an AI-generated hypothesis experiment |
| `created_at`, `retired_at`, `retirement_reason` | registry | Append-only history |

### 1.3 Isolation enforcement (not just convention)

`CLAUDE.md` already flags this as Risk 2 ("must be enforced in code, not convention"). At Strategy Factory scale it needs a concrete mechanism:

- Each strategy spec is invoked in a **pure function call** with an immutable snapshot of market state passed in — no shared DB connection, no shared in-memory cache across strategies within a single evaluation pass. If two strategies need the same expensive computation (e.g. an IV surface), it's computed once upstream and passed to both as the same immutable input — never recomputed with side-effectful caching that could drift between them.
- A nightly **isolation auditor** (`strategy_isolation_auditor.py`) statically scans strategy spec files for cross-strategy imports or writes to any table other than its own decision/outcome rows, and fails CI / raises a ticket if violated.
- Bot-level isolation rules (US/EU/AUS/China market-data boundaries from `CLAUDE.md` §"Bot Isolation Rules") become a special case of strategy-level isolation, enforced the same way.

---

## 2. Strategy Lifecycle

### 2.1 Stages

```
RESEARCH → BACKTEST → PAPER → SHADOW → CAPITAL_1PCT → CAPITAL_5PCT
    → CAPITAL_10PCT → FULL_ALLOCATION → MONITORING → RETIRED
                              ↑______________________|
                        (demotion path — see 2.3)
```

`nwt_strategy_registry.lifecycle_stage` is the enum. Stage transitions are **one-directional forward under evidence, and reversible backward under decay** — a strategy can be demoted from `FULL_ALLOCATION` straight to `MONITORING` (paused, zero capital, still tracked) or `RETIRED`, but can never skip a forward stage. This mirrors the existing Learning Gate philosophy exactly (100+ trades before mutation promotion) — it's the same gate, applied to strategy birth instead of strategy mutation.

### 2.2 Stage gates (promotion criteria)

| Transition | Gate |
|---|---|
| RESEARCH → BACKTEST | Hypothesis + entry/exit rules formally specified; passes static isolation audit |
| BACKTEST → PAPER | Backtest run against the full historical regime library (§7); Sharpe/Sortino/Calmar computed; not overfit per §7.4 overfitting checks |
| PAPER → SHADOW | 30+ paper trades (mirrors existing "shadow from 30 trades" mutation rule) with no execution authority, logged identically to live trades in the research warehouse |
| SHADOW → CAPITAL_1PCT | Same **Learning Gate** already defined in `CLAUDE.md` for mutations: 100+ trades, 2+ distinct vol regimes, statistically meaningful edge, no tail-risk degradation. A brand-new strategy passes through the *identical* gate a mutation does — there is one gate in this system, not two. |
| CAPITAL_1PCT → 5PCT → 10PCT → FULL | Each step requires a fresh minimum trade count *at that capital level* (fills/slippage behave differently at different size — a strategy proven at 1% capital has not proven it survives its own market impact at 10%). Suggested floor: 20 trades per step, no regression in Sharpe vs. the prior step. |
| FULL_ALLOCATION → MONITORING | Continuous — see §8 (Continuous Competition). Not a one-time gate; monitoring is where every strategy lives once fully proven, permanently. |
| MONITORING → RETIRED | Auto-retire triggers from `CLAUDE.md` (20-trade rolling win rate <45%, drawdown >8%, Sharpe collapse, decay flag) or manual governance action. |

### 2.3 Demotion is not punishment, it's the design working

A strategy dropping from `FULL_ALLOCATION` to `CAPITAL_5PCT` on a decay flag is the expected steady-state behavior of a healthy factory, not an incident. `nwt_strategy_registry` logs every transition (append-only, same pattern as `nwt_tickets`) so the full capital history of every strategy is reconstructable.

### 2.4 Nothing skips stages — mechanically, not just by policy

The allocator (§3) reads `lifecycle_stage` and enforces the maximum capital ceiling per stage as a hard cap, independent of what the strategy or any override requests. A strategy in `SHADOW` requesting capital is a bug report, not a signal to size it — the allocator logs and rejects, same posture as the Risk Agent's veto rules.

---

## 3. Portfolio of Strategies — the Allocator

### 3.1 Reframing the central question

`master/allocator.py` already exists and already answers "where should capital go?" at the 4-bot level, tilting `BASELINE_WEIGHTS` by a bounded z-score once a bot clears 15 trades. The Strategy Factory generalizes this from **4 bots** to **N strategies** (N growing from dozens to hundreds) and adds the missing dimensions: correlation, liquidity, and tail risk are not yet inputs to `compute_dynamic_weights()` per the current `CLAUDE.md` description (only Sharpe-like performance and regime-conditioning are).

### 3.2 Allocator inputs, per strategy, per cycle

- Expected return, volatility, drawdown (existing, from `nwt_trade_outcomes`)
- Sharpe, Sortino (downside-only), Calmar (return / max drawdown) — Sortino and Calmar are new computed views, not new raw data
- Regime-conditioned performance (existing pattern from Layer D, generalized past 4 buckets)
- Confidence score (from Learning Gate sample size + statistical significance, not a vibe)
- **Correlation matrix** across all active strategies' realized P&L series — new. This is the single most important missing piece: `CLAUDE.md` Risk 1 ("cross-bot correlation under stress... deferred") stops being a deferred risk once dozens of strategies are running, because uncorrelated-by-construction stops being a safe assumption to hand-wave.
- **Liquidity** — position size relative to average daily volume / options open interest; a strategy that scales in backtest but can't fill at size in production is a capital-efficiency bug, not an edge.
- **Tail risk** — CVaR / expected shortfall at the 5th percentile, not just max drawdown, because two strategies with identical Sharpe can have very different tail behavior.

### 3.3 Allocation as constrained optimization, not weighted average

The existing bounded-z-score tilt (±25% of baseline, no bot dominates) is a reasonable heuristic at 4 strategies. At N strategies it should become an explicit constrained optimization: maximize portfolio Sharpe (or a drawdown-penalized variant) subject to:

- Per-strategy cap (no single strategy > X% of risk capital — generalizes the existing `PER_BOT_WEIGHT_CEILING` / `DIRECTIONAL_CAP_PCT` concept)
- Per-correlation-cluster cap (strategies whose P&L correlation exceeds a threshold are treated as one exposure for capping purposes — this is how the system avoids "20 strategies that are secretly all long-vol" quietly recreating single-strategy concentration)
- Per-sector / per-asset-class cap
- Portfolio-level volatility target (vol targeting, not just capital targeting — a strategy sized the same in a calm regime and a stressed regime is a bug)
- Lifecycle-stage capital ceiling (§2.4, hard constraint, not optimized away)

This is a solvable convex(-ish) problem at the scale in question (hundreds, not tens of thousands, of strategies) — a standard quadratic-program formulation (mean-variance with position and cluster caps) is sufficient; no need for anything exotic. Recommend implementing as `master/portfolio_optimizer.py`, called from `strategist.py` after `allocator.py`'s per-bucket performance stats are computed, with `allocator.py`'s existing bounded-tilt logic retained as the fallback/cold-start path when the optimizer's inputs are too sparse (mirrors the existing cold-start-returns-baseline-weights behavior).

### 3.4 Authority boundary

The Portfolio Optimizer has exactly the same authority as the existing Portfolio Brain: **allocation, throttling, risk gating — never signal generation, never strategy logic override.** It sits above the existing Portfolio Brain / `master-directives.json` layer conceptually (it decides strategy-level and bot-level weights together, since bots and options strategies are now the same kind of object — competing units), but does not gain any new authority kind. The Risk Agent's veto authority remains strictly senior to the allocator's weighting decision, exactly as today.

---

## 4. Research Data Warehouse

### 4.1 What's already logged vs. what's missing

`nwt_trade_outcomes` (existing) already captures PnL, slippage, IV at entry/exit, regime object, DTE, signal-quality scores. That's a solid Layer-A foundation. What's structurally missing for institutional-grade research:

**Market state (new table `nwt_market_state_snapshot`, one row per strategy-decision timestamp, not just per trade)**
- VIX, VVIX, realized vol (multiple lookback windows), IV rank, IV percentile, skew (25-delta risk reversal), term structure (front-month vs. back-month IV spread), breadth, put/call ratio, a dealer-positioning proxy (gamma exposure estimate from open interest — approximate, documented as approximate, not fabricated precision), macro event calendar proximity, earnings proximity.
- This must be captured **at decision time**, not reconstructed later — IV rank six months from now is not the IV rank the strategy actually saw. This is a new invariant: every strategy decision snapshots the market state it acted on, immutably, alongside it (foreign key from `nwt_trade_outcomes` / `nwt_ticket_decisions` to `nwt_market_state_snapshot`).

**Execution quality (new table `nwt_execution_quality`, one row per fill)**
- Quoted spread at order time, effective spread realized, latency (decision timestamp → order timestamp → fill timestamp, all three, not just slippage), fill quality classification (full/partial/none), partial-fill remainder handling. This directly extends the existing `slippage` field into its components — slippage is an output, these are the inputs that explain it, which is what makes "spread widening >3x" (an existing Risk Agent trigger) attributable after the fact instead of just detected in real time.

**Strategy metadata (extends `nwt_trade_outcomes`, most fields already present)**
- `hypothesis_id` (FK to registry), `genome_version` (already exists per the mutation-shadow pattern), `model_version` if an ML/statistical component is involved, `expected_edge`, `expected_probability`, sizing decision and the model that produced it (FK, not inline params — the sizing model itself gets versioned).

**Risk / Greeks (new table `nwt_position_greeks`, snapshotted at entry, at each risk-agent poll, and at exit)**
- delta, gamma, theta, vega, rho per position, aggregated to portfolio-level net Greeks. This feeds both the existing Portfolio Brain net-delta/net-vega estimate (currently "lightweight," per Risk 1) and the tail-risk input to the allocator (§3.2).

### 4.2 Design constraint: queryable years later

Two consequences:
1. **No destructive schema changes.** Every genome/registry/spec change is a new version row, never an in-place mutation of a historical parameter — this is already the pattern for `nwt_strategy_genome`; the warehouse tables inherit it.
2. **Partitioning.** `nwt_market_state_snapshot`, `nwt_execution_quality`, and `nwt_position_greeks` will be the highest-row-count tables in the system (per-decision, not per-trade). Partition by month from day one (native Postgres declarative partitioning) so 10+ years of history stays queryable without a later migration crisis.

---

## 5. AI Research Layer

### 5.1 Strict authority boundary

The AI Research Agent (`nwt_agents/research_agent.py`, new) has **read-only** access to the research warehouse and **zero** write access to genome, registry, orders, or ledger. Its only output is a row in a new append-only table, `nwt_research_hypotheses` — text + supporting statistics + a proposed experiment design. This is the same authority discipline already applied to the Mutation Engine ("AI never deploys changes directly... humans approve experiments") — the Research Agent is one step further removed than the Mutation Engine: it doesn't even get to write a shadow genome row itself. A human (or the existing Mutation Engine, after human sign-off on the hypothesis) turns a hypothesis into an actual shadow-mode experiment.

### 5.2 Responsibilities

- Pattern identification across strategies/regimes ("momentum strategies underperform when VIX crosses above 28 after 3 consecutive up days" — the example given is exactly the right shape: a specific, falsifiable, testable claim, not a narrative).
- Losing-streak analysis: cluster losing trades by market-state features (§4.1) and surface the shared feature, not just "these lost money."
- Winning-trade clustering: same, inverted — what do the best trades have in common that the strategy's own entry rule doesn't currently check for?
- Failure explanation: for a retired or decaying strategy, produce a factor-based writeup in the same style as the existing Layer B Attribution Engine ("vega exposure +42% correlated with loss cluster in IV>50 regime" — right; "market was volatile" — wrong). The Research Agent is Layer B's natural consumer/extension, not a replacement for it.
- Experiment suggestion: propose a new strategy spec, a new mutation candidate, or a new data feature to capture — always as a hypothesis row, never as a deployed change.

### 5.3 Guardrails against the two failure modes of AI-driven research

1. **Multiple-comparisons / p-hacking.** With hundreds of strategies and years of history, an LLM mining for patterns will find spurious correlations by construction. Every hypothesis row must carry a sample size, an out-of-sample holdout check (the Research Agent must be given a train/test split of the warehouse, never the full history at once), and a plain-language confidence caveat. Hypotheses are inputs to human judgment, not conclusions.
2. **Narrative-fitting.** The existing rule ("WRONG: market was volatile / RIGHT: vega exposure +42% correlated with...") is enforced by *requiring the hypothesis row to reference specific columns in specific warehouse tables* — a hypothesis that can't be phrased as a query is rejected by the ingestion step, not just discouraged by prompt instruction.

---

## 6. Strategy Mutation Engine (extension of existing `mutation_agent.py`)

The existing engine already gets the hard part right: one parameter at a time, shadow mode mandatory, Learning Gate for promotion, append-only `nwt_mutation_log`. Extensions needed for Strategy-Factory scale:

- **Explicit expected-improvement + rollback spec per mutation.** Add columns to the shadow genome row (or a companion `nwt_mutation_proposals` table): `rationale`, `expected_improvement_metric` + `expected_improvement_magnitude`, `estimated_risk`, `success_metric_definition`, `rollback_condition`. Today the file's docstring documents *which* parameters are mutable (`entry_threshold`, `iv_filter_max`, `stop_loss_pct`); it should also require, per proposal, a machine-readable rollback trigger (e.g. "if shadow Sharpe < baseline Sharpe − 0.3 after 50 shadow trades, auto-retire the candidate") so demotion isn't a judgment call made after the fact.
- **Source-agnostic proposals.** Currently proposals are self-generated from `decay_flag`/`win_loss_ratio_trend`. Add a second proposal source: an approved Research Agent hypothesis (§5) can seed a mutation proposal, going through the identical shadow → Learning Gate pipeline — no shortcut for AI-originated ideas.
- **DTE-range and per-regime-frequency mutations** — explicitly out of scope in the current docstring ("not yet implemented"). These are legitimate next mutations once the single-parameter-at-a-time pipeline is proven at scale; do not implement multi-parameter mutations even then — instead, run them as *sequential* single-parameter shadow experiments, preserving the one-change-at-a-time invariant.

---

## 7. Massive Backtesting Infrastructure

### 7.1 Why this is new, not an extension

Nothing in the current repo runs a historical backtest — Track C/D/E are paper-trading forward in real time. A Strategy Factory cannot gate `BACKTEST → PAPER` (§2.2) without one. This is new infrastructure: `research/backtest_engine.py` + a historical data store, `nwt_historical_bars` / `nwt_historical_options_chains` (new, separate from the live warehouse — 10-20 years of daily/intraday equity bars and options chains is a materially different storage and access pattern than live decision logging).

### 7.2 Regime library

A curated, labeled set of historical windows the backtester must run every candidate strategy against before `BACKTEST → PAPER`:
- 2008 GFC, 2020 COVID crash + recovery, 2022 inflation/rate-shock, 2018 Feb vol crash, 2015 China devaluation, a multi-year low-vol grind (2017 or 2012-2014), a high-rate regime (2022-2023), at least 2 major earnings-season windows, at least 1 flash-crash-style liquidity event (May 2010 or similar).
- This is the same list as `CLAUDE.md`'s "Stress Simulations (Pre-Go-Live Requirement)" section — that section already specifies the required scenarios for the *current* system's go-live. The Strategy Factory's backtest regime library is that same list, generalized to run per-candidate-strategy instead of once per system, and run automatically as a stage gate instead of manually before go-live.

### 7.3 Realism requirements

- Commissions and per-contract options fees, modeled explicitly, not ignored.
- Slippage model calibrated from the *live* `nwt_execution_quality` table (§4.1) — once enough live execution data exists, backtests use empirically-observed spread/impact, not a flat assumed bps.
- Early assignment risk for short options legs (American-style equity options) — a real and currently-undocumented gap; any short-leg spread strategy (`bull_call_spread`, `bear_put_spread`, `iron_condor` per the existing execution engine) must model assignment risk in backtest, since it's real risk in production.
- Liquidity limits — a backtest that assumes a strategy can execute its full backtested size at every historical timestamp regardless of historical volume/open interest is not a backtest, it's a fantasy. Cap simulated fill size to a fraction of historical volume/OI.

### 7.4 Overfitting rejection

- Mandatory train/test split (e.g. in-sample parameter selection on the first 70% of history, out-of-sample validation on the remaining 30%, never the reverse).
- Walk-forward validation, not a single static split, for any strategy with tunable parameters.
- Deflated Sharpe ratio or an equivalent multiple-testing correction when a strategy was selected from a larger search space of parameter combinations (the more variants tried, the more the raw backtested Sharpe must be discounted before it's trusted) — this is the backtest-time analogue of the Research Agent's multiple-comparisons guardrail in §5.3.
- A strategy failing walk-forward validation does not proceed to `PAPER`, full stop — same "nothing skips stages" discipline as §2.4.

---

## 8. Continuous Competition

### 8.1 Monthly (or rolling) recompute, per strategy

Extends the existing Strategy Decay Tracking table (`nwt_strategy_decay`) — already tracks rolling expectancy decay, win/loss ratio compression, false-positive rate, recovery time. Add, computed on the same cadence:
- Regime dependency score (how much of the strategy's edge is explained by one specific regime — a strategy that only works in one regime is not necessarily bad, but the allocator needs to know so it can size it as regime-conditional, not permanent).
- Consistency (variance of rolling-window Sharpe — already a named "stability metric equally important" in `CLAUDE.md`'s Success Metrics section, formalized here as a monthly-computed number).
- Correlation drift (has this strategy's P&L correlation to the rest of the active book changed since last month — catches "silent convergence," the Risk 2 failure mode, as it happens rather than after a drawdown reveals it).

### 8.2 Capital moves automatically within governance-approved bounds

The Portfolio Optimizer (§3.3) re-runs on this same cadence and reallocates within its existing hard caps (per-strategy, per-cluster, lifecycle-stage ceiling). No manual approval needed for a within-bounds reallocation — that's the entire point of building the allocator. Manual governance approval is required only for: a strategy's lifecycle-stage promotion (§2.2 gates), a mutation promotion (existing Learning Gate), and any change to the caps/constraints themselves. This mirrors the existing split between Risk Agent (automatic, no discretion, 13 veto rules) and Mutation Engine (requires the Learning Gate, is still automatic once the gate is met) — automation is fine for anything with a pre-approved, evidence-based gate; governance is required only to change the gate itself.

---

## 9. Portfolio Optimisation (Portfolio Intelligence Layer)

This is largely §3's optimizer restated from the portfolio-construction angle rather than the capital-allocation angle — same component, different lens, so it's specified once (§3.3) and referenced here rather than duplicated. Additional inputs specific to the "behave like an institutional multi-strategy fund" framing:

- **Volatility targeting at the portfolio level**, not just per-strategy — the book's total realized vol should be actively managed toward a target (e.g. scale all positions down uniformly, not per-strategy, when realized portfolio vol exceeds target — distinct from the existing per-strategy Risk Agent sizing-reduction rules, which act on individual strategies/tracks).
- **Options Greeks aggregation** (§4.1's new Greeks table feeds this directly) — net delta, net vega, net gamma, net theta at the whole-portfolio level, checked against explicit caps, extending Portfolio Brain's current "lightweight net delta + net vega estimation" (flagged as incomplete in `CLAUDE.md` Risk 1) into the full Greek set.
- **Capital efficiency** — margin/buying-power consumed per unit of expected edge; relevant once defined-risk spreads and directional single-leg positions compete for the same options buying power pool, so the optimizer should not just maximize Sharpe per dollar of notional but per dollar of *buying power consumed*.

---

## 10. Research Dashboard

Extends the existing `dashboard/` FastAPI service (`nwt-dashboard`, always-on per the PM2 table) with new views. Each is read-only, sourced from the tables introduced above — the dashboard has never had order authority and does not gain any here.

| Dashboard | Primary source |
|---|---|
| Strategy Health | `nwt_strategy_registry` + `nwt_strategy_decay` + rolling Sharpe/Sortino/Calmar |
| Portfolio Health | Portfolio Optimizer output + net Greeks + correlation matrix heatmap |
| Learning Progress | Trade-count progression per strategy toward next lifecycle gate (§2.2) |
| Capital Allocation | Current + historical weights per strategy, vs. lifecycle-stage ceiling |
| Risk | Existing Risk Agent trigger states + new tail-risk (CVaR) view |
| Experiments | `nwt_research_hypotheses` (§5) queue — pending / approved / rejected, with the human decision logged |
| Edge Decay | `nwt_strategy_decay` trend charts, decay-flag history |
| Regime Performance | Per-strategy, per-regime performance matrix (extends existing regime-conditioning from Layer D) |
| Mutation History | `nwt_mutation_log` (existing, already append-only) — timeline view per strategy |

Every strategy row links through to its full audit trail: registry history → genome version history → mutation log entries → every trade outcome → every hypothesis that referenced it. This is achievable specifically *because* every table in this design is append-only or versioned — there is no "current state overwrote history" gap to reconstruct around.

---

## 11. Database Schema Additions (summary)

New tables (all additive — no changes to existing table shapes, only new FKs from existing tables where noted):

```sql
-- Strategy identity + lifecycle (genome table already exists and is extended, not replaced)
CREATE TABLE nwt_strategy_registry (
  strategy_id TEXT PRIMARY KEY,
  hypothesis TEXT NOT NULL,
  market_assumptions JSONB,
  risk_limits JSONB NOT NULL,
  instruments_traded TEXT[] NOT NULL,
  track TEXT NOT NULL,
  asset_class TEXT NOT NULL,           -- 'equity' | 'option'
  lifecycle_stage TEXT NOT NULL DEFAULT 'research',
  parent_strategy_id TEXT REFERENCES nwt_strategy_registry(strategy_id),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  retired_at TIMESTAMPTZ,
  retirement_reason TEXT
);

-- Append-only lifecycle transition history
CREATE TABLE nwt_strategy_lifecycle_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id TEXT NOT NULL REFERENCES nwt_strategy_registry(strategy_id),
  from_stage TEXT, to_stage TEXT NOT NULL,
  gate_evidence JSONB NOT NULL,        -- trade count, regime count, statistical test result, etc.
  decided_by TEXT NOT NULL,            -- 'system' | human identifier
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE RULE no_update_lifecycle_log AS ON UPDATE TO nwt_strategy_lifecycle_log DO INSTEAD NOTHING;
CREATE RULE no_delete_lifecycle_log AS ON DELETE TO nwt_strategy_lifecycle_log DO INSTEAD NOTHING;

-- Market state snapshot at decision time (partitioned by month)
CREATE TABLE nwt_market_state_snapshot (
  id UUID DEFAULT gen_random_uuid(),
  snapshot_time TIMESTAMPTZ NOT NULL,
  vix NUMERIC, vvix NUMERIC, realized_vol_20d NUMERIC,
  iv_rank NUMERIC, iv_percentile NUMERIC, skew_25d NUMERIC,
  term_structure_spread NUMERIC, breadth NUMERIC, put_call_ratio NUMERIC,
  dealer_gamma_proxy NUMERIC, macro_event_proximity_days INTEGER,
  earnings_proximity_days INTEGER,
  PRIMARY KEY (id, snapshot_time)
) PARTITION BY RANGE (snapshot_time);

-- Execution quality per fill
CREATE TABLE nwt_execution_quality (
  id UUID DEFAULT gen_random_uuid(),
  position_id UUID NOT NULL,           -- FK to nwt_portfolio_ledger, not enforced across partition boundary
  quoted_spread NUMERIC, effective_spread NUMERIC,
  decision_to_order_ms INTEGER, order_to_fill_ms INTEGER,
  fill_quality TEXT,                   -- 'full' | 'partial' | 'none'
  fill_pct NUMERIC,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- Greeks, snapshotted at entry / each risk poll / exit
CREATE TABLE nwt_position_greeks (
  id UUID DEFAULT gen_random_uuid(),
  position_id UUID NOT NULL,
  snapshot_time TIMESTAMPTZ NOT NULL,
  delta NUMERIC, gamma NUMERIC, theta NUMERIC, vega NUMERIC, rho NUMERIC,
  PRIMARY KEY (id, snapshot_time)
) PARTITION BY RANGE (snapshot_time);

-- AI Research Agent output — read-only advisory, never auto-applied
CREATE TABLE nwt_research_hypotheses (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  hypothesis TEXT NOT NULL,
  supporting_query TEXT NOT NULL,      -- the actual warehouse query backing the claim
  sample_size INTEGER NOT NULL,
  out_of_sample_check JSONB,
  status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'approved' | 'rejected' | 'promoted_to_mutation'
  reviewed_by TEXT, reviewed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE RULE no_update_hypotheses_core AS ON DELETE TO nwt_research_hypotheses DO INSTEAD NOTHING;

-- Mutation proposal detail (extends existing nwt_mutation_log with pre-registered expectations)
CREATE TABLE nwt_mutation_proposals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id TEXT NOT NULL, shadow_genome_version INTEGER NOT NULL,
  rationale TEXT NOT NULL,
  expected_improvement_metric TEXT NOT NULL,
  expected_improvement_magnitude NUMERIC NOT NULL,
  estimated_risk TEXT NOT NULL,
  success_metric_definition TEXT NOT NULL,
  rollback_condition TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'decay_trigger',  -- 'decay_trigger' | 'research_hypothesis'
  source_hypothesis_id UUID REFERENCES nwt_research_hypotheses(id),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Portfolio Optimizer run log (audit trail, parallels nwt_allocator_history)
CREATE TABLE nwt_portfolio_optimizer_history (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_time TIMESTAMPTZ DEFAULT NOW(),
  inputs JSONB NOT NULL,               -- per-strategy Sharpe/Sortino/Calmar/corr/liquidity/tail-risk snapshot
  correlation_matrix JSONB NOT NULL,
  outputs JSONB NOT NULL,              -- resulting weights per strategy
  binding_constraints TEXT[]           -- which caps were active this run, for explainability
);

-- Historical data store for backtesting (separate from live warehouse)
CREATE TABLE nwt_historical_bars (
  symbol TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL,
  open NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC, volume BIGINT,
  PRIMARY KEY (symbol, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE nwt_historical_options_chains (
  underlying TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL,
  expiry DATE NOT NULL, strike NUMERIC NOT NULL, option_type TEXT NOT NULL,
  bid NUMERIC, ask NUMERIC, iv NUMERIC, open_interest BIGINT, volume BIGINT,
  PRIMARY KEY (underlying, ts, expiry, strike, option_type)
) PARTITION BY RANGE (ts);

-- Backtest run results, per candidate strategy per regime window
CREATE TABLE nwt_backtest_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id TEXT NOT NULL, genome_version INTEGER NOT NULL,
  regime_window_label TEXT NOT NULL,   -- e.g. '2022_inflation_shock'
  start_date DATE NOT NULL, end_date DATE NOT NULL,
  sharpe NUMERIC, sortino NUMERIC, calmar NUMERIC, max_drawdown NUMERIC,
  deflated_sharpe NUMERIC,
  walk_forward_pass BOOLEAN NOT NULL,
  commissions_modeled BOOLEAN NOT NULL DEFAULT TRUE,
  slippage_model TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

Existing tables extended with new nullable FK columns only (never a breaking change):
- `nwt_trade_outcomes` gains `market_state_snapshot_id`, `hypothesis_id` (nullable — most trades won't originate from a hypothesis).
- `nwt_strategy_genome` gains `strategy_registry_id` FK (backfilled) and `sizing_model_version`.

---

## 12. New Services Required

| Service | Path | Cadence | Authority |
|---|---|---|---|
| Strategy Registry API | `factory/registry_service.py` | on-demand (dashboard + agents read/write registry rows) | Registry CRUD only — never trading |
| Portfolio Optimizer | `master/portfolio_optimizer.py` | Same cadence as `strategist.py` (21:30 UTC) + monthly deep recompute | Allocation output only, feeds `master-directives.json`'s successor schema |
| Backtest Engine | `research/backtest_engine.py` | On-demand, triggered by `BACKTEST` stage entry | Read-only vs. historical store; writes only to `nwt_backtest_runs` |
| Historical Data Loader | `research/historical_data_loader.py` | One-time bulk load + daily incremental append | Write-only to `nwt_historical_*` tables |
| Isolation Auditor | `factory/strategy_isolation_auditor.py` | Nightly (alongside recon, 23:00 UTC window) | Read-only static analysis; raises tickets, never blocks trading directly (Risk Agent already owns that authority) |
| AI Research Agent | `nwt_agents/research_agent.py` | Weekly (or on-demand from dashboard) | Read-only warehouse access; writes only to `nwt_research_hypotheses` |
| Correlation Engine | `master/correlation_engine.py` | Same cadence as Portfolio Optimizer (it's an input to it) | Read-only; writes `correlation_matrix` into `nwt_portfolio_optimizer_history` |

None of these introduce a new order-authority holder. The Execution Engine remains the only component that calls Alpaca order endpoints, exactly as `CLAUDE.md` currently mandates.

---

## 13. Event Flows

### 13.1 New strategy birth (research → paper)

```
Human or Research Agent hypothesis
  → nwt_research_hypotheses row (status='pending')
  → human review → status='approved'
  → nwt_strategy_registry row created (lifecycle_stage='research')
  → strategy spec file authored + isolation-audited
  → nwt_strategy_lifecycle_log: research → backtest
  → backtest_engine.py runs full regime library
  → nwt_backtest_runs rows written
  → walk-forward + deflation check
  → PASS → nwt_strategy_lifecycle_log: backtest → paper
         → genome row created (active=TRUE within paper scope, capital_weight=0 in optimizer)
  → FAIL → lifecycle_stage stays 'backtest', ticket raised, human notified
```

### 13.2 Ongoing decision cycle (steady state, generalizing today's Track C/D/E flow)

```
Market data + genome (per strategy) → strategy spec evaluates
  → candidate signal (or none — logged as inactivity, per existing "Do Nothing as a First-Class State")
  → nwt_market_state_snapshot row written (decision-time snapshot)
  → Portfolio Optimizer's last-computed weight for this strategy determines sizing ceiling
  → Risk Agent veto pass (13+ rules, now strategy-agnostic — operates on any strategy regardless of track)
  → Execution Engine places order (only path to Alpaca)
  → nwt_execution_quality + nwt_position_greeks rows written at fill
  → nwt_portfolio_ledger updated (existing table, unchanged shape)
  → nwt_trade_outcomes row written at close, FK'd to market_state_snapshot + hypothesis if applicable
```

### 13.3 Monthly competition cycle

```
nwt_strategy_decay recompute (existing, extended per §8.1)
  → Portfolio Optimizer full recompute (correlation matrix + all inputs)
  → within-bounds reallocation applied automatically
  → any strategy crossing an auto-retire trigger → nwt_strategy_lifecycle_log: * → retired
  → any strategy eligible for next capital step → lifecycle gate check → promote or hold
  → dashboard updated, no human action required unless a gate/cap itself needs revisiting
```

### 13.4 Mutation / hypothesis-to-experiment flow

```
Decay flag OR approved research hypothesis
  → nwt_mutation_proposals row (rationale, expected improvement, rollback condition)
  → shadow genome row (existing pattern: version+1, active=FALSE, shadow_mode=TRUE)
  → shadow evaluation alongside baseline (existing shared_context.evaluate_shadow_mutation)
  → Learning Gate check (existing, unchanged criteria)
  → promote (flip active) OR reject-and-retire OR keep waiting
  → nwt_mutation_log row (existing, append-only) + nwt_tickets row if promoted
```

---

## 14. Agent Responsibilities (delta from current `CLAUDE.md`)

| Agent | Current role | Added role |
|---|---|---|
| Portfolio Brain (`master/strategist.py`) | Regime classification, per-bot allocation | Delegates capital-weighting math to Portfolio Optimizer; retains regime classification and kill-switch authority unchanged |
| Risk Agent | 13 veto rules, per-track | Same 13 rules, now evaluated per-strategy across a much larger strategy population — no new rule types, just broader scope |
| Execution Engine | Places all orders | Unchanged — remains the sole order-authority holder |
| Mutation Agent | Propose/promote genome mutations | Additionally accepts research-hypothesis-sourced proposals (§6); adds explicit rollback-condition field |
| Learning Agent | Win-rate/attribution | Additional consumer: feeds the Research Agent's warehouse queries and the Portfolio Optimizer's per-strategy stats |
| Recon Agent | Ledger vs. Alpaca reconciliation | Unchanged |
| **New: Research Agent** | — | Read-only hypothesis generation (§5) |
| **New: Portfolio Optimizer** | — | Capital allocation across the full strategy population (§3, §9) |
| **New: Backtest Engine** | — | Historical validation gate for `BACKTEST → PAPER` (§7) |
| **New: Isolation Auditor** | — | Static enforcement of strategy non-interference (§1.3) |

---

## 15. Suggested Directory Structure

```
/home/northworld/trading/
├── factory/                          # NEW — Strategy Factory core
│   ├── registry_service.py
│   ├── strategy_isolation_auditor.py
│   └── strategy_specs/               # one file per strategy, pure logic only
│       ├── US-ORB-001.py
│       ├── OPT-C-IRONCONDOR-014.py
│       └── ...
├── research/                          # NEW — backtesting + historical data
│   ├── backtest_engine.py
│   ├── historical_data_loader.py
│   └── regime_library.py             # the labeled historical windows from §7.2
├── master/
│   ├── strategist.py                 # existing, delegates weighting to optimizer
│   ├── allocator.py                  # existing, retained as cold-start fallback
│   ├── portfolio_optimizer.py        # NEW
│   ├── correlation_engine.py         # NEW
│   └── market_internals.py           # existing
├── nwt_agents/
│   ├── research_agent.py             # NEW
│   ├── mutation_agent.py             # existing, extended per §6
│   ├── track_c.py / track_d.py / track_e.py   # existing
│   └── ...                           # existing agents unchanged
├── execution/                        # existing, unchanged authority
├── dashboard/
│   └── views/                        # NEW subviews per §10
├── db/
│   ├── schema.sql                    # existing Day-1 baseline, unchanged
│   └── migrate_2026_XX_strategy_factory.sql   # NEW — schema in §11
└── ecosystem.config.cjs / crontab.txt          # extended with new services' schedules
```

---

## 16. Implementation Roadmap & Priority Order

Sequenced so every phase leaves the system in a fully-working, fully-safe state — nothing here requires a "big bang" cutover, consistent with the existing rebuild's own incremental philosophy.

**Phase 1 — Foundation (warehouse before anything else)**
1. Research Data Warehouse schema (§11, §4) — partitioned tables, FK columns added to existing tables. This must come first: nothing else (optimizer, research agent, backtester validation) has data to work with otherwise, and retrofitting historical attribution later is far more expensive than capturing it from day one.
2. Strategy Registry (§1, §2) — wrap the *existing* 36 Track C/D/E strategies + 4 Track A bots into the registry as a pure metadata migration. No behavior change yet. This proves the schema against real strategies before any new one is added.

**Phase 2 — Governance layer**
3. Isolation Auditor — run against the existing codebase first; fix any existing violations before adding more strategies (this is a genuinely useful audit of the *current* system, not just future-proofing).
4. Lifecycle gates (§2.2) wired to the existing Learning Gate logic — no new gate math needed, just formalize the existing mutation Learning Gate as the general strategy-promotion gate.

**Phase 3 — Backtesting infrastructure**
5. Historical data loader + regime library (§7.2) — this is the single largest new build (10-20 years of bars + options chains) and the longest lead time; start it early so it's ready when new strategies need it.
6. Backtest engine + overfitting checks (§7.3, §7.4).

**Phase 4 — Portfolio intelligence**
7. Correlation Engine — the highest-value missing risk control per `CLAUDE.md`'s own Risk 1, and buildable with only warehouse data that Phase 1 already provides.
8. Portfolio Optimizer (§3.3, §9) — subsumes and extends the existing `allocator.py`, keeping it as cold-start fallback.

**Phase 5 — Learning acceleration**
9. AI Research Agent (§5) — deliberately sequenced *after* the warehouse and correlation engine exist, since it needs rich data to mine and existing attribution infrastructure (Layer B) to anchor its output format against.
10. Mutation Engine extensions (§6) — rollback-condition field, research-hypothesis-sourced proposals.

**Phase 6 — Scale-out**
11. Onboard genuinely new strategies (beyond the existing 36+4) through the full Phase 1-5 pipeline, one at a time, watching the gates actually reject weak candidates before trusting the pipeline with volume.
12. Dashboard views (§10) — can be built incrementally alongside any phase above; sequenced last only because it has no gating dependency on anything else and delivers no risk-reduction on its own, only visibility.

Priority principle: **risk controls and data capture before new strategy volume.** Every phase before Phase 6 makes the *existing* system safer or more observable without adding a single new strategy. Phase 6 — the part that actually grows strategy count — comes last, deliberately, because it's the phase that most needs everything before it to already be trustworthy.

---

## 17. Potential Weaknesses, Failure Modes, Missing Components

### 17.1 Weaknesses in this design itself

- **Correlation matrices are backward-looking and unstable in exactly the regimes that matter most.** Correlations spike toward 1 in genuine crises — the Correlation Engine (§3.2, §12) will under-estimate tail correlation precisely when it matters most, because it's fit on calmer historical windows. Mitigation: the Portfolio Optimizer must use stressed/tail correlation estimates (e.g. correlation conditional on both strategies' worst 5% days) as a separate, more conservative input alongside the standard rolling correlation — not just one correlation number.
- **The Learning Gate (100+ trades, 2+ regimes) is a good bar for an individual strategy but does not, by itself, protect against portfolio-level overfitting** — approving 50 strategies each individually clearing the gate does not mean the *portfolio* of 50 is not overfit to the shared backtest period. This needs an explicit portfolio-level walk-forward validation step, not just strategy-by-strategy validation (a gap in §7 as specified — flagging it here rather than silently fixing it, since it changes backtest infrastructure scope).
- **The Isolation Auditor is static analysis and will not catch all forms of coupling** — e.g. two strategies that both key off the same crowded, thinly-traded factor without any shared code (§5's "signal crowding" decay signal is the intended catch for this, but it's a lagging detector, not a preventive one like the auditor).
- **Options backtesting realism is fundamentally harder than equities** — historical options chain data (bid/ask/IV/OI at every strike/expiry, 10-20 years) is expensive to source and often has survivorship and quote-staleness issues that are easy to underestimate. This is flagged, not solved, here — budget real time and real cost for options historical data sourcing before assuming Phase 3 is a quick build.

### 17.2 Failure modes to design against explicitly

- **Optimizer instability**: a QP-based allocator can produce large weight swings from small input changes near a constraint boundary. Add a turnover penalty / smoothing constraint (max weight change per cycle) so the optimizer doesn't whipsaw capital between strategies — this is a standard institutional practice and is currently absent from the spec above; it should be added to §3.3 as an explicit constraint before implementation.
- **Research Agent hallucinated hypotheses at scale**: as strategy count and data volume grow, an LLM given "find patterns" without a bounded, structured query interface will generate large volumes of low-quality hypotheses that overwhelm human review capacity. Mitigation: rate-limit hypothesis generation, and require every hypothesis to pass the sample-size + out-of-sample check (§5.3) *before* it reaches the human review queue, not after.
- **Silent lifecycle-stage drift**: a strategy stuck in `SHADOW` for months with no proposal to advance or retire it is a failure mode of its own — a "zombie strategy" consuming research-agent attention and dashboard space with no path forward. Add a max-time-in-stage alert (not an auto-retire, since some strategies genuinely need more shadow time — but a stall this long should always surface for human attention).

### 17.3 Missing institutional-grade components not otherwise covered above

- **Independent model risk / validation function.** Everything above (backtest engine, mutation engine, research agent) is built and evaluated by the same architecture that runs it. Institutional practice separates "who builds the model" from "who validates the model" (a model-risk-management function). At minimum, a second, independently-configured backtest run (different code path, ideally different data pull) should periodically cross-check the primary backtest engine's results for a sample of strategies — a "who backtests the backtester" check.
- **Capacity estimation.** Nothing in this design estimates how much capital a given strategy or the book as a whole can absorb before its own trading moves the market against itself. At the current ~$97k paper scale this is moot; it becomes real the moment "Full Allocation" capital grows meaningfully relative to the liquidity of the traded instruments. Worth a placeholder line item now so it isn't forgotten later, not built now.
- **Formal incident post-mortem process.** The existing ticket system captures *what* happened (append-only, good). Nothing formalizes a structured post-mortem for a strategy retirement, a Risk Agent kill-switch trigger, or an optimizer-caused concentration near-miss — a lightweight `nwt_incident_log` + a required written post-mortem for any kill-switch event or forced-retirement would close this gap and feed directly back into the Research Agent's hypothesis generation.
- **Governance sign-off trail distinct from system tickets.** `nwt_tickets` records system events; there is no current table distinguishing a *human governance decision* (approving a hypothesis, approving a lifecycle promotion override, changing an optimizer constraint) from an automated ticket. `nwt_strategy_lifecycle_log.decided_by` and `nwt_research_hypotheses.reviewed_by` are a start (§11) but a dedicated `nwt_governance_log` may be worth formalizing once governance actions are frequent enough to need their own audit view, separate from the Mutation History dashboard.

---

## 18. What Does Not Change

To be explicit, since the request is to evolve rather than replace: the append-only ticket model, the genome-must-exist-at-startup rule, `no_trade_mode` semantics, the Risk Agent's 13-rule veto authority and its seniority over every other component, the Execution Engine as sole order-authority holder, the Ledger as sole position-truth source (never Alpaca directly), shadow-mode-before-promotion for any strategy change, and the Startup Integrity Gate — all carry forward unchanged. The Strategy Factory is additive scaffolding around a decision-making core that was already built correctly; it does not touch that core's authority model.
