# Owner decisions, 2026-10-15

The repository owner made these decisions directly on 2026-10-15. Each decision quotes
or closely follows the option text the owner selected; rationale lines are the
implementer's, not the owner's. "Implemented
by" names the task or pull request that carries, or will carry, the decision. Nothing
below waives a test, gate or spec requirement.

## Contract decisions (in effect now)

1. **Project contract 0.3.0 and 0.4.0 are confirmed.**
   Rationale: both were in use by the API and web guards while marked "proposed, pending
   owner confirmation".
   Implemented by: this change (`contracts/amendments.json` `versions`); the amendments
   themselves landed with T036 increment 4 and T053-api reads increment 2.

2. **If-Match: 412 Precondition Failed for a stale If-Match, and 428 Precondition
   Required when it is missing; recorded as a contract amendment.** The OpenAPI If-Match
   descriptions say 409; this decision replaces that text.
   Rationale: 412 and 428 are the HTTP-standard codes, and the routes that take If-Match
   today (T010 memberships, T019 policies, T024 watchlists, T056 notifications) already
   answer them; the amendment applies the convention to every route and treats a
   malformed If-Match like a stale one (412).
   Implemented by: this change (project contract 0.5.0 `changes`). Those routes already
   conform; routes added later (for example adoptStrategy, T027) must follow it.

3. **Watchlist `review_at` is settable through the API; `needs` is not.** `review_at`
   is an optional, nullable field on the request `WatchlistEntry` used by
   addWatchlistEntry and updateWatchlistEntry. `needs` stays server-derived.
   Rationale: spec §2 and §13 give an entry a review date, the response entry already
   carries `review_at`, and the closed request schema left no way to set it (T024
   increment 3, decision 1).
   Implemented by: this change: project contract 0.5.0 (`tools/project_contract.py`
   `request()`) and T024 increment 4 (routes, store and `0024_watchlist_review_at.sql`;
   docs/receipts/T024.md).

## Recorded for later implementation (not implemented now)

| # | Decision | Rationale | Implemented by |
|---|---|---|---|
| 4 | An external notification channel (email, push) requires step-up authentication. | External channels send outside the application, so enabling one is a sensitive change. | T056 |
| 5 | Claim text is withheld when rights block the evidence it cites. | A claim must not leak text from evidence that the user has no right to display. | T036 |
| 6 | Only an operator can grant rights entitlements. | Rights grants need permission evidence and attribution (R094), which a tenant user cannot attest. | T021 |
| 7 | The 60 s back-dating window for server-recorded times stays. | It allows for commit latency while keeping `recorded_at` close to the database clock. | No change; existing store guards (for example `0022_watchlists.sql`) |
| 8 | Any member may pause; resume is owner or admin only, with step-up. Note: the question offered to the owner assumed a tenant admin role, but none exists (memberships are owner or member). Pause already matches; resume stays owner-only with step-up until the owner decides whether to add an admin role. | Pausing only reduces risk; resuming re-enables actions and needs stronger authority. | T020 (blocked on the admin-role question) |
| 9 | Retention defaults are minimal, with legal hold, held as configuration. | Keep the least data by default, while a hold can still preserve records. | T058 |
| 10 | The selected option was labelled "Seed with free/synthetic defaults"; its description named free or delayed data feeds. Decision: seed all six families with free or synthetic defaults and a zero budget; all marked not_tested and not action-eligible. Text of the option the owner chose: "Seed all six with conservative defaults: free or delayed data feeds, the ai_thesis family pointed at the model gateway with a zero budget so it can't spend, and every family marked not_tested and not action-eligible." | All six families must exist in the first release without spending or claiming qualification. | T027 (seeding), T028 to T031 (families) |
| 11 | `readJob` is to be built. | 202 `job` responses are currently not pollable (T043, T024 increment 2). | T025 |
| 12 | The IMPROVABLE list stays at all five modules, with quant review for the returns and benchmark modules. | Keeps the code-improvement target broad, with numerical changes checked by a quant reviewer. | T049 |
| 13 | The Python candidate rootfs is built in CI and pinned by digest. | Candidate runs need a reproducible, scanned rootfs (T007 open item). | T049, T007 |
| 14 | QUAL17 is qualified by the owner running a script on the reference Linux. | The gVisor isolation claim needs evidence from the owner's reference host. | T007 (script), owner (run) |
| 15 | CI mirrors the PostgreSQL image into GHCR. | Removes the dependency on a third-party registry for the test database. | PR #111 (`ci-ghcr-postgres`) |
| 16 | GitHub push protection is to be enabled by the owner. | Secret scanning at push time is an account setting that only the owner can change. | Owner action |
