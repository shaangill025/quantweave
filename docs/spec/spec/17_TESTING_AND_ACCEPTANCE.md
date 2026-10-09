# 17. Testing strategy and evidence receipts

## Layers

Specification checks validate internal file consistency, IDs, dependencies, JSON schemas, OpenAPI references, configuration counts, fixtures and links. They are executed on this package and reported separately. They do not test an application that does not yet exist.

Application unit/property tests cover monetary/unit accounting, rounding, source matching, timestamps, calculations, eligibility and state machines. Contract tests cover API/adapter schemas and negative permissions. Integration tests use production-like PostgreSQL roles and isolated workers. End-to-end tests exercise user journeys on supported deployment profiles. Adversarial tests cover prompt injection, cross-tenant access, untrusted code and archive/CSV/network attacks. Research qualification and legal/rights gates require separate evidence beyond software tests.

## Required numerical oracles

Use synthetic exact cases for cash and lots, split invariance, dividend handling, FX return interaction, TWR cash flows, unique/ambiguous MWR, coordinated cash proposals, covered-call coverage, cash-secured put collateral and all vertical directions. Compare calculation outputs to independent oracles, not two copies of the same implementation. Edge cases include missing values, fees/currencies, partial fills, negative equity and no valid roots.

## Property and concurrency tests

Repeated imports preserve total holdings/cash; corrections reverse/repost without deleting original knowledge; unit/currency postings balance in their own commodity; split preserves cost and economic value; no tenant can cross namespace boundaries; final proposal commitments never exceed qualified cash; duplicate event processing cannot duplicate account activity; deletion tombstones survive restore. Run randomized input sequences with fixed reproducible seeds and shrink failing examples.

## Research and AI tests

Claim-verification cases include wrong company/date/unit, outdated filing, uncited material assumptions, syndicated evidence, retracted source, calculation mismatch and hostile instructions. AI evaluators see blind evidence first and log independent execution IDs. Timeout, insufficient budget and revision changes exercise pending/research-only/expired states. No test assumes a model answer is ground truth.

Historical strategy tests include future filings, survivor-biased universes, corporate-action leakage, ambiguous fills and unrealistic costs. Prospective decisions must be frozen before outcomes. Promotion criteria are defined before evaluating candidates; incomplete criteria block qualification. Required full-scope functionality cannot be certified from mocks alone.

## Full release checklist

All 16 release gates in planning/release_gates.csv need receipts. Demonstrate free useful decisions, each named import/sync, independent AI thesis/review, timely intraday, options risk/lifecycle, scoped loss pauses, simulation/benchmarks, all six improvement targets and recursion, portability/deletion, combined performance, security and recovery. Rights/legal approval and actual provider access are independent gates.

Every acceptance row has a stable ID. The Gherkin files are specification scenarios, not step implementations. Create executable steps/tests during the corresponding tasks, refine vague triggers into concrete fixtures, and preserve the original requirement linkage. Include negative controls, not just happy paths.

## Completion receipt format

Task/test/gate IDs; actual code commit and dirty-tree status; schema/config/data versions; environment and hardware; command or manual procedure; authorized dataset provenance; start/end times; expected and observed outcomes; artifacts/log hashes; test count/pass/fail/skipped; numerical residuals; model/provider cost and deadlines where relevant; limitations; reviewer identity; rollback/recovery notes. Skips are not passes.

Do not invent application command names in documentation before they exist. Bootstrap establishes the reviewed command registry and leaves unobserved checks configured=false. Existing AC/ECC registries are merged, not replaced. Small edits need scoped tests; scope-critical changes also run the financial/security regression suite.
