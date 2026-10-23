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

## Reviewed strategy code

68. **What the strategy code hash covers.**
    Question: "Decision 68: the strategy code hash (decision 65) covers only the file
    that defines the strategy. If a strategy imports shared helper code from another
    file and that file changes, the hash stays the same. How should this be
    handled?"
    Selected label: "Hash project imports too (Recommended)".
    Option text as presented: "Include in the hash every project module the
    strategy's file imports, transitively (standard-library and pinned third-party
    code excluded, since the lockfile pins them). Why: it makes 'the hash catches
    code that changed' true for all project code a strategy actually runs, without
    restricting how strategies are written. Not 'self-contained only': it would
    force copying shared code into each strategy."
    Implemented by: T044 (decision 39, the assessor-run promotion).

69. **The version a stored strategy reference carries.**
    Question: "Decision 69: a stored request's strategy reference needs one
    'version'. The request store (#168) used the candidate's id; the assessor
    registry uses the base strategy's version. Which should the reference carry?"
    Selected label: "Base strategy version (Recommended)".
    Option text as presented: "Use the reviewed base strategy's id and version,
    matching the registry entry whose code actually runs; the candidate is already
    bound separately by its id and content hash. Why: the reference's job is to name
    the reviewed code, and code is registered per base version, not per candidate.
    Two meanings of 'version' would let them drift apart."
    Implemented by: T044 (decision 39, the assessor-run promotion).

## Assessor-run inputs

70. **Where the hash-bound run inputs come from.**
    Question: "Decision 70: the assessor-run path is now wired end to end, but every
    run refuses with 'inputs_unavailable'. Inputs bound only by hash (decision 67:
    the study, experiment registry, strategy manifests, research dataset, research
    rights) have nowhere for the assessor to load them from. What next?"
    Selected label: "Build those stores next (Recommended)".
    Option text as presented: "Build durable stores for the missing inputs (the
    T032 experiment/trial store, research datasets, search spaces and evaluation
    protocols), each with its own task's rules, and give qw_assessor read-only
    grants on what it must read. Why: those stores are required by their own tasks
    anyway, so this finishes real work instead of adding a temporary path, and it
    keeps decision 67's 'bind by hash, refuse on mismatch' honest. Not 'store them
    in the request': that's the throwaway-format route decision 67 rejected."
    Implemented by: T032 and the research-data tasks, then T044 (the assessor run).

## Assessor-run input stores

71. **How the experiment registry is stored and read.**
    Question: "Decision 71: the assessor needs the experiment registry (protocols,
    candidates, verdicts) that a request binds by hash. A new verdict anywhere in
    the tenant between submit and run would change that hash. How should the
    registry be stored and read?"
    Selected label: "Tenant log, read as of submit (Recommended)".
    Option text as presented: "A new append-only, hash-chained tenant log of
    registry entries, written by qw_worker as protocols, candidates and verdicts are
    recorded. The assessor replays it as of the request's recorded time, so later
    entries don't disturb a pending run or its re-runs. Why: it's the real T044
    registry the spec needs anyway, and reading 'as of submit' makes the hash stable
    without freezing the tenant."
    Implemented by: T044 (the experiment registry store).

72. **Who writes the study inputs.**
    Question: "Decision 72: who may write the study inputs (search space,
    evaluation protocol)? The evaluation protocol names an approver, but today
    that's just whatever the caller writes."
    Selected label: "Worker writes; approval later (Recommended)".
    Option text as presented: "qw_worker records them (content-addressed,
    append-only); the assessor only reads. The 'approved_by' stays caller-asserted
    for now and is marked as such, like decision 43's marks, until a step-up
    approval flow exists. Why: these are the job side's inputs, and their hashes are
    already frozen into the protocol and plan, so a later write can't change a
    frozen study; real approval needs its own step-up task."
    Implemented by: T047 and T033 (the study input store).

73. **Who writes and reads stored research datasets.**
    Question: "Decision 73: who writes and reads the stored research datasets
    (facts, universe)?"
    Selected label: "Ingest writes; worker+assessor read (Recommended)".
    Option text as presented: "qw_ingest (the data-loading role) writes; qw_worker
    reads (development trials need datasets) and qw_assessor reads; qw_app nothing
    until a route needs it. Why: datasets come from ingested data, not from jobs,
    and both evaluation roles legitimately need them; keeping qw_app off matches the
    other research stores."
    Implemented by: T032 (the research dataset store).

74. **Re-freezing a dataset or search space id.**
    Question: "Decision 74: can a dataset or search space id be re-frozen with
    different content?"
    Selected label: "Refuse; new content = new id (Recommended)".
    Option text as presented: "One content per id; a second, different freeze is
    refused ('dataset_exists'). Why: like decision 52, there's never a choice of
    which version a run used, so nobody can swap data under an id; a new id is
    cheap."
    Implemented by: T032 and T047 (the dataset and study input stores).

75. **Purge or rights revocation of a frozen dataset.**
    Question: "Decision 75: stored research facts may later need retention purges
    or rights revocation (T023), but a frozen dataset is hash-bound and re-runs
    (decision 59) need it unchanged. What happens when a frozen dataset's facts
    must be purged or their rights are revoked?"
    Selected label: "Refuse further use (Recommended)".
    Option text as presented: "Purge or revocation is honored: the facts are
    removed or marked revoked, and any run that needs the dataset afterwards refuses
    ('dataset_purged' / rights refusal), even a pending re-run. Past verdicts keep
    their hashes as evidence. Why: data rights and retention outrank research
    convenience; nothing is silently kept, and the refusal is explicit rather than a
    broken hash."
    Implemented by: T032 and T023 (the research dataset store).

76. **What the dataset content hash covers.**
    Question: "Decision 76: a dataset's content hash omits each fact's instrument
    and several other fields, yet the holdout split hides facts by instrument. Fix
    the hash?"
    Selected label: "Extend the hash to all fields (Recommended)".
    Option text as presented: "Include every fact field (instrument, fiscal
    year/period, form, filing and acceptance times, publication basis) in the
    dataset content hash. Only synthetic data exists today, so changing every
    manifest hash now costs nothing. Why: a field the split relies on must be bound,
    or facts could be moved between instruments without changing the hash."
    Implemented by: T032 (research datasets).

77. **Where research-rights terms live and which time applies.**
    Question: "Decision 77: a run checks research rights against terms (use scope,
    jurisdiction, retention period) and a time. Where do those terms live, and which
    time applies?"
    Selected label: "On the dataset; DB clock (Recommended)".
    Option text as presented: "Store the terms with the frozen dataset (checked when
    frozen and again at each run), and check rights at the database clock's time of
    use. Why: the terms belong to the data, and T021 says 'at' must be the time of
    use from the server clock, so a caller can't pick a convenient time. A rights
    change after freezing is then caught at run time."
    Implemented by: T032 and T021, then T044 (the assessor run).

78. **The candidate strategy manifest.**
    Question: "Decision 78: the candidate strategy's manifest isn't stored; it can
    be re-derived exactly from the base manifest, the search space and the
    candidate's parameters. Store it, or re-derive it?"
    Selected label: "Re-derive and check hash (Recommended)".
    Option text as presented: "Re-derive it from stored inputs on load and require
    its hash to match the bound one; no new table. qw_assessor gets read access to
    strategy manifests but not adoption records. Why: everything it's built from is
    already stored and hash-bound, so a separate copy adds a second source of truth
    that could disagree."
    Implemented by: T027 and T047, then T044 (the assessor run).

## Experiment registry log

79. **Who reads the experiment registry log.**
    Question: "Decision 79: the experiment registry log (decision 71) records
    verdicts, which carry holdout scores. Decision 17 keeps holdout scores away from
    qw_worker, but the worker's append needs to replay the whole log. How should
    appends work?"
    Selected label: "Assessor replays; worker blind (Recommended)".
    Option text as presented: "Only qw_assessor can read the log. When a verdict is
    recorded, the assessor-side step replays it and hands the worker the current
    head; the database refuses any append that doesn't extend that exact head, so
    qw_worker needs no read access. Why: keeps decision 17's separation — the role
    that runs untrusted experiments never sees holdout scores — while the chain stays
    verified."
    Implemented by: T044 (the experiment registry store).

80. **Candidate ids across experiments.**
    Question: "Decision 80: the domain registry requires candidate ids to be unique
    across a whole tenant, but the stores today key candidates per experiment (two
    experiments may each have a 'cand-1'). Which rule wins?"
    Selected label: "Unique per tenant (Recommended)".
    Option text as presented: "Keep the domain rule: a candidate id is unique within
    the tenant, and the stores refuse a duplicate across experiments. Why: the
    registry's cross-experiment rules (no reusing ids or reservations to blur trial
    counts) depend on it, and there is no real data yet, so tightening now is free.
    Not 'per experiment': that loses those cross-experiment protections."
    Implemented by: T044 (the experiment and assessor stores).

81. **Who logs protocols and candidates.**
    Question: "Decision 81: nothing writes protocols and candidates to the registry
    log yet; the only place they're stored is when the assessor binds a run. Who
    should log them?"
    Selected label: "Mirror at bind, checked (Recommended)".
    Option text as presented: "When the assessor binds a protocol and candidate
    lineage, append them to the log in the same step, re-checking the candidate's
    evidence the way the registry requires. Why: the log then has every entry a
    verdict depends on, written by the one path that already validates them, with
    no separate registration step to forget. Rechecking evidence closes the gap that
    bind doesn't check it today."
    Implemented by: T044 (the assessor store and the registry log).
