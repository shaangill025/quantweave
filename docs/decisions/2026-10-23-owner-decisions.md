# Owner decisions, 2026-10-23

The repository owner made these decisions on 2026-10-23 in a recorded quiz. The
owner selected an option label; each option also carried a description, written by
the orchestrator and shown to the owner. Both are quoted verbatim below and kept
separate: the label is what was selected, the option text is what was presented.
"Implemented by" names the task that carries, or will carry, the decision. Nothing
below waives a test, gate or spec requirement. Numbering continues from
`2026-10-22-owner-decisions.md`; decisions 1 to 53 are in the earlier files.

## Protected holdout series

54. **A baseline loaded for two instruments of one plan.**
    Question: "Decision 54: decision 52 keys protected series on plan, instrument
    and role, so the same baseline (such as buy-and-hold) can be loaded twice for
    one plan under two instruments. Today the load accepts this and the read later
    refuses with 'baseline_ambiguous'. Should the database refuse the second
    baseline at load time instead?"
    Selected label: "Refuse at load (Recommended)".
    Option text as presented: "Add a uniqueness rule so a plan has at most one row
    per baseline. A second load is refused right away with 'series_exists'. Why: the
    mistake surfaces when the operator makes it, before the freeze while a new plan
    is still cheap, rather than at promotion time when the plan is already frozen
    and the run can't proceed. It follows decision 52's 'no choice of which
    version' rule."
    Implemented by: T044 (decision 39, the protected holdout series).

## Release history read

55. **The signature reference shown to tenants.**
    Question: "Decision 55: in the release-history read, a release's signature
    reference contains the maintainer key id ('sig.<key_id>.<hash>'), and earlier
    decisions hide maintainer identity from tenants. What should tenants see?"
    Selected label: "Hash only, no key id (Recommended)".
    Option text as presented: "Serve a reference made from the signature hash
    alone, with no key id. Why: tenants can still check that two releases share or
    differ in signature, and match it against later receipts, without learning
    which maintainer key signed. Not 'null': it throws away harmless, useful
    evidence."
    Implemented by: T055 (the release-history read).

56. **The self-approved flag shown to tenants.**
    Question: "Decision 56: the read shows tenants 'self_approved' (the release's
    signer also installed it, without saying who). Keep showing it?"
    Selected label: "Keep showing it (Recommended)".
    Option text as presented: "Show the true/false flag. Why: it's the main warning
    sign of solo-maintainer releases (decision 23), and it names nobody. Hiding it
    removes the one signal that tells a tenant no second person looked at the
    release."
    Implemented by: T055 (the release-history read).

57. **An operator read of release history.**
    Question: "Decision 57: no session can yet hold an installation operator or
    maintainer role, so only tenant members can read release history and
    identities are never shown. Build an operator read path now?"
    Selected label: "Later, via operator CLI (Recommended)".
    Option text as presented: "Defer it. When needed, add an operator read to the
    qw-release-admin command, which uses the qw_release database login, rather than
    a web session role. Why: the operator is already authenticated by database
    credentials on the host, so building web sign-in and a new role now adds attack
    surface with no user in the one-operator pilot."
    Implemented by: T051 and T055 (deferred; no code now).

58. **The approval time in the release-history read.**
    Question: "Decision 58: an earlier decision says tenants see when a release was
    approved, but approval times live in receipts, which aren't stored yet. Only the
    install time is served. What now?"
    Selected label: "Add as unavailable (Recommended)".
    Option text as presented: "Add 'approved_at' to the contract now, served as null
    and named 'unavailable:receipts', and fill it once receipts are stored. Why: it
    keeps the promise visible in the contract and honest about the gap; nothing is
    invented, and clients won't need a contract change later."
    Implemented by: T055 (the release-history read).

## Assessor-run promotion re-runs

59. **More than one crash re-run.**
    Question: "Decision 59: decision 42 allows 're-run once' after a crash. If the
    re-run also crashes, the next call is refused and the plan's one-time holdout
    read is permanently spent, which decision 42's reasoning ('an infrastructure
    hiccup would permanently burn a holdout window') wanted to avoid. Allow more
    crash re-runs?"
    Selected label: "Up to 3, same digest (Recommended)".
    Option text as presented: "Allow up to three re-runs, each under exactly the
    same recorded read and input digest, then stop. Why: each re-run is
    deterministic on the same inputs, so it learns nothing new and is not a second
    look; a small fixed cap handles repeated infrastructure failures without
    allowing endless retries. Not 'unlimited': a fixed limit keeps the rule
    auditable."
    Implemented by: T044 (decision 39, the assessor-run promotion).

60. **Comparing a re-run when no receipt was stored.**
    Question: "Decision 60: 'if the result differs, refuse as a conflict' can only
    be checked when the first attempt's receipt was stored. A crash after
    evaluating but before storing leaves nothing to compare. Record the outcome
    first?"
    Selected label: "Record outcome digest first (Recommended)".
    Option text as presented: "Right after evaluation, durably record a digest of
    the trial outcome before the receipt. Every re-run is then compared against it,
    and a different result is refused as a conflict. Why: it makes decision 42's
    conflict check work for every crash point, not just some, and a
    non-deterministic strategy is caught."
    Implemented by: T044 (decision 39, the assessor-run promotion).

61. **Loading the protected series before the reservation.**
    Question: "Decision 61: before reserving the read, the run loads and
    hash-checks the protected holdout series (without evaluating it), so a missing
    series is refused without spending the read. Does loading count as 'opening'
    the holdout?"
    Selected label: "Loading is not opening (Recommended)".
    Option text as presented: "Keep it: loading and verifying stored series is a
    technical check that nobody and no strategy sees; 'opening' means evaluating a
    strategy on it. Why: refusing a missing or tampered series before the
    reservation saves the read from being burned by a data-loading fault, and no
    information about the data reaches anyone."
    Implemented by: T044 (decision 39, the assessor-run promotion).

62. **Certain BLOCKED verdicts before the read.**
    Question: "Decision 62: some BLOCKED verdicts (sandbox unavailable, assessor not
    registered, budget reservation, plan problems) are certain before the read, but
    the run still spends the read to record them. Refuse those before the read
    too?"
    Selected label: "Refuse before read (Recommended)".
    Option text as presented: "Extend decision 50's rule: any cause that is known
    before the read and would certainly give BLOCKED refuses up front, so the read
    is kept. Why: the same reasoning as decision 50; spending the one-time read on
    an outcome known in advance wastes it. The refusal is still recorded as a job
    failure, so nothing is hidden."
    Implemented by: T044 (decision 39, the assessor-run promotion).

## Release history events

63. **Release fields on deployment events.**
    Question: "Decision 63: in the deployment-events list, which release fields
    does each event show? A rollback withdraws one release and restores another; the
    stored row also names the withdrawn release as its 'release_id'."
    Selected label: "Rollback: from/to only (Recommended)".
    Option text as presented: "Install and pause show release_id; a rollback shows
    from_release_id (withdrawn) and to_release_id (restored), with release_id null.
    Why: each field has one meaning across all kinds, so a client can't mistake a
    withdrawn release for an installed one; the schema enforces which fields each
    kind fills."
    Implemented by: T055 (the release-history read).

## Assessment request inputs

64. **Who writes and reads the assessment request store.**
    Question: "Decision 64: a new 'assessment_request' table will keep each
    promotion request durably until its run returns. Decision 40 says 'no new
    database grants' (meaning: don't widen the roles). Who may write and read it?"
    Selected label: "Worker writes, assessor reads (Recommended)".
    Option text as presented: "qw_worker inserts the request as part of the job it
    submits; qw_assessor may only read it; qw_app nothing. Why: the request comes
    from the job side, and the assessor must read exactly what was submitted. A
    read-only grant on one request table doesn't let a compromised assessor change
    jobs or revisions, which is what decision 40 guards against."
    Implemented by: T044 (decision 39, the assessor-run promotion).

65. **How a stored request refers to strategy code.**
    Question: "Decision 65: a request names strategy code (a Python function), which
    can't be stored. How should a stored request refer to it?"
    Selected label: "Id + version + code hash (Recommended)".
    Option text as presented: "Store the strategy id, version and implementation
    hash; on load, look it up in the qw-assessor process's built-in, reviewed
    strategy registry and refuse if missing or the hash differs. Add the hash to the
    re-run digest. Why: only reviewed, registered code can run (decision 49), and
    the hash catches code that changed between attempts. The registry is built with
    the qw-assessor entrypoint and starts empty, so runs fail closed until a real
    strategy is registered."
    Implemented by: T044 (decision 39, the assessor-run promotion).

66. **Where the caller-supplied marks live.**
    Question: "Decision 66: decision 43 says each verdict is marked 'ledger
    caller-supplied' and 'budget caller-supplied', visible to reviewers, mirroring
    decision 33. Where do the marks live?"
    Selected label: "Columns on the verdict (Recommended)".
    Option text as presented: "Add the two marks as hashed columns on the stored
    verdict record, like decision 33's verdict_source on a signed release, set when
    the verdict is recorded. Reviewers see them wherever they see the verdict. Why:
    a mark that lives on the record can't be separated from it or forgotten, and it
    reuses the read paths reviewers already have. Built in the wiring increment,
    after the re-run work lands."
    Implemented by: T044 (decision 39, the assessor-run promotion).

67. **What the request store holds.**
    Question: "Decision 67: storing the whole request means building storable
    formats for ~15 inputs (ledger, budget, experiment and strategy registries,
    dataset, etc.), about 4 PRs. Most of those inputs don't have their own stores
    yet. What should the request store hold?"
    Selected label: "Ledger + budget in full, rest by hash (Recommended)".
    Option text as presented: "Store the trial ledger and budget book in full (what
    decision 43 names) and bind every other input by content hash; the run refuses
    if the inputs it's given don't match. Why: the two caller-supplied inputs are
    the ones with no other durable home, while the rest will get real stores in
    their own tasks (e.g. T032); building throwaway formats for them now would be
    wasted. The decision-59 digest already binds them."
    Implemented by: T044 (decision 39, the assessor-run promotion).
