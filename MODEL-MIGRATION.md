# Model migration candidate â€” October 15, 2026

**Prepared offline; not deployed or activated.** Baseline is main `46dd22de17a20763ec460fc99aeff5bd0e9c817e`. October 15 natural paper adjudication completed before this branch was created. The running system still uses that baseline.

## Model choice and compatibility

Candidate: **gpt-6-luna**, explicit `reasoning.effort=none`, standard service tier, existing 2,048 output-token cap. The official model page lists this alias, no dated snapshot. Record both requested and provider-resolved model plus the local policy epoch; the alias is not an immutable provider snapshot.

| Component | Current model | Candidate rationale and remaining validation |
|---|---|---|
| Prescreener | gpt-4.1-mini-2025-04-14 | Luna suits bounded high-volume scoring; verify ranking, symbols, shape and threshold stability on representative inputs. |
| Investor classifier | gpt-4.1-mini-2025-04-14 | Luna suits source-only extraction; exact-quote/negation gates remain. Must evaluate the observed source-quote failures; changing model is not proof they disappear. |
| Global-event classifier | gpt-4.1-mini-2025-04-14 | Luna suits bounded classification; retain allowed instruments and verbatim evidence checks. |
| Conviction engine | gpt-4.1-2025-04-14 | Luna is a provisional low-cost candidate, not quality-approved. Require human-reviewed strategy proposals, no-action cases and deterministic risk-rule agreement before activation. |

GPT-6.1 Sol was considered for conviction: standard input/output $2/$10 per million, and minimum reasoning effort low. It has higher output pricing than existing GPT-4.1 ($8) and changes the effective reasoning budget. No evidence yet justifies that extra cost. It is not enabled as an automatic fallback.

OpenAI's model metadata endpoint returned HTTP 200 and id gpt-6-luna using the existing account. This read-only lookup does not prove inference request acceptance, latency, quota or judgment quality. No Responses inference requests were made.

NWT already uses the Responses REST API via requests. Preserve store=false, fixed OpenAI endpoint, no redirects, no tools, no provider fallback, and existing JSON root-shape/domain validators. Requests use prompt-specified JSON; this change does not claim strict JSON Schema enforcement or silently introduce an incompatible array wrapper. Non-message output items are ignored; refusals, incomplete responses and malformed JSON remain rejected. No temperature/top_p/logprob parameters are added.

Explicit caching with no breakpoints avoids cache writes for these one-shot prompts. Accounting nevertheless handles separately reported reads/writes, the >272,000-token long-context multiplier and all output usage (including reasoning if reported). Standard tier is pinned to avoid project-level Fast defaults. Historical GPT-4.1 rates remain available; existing model environment overrides retain their legacy attribution rather than being silently rewritten.

## Evidence boundaries

- Shared policy epoch `nwt-openai-20261015-luna-v1` on new usage and completion records; prompt SHA256, requested/resolved model, response ID and usage retained without storing raw prompts or rejected model output.
- Prescreened records and conviction tickets carry model policy; conviction also retains the prescreener policy or explicit unknown for old input.
- Investor policy v4 appends a boundary while accepting v1/v2/v3 history. Its frozen code hash includes the shared API client. Old classifications/forecasts and daily attempt counters remain unchanged. New investor usage is retained before validation, including invalid results; this SQLite evidence is not yet a replacement for complete unified operating-cost accounting.
- Global-event v2 appends an immutable model boundary with client hash. Forecasts and watches retain the originating classification version/model, including old or unknown metadata.
- No historical reclassification, retrospective predictions, policy reset, trial reset or changes to broker/strategy limits.

## Costs and spending

Standard USD per million tokens:

| Model | Input | Cached read | Cache write | Output |
|---|---:|---:|---:|---:|
| GPT-4.1 mini (existing) | 0.40 | 0.10 | n/a | 1.60 |
| GPT-4.1 (existing) | 2.00 | 0.50 | n/a | 8.00 |
| GPT-6 Luna (candidate) | 0.10 | 0.01 | 0.125 | 0.50 |

For the same 1,000 uncached input + 100 output tokens: mini $0.00056, GPT-4.1 $0.00280, Luna $0.00015 (73.2% / 94.6% lower). Actual usage and quality can differ, so these are unit-cost comparisons, not promised daily savings. Long-context Luna doubles input/cache rates and multiplies output by 1.5. No regional premium applies to the fixed global endpoint.

Existing research limits remain 20 attempts per UTC day; global events retain four per run and investor research one per run. Failed attempts and cooldown behavior remain unchanged. The source has no shared monetary pre-call budget gate for these four call sites; cost_agent reports usage, it is not a dollar-budget enforcer. No daily monetary setting, provider budget, schedule or spending limit was changed or increased. Do not interpret the unchanged counters as a proven hard dollar cap.

A proposed **separate, explicitly approved** smoke evaluation could use at most 20 total Luna requests (five per component), no retries, no tools, max 2,048 output tokens and <=16,000 UTF-8 bytes of input per request. Budget conservatively at 20,000 input tokens/request including framing: **$0.06048 total** at standard no-cache rates, rounded to a **$0.10 hard approval ceiling**. A harness must stop before any call that would exceed this ceiling using a conservative reservation, retain usage and never submit tickets/orders. This is only a bounded compatibility/quality smoke check, not proof of profitable strategy performance. More extensive quality evaluation would require a separately bounded scope. None was run.

## Validation and activation blockers

See local evidence files model-migration-focused-tests.txt, model-migration-full-tests.txt and model-migration-supported-tests.txt. The focused suite passes 90 tests. Compilation and git diff whitespace checks pass.

The initial broad Windows run: 291 passed, 56 skipped, 26 setup errors and four failures. Twenty-six setup errors and two failures require NWT_TEST_DB_DSN; one failure requires real Unix fcntl locking; one used an inaccessible Windows sandbox temp directory. The supported rerun passes **269 tests**, with 56 skipped and one deselected; it uses a writable temp directory and excludes the identified DB-test files (including their supported cases) and the real-locking case. This is not a full CI pass. The local fcntl shim raises if real locking is attempted; it does not pretend to validate Linux locking.

Before activation:

1. Run the complete suite on isolated Linux/Python 3.12/PostgreSQL 16 with broker network blocked. This computer lacks WSL/PostgreSQL; no production database or paid CI run was substituted.
2. Obtain approval for bounded inference evaluation or supply existing representative Luna outputs. Offline mocked fixtures cannot establish provider request acceptance or output quality. Review exact-quote/negation, field validity, unsupported symbols, no-action behavior, conviction risk constraints and response latency.
3. Review candidate outputs and actual costs, then activate only a reviewed revision with a recorded effective boundary and preserved paper-only controls. No deployment is included in the completed work.

## Official sources verified October 15

- [GPT-4 retirement notice](https://developers.openai.com/api/docs/deprecations#2026-10-14-gpt-4-series-model-deprecations): January 14, 2027 shutdown; Luna recommended.
- [Luna model](https://developers.openai.com/api/docs/models/gpt-6-luna): alias, supported API/features, effort and rates.
- [GPT-6 migration guide](https://developers.openai.com/api/docs/guides/latest-model): request compatibility and reasoning choices.
- [Pricing](https://developers.openai.com/api/docs/pricing) and [prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching): cache-write accounting and long-context pricing.
- [Reasoning](https://developers.openai.com/api/docs/guides/reasoning): output-token cap includes reasoning.
- [Fast mode](https://developers.openai.com/api/docs/guides/fast-mode): project defaults and standard tier.
- [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol): alternative's cost and minimum effort.

Existing model-rate references: [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini) and [GPT-4.1](https://developers.openai.com/api/docs/models/gpt-4.1).

## Evaluation authorization details and quality gate

The proposed smoke evaluation is billed by **OpenAI in USD**, against the **existing NWT OpenAI API account/project usage** (its existing billing arrangement; no new card, subscription or funding). It permits at most **20 total requests, including failed attempts**, with **zero automatic retries**. Estimated inference charges are at most **USD 0.06048**, subject to the documented conservative input/output bounds and standard tier. The requested authorization ceiling is **USD 0.10 total including any applicable tax**. The account's actual tax treatment has not been verified; before execution the harness/operator must verify the applicable tax multiplier and reserve the tax-inclusive worst case within USD 0.10, or stop. No inference is authorized by this document and none has been run.

Inputs would be only existing, authorized NWT **public-market research inputs**: public issuer filings/official releases, market-data excerpts already used by NWT, and the existing prompts with bounded public-market features. Exclude credentials, account identifiers, account balances, private portfolio/position details, customer data and operational logs. No new data collection or external research tools. Each fixture must pass this input review before submission to the fixed OpenAI endpoint. Only usage, response metadata and reviewed evaluation output go into isolated evaluation artifacts, never trading tickets.

The 20-request smoke test establishes transport, request/response compatibility, accounting, and obvious contract failures. **It cannot establish trading-quality equivalence.** Before activation, freeze a separate representative labelled set of at least 100 cases (25 per component), sampled across bullish/bearish/neutral regimes, liquid/illiquid or missing-data cases, score-threshold boundaries, allowed/disallowed instruments, positive/negated/boilerplate disclosures, adversarial source instructions, and no-action/invalid-strategy cases. A human reviewer must approve labels before seeing candidate outputs. Include the observed source-quote failure pattern without exposing private logs.

Predeclared gates: zero fabricated source quotes, invented symbols or risk-rule violations; all refusals/incomplete/malformed outputs fail closed; at least 99% valid contract outputs; at least 95% agreement with reviewed action/no-action and eligibility labels; no material regression against retained baseline outputs on score-threshold decisions or approved conviction strategy/risk constraints. Any disagreement in a safety-critical case blocks activation. Check per-component latency against the existing 90-second response timeout and actual costs against the unchanged budget. Review whether the sample is representative enough before drawing quality conclusions. Existing representative candidate outputs can be assessed offline; otherwise a further bounded paid-evaluation approval is needed beyond the 20-call smoke scope. These gates assess application behavior, not profitability.

The normal GitHub Linux/Python 3.12/PostgreSQL 16 CI route is authorized and will be used for this PR; local Windows limitations are not the end of regression validation.
