# 7. Risk policy, proposal states and publication

## Policy as a versioned contract

An approved investment policy contains declared scope, goal/horizon, account/sleeve allocations, instrument permissions, concentration rules, liquidity reserves, numerical planned-loss and stress limits, loss-pause triggers, resumption procedure, strategy adoption and data/AI settings. Limits specify denomination and denominator. A profile label never substitutes for numeric authorization.

Missing required fields block affected sizing. A user's selected strategy may constrain configurable ranges, but exposed combinations do not inherit validation automatically. An investment-affecting change creates a candidate configuration and follows validation/adoption. Self-improvement cannot raise limits, enable leverage or broaden trading sessions on its own.

## Eligibility ordering

Evaluate jurisdiction/feature permission → instrument/catalogue eligibility → strategy/version qualification → user adoption → source rights and feed suitability → data/account freshness and reconciliation → sufficient analytical inputs → individual and combined portfolio risk → required evidence and model review → current validity and atomic publication. All checks produce typed reason codes. Early rejection saves cost but never suppresses required final checks after a candidate changes.

Conservative defaults exclude OTC/penny securities and failed liquidity/spread/status/quality checks from actionable discovery. Exact numerical market-screen thresholds are versioned research choices and must be qualified, not smuggled into personal risk settings. Optional speculative research never changes hard eligibility. Existing outside-eligibility holdings are still represented with known risk and gaps.

## Proposal objects and lifecycle

Proposal type distinguishes factual event, research candidate, rules-based action and AI-reviewed action. An action includes account/sleeve, strategy version, source origin, security/legs, direction, quantities or explicit unsized status, entry conditions, horizon, invalidation, expiry, evidence/claims/calculation references, evaluator receipt, current policy/portfolio revisions, alternatives, costs, risks and all three qualification assessments.

State machine: `candidate → verifying → awaiting_review (AI mode) → ready_for_final_check → active`, with exits to `research_only`, `rejected`, `expired` or `invalidated`. A permitted revision creates another proposal version; one evaluation plus at most one revised submission is the initial bound. Active proposals can be dismissed, superseded, invalidated or expire. Execution tracking is orthogonal: none, planned, user_reported, broker_confirmed, reconciled. Do not erase a proposal simply because it was ignored or lost money in a shadow evaluation.

Any material content change invalidates prior evidence/risk/review attestations that depended on it. An evaluator's alternative proposal is not privileged: new claims, positions or sizing pass the same checks. A reviewer may abstain; the generator cannot keep asking until approval.

## Portfolio-feasible sets

Proposals that compete for capital belong to an alternatives group or a jointly feasible basket. For USD5000 available cash, two USD4000 alternatives may each be eligible as alternatives; they cannot both be published as jointly fundable purchases. Existing external order holds, collateral and internal planned commitments are reconciled and deducted once. Unexecuted sales and unconfirmed transfers are prerequisites, not spendable balances.

At final publication bind account revision, policy version, adopted strategy hash, feed/entitlement revision, available resources and trigger/expiry. Under a short transaction, recheck changes and reserve only application planning capacity. Changed revisions cause conflict/re-evaluation. Avoid long locks while making model calls. Stale proposed quantities cannot be patched without rerunning applicable checks.

## Timing and invalidation

Source corrections, price-condition breaches, account changes, strategy suspension, risk pauses, deleted evidence, permission revocation or expired horizon can invalidate a proposal. The API and UI resolve status at read time. Email content is not an authority source. User-action windows must remain meaningful after verification; meeting a 60-second AI deadline is insufficient when the setup ceased to exist sooner. Historical gap recovery produces retrospective analytics only.

## Scoped loss pauses

A sleeve-level trigger pauses that sleeve's new risk; an account/portfolio trigger affects its defined scope. Continue factual monitoring and independently checked reductions. The pause is not a broker instruction and never automatically liquidates positions. Selling a stock covering a short call is not assumed risk-reducing.

Trigger definitions specify period, realized/unrealized P&L, fees, FX and cash-flow normalization. Unitized drawdown or another approved definition prevents withdrawals from fabricating losses and deposits from erasing breaches. A material restart requires fresh reconciled state plus explicit user acknowledgement under the approved recovery policy. Midnight alone does not reset authority.

## Typed reasons

Examples: `ACCOUNT_STALE`, `ACCOUNT_CONFLICT`, `CASH_UNCONFIRMED`, `POLICY_NOT_ADOPTED`, `STRATEGY_UNQUALIFIED`, `FEED_UNQUALIFIED`, `SOURCE_RIGHTS_UNKNOWN`, `OUTSIDE_SESSION`, `PRICE_STALE`, `MATERIAL_CLAIM_UNSUPPORTED`, `REVIEW_REQUIRED`, `REVIEW_DEADLINE_MISSED`, `RISK_PAUSED`, `CONCENTRATION_LIMIT`, `BUDGET_UNAVAILABLE`, `CAPITAL_CONFLICT`, `OPTION_TERMS_UNKNOWN`, `EVIDENCE_REDACTED`. Each reason identifies affected capability and permitted alternatives without exposing secrets.
