# 12. Threat model, privacy and trust boundaries

## Assets and adversaries

Protect portfolio/transaction history, model/broker credentials, user policy and approvals, private learning, evidence integrity, financial calculations, provider entitlements, software signing keys and budgets. Consider malicious external content, compromised providers, hostile CSV/archive files, malicious tenants, accidental privileged roles, generated-code misuse, unauthorized support access and compromised dependencies.

Trust boundaries: browser↔application API; tenant↔tenant; application↔provider; untrusted evidence↔model; model↔read tools; live services↔experiment sandbox; candidate↔assessor; assessor↔human release authority; backup/export↔restored application. Authentication does not collapse these boundaries.

## Baseline controls

Use TLS for exposed interfaces, secure HttpOnly session cookies, appropriate SameSite and CSRF controls, restrictive CORS, server-side authorization on every object/action, session expiry/revocation and step-up authentication for sensitive actions. Store password verifiers using a reviewed current standard or use a qualified external identity provider; self-hosting must not require project-vendor identity. No frontend local storage of broker/model secrets.

Tenant identity is derived from authenticated membership. Enforce tenant scope in application authorization, database composite relationships/policies, object paths, retrieval indexes, jobs and caches. PostgreSQL service roles are nonowner and lack superuser/BYPASSRLS permissions; migration roles are separately controlled. Database owners can ordinarily bypass row policies, so tests must use production-like roles rather than a convenient administrator. [SRC-15]

Encrypt secrets at rest with per-installation/operator-controlled key management and documented rotation/recovery. Retrieval/model workers receive opaque secret references or mediated calls, not plaintext credentials. Hosted operators must audit exceptional access. Do not bundle universal secret keys in a container or require a hidden vendor bootstrap call for self-hosting.

## Broker and model controls

Request read-only scopes where available. Connector operation allowlists block broker order creation, amendment/cancel, withdrawals and unauthorized exercise even if a broad credential technically supports them. No order tool exists in a model registry. A virtual-order endpoint writes only the simulator ledger. Auth routes and provider financial-use terms must both pass qualification.

Never collect prohibited consumer-session tokens, scrape authenticated services as an undocumented fallback, or use another tenant's credentials to overcome a limit. Costs are reserved transactionally before calls; tool recursion/retries have hard ceilings. A model-generated budget override is invalid.

## Untrusted input controls

Controlled HTTP retrieval blocks private/link-local/metadata addresses, disallowed schemes, DNS rebinding, redirects to forbidden targets, oversized content, MIME mismatches and decompression bombs. Parse HTML/documents without executing scripts. Public URLs are not intrinsically trusted. Imported files get type/size/row/encoding checks; CSV output neutralizes spreadsheet formula injection. Archive import rejects traversal, symlinks, absolute paths, duplicate paths and executable auto-activation.

External text is evidence, never instructions. Keep prompts and retrieved content separated with explicit trust tags. Model proposals must pass schema, claims, numerical and final authorization checks. The control plane cannot be overwritten by a clever cited passage. No inaccessible chain-of-thought is required for auditing; inspect structured tool/evidence records.

## Sandbox and supply chain

Experimental code receives a dedicated sandbox role/filesystem/network policy; no host Docker socket or production environment. Test attempted host-file reads, process escape, network exfiltration, fork/resource exhaustion, dependency substitution and signing-key access. Scans and a named runtime are supporting evidence, not a security guarantee. Record actual version, host configuration and attack results.

Dependencies, prompts, datasets, models and community skills have separate provenance/license inventories. Require lockfiles, package integrity checks and a software bill of materials for releases. A code change cannot modify protected acceptance controls, authorization policy or signing keys through an ordinary improvement task. Such modifications require independent privileged governance outside the candidate pathway.

## Privacy and lifecycle

Collect only information needed for chosen capabilities. Partial portfolio disclosure is allowed; broad financial suitability claims are constrained accordingly. Private learning is tenant/installation-local by default. Explicit consent is purpose-, data-category-, recipient- and version-specific; revocation prevents further use and triggers the defined derivative-state handling.

Retention classes distinguish financial records, recommendation history, licensed evidence, transient provider payloads, logs, experiments and backups. Do not invent one universal legal retention period for Canada/US. Product-specific counsel and provider terms establish applicable obligations; until configured, commercial activation is gated. Logs use redacted identifiers and no prompts/raw financial data by default.

Deletion applies to source objects, retrieval indexes, caches, private learned state and exports under policy. A trained derivative that cannot be selectively unlearned must be retired/rebuilt or retained only under a lawful disclosed exception; do not promise impossible instantaneous unlearning. Deletion tombstones are reapplied on restore. Evidence removal changes reproducibility status and may invalidate current proposals. Legal holds are explicit, scoped and auditable.

## Incident procedures

Credential exposure: revoke/rotate, suspend affected connector, assess access and notify under qualified policy. Cross-tenant exposure: disable affected paths, preserve permitted forensic evidence, determine scope and remediate before reactivation. Wrong/stale data: quarantine source version, invalidate dependent proposals, disclose affected analyses and recompute without rewriting prior knowledge. Sandbox failure: stop experiments and freeze promotion. No remediation includes unauthorized broker actions.
