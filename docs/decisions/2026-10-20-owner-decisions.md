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
