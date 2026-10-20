# Owner decisions, 2026-10-20

The repository owner made these decisions on 2026-10-20 in a recorded quiz. The
owner selected an option label; each option also carried a description, written by
the orchestrator and shown to the owner. Both are quoted verbatim below and kept
separate: the label is what was selected, the option text is what was presented.
Rationale lines are the implementer's, not the owner's, and anything they add beyond
the option text is a proposal, not a decision. "Implemented by" names the task or
pull request that carries, or will carry, the decision. Nothing below waives a test,
gate or spec requirement.

## Contract decisions (recorded for implementation after PR #126 merges)

PR #126 (`t044-experiment-store-reads`) serves listExperiments and readExperiment
under project contract 0.5.0 and raised these as F5, F6 and F7 in
`docs/receipts/T044.md`. Decisions 1 to 3 are implemented by a separate worker after
PR #126 merges, as project contract 0.6.0. The implementer's proposed exact target
is in `docs/decisions/2026-10-20-contract-0.6.0-read-proposals.md`, section
"Decisions 1 to 3: exact target"; it is pending owner review, not decided.

1. **readExperiment item coverage.**
   Selected label: "Add item coverage (Recommended)".
   Option text as presented: "Give the experiment response a coverage field, as the
   proposal response got in 0.4.0. Incomplete or blocked records are then served with
   their reasons instead of refused. This reuses an existing pattern, the governance
   view can show single experiments, and nothing pretends to be complete."
   Rationale (implementer): every current record carries a reason (sandbox gate,
   synthetic evidence, no receipt store), so readExperiment cannot return 200 today
   (T044 F7). Proposed, not decided: the reason forms and the removal of the 409
   `coverage_required` answer.
   Implemented by: T044, project contract 0.6.0, after PR #126.

2. **`parent_optimizer_version`.**
   Selected label: "Make it nullable (Recommended)".
   Option text as presented: "The field is null for non-optimizer targets. All six
   targets then appear in the list, and the value stays meaningful where it exists."
   Rationale (implementer): the schema requires a string, and the domain has no sound
   value for other targets; such experiments are withheld from listExperiments today
   (T044 F5). Proposed, not decided: the `allOf` conditional rule and naming the null
   `none:parent_optimizer_version` in coverage.
   Implemented by: T044, project contract 0.6.0, after PR #126.

3. **Experiment states.**
   Selected label: "Add blocked + insufficient (Recommended)".
   Option text as presented: "Add `blocked` and `insufficient_evidence` states.
   `insufficient_evidence` can be re-assessed once more data exists. This is honest,
   the governance view can show why an experiment stalled, and it ends the misleading
   `failed`."
   Rationale (implementer): insufficient evidence maps to the final `failed` state
   today, and blocked records read as proposed or authorized with a reason (T044 F6,
   T008 C-21). Proposed, not decided, pending open question Q7 of the proposal: the
   transition table, and the mechanics of re-assessment (it can succeed only while
   the plan's single holdout read is unused; data that needs a new plan means a new
   experiment).
   Implemented by: T044, project contract 0.6.0, after PR #126.

Note: where decisions 3 and 4 above say "pending Q7" or "until the owner reviews", the section "Answers to the proposal's open questions" below now settles them.

## Design work requested (proposals for owner review, not contract changes)

4. **Reads to design for 0.6.0, as proposals for owner review.** Selected labels:
   - "Scorecard & verdict read (Recommended)";
   - "Release & install history (Recommended)";
   - "Caller's approval classes (Recommended)".

   "Budget amounts" was not selected.
   Rationale (implementer): these are the T055 API gaps (`docs/receipts/T055.md`)
   that block the governance view's scorecards, release history and role-dependent
   rendering.
   Implemented by: `docs/decisions/2026-10-20-contract-0.6.0-read-proposals.md`
   (proposal only). Nothing is added to the contract or the API until the owner
   reviews that proposal.

## Answers to the proposal's open questions (same day, owner quiz)

The owner answered eight questions taken from the proposal's Q1–Q8. Each
entry shows the selected label and the option text as it was presented,
both verbatim.

5. **Q2, when the new reads go live.** Selected: "Approve shapes, serve when backed (Recommended)".
   Option text: "Agree the designs now. Each read goes live only once its storage really exists: approval classes first, then scorecards, then release history. Until then the server never returns an empty list as if it were real data, so nothing looks complete when it isn't."
6. **Q3, scorecard values.** Selected: "Tenants pass/fail only (Recommended)".
   Option text: "Tenants see pass/fail per metric. Numbers go only to the maintainer and assessor roles, once those exist. Numbers for rejected candidates could let later candidates be tuned to test cases that are reused across plans; pass/fail gives much less of that away."
7. **Q7, re-assessment after insufficient evidence.** Selected: "New plan = new experiment (Recommended)".
   Option text: "The same experiment can be re-assessed only while its one-time holdout read is unused. Data that needs a new plan starts a new experiment that links back to the old one. The protected holdout is never re-used, and the lineage stays clear."
8. **Q1, amendment tooling.** Selected: "Yes, extend the tooling (Recommended)".
   Option text: "Add `operations` and `new_schemas` amendment keys, applied by project_contract.py and checked by check_contracts.py. Also document GET /session in the contract. The contract stays the single source of truth for what is served."
9. **Q4, installation-scope scorecards.** Selected: "Only for releases offered to them (Recommended)".
   Option text: "A tenant sees an installation-wide scorecard only for a release it is being asked to adopt. That gives tenants what they need to decide on adoption without exposing all installation-wide experimentation."
10. **Q5, operator and maintainer identities.** Selected: "Hide from tenants (Recommended)".
    Option text: "Tenants see that a release was approved and installed, and when, but not by whom. Operator and maintainer identities are visible only to installation roles, which protects staff privacy and reduces social-engineering risk."
11. **Q6, self-hosted maintainer release.** Selected: "Only if explicitly set up (Recommended)".
    Option text: "A self-hosted operator holds maintainer-release approval only if the installation is explicitly configured that way. This keeps install and approve separated by default, so one person can't silently do both."
12. **Q8, versioning.** Selected: "0.6.0 now, reads in 0.7.0 (Recommended)".
    Option text: "Ship the three experiment decisions as 0.6.0 now (already in progress). The new reads come in 0.7.0, each switched on only once its storage exists, as you just chose."

Effect: the proposal's read shapes are approved in principle for contract
0.7.0. Each read is served only once its backing store exists, and decisions
1–3 ship as 0.6.0. Q7 settles the re-assessment mechanics that decision 3
left open.
