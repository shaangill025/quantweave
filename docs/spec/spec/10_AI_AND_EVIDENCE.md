# 10. AI workflow, evidence and independent verification

## Provider-neutral but permission-specific

Implement OpenAI, Anthropic and xAI API adapters, plus separately qualified subscription/native-runtime routes for OpenAI and Anthropic. Adapter capability records cover structured outputs, tool restrictions, model/runtime version, context size, timing, supported use, retention/training options, billing and deployment scope. Authentication success does not establish permitted financial-advice use. [SRC-12, SRC-13, SRC-14]

Native runtimes and API keys are different trust models. Never collect or intermediate prohibited consumer-session credentials, emulate an unofficial subscription endpoint or assume a subscription grants arbitrary API usage. Unsupported routes remain explicit qualification blockers for the promised route, not silent paid fallbacks. A no-model mode remains useful.

## Evidence objects and claims

An evidence record includes canonical source identity, exact permitted locator/passage/data record, publication/event/receipt timestamps, content digest, issuer/security mapping, source type, rights/retention profile and correction/retraction state. Store only permitted content. URL existence alone does not support a claim.

Claims are atomic material propositions with type `reported_fact`, `calculated`, `assumption` or `forecast`. Fact claims cite supporting passages or data; calculations cite formula version and typed input records; assumptions are explicit; forecasts state method, horizon and uncertainty. A source's own claim is distinguished from corroborated fact. Syndicated repetitions of one press release share provenance and do not count as independent corroboration.

Claim verification checks entity, date, unit/currency, scope, numerical magnitude, quotation support and contrary evidence. Missing context, stale information or source correction triggers re-evaluation. Evidence-confidence and probability-of-profit are distinct concepts. The app can explain why a claim is well-supported while making no claim that the investment will win.

## Live workflow

1. Build immutable permitted evidence/account/policy snapshot and determine remaining deadline/budget.
2. Rules engine and/or AI researcher produce a structured candidate. Research outputs can stand independently of rule triggers.
3. Validate schema, source support, calculations and obvious hard eligibility before expensive review.
4. In AI-enabled mode, a separately run evaluator first assesses evidence/constraints, then inspects the proposer and alternatives. Both rules-origin and AI-origin actionable candidates use this path.
5. Evaluator returns accept, revise, research-only, reject or insufficient evidence with structured justification. Initial maximum is one revised submission, and newly introduced material claims repeat verification.
6. Final deterministic validator recomputes current applicability and publishes atomically only when all conditions still hold.

Rule-only factual alerts and qualified actions use deterministic verification and templated evidence-linked explanations without pretending a second model reviewed them. An AI review outage leaves affected candidates pending, research-only or expired; no automatic silent mode switch.

## Tool permissions

Research tools: permitted source search/fetch, canonical security lookup, read-only account snapshot by authorized scope, deterministic calculator/valuation/scenario service, allowed strategy/evidence retrieval. No unrestricted SQL, filesystem writes, shell, broker mutations, secret access or release-control tools. URL fetches pass SSRF/redirect/content-size/type checks through a controlled retrieval service. A prompt cannot elevate these permissions.

Evidence content is untrusted data even when retrieved from an official domain. Instructions embedded in a filing, news article, CSV cell or README cannot become policy. Tool responses preserve trust/taint metadata rather than concatenating external text into system instructions.

## Evaluator and improvement assessor separation

The decision evaluator judges one proposal. The improvement assessor compares system versions. The release controller enforces human authority. The candidate evaluator cannot certify itself or replace final risk checks. Same-model separate contexts are allowed with honest labeling; cross-provider diversity is optional and measured. Agreement is not source verification.

Do not require private model chain-of-thought. Persist concise audit rationale, structured alternatives, claim support, tool calls/results where permitted, input/output hashes and reviewer disposition. A full private internal reasoning transcript is neither available nor necessary for source/numerical verification.

## Budgets, latency and retries

Every chargeable call reserves maximum authorized cost, output/tool limits and wall-time before dispatch. Include retrieval/tool charges and retries where the provider bills them. Token ceilings are not exact billing receipts; reconcile actual reported usage and keep conservative unresolved reservations for uncertain outcomes. Explicit user permission is required for provider/funding fallback. Separate personal USD100 ceiling and USD20 improvement maximum from subscription/hosting costs.

Cache/reuse only with lawful content rights, matching tenant/data scope and versioned inputs. New price/account/material evidence changes invalidate dependent results. Retry cannot move the original trigger forward or expand the review limit. Deadline misses are measured, including candidates that expire before final review. A higher-cost model is not selected merely to conceal poor efficiency.

## Model qualification

Use synthetic correctness/negative tests, permitted research cases and prospective capture. Compare rules-only, proposer-only and proposer-plus-evaluator under the same decision opportunities and cost assumptions. Measure unsupported claims, factual/numerical errors, hard violations, false approvals, unnecessary rejections, calibration where meaningful, latency/cost and investment outcomes. Freeze test access and separate model-generated judgments from independent evidence.

A provider-side model update without a reproducible snapshot is recorded as a version uncertainty and triggers scoped requalification. Historical LLM research can contain future knowledge; no historical profit claim should assume a date-limited prompt removes training-data leakage.
