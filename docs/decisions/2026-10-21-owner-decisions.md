# Owner decisions, 2026-10-21

The repository owner made these decisions on 2026-10-21 in a recorded quiz. The
owner selected an option label; each option also carried a description, written by
the orchestrator and shown to the owner. Both are quoted verbatim below and kept
separate: the label is what was selected, the option text is what was presented.
"Implemented by" names the task that carries, or will carry, the decision. Nothing
below waives a test, gate or spec requirement. Numbering continues from
`2026-10-20-owner-decisions.md` (decisions 1 to 12). In the quoted option text,
"your decision 7" (decision 18) is decision 7 of `2026-10-20-owner-decisions.md`,
and "your decision 9" (decision 20) is decision 9 of `2026-10-15-owner-decisions.md`.

## Session contract

13. **`csrf_token` in readSession.**
    Question: "The session read returns a `csrf_token`, which the contract checker
    flags as a sensitive name. How should it be handled?"
    Selected label: "Document it (Recommended)".
    Option text as presented: "Add `csrf_token` to the readSession schema with an
    approved allowlist entry. Why: the browser UI needs it to make step-up and
    mutation calls, and it's only useful together with the HttpOnly session cookie,
    so returning it to the logged-in user is the standard double-submit pattern. Not
    'Remove': that breaks the UI's CSRF defence. Not 'Separate endpoint': it adds a
    route and a round trip for no security gain."
    Implemented by: T055 contract-checker hardening (rule 7 `sensitive_fields`).

14. **Session cookie name.**
    Question: "The contract names the session cookie `pi_session`, but the server
    sets `__Host-pi_session`. Which should be the documented name?"
    Selected label: "__Host-pi_session (Recommended)".
    Option text as presented: "Fix the contract. Why: the `__Host-` prefix makes
    browsers require Secure, Path=/ and no Domain, which blocks subdomain cookie
    injection. That is a real protection for a financial app. Not 'pi_session':
    dropping the prefix weakens security just to match a document."
    Implemented by: a project-contract amendment (T010/T055).

15. **Undocumented session and membership routes.**
    Question: "The server has five session and membership routes that aren't in the
    contract. What should happen to them?"
    Selected label: "Document them (Recommended)".
    Option text as presented: "Write their schemas into a contract amendment, with
    the checker's new rules applied to the reads and owner-approved entries for any
    mutations. Why: the contract is meant to be the complete surface, and routes
    outside it escape the security review. Not 'Remove': the UI uses them for login,
    logout and membership. Not 'Leave': it keeps a hole in the review."
    Implemented by: a project-contract amendment (T010/T055). Any mutation among
    them still needs its own owner-approved allowlist entry.

16. **Sensitive field names.**
    Question: "The checker flags field names containing token, secret, password,
    apikey, privatekey or credential, the spec's six words. Should it flag more?"
    Selected label: "Add passwd/pwd (Recommended)".
    Option text as presented: "Also flag `passwd` and `pwd`. Why: they are common
    password spellings with almost no false positives in a finance schema. Not
    'bearer/jwt too': those overlap with the existing 'token' word and could trip
    legitimate auth documentation. Not 'Spec list only': it lets an obvious password
    field through."
    Implemented by: T055 contract-checker hardening (rule 7).

## Assessor and experiments

17. **Assessor database role.**
    Question: "Assessor tables (holdout reads, scores, receipts) are currently
    written by the general `qw_worker` database role, which can therefore also read
    protected holdout data. Should a dedicated `qw_assessor` role exist?"
    Selected label: "Add qw_assessor (Recommended)".
    Option text as presented: "A separate runtime role that alone writes assessor
    tables and reads holdout data, with qw_worker losing that access. Why: the spec
    requires an independent evaluator, and if the same worker that runs experiments
    can read the holdout, a compromised or buggy experiment path could peek at it.
    Separation turns that into a database-enforced guarantee. Not 'Keep qw_worker':
    it's simpler, but independence rests only on code discipline. Cost: one more DB
    credential for operators to manage."
    Implemented by: T044 (migration and runtime configuration).

18. **New plan over an already-read promotion window.**
    Question: "Holdout reuse is blocked per plan. A new plan covering a promotion
    window whose holdout was already read for an earlier plan: what happens?"
    Selected label: "Refuse (Recommended)".
    Option text as presented: "Refuse the new plan with `holdout_window_used`. Why:
    your decision 7 says 'the protected holdout is never re-used'. A fresh plan over
    the same window is exactly the reuse that loophole allows: you could keep writing
    new plans until one scores well. Not 'Flag': a flag still lets overfit results
    through to review."
    Implemented by: T044.

19. **Experiment lineage link.**
    Question: "Should an experiment record which earlier experiment it re-tests (a
    lineage link), so repeated attempts at the same idea are visible and counted
    toward multiple-testing correction?"
    Selected label: "Add lineage link (Recommended)".
    Option text as presented: "An optional, immutable `retest_of` experiment id; the
    assessor counts the whole chain's trials for multiple-testing correction. Why:
    without it, running ten near-identical experiments and keeping the best one hides
    data mining, which is the overfitting the independent assessor exists to catch.
    Not 'No link': it relies on the trial ledger alone, which can't connect separate
    experiments."
    Implemented by: T044.

## Retention and privacy

20. **Implementer retention defaults.**
    Question: "For data classes with no legal period (security, policy, research,
    market, evidence, improvement, simulation, operations), the implementer defaulted
    to delete on request with no age expiry; provider payloads 48 h; logs 30 days.
    Confirm?"
    Selected label: "Confirm defaults (Recommended)".
    Option text as presented: "Why: it matches your decision 9, keeping personal
    content only as long as needed, without inventing legal periods (spec §12 leaves
    those to counsel and provider terms). 48 h and 30 days line up with the existing
    idempotency and backup windows. All values stay adjustable config. Not 'Add age
    limits': picking periods now would be guessing at legal obligations."
    Implemented by: T058 (`retention.IMPLEMENTER_DEFAULTS` become owner-confirmed).

21. **Financial journal deletion and tombstone retention.**
    Question: "Can financial journal records be deleted on request (for example on
    account deletion), and how long are deletion tombstones kept?"
    Selected label: "Retain; tombstones 7y (Recommended)".
    Option text as presented: "The journal stays append-only and is never deleted on
    request, only expired by a retention period you set later. Tombstones are kept 7
    years, the same as audit. Why: the spec's financial invariants need an immutable
    journal for reconciliation and tax evidence, and tombstones prove a deletion
    happened, so they should outlive any backup that still holds the data. Not
    'Deletable': it breaks reconciliation and audit."
    Implemented by: T058. The journal's own retention period is still open.

22. **Legal hold roles.**
    Question: "Which roles may place and release legal holds, which stop deletion
    and purges?"
    Selected label: "Owner places, 2 release (Recommended)".
    Option text as presented: "The tenant owner or the installation operator can
    place a hold, and releasing one needs both, each with step-up. Why: placing a
    hold is safe because it only keeps data longer, so it should be easy. Releasing a
    hold allows deletion, so it needs two people to prevent quiet evidence
    destruction. Not 'Operator only': in self-hosted mode you are both roles anyway,
    but on hosted a tenant must be able to protect their own data."
    Implemented by: T058.

## Releases

23. **Maintainer and operator separation.**
    Question: "Release install currently refuses when the installing operator is the
    same person who signed the release as maintainer. In self-hosted personal use you
    may be both. Keep this separation?"
    Selected label: "Keep; allow solo opt-in (Recommended)".
    Option text as presented: "Keep separation by default, but let a self-hosted
    installation with a single operator explicitly configure 'solo maintainer'. Every
    install is then recorded as self-approved. Why: on hosted, separation stops one
    compromised account from both signing and shipping code. In your personal pilot
    there is literally one person, so a hard rule would block you entirely. An
    explicit, recorded opt-in keeps the audit honest. Not 'Always enforce': it blocks
    the solo pilot. Not 'Drop it': it weakens hosted."
    Implemented by: T051.

## Session and membership mutations (second quiz, 2026-10-21)

These four decisions answer the proposals in
`docs/decisions/2026-10-21-contract-0.8.0-session-mutation-proposals.md`. An
`Approves:` line names one operation that the selected label approves. The label
"All four" covers the four operations the option text names, and nothing else. The
`Approves:` lines are not part of the option text: they are the orchestrator's
derivation, taking the method and path of each operationId the option text names
from sections A to D of the proposals file. Decision 24 approves the operations
only; decision 25 settles how they meet the contract checker.

24. **Session and membership mutations.**
    Question: "Which of the four undocumented session/membership mutations should be
    documented in the contract (exact shapes in
    docs/decisions/2026-10-21-contract-0.8.0-session-mutation-proposals.md)?"
    Selected label: "All four (Recommended)".
    Option text as presented: "Approve createSession (login), revokeSession
    (logout), stepUpSession and changeMembershipRole. Why: the UI uses all four. They
    touch no broker, money or order surface. Leaving them undocumented keeps them
    outside the contract checker's security rules, the gap decision 15 meant to
    close. Not 'only some': each one left out stays unreviewed."
    Approves: `POST /session` (`createSession`)
    Approves: `DELETE /session` (`revokeSession`)
    Approves: `POST /session/step-up` (`stepUpSession`)
    Approves: `PATCH /memberships/{user_id}` (`changeMembershipRole`)
    Implemented by: a project-contract amendment (T010/T055), with decision 25.

25. **Checker conflicts for those mutations.**
    Question: "The proposed mutations conflict with the hardened checker: `password`
    and `X-CSRF-Token` trip the sensitive-name rule, login has no
    session/CSRF/idempotency, and logout returns 204 with no body. How should these
    be resolved?"
    Selected label: "Entries + narrow exemptions (Recommended)".
    Option text as presented: "For `password` and `X-CSRF-Token`: per-operation
    allowlist entries citing your approval. For login's `security: []` and missing
    headers, and logout's 204: narrow checker exemptions that only work for an
    operation explicitly listed and approved. Why: the checker stays strict for
    everything else, and each exception is recorded. Not 'change the server': a 204
    logout and an unauthenticated login are correct HTTP, and changing them breaks
    the UI for no security gain. Not 'blanket exemption': it would weaken the checker
    for future routes."
    The two `Exempts:` lines below are the orchestrator's derivation from the
    selected option ("For login's `security: []` and missing headers, and logout's
    204"); the quoted question and option text above are unchanged.
    Exempts: `unauthenticated_login` (`createSession`)
    Exempts: `no_content` (`revokeSession`)
    Implemented by: T055 (`tools/check_contracts.py` rules 2, 6 and 7).

26. **`display_name` in the role-change response.**
    Question: "The role-change response always sends `display_name: null`, while the
    members list sends the stored name. What should happen?"
    Selected label: "Fix the server (Recommended)".
    Option text as presented: "Return the stored name, and reuse the list's item
    schema. Why: a null that means 'not fetched' looks like 'no name' to the UI and
    is an inconsistency, not a design. One schema for one object is simpler to
    review. Not 'document null': that freezes a quirk into the contract."
    Implemented by: T010 (`apps/api`), with decision 24.

27. **Health probe and policy list.**
    Question: "Two more served routes are outside the contract: the unauthenticated
    liveness probe `GET /health/live`, and the policy list `GET /policies` (T019).
    What should happen to them?"
    Selected label: "Health out, policies in (Recommended)".
    Option text as presented: "Keep `/health/live` outside the contract as an
    infrastructure probe (no session, no data), and document `GET /policies` through
    a T019 amendment. Why: the health check carries no user data and must work
    without a login for container orchestration. The policy list is real API surface
    that should get the same review as every other read. Not 'both in': the health
    probe can't meet the session rules and doesn't need to."
    Implemented by: T019 (contract amendment for `GET /policies`).

## Third and fourth quizzes (2026-10-21)

28. **Scope of the used-holdout-window refusal (decision 18).**
    Question: "Decision 18 refuses a new plan over an already-scored holdout window.
    Within what scope? (A 'baseline' is the version a candidate is compared
    against.)"
    Selected label: "Tenant + target (Recommended)".
    Option text as presented: "Any plan for the same tenant and improvement target
    that overlaps a scored window is refused, whatever its baseline. Why:
    otherwise, once v1 is promoted on window W, a new plan using v1 as baseline can
    score on W again. That is the same 'keep testing until one scores well'
    loophole decision 18 closes. The cost: every baseline of a target must use a
    fresh window, which needs more data. Not 'Tenant + target + baseline': it
    leaves that successor gap open."
    Implemented by: T044.

29. **Multiple-testing adjustment for the retest chain (decision 19).**
    Question: "Decision 19 counts trials across a whole retest chain, but the spec
    names no multiple-testing adjustment to apply them to. Today the count is
    stored and shown as a limitation, and verdicts don't change. What should
    happen?"
    Selected label: "Bonferroni on promotion (Recommended)".
    Option text as presented: "Divide the promotion test's significance level by
    the chain-wide trial count (alpha / chain_trials) before a candidate can be
    accepted. Why: Bonferroni is conservative and simple to audit, and it makes the
    chain count actually change verdicts, which is decision 19's purpose. With few
    trials it barely tightens the threshold; with many it forces a real edge. Not
    'Holm or BH': stronger, but they need the full set of p-values across the
    chain, which isn't stored. Not 'Report only': repeated testing can still slip
    through."
    Implemented by: T044.

30. **Tombstone retention floor (decision 21).**
    Question: "Decision 21 kept deletion tombstones for 7 years, but an operator's
    config can shorten that to just above the 30-day backup window. Should 7 years
    be a hard minimum?"
    Selected label: "7-year floor (Recommended)".
    Option text as presented: "Config may lengthen but never shorten tombstones
    below 2557 days. Why: tombstones prove a deletion happened, for audits and
    disputes, so a shorter period weakens that proof. Unlike decision 20, decision
    21 didn't say the value stays adjustable. Not 'Adjustable': an operator could
    quietly cut audit evidence."
    Implemented by: T058.

31. **Legal hold release on a one-person install (decision 22).**
    Question: "Releasing a legal hold needs both the tenant owner and the operator,
    as two different people. On a one-person self-hosted install you hold both
    roles and could never release a hold. Allow a solo release?"
    Selected label: "Solo opt-in, delayed (Recommended)".
    Option text as presented: "Mirror decision 23: a self-hosted install with a
    single configured operator can explicitly enable 'solo hold release'. A release
    then needs two separate step-ups at least 24 hours apart, and is recorded as
    solo. Why: one person must not be locked out of their own data forever, while
    the delay stops an impulsive or hijacked-session release. Not 'Never': it
    bricks deletion for the personal pilot. Not 'Plain solo': one compromised
    session could destroy held evidence."
    Implemented by: T058.

32. **Derived `Exempts:` lines in decision 25.**
    Question: "To tie the two contract exceptions to your decision 25, I added two
    derived lines to it: `Exempts: unauthenticated_login (createSession)` and
    `Exempts: no_content (revokeSession)`, plus a sentence saying they're my
    derivation. Your quoted text is unchanged. The checker now treats these lines as
    your approval. Do you confirm them?"
    Selected label: "Confirm (Recommended)".
    Option text as presented: "The two lines match exactly what your option text
    approved: login without a session, logout's empty 204, and nothing else. Why:
    the checker needs a machine-readable line to pin each exception to its one
    operation, and the reviewer confirmed neither can be reused elsewhere. Not
    'Reject': the exceptions would have no approval, and contract 0.9.0 couldn't
    document login or logout."
    Implemented by: T055 (the lines are added to decision 25 by the contract 0.9.0
    pull request).

33. **Releases before an installation-scope assessor store.**
    Question: "Releases are checked against the assessor's verdict as supplied by
    the caller, because no installation-wide assessor store exists yet (only a
    per-tenant one). Until one exists, what should happen to new releases?"
    Selected label: "Record, marked (Recommended)".
    Option text as presented: "Keep recording releases, re-checked against the
    caller's registry, and mark them 'verdict caller-supplied'. Why: nothing can be
    deployed yet anyway, since signing uses test-only keys until the T009
    dependency choice. Blocking now only stops testing the release path. The mark
    keeps it honest. Not 'Refuse': it blocks development for no safety gain
    today."
    Implemented by: T051.

34. **Reference and release data retention.**
    Question: "Reference data (shared lookups like securities) and release evidence
    (signed releases, installs, rollbacks) are kept indefinitely and never deleted
    on request. These are implementer defaults; decision 20 didn't cover them.
    Confirm?"
    Selected label: "Confirm (Recommended)".
    Option text as presented: "Why: reference data isn't personal, and release
    evidence is the audit trail of what code ran; deleting either breaks
    reproducibility. Neither holds personal content that 'keep only as long as
    needed' targets. Not 'Expire': it would break rollback history and
    reconciliation."
    Implemented by: T058.
