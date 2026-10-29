# Task receipt — T055 (API: the read-only experiment routes)

Status: In progress. No route was added. `listExperiments` and `readExperiment` have been served since T044 increment 2 (#126, 2026-10-20). T055 increment 1 (#123, 2026-10-16) predates those routes, so its "apps/api serves no experiment route" line no longer held. This increment checks the served routes against the contract, adds the route-level test that was missing, and re-verifies the whole T055 gap table and status line against the served code. T055 backlog status is not changed.

## Scope and authority
- Task: T055 "Improvement and governance UI", the API reads behind its experiment view. Requirement IDs: R030, R092 and R099 (read side only).
- Base `78cf068` (origin/main). The worker did not commit.
- No dependency, migration, contract, openapi, schema or `docs/spec` change. No mutating route. Cancel, promote and rollback are not implemented. No broker, provider, network or billable call.
- Read: CLAUDE.md, docs/spec/AGENTS.md, docs/spec/CLAUDE.md, `openapi.yaml` (`/experiments`, `/experiments/{id}`), spec §13 "Improvement and governance", `apps/api/src/qw_api/experiments.py`, `policies.py`, `security.py`, `qw_adapters.experiment_store`, `0047_optimizer_store.sql`, `test_experiments_api.py`, `test_optimizer_store.py`, receipts T044, T053-api, T054-api and T055.

## What is served, checked against the contract (no code change)
| Contract point | Served by `qw_api.experiments` (T044 inc. 2, 0.6.0 and 0.11.0) |
|---|---|
| `GET /api/v1/experiments`: `cursor`, `limit` 1..200, tenant from the session | Yes. The same `Limit` and HMAC `Cursor` as `qw_api.reads` (C-02 keyset by `experiment_id`), bound to the principal and the route. Body `experimentPage` |
| `GET /api/v1/experiments/{id}` | Yes. 200 `experiment`. Unknown and foreign ids get the same 404 `not_found`; a malformed id gets 400 |
| Tenant scope | `experiment_store` reads `app.experiment_revision` in the session tenant's transaction as qw_app (forced RLS) and re-verifies every revision's hash chain. A failed check is 409 `integrity` |
| Fields the store does not hold | `evaluation_receipt_ids` `[]` and `release_receipt_id` null: neither has a store. `next_cycle_run_id` is null (no route has a lineage). `parent_optimizer_version` is null except for the optimizer target. `retest_of` is null when there is no link. Each null or absent field is named in the item's `coverage.reasons` (`none:` / `unavailable:`), never invented. The schema has no money or instant fields |
| Roles | `Reader` = `require_role(tenant_member)`: any member of the session tenant (`tenant_member` or `tenant_owner`). Spec §13 lets users inspect experiments and failures, and only protected actions need a role and step-up. The contract names no role for these reads. Tenant membership has only two roles and both may read, so no member role is forbidden. No session gives 401 `unauthenticated` |
| Read-only | POST, DELETE and PATCH on the collection, plus `/cancel` and `/promote`, are 403, 404 or 405. The revision count is unchanged after the reads (existing test) |

Installation-scope optimizer experiments (0047, owner decisions 87/90/91) are not served. They live in `app.optimizer_registry_entry`, `optimizer_cycle` and `optimizer_lineage`. qw_app holds no privilege on these tables (`test_optimizer_store.py::test_exact_privileges_hardened_guards_and_forbidden_logins`), and the routes read only `experiment_revision`. Tenant-scope experiments with target `recursive_optimizer` are tenant records and are served.

## Changes (3 files)
| File | Change |
|---|---|
| `apps/api/tests/test_experiments_api.py` | New DB test `test_installation_optimizer_experiments_never_reach_tenant_users` (SYNTHETIC, GATE-LIFTED fixtures). It logs the T050 optimizer registry (`exp-opt-1`, scope `installation`) through `optimizer_store` as qw_assessor, and confirms the row exists. For alice (`tenant_owner`), bob (`tenant_member`) and carol (tenant B), the list is an empty conforming page and the read is 404 `not_found`. A tenant record under the same id is then served to bob only as his tenant's own record (`scope` `tenant`, its `scope_id`), and stays a 404 for carol. With no session, both routes give 401 `unauthenticated` and the body does not name the id |
| `docs/receipts/T055.md` | Status line and API-gap table re-verified against origin/main `78cf068`: a dated note; every row now gives what is served (and by which PR) and what is still missing; the `listBudgets` contract row is corrected; and two older lines (the design note on `GET /experiments`, and the 0.6.0 "not served" line) are marked as of their date |
| `docs/receipts/T055-api.md` (new) | This receipt |

The tests that already covered the rest are unchanged (`test_experiments_api.py`):
- the happy paths, with each item validated against `experiment` and each page against `experimentPage`;
- one-per-page and limit-2 pagination, plus bad and foreign cursors;
- foreign-tenant 404s;
- a missing id, and the read-only methods;
- 409 for tampered data and 500 for a database failure;
- `retest_of` and `next_cycle_run_id`, with `experiment_recursive.valid.json` and the self-promotion invalid example.

## Actual checks (2026-10-28, Linux, Python 3.13 via uv, throwaway PostgreSQL 16 from the conftest harness)
| Command | Result |
|---|---|
| `QW_REQUIRE_DB=1 uv run pytest -q apps/api/tests/test_experiments_api.py` | 8 passed, 0 failed, 0 skipped (2 library deprecation warnings) |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | all files already formatted (373 files when re-run after the receipts were added) |
| `uv run mypy --strict` | no issues found in 305 source files |
| `python3 tools/check_no_float.py` | PASS: 0 problems in 141 files |
| `uv run python tools/check_contracts.py` | PASS: 63 paths, 37 mutating operations, 26 schemas, 7 examples, project contract 0.13.0 |
| `uv run python tools/check_contracts.py --self-test` | 195/195 defects detected |
| `python3 tools/check_spec_artifacts.py` | PASS |
| `python3 tools/pytest_shard.py --self-test` | PASS: 155 test files partition into 1 to 4 shards |
| Full suite as the 4 CI shards, `QW_REQUIRE_DB=1 uv run pytest -q <shard i of 4 files>`, i = 0..3 in parallel | shard 0: 1076 passed, 14 skipped; shard 1: 675 passed, 7 skipped; shard 2: 907 passed; shard 3: 968 passed. Total 3626 passed, 0 failed, 21 skipped. Each shard exited 0. The 21 skips are the gVisor tests in `test_gvisor_sandbox.py` and `test_code_stager.py` (`runsc_unavailable`: no runsc on this host). A skip is not a pass |

No mutants were run for the new test.

## Remaining gaps (not built here)
- Installation optimizer experiments have no tenant route, by design (decisions 87/90/91). An operator or maintainer read would need an installation role and session field, which do not exist (`routes.py`: `role_not_served`), and would need a contract operation.
- `evaluation_receipt_ids` stays empty and `release_receipt_id` stays null: no evaluation-receipt store, and no link from a release receipt to its experiment.
- `next_cycle_run_id` is null on every served item: the installation lineage store is not readable by qw_app.
- Cancel, promote and rollback (`cancelExperiment`, `promoteExperiment`, `rollbackRelease`) are mutating governance routes and are not served. They need their own increment.
- The re-verified T055 gap table lists what is still missing for each row: scorecard/verdict reads, promotion, install and adoption actions, rollback, the next-cycle run read, and the `listBudgets` route. The UI does not yet request the served release, deployment-event or approval-class reads.

## Review
Cross-model review by a subagent running a different model from the implementer: REQUEST_CHANGES, then fixed. The reviewer re-ran `test_experiments_api.py` (8 passed, 0 failed, 0 skipped) and `check_spec_artifacts.py` (PASS). It checked every gap-table claim against the code (served routes, null fields, `listBudgets` unserved, `release_store.install` without a non-test caller, `tenant_adoption`, deployment events, approval classes) and found them true. It confirmed the new test uses real PostgreSQL, non-owner logins and real 0047 rows, and would fail if the routes read the 0047 tables or served without a session; a grant change alone would not fail it (the grant is covered by `test_optimizer_store.py`). That is reasoning, not a mutation run. Findings, all in documentation, fixed before commit: this receipt understated the T055.md change; one sentence read as blame; two older T055.md lines were not marked as dated; and the format-check file count did not reproduce. A follow-up check of these fixes by the same reviewer: APPROVE.
