# Portfolio Intelligence
## Master implementation specification

Version 1.0.0 • 8 October 2026

Status: adopted planning baseline; implementation, provider rights, investment evidence and release gates not yet verified. Descriptive working title, not an approved brand.

## Document map

- [1. Product contract and release boundary](spec/01_PRODUCT_SCOPE.md)
- [2. User journeys and onboarding](spec/02_USER_WORKFLOWS.md)
- [3. Architecture and module contracts](spec/03_ARCHITECTURE.md)
- [4. Domain model, accounting and reconciliation](spec/04_DOMAIN_AND_LEDGER.md)
- [5. Numerical calculation contract](spec/05_CALCULATIONS.md)
- [6. Market data, reference sources and integration qualification](spec/06_DATA_AND_INTEGRATIONS.md)
- [7. Risk policy, proposal states and publication](spec/07_POLICY_AND_PROPOSALS.md)
- [8. Six-family strategy specifications and qualification](spec/08_STRATEGIES.md)
- [9. Options contracts, scenarios and action controls](spec/09_OPTIONS.md)
- [10. AI workflow, evidence and independent verification](spec/10_AI_AND_EVIDENCE.md)
- [11. Full self-improvement and bounded recursion](spec/11_SELF_IMPROVEMENT.md)
- [12. Threat model, privacy and trust boundaries](spec/12_SECURITY_PRIVACY.md)
- [13. Dashboard, research and operational UX](spec/13_UX.md)
- [14. Simulation, backtesting and performance evidence](spec/14_SIMULATION_AND_RESEARCH.md)
- [15. Deployment, reliability, cost and release operations](spec/15_OPERATIONS_AND_RELEASE.md)
- [16. Governance, commercial permissions and honest claims](spec/16_COMPLIANCE_AND_GOVERNANCE.md)
- [17. Testing strategy and evidence receipts](spec/17_TESTING_AND_ACCEPTANCE.md)

## Package navigation

[Start here](START_HERE.md) · [Work plan](planning/WORK_PLAN.md) · [Requirements](planning/requirements.csv) · [Decision register](planning/decision_register.csv) · [Contracts](contracts/README.md) · [Sources](reference/SOURCES.md)

All waves are part of the full first release. No broker order execution is authorized.


---

# 1. Product contract and release boundary

## Purpose

Build an evidence-first investment decision-support application for self-directed users. The central question is: given the user's declared goal, account state, approved policy and permitted data, what should the user investigate, hold, buy, reduce, sell or leave unchanged, and why? Frequent trading signals are required but are not a quota of trades. A justified abstention is a valid outcome.

The personal pilot and near-term commercial product share an independent application core. Both a complete self-hosted edition and a hosted multi-user edition are required at first release. The application is intended to be open source under Apache-2.0; the descriptive working title is not a selected brand. Hosted revenue pays for managed operation, not privileged investment rankings.

## Mandatory release scope

US-listed stocks and ETFs are the mandatory discovery universe. Accounts and reporting are currency-aware, initially including CAD and USD. Canadian and US residents are both in the commercial launch scope, subject to product-specific legal clearance. Imported instruments outside recommendation eligibility remain represented; absent data is an explicit limitation, never zero exposure.

First release includes policy onboarding; portfolio snapshots and transaction journals; manual and CSV workflows; named read-only integrations; watchlist CRUD; monitored events; all six strategy/research families; independent AI-originated proposals and a separate evaluator; opt-in options; user-facing virtual portfolios; cash-flow-aware benchmarking; governed chat; evidence/provenance; all six improvement areas including actual code and optimizer recursion; runtime human promotion/adoption; migration, deletion and recovery.

The six stock/ETF families are allocation/rebalancing, quality/valuation, income research, long-only swing/position trend-momentum, regular-session long-only opening-range breakout, and independent AI theses. Options add long calls/puts, covered calls, cash-secured puts and defined-risk vertical spreads. This is not permission to postpone the difficult families as placeholders.

## Free baseline and qualified capability profiles

The free-data/rules-only profile must deliver portfolio-specific allocation/rebalancing and at least one qualified non-intraday long-only strategy without paid market data or paid inference. A supported free provider account can be required. Hosting, optional connectors, premium data and existing model subscriptions are separate costs. No claim of free universal live consolidated stock/option coverage is permitted. [SRC-01, SRC-02]

Feature presence, operational qualification, investment evidence and current user eligibility are separate concepts. Catalogue inclusion does not authorize action. Every family must have a functioning versioned implementation and evaluated status; a failed variant stays research-only. For the complete release, demonstrate the free floor and qualified configurations for required intraday and live options use. Turning off every substantive strategy does not satisfy the release requirement.

## Exclusions and non-authority

No broker order submission, amendment, cancellation, discretionary custody or autonomous execution. No required sub-minute scalping, extended-hours actionable trades, native mobile clients, SMS, full tax-return engine, automated tax-loss harvesting, unlimited AI bundle, mandatory GPU/Kubernetes, unrestricted hosted arbitrary-code strategy builder, or automatic Yahoo account synchronization. Stock proposals are cash-funded long-only, nonleveraged and noninverse. This does not suppress the approved options catalogue or existing unsupported exposures.

A model cannot expand these permissions. The user's acceptance of planning recommendations is not an investment-policy signature, a software-promotion authorization, a provider payment approval or permission to share private learning.

## Product success measures

Primary: reconciled financial correctness; supported material claims; meaningful portfolio-specific decisions; timely invalidation; suitable abstention; prospective evaluation integrity; safe degradation; user control. Secondary: workflow completion, relevant alert rates, explanatory usefulness, cost and operational reliability. Do not optimize trades per day, user clicks, recommendation approval or recent P&L as the sole objective.

All acceptance targets are requirements to test, not achieved results. Implementation waves are dependency order inside the same release. External permissions, legal clearance and resource feasibility remain named gates, not preference questions to reopen indefinitely.


---

# 2. User journeys and onboarding

## Journey A — first useful free decision

Create an installation/user → select declared portfolio scope → choose reporting currency and resident jurisdiction → connect a permitted free data account → import holdings or transactions → map sources to real accounts → resolve material conflicts → answer conditional onboarding → review explicit policy → adopt policy and eligible strategy version → run qualified rules → inspect proposal or abstention and evidence. No model account is necessary for this journey.

A snapshot-only user may view current exposures and prospective recommendations but cannot receive invented historical returns. An incomplete profile permits general research and labelled scenarios; required missing account/risk information prevents affected sizing. Outside assets can be summarized without individual disclosure, but cannot be claimed independently verified.

## Questionnaire specification

Use stable question IDs and versioned response schemas. MCQs describe preferences; amounts, dates and percentages use typed numeric/date fields. Every inference records which responses support it. An answer change creates a new proposed policy where semantics are affected, not an immediate live edit.

| ID | Input | Choices / validation | Effect |
|---|---|---|---|
| ONB01 | Analysis scope | Selected accounts; all disclosed investment accounts; hypothetical only | Scope and completeness language |
| ONB02 | Residency | Canada + province; US + state; other/unknown | Permitted commercial feature matrix, not trading exchange |
| ONB03 | Reporting currency | CAD or USD initially | Display and benchmark currency |
| ONB04 | Objectives | Capital preservation, growth, income, active trading, learning; rank priorities | Mandates and conflicts |
| ONB05 | Horizon | <1y, 1–3y, 3–7y, >7y; exact goal date optional | Strategy eligibility |
| ONB06 | Liquidity need | None known, periodic, known dated need, uncertain; amount/date when relevant | Reserved cash and incompatible horizons |
| ONB07 | Loss capacity | Essential spending affected, goal delayed, financially manageable, uncertain | Proposed reviewed risk template, never a sole score |
| ONB08 | Drawdown response | Reduce, review, hold, add, unsure | Behavioural discussion; not permission to increase risk |
| ONB09 | Experience | Beginner; stocks/ETFs; options; active trader | Education and instrument permissions |
| ONB10 | Attention | Monthly, weekly, daily, regular-session availability | Monitoring-compatible strategy selection |
| ONB11 | Instruments | Stocks; ETFs; explicitly enabled options | Hard discovery/recommendation permissions |
| ONB12 | Horizons | Long-term, swing/position, same-day | Separate strategy/latency requirements |
| ONB13 | Capital allocation | Numeric per account/currency/sleeve | No cross-account spendability assumption |
| ONB14 | Numerical limits | Cash reserve, position/issuer/sector, planned loss, drawdown, approved stress limits | Denominator/currency required; missing blocks sizing |
| ONB15 | Research inputs | Primary/reporting; optional social | Source and privacy permission |
| ONB16 | AI mode | Rules-only; AI-enabled with named proposer/evaluator configs | Mandatory AI-mode evaluation |
| ONB17 | Provider privacy | Allowed providers, purpose/data categories, retention options | No unauthorized fallback or sharing |
| ONB18 | Budget | Incremental spend and improvement sublimit | Ceiling, not consumption target |
| ONB19 | Notifications | Channel/priority/quiet hours/detail exposure | No implied read/execute acknowledgement |
| ONB20 | Outside context | Optional aggregate exposure/liabilities; unknown allowed | Qualified scope statements |
| ONB21 | Review/adoption | Show draft, contradictions, exclusions, numerical limits, change effects | Explicit user adoption receipt |

## Contradiction rules

Preservation plus near-term essential withdrawal plus aggressive intraday requests triggers a visible conflict; it is not averaged into a moderate score. Insufficient monitoring availability excludes dependent intraday policies. Options-off plus existing short option does not delete the exposure: suppress new option ideas but retain risk/expiry warnings. Unknown account trading permissions block the relevant option proposal. A profit goal incompatible with declared constraints is a feasibility warning, never an instruction to optimize away the constraints.

Templates may propose numbers after human review of their methodology. This package does not set a universal safe risk percentage. Draft risk limits remain null until the user approves required values. Hypothetical test policies carry SYNTHETIC labels and cannot populate a live user's authorization.

## Journey B — external execution and reconciliation

Open a current proposal → inspect account, sleeve, costs, review status, evidence, alternatives and invalidation → user may mark planned/dismissed or execute outside the app → report quantity/price/time/fees or wait for broker sync → reconcile reported and broker records. A report is not broker confirmation. Proposed sale proceeds do not increase available funds. Stale records visibly constrain subsequent sizing.

## Journey C — discovery and alerts

Watchlist entry contains security, thesis, tags, review date, signal conditions and priority. Assign actual streaming slots; leave overflow in labelled scheduled mode. Distinguish factual event, unqualified candidate and portfolio-qualified action. A notification links to current server state; an expired proposal never becomes active because a user opens an old email.

## Journey D — improvement adoption

Inspect candidate version and evidence → maintainer reviews shared release → operator installs compatible release → affected user adopts a material strategy change. These are distinct authorizations. Routine configuration-preserving maintenance follows documented update policy; incompatible or unsafe old versions pause rather than silently migrate users. The user can inspect failed candidates and rollback outcomes.

## Journey E — portability/deletion

Preview export scope and rights exclusions → create encrypted optional archive without ordinary credential export → verify manifest on destination → map accounts, reauthorize providers and adopt compatible configurations → report missing evidence and unsupported learning state. Deletion identifies retained legal/operational exceptions and backup expiry; downstream indexes and learned state are removed or invalidated. Restoring an old backup reapplies tombstones before serving users.


---

# 3. Architecture and module contracts

## Selected architecture

A modular monolith with separately permissioned workers is the starting architecture. Typed Python owns the independent domain/quant core; TypeScript/React owns the responsive interface; PostgreSQL owns transactional state and durable coordination. Permitted immutable analytical/evidence objects use filesystem storage locally and object storage in hosted operation. Do not add Kafka, a service mesh, a mandatory vector database or microservices without a measured need and an ADR.

Proposed repository modules after scaffold: `packages/domain`, `packages/portfolio`, `packages/quant`, `packages/strategies`, `packages/evidence`, `packages/ai`, `packages/improvement`, `packages/adapters`, `apps/api`, `apps/worker`, `apps/web`, `ops`, `tests`. These directories are implementation targets, not present application code in this bundle. Dependency selection/version pinning happens during T009 against current official docs, license/security review and the actual environment.

The core imports no web/UI framework or provider SDK. It accepts typed immutable snapshots and returns results plus provenance. Provider SDKs stay behind adapter interfaces. Database repositories and object stores are injected. A browser is never needed to run a strategy, reconcile a ledger, evaluate a candidate or execute a test.

## Three authority paths

**Live:** ingestion → normalization → financial/market snapshot → strategy/research → evidence/reviewer checks → deterministic final validation → atomic publication/commitments → notification.

**Experiment:** authorized evidence/corpus snapshot → candidate generator → isolated candidate execution → fixed independent assessment → candidate evidence record. No production write credentials, live broker tools, protected acceptance controls or signing keys.

**Release:** authorized human review → immutable approved version/package → integrity verification → operator install → user adoption when strategy semantics change → monitoring/rollback. The release service cannot infer a human decision from an LLM output.

## Domain interfaces

| Interface | Inputs | Outputs / invariants |
|---|---|---|
| AccountConnector | Secret reference, read-only operation, cursor, entitlement | Observations with source/account identity, time and coverage; never direct journal mutation |
| MarketDataAdapter | Explicit feed spec, instruments, interval/as-of | Typed observations with event/receipt time, gaps and revision flags |
| LedgerService | Reconciled command, expected revision, idempotency key | Committed journal revision or conflict; balanced monetary/unit postings |
| StrategyEngine | Strategy version, input snapshot, approved config | Deterministic candidate set/rejections and calculation trace |
| ResearchEngine | Mandate, evidence permissions, tool budget | Structured thesis/claims, assumptions, limitations and candidate |
| DecisionEvaluator | Separately constructed evidence and candidate context | Accept/revise/reject/research-only with supported reasons; no final authority |
| RiskValidator | Final proposed set, policy/adoption, current financial/data revisions | All checks, blockers and allowed sizes; deterministic final gate |
| ProposalPublisher | Checked revisions and reserved planning capacity | Atomic persisted proposal set + outbox event, or retryable conflict |
| ExperimentAssessor | Candidate, protected test/version manifest | Evidence report; no deployment side effects |
| ReleaseController | Candidate hash, signed human decision, compatibility | Available release/adoption state; rollback lineage |

## Durable coordination

Use at-least-once jobs with an idempotency key scoped to tenant/operation/input revision. Jobs have leases, heartbeats, attempt limits, next-available time and an explicit dead-letter state. Exponential backoff with jitter and provider-specific rate budgets prevent retry storms. An outbox record is committed in the same transaction as the business state; publishing is retried and consumers deduplicate by event ID. Do not promise globally exactly-once delivery.

Priority order: safety invalidations/reconciliation → current monitoring and required verification → interactive research → scheduled research → improvement experiments. Weighted tenant fairness prevents one user monopolizing workers. Experimental load has a separate resource pool even on one machine. Quota counters reserve before dispatch and release/reconcile safely after errors.

## Atomic final decision

Read a portfolio snapshot with account, policy, entitlement and strategy-adoption revisions. Evaluate outside long database locks. Before publication, lock only the involved account/currency/sleeve planning rows in a deterministic order; confirm revisions and valid market time; recompute affordable amounts against current commitments; write proposal-set decisions and outbox atomically. A stale revision returns conflict and triggers bounded reevaluation. Never reserve funds at a broker: this is only application planning state.

## Storage and access

Tenant-owned keys include tenant identity in foreign-key relationships; clients cannot supply a tenant ID to access another scope. Global public reference data and private tenant data have separate access policies. UUID identifiers are unguessability aids, not authorization. Price/time-series retention follows rights and workload policy; essential ledger/evaluation lineage remains referentially intact or marked redacted/unavailable when deletion applies.

## API conventions

OpenAPI 3.1 in `contracts/openapi.yaml` describes a design contract, not running endpoints. JSON money/quantities are decimal strings. Date-times include timezone offsets and normalize to UTC; exchange calendar IDs preserve local session meaning. Reads use cursor pagination, stable sorting and coverage metadata. Mutating application commands require idempotency; revisioned edits use `If-Match`. Long work returns 202 + a job resource. No broker order routes are defined.

Session authentication, CSRF protection for browser writes, membership checks, rate limits, structured errors and audit events are mandatory. Provider auth is separate from application identity. A shared error envelope includes code, retryability, correlation ID and nonsecret field errors. Secrets must not appear in payload examples or logs.


---

# 4. Domain model, accounting and reconciliation

## Canonical boundaries

The entity/field catalogue is in `contracts/data_dictionary.json`. Financial state is derived from a reconciled journal, not an LLM narrative or the latest CSV row. Keep **source observation**, **reconciliation decision**, **posted economic event**, **snapshot projection** and **proposal commitment** distinct.

Tenant → user membership → accounts → cash currencies and holdings; a sleeve allocates purpose/budget, not custody. An account can have multiple sources. A source can contain watchlists, snapshots, transactions or mixed records. Import classification requires explicit resolution before commit. A Yahoo record and a brokerage record of the same shares do not create two holdings. Two genuine accounts holding the same shares remain separate.

## Monetary and unit ledgers

Use Decimal-based amounts at domain boundaries; preserve original precision and currency. Round only according to versioned instrument/currency rules at explicit reporting/transaction boundaries. Never convert unknown cost basis to zero. Quant statistical arrays may be floating point but require declared tolerances and deterministic fixture tests.

Use balanced monetary postings in each currency and a separate balanced commodity/unit journal for security quantities. Do not add ten shares to fifty dollars and call the result balanced. Trade events connect monetary postings, security-unit postings, lots and source observations. Split and conversion events preserve the appropriate economic invariants and create explicit unit adjustments.

Suggested monetary ledger accounts: cash, security cost asset, external capital, realized gain/loss, investment income, expense and transfer clearing. Opening snapshots post labelled opening balances with unknown historical provenance where necessary; they do not synthesize past trades or contributions. Fair-value marks belong in valuation snapshots, not fictional cash movements.

For the reference economic lot convention, allocate acquisition fees into lot cost and subtract disposition fees from proceeds. This is a performance convention, not a jurisdictional tax rule. Keep raw fees independently available and avoid subtracting capitalized fees twice. Alternate tax-lot data supplied by a broker remains separately sourced.

## Supported economic events

Deposits, withdrawals, buys, sells, dividends/distributions, fees, interest, FX conversions, account transfers, splits/reverse splits, symbol changes, mergers/spinoffs with known terms, and supported option expirations/exercises/assignments. Unknown corporate actions are quarantined, surfaced and block affected calculations. Manual adjustments require reason, evidence and actor; they cannot silently repair unexplained differences.

For ordinary corporate actions, distinguish announcement, ex/effective, record and payable dates where supplied. Entitlement and actual cash receipt differ. Return histories need versioned total-return adjustment or dividend cash accounting, not both simultaneously. Restatements create new observations and dependent recalculation/invalidation.

## Idempotency and conflict resolution

Use provider event IDs where stable. Otherwise derive candidate fingerprints from normalized source/account/event kind/effective time/instrument/quantity/amount/currency, retaining full row hashes and batch IDs. Fingerprint equality is a match signal, not permission to merge ambiguous repeated legitimate trades. Store reconciliation links so reimporting either original source is idempotent.

Observation precedence is field-specific and time-aware. A broker-confirmed execution is stronger evidence of execution than a user report, but an older snapshot cannot erase a newer reported event. Link the report to the eventual broker event rather than posting both. Material mismatches in quantity/cash/permissions generate a conflict case and block affected sizing; unaffected accounts can continue.

Import workflow: upload into a size/type-limited quarantine → parse with encoding/locale diagnostics → map columns → classify data and account scope → normalize → produce proposed matches/duplicates/errors → user reviews ambiguous mappings → commit against expected account revision → update projections → emit reconciliation event. A preview is not a mutation. CSV formula-injection protection is applied on spreadsheet-targeted export without corrupting canonical raw values.

## Cash and commitments

Represent settled cash, unsettled credits/debits, reported available funds, source timestamp, external open-order encumbrances, collateral and internal planned commitments. Do not claim real buying power when only a stale or user-reported balance exists. Conservative default is no reuse of unconfirmed sale proceeds or presumed transfer/FX availability. Broker-specific settlement permissions require qualification; do not encode a universal regulation from memory.

A sleeve is not an account transfer. Committing capital to an alternative proposal does not reserve funds at the broker. Alternative groups reserve at most the declared mutually exclusive requirement; jointly intended legs reserve combined resources. Planned status expires or reconciles, and overlapping commitments require explicit linkage to avoid double subtraction.

## Options and outside-coverage holdings

Option identity includes underlying canonical ID, expiry/session, right, strike/currency, multiplier, deliverable components, exercise style, settlement type and adjustment revision. Never infer standard terms for an adjusted contract without evidence. Existing ineligible/unknown holdings are retained and flagged, not hidden by stocks-only preference or eligibility filters.

Value coverage is explicit: known valued subset, unvalued positions and missing outside context. A partial NAV is not total net worth. Consolidated exposure can be informative while account-specific spendability remains separate.

## Corrections and retention

Corrections use reversals/superseding events while retained. Historical recommendation records bind the then-known financial snapshot; reconstruction after correction does not rewrite that decision's knowledge. Lawful deletion/redaction can remove evidence while preserving permitted tombstones and clearly marking incomplete replay. Backup restoration applies deletion state before serving users.

## Required financial tests

Deposit/buy/partial sale fee allocation; duplicate imports across sources; same symbols in distinct accounts; FX transfer with two currencies; split preserving value/cost; dividend entitlement versus payment; unknown cost; negative/zero equity; options adjusted deliverables; stale snapshots; concurrent proposal commitments; correction replay and deletion-aware restoration. Numeric oracles in `tests/fixtures/numerical_oracles.json` are synthetic, not brokerage exports.


---

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


---

# 6. Market data, reference sources and integration qualification

## Three connector classes

Account connectors describe holdings/cash/activity. Interchange adapters import/export user files. Market/research adapters supply prices, filings, calendars and related evidence. A successful implementation in one class does not imply permission or functionality in another. Each provider/deployment record tracks documentation, contractual permission, authentication, field coverage and workload qualification separately.

The capability registry includes provider/plan/feed, allowed instruments, observations/history, quotas, sessions, latency mode, retention, display/export/redistribution/model-processing rights, commercial usage, allowed auth route, review date and qualifying receipts. Default statuses are unqualified until evidence exists. Direct user credentials never authorize centrally pooling an individual feed across tenants.

## Selected integration commitments

| Source | First-release role | Qualification boundary |
|---|---|---|
| Alpaca | Free IEX monitored set; qualified delayed/EOD historical inputs; optional consolidated/OPRA configuration | Actual entitlement, provider use rights and strategy-specific fitness [SRC-01, SRC-02] |
| SEC EDGAR | Primary US company filings and normalized reported facts | Publication-aware units/restatements, permitted retention and fair access [SRC-10] |
| Bank of Canada Valet | Daily CAD/USD reference FX | Not executable pricing; time/series/term checks [SRC-11] |
| CIBC Investor's Edge | Tested portfolio import/reconciliation | Authorized real export samples; no automatic sync claimed |
| Questrade | Read-only sync, direct or qualified aggregator route | Separate personal/commercial route and field tests [SRC-06] |
| Interactive Brokers | Read-only sync via qualified route | Vendor approval/auth/session/field qualification [SRC-07] |
| Wealthsimple | Read-only sync via qualified route; CSV fallback | SnapTrade candidate plus actual tests; fallback not a silent replacement [SRC-04, SRC-08] |
| Yahoo Finance | Portfolio/watchlist CSV import and compatible export | No automatic account sync or Yahoo data license; exact formats remain sample-qualified [SRC-09] |

Additional paid sources may implement the same contracts after rights and fitness review. They are not necessary to make the free baseline useful. No scraping bypass, unofficial token capture or undocumented redistribution route is an accepted fallback.

## Free monitoring workload

Maintain separate tracked universe, scheduled-discovery universe and active streamed set. Prioritize owned/risk-sensitive positions and user-prioritized candidates with explicit slot allocation. A 250-instrument account does not imply 250 live subscriptions. When the provider allows 30 stock streams, expose the remaining scheduled coverage and avoid secret rotation that masquerades as continuity. Batch historical requests within quotas and use permitted caching. [SRC-01]

Qualify daily consolidated history with explicit `feed=sip` and permitted cutoff rather than relying on subscription-selected defaults. Test coverage gaps, corporate actions and required history. IEX volume is not consolidated volume. A strategy requiring unavailable data stays disabled for that configuration, not silently weakened. [SRC-02]

## Freshness is multidimensional

For every proposal evaluate market-event age, receipt delay, portfolio observation age, unresolved account changes, fundamental publication age, calendar validity and FX reference age. Target processing times in Q67 start from recorded trigger receipt but do not make old observations fresh.

SnapTrade documents trade detection for Wealthsimple and Questrade at five-minute-or-longer intervals. Do not claim second-by-second confirmed buying power because quotes refresh faster. [SRC-05] Broker account freshness profiles are independently qualified. A fresh user attestation of no intervening trades is recorded as an attestation and never resets the broker-observed timestamp or upgrades it to broker-confirmed. When required current account state is unavailable, withhold sizing while continuing general research.

## Normalization and continuity

Market observations carry feed-spec hash, source event ID, security/listing ID, raw/adjusted mode, session, eligible conditions, event/publication/receipt time, sequence/gap flags and corrections. Build bars only with specified trade/quote eligibility and left/right interval conventions. Do not backfill the current opening range with unavailable future data or a different feed without reinitialization.

On feed/entitlement change: check compatibility; revalidate rights and expected timing; recover state only from permissible data; invalidate/reassess affected candidates. A reconstructed historical opportunity is retrospective. Suspend affected action flows during incompatible gaps while preserving unrelated services.

## Connector conformance

Test authentication/reauthentication, consent revocation, API limits, pagination, corrected and duplicate events, account selection, currencies, source data timestamps, option identifiers, failed/partial sync, permissions and provider outages. A connector returns observations and diagnostics, not direct journal postings. Secrets remain in an isolated vault/connector runtime and never in model context.

Broker mutations are denied by route/method operation allowlists even where a broad credential could technically perform them. A GET returning orders is account context; it is not authority to create or cancel orders. Distinguish reported open orders from planning commitments so the same cash is not encumbered twice.

## Rights-aware evidence and exports

Content can be stored, indexed, displayed, sent to a model, or exported only under its qualified use profile. Deny by default when rights are unknown. When an evidence snapshot cannot be retained, store permitted metadata and make reproducibility limits explicit. Migration includes a rights-exclusion report. Provider policy changes revoke dependent qualification rather than retrospectively pretending permission was always known.


---

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


---

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


---

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


---

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


---

# 11. Full self-improvement and bounded recursion

## Scope and non-negotiable first-release requirement

The application must support all six targets: user adaptation; research/retrieval quality; strategy/ranking/model quality; decision-evaluator quality; agent/application code; and the improvement process itself. Every target needs real candidate generation, evaluation, review, promotion and recovery. Code and optimizer recursion cannot be deferred as nonessential experiments.

The objective is evaluation-driven improvement under explicit authority, not unconstrained self-modification. A change is not an improvement merely because it is different, earns higher recent simulated P&L or receives another model's approval. Data/consent/risk/budget/release controls stay outside candidate authority.

## Experiment objects and states

An experiment specifies parent version, target class, hypothesis, permitted mutations, immutable input/corpus manifest, development versus final assessment access, predeclared scorecard, maximum cost/compute/storage/time, owner/consent scope and expected approval classes. Candidate states: drafted, running, assessed, rejected, insufficient_evidence, awaiting_human_review, release_approved, installed, adoption_pending, active, rolled_back or suspended.

Every candidate preserves a diff/artifact hash, exact tool/runtime/model versions, all trial attempts, results including failures, resource usage, assessor identity and lineage. No global best-candidate record may discard failures. A candidate can be technically operational yet fail its goal or remain statistically inconclusive.

## Six concrete launch demonstrations

| Target | Positive controlled demonstration | Required negative control |
|---|---|---|
| User adaptation | Improve alert relevance/explanation preference under a permitted private fixture and measured rubric | Candidate broadens trading risk or exports private examples |
| Research/retrieval | Correct a recurring publication-date or unit-extraction defect on separate cases | Better average score but introduces an unsupported material fact |
| Strategy/ranking | Generate a bounded variant, evaluate costs/chronology/prospective policy, produce promotion/adoption evidence when eligible | Higher in-sample P&L from leakage, survivorship or unrecorded trials |
| Decision evaluator | Reduce harmful approvals without unacceptable rejection/latency regressions | Candidate edits its own answer keys or accepts unverified claims |
| Application/agent code | Repair a seeded deterministic defect in an isolated branch, run independent tests and promote via a reviewed package | Attempts production secrets, host writes, unauthorized dependencies or protected-control mutation |
| Optimizer recursion | V0 proposes V1 improvement logic, independent assessment passes, human promotes V1, V1 runs the next candidate cycle | Relabeling identical code or changing acceptance thresholds without authority |

Controlled fixtures demonstrate machinery and specific task quality, not live profitability. A successful code-defect repair is not evidence that a trading strategy has alpha. Predictive strategy candidates can be rejected/inconclusive while the framework works; however, full release still requires the separately specified qualified user-facing capabilities.

## Recursive lineage acceptance

Record optimizer_v0_hash; candidate_v1_hash and nonempty meaningful diff; fixed assessor/test manifest hash; signed approval receipt; installed_v1_hash; next experiment receipt reporting V1 as actual runner; next candidate lineage referring to V1. Test that V0 cannot write approval records, install V1, alter assessor controls or forge signing keys. The next candidate may fail; recursion requires use of the improved optimizer, not a forced chain of wins.

## Independence of assessment

Development cases are accessible within the experiment mandate. Promotion evaluation is executed by an independent service with protected configuration. Public framework source does not imply candidates may mutate or repeatedly query final assessment feedback. Record all assessment access; rotate/add prospectively collected cases when necessary and account for selection pressure. Candidate-proposed rubric changes are separate reviewed changes and cannot validate that same candidate.

Metrics are target-specific: correctness/claim support, usefulness, cost/latency, numerical oracles, quantitative research outcomes, regression rate and protected invariants. Hard constraints cannot be offset by improved average scores. Human-approved minimum effect, uncertainty and regression criteria are declared before promotion evaluation; unresolved thresholds are qualification work, not an invitation for agents to invent favourable ones.

## Isolation and resource enforcement

Run candidate code in an immutable sandbox with bounded CPU/memory/pids/disk/time, restricted syscalls/filesystems and egress denied except mediated permitted retrieval. No production DB write credentials, host Docker socket, signing keys or broad model-provider secrets. Candidate dependencies are pinned/scanned and preapproved or quarantined for review. gVisor is the initial Linux isolation target; its suitability and host compatibility require actual tests. [SRC-16]

Sandbox permission failure pauses the relevant capability and fails the complete-release gate; do not substitute ordinary in-process Python execution. Running all workflows on one machine does not merge their trust boundaries. Supported macOS/Windows operation may require an operator-managed compatible Linux VM, qualified during W0; no native isolation guarantee is asserted in this package.

## Scope, consent and financial budgets

Private adaptation/research configurations live per user or installation. Shared executable changes are installation/release-level and controlled by maintainers. Sharing a generalized lesson still requires authorized data handling; removing a name alone is not proof that financial history is anonymous. Public starter cases and community contributions need license/provenance review.

Improvement inference is capped at USD20/month within the personal USD100 incremental inference ceiling; compute/storage are separately configured operational limits, not secretly unlimited. Essential monitoring and proposal verification outrank experiments. Authorized local resources may reduce API spending, but the system cannot claim they have zero hardware/energy cost or bypass resource limits.

## Promotion, adoption and recovery

Maintainer release approval, operator installation and end-user strategy adoption are distinct. Material investment changes require user adoption after qualification. Candidate code cannot self-install even on a private installation. Rollback restores compatible approved artifacts/configurations; it never edits history to remove poor results. Irreversible migration risks must be reviewed before promotion, with backup/forward-repair or suspension procedures.

Record post-promotion regressions and invalidate dependent recommendations where appropriate. Automatic rollback is permissible only under an already-approved operational policy to a known compatible version; it cannot widen authority or substitute an unadopted strategy. Explanations and evidence remain available to users while retained.


---

# 12. Threat model, privacy and trust boundaries

## Assets and adversaries

Protect portfolio/transaction history, model/broker credentials, user policy and approvals, private learning, evidence integrity, financial calculations, provider entitlements, software signing keys and budgets. Consider malicious external content, compromised providers, hostile CSV/archive files, malicious tenants, accidental privileged roles, generated-code misuse, unauthorized support access and compromised dependencies.

Trust boundaries: browser↔application API; tenant↔tenant; application↔provider; untrusted evidence↔model; model↔read tools; live services↔experiment sandbox; candidate↔assessor; assessor↔human release authority; backup/export↔restored application. Authentication does not collapse these boundaries.

## Baseline controls

Use TLS for exposed interfaces, secure HttpOnly session cookies, appropriate SameSite and CSRF controls, restrictive CORS, server-side authorization on every object/action, session expiry/revocation and step-up authentication for sensitive actions. Store password verifiers using a reviewed current standard or use a qualified external identity provider; self-hosting must not require project-vendor identity. No frontend local storage of broker/model secrets.

Tenant identity is derived from authenticated membership. Enforce tenant scope in application authorization, database composite relationships/policies, object paths, retrieval indexes, jobs and caches. PostgreSQL service roles are nonowner and lack superuser/BYPASSRLS permissions; migration roles are separately controlled. Database owners can ordinarily bypass row policies, so tests must use production-like roles rather than a convenient administrator. [SRC-15]

Encrypt secrets at rest with per-installation/operator-controlled key management and documented rotation/recovery. Retrieval/model workers receive opaque secret references or mediated calls, not plaintext credentials. Hosted operators must audit exceptional access. Do not bundle universal secret keys in a container or require a hidden vendor bootstrap call for self-hosting.

## Broker and model controls

Request read-only scopes where available. Connector operation allowlists block broker order creation, amendment/cancel, withdrawals and unauthorized exercise even if a broad credential technically supports them. No order tool exists in a model registry. A virtual-order endpoint writes only the simulator ledger. Auth routes and provider financial-use terms must both pass qualification.

Never collect prohibited consumer-session tokens, scrape authenticated services as an undocumented fallback, or use another tenant's credentials to overcome a limit. Costs are reserved transactionally before calls; tool recursion/retries have hard ceilings. A model-generated budget override is invalid.

## Untrusted input controls

Controlled HTTP retrieval blocks private/link-local/metadata addresses, disallowed schemes, DNS rebinding, redirects to forbidden targets, oversized content, MIME mismatches and decompression bombs. Parse HTML/documents without executing scripts. Public URLs are not intrinsically trusted. Imported files get type/size/row/encoding checks; CSV output neutralizes spreadsheet formula injection. Archive import rejects traversal, symlinks, absolute paths, duplicate paths and executable auto-activation.

External text is evidence, never instructions. Keep prompts and retrieved content separated with explicit trust tags. Model proposals must pass schema, claims, numerical and final authorization checks. The control plane cannot be overwritten by a clever cited passage. No inaccessible chain-of-thought is required for auditing; inspect structured tool/evidence records.

## Sandbox and supply chain

Experimental code receives a dedicated sandbox role/filesystem/network policy; no host Docker socket or production environment. Test attempted host-file reads, process escape, network exfiltration, fork/resource exhaustion, dependency substitution and signing-key access. Scans and a named runtime are supporting evidence, not a security guarantee. Record actual version, host configuration and attack results.

Dependencies, prompts, datasets, models and community skills have separate provenance/license inventories. Require lockfiles, package integrity checks and a software bill of materials for releases. A code change cannot modify protected acceptance controls, authorization policy or signing keys through an ordinary improvement task. Such modifications require independent privileged governance outside the candidate pathway.

## Privacy and lifecycle

Collect only information needed for chosen capabilities. Partial portfolio disclosure is allowed; broad financial suitability claims are constrained accordingly. Private learning is tenant/installation-local by default. Explicit consent is purpose-, data-category-, recipient- and version-specific; revocation prevents further use and triggers the defined derivative-state handling.

Retention classes distinguish financial records, recommendation history, licensed evidence, transient provider payloads, logs, experiments and backups. Do not invent one universal legal retention period for Canada/US. Product-specific counsel and provider terms establish applicable obligations; until configured, commercial activation is gated. Logs use redacted identifiers and no prompts/raw financial data by default.

Deletion applies to source objects, retrieval indexes, caches, private learned state and exports under policy. A trained derivative that cannot be selectively unlearned must be retired/rebuilt or retained only under a lawful disclosed exception; do not promise impossible instantaneous unlearning. Deletion tombstones are reapplied on restore. Evidence removal changes reproducibility status and may invalidate current proposals. Legal holds are explicit, scoped and auditable.

## Incident procedures

Credential exposure: revoke/rotate, suspend affected connector, assess access and notify under qualified policy. Cross-tenant exposure: disable affected paths, preserve permitted forensic evidence, determine scope and remediate before reactivation. Wrong/stale data: quarantine source version, invalidate dependent proposals, disclose affected analyses and recompute without rewriting prior knowledge. Sandbox failure: stop experiments and freeze promotion. No remediation includes unauthorized broker actions.


---

# 13. Dashboard, research and operational UX

## Navigation and persistent state

Primary navigation: Overview, Accounts, Decisions, Watchlists, Research, Strategies, Simulation, Improvement, Settings. A persistent mode banner distinguishes real-account analysis from simulation, and rules-only from AI-enabled workflows. Every screen communicates declared portfolio scope and relevant last-update/coverage state.

Avoid a chat-only application. Users must discover stale accounts, expired proposals and pending policy adoption without asking the model the right question. Chat reads and explains the same records and can initiate a governed candidate; it cannot publish a trade recommendation outside the normal pipeline.

## Overview and accounts

Show account-specific cash and consolidated exposure separately. Present capital allocations, pending reconciliation, value coverage, outside-scope holdings and risk pauses. A partial-value portfolio is labelled partial. Toggle reporting currency without implying a real FX conversion. Account details show sources and effective timestamps; reconciliation previews show candidate matches, duplicates, unresolved rows and consequences before commit.

## Decision card contract

Each card shows action/horizon, account/sleeve, status/expiry, origin, policy/strategy versions, quantity or unsized reason, entry/exit conditions, data status, three qualification assessments, costs, capital impact, evidence, counter-thesis and alternatives. Expose an explicit reason for rejection/abstention without overwhelming the default view; details expand into calculation and claim provenance.

Primary controls: inspect, dismiss, mark planned, report external execution, request fresh analysis. No live broker buy/sell/submit button. Reporting execution requires clear fields and displays user-reported status until reconciliation. Material proposal changes create a new version and notification, not an invisible edit.

## Watchlists and research

Watchlists support create/read/update/delete, tags, thesis, invalidation conditions, review dates, per-symbol monitoring mode and priority. Removing an entry never removes an account position. Security research distinguishes data/facts, model assumptions, strategy scores, portfolio fit and missing coverage. Social input is opt-in and labelled claim/sentiment, not automatic corroboration.

Sources drill down to permitted locators and exact supporting passages/data. Deleted or inaccessible source snapshots display limitations. Confidence language distinguishes evidence support from investment outcomes. Text alternatives accompany charts and Greeks; units, currency and horizons are always visible.

## Strategies and policy

Strategy cards separately show implemented version, operational qualification, investment evidence and current eligibility. Configuration controls only expose declared parameters/ranges. A material setting change opens a candidate/review/adoption process; it never becomes live because an AI recommended it. Draft numerical risk fields have no hidden default approval. Explain conservative universe exclusions and liquidity/data prerequisites in plain language.

## Options and simulation

Options-disabled users do not see new options pitches but existing option risk remains visible. Payoff chart captions identify expiry versus current-value scenario, known contract terms and uncertainty. Collateral and assignment warnings are distinct from premium P&L. Simulator pages retain a persistent virtual badge, modeled fill assumptions, no-fill outcomes and versioned cash-flow-matched benchmarks. No mixing simulated success with actual investment returns.

## Improvement and governance

Show all six target classes, private versus shared scope, experiment budgets, queued/running/rejected/inconclusive candidates, parent/child versions, independent assessment and promotion/adoption status. Users can inspect failures and rollback. Protected actions require correct role and step-up authentication; UI visibility never substitutes for backend authorization. Operator controls expose isolation health and why an experiment cannot run.

## Notifications

In-app feed is canonical. Email uses a configurable provider-neutral SMTP/service adapter; browser push is supported where the client/environment permits it. Self-hosting has no required project-vendor push service. Quiet hours, snooze, severity, deduplication and digests are configurable. Default external notification payloads minimize financial details.

Delivery records distinguish generated, attempted, provider accepted, failed and any separately supported user-open event. Sending is not proof of receipt, attention or execution. Links require normal authorization and retrieve current status; an old message cannot reactivate an expired proposal.

## Accessibility and usability acceptance

Keyboard access, clear focus order, descriptive field labels, noncolor status cues, accessible tables, readable number/currency formats, error summaries and mobile layout are required. Document accessibility tests rather than claiming conformance without assessment. Test empty portfolios, partial imports, unknown values, lost provider access, budgets exhausted, paused risk, incomplete AI review and deleted evidence. Chart convenience must never hide the underlying values.


---

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


---

# 15. Deployment, reliability, cost and release operations

## Supported deployment target

A single-machine Linux container deployment is the first self-hosted reference. Qualify actual CPU/RAM/disk and supported host/kernel/runtime versions in W0; candidate evaluation needs extra bounded resources. No mandatory GPU, Kubernetes, vendor account or proprietary signal service. Non-Linux hosts may use an operator-managed compatible Linux environment after testing; do not claim native gVisor parity without evidence.

Logical processes: API, standard worker, market ingestion, model gateway, isolated experiment runner, PostgreSQL, static/web interface and optional notification gateway. Secrets, backing storage and roles differ by process. Optional local models are independently configured and have their own hardware requirements. A local filesystem storage adapter replaces hosted object storage without changing domain logic.

Hosted deployment separates tenant/application data, backups and monitoring. Managed service billing is not a financial-ranking input. Provider costs are visible separately. No pricing or scaling promise is inferred from this specification.

## Measurable reference workload

One self-hosted active user with up to 10 accounts, 250 distinct tracked instruments including contracts and 100000 activity records. Hosted reference is 100 onboarded/background-monitored users and 20 simultaneous interactive sessions. Streaming capacity remains governed by each user's actual entitlements; 250 tracked does not mean live marketwide scanning. Personal USD100 inference budget is not a budget for 100 customers.

Combined tests run imports/reconciliation, monitoring, UI research, simulations and bounded improvement concurrently. Measure sustained rates and bursts, memory/disk growth, queue age, cost reservations and tenant fairness. Mark actual hardware/runtime and data-source configuration on every result.

## Timing and service targets

P95 factual event publication <=5 seconds after receipt; P95 rules-only actionable proposals <=15 seconds including mandatory checks. AI intraday deadline is the earlier of the strategy's useful window and a provisional 60 seconds from original trigger. Source freshness and remaining action window are independent conditions. Count rejected/expired/deadline-missed candidates in the denominator and report why work was dropped.

Target hosted application availability is 99.9% monthly for its defined service surface; hosted RPO 15 minutes and RTO four hours under tested scenarios. These are planning targets, not measured SLAs. Distinguish application health, provider health and end-to-end capability availability. A healthy server without required feed data cannot show monitoring as fully healthy.

## Budget ledger

For the personal pilot: USD100/month maximum incremental inference and at most USD20/month for improvement. Existing subscriptions, hosting, market-data upgrades and other approved operational charges are separate. Limits apply to application-initiated requests, not unrelated spending with the same key elsewhere.

Before a call, atomically reserve a conservative maximum including bounded output/tool/retry charges. Multiple workers cannot reserve the same remaining allowance. After completion reconcile actual usage; unknown billing outcomes retain a conservative reservation pending reconciliation. No silent auto-top-up/provider switch. Missing authorized rate information blocks a billable call or uses a separately approved nonbillable path; do not guess cost to pass a budget test.

Experiments get lower priority and independent CPU/memory/storage/time ceilings. Disabling all experiments to save money fails the demonstrated-improvement requirement; measure representative completed workloads and use efficient/cadence-limited runs within approved limits. Expanding budget requires separate authorization, not an agent recommendation.

## Recovery and rollback

Backups are encrypted with operator-controlled keys; recovery drills verify data integrity, current deletion tombstones, secret reauthorization and projection rebuild. The journal is not reconstructed from model messages. Record backup completion/cutoff, restore point, measured outage and recovery data loss.

Release packages have manifest, provenance, dependency inventory, approval identity, schema compatibility and integrity signature. Migration follows expand/contract or another reviewed safe plan. No experiment self-installs. Rollback selects compatible approved artifacts; unsafe downgrade triggers suspension/forward repair, not data corruption. Preserve original decision records and public release notes.

## Operational runbooks required

Provider outage/revocation, wrong/corrected prices, partial broker sync, unresolved account conflict, quota exhaustion, model timeout, risk-pause/resume, credential compromise, cross-tenant incident, sandbox breach, failed migration, restore and deletion reappearance. Each runbook names trigger, immediate containment, affected-capability status, evidence preservation, recovery checks and user communication.

A signed release is not automatically ready for commercial use. All gates applicable to each deployment and user jurisdiction must pass; opening one market while silently dropping the other agreed commercial scope is not the full release. Internal development demos may proceed with unmistakable limitations and no production claim.


---

# 16. Governance, commercial permissions and honest claims

## Product-specific legal work

The product gives personalized investment action proposals even though users execute externally. Nonexecution alone does not establish an exemption from advisory regulation. BCSC materials discuss registration obligations for the business of advising about securities; US regulator guidance identifies relevant adviser frameworks for automated advice. These are issue-spotting sources, not a legal determination for this app. [SRC-17, SRC-18]

Qualified review must cover intended Canadian provinces/territories and US federal/state scope, target customers, personalization, compensation, registration/exemption/partner model, options, marketing/performance claims, privacy, cross-border processing, retention, complaints and terms. User residence and security listing market are separate. Record approved capability/jurisdiction combinations; unresolved clearance blocks commercial activation, not private engineering work that is otherwise permitted.

## Provider rights and financial-use policies

For every source/model/runtime/connector, review authentication and permitted content use separately: commercial display, caching, history, redistributing/derived outputs, user exports, model input processing, retention/training, automated financial recommendations and third-party hosting. BYO keys are a funding/auth mechanism, not a commercial sublicense. Provider feature changes trigger requalification. No archive of web documentation alone counts as approval.

Data-source omissions must remain visible in user explanations. A missing news integration is missing coverage, not evidence that nothing happened. Subscription-backed AI is required through supported provider-specific routes, but cannot be delivered through prohibited credentials if partnership/permission is unavailable. This is a named factual blocker to resolve, not silently waive.

## Open-source governance

Apache-2.0 is the selected application license. The core, default strategies, prompts, evaluator instructions, orchestration and suitable tests should be inspectable and independently usable. Data/model/provider licenses remain separate. The package does not reproduce ECC or other third-party code. Final copyright ownership, contribution process, dependency notices and commercial name/domain are owner-controlled publication details. [SRC-23]

Community proposals enter the same review/qualification process; open-source availability is not trust. No change can secretly upload portfolios, weaken risk checks, broaden hosted executable permissions or rewrite its own evaluator controls. Managed hosting value is reliable operation/support/onboarding, not withholding core decision logic behind a mandatory vendor service.

## Authority hierarchy

User-approved product scope → applicable law/provider permission → protected system controls → versioned family qualification policy → maintainer release approval → operator installation → user investment-policy/strategy adoption → current proposal validation. More specific technical configuration cannot waive higher-authority safety or permission requirements. Current account/data state can render an adopted strategy temporarily ineligible.

The planning delegation means engineers need not ask repetitive preference questions. It does not authorize actual API charges, publishing, deployment, credential collection, broker actions or automatic strategy adoption. Record concrete blockers and recommended resolutions; continue unrelated safe work instead of stalling the entire project.

## Evidence and completion claims

Status vocabulary: specified; docs checked; permission pending/granted; auth tested; field tested; workload tested; qualified for stated deployment; suspended. Keep strategy operational qualification, investment evidence and user eligibility distinct. A test case is not test execution. Synthetic fixtures are not real broker exports or live trading outcomes. A code-improvement demo is not proof of improved investment returns.

Release claims require versioned execution receipts and explicit remaining limitations. Marketing cannot cherry-pick winners or hide costs/unqualified sources. Model confidence does not certify outcomes. The workbook and PDF in this package summarize implementation planning, not a completed or registered financial product.


---

# 17. Testing strategy and evidence receipts

## Layers

Specification checks validate internal file consistency, IDs, dependencies, JSON schemas, OpenAPI references, configuration counts, fixtures and links. They are executed on this package and reported separately. They do not test an application that does not yet exist.

Application unit/property tests cover monetary/unit accounting, rounding, source matching, timestamps, calculations, eligibility and state machines. Contract tests cover API/adapter schemas and negative permissions. Integration tests use production-like PostgreSQL roles and isolated workers. End-to-end tests exercise user journeys on supported deployment profiles. Adversarial tests cover prompt injection, cross-tenant access, untrusted code and archive/CSV/network attacks. Research qualification and legal/rights gates require separate evidence beyond software tests.

## Required numerical oracles

Use synthetic exact cases for cash and lots, split invariance, dividend handling, FX return interaction, TWR cash flows, unique/ambiguous MWR, coordinated cash proposals, covered-call coverage, cash-secured put collateral and all vertical directions. Compare calculation outputs to independent oracles, not two copies of the same implementation. Edge cases include missing values, fees/currencies, partial fills, negative equity and no valid roots.

## Property and concurrency tests

Repeated imports preserve total holdings/cash; corrections reverse/repost without deleting original knowledge; unit/currency postings balance in their own commodity; split preserves cost and economic value; no tenant can cross namespace boundaries; final proposal commitments never exceed qualified cash; duplicate event processing cannot duplicate account activity; deletion tombstones survive restore. Run randomized input sequences with fixed reproducible seeds and shrink failing examples.

## Research and AI tests

Claim-verification cases include wrong company/date/unit, outdated filing, uncited material assumptions, syndicated evidence, retracted source, calculation mismatch and hostile instructions. AI evaluators see blind evidence first and log independent execution IDs. Timeout, insufficient budget and revision changes exercise pending/research-only/expired states. No test assumes a model answer is ground truth.

Historical strategy tests include future filings, survivor-biased universes, corporate-action leakage, ambiguous fills and unrealistic costs. Prospective decisions must be frozen before outcomes. Promotion criteria are defined before evaluating candidates; incomplete criteria block qualification. Required full-scope functionality cannot be certified from mocks alone.

## Full release checklist

All 16 release gates in planning/release_gates.csv need receipts. Demonstrate free useful decisions, each named import/sync, independent AI thesis/review, timely intraday, options risk/lifecycle, scoped loss pauses, simulation/benchmarks, all six improvement targets and recursion, portability/deletion, combined performance, security and recovery. Rights/legal approval and actual provider access are independent gates.

Every acceptance row has a stable ID. The Gherkin files are specification scenarios, not step implementations. Create executable steps/tests during the corresponding tasks, refine vague triggers into concrete fixtures, and preserve the original requirement linkage. Include negative controls, not just happy paths.

## Completion receipt format

Task/test/gate IDs; actual code commit and dirty-tree status; schema/config/data versions; environment and hardware; command or manual procedure; authorized dataset provenance; start/end times; expected and observed outcomes; artifacts/log hashes; test count/pass/fail/skipped; numerical residuals; model/provider cost and deadlines where relevant; limitations; reviewer identity; rollback/recovery notes. Skips are not passes.

Do not invent application command names in documentation before they exist. Bootstrap establishes the reviewed command registry and leaves unobserved checks configured=false. Existing AC/ECC registries are merged, not replaced. Small edits need scoped tests; scope-critical changes also run the financial/security regression suite.


---
# 18. Dependency-ordered work plan


All waves belong to the same agreed first release. Intermediate demos are internal integration milestones, not permission to commercialize an incomplete or unqualified product. There are no speculative calendar estimates or commitments to future delivery.

| Wave | Purpose | Integration exit |
|---|---|---|
| W0 | Repository/contract inspection; provider, legal, free-data, model and sandbox proofs | Uncertainties have explicit evidence plans; foundational blockers discovered early |
| W1 | Independent workspace, tenancy, identity, journal, real import/sync and calculations | Reconciled multi-source account state with independent numerical tests |
| W2 | Approved policy, risk, explicit feeds, durable jobs and proposal transactions | Deterministic event-to-proposal pipeline obeys freshness and concurrency controls |
| W3 | Six-family stock foundations and honest research qualification | Free non-intraday path plus evaluated regular-session candidates, no edge claims from implementation |
| W4 | Options, evidence, model gateway, independent research/review, simulation | Qualified configuration-specific AI/options workflows and clean real/sim separation |
| W5 | ALL SIX improvement paths, independent assessor, code sandbox and recursion | V0→V1→next cycle demonstrated; human promotion and user adoption intact |
| W6 | Accessible dashboards, notifications, portability, privacy and managed-service boundary | Users can inspect, act externally, export, delete and migrate without hidden authority |
| W7 | Combined load, adversarial security, recovery, complete E2E and commercial/open-source review | Every applicable gate has evidence; required unresolved blockers stop full release |

## Critical dependencies

Start legal/provider approval and sandbox qualification alongside architecture, not after the UI. The journal and account-source reconciliation precede trustworthy sizing. Evidence and final portfolio checks precede AI recommendations. The improvement assessor and protected release plane precede code/optimizer promotion. A connected broker or model is never a substitute for permission and field/capability qualification.

Independent worktrees may parallelize disjoint tasks after contract ownership is assigned. Financial schemas, final-risk predicates, shared job semantics and source-of-truth contracts each have one accountable integrator. Review interface changes before parallel consumers implement incompatible variants.

## Task execution protocol

For each task: inspect current repository → read bounded task/context → refine local plan → write failure/acceptance tests → implement the smallest coherent increment → execute real checks → independent review → evidence receipt → update status/handoff. Stop unsafe or externally blocked operations, but continue authorized unrelated work. Do not restart preference interviews for already-settled scope.

## First coding session

Use handoff/BOOTSTRAP_PROMPT.md. Complete T001 only unless the session is explicitly authorized for more. Leave unknown runtime commands unconfigured. The next safe implementation unit is the reviewed contract/scaffold plus deterministic financial journal fixture path, while qualified people pursue external approvals.

## Change control

An implementation discovery can refine a technical default through an ADR with traceability and tests. It cannot delete mandatory scope, raise spending ceilings, enable broker mutation, substitute unlicensed data or waive runtime approvals. Track unresolved factual issues in planning/qualification_register.csv; do not write fiction to close them.


---
# 19. Dated source register


Checked 8 October 2026. Source facts are distinguished from selected design requirements. No provider credentials were supplied; no authenticated integration was tested. The source summaries are deliberately bounded; linked documentation must be rechecked at the relevant release gate.

## SRC-01 — Alpaca Market Data plans

https://docs.alpaca.markets/us/docs/about-market-data-api

**Access:** Official documentation inspected. **Supports:** Individual Basic lists IEX-only stocks, 30 stock streams, 200 historical requests/minute, indicative options. Individual and business arrangements differ.

**Boundary:** Provider/runtime/rights testing still required.

## SRC-02 — Alpaca feed semantics

https://docs.alpaca.markets/us/docs/market-data-faq

**Access:** Official documentation inspected. **Supports:** Historical feed selection is explicit; free SIP history requires end at least 15 minutes old. Subscription-default feed selection can change.

**Boundary:** No assumption of consolidated live quotes in the free mode.

## SRC-03 — Alpaca paper environment

https://docs.alpaca.markets/us/docs/paper-trading

**Access:** Official documentation inspected. **Supports:** Paper simulation omits important real execution effects. Paper-only access is a candidate for free onboarding.

**Boundary:** Our simulator must declare its own economics; paper results are not proof of real performance.

## SRC-04 — SnapTrade broker access

https://docs.snaptrade.com/docs/broker-access-guide

**Access:** Official documentation inspected. **Supports:** Broker guide lists supported integration routes, with production access requirements.

**Boundary:** Test each broker, account type, auth method, deployment and field; names on a list are not acceptance evidence.

## SRC-05 — SnapTrade trade detection

https://docs.snaptrade.com/docs/trade-detection

**Access:** Official documentation inspected. **Supports:** Wealthsimple and Questrade trade detection is documented at five-minute-or-longer intervals.

**Boundary:** Account-state freshness must be separate from quote and processing latency.

## SRC-06 — Questrade API

https://www.questrade.com/api

**Access:** Official documentation inspected. **Supports:** Personal users can access account/market information; API trade execution is limited to partner developers.

**Boundary:** Read-only capabilities only; company integration permission is a separate gate.

## SRC-07 — IBKR Web API

https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/

**Access:** Official documentation inspected. **Supports:** Read-only portfolio subset exists. Third-party vendor integration requires approval and a distinct authentication process.

**Boundary:** No unattended commercial or local auth route assumed from a personal login.

## SRC-08 — Wealthsimple activity export

https://help.wealthsimple.com/hc/en-ca/articles/39449252453019-View-your-account-activity-and-running-balance

**Access:** Official documentation inspected. **Supports:** Activity CSV download is documented.

**Boundary:** Real anonymized samples and corporate-action reconciliation are not yet tested.

## SRC-09 — Yahoo portfolio CSV

https://help.yahoo.com/kb/finance-for-web/download-portfolio-data-yahoo-finance-sln15034.html

**Access:** Official search excerpt only; direct open failed. **Supports:** Yahoo help describes portfolio/list CSV transfer.

**Boundary:** Exact current columns, round-trip loss and transaction completeness remain sample-based qualification gates. Not a Yahoo market data license.

## SRC-10 — SEC EDGAR APIs

https://www.sec.gov/search-filings/edgar-application-programming-interfaces

**Access:** Official documentation inspected. **Supports:** SEC provides submissions and XBRL APIs.

**Boundary:** Respect fair-access rules; publication dates, units and restatements require normalization. No analyst-consensus promise.

## SRC-11 — Bank of Canada Valet

https://www.bankofcanada.ca/valet-api-how-to/

**Access:** Official documentation inspected. **Supports:** Valet has no usage charge, registration or access-key requirement; daily data can be cached.

**Boundary:** Reference FX, not an executable conversion quote; respect terms and data timing.

## SRC-12 — ChatGPT plan in other apps

https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites

**Access:** Official documentation inspected. **Supports:** Plan-funded requests are available in participating apps/tools subject to eligibility and plan rules.

**Boundary:** App participation, deployment and permissible financial use must be qualified; no universal subscription API.

## SRC-13 — Claude Code authentication rules

https://code.claude.com/docs/en/legal-and-compliance

**Access:** Official documentation inspected. **Supports:** Native unmodified Claude Code authentication is distinguished from prohibited collection/intermediation of Claude.ai credentials by third parties.

**Boundary:** Use API keys or approved native flow; no session-token proxy. Hosted feasibility remains open.

## SRC-14 — xAI API authentication

https://docs.x.ai/developers/rest-api-reference/management/auth

**Access:** Official documentation inspected. **Supports:** API keys authenticate inference requests as bearer credentials.

**Boundary:** API adapter required; consumer-subscription access is not promised.

## SRC-15 — PostgreSQL row security

https://www.postgresql.org/docs/current/ddl-rowsecurity.html

**Access:** Official documentation inspected. **Supports:** Superusers, BYPASSRLS roles and ordinarily table owners can bypass row security.

**Boundary:** Use nonowner/nonbypass service roles and explicit isolation tests; RLS alone is insufficient.

## SRC-16 — gVisor overview

https://gvisor.dev/docs/

**Access:** Official documentation inspected. **Supports:** gVisor provides an application-kernel isolation approach integrated with container execution.

**Boundary:** Chosen Linux sandbox qualification target, not proof of sufficient security or universal host compatibility.

## SRC-17 — BCSC securities fundamentals

https://www.bcsc.bc.ca/industry/financial-technology-innovation/securities-law-fundamentals

**Access:** Official documentation inspected. **Supports:** Being in the business of advising about securities can engage registration requirements, subject to applicable exemptions.

**Boundary:** Counsel must classify this actual non-discretionary product per jurisdiction. A disclaimer is not clearance.

## SRC-18 — SEC investor bulletin on robo-advisers

https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-45

**Access:** Official regulator guidance inspected. **Supports:** Automated investment advice has securities-law considerations, including SEC/state adviser frameworks.

**Boundary:** Not a product-specific determination; map federal and relevant state obligations with counsel.

## SRC-19 — FINRA stop-order risks

https://www.finra.org/investors/insights/stop-orders-factors-consider-during-volatile-markets

**Access:** Official guidance inspected. **Supports:** A stop trigger does not guarantee the execution price, and stop-limit orders may not execute.

**Boundary:** Planned stop loss is a scenario, not guaranteed maximum loss.

## SRC-20 — OIC assignment

https://www.optionseducation.org/referencelibrary/faq/options-assignment

**Access:** Official industry education inspected. **Supports:** American-style short options can be assigned before expiry.

**Boundary:** Separate expiration payoff from pathwise collateral, assignment and settlement risk.

## SRC-21 — OIC vertical put spreads

https://www.optionseducation.org/strategies/all-strategies/bear-put-spread

**Access:** Official industry education inspected. **Supports:** Vertical-spread analysis includes early-assignment and financing consequences, not merely an expiry payoff.

**Boundary:** Do not model a spread as an inseparable atomic position throughout its life.

## SRC-22 — Probability of Backtest Overfitting

https://scholarworks.wmich.edu/math_pubs/42/

**Access:** Primary research landing page inspected. **Supports:** Bailey, Borwein, Lopez de Prado and Zhu discuss selection-related backtest overfitting and a diagnostic framework.

**Boundary:** Diagnostics inform research; no universal test proves a trading edge.

## SRC-23 — Apache License 2.0

https://www.apache.org/licenses/LICENSE-2.0.txt

**Access:** Official license text inspected. **Supports:** Chosen project license. Dependencies, datasets, prompts and model terms still require separate compatibility review.

**Boundary:** Copyright ownership/contributor policy must be finalized before public publication.

## SRC-24 — Codex AGENTS.md

https://learn.chatgpt.com/docs/agent-configuration/agents-md

**Access:** Official documentation inspected. **Supports:** AGENTS.md provides repository instructions with discovery/precedence rules.

**Boundary:** Verify actual client version and applicable instructions in the target repository.

## SRC-25 — Codex execution plans

https://developers.openai.com/cookbook/articles/codex_exec_plans

**Access:** Official documentation inspected. **Supports:** Persistent execution plans can specify goals, steps and verification evidence for longer work.

**Boundary:** This bundle supplies its own bounded task contracts; no external plugin is required.

## SRC-26 — Existing user AC/ECC compatibility guide

Library: Codex_AC_ECC_Setup.md, 2026-10-06

**Access:** User Library excerpt inspected. **Supports:** Existing guide uses AGENTS.md, CLAUDE.md and a shared .agentic state; configured checks must not be overwritten.

**Boundary:** This bundle is an original project-specific handoff, not the ECC distribution or an installer.
