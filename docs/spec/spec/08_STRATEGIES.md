# 8. Six-family strategy specifications and qualification

## Shared execution contract

Every strategy version specifies mandate, universe, required feeds, event/publication cutoffs, feature formulas, candidate ranking, action conditions, exits/invalidation, sizing interface, bounded parameters, costs, output reasons, evaluation protocol and qualification policy. Inputs are immutable snapshots. Rule outputs are reproducible for a given version and input; AI outputs retain execution receipts rather than claiming deterministic replay from a nondeterministic provider.

Each family has at least one implementation at first release. The baseline manifest versions in `config/strategy_catalogue.json` are research starting points with action eligibility false until qualification. Financial risk limits are supplied by user-approved policy, never embedded as universal recommendation percentages.

## STR-ALLOC-001 — cash-first allocation/rebalancing

Purpose: move toward explicitly approved allocation targets, not claim index outperformance. Input target weights, current valued positions/cash, allowed instruments, account/sleeve budgets, reserves, minimum lot increments, transaction costs and rebalance bands. Unknown valuation/target/account permissions block affected sizing.

Compute target value from the declared post-flow capital base and deviation for each asset. First allocate eligible free cash to positive underweights in largest-relative-underweight order, tie-broken by canonical security ID. Round down to valid units and recompute cash/weights after each allocation. No assumed sale proceeds. If remaining deviations require sales, produce a coordinated conditional basket or clearly identify funding prerequisites; apply user tax-sensitive restrictions and turnover constraints. Shared issuer/ETF exposure checks may reduce quantities. No integer rounding loop may exceed cash.

Triggers are scheduled reviews, committed cash-flow changes or deviation bands. Invalidation: changed policy, account revision, stale mark or different target allocation. Acceptance is numerical fidelity to approved policy, costs/rounding, cash/concentration feasibility and appropriate abstention. Useful free-mode implementation is required.

## STR-QUALITY-001 — sector-aware quality/valuation research

Initial corporate-equity recipe uses trailing reported operating profitability, return on invested capital where meaningful, cash-flow conversion and leverage; valuation features use positive-denominator enterprise/earnings or free-cash-flow measures. Normalize each supported metric against the point-in-time eligible peer group using explicit winsorization/rank rules. Version weights and missingness requirements; do not silently replace missing features with zero or negative earnings with a cheap multiple.

Different sector recipes require separate manifests. The initial general corporate recipe does not claim to value banks, insurers, funds or pre-revenue issuers with the same accounting ratios. Unsupported cases can be research leads with limitations, not fictitious comparable scores. ETF research uses appropriately sourced fund attributes instead of corporate accounting features.

Input availability uses actual filing publication, units/currency, amendment version and annual/quarterly consistency. The researcher may explain a ranking but cannot invent analyst consensus or future earnings. Proposals require portfolio suitability and a qualified workflow. Evaluation examines measurement accuracy, ranking stability, missingness, costs, historical/prospective evidence and claim calibration. Strong ranking is not a probability of profit.

## STR-INCOME-001 — distribution sustainability research

Separate trailing paid distributions, announced future distributions and model assumptions. Compare ordinary distributions with normalized operating/free cash flow and debt obligations under a declared recipe; flag special distributions, nonrecurring income and insufficient coverage. A negative/unknown cash-flow denominator makes a payout metric inapplicable, not attractive. For funds, analyze available sponsor-reported distribution and asset information with its own source/coverage limitations.

Rank only within eligible recipes using sustainability, balance-sheet resilience and approved concentration/income objectives. Headline yield does not override a deteriorating thesis or mandate mismatch. Forecast payments are assumption-dependent and never guaranteed. Compare income requirements with required capital without increasing risk until a target is mechanically met. Evaluate dividend/event handling and contribution to total return as well as nominal income.

## STR-TREND-001 — long-only swing/position reference

Research configuration: finalized daily adjusted-price history, 126-session momentum `P_t/P_(t-126)-1`, 200-session simple moving average, and an explicitly defined volatility estimator for sizing. A candidate must have positive momentum and finalized close above its 200-session mean. Rank eligible candidates by momentum subject to portfolio diversification and approved limits. At least 200 valid observations are needed for the moving average; missing required sessions, adjustment discontinuities or unqualified universe membership prevent use.

Signal is known only after its source bar is finalized and available. Proposed entry is a conditional next-regular-session action using fresh suitable prices/account state; never an assumed fill at the already-observed signal close. Reference exit is a finalized close below the trend filter or policy/thesis/risk invalidation, plus an explicit approved horizon/review policy. Any stop-based sizing is a scenario and separately stress-tested. Parameters are initial research choices, not optimized or proven settings.

Qualification requires correct PIT construction, declared costs and execution assumptions, chronological development/validation, trial accounting, meaningful prospective observations and family-approved minimum evidence. The free release floor must include a qualified non-intraday implementation; this reference is the first candidate, not guaranteed success.

## STR-ORB-001 — regular-session opening-range breakout

Research configuration: initial 15 regular-session minutes, versioned exchange calendar, qualified intraday bars/trades and synchronized event times. Range interval is session open inclusive to open+15min exclusive. Build only after the interval is complete and required late-data policy has been satisfied. `range_high`, `range_low` and range quality become frozen/versioned inputs.

After formation, an eligible long candidate requires a subsequent qualified breakout observation above range_high plus declared tick/spread/slippage checks. Any volume criterion must use the exact qualified coverage; IEX activity cannot masquerade as total-market volume. Reject zero/invalid ranges, incomplete data, halts and policy-ineligible names. Entry sizing comes from the final risk engine with a declared adverse-exit scenario; the range low alone is not a guaranteed stop fill.

Reference exit is a pre-close deadline derived from the actual calendar, or earlier invalidation. No new proposal may leave an inadequate action window before that deadline. The initial policy allows at most one new long entry per security/session; re-entry would be a separately evaluated version. Early closes, DST, corrected bars, mid-session feed switches and account conflicts are mandatory negative tests.

AI-mode actions require independent evaluator completion within the earlier of policy deadline and the useful setup window. A complete model review that finishes after the breakout has ceased to qualify is not an actionable success. No sub-minute scalping or extended-hours trading commitment is implied.

## STR-AITHESIS-001 — independent AI research

Research starts from an approved mandate, horizon and source policy, not a compulsory technical trigger. It may originate a thesis from filings, business developments, valuation sensitivity, industry facts or other permitted evidence. Output separates factual premises, assumptions, alternative interpretations, forecast scenarios, portfolio relevance, invalidation and uncertainty.

A separate evaluator inspects evidence independently, then compares the original thesis and alternatives including no action. Newly introduced material claims are verified. Claims such as a target price or probability require documented methods; unsupported precise confidence is prohibited. No material contradiction or missing hard gate can be averaged away by model votes.

Qualify the workflow/version and each proposal rather than fabricating a backtest for a unique business event. Historical LLM simulation can contain model knowledge of future outcomes; restrict claims and rely on prospective capture. A change to model, prompts, tools, mandate or evaluation logic is a new version with scoped requalification.

## Three-axis qualification and research discipline

The API qualification fields use explicit enums: operational `not_tested`, `passed`, `failed`, `suspended`; investment evidence `none`, `limited`, `historical`, `prospective_limited`, `qualified_for_declared_claim`; user eligibility `not_evaluated`, `eligible`, `ineligible`. Synthetic-only evidence is recorded as limited with that limitation stated. Research-only is a proposal state, not a synonym for user eligibility. These are not compressed into a single validated badge.

Before evaluating candidates, a human-approved family policy specifies data sufficiency, sample/dependence treatment, metrics, costs, regression thresholds, prospective protocol and permitted claims. A candidate cannot change these thresholds to pass. Keep all trials and evaluation-access history; use chronological splits and dependence-aware uncertainty estimates, and disclose selection risk. Research on backtest overfitting motivates this discipline but does not supply universal magic thresholds. [SRC-22]

Predictive performance evaluation must include relevant baselines, turnover, cost sensitivity, drawdown/tail scenarios, parameter stability, failure modes and prospective observations. Return estimates must not become guaranteed outcomes. When evidence is insufficient, keep the prior version or research-only state. Limited-evidence action is permitted only where predeclared family policy explicitly allows it, never a candidate-specific waiver.
