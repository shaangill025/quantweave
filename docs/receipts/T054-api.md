# Task receipt — T054 (API read routes for the research and simulation views)

Status: In progress. This increment persists calculation receipts and serves `GET /calculations/{calculation_id}`. The other routes the T054 views call have no store yet and are not served. T054 backlog status is not changed.

## Scope and authority
- Task: T054, "Research/options/simulation UI", increment 2 (backend reads). Requirement touched: R019 (calculation verification drilldown). Increment 1 (the views, `apps/web/src/sources.ts`) is a separate PR.
- Repository worktree `/home/user/qw-sim2`, branch `t054-research-read-routes`, baseline `006616d` (origin/main). Not committed.
- Operations: one migration (one append-only table), one adapter store, one read-only route. No dependency, no write route, no broker, provider or network access. `docs/spec` is not edited. No contract amendment: the body is the spec `calculation` schema as is.

## Which routes can be served honestly
| Route (openapi.yaml 1.0.0) | Contract | Backing data on main | Served here |
|---|---|---|---|
| `GET /calculations/{calculation_id}` (readCalculation) | `calculation` schema | `qw_domain.calculation.CalculationRecord` (T018), content-addressed, with `to_contract()`; no table before this increment | **Yes**, with the new `app.calculation_record` table |
| `GET /claims/{claim_id}` (readClaim) | `claim` | `qw_domain.evidence.EvidenceStore` (T036): in memory only ("LIMITATIONS: in-memory") | No |
| `GET /evidence/{evidence_id}` (readEvidence) | `evidence` | same in-memory `EvidenceStore` | No |
| `GET /instruments/{instrument_id}/option-terms` (readOptionTerms) | `option_terms` | `qw_domain.options` (T011/T034) values only; no instrument or terms registry | No |
| `GET /simulations` (listSimulations) | `SimulationSummaryPage` | `qw_domain.simulation` (T041) virtual portfolios in memory only | No |
| `GET /simulations/{simulation_id}/results` (readSimulationResults) | `calculationPage` | no link from a simulation to stored calculation ids | No |

The evidence/claim graph, option terms and virtual portfolios each need their own persistence slice (tables, append-only and RLS rules, a hash-verified loader). Each is bigger than this increment, so none was invented here.

## Changes (9 files)
| File | Change |
|---|---|
| `packages/adapters/src/qw_adapters/sql/0011_calculation_record.sql` (new) | `app.calculation_record(tenant_id, id, content_hash, record jsonb, computed_at, recorded_at)`. PK `(tenant_id, id)`; CHECK `id = 'calc:' \|\| content_hash`, 64-hex hash, object record at most 256 KiB. Statement triggers refuse UPDATE, DELETE and TRUNCATE (`app.journal_append_only`, as 0007). Forced RLS `tenant_isolation`. `qw_app` and `qw_worker` get SELECT and column INSERT only. |
| `packages/adapters/src/qw_adapters/calculation_store.py` (new) | `record(tx, rec)`: checks the domain rules, then inserts the canonical record (`ON CONFLICT DO NOTHING`, so a recomputation keeps the first `computed_at`). It reads the row back: an existing row under the id whose hash or body differs is `CalculationStoreError("integrity")`. `computed_at` is outside the content hash; only `as_of <= computed_at` guards it, so a hostile writer with INSERT could store a later or earlier `computed_at` that still loads. `load(tx, id)`: the tenant's record or None. It rebuilds through `CalcInput` (canonical decimals and dates), requires the canonical form to round-trip, recomputes the content hash against the column and the id, and re-checks: status ok iff a result exists, iff no unavailable reason; the reason's status mapping; canonical result decimal; unique input names; `as_of <= computed_at`. Any failure is `CalculationStoreError("integrity")`. |
| `apps/api/src/qw_api/research.py` (new) | `GET /api/v1/calculations/{calculation_id}`: membership `Reader`, session tenant under RLS, id pattern as `/accounts`, 404 `not_found` for unknown and other-tenant ids alike, body `CalculationRecord.to_contract()`. An integrity failure propagates to the 500 `internal_error` handler. Nothing is written. |
| `apps/api/src/qw_api/app.py` | Includes the router. |
| `packages/adapters/tests/test_calculation_store.py` (new), `apps/api/tests/test_research_api.py` (new) | Tests below. |
| `packages/adapters/tests/test_tenancy.py` | Adds the table to the forced-RLS / no-PUBLIC-grant list. |
| `apps/api/tests/test_api.py` | Operation count 14 to 15. |
| `docs/receipts/T054-api.md` (new) | This receipt. |

Wire form: decimals are strings (`"0.1"`, `"110"`), timestamps UTC with `Z`. What the spec schema has no field for (as-of, rounding, coverage, date and reference inputs, input as-of times, the unavailable code) is named in `warnings` by the T018 projection, never dropped.

## Actual checks
Environment: Linux, Python 3.13 via `uv`, throwaway PostgreSQL 16 cluster from the conftest harness, `QW_REQUIRE_DB=1`.

| Command | Result |
|---|---|
| `uv sync --locked` | ok |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 232 files already formatted |
| `uv run mypy --strict` | Success: no issues found in 180 source files |
| `QW_REQUIRE_DB=1 uv run pytest -q` | first run: 1 failed (`test_no_implemented_operation_accepts_a_tenant_id`: 15 operations, expected 14; the new route), 1922 passed. The count was updated. |
| `QW_REQUIRE_DB=1 uv run pytest -q` (before review nits) | 1923 passed, 0 failed, 0 skipped |
| `QW_REQUIRE_DB=1 uv run pytest -q` (after nits N1-N5) | 1926 passed, 0 failed, 0 skipped (2 library deprecation warnings) |
| Temporary revert, N1: read-back comparison reduced to `row is None` | `test_recording_over_a_poisoned_row_is_refused` FAILED (1 failed, 7 passed); restored |
| Temporary revert, N2: canonical round-trip check replaced by `if False` | `test_a_non_canonical_row_with_a_matching_hash_fails_load` FAILED (1 failed, 7 passed); restored |
| `uv run python tools/check_no_float.py` | PASS: 0 problem(s) in 89 file(s) |
| `python3 tools/check_spec_artifacts.py` | PASS docs/spec matches reports/ARTIFACT_MANIFEST.json; backlog statuses valid |
| `uv run python tools/project_contract.py` | exit 0 (no amendment changed) |
| `uv run python tools/check_contracts.py` and `--self-test` | PASS: 63 paths, 37 mutating operations, 26 schemas, 7 examples; self-test 16/16 defects detected |

Not run: pnpm checks (no TypeScript changed).

## Negative/adverse cases (tested)
- Another tenant's calculation id: 404 `not_found`, identical (except correlation id) to an unknown id; the same figure stored by two tenants is two rows, each visible only to its tenant under RLS.
- No session: 401 `unauthenticated`. Malformed id `-bad`: 400 `invalid_request`.
- A row tampered by an owner bypassing triggers (`session_replication_role = replica`, result 0.1 to 0.2): `load` raises integrity; the route answers 500 `internal_error` without the forged value.
- Hash-consistent forgeries written through the unchecked insert (status ok without a result, a result on an unavailable status, as_of after computed_at): `load` refuses each.
- Recording the real figure over a pre-inserted poisoned row (same id and hash column, result 0.2): `record` raises integrity (N1).
- A row whose hash matches but whose body carries an extra key: `load` refuses it through the canonical round-trip check (N2).
- A `qw_app` INSERT in tenant A's transaction naming tenant B: refused by row-level security (`InsufficientPrivilege`), and B sees nothing (N5).
- UPDATE, DELETE, TRUNCATE as `qw_app`: permission denied. Inserts with an id that is not the hash, a malformed hash, or a non-object record: check violation.
- Recomputing the same figure a day later keeps the first `computed_at`.
- The response validates against `calculation.schema.json` with date-time formats enforced; reads write no rows.

Oracle: (110 - 100) / 100 = 0.1, hand-computed. All data SYNTHETIC.

## Unresolved gates and remaining work in T054
- No production path writes calculation receipts yet: T018 figures are computed in memory. Wiring the calculation, backtest and simulation engines to `calculation_store.record` is follow-up work, so on a real install the route currently answers 404 for every id.
- Not served (no store): `/claims/{id}`, `/evidence/{id}`, `/instruments/{id}/option-terms`, `/simulations`, `/simulations/{id}/results`. Each needs a persistence slice first (evidence/claim graph from T036, an option-terms registry, virtual portfolios from T041 and their result links).
- Option scenario grids and differentiated scorecards have no contract route or schema; they need an owner contract decision. No route was added for them.
- Acceptance scenarios AT017, AT019, AT020, AT056, AT063, AT064, AT065, AT084, AT085, AT089, AT090, AT093, AT098 and AT099 were not run as features.
- Rollback: revert the commit; 0011 is forward-only, so on a migrated database recovery is a new forward migration dropping the table (ADR-013).

## Review
Round 1 (cross-model reviewer): APPROVE with nits N1-N5.
- N1: `record` verifies an existing row on conflict. Applied, with a test.
- N2: test for the canonical round-trip check. Applied.
- N3: `computed_at` outside the hash noted (store docstring and Changes above). Applied.
- N4: format count 232; `test_research_api.py` docstring describes the warnings-based handling. Applied.
- N5: cross-tenant INSERT under RLS tested. Applied.

All applied. Verify pass (the same reviewer, running a different model from the implementer): APPROVE. The N1 read-back runs in the same tenant transaction and keeps idempotent re-records; mutations disabling the N1 read-back, the canonical round-trip and RLS WITH CHECK are each caught by exactly their new test; all checks re-run (1926 passed).
