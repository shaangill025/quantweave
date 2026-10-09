# 11. Full self-improvement and bounded recursion

## Scope and non-negotiable first-release requirement

The application must support all six targets: user adaptation; research/retrieval quality; strategy/ranking/model quality; decision-evaluator quality; agent/application code; and the improvement process itself. Every target needs real candidate generation, evaluation, review, promotion and recovery. Code and optimizer recursion cannot be deferred as nonessential experiments.

The objective is evaluation-driven improvement under explicit authority, not unconstrained self-modification. A change is not an improvement merely because it is different, earns higher recent simulated P&L or receives another model's approval. Data/consent/risk/budget/release controls stay outside candidate authority.

## Experiment objects and states

An experiment specifies parent version, target class, hypothesis, permitted mutations, immutable input/corpus manifest, development versus final assessment access, predeclared scorecard, maximum cost/compute/storage/time, owner/consent scope and expected approval classes. Candidate states: drafted, running, assessed, rejected, insufficient_evidence, awaiting_human_review, release_approved, installed, adoption_pending, active, rolled_back or suspended.

Every candidate preserves a diff/artifact hash, exact tool/runtime/model versions, all trial attempts, results including failures, resource usage, assessor identity and lineage. No global best-candidate record may discard failures. A candidate can be technically operational yet fail its goal or remain statistically inconclusive.

## Six concrete launch demonstrations

| Target | Positive controlled demonstration | Required negative control |
|---|---|---|
| User adaptation | Improve alert relevance/explanation preference under a permitted private fixture and measured rubric | Candidate broadens trading risk or exports private examples |
| Research/retrieval | Correct a recurring publication-date or unit-extraction defect on separate cases | Better average score but introduces an unsupported material fact |
| Strategy/ranking | Generate a bounded variant, evaluate costs/chronology/prospective policy, produce promotion/adoption evidence when eligible | Higher in-sample P&L from leakage, survivorship or unrecorded trials |
| Decision evaluator | Reduce harmful approvals without unacceptable rejection/latency regressions | Candidate edits its own answer keys or accepts unverified claims |
| Application/agent code | Repair a seeded deterministic defect in an isolated branch, run independent tests and promote via a reviewed package | Attempts production secrets, host writes, unauthorized dependencies or protected-control mutation |
| Optimizer recursion | V0 proposes V1 improvement logic, independent assessment passes, human promotes V1, V1 runs the next candidate cycle | Relabeling identical code or changing acceptance thresholds without authority |

Controlled fixtures demonstrate machinery and specific task quality, not live profitability. A successful code-defect repair is not evidence that a trading strategy has alpha. Predictive strategy candidates can be rejected/inconclusive while the framework works; however, full release still requires the separately specified qualified user-facing capabilities.

## Recursive lineage acceptance

Record optimizer_v0_hash; candidate_v1_hash and nonempty meaningful diff; fixed assessor/test manifest hash; signed approval receipt; installed_v1_hash; next experiment receipt reporting V1 as actual runner; next candidate lineage referring to V1. Test that V0 cannot write approval records, install V1, alter assessor controls or forge signing keys. The next candidate may fail; recursion requires use of the improved optimizer, not a forced chain of wins.

## Independence of assessment

Development cases are accessible within the experiment mandate. Promotion evaluation is executed by an independent service with protected configuration. Public framework source does not imply candidates may mutate or repeatedly query final assessment feedback. Record all assessment access; rotate/add prospectively collected cases when necessary and account for selection pressure. Candidate-proposed rubric changes are separate reviewed changes and cannot validate that same candidate.

Metrics are target-specific: correctness/claim support, usefulness, cost/latency, numerical oracles, quantitative research outcomes, regression rate and protected invariants. Hard constraints cannot be offset by improved average scores. Human-approved minimum effect, uncertainty and regression criteria are declared before promotion evaluation; unresolved thresholds are qualification work, not an invitation for agents to invent favourable ones.

## Isolation and resource enforcement

Run candidate code in an immutable sandbox with bounded CPU/memory/pids/disk/time, restricted syscalls/filesystems and egress denied except mediated permitted retrieval. No production DB write credentials, host Docker socket, signing keys or broad model-provider secrets. Candidate dependencies are pinned/scanned and preapproved or quarantined for review. gVisor is the initial Linux isolation target; its suitability and host compatibility require actual tests. [SRC-16]

Sandbox permission failure pauses the relevant capability and fails the complete-release gate; do not substitute ordinary in-process Python execution. Running all workflows on one machine does not merge their trust boundaries. Supported macOS/Windows operation may require an operator-managed compatible Linux VM, qualified during W0; no native isolation guarantee is asserted in this package.

## Scope, consent and financial budgets

Private adaptation/research configurations live per user or installation. Shared executable changes are installation/release-level and controlled by maintainers. Sharing a generalized lesson still requires authorized data handling; removing a name alone is not proof that financial history is anonymous. Public starter cases and community contributions need license/provenance review.

Improvement inference is capped at USD20/month within the personal USD100 incremental inference ceiling; compute/storage are separately configured operational limits, not secretly unlimited. Essential monitoring and proposal verification outrank experiments. Authorized local resources may reduce API spending, but the system cannot claim they have zero hardware/energy cost or bypass resource limits.

## Promotion, adoption and recovery

Maintainer release approval, operator installation and end-user strategy adoption are distinct. Material investment changes require user adoption after qualification. Candidate code cannot self-install even on a private installation. Rollback restores compatible approved artifacts/configurations; it never edits history to remove poor results. Irreversible migration risks must be reviewed before promotion, with backup/forward-repair or suspension procedures.

Record post-promotion regressions and invalidate dependent recommendations where appropriate. Automatic rollback is permissible only under an already-approved operational policy to a known compatible version; it cannot widen authority or substitute an unadopted strategy. Explanations and evidence remain available to users while retained.
