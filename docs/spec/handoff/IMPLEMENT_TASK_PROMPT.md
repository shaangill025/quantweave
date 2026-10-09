# Bounded implementation prompt

Implement the explicitly assigned Txxx task, after verifying its prerequisites and actual repository state. Read AGENTS.md, its task contract and linked specs/requirements/contracts. Explain any material contradiction before changing the source of truth; do not add or remove launch scope.

Create a short execution plan naming affected files, invariants, negative cases, verification commands and acceptance receipts. Prefer a vertical tested slice. Add meaningful failing tests, implement the minimum necessary change, run the bound local checks, and inspect both positive and adverse paths. Keep deterministic financial/security tests independent from model opinions. Mocks are labelled as mocks.

Treat credentials, source documents and generated experimental code as untrusted/permission-scoped. Do not call live providers or spend without explicit authority. Do not implement broker mutation endpoints. Do not self-promote strategy/model/code changes into runtime use.

Deliver changed files, command receipts, scope/requirement coverage, unresolved qualification gates, and a handoff identifying next dependency-ready work. Do not claim done when required checks failed or were not run. Keep autonomous work within the assigned task.
