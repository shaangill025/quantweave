# 5. Numerical calculation contract

All formulas here define proposed application conventions. They are not a validated strategy or personalized investment advice. Implement them with independent tests and explicit applicability checks.

## Valuation and exposure

For a supported simple position, local market value = signed quantity × qualified mark × explicit contract multiplier. FX rate `fx_to_reporting` means units of reporting currency per one unit of local currency; reporting value = local value × that rate. Equity, cash and derivative exposures are separate measures. An option's premium value is not its delta-equivalent exposure, collateral need or maximum scenario loss.

Mark policy identifies bid/ask/mid/last/reference type, event timestamp, source/feed and why that mark is suitable. Stale/crossed/zero quotes or nonstandard deliverables may make valuation unavailable or indicative. Display valuation coverage and source freshness. Do not price every position at an optimistic executable mid.

Gross exposure sums absolute supported economic exposures; net exposure sums signed exposures under the same definition. Never combine premium values and delta-dollar values into one unlabeled total. Unknown factor/ETF constituent coverage is missing, not zero. Concentration uses a specified denominator: account equity, sleeve capital or declared analyzed portfolio, and is undefined when that denominator is unsuitable.

## Cost and P&L reference convention

For a buy, economic cost = quantity × execution price + acquisition fees. Allocate proportional cost to partially closed units; sale net proceeds = units × sale price − disposition fees. Realized net P&L = net proceeds − allocated acquisition cost. Unrealized P&L = current marked value − remaining economic cost. Preserve original broker lots/tax data separately. The chosen economic convention is versioned and does not assert tax-law treatment.

Synthetic oracle: deposit USD1000; buy 10 at USD50 with USD1 fee; sell 4 at USD60 with USD1 fee; mark remaining 6 at USD60. Cash is 738; remaining cost 300.60; realized net P&L 38.60; unrealized P&L 59.40; NAV 1098; total economic gain 98. A 2-for-1 split changes remaining units to 12 and cost/unit to 25.05 without changing total cost or value when price halves. Unknown historical cost makes P&L unavailable, not artificially large.

## Time-weighted return

Use exact subperiod valuations immediately before/after external cash flows where available. Subperiod return = end pre-flow value / start post-flow value − 1. Chain total return = product(1 + subperiod return) − 1. Fees charged to the portfolio and investment income are internal performance effects; external contributions/withdrawals are cash flows. Transfers between included accounts are internal at consolidated scope, external when examining one account alone.

If exact flow-time valuations are absent, label a separately specified approximation such as Modified Dietz: (V_end − V_start − sum(CF_i)) / (V_start + sum(w_i CF_i)), with positive CF into the portfolio and explicit time weights. Do not label an approximation exact TWR. Nonpositive/undefined denominators produce an unavailable status and reason.

Oracle: 1000 grows to 1100, then 1000 is deposited; 2100 grows to 2310. TWR = 1.1 × 1.1 − 1 = 21%, not 131%. Use per-flow valuations and unambiguous event ordering.

## Money-weighted return

Solve the dated investor-perspective cash-flow equation sum(CF_i / (1+r)^tau_i) = 0, where investments are negative, withdrawals/distributions to the investor positive, and terminal portfolio value is included once. The annual day-count convention is ACT/365F for this reference engine. Report root domain, convergence, residual tolerance and whether a unique economically meaningful root was established.

A two-year sequence −100, +230, −132 has two periodic IRRs, 10% and 20%; report ambiguity instead of choosing a favourable root. No positive/negative sign change, zero equity or incomplete flows is not a valid percentage. Do not annualize tiny observation windows by default; show period return and a clearly requested qualified annualized view.

## Benchmark simulation

Select benchmark identity/version and cash-flow treatment before the evaluated period. Distinguish theoretical total-return index from an investable proxy and include proxy fees/rounding/cash treatment. At every permitted external flow time, simulate the same amount in reporting currency using the declared execution convention; unavailable prices or negative units cannot be silently filled. An imported opening snapshot supplies a prospective starting value, not an invented history.

Actual portfolio returns, approved-policy shadow returns and proposal-selection analytics are three scorecards. An ignored proposal is not a real trade. User-reported and broker-confirmed executions remain separable. Contributions, fees, income and FX are shown alongside investment returns.

## FX attribution

For an unleveraged unchanged asset held across a period, 1 + r_reporting = (1 + r_local)(1 + r_fx). Report local effect, currency effect and their interaction, or use a separately documented allocation convention. Example: local +10%, reporting-currency value per local unit +5% gives +15.5%, not +15%. Multi-flow/account attribution requires pathwise values; do not apply this shortcut blindly to an actively traded portfolio. Bank of Canada data is a reference input, not an executable FX price. [SRC-11]

## Position sizing

A long stock candidate's quantity is the floor to permitted lot increment of the minimum among available-cash capacity, concentration capacity, approved planned-loss capacity and other hard constraints. Planned per-unit loss under an exit scenario includes entry-to-exit difference and costs; nonpositive or unknown loss inputs require a different declared sizing rule or abstention, not division by zero. Stress loss is separately evaluated. Stops do not guarantee fills or maximum losses. [SRC-19]

For an option package, size based on explicit deliverables/collateral, declared expiration-payoff bounds where valid, stress/pathwise exposure and available funds. Do not use a capped expiry loss to ignore an interim assignment funding obligation.

## Drawdown and loss pauses

Use a unitized or cash-flow-neutral equity series for investment drawdown, with high-water mark H_t and D_t = 1 − V_t/H_t when the series is valid. Account cash withdrawals are not investment losses; deposits do not erase a triggered pause. Store trigger period, realized/unrealized treatment, FX/fees policy, scope and breach receipt. Risk-reducing proposals still pass full checks; selling coverage against a short call may increase risk.

All numeric functions return value, units, input coverage, formula/version, as-of times and warnings. Missing inputs return a typed unavailable result, not NaN rendered as zero.
