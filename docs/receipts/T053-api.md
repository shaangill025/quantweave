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
