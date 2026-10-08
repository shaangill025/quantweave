# Specification package (as received)

Portfolio Intelligence master specification v1.0.0, 8 October 2026. This is a planning
baseline; nothing in it is an implemented, qualified or legally cleared product.

| File | Role |
|---|---|
| `MASTER_SPEC.md` | Normative text: sections 1–19 (product, architecture, ledger, calculations, data, policy, strategies, options, AI, self-improvement, security, UX, simulation, operations, governance, testing, work plan, sources) |
| `Portfolio_Intelligence_Master_Spec_v1.0.pdf` | Rendered copy of the master spec |
| `Portfolio_Intelligence_Implementation_Tracker_v1.0.xlsx` | Working tracker: tasks, requirements, gates, qualifications, risks, decisions, sources |
| `VALIDATION.md` | Report from the package validator, run by the package author on the full package (not reproduced here) |
| `BOOTSTRAP_PROMPT.md` | Instructions for the first coding session (T001) |
| `derived/*.csv` | Registers exported from the workbook, with the banner rows removed. Working copies, **not** the canonical `planning/*.csv`. Formula columns (for example `tasks.csv` "Dependency readiness") hold the workbook's cached values |
| `SHA256SUMS` | Hashes of the imported files; checked by `python3 tools/check_spec_artifacts.py` |

## Not supplied

`MASTER_SPEC.md` links to a larger package that was not part of the upload. Missing:
`START_HERE.md`, `handoff/PROJECT_CONTEXT.md`, `planning/` (`WORK_PLAN.md`, `backlog.csv`,
`requirements.csv`, `decision_register.csv`, `release_gates.csv`,
`qualification_register.csv`, `tasks/T*.md`), `contracts/` (`openapi.yaml`,
`data_dictionary.json`, JSON schemas), `fixtures/numerical_oracles.json`, Gherkin
acceptance files, `reference/SOURCES.md`, `spec/*.md` (whose content appears to be
concatenated into `MASTER_SPEC.md`) and `tools/validate_spec.py`. The internal links in
`MASTER_SPEC.md` therefore don't resolve in this repository. See
`docs/receipts/T001_bootstrap_receipt.md`.
