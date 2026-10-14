# Task receipt — T053 (API read routes, project contract 0.2.0, UI null handling)

Status: In progress. This increment adds the read-only API routes the T053 UI needs, the owner-approved project contract amendment, and the UI changes that follow from it. T053 backlog status is not changed.

## Scope and authority
- Task: T053, "Dashboard and policy/reconciliation UI". Requirement IDs touched: R042 (unknown is never zero), R043 (cash per account and currency) and R082 (provenance and coverage). The stores come from T012, T019 and T026.
- Repository `/home/user/quantweave`, branch `api-accounts-proposals-routes`, fast-forwarded to `a382258` (main, including the T053 UI). Not committed.
- Operations: read-only routes, two read-only store functions, one pure domain function, the project contract and UI display changes. No table, migration, dependency or write path. No broker, provider or network access.
- Spec contract: `docs/spec/contracts/openapi.yaml` operations `listAccounts`, `readSnapshot` and `listProposals`. The routes conform to the project contract 0.2.0 below. `docs/spec` is not edited.

## Owner decision (2026-10-14): project contract 0.2.0 (T008 A-01)
The first version of these routes returned null for fields no store holds. That was schema-non-conforming, and the T053 UI guards refused it (`invalid_response`). The owner decided to amend the project-owned contract rather than invent values.

- **Where it lives.** A-01 puts the project contract at `contracts/`, version 0.2.0, derived from `docs/spec/contracts` 0.1.0. This increment creates it.
  - `contracts/amendments.json` lists the amendments.
  - `tools/project_contract.py` derives each amended schema in memory: `schema(name)` for an item and `page(name)` for a page.
  - A schema that `amendments.json` does not name is the spec schema, unchanged.
  - The C-01…C-27 items of T008 not yet bound are still open; only these amendments exist.
- **Changes:**
  - `account`: `name`, `broker`, `account_type`, `reporting_currency`, `analysis_scope` and `status` are nullable.
  - `account_snapshot`: `reconciliation`, `available_cash` and `unresolved_ids` are nullable. It gains `ledger_cash` (trade-date journal cash, not available cash) and `coverage`.
  - `proposal`: these are nullable:
    - `sleeve_id`, `origin` and `horizon`;
    - every `binding` id;
    - `conditions` and `invalidation_conditions`;
    - `claim_ids`, `evidence_ids` and `calculation_ids`;
    - `qualification`.

    The active-state if/then also accepts a null `final_check_receipt_id` or `qualification`.
  - Pages (`accountPage`, `proposalPage`) gain the C-02 `coverage`.
- **Cross-field rule.** JSON Schema cannot express it, so consumers enforce it: every null in an amended field is named in that response's `coverage.reasons`. The reason forms are:
  - `unavailable:<field>` or `unavailable:<id>/<field>`: no store holds the value.
  - `unknown:<id>/<field>`: the stored value is outside the contract.
  - `none:<id>/<field>`: the value is known to be absent, e.g. no sleeve.

  An active proposal may have a null receipt or qualification only when coverage names it, and it is then never actionable.

## Changes (15 files)
| File | Change |
|---|---|
| `apps/api/src/qw_api/reads.py` (new) | The three routes. Membership `Reader` and session tenant under RLS (as `/policies`), problem+json errors, nothing written. C-02 keyset cursor: (expiry, key) with an HMAC tag over tenant, principal, route, sort key and body, valid for 1 h. Cursor limit 256, so 128-character ids fit. An empty `cursor=` is treated as absent, as `/policies` does. |
| `apps/api/src/qw_api/app.py` | Includes the router. |
| `packages/adapters/src/qw_adapters/journal_store.py` | `list_accounts`. |
| `packages/adapters/src/qw_adapters/proposal_store.py` | `list_proposals`. |
| `packages/domain/src/qw_domain/proposals.py` | `resolve_recorded`: read-time state from stored records only. It applies expiry, input freshness, the adopted policy hash and account revisions, as `resolve` does, and records nothing. |
| `contracts/amendments.json` (new), `tools/project_contract.py` (new) | Project contract 0.2.0. |
| `apps/web/src/api.ts` | Types and guards accept null in exactly the amended fields the views read. Any such null without a coverage reason is refused as `invalid_response`. Adds `ledger_cash` and snapshot `coverage` (decimal strings checked) and `actionable()`. |
| `apps/web/src/views.tsx` | Each null is shown as `Unavailable (<coverage reason>)`, never blank or 0. Accounts with undeclared scope get their own table, apart from real and hypothetical accounts. Ledger cash has its own table, captioned "Ledger cash (trade-date, not available cash)". An active proposal without a receipt or qualification is tagged "Not actionable: final check or qualification unavailable", and Overview lists it. |
| `apps/api/tests/test_reads_api.py` (new), `apps/api/tests/test_api.py`, `packages/domain/tests/test_proposals.py`, `apps/web/src/api.test.ts`, `apps/web/src/views.test.tsx` | Tests. The existing active fixture gains the final-check receipt the contract requires. |

Design (unchanged from the first review except where marked):
- **Accounts** are the distinct `account_id`s in `app.ledger_event`.
  - `revision` = 1 + latest `account_rev`.
  - `source_ids` are the sources that fed the account: journal events ∪ `app.source_record`.
  - `cash_basis` is `unknown`.
- **Snapshot**: `load_journal` and then `materialize`.
  - A known cost is shown with `lot_coverage: complete`. An unknown cost is null with `lot_coverage: unknown` and a reason, never 0.
  - `available_cash` is null: settlement terms and open orders are not persisted (T017).
  - `freshness_status` is `unknown`. `reconciliation` and `unresolved_ids` are null with reasons.
  - Another tenant's account and an unknown account get the same 404.
- **Proposals**:
  - The state is resolved at read time from the DB clock. Live states carry `RISK_NOT_RECHECKED`, because risk inputs and pauses are not persisted.
  - A buy's cash effect is minus the bound requirement.
  - `policy_version_id` = `<policy_id>:<version>` of the bound policy hash.
  - Review nit: `horizon` is checked against the contract enum. A value outside it becomes null with `unknown:<id>/horizon`.
  - Review nit: `risk_summary` now comes from the stored binding: its risk outcome and digest prefix, marked as not re-evaluated at read time.
  - `sleeve_id: null` gets `none:<id>/sleeve_id`.
- **Query costs** (review nit; recorded, not fixed):
  - `list_accounts` groups the tenant's whole `ledger_event` set on every page. Each account adds two correlated scans (its events and its source records) for `source_ids`. The cost is O(tenant events + tenant source records) per page.
  - The snapshot replays the account's whole journal (O(account events)).
  - `GET /proposals` costs, per item: one version query and one event query (`load`), one policy load (versions and adoptions), and one revision query per bound account. That is about 4 + accounts queries per item, plus one `DISTINCT ON` scan of the tenant's proposal versions.
  - An account registry or a materialised per-account summary should replace the scan before tenants grow.

## Actual checks
| Command | Result |
|---|---|
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 204 files already formatted |
| `uv run mypy --strict` | no issues in 160 source files |
| `QW_REQUIRE_DB=1 uv run pytest -q` | 1795 passed, 0 skipped (2 third-party deprecation warnings), throwaway PostgreSQL 16 |
| `uv run python tools/check_no_float.py` | PASS |
| `uv run python tools/check_contracts.py` | PASS (checks the spec contracts only) |
| `uv run python tools/project_contract.py` | `project contract 0.2.0: OK` |
| `python3 tools/check_spec_artifacts.py` | PASS |
| `pnpm typecheck` / `pnpm lint` | exit 0 / exit 0 |
| `pnpm test` | 4 files, 168 tests passed |
| `pnpm build` | built |
| `pnpm run licenses` | PASS: no denied, unknown or unreviewed licences |

Tests (SYNTHETIC data only):
- **Hand-computed oracle**: deposit 10000 CAD and 300 USD, then buy 10 at 50.25 with a 4.95 fee. Cost is 507.45 CAD and ledger cash is 9492.55 CAD.
- **Strict validation**: every API response is validated against the amended schema, with no error and no extra field allowed. Every amended null must be named by a reason, and the set of nulls is asserted.
- **Negative controls**:
  - An unexplained null fails the strict check, and so does an extra field.
  - The spec 0.1.0 schema refuses the active example with a null qualification.
  - Replacing `resolve_recorded` with the recorded state made the proposals test fail. The mutation was reverted.
- **Web tests**:
  - A page with an unexplained null shows `Unavailable (invalid_response)`.
  - A per-id reason and an all-items reason are both accepted.
  - Ledger cash amounts must be decimal strings.
  - An active proposal without a receipt is refused unless coverage names it, and is then not actionable.
  - The rendered views show the coverage reason for each null and never show 0.

## Negative/adverse cases
- Tenant B sees empty account and proposal pages. Its request for tenant A's snapshot gets the same 404 as an unknown account.
- A forged, malformed, truncated or other-route cursor gets 400 `invalid_cursor`. An empty cursor returns the first page. A malformed id gets 400. No session gets 401.
- Reads write nothing: the `app.proposal_event` count is unchanged after reading an overdue proposal and an invalidated one.

## Unresolved gates
- No store exists yet for: an account registry, freshness policy, persisted reconciliation (T017), feed and entitlement bindings (T021), risk inputs and pauses (T020), reviews (T039 records are not linked) and qualification. Until each lands, its fields stay null with reasons.
- `tools/check_contracts.py` checks only `docs/spec/contracts`. The project-contract amendments add no number or money field, so its rules still hold.
- `GET /proposals/{id}`, the ledger and valuation are not done.

## Review and next handoff
Review: a reviewer running a different model from the implementer.
- Round 1: HOLD (owner decision). The routes had no defects, but the responses did not conform to the spec 0.1.0 schemas, and the T053 UI rejected them as `invalid_response`.
- Owner decision (2026-10-14): amend the project contract (option a).
- Round 2: APPROVE.
  - The amendment matches the decision and leaves `docs/spec` untouched.
  - The cross-field rule (every amended null is named in `coverage.reasons`) is enforced by the API tests and the UI guards, including for reasons that name another item.
  - An active proposal without a receipt or qualification is never actionable.
  - Decimal safety and the earlier cursor, tenant and no-write probes still hold.
  - `horizon` being nullable goes beyond the owner's literal list. The reviewer accepted it, since an unvalidated stored value would otherwise break the schema with no reason given.
- Follow-ups:
  - Reasons are page-wide for proposals.
  - Keep the `unavailable:` and `none:` meanings consistent in later routes.
  - The query costs above apply.
- Outcome: approved, as evidence, not authority.

## Reads increment 2 (T053-api, T012): `GET /proposals/{id}` and `GET /accounts/{id}/ledger`
Branch `t053-proposal-and-ledger-reads`, from `origin/main` c04763f. Requirements: openapi `readProposal` and `listLedger`, `proposal.schema.json` and `ledger_event.schema.json` (as amended), C-02 pages, T008 A-01 rule, spec §4 (corrections use reversals or superseding events), T012 ledger reads.

### Changes
- `apps/api/src/qw_api/reads.py`
  - `GET /proposals/{id}` reuses the `proposal(...)` view. The proposal is loaded with its policy history and account revisions at the database clock. One `_proposal` helper now serves both proposal routes, so the list and read bodies are identical. An unknown id and another tenant's id both return the same 404 (RLS).
  - `GET /accounts/{id}/ledger` returns a keyset page in `account_rev` order. It reuses `_page`, `_after` and the HMAC cursor. The cursor key is the revision and the MAC binds tenant, user, route path (so also the account) and sort key. `_page` gained an optional `last` key.
  - The ledger route loads the account's journal with `js.load_journal`, which verifies every event id. An empty journal is a 404. That covers an unknown account, another tenant's account, and a simulation id: the 0014 guard keeps simulation ids out of `ledger_event`.
- `packages/adapters/src/qw_adapters/proposal_store.py`: `policy_of(tx, pid)`, the latest version's policy id (read-only).
- `contracts/amendments.json` 0.4.0 (`"status": "proposed, pending owner confirmation"`) and `tools/project_contract.py` (version text only):
  - `proposal` gains `coverage`. readProposal returns one item, so the T008 A-01 rule needs the reasons on the item. List items carry it too, and the page coverage is unchanged. The web guard ignores extra fields, so the web needs no change.
  - `ledger_event.lot_policy_id` becomes nullable. The journal stores a lot-policy name (`economic_average_cost/1`), which fails the contract id pattern and is not a lot-policy record id. It is served in `economic_annotations` as `lot_policy:<name>`, and the null is named `unavailable:lot_policy_id`.
- `apps/api/tests/test_reads_api.py`: five new tests, and the strict-amendment test now requires proposal coverage.
- `apps/api/tests/test_api.py`: the operation count is 25 (main's 23 plus these 2 routes).
  - The branch started from c04763f. During review, main moved on with #93, #94 and #95, and the branch was fast-forwarded to origin/main 4634833 (`git merge --ff-only`; no merge commit, nothing committed).
  - Re-applying the work-in-progress conflicted on one line only, this count (main 23, branch 21). It was resolved to 25.
  - `app.py` keeps every router from main (capabilities, policies, reads, research, simulations, strategies).

### Ledger wire mapping (all values come from stored rows; nothing invented)
- `kind`:
  - The journal kinds deposit, withdrawal, buy, sell, fee, dividend, interest and split map to themselves.
  - `reversal` has no wire kind. It is served as `correction`, with `reverses_event_id` set, and named per item as `mapped:<id>/kind=reversal->correction`.
  - Any other unmapped kind raises (500); an event is never dropped. `KINDS` is an identity map on purpose, so a new `EventKind` fails closed.
  - The wire kinds `opening_balance`, `fx_conversion`, `transfer`, `exercise`, `assignment` and `expiry` have no journal kind yet, so they are never emitted.
- `status`:
  - `reversed` when a stored reversal names the event; otherwise `accepted`, meaning posted to the journal after the domain checks.
  - The store holds no observed-only or conflicted journal events: a conflict is refused, not stored.
  - No reconciliation record exists (T017), so every non-empty page names `unreconciled:status_is_journal_acceptance`.
- `postings`:
  - Money lines have `book_account` set to the C-12 account and `commodity` set to the currency. A line on an instrument sub-ledger uses `<account>:<instrument uuid>`.
  - Unit lines have `book_account` set to the unit account and `commodity` set to the instrument uuid.
  - Every amount is the stored decimal string, and lines are in stored line order. Amounts are never summed across commodities.
- `instrument_movements`: the `position` unit lines.
- `economic_annotations`: `fee:<amount> <currency>` when stored, `lot_policy:<name>`, and `supersedes:<id>` when stored.
- `idempotency_key`: the same encoding as the generated DB column (`record/<source>/<record>` or `reversal/<id>`). The test compares it with that column.
- `source_id`, `source_record_id`, `effective_at` and `recorded_at` are stored, so they are served as stored.

### Actual checks (2026-10-14, this worktree)
| Command | Result |
|---|---|
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 248 files already formatted |
| `uv run mypy --strict` | no issues in 193 source files |
| `QW_REQUIRE_DB=1 uv run pytest -q` | 2049 passed, 0 skipped (2 third-party deprecation warnings) |
| `QW_REQUIRE_DB=1 uv run pytest -q -m db` | 324 passed |
| `uv run python tools/check_no_float.py` | PASS: 0 problems in 95 files |
| `python3 tools/check_spec_artifacts.py` | PASS |
| `uv run python tools/project_contract.py` | `project contract 0.4.0: OK` |
| `uv run python tools/check_contracts.py` / `--self-test` | PASS (63 paths, 26 schemas) / 16/16 |

pnpm checks were not run, because no web file changed. This table was re-run after the fast-forward to origin/main 4634833: see "Review round 1" below.

Tests (SYNTHETIC data only; journal dates are relative to the database clock):
- **Ledger oracle**:
  - The journal is: deposit 10000 CAD; deposit 300 USD; buy 10 at 50.25 with a 4.95 fee (cost 507.45); sell 4 at 60 with a 1.00 fee; then the USD deposit corrected to 250.
  - Sell, hand-computed: remaining cost 507.45 × 6/10 = 304.47, allocated 202.98, net 239, realised 36.02.
  - The postings are asserted line by line.
  - Each event sums to zero per commodity (Decimal).
  - Cash sums to 9731.55 CAD and 250 USD.
- **Paging**:
  - Limits 1, 2, 4, 5 and 6 each return the same 6 events once, in revision order.
  - A walk that does not advance fails.
- **Refusals**:
  - A cursor for another account, for `/accounts`, or used by another tenant's user gets 400 `invalid_cursor`.
  - A simulation id (`SIM-LED`, saved through `simulation_store`), an unknown account and another tenant's account all get the same 404.
  - `limit=0` and a malformed id get 400. No session gets 401.
- **Proposal reads**:
  - Each proposal's body equals its list item.
  - States are resolved at read time: candidate with `RISK_NOT_RECHECKED`, and expired.
  - The coverage names each unavailable field and `none:<id>/sleeve_id`. It contains `not_rechecked:` exactly when the state is live.
  - Another tenant's id gets the same 404 as an unknown id.
- **Unmapped kind**: with `KINDS` and `MAPPED` monkeypatched to omit `deposit`, the ledger returns 500 `internal_error`. The body names no kind, account, event id or exception type, and has no field errors or reasons.
- **Writes**: row counts of every `app` table are unchanged across all the reads.
- **Strict**: each ledger item and proposal body is validated against project contract 0.4.0, with date-time formats enforced. Every amended null is named.

### Mutants (reads.py, run against `test_reads_api.py`, each reverted)
Each mutant was run with `QW_REQUIRE_DB=1 uv run pytest -q -x apps/api/tests/test_reads_api.py` and reverted afterwards (`cmp` with the saved original).
- M1: status always `accepted`. Killed.
- M2: the `mapped:` reason for a reversal dropped. Killed.
- M3: the cursor key off by one (`start + len - 1`).
  - Killed. The first run hung in an endless walk.
  - The walk is now bounded and fails with "paging does not advance".
- M4: `more` computed with `<=`.
  - Survived at first: the last page carried a cursor to an empty page.
  - The test now refuses an empty page reached through a cursor. On re-run it was killed.
- M5: the sign of unit postings flipped. Killed.
- M6: `instrument_movements` include clearing lines. Killed.
- M7: readProposal with an unknown id not a 404. Killed.
- M8: proposal coverage drops `not_rechecked:`. Killed.
- M9: the cursor MAC not bound to the route. Killed.
- M10: the `unavailable:lot_policy_id` reason dropped. Killed.
- M11: the fee annotation dropped. Killed.
- M12: the reversal idempotency key encoded `reversal:` instead of `reversal/`. Killed.
- M13: an empty journal (simulation or unknown id) not a 404. Killed.
- M14: the instrument sub-ledger suffix dropped from money lines. Killed.
- M15: the cursor MAC not bound to tenant and user. Killed.
- Review round 1 (N2) added two more mutants on the `MAPPED` fallback:
  - M16: `MAPPED.get(kind, kind)`, a silent pass-through. Killed by the unmapped-kind test.
  - M17: `kind = e.kind.value`, no mapping at all. Killed by three tests.

### Unresolved gates and remaining work
- 0.4.0 needs owner confirmation, like 0.3.0.
- The ledger route replays the whole account journal on every page, O(account events): `load_journal` verifies replay and the `reversed` status needs later events. A paged store read with a materialised reversal index should replace it before accounts grow.
- There is no reconciliation store (T017), so `accepted` is journal acceptance, not reconciliation.
- Opening balances, FX conversions, transfers and option lifecycle events have no journal kind yet.
- A future web ledger view must show the page `coverage.reasons` next to each item's `status`. The caveat that `accepted` means journal acceptance, not reconciliation, is only stated at page level (`unreconciled:status_is_journal_acceptance`).
- Still to do in T053-api: valuation and the remaining contract reads.

### Review round 1
Outcome: APPROVE with nits, by a cross-model reviewer. N1 to N4 are applied below. This approval is evidence, not authority.
- N1: the remaining work now notes that a web ledger view must show the page coverage next to each item's status.
- N2: an unmapped-kind 500 test was added. M16 and M17 confirm that removing the `MAPPED` fallback fails a test.
- N3: a comment on `KINDS` says it is an identity map so that new kinds fail closed.
- N4: the branch was fast-forwarded to origin/main 4634833 (#93, #94, #95). The operation count was resolved to 25, every router in `app.py` was kept, and the conflict statement above was corrected.

Checks re-run on the fast-forwarded tree (2026-10-14):
| Command | Result |
|---|---|
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 258 files already formatted |
| `uv run mypy --strict` | no issues in 203 source files |
| `QW_REQUIRE_DB=1 uv run pytest -q` | 2115 passed, 0 skipped (2 third-party deprecation warnings) |
| `uv run python tools/check_no_float.py` | PASS: 0 problems in 100 files |
| `python3 tools/check_spec_artifacts.py` | PASS |
| `uv run python tools/project_contract.py` | `project contract 0.4.0: OK` |
| `uv run python tools/check_contracts.py` / `--self-test` | PASS / 16/16 |

Review: round 1 APPROVE with N1–N4 applied; verify pass (same reviewer, a different model from the implementer) APPROVE: only the 7 files differ from 4634833, every router is still included, an equivalent of M16 (unmapped kinds passed through) fails the new test, the test asserts a leak-free 500 body, and all eight checks re-run (2115 passed).
