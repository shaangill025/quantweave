# quantweave — agent instructions

This repository implements the **Portfolio Intelligence** specification (v1.0.0, 8 Oct 2026).
The specification lives in `docs/spec/`; start with `docs/spec/README.md`, then
`docs/receipts/T001_bootstrap_receipt.md` for current state.

## Owner rules (non-negotiable)

- **Authorship.** The repository owner is the only author. Commits are authored
  `Shaanjot Gill <shaangill025@users.noreply.github.com>`. Never add `Co-Authored-By`
  trailers or any other AI attribution to commit messages, PR titles or PR bodies.
- **Orchestration.** One orchestrator session plans each bounded task and spawns
  worker/role subagents (implementer, quant reviewer, security reviewer, verifier).
  Workers receive a bounded task, its requirement IDs and the files they own.
- **Cross-model review.** Every change set is reviewed by a subagent running a
  *different model* from the one that wrote it before it is committed. Record in the
  task's evidence receipt that the reviewer ran on a different model, plus its findings
  and outcome. A reviewer's approval is evidence, not authority: it cannot waive tests,
  gates or owner decisions.

## Scope and authority (from the spec, §1, §16)

- No broker order submission, amendment or cancellation — ever. No routes, adapters or
  tools that mutate broker state.
- Do not spend money, call billable providers, collect credentials, connect accounts,
  publish or deploy without explicit owner authorization for that specific activity.
- Mandatory first-release scope cannot be dropped or stubbed as placeholders: hosted +
  complete self-hosted editions, all six strategy families, options, independent AI
  evaluator, and all six self-improvement targets including code and optimizer recursion.
- Budget caps: USD100/month inference, USD20/month improvement (personal pilot).
- Status vocabulary and evidence rules are in spec §16–17. A test case is not a test run;
  skips are not passes; synthetic fixtures are not real exports; never fabricate results.

## Task protocol (spec §18)

inspect repo → read bounded task + requirements (`docs/spec/derived/tasks.csv`,
`requirements.csv`) → plan → write failing/acceptance tests → smallest coherent
increment → run real checks → cross-model review → evidence receipt in
`docs/receipts/` → update status/handoff.

## Checks

The check registry is `.agentic/check_registry.json`. Only entries with
`"configured": true` have been bound to commands that were actually executed here.
Do not document or run invented application commands; bind new checks when the code
they test exists, and record the observed outcome.

## Engineering defaults (spec §3)

Modular monolith. Typed Python domain/quant core with no UI or provider-SDK imports;
TypeScript/React UI; PostgreSQL for transactional state and durable jobs. Money and
quantities are decimals (decimal strings in JSON); timestamps carry offsets and normalize
to UTC. Dependency selection happens in T009 after review — do not add dependencies
ahead of it.
