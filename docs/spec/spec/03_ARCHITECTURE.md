# 3. Architecture and module contracts

## Selected architecture

A modular monolith with separately permissioned workers is the starting architecture. Typed Python owns the independent domain/quant core; TypeScript/React owns the responsive interface; PostgreSQL owns transactional state and durable coordination. Permitted immutable analytical/evidence objects use filesystem storage locally and object storage in hosted operation. Do not add Kafka, a service mesh, a mandatory vector database or microservices without a measured need and an ADR.

Proposed repository modules after scaffold: `packages/domain`, `packages/portfolio`, `packages/quant`, `packages/strategies`, `packages/evidence`, `packages/ai`, `packages/improvement`, `packages/adapters`, `apps/api`, `apps/worker`, `apps/web`, `ops`, `tests`. These directories are implementation targets, not present application code in this bundle. Dependency selection/version pinning happens during T009 against current official docs, license/security review and the actual environment.

The core imports no web/UI framework or provider SDK. It accepts typed immutable snapshots and returns results plus provenance. Provider SDKs stay behind adapter interfaces. Database repositories and object stores are injected. A browser is never needed to run a strategy, reconcile a ledger, evaluate a candidate or execute a test.

## Three authority paths

**Live:** ingestion → normalization → financial/market snapshot → strategy/research → evidence/reviewer checks → deterministic final validation → atomic publication/commitments → notification.

**Experiment:** authorized evidence/corpus snapshot → candidate generator → isolated candidate execution → fixed independent assessment → candidate evidence record. No production write credentials, live broker tools, protected acceptance controls or signing keys.

**Release:** authorized human review → immutable approved version/package → integrity verification → operator install → user adoption when strategy semantics change → monitoring/rollback. The release service cannot infer a human decision from an LLM output.

## Domain interfaces

| Interface | Inputs | Outputs / invariants |
|---|---|---|
| AccountConnector | Secret reference, read-only operation, cursor, entitlement | Observations with source/account identity, time and coverage; never direct journal mutation |
| MarketDataAdapter | Explicit feed spec, instruments, interval/as-of | Typed observations with event/receipt time, gaps and revision flags |
| LedgerService | Reconciled command, expected revision, idempotency key | Committed journal revision or conflict; balanced monetary/unit postings |
| StrategyEngine | Strategy version, input snapshot, approved config | Deterministic candidate set/rejections and calculation trace |
| ResearchEngine | Mandate, evidence permissions, tool budget | Structured thesis/claims, assumptions, limitations and candidate |
| DecisionEvaluator | Separately constructed evidence and candidate context | Accept/revise/reject/research-only with supported reasons; no final authority |
| RiskValidator | Final proposed set, policy/adoption, current financial/data revisions | All checks, blockers and allowed sizes; deterministic final gate |
| ProposalPublisher | Checked revisions and reserved planning capacity | Atomic persisted proposal set + outbox event, or retryable conflict |
| ExperimentAssessor | Candidate, protected test/version manifest | Evidence report; no deployment side effects |
| ReleaseController | Candidate hash, signed human decision, compatibility | Available release/adoption state; rollback lineage |

## Durable coordination

Use at-least-once jobs with an idempotency key scoped to tenant/operation/input revision. Jobs have leases, heartbeats, attempt limits, next-available time and an explicit dead-letter state. Exponential backoff with jitter and provider-specific rate budgets prevent retry storms. An outbox record is committed in the same transaction as the business state; publishing is retried and consumers deduplicate by event ID. Do not promise globally exactly-once delivery.

Priority order: safety invalidations/reconciliation → current monitoring and required verification → interactive research → scheduled research → improvement experiments. Weighted tenant fairness prevents one user monopolizing workers. Experimental load has a separate resource pool even on one machine. Quota counters reserve before dispatch and release/reconcile safely after errors.

## Atomic final decision

Read a portfolio snapshot with account, policy, entitlement and strategy-adoption revisions. Evaluate outside long database locks. Before publication, lock only the involved account/currency/sleeve planning rows in a deterministic order; confirm revisions and valid market time; recompute affordable amounts against current commitments; write proposal-set decisions and outbox atomically. A stale revision returns conflict and triggers bounded reevaluation. Never reserve funds at a broker: this is only application planning state.

## Storage and access

Tenant-owned keys include tenant identity in foreign-key relationships; clients cannot supply a tenant ID to access another scope. Global public reference data and private tenant data have separate access policies. UUID identifiers are unguessability aids, not authorization. Price/time-series retention follows rights and workload policy; essential ledger/evaluation lineage remains referentially intact or marked redacted/unavailable when deletion applies.

## API conventions

OpenAPI 3.1 in `contracts/openapi.yaml` describes a design contract, not running endpoints. JSON money/quantities are decimal strings. Date-times include timezone offsets and normalize to UTC; exchange calendar IDs preserve local session meaning. Reads use cursor pagination, stable sorting and coverage metadata. Mutating application commands require idempotency; revisioned edits use `If-Match`. Long work returns 202 + a job resource. No broker order routes are defined.

Session authentication, CSRF protection for browser writes, membership checks, rate limits, structured errors and audit events are mandatory. Provider auth is separate from application identity. A shared error envelope includes code, retryability, correlation ID and nonsecret field errors. Secrets must not appear in payload examples or logs.
