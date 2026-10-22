# Owner decisions, 2026-10-22

The repository owner made these decisions on 2026-10-22 in a recorded quiz. The
owner selected an option label; each option also carried a description, written by
the orchestrator and shown to the owner. Both are quoted verbatim below and kept
separate: the label is what was selected, the option text is what was presented.
"Implemented by" names the task that carries, or will carry, the decision. Nothing
below waives a test, gate or spec requirement. Numbering continues from
`2026-10-21-owner-decisions.md`; decisions 1 to 44 are in the earlier files. In the
quoted option text, "decision 5" is decision 5 of `2026-10-20-owner-decisions.md`.

## Release history

45. **Release and install history before its backing exists.**
    Question: "The approved release-history read can't be fully backed yet: nothing
    records the server's own deployment, there's no trusted-key store to verify
    signatures, and release receipts aren't stored. What should happen?"
    Selected label: "Build operator setup first (Recommended)".
    Option text as presented: "First add an operator command that records the
    deployment and its trusted public keys (with revocation), then serve the read
    with full signature checks; receipts shown as 'unavailable' until stored. Why:
    decision 5 says reads go live only when real storage backs them, and without a
    recorded deployment every caller would just get 404. Not 'Serve keyless now': it
    ships a read that always 404s and shows unverified signatures."
    Implemented by: T051 (operator setup), then T055 (the read).

## Strategy promotion evidence

46. **Strategy hard metrics for assessor-run promotions.**
    Question: "Decision 46: every strategy scorecard needs three hard metrics
    (lookahead_violations, survivorship_violations, unrecorded_trials), and nothing
    records them yet. Until something does, every assessor-run promotion ends
    'insufficient: metric missing' and uses up its one holdout read. How should they
    be produced?"
    Selected label: "Derive in the run (Recommended)".
    Option text as presented: "The assessor run derives them from evidence it
    already holds. Look-ahead is 0 by construction, because walk-forward refuses any
    look-ahead read. Unrecorded trials are counted from the trial ledger.
    Survivorship violations are 0 only when there is no universe selection (a fixed
    single instrument), with that basis recorded; otherwise it is left missing. Why:
    every value comes from a check the system really runs, nothing is guessed, and
    the widened bound applies right away. Not 'Record in backtest': the backtest
    can't see the trial ledger."
    Implemented by: T044 (decision 39, the assessor-run promotion).

## Release deployment

47. **Who may record the installation's deployment.**
    Question: "Decision 47: qw_worker can still record the installation's
    deployment (granted in migration 0029). Deployment membership will decide who
    can see release history, and generated experiment code runs as qw_worker. Should
    only the operator (qw_release) be able to record it?"
    Selected label: "Operator only (Recommended)".
    Option text as presented: "Revoke qw_worker's write on release_deployment, so
    only the qw_release operator command can record it. Why: untrusted generated code
    runs as the worker, so it must not be able to rewrite who can see release
    history. Nothing needs the worker grant today, because the deployment is recorded
    once at setup."
    Implemented by: T051 (operator setup).

48. **Versioning a recorded deployment.**
    Question: "Decision 48: once recorded, a deployment is frozen. Should adding a
    tenant or operator to it later (versioning) be built before the release-history
    read ships?"
    Selected label: "Later, when needed (Recommended)".
    Option text as presented: "Ship the read on a frozen deployment and add
    append-only versioning in a later increment, when a real need to change
    membership appears. Why: the personal pilot has one tenant and one operator, so
    versioning now adds schema and tests with no user, and a frozen record is the
    safer default. Not 'Before the read': it delays the approved read for a case
    that doesn't exist yet."
    Implemented by: T051 and T055 (a frozen deployment; versioning deferred).

49. **Look-ahead "by construction" for decision 46.**
    Question: "Decision 49: the implementer found a hole in the look-ahead guard.
    Strategies receive the raw data view through the backtest context, and through
    it they can read facts dated after 'now' without being refused (shown with a
    probe; no strategy uses it today). With the hole closed, the guard still can't
    see data a strategy smuggles in another way, such as data captured when the
    strategy was built or private attributes. Does that count as decision 46's
    'look-ahead is 0 by construction'?"
    Selected label: "Fix, then record 0 with basis (Recommended)".
    Option text as presented: "Hide the raw view, so the context's own time-checked
    reads are the only way in, and add a regression test. Then record
    lookahead_violations = 0 with its basis stated: 'walk-forward refusal; strategy
    code is manifest-registered and trusted; data captured outside the context is
    not detected'. Why: it closes the real hole, and stating the limit keeps the
    evidence honest. Strategy code is reviewed and registered, not generated at run
    time. Not 'Leave it missing': no strategy promotion could ever pass."
    Implemented by: T044 (with T033, the backtest look-ahead guard).

50. **Unrecorded trials before an assessor-run promotion.**
    Question: "Decision 50: before an assessor-run promotion, the trial ledger may
    show granted reads that have no recorded trial (unrecorded trials). What should
    the run do?"
    Selected label: "Refuse before the read (Recommended)".
    Option text as presented: "If any unrecorded trial exists, refuse up front with
    'unrecorded_trials', so the holdout read is not spent. Why: the result would be
    a certain REJECT anyway, and spending the one-time holdout read on a known
    reject wastes it. Refusing keeps the read for after the missing trials are
    recorded."
    Implemented by: T044 (decision 39, the assessor-run promotion).
