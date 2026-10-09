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
