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
