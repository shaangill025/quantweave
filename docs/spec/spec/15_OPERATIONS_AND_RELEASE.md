# 15. Deployment, reliability, cost and release operations

## Supported deployment target

A single-machine Linux container deployment is the first self-hosted reference. Qualify actual CPU/RAM/disk and supported host/kernel/runtime versions in W0; candidate evaluation needs extra bounded resources. No mandatory GPU, Kubernetes, vendor account or proprietary signal service. Non-Linux hosts may use an operator-managed compatible Linux environment after testing; do not claim native gVisor parity without evidence.

Logical processes: API, standard worker, market ingestion, model gateway, isolated experiment runner, PostgreSQL, static/web interface and optional notification gateway. Secrets, backing storage and roles differ by process. Optional local models are independently configured and have their own hardware requirements. A local filesystem storage adapter replaces hosted object storage without changing domain logic.

Hosted deployment separates tenant/application data, backups and monitoring. Managed service billing is not a financial-ranking input. Provider costs are visible separately. No pricing or scaling promise is inferred from this specification.

## Measurable reference workload

One self-hosted active user with up to 10 accounts, 250 distinct tracked instruments including contracts and 100000 activity records. Hosted reference is 100 onboarded/background-monitored users and 20 simultaneous interactive sessions. Streaming capacity remains governed by each user's actual entitlements; 250 tracked does not mean live marketwide scanning. Personal USD100 inference budget is not a budget for 100 customers.

Combined tests run imports/reconciliation, monitoring, UI research, simulations and bounded improvement concurrently. Measure sustained rates and bursts, memory/disk growth, queue age, cost reservations and tenant fairness. Mark actual hardware/runtime and data-source configuration on every result.

## Timing and service targets

P95 factual event publication <=5 seconds after receipt; P95 rules-only actionable proposals <=15 seconds including mandatory checks. AI intraday deadline is the earlier of the strategy's useful window and a provisional 60 seconds from original trigger. Source freshness and remaining action window are independent conditions. Count rejected/expired/deadline-missed candidates in the denominator and report why work was dropped.

Target hosted application availability is 99.9% monthly for its defined service surface; hosted RPO 15 minutes and RTO four hours under tested scenarios. These are planning targets, not measured SLAs. Distinguish application health, provider health and end-to-end capability availability. A healthy server without required feed data cannot show monitoring as fully healthy.

## Budget ledger

For the personal pilot: USD100/month maximum incremental inference and at most USD20/month for improvement. Existing subscriptions, hosting, market-data upgrades and other approved operational charges are separate. Limits apply to application-initiated requests, not unrelated spending with the same key elsewhere.

Before a call, atomically reserve a conservative maximum including bounded output/tool/retry charges. Multiple workers cannot reserve the same remaining allowance. After completion reconcile actual usage; unknown billing outcomes retain a conservative reservation pending reconciliation. No silent auto-top-up/provider switch. Missing authorized rate information blocks a billable call or uses a separately approved nonbillable path; do not guess cost to pass a budget test.

Experiments get lower priority and independent CPU/memory/storage/time ceilings. Disabling all experiments to save money fails the demonstrated-improvement requirement; measure representative completed workloads and use efficient/cadence-limited runs within approved limits. Expanding budget requires separate authorization, not an agent recommendation.

## Recovery and rollback

Backups are encrypted with operator-controlled keys; recovery drills verify data integrity, current deletion tombstones, secret reauthorization and projection rebuild. The journal is not reconstructed from model messages. Record backup completion/cutoff, restore point, measured outage and recovery data loss.

Release packages have manifest, provenance, dependency inventory, approval identity, schema compatibility and integrity signature. Migration follows expand/contract or another reviewed safe plan. No experiment self-installs. Rollback selects compatible approved artifacts; unsafe downgrade triggers suspension/forward repair, not data corruption. Preserve original decision records and public release notes.

## Operational runbooks required

Provider outage/revocation, wrong/corrected prices, partial broker sync, unresolved account conflict, quota exhaustion, model timeout, risk-pause/resume, credential compromise, cross-tenant incident, sandbox breach, failed migration, restore and deletion reappearance. Each runbook names trigger, immediate containment, affected-capability status, evidence preservation, recovery checks and user communication.

A signed release is not automatically ready for commercial use. All gates applicable to each deployment and user jurisdiction must pass; opening one market while silently dropping the other agreed commercial scope is not the full release. Internal development demos may proceed with unmistakable limitations and no production claim.
