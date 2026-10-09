# Portfolio Intelligence — implementation specification package

**Working descriptive title, not an approved brand. Version 1.0.0 — 8 October 2026.**

This is a repository-ready design and planning package, not a running trading application. It contains specifications, draft implementation contracts, task contracts, synthetic test data and handoff instructions. No production deployment, account connection, strategy qualification, trading edge or regulatory clearance is claimed.

## Start here

1. Read [START_HERE.md](START_HERE.md) and [MASTER_SPEC.md](MASTER_SPEC.md).
2. Use [requirements](planning/requirements.csv), [decisions](planning/decision_register.csv), [backlog](planning/backlog.csv) and [release gates](planning/release_gates.csv) for scope and traceability.
3. Give a coding agent [AGENTS.md](AGENTS.md), [BOOTSTRAP_PROMPT.md](handoff/BOOTSTRAP_PROMPT.md) and the first bounded task from [WORK_PLAN.md](planning/WORK_PLAN.md).
4. Run the **specification validator**, not an application test, using the command in [VALIDATION.md](reports/VALIDATION.md).

## Non-negotiable scope

Apache-2.0; independent full self-hosted and hosted editions; Canadian and US commercial audience; US-listed stocks/ETFs; optional supported options; CSV/manual and named read-only integrations; six strategy families; useful free-data/rules-only decisions; independent AI proposal evaluation; all six improvement areas, including code and optimizer recursion; runtime human promotion/adoption controls; no broker order submission.

Implementation waves are dependency order, not permission to ship a reduced MVP as the agreed release. Required but unqualified providers/strategies remain explicit blockers or capability restrictions as defined in the release policy. A catalogued research-only strategy is not marketed as live-qualified. The full release still requires working qualified stock intraday and options paths with suitable configurations, and the specific free-mode floor.

## Contents

- `spec/`: normative product, accounting, architecture, quant, AI, options, security, operations and UX specifications.
- `contracts/`: OpenAPI 3.1 application API, JSON Schema contracts and synthetic examples. These are designed interfaces; no server is implemented.
- `config/`: proposed versioned policies/registries; live eligibility and promotion remain disabled until qualification.
- `planning/`: Q1–Q79 lineage, requirements, implementation tasks, risks, provider checks and release gates.
- `tests/`: Gherkin acceptance specifications, numerical oracles and synthetic fixtures; not executed application tests.
- `handoff/`: agent prompts, task and evidence templates, AC/ECC coexistence guidance.
- `adr/`: architecture decisions and explicit consequences.
- `tools/`: offline artifact validator only; no broker/model/network operations.
- `reference/`: dated primary-source registry and qualification limitations.
- `reports/`: artifact-validation report; does not assert application validation.

Master Markdown and PDF are reading views; specialized contracts, requirement rows and linked specifications carry implementation detail. Derived tracker/PDF changes must not silently supersede the canonical text/JSON/CSV records.

## Reading and tracking views

- [Master specification PDF](reports/MASTER_SPEC.pdf)
- [Implementation tracker workbook](planning/Implementation_Tracker.xlsx)

These views are packaged alongside their canonical Markdown/CSV sources. The artifact validator checks schema/reference/numerical expectations, not the application. See reports/VALIDATION.md for the actual executed checks and reports/ARTIFACT_MANIFEST.json for content hashes.
