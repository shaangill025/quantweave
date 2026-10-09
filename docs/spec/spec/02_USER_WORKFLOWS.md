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
