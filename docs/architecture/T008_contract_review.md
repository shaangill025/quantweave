# T008 — Architecture and contracts review

| Field | Value |
|---|---|
| Task | T008 Architecture and contracts review (W0). Roles: lead engineer, quant, security |
| Requirements | R065, R070, R071, R075, R078, R080, R081, R084, R086, R087, R088, R099 |
| Inputs reviewed | `docs/spec/adr/ADR-001..012`; `docs/spec/spec/03, 04, 05, 07 (states), 11 (states/roles), 12, 15 (processes), 17`; `docs/spec/contracts/` 0.1.0 (README, `openapi.yaml` 0.1.0-design, 26 schemas, `data_dictionary.json`, 7 examples); `docs/spec/tests/fixtures/`; AT065–AT099 rows of `tests/acceptance_matrix.csv` |
| Baseline | `7181c12` on branch `t008-architecture-contracts-review` |
| Executable support | `tools/check_contracts.py` (no broker mutation route, idempotency/CSRF headers on writes, decimal-string money/quantities, offset timestamps in examples; 7 negative controls) |

Throughout this review, "schema-valid" means the payload has the right shape. It does not mean the
payload is finance-valid. A balanced-looking `ledger_event` can still lose a share, and an `active`
proposal can still spend cash that is already committed. Section 4 lists the invariants that only domain
code can enforce and names the test task for each.

`docs/spec/` is the imported package and stays unchanged. The resolutions below apply to the project-owned
contract (see A-01). They do not edit `docs/spec/contracts`.

## 1. ADR dispositions

| ADR | Disposition | Reasons and refinements |
|---|---|---|
| 001 Independent modular core | **Accept, refine** | The single Python core shared by API, worker, simulation and experiments is right for one maintainer and one machine. Refinements: (a) enforce the import rules in §2 mechanically from T009, because the ADR's "cross-module ownership" is otherwise unenforced; (b) `apps/api` and `apps/worker` are the only composition roots, and nothing in `packages/*` reads the environment or opens connections; (c) the experiment runner uses the same packages but runs as a separate process with a separate role (§5). Being one monolith does not mean one trust domain. |
| 002 No broker execution | **Accept, refine** | Correct and non-negotiable. The ADR names only submit/amend/cancel/exercise. Extend the forbidden set to match spec §12: withdrawals, transfers and unauthorised exercise. Add a second layer beyond connector allowlists: no route, no model tool and no adapter method may carry these verbs. `tools/check_contracts.py` now asserts this for the OpenAPI surface. `POST /accounts/{id}/execution-reports` records a user's statement (`source: user_reported`, `broker_confirmed: false`) and does not touch a broker. T016 must give connector port types no mutating methods at all, so the absence is enforced by types and not only by a runtime allowlist. |
| 003 Journal and source identity | **Accept, refine** | Append/reversal journal and separate observations are right. Gaps: the unit journal is single-sided (`instrument_movements`), the `observed` status mixes source observations into the journal, and decision/knowledge time is implicit (C-12, C-13). The journal must be bitemporal: `effective_at` (economic time) and `recorded_at` (knowledge time, server-assigned, monotonic per account). |
| 004 Feed and capability registry | **Accept** | Fail-closed qualification is right. Refinement: calendar identity must reach observations and option expiry, not only `feed_spec` (C-08). |
| 005 Proposal authority, atomic final check | **Accept, refine** | Keep the lock order specified in §3: account, then currency, then sleeve, sorted by id within each class. Make If-Match versus body-revision semantics explicit (C-04). Approvals must be typed receipts, not free-text acknowledgements (C-20, R099). |
| 006 Self-improvement, protected release | **Accept, refine** | Reconcile the experiment state vocabulary with spec §11 and map six targets onto four promotion `change_kind`s (C-21). Enforce protection with database grants and process credentials (§5), not only with the `requires_human_promotion: true` constant. |
| 007 Tenant-private default | **Accept, refine** | The server derives tenant from membership. Cross-tenant ids return 404, never 403 (no existence oracle). Composite `(tenant_id, id)` keys back every tenant-owned FK. Ids are UUIDs (C-07). |
| 008 Research and performance claims | **Accept** | The three-part `Qualification` is right. `strategy_version.evidence_status` is a free string and `action_eligible` is a stored boolean, which recreate the single badge the ADR forbids (C-22). |
| 009 Financial-risk computation | **Accept, refine** | Needs a positive-decimal type for live quantities, typed denominators and conservative rounding directions (§3.2). |
| 010 Budgets and durable work | **Accept, refine** | Job states lack retry-wait and dead-letter (C-17). The budget month has no time zone (C-19). There are two idempotency layers (HTTP and job) and they need separate definitions (§3.3). |
| 011 Open-source packaging | **Accept** | ADR-013 sets the licence policy. psycopg 3 is LGPL-3.0, and ADR-013 records how it is used. |
| 012 Contracts before implementation | **Accept, refine** | The artifact validator passes `format: date-time` checks only when an RFC 3339 checker is installed. One is not installed here, so "Schema rejects timestamps without offsets" (AT080) is **not** enforced today (C-05). Validators must fail closed when a format checker is missing. |

## 2. Module boundaries and import rules

Arrows mean "may import". Anything not listed is forbidden. T009 enforces these rules with a stdlib AST
check under `tools/` (no new dependency) that runs in CI.

| Module | Owns | May import | Must not import |
|---|---|---|---|
| `packages/domain` | Value types: `Money`, `Quantity`, `Price`, `Rate`, decimal context and rounding rules, ids, UTC instants, `CalendarId`/`SessionDate`, enums, `Unavailable` result, error codes, approval receipt types | stdlib only | everything else |
| `packages/portfolio` | Journal, postings, lots, reconciliation, snapshots, cash/commitments, ports `LedgerRepository`, `AccountConnector`, `SourceRecordStore` | domain | quant, strategies, ai, adapters, apps, any SDK, any DB driver |
| `packages/quant` | Valuation, TWR/MWR, FX attribution, sizing, drawdown, options payoff/collateral, statistics (floats allowed only here, behind declared tolerances) | domain | portfolio internals (takes typed series and snapshots from domain), ai, adapters |
| `packages/strategies` | Six families, `StrategyEngine`, `RiskValidator`, `ProposalPublisher` port | domain, quant, portfolio (read-only snapshot and commitment ports) | ai, improvement, adapters |
| `packages/evidence` | Evidence, claims, rights profiles, `EvidenceStore` port | domain | ai, strategies |
| `packages/ai` | Researcher, `DecisionEvaluator`, prompt/tool registry (read tools only), `ModelGateway` port, budget reservation client | domain, evidence, quant (calculation checks) | portfolio write ports, strategies' publisher, improvement, adapters, provider SDKs |
| `packages/improvement` | Experiment orchestration, candidate lineage, `ExperimentAssessor` (protected subpackage), `ReleaseController` port | domain, evidence, quant, strategies (to run candidates), ai | portfolio write ports, publisher, adapters, signing implementations |
| `packages/adapters` | Implementations: PostgreSQL repositories (psycopg), object/file store, provider and broker read connectors, model APIs, SMTP/push, KMS signer, **the only HTTP client**: `adapters/http` (SSRF-safe fetcher) | domain plus the port modules it implements | apps; one adapter must not import another adapter's internals; no adapter other than `adapters/http` imports an HTTP library directly |
| `apps/experiment_runner` | Sandboxed entry point that runs one candidate artifact against an immutable snapshot | domain, quant, strategies, evidence, ai (read-only), the candidate artifact; a budget-scoped model-gateway client | adapters (no DB, object-store write or HTTP client), `improvement.assessment`, signing, any live write port |
| `apps/assessor` | Entry point for the protected `ExperimentAssessor`, outside the sandbox | domain, quant, evidence, `improvement.assessment`, the evaluation-receipt repository adapter | candidate code (it reads candidate *outputs* only), signing, live write ports |
| `apps/api` | FastAPI, pydantic DTOs, auth, CSRF, error envelope, OpenAPI conformance | all packages, adapters (composition root) | `apps/worker`, experiment runner, signing adapter |
| `apps/worker` | Job runner, outbox relay, ingestion, model-gateway process, release controller process (separate entry points and credentials) | all packages, adapters | FastAPI or pydantic web layer; `improvement.assessment` |
| `apps/web` | TypeScript/React client; types generated from the project OpenAPI | nothing from Python | money arithmetic for authority; it displays decimal strings only |

Further rules:
- Pydantic and FastAPI stay in `apps/api`. Domain types are frozen dataclasses, and DTOs map to them at the
  edge. The core never imports a provider SDK, `psycopg`, `httpx` or `fastapi`.
- `ExperimentAssessor` and the protected test manifests are read-only inputs to candidates. A candidate is
  an artifact (hash, package, config), never a module that the live process imports.
- Outbound HTTP (evidence retrieval, provider calls, webhooks) goes only through `adapters/http`. It
  allows `https` only (plus explicit provider hosts), resolves DNS once and pins the address, and blocks
  private, loopback, link-local, metadata and other non-public ranges after every redirect. It caps
  redirects, size, time and decompression ratio, and checks the MIME type. Hostile files (CSV, XLSX,
  archives) are parsed only in `portfolio.imports` and `evidence` parsers that receive bytes from the
  quarantine store, never from a URL or filesystem path. They neutralise spreadsheet formulas on export
  and apply the archive rules of C-10 (F-25).
- **Recorded deviation:** `strategies` imports `portfolio`. This is limited to the read-only snapshot and
  commitment-query **port protocols**, which T009 places in `portfolio.ports`. Importing any `portfolio`
  write service or repository fails the boundary check. Strategy output reaches the journal or
  commitments only through `ProposalPublisher` in the API/worker composition root.
- Floats never cross from `quant` into `Money`/`Quantity`. A conversion back to `Decimal` happens only in a
  named function with a declared quantum, and that function is tested.

## 3. Contract gaps and resolutions

Severity: **B** = blocks the dependent task until resolved; **S** = resolve in the owning task; **N** = note.

### 3.1 Cross-cutting

| ID | Sev | Gap in contracts 0.1.0 | Resolution (project contract 0.2.0) | Owner |
|---|---|---|---|---|
| C-01 | B | `Problem` has `trace_id` and `reasons` but not the retryability, correlation id or field errors that spec §3 requires, and no version. Every operation has only a `default` error response. | Versioned RFC 9457 envelope: `type` (URI `/problems/{code}`), `title`, `status`, `code`, `detail`, `correlation_id` (replaces `trace_id`), `retryable: bool`, `retry_after_s: int\|null`, `field_errors: [{pointer (RFC 6901), code, message}]` (messages never echo secret values), `reasons`, `error_schema: "1"`. List explicit statuses: 400 malformed, 401, 404 (also cross-tenant), 409 revision/idempotency conflict, 412 If-Match mismatch, 422 domain-invalid (finance-invalid but schema-valid), 428 missing If-Match, 429 rate or budget, 503 dependency. | T010 |
| C-02 | S | `Page` has no coverage metadata or sort order, though spec §3 requires both. Cursor binding is unspecified. | `{items, next_cursor, as_of, coverage: {status: complete\|partial\|unknown, reasons}}`. Each endpoint fixes its sort key, e.g. `(effective_at, id)` for the ledger. The cursor is opaque and HMAC-bound to tenant, principal, route and filters, and it expires. A foreign or tampered cursor returns 400 `invalid_cursor` and never data. Default `limit` 50; max 200 is kept. | T010 |
| C-03 | B | `Idempotency-Key` is only `minLength: 1`. The README scopes it by principal, route and payload hash, while spec §3 scopes job keys by tenant, operation and input revision. These are two different keys. | **HTTP layer:** key is 16–128 chars from `[A-Za-z0-9_-]`. The record is keyed on `(tenant, principal, method, route template + path params, key)` and stores the request hash and the first response. Same key with a different hash returns 409 `idempotency_key_reused`. Same key while the first request is in flight returns 409 with `retryable: true`. A replay returns the original status and body. Retention is at least 24 h (A-05). **Job layer:** the server derives the key from `(tenant, operation, input revision)` and never takes it from the client. A retry never resets trigger time or budget reservation. | T010, T025 |
| C-04 | S | Revisions appear twice: in the `If-Match` header and in the body (`RevisionAction.expected_revision`). Lower bounds differ (`CommitImport` ≥ 0, others ≥ 1). `pause` and `rollback` have no `If-Match`. | `If-Match` carries the strong ETag `"r<revision>"` of the **target** resource. A body `expected_*_revision` is only for **other** resources (an account in import commit or proposal selection). Where both name the same resource they must match, otherwise 400. `pause` intentionally omits If-Match so a user can always stop risk. `resume` and `rollback` require it; add it to rollback. All revisions are ≥ 1, and new accounts start at revision 1. Integers stay below 2^53 for JS safety. | T010, T026 |
| C-05 | B | `format: date-time` is not enforced here. The installed jsonschema 4.26.0 has no RFC 3339 checker, so `"2026-10-08T10:00:00"` and `"not a date"` both validate (observed by `tools/check_contracts.py`). AT080's negative case is therefore unproven. Two layers. (1) `Timestamp` gets a range-limited pattern: `^\d{4}-(0[1-9]\|1[0-2])-(0[1-9]\|[12]\d\|3[01])T([01]\d\|2[0-3]):[0-5]\d:[0-5]\d(\.\d{1,6})?(Z\|\+([01]\d\|2[0-3]):[0-5]\d\|-(?!00:00)([01]\d\|2[0-3]):[0-5]\d)$`. It rejects a missing offset, `-00:00` (unknown offset), month 13, hour ≥ 24, minute or second ≥ 60 (so leap second `60` too), offsets such as `+25:99`, and more than 6 fractional digits (PostgreSQL `timestamptz` has microsecond resolution, so silent truncation could reorder events). It does **not** reject impossible calendar dates such as `2026-02-30`. (2) A **semantic validator is therefore required**: the API parses every timestamp into an aware `datetime`, rejects failures with 400, and normalises to UTC. The server emits `Z`. T009 must also install a date-time format checker, and validators fail closed if `date-time` is not registered. `tools/check_contracts.py` applies both layers to the examples. | T009, T011 |
| C-06 | B | The decimal pattern allows `-0`, unlimited scale within 60 chars, and zero for live quantities. The data dictionary defers precision and scale to "T008/T012". | Typed decimal classes in §3.2 replace the two generic patterns. Out-of-scale input is rejected with 422 `decimal_scale_exceeded` and never rounded silently. | T012 |
| C-07 | S | The API `Id` is any 1–128 char token while the data dictionary says `uuid pk`. `tenant_id`/`owner_tenant_id` appear in response schemas. | Tenant-owned object ids are lowercase canonical UUIDs. Codes (reason codes, formula ids, catalogue ids) keep `Id`. No request body accepts a tenant id (true today; keep it as a contract test). Responses may omit tenant ids. Each `{x}_id` path parameter is resolved inside the session tenant only. | T010 |
| C-08 | B | R080 requires "UTC with exchange calendar identity". Only `feed_spec.session_calendar_id` has a calendar. `observation`, `option_terms.expiry` (bare date), intraday `proposal.trigger_at` and `instrument` have none. | Add `calendar_id` + `session_date` to observations, intraday triggers and simulated fills. `option_terms` gets `expiry_session: {calendar_id, session_date, exercise_cutoff_local}`. `instrument` gets `calendar_id` and the listing MIC. Calendar versions are pinned through `feed_spec`. Local session meaning is never inferred from UTC. | T011 |

### 3.2 Decimal policy per field type (resolves C-06)

Wire form: a JSON string with no exponent and no `+`. For a class with at most *I* integer digits and
scale *S*, a signed value must match `^(?!-0(\.0+)?$)-?(0|[1-9]\d{0,I-1})(\.\d{1,S})?$` (the lookahead
rejects `-0`, `-0.0`, `-0.00` …). A positive-only class must match `^(?!0(\.0+)?$)(0|[1-9]\d{0,I-1})(\.\d{1,S})?$`.
Example for `MoneyAmount` (I = 26, S = 12): `^(?!-0(\.0+)?$)-?(0|[1-9]\d{0,25})(\.\d{1,12})?$`.
More than *S* fractional digits is rejected even when the extra digits are zeros, so the bound stays
purely syntactic. The domain constructor repeats the same bounds as a validator rule (`Decimal` finite,
`-as_tuple().exponent ≤ S`, adjusted exponent < I, not negative zero), because schema validation is
optional for some callers. Storage is PostgreSQL `NUMERIC(p,s)`. Domain values
are `decimal.Decimal` under the ADR-013 context. Trailing zeros carry no meaning, and outputs are
normalised to at most the class scale without rounding.

| Class | Used for | Max int digits / scale | Storage | Rounding at explicit boundaries |
|---|---|---|---|---|
| `MoneyAmount` | postings, cash, cost, fees, proceeds, commitments | 26 / 12 | `NUMERIC(38,12)` | To the currency minor unit (versioned ISO 4217 table) only where a broker or reporting rule requires it. Planned costs and commitments round **up**, planned proceeds round **down** (conservative), display rounds half-even |
| `Price` | per-unit price, strike, limit/stop | 26 / 12 | `NUMERIC(38,12)` | Simulator fills use the instrument tick; never rounded otherwise |
| `Quantity` | units, lot sizes, deliverable quantities | 26 / 12 | `NUMERIC(38,12)` | Sized quantities round **down** to the lot increment (spec §5) |
| `PositiveQuantity` | live/sim order and proposal sized legs, execution reports | as `Quantity`, `> 0` | same | as above. Zero is rejected (closes the `"0"` gap in active proposals and execution reports) |
| `Multiplier` | option multiplier, split ratio | 12 / 12, `> 0` | `NUMERIC(24,12)` | never |
| `FxRate` | `fx_to_reporting` | 12 / 18, `> 0` | `NUMERIC(30,18)` | never |
| `Ratio` | returns, weights, limits as fractions, drawdown | 12 / 18 | `NUMERIC(30,18)` | Display only. Statistical floats stay in `quant` and are not wire money |
| `UsdBudget` | budgets, model cost | 14 / 6 | `NUMERIC(20,6)` | Reservation rounds up |

Money is always `{amount, currency}`. A bare amount appears only where the currency is fixed by the
schema (`budget_reservation.currency: USD`). `commodity` in postings is typed as an ISO currency or
`instrument:<uuid>`.

### 3.3 Operation and schema findings

| ID | Sev | Finding | Resolution | Owner |
|---|---|---|---|---|
| C-09 | B (security) | `ConnectionAuth.return_path` pattern `^/` accepts `//evil.example/x`, a protocol-relative open redirect after the OAuth return. | Pattern `^/(?![/\\])[A-Za-z0-9/_.-]*$` plus a server-side allowlist of return routes. | T010, T016 |
| C-10 | B (R087) | `ManifestFile.path` (export/import manifest) is unconstrained, so `../`, absolute paths, backslashes and NUL pass the schema. | Pattern for relative POSIX paths with no `..` segment, no leading `/`, no `\`, no NUL or control characters, ≤ 255 bytes. The importer additionally rejects symlinks, hard links, duplicate or case-folded collisions, device files, executable bits, oversize members and compression ratios above a bound. Schema checks alone are not sufficient. | T057, T058 |
| C-11 | S | `GET /health` inherits the session-cookie requirement, so liveness probes need a session. | `/health/live` is unauthenticated and returns only `{status}`. Component detail stays authenticated and operator-only. | T060 |
| C-12 | B (R081) | `ledger_event.postings` has `minItems: 2`, which forces split, symbol-change and unit-only events to invent money postings. `instrument_movements` is single-sided, so unit balance per commodity cannot be checked. Align with spec §4, which asks for a **separate balanced commodity/unit journal**. Keep two posting sets on one event: `money_postings: [{book_account, currency, amount: MoneyAmount}]` and `unit_postings: [{unit_account, instrument_id, quantity: Quantity, lot_id\|null}]`. Each set is either empty or has ≥ 2 lines. At least one set is non-empty. Money sums to zero per currency (F-01), and units sum to zero per instrument (F-02), using an explicit offsetting unit account (`position`, `unit_clearing`, `corporate_action_clearing`). A split then has only unit postings, and a deposit has only money postings. The two journals stay in separate tables (`posting`, `unit_posting`) linked by event id, so no query can add shares to dollars. Add a typed `book_account` enum from spec §4 (cash, security_cost, external_capital, realized_pl, income, expense, transfer_clearing). | T012 |
| C-13 | S (R071) | `ledger_event.status` includes `observed`/`conflicted`, which mixes source observation, reconciliation and posted event. Event kinds miss symbol change, merger, spinoff, dividend entitlement versus payment, and return of capital. | Journal status is `accepted\|reversed` only. Observations and conflicts live on `source_record` and `reconciliation_issue`. Add the missing kinds. A kind with unknown terms is quarantined and never posted. Add `source_record_ids` and `recorded_at` (server-assigned knowledge time). | T012, T017 |
| C-14 | S | `account_snapshot` has no cash breakdown (settled, unsettled, encumbered, planned commitments, collateral), no per-currency timestamps and no valuation coverage, all of which spec §4 requires. | Add `cash: [{currency, settled, unsettled, broker_reported_available\|null, external_encumbrances, internal_commitments, as_of}]` and `valuation_coverage {valued_ids, unvalued_ids, outside_context}`. NAV comes from calculation receipts and is never stored as a snapshot field. | T017, T018 |
| C-15 | S | `calculation` lacks the as-of times and input coverage required by spec §5 ("value, units, input coverage, formula/version, as-of times, warnings"). Inputs can only be decimals, so MWR dates cannot be represented. `result` nullability is not tied to status. | Add `as_of`, `inputs[].as_of`, `coverage`, typed `inputs[].kind` (decimal, date, money). Conditional: `status = ok` ⇔ `result` non-null. | T018 |
| C-16 | B (R084) | `VirtualOrderRequest.submitted_at` is client-supplied, which allows backdated simulated orders and look-ahead in shadow scorecards. `limit_price`/`stop_price` are not required by `order_style`. | The server assigns `submitted_at` (wall clock for manual mode, the simulation clock for replay). Fills must satisfy `filled_at > submitted_at` and use only data with `received_at ≤ fill decision time`. Add if/then rules: limit ⇒ limit_price, stop ⇒ stop_price, stop_limit ⇒ both. | T041 |
| C-17 | S (R088) | `job.state` lacks `retry_scheduled` and `dead_lettered` and exposes no `next_attempt_at`/`max_attempts`. Spec §3 requires an explicit dead-letter state. | Add these states and fields. A dead-lettered job is retried only by operator action under a new attempt id, keeping the original idempotency key. | T025 |
| C-18 | S | `POST /simulations` returns `201` with a `job`. There is no `GET /simulations/{id}`, `GET /policies`, `GET /connections/{id}` or `GET /releases/{id}`. | Return 202 + job, or 201 + `SimulationSummary`. Add the missing reads in their owning tasks (T041, T019, T016, T051). | Owners |
| C-19 | S | `budget_reservation.period` `YYYY-MM` has no time zone. Spend above the reservation (`actual_amount > reserved_amount`) is unspecified. | Period boundaries are UTC unless the installation policy names an IANA zone (A-06). An overrun is recorded, counts against the cap and blocks further dispatch in that category until reconciled. | T037 |
| C-20 | B (R099) | `policy.accepted_by` is a free string. `AdoptVersion`/`RevisionAction.acknowledgement` free text is the only signal of consent. `promotion_receipt.approved_by` is a bare `Id` and `signature_reference` names neither key nor algorithm. | Approvals are typed receipts `{kind: maintainer_release\|operator_install\|user_strategy_adoption\|policy_adoption, principal_id, object_hash, step_up_receipt_id, signed_at, key_id, algorithm, signature}`. The server derives `kind` and checks role. Acknowledgement text is recorded for UX but never authorises. LLM or agent output cannot produce a receipt (no credential path, §5). | T019, T051 |
| C-21 | S | `experiment.state` (proposed…cancelled) differs from spec §11 (drafted, assessed, insufficient_evidence, release_approved, installed, adoption_pending, active, suspended…). Six experiment targets map to four `change_kind`s, and `evaluator`/`research`/`personal_adaptation` are unmapped. | Adopt the spec §11 state list. Map research and personal_adaptation to `configuration` (user adoption when private), strategy to `strategy` (user adoption required), evaluator to a new `evaluator` kind (maintainer release, protected assessor unchanged), application_code to `application`, recursive_optimizer to `optimizer`. | T044, T051 |
| C-22 | S (R065) | `strategy_version.evidence_status` is a free string and `action_eligible` a stored boolean. `approved_by` is nullable even when eligible. | Replace both with the `Qualification` triple. Eligibility is computed per user and policy at final check and never stored as a global flag. | T027 |
| C-23 | S | `review.model_family_distinct` is self-declared. | The server derives it from `model_run` records. `reviewer_run_id ≠ researcher_run_id` and separate input manifests are checked in the domain (F-17). | T039 |
| C-24 | S | `option_terms` lacks `terms_version`, which the data dictionary has. `deliverables[].quantity` may be negative, and `GET …/option-terms` returns a single unversioned object. | Add `terms_version`, positive deliverable quantities, and cash deliverables as `Money`. The read takes `as_of` and returns the version in force. `adjusted ∧ ¬terms_verified` makes live sizing unavailable (F-14). | T034 |
| C-25 | S | `ExecutionReportRequest` repeats `account_id` from the path and allows quantity `0`. `ConnectionAuth.connection_id` and `PromoteCandidate.candidate_id` repeat path ids. | Remove the body copies or require equality (400 `path_body_mismatch`). Quantity is `PositiveQuantity`. Price currency must equal instrument currency. | T010, T017 |
| C-26 | N | `/chat/research` and `/research` share one schema. | Same budget reservation, evaluator and tool registry. `origin` is the only difference. | T040 |
| C-27 | N | `instrument.symbol` is ambiguous over time. | Response symbol is "alias as of `as_of`" from `instrument_alias`. History queries use alias validity windows. | T011 |

## 4. Financial and authority invariants that schemas cannot express

Each invariant becomes a domain test in the named task, using synthetic fixtures labelled SYNTHETIC and
independent oracles (`numerical_oracles.json` NUM01–NUM07 or hand-computed values), with property tests
under Hypothesis using fixed seeds.

| ID | Invariant | Enforcing test (task) |
|---|---|---|
| F-01 | For every event and every currency, money postings sum to 0. Currencies are never netted together. | Property test over random event sequences (T012) |
| F-02 | Unit postings sum to 0 per instrument commodity. A split changes units and cost per unit but preserves total cost and value (NUM02: 6 → 12 units, 25.05/unit, value 360). | Property + oracle (T012, T018) |
| F-03 | Acquisition fees are capitalised into lot cost and disposition fees reduce proceeds, never both. NUM01: cash 738, remaining cost 300.60, realised 38.60, unrealised 59.40, NAV 1098. | Oracle (T012, T018) |
| F-04 | Re-importing the same file, or an overlapping view of the same account, leaves holdings and cash unchanged. Two genuine accounts add (overlapping fixture: 100, not 150 or 50). | Idempotency property (T013, T014, T017) |
| F-05 | A correction posts a reversal plus a superseding event. The original event and its `recorded_at` survive. A proposal binds the snapshot that was current at its decision time. | Replay test (T012, T017, T026) |
| F-06 | Unknown cost or an unknown mark gives `Unavailable`, never 0. Partial NAV is labelled partial. | Unit tests (T018) |
| F-07 | Chained TWR is exact only with per-flow valuations (NUM03: 21%, not 131%). Modified Dietz is labelled approximate. A non-positive denominator gives `Unavailable`. MWR reports ambiguity (NUM05: roots 0.10 and 0.20). | Oracle (T018) |
| F-08 | FX: (1+r_l)(1+r_fx) − 1 with an explicit interaction term (NUM04: 0.155, interaction 0.005). | Oracle (T018, T023) |
| F-09 | Active planned commitments per (account, currency) never exceed qualified available cash. Alternatives reserve the maximum, joint sets reserve the sum (NUM06). Concurrent publishes cannot both succeed. | Concurrency test against real PostgreSQL with production-like roles (T017, T026) |
| F-10 | Final publication succeeds only if all `VersionBinding` revisions are still current under locks taken in deterministic order. Otherwise it returns a 409 conflict and a bounded re-evaluation follows. | Integration (T026) |
| F-11 | Sized quantity = floor to lot increment of min(cash, concentration, planned-loss capacities). A non-positive or unknown per-unit loss means abstain, never divide (NUM07). | Oracle (T020) |
| F-12 | Active `buy/sell/reduce` legs have quantity > 0 and within the computed capacity. Sells never exceed reconciled units unless a covered or permitted option structure allows it. | Unit (T020, T026) |
| F-13 | Loss pauses use unitised drawdown. Deposits do not clear a breach, withdrawals do not cause one, and midnight does not reset one. | Unit (T020) |
| F-14 | Adjusted or unverified option terms are never used for live sizing. There is no default multiplier of 100. Covered-call and cash-secured-put collateral is checked combined, before and after. | Unit (T034, T020) |
| F-15 | `trigger_at ≤ received_at ≤ created_at < expires_at`. An expired proposal can never be published. | Unit (T026) |
| F-16 | No market data with `received_at`/`publication_at` after decision time enters a decision, backtest or fill. Universes are point-in-time and include delistings (R084). | Leakage tests (T032, T033, T041) |
| F-17 | Reviewer run ≠ researcher run with separate context manifests. One bounded revision. Prompt injection cannot change the final validator outcome (R086). | Adversarial (T039, T020) |
| F-18 | reserved + spent ≤ cap per period and category under concurrency. Retries never reset reservations or triggers. | Concurrency (T037, T025) |
| F-19 | A worker crash after commit does not duplicate holdings or actionable alerts (AT088). Consumers deduplicate by event id. | Fault-injection integration (T025) |
| F-20 | No cross-tenant read, write, FK, cursor, job or cache hit. Tests use non-owner roles without BYPASSRLS (R075). | Integration (T010, T062) |
| F-21 | Installed artifact hash = assessed hash = approved hash. The approver is a human principal with the right role and step-up. The candidate cannot change the assessor or test manifests. | Integration (T044, T051) |
| F-22 | Simulation ledgers never contribute to real cash, exposure or performance. | Unit (T041) |
| F-23 | Import preview: accepted + rejected + duplicate = row_count. A preview never mutates the journal. | Unit (T014) |
| F-24 | Deletion tombstones are reapplied before serving after restore. | Drill (T058, T063) |
| F-25 | Untrusted inputs never widen access (spec §12, untrusted input controls). Fetches to private, loopback, link-local or metadata addresses, through DNS rebinding or redirects, or to disallowed schemes are refused. Oversize, MIME-mismatched and decompression-bomb content is refused. CSV/XLSX import never evaluates formulas, and exports neutralise them while keeping canonical raw values. Archive import rejects traversal, absolute paths, symlinks, duplicates and executables (R087). | Adversarial tests: SSRF suite in T022 and T038 (retrieval), hostile CSV in T014, archive import in T057, all re-run in T062 |

## 5. Protected control plane

Three authority paths share contracts and code. Each runs as its own process with its own database role
and its own credentials.

| Path | Processes | DB role (all NOLOGIN-inherited, non-owner, no SUPERUSER/BYPASSRLS) | Credentials it holds | It can never |
|---|---|---|---|---|
| Live | `api`, `worker`, `ingest`, `model-gateway`, `outbox-relay` | `qw_app` (DML on tenant tables under RLS; INSERT-only on `promotion_request`), `qw_worker`, `qw_ingest` (INSERT on observations/source records only) | Session signing secret; vault references for read-only broker/data connectors; model API keys only inside `model-gateway` | Write release, assessment or evaluation tables; read signing keys; call a broker mutation (none exists) |
| Experiment | `experiment-runner` (sandboxed per T007), `assessor` (outside the sandbox) | Runner: **no DB login**, receives immutable snapshot files; `qw_assessor`: INSERT `evaluation_receipt`, SELECT the protected manifests | Runner: a budget-scoped token to `model-gateway` only, no network otherwise; assessor: none beyond its role | Reach production secrets, the Docker socket, signing keys or live tables; edit the assessor or protected tests |
| Release | `release-controller` | `qw_release`: SELECT `promotion_request`, `approval_receipt`, `evaluation_receipt`; INSERT/UPDATE `release_receipt`, `installation_state`, `adoption_receipt` | KMS/offline signing via a `Signer` adapter (key never in process memory where KMS allows) | Infer approval from text or model output; sign without a valid human approval receipt |
| Migration | one-shot `migrate` job | `qw_migrate` owns the schema and is used only at deploy time | DB owner password, operator-held | Run at application runtime |

Human roles: `tenant_owner`, `tenant_member`, `installation_operator`, `maintainer` (hosted release),
`support` (audited exceptional access). Agents and models hold no role and no path to an approval receipt.
`POST /experiments/{id}/promote` in `apps/api` only records a *request* plus the step-up receipt. The
release controller verifies role, hashes and signature. A compromised API process therefore cannot
promote. Assignment of maintainer and operator roles happens outside the request path (no
self-assignment, per the data dictionary).

## 6. Open assumptions

| ID | Assumption | Owner task |
|---|---|---|
| A-01 | The project-owned contract lives at `contracts/` (repo root), versioned 0.2.0 and derived from `docs/spec/contracts` 0.1.0 with C-01…C-27 applied. `docs/spec` stays immutable. | T009 (create), T010 (bind) |
| A-02 | The scales in §3.2 cover fractional-share precision of the named brokers. Confirm against authorised samples. | T005, T012 |
| A-03 | The ISO 4217 minor-unit table and calendar sources need versioning and licence review. | T011, T021 |
| A-04 | The OpenAPI type generator for `apps/web` and the contract-conformance tool are chosen during dependency review. | T009 |
| A-05 | HTTP idempotency retention is 24 h, and job idempotency lasts the life of the input revision. | T025 |
| A-06 | Budget periods are UTC calendar months unless the owner sets a zone. | T037, owner |
| A-07 | KMS or offline signing differs between hosted and self-hosted. Key custody is an owner decision. | T051, owner |
| A-08 | The sandbox runtime is unqualified here (`runsc` absent, T001), so the experiment path stays blocked. | T007 |
| A-09 | psycopg (LGPL-3.0) is acceptable as an unmodified dependency with notices. Needs a legal check (QUAL23). | T062, T065 |
| A-10 | Cross-tenant 404 also applies to support access; support uses an audited separate route. | T010, T059 |
