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
