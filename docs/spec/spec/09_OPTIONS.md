# 9. Options contracts, scenarios and action controls

## Mode and catalogue

Options are explicitly opt-in. Stocks-only mode suppresses new option discoveries/proposals and unnecessary UI, but imported option positions remain in account valuation, collateral, expiry and portfolio-risk warnings. Initial supported actions: long calls, long puts, covered calls, cash-secured puts and defined-risk vertical spreads, including bull-call debit, bear-put debit, bear-call credit and bull-put credit structures. No new naked short positions or 0DTE strategy catalogue is required.

Import capability is broader than action eligibility. Unsupported/adjusted/nonstandard contracts remain visible with precise analytical gaps. Do not discard them to create a clean-looking portfolio.

## Required contract identity

Canonical contract ID plus underlying ID, provider symbol/ID, call/put, strike and currency, expiration date, actual last-trading/exercise/settlement calendars where relevant, explicit multiplier, deliverable list and quantities, exercise style, settlement type, adjustment version and source. Standard 100-share examples are fixtures, not a default for missing real metadata. Unknown exercise/deliverable terms block dependent payoffs and sizing.

## Payoff convention

For a standard physically deliverable contract with known multiplier M, intrinsic at expiry is `M max(S-K,0)` for a long call and `M max(K-S,0)` for a long put. Package P&L at expiry is signed intrinsic across legs plus stock value change minus net premium debit and fees, using explicitly stated stock acquisition basis. Premium cash-flow sign is positive cash received/negative paid in the journal; scenario calculations must state their convention to avoid double inversion.

For a debit vertical with equal quantities/expiry/known standard deliverables and width W, idealized expiry max loss is net debit plus fees and max gain is W×M×contracts minus debit minus fees. For a credit vertical, idealized expiry max gain is credit minus fees and max loss is W×M×contracts minus credit plus fees. These formulas do not prove that interim assignment/settlement is operationally harmless. [SRC-20, SRC-21]

A covered call's equity downside remains substantial; collected premium is not standalone profit and shares cannot simultaneously cover multiple calls. Cash-secured puts reserve strike×deliverable cash per contract under a declared conservative collateral convention; do not spend future/unsettled premium as though it were settled funding. Long option loss under simple expiry economics can equal premium plus costs, but modelled exercise may introduce funding/deliverable events that still need handling.

## Pre-expiry analysis

Expiry payoff and current mark are different outputs. Greeks and implied volatility must identify pricing model, dividend/rate assumptions, time convention, input quote, numerical method and applicability. Closed-form European estimates cannot silently become authoritative prices for American exercise with discrete dividends. A numerically qualified tree or other applicable model is needed when exercise/assignment assumptions matter. Model-derived probability is not automatically a calibrated real-world profit probability.

Scenario grids vary underlying, implied volatility, time and relevant discrete events, showing premium value, delta-equivalent exposure, cash/collateral and stress losses separately. Quotes must be fresh/entitled and liquidity/spread/open-interest inputs must disclose timestamps. Missing chain data permits educational scenarios with user-entered assumptions, not executable-price claims. The free indicative feed is not interchangeable with a qualified live options feed. [SRC-01]

## Lifecycle and collateral state

States include open, partially closed, expiration pending, exercise reported, assignment reported, settlement pending, reconciled and exception. Events are based on observed/user-reported records and later broker reconciliation, not inferred certainty about random assignment. Expiry checks account for contract/session calendars and provider cutoffs.

A short call may be assigned while its paired long option remains open. Re-evaluate stock delivery, financing and residual exposure; do not automatically assume the other leg exercised simultaneously. A proposed sale of covering stock must consider the short option before calling the action a risk reduction. Model temporary obligations and missing broker permission explicitly. Alerting cannot cancel or exercise a broker position; the app has no such authority.

## Required fixture matrix

Long call/put at in/at/out-of-money expiry; covered call gains and equity downside; cash-secured put loss and cash encumbrance; four vertical directions; unequal multipliers; adjusted deliverables; stale/crossed/zero quotes; spread narrowed/closed one leg at a time; dividends and early assignment; conflicting coverage across two proposals; expiry on half-day/calendar mismatch; user stocks-only with existing short option. Scenarios distinguish standard synthetic examples from real contract tests.

First-release gate requires a functioning educational/import module and at least one fully qualified permitted live-data configuration for the agreed actionable catalogue, subject to account permissions. It must not claim this live-data path is free or qualify the whole catalogue from a payoff diagram alone.
