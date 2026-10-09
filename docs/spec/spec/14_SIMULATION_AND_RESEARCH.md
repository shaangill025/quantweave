# 14. Simulation, backtesting and performance evidence

## Three separate records

Real account activity is user-reported or broker-confirmed financial history. Virtual portfolio activity is a declared simulation policy/user decision. Recommendation evaluation records what the app proposed with the information then available, including rejected, expired and ignored candidates. None can silently replace another.

First release includes application-managed user-facing simulation with virtual starting capital, manual virtual decisions and optional automatic shadow tracking of an explicitly approved strategy policy. External broker paper-order APIs are not automatically required. No simulation operation calls a broker mutation endpoint.

## Event-driven simulation contract

The engine advances a versioned time/event stream. Information becomes available according to publication/receipt conventions. A signal created from a finalized bar cannot fill before modeled submission. Execution policies specify order type, modeled latency, valid session, bid/ask availability, price/slippage, volume participation, commissions/fees, minimum increments, partial/no fills and expiry.

For OHLC-only data, an intrabar stop/target ordering is often ambiguous. Mark the result indeterminate or use a predeclared conservative convention, with sensitivity analysis; never choose the favourable ordering after seeing performance. A close-generated signal does not earn the already-known close unless a realistic prior-order protocol justifies it. Lack of consolidated volume prevents unsupported capacity claims.

Broker paper environments may omit economic/operational effects; do not treat their reported balances as a universal oracle. [SRC-03] Our ledger explicitly handles permitted dividends, splits, fees, FX, interest where modeled, option expiration/exercise/assignment and corporate-action exceptions. Unknown contract terms or required data yield unsupported-scenario status, not invented fills.

## Research datasets

Maintain immutable manifests with observation range, point-in-time universe membership, delisted assets where needed, data source/feed/version, corporate-action mode, publication/availability timestamps, permitted uses and hashes. Separate raw tradable prices, adjusted research prices and cash distributions to avoid double counting. Today's survivor list is not a historical universe.

Chronological development windows and a protected final promotion evaluation are preregistered. Use purging/embargo where labels overlap and dependence-aware uncertainty when justified. Do not assume random train/test splits are valid for time-series decisions. Register all trials, including failed variants, to expose multiple-selection effects. Additional final-test queries count as evaluation access, not free fresh evidence.

## Metrics and minimum evidence

Report net returns, turnover, drawdown, exposure, cost sensitivity, regime/subperiod behaviour, sample size/effective dependence and uncertainty. Compare to preselected goal/risk-appropriate baselines. Performance claims are constrained by historical coverage, missing data, approximation and prospective evidence. Selection diagnostics such as backtest-overfitting analysis are tools, not proof of profitability. [SRC-22]

Do not hardcode one universal Sharpe ratio, number of trades or days as scientific proof. Human-approved family policies must set quantitative promotion criteria before candidate evaluation, with justification. Unfilled policies mean unqualified research, not permissive defaults. Full release must still deliver the required qualified capability profiles; an indefinitely unqualified implementation does not meet the useful free/intraday/options acceptance floor.

## AI-specific evaluation

A model may contain future event knowledge during historical simulations even when retrieval is date-filtered. Label historical AI investment tests as exploratory unless leakage constraints are defensibly established. Prospective capture uses frozen model/runtime/prompt/mandate versions and timestamps decisions before outcomes. Test proposer-only versus evaluated decisions on the same opportunity set; do not compare cherry-picked accepted winners to all rejected alternatives.

Assess factual correctness and decision process independently from outcome. A lucky trade can rest on a false premise; an appropriately uncertain decision can lose money. Track false approvals, unnecessary rejections, explanation quality, latency and cost along with investment outcomes. User disagreement is not ground truth that the investment thesis was wrong.

## Baselines and cash flows

Benchmark identity, version, currency, reinvestment, fee and execution policy are selected before the evaluation period. Simulate the same external cash flows where appropriate. Distinguish a theoretical index and its investable proxy. Never fabricate holdings history from a current snapshot or use a future rebalancing choice in a historical baseline.

Actual, shadow-policy and recommendation-quality dashboards remain separate. Any aggregate public performance claim needs a qualified marketing/compliance review, including inclusion rules and risk disclosure, before publication. User-level records remain private unless separately authorized.
