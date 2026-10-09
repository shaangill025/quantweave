# ADR-013: Toolchain, decimal policy and dependency licensing

Status: reviewed decision for T009 to implement; not yet implemented or verified. Date: 2026-10-09.
Task: T008. Requirements: R070, R080, R088 (and R022 for licensing). Builds on ADR-001, ADR-003,
ADR-010, ADR-011 and ADR-012. Review record: `docs/architecture/T008_contract_review.md`.

## Context

The spec (§3) fixes a typed Python core, a TypeScript/React client and PostgreSQL. It leaves the
concrete tools and version pins to T009, after review. This ADR records which tools to use and why.
**No version numbers are chosen here.** T009 pins each version against the official registries (PyPI,
npm, GitHub) on the day it runs. It records those pins in lockfiles and the check registry, and records
the licence of every resolved package.

The host has been observed with Python 3.13, uv, ruff, mypy, pytest, Node 22, pnpm and PostgreSQL 16
binaries (T001 §3). Those are session observations. They are not project pins.

## Decisions

### Python: 3.13, uv workspace
- **Choice.** CPython 3.13 is the only supported interpreter for `packages/*` and `apps/{api,worker}`
  (`requires-python` limited to 3.13 until a superseding change). A single **uv workspace** has one
  member per package, one `uv.lock`, and `uv sync --locked` in CI.
- **Why.** 3.13 is current, it is present on the reference host, and it has years of security support
  ahead. uv gives one cross-platform lockfile with hashes, workspace path dependencies between packages,
  and fast reproducible installs. That serves the "lockfiles and integrity checks" rule in spec §12.
- **Alternatives.** Poetry (workspace support is weaker and resolution slower), pip-tools (no workspace
  model, so one lock per package), Hatch (good builds but not a lock-first workflow), Python 3.12
  (older, and nothing gained).

### Tests and quality: pytest + Hypothesis; mypy --strict; ruff
- **pytest** runs all Python tests. **Hypothesis** runs the property tests that spec §17 requires
  (balanced postings, idempotent re-import, split invariance, commitments ≤ cash). Each profile has a
  fixed `derandomize`/seed, and the failing example is recorded in the receipt. A test case is not a
  test run, and skips are reported as skips.
- **mypy --strict** over `packages/` and `apps/{api,worker}`, with no `Any` leaking from DTOs into domain
  types. Pyright was considered. It is Node-based and its strictness is comparable, but mypy keeps the
  Python toolchain single-runtime. T009 may add pyright later as an extra check, never as a replacement.
- **ruff** is both linter and formatter, replacing black, isort and flake8 (one tool, one config). The
  rule set includes the bugbear, simplify, pyupgrade and banned-api families. Banned APIs: `float(` on
  money paths and `datetime.utcnow`/naive `datetime.now()`. T009 configures these and checks they fire.
- **Import boundaries** (review §2) are enforced by a stdlib AST script under `tools/`, without adding
  import-linter.

### Decimal policy
- Money, prices, quantities, rates and budgets are `decimal.Decimal` in Python, `NUMERIC(p,s)` in
  PostgreSQL, and decimal strings on the wire. The per-class scales and rounding directions are in review
  §3.2.
- A project decimal context is installed in each process entry point and used explicitly by domain code
  (`localcontext`). It sets precision 50, which exceeds the 38-digit storage, so intermediate results do
  not round before an explicit quantize. It traps `InvalidOperation`, `DivisionByZero`, `Overflow` and
  **`FloatOperation`**, so `Decimal(0.1)` and float/Decimal ordering comparisons raise. This was
  observed on the host's Python 3.13. Equality with a float stays silent (`Decimal("1") == 0.5` is
  `False`), so mypy's `--strict-equality` (part of `--strict`) has to catch that case.
- Persisted and wire values are quantized only through named functions that declare the class and
  rounding mode. Out-of-scale input is rejected and never rounded silently.
- JSON: money fields accept strings only. FastAPI/pydantic fields use a strict decimal-string type that
  rejects JSON numbers. pydantic's default `Decimal` coerces floats, so it is not allowed. `json.loads`
  is never used with `parse_float=float` on money payloads.
- PostgreSQL: psycopg 3 maps `numeric` to `Decimal`. `real`/`double precision` columns for money or
  quantities are forbidden, and a migration test checks the catalogue for them.
- TypeScript: decimal strings stay strings. The UI never computes authoritative amounts. Display
  formatting uses string operations or a reviewed decimal library chosen at T009 under the licence policy.
- `quant` statistics may use floats behind declared tolerances. Converting a result to a money class
  needs an explicit quantum.

### Database: PostgreSQL 16, psycopg 3, plain SQL migrations
- **PostgreSQL 16** for transactional state, RLS tenant isolation, leases and outbox (ADR-010). The
  reference host already has 16 binaries. Moving to a newer major needs its own ADR and a restore drill.
- **psycopg 3** is the only driver. It supports server-side binding, `COPY`, pipeline mode and async if
  needed later, and `numeric` comes back as `Decimal`. asyncpg was rejected because it is
  async-only, has a weaker typing story, and returns `Decimal` only with extra codecs. SQLAlchemy Core/ORM
  was rejected because the domain does not use an ORM, and repositories are hand-written SQL in
  `packages/adapters`.
- **Migrations: plain, forward-only SQL files** (`NNNN_description.sql`) applied by a small in-repo
  runner on psycopg 3. The runner applies each file in one transaction and records file name, SHA-256
  and applied-at in a `schema_migrations` table. It refuses to run if an applied file's checksum changed,
  and it runs only as `qw_migrate` (review §5). Recovery uses backup plus forward repair, as spec §11
  requires, and no down-migrations exist.
  - *Why.* The security-critical schema is SQL-native: RLS policies, `GRANT`s per role, composite FKs,
    exclusion and partial indexes. Reviewers read exactly what runs. No ORM metadata needs to be kept in
    sync, and no dependency is added.
  - *Alternatives.* Alembic (needs SQLAlchemy; autogenerate is ORM-driven and weak on RLS and grants),
    Sqitch (Perl runtime), dbmate/goose (extra Go binary in every profile), yoyo-migrations (small, but
    adds a dependency for what roughly 150 lines of reviewed code can do). This choice is revisited if
    the runner grows beyond that.

### API: FastAPI in `apps/api` only
- FastAPI (with pydantic and an ASGI server chosen at T009) is confined to `apps/api`. `packages/*` must
  not import FastAPI or pydantic (review §2).
- The OpenAPI document is **contract-first**. The project contract (review A-01) is the source of truth.
  A CI contract test compares FastAPI's generated schema against it, and a mismatch fails. The generated
  schema never replaces the contract.
- *Alternatives.* Litestar (capable but a smaller ecosystem), plain Starlette (more hand-written
  validation), Django REST Framework (brings an ORM and an app model the core does not use).

### Web: TypeScript + React + Vite + pnpm + vitest in `apps/web`
- `strict` TypeScript, React, Vite for dev and build, pnpm with a committed `pnpm-lock.yaml` and
  `--frozen-lockfile` in CI, and vitest for unit and component tests. API types are generated from the
  project OpenAPI contract (tool chosen at T009, review A-04). E2E tooling is chosen at T053/T064.
- *Why.* The web app is a static client of the API (ADR-001). Vite produces static assets that any
  profile can serve, and no second server runtime is needed. pnpm gives strict, content-addressed
  installs. vitest shares Vite's transform pipeline.
- *Alternatives.* Next.js (adds a Node server tier and server-side data paths that bypass the API
  boundary), npm or yarn (weaker strictness for phantom dependencies), Jest (separate transform
  configuration).

### Licence compatibility policy (Apache-2.0 project, R022, QUAL23)
- **Allowed** for runtime and distributed dependencies: Apache-2.0, MIT, BSD-2-Clause, BSD-3-Clause, ISC,
  PSF-2.0, Zlib, PostgreSQL, 0BSD and CC0-1.0, provided notices are preserved in `NOTICE`/SBOM.
- **Allowed with conditions (case-by-case, recorded):** weak copyleft that is used unmodified and
  dynamically, such as MPL-2.0 and **LGPL-3.0 (psycopg 3)**. These may not be vendored or modified. Any
  container image that ships them must include licence texts and the corresponding-source information.
  Legal confirmation is tracked under QUAL23 (review A-09).
- **Denied:** GPL-2.0/3.0, AGPL-3.0, SSPL, BUSL, Elastic License, Commons Clause or other
  "non-commercial" or field-of-use restrictions, and any package with no licence or an unknown one. Build
  and dev tools that are not distributed follow the same denial list, so no AGPL in images or CI caches.
- Datasets, models, prompts and provider terms are **not** covered by this policy and need their own
  rights review (spec §16, T002/T021).
- T009 produces a dependency inventory with SPDX ids from the lockfiles. CI fails on a denied or unknown
  licence. An SBOM is required for releases (T062/T065).

### CI: GitHub Actions
- Workflows run on `pull_request` and `push` to `main`. Jobs: spec checks (`tools/check_spec_artifacts.py`,
  `tools/check_contracts.py`, the validator on a copy), Python lint/format/type/test, web
  typecheck/lint/test, and DB integration against a PostgreSQL 16 service container using the non-owner
  roles.
- Hardening: third-party actions pinned by full commit SHA; workflow `permissions: contents: read` by
  default; no secrets exposed to forked PRs; service images pinned by digest at T009; no deploy, publish
  or release job without explicit owner authorisation.
- Cost: if the repository is private, Actions minutes may be billable. Enabling CI on a private
  repository needs the owner's approval of the activity, as the project rule on spending requires.
- *Alternatives.* Self-hosted runners (operational burden, and they could expose a host), other hosted
  CI (the repository already lives on GitHub).

## Consequences
- T009 creates the uv workspace, `pnpm` workspace, configs, decimal context module, migration runner
  skeleton with its first test, import-boundary check and CI workflows. It pins every version against
  registries on the day, and binds each check in `.agentic/check_registry.json` only after observing it
  run.
- No dependency is added before T009. This ADR names tools, not versions.

## Revisit trigger
A measured problem (resolver failure, typing gap, migration runner growth, licence finding), an
upstream end-of-life, or an owner decision. Supersede with a new ADR and keep this lineage.
