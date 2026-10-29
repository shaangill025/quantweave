// Guards for reads apps/api serves since T021, T027 and T053-api: GET /capabilities,
// /strategies, /strategies/{id}, /accounts/{id}/ledger and /proposals/{id} (openapi.yaml 1.0.0
// as amended by project contract 0.4.0, owner-confirmed: proposal coverage, nullable lot_policy_id).
// Each refuses a payload the views could not show honestly; decimals stay strings.
import { checks as c, isProposal, pageOf, proposalPage, reasonFor, type Coverage, type Guard, type Page, type Proposal } from "./api";
import type { DecimalString as Dec } from "./wire";

const { str, id, dec, instant, oneOf, nullable: n, arr, obj, coverage } = c;
const bool = (v: unknown) => typeof v === "boolean";
const hash = (v: unknown) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
/** A page whose coverage is present: these views show its reasons, so none may be missing. */
export const covered = <T>(page: Guard<Page<T>>) => ((v: unknown) =>
  page(v) && v.coverage !== undefined) as Guard<Page<T> & { coverage: Coverage }>;

export interface Feature { id: string; enabled: boolean; reason_codes: string[] }
export interface Capabilities { deployment: "hosted" | "self_hosted"; features: Feature[] }
const capabilities = obj({
  deployment: oneOf("hosted", "self_hosted"), features: arr(obj({ id, enabled: bool, reason_codes: arr(id) })),
});
/** Enabled exactly when no reason is given; feature ids are unique. */
export const isCapabilities = ((v: unknown) => {
  if (!capabilities(v)) return false;
  const fs = (v as Capabilities).features;
  return fs.every((f) => f.enabled === (f.reason_codes.length === 0)) && new Set(fs.map((f) => f.id)).size === fs.length;
}) as Guard<Capabilities>;

export const FAMILIES = ["allocation", "quality_value", "income", "swing_momentum", "opening_range_breakout", "ai_thesis"];
export interface Strategy {
  id: string; family: string; version: string; implementation_hash: string; configuration_hash: string;
  required_feed_spec_ids: string[]; policy_requirements: string[]; research_protocol_id: string;
  operational_status: "not_tested" | "passed" | "failed" | "suspended"; evidence_status: string;
  action_eligible: boolean; approved_by: string | null; source_decisions: string[];
}
export const isStrategy = obj({
  id, family: oneOf(...FAMILIES), version: str, implementation_hash: hash, configuration_hash: hash,
  required_feed_spec_ids: arr(id), policy_requirements: arr(id), research_protocol_id: id,
  operational_status: oneOf("not_tested", "passed", "failed", "suspended"), evidence_status: str,
  action_eligible: bool, approved_by: n(str), source_decisions: arr(id),
}) as Guard<Strategy>;
export const isStrategyPage = covered(pageOf(isStrategy));

export type LedgerStatus = "observed" | "accepted" | "conflicted" | "reversed";
export interface LedgerEvent {
  id: string; account_id: string; kind: string; effective_at: string; recorded_at: string; source_id: string;
  source_record_id: string; idempotency_key: string; status: LedgerStatus; reverses_event_id: string | null;
  postings: { book_account: string; commodity: string; amount: Dec }[];
  instrument_movements: { instrument_id: string; quantity: Dec }[];
  lot_policy_id: string | null; economic_annotations: string[];
}
const ledgerEvent = obj({
  id, account_id: id, kind: oneOf("opening_balance", "deposit", "withdrawal", "buy", "sell", "fee", "dividend",
    "interest", "fx_conversion", "transfer", "split", "exercise", "assignment", "expiry", "correction"),
  effective_at: instant, recorded_at: instant, source_id: id, source_record_id: str, idempotency_key: str,
  status: oneOf("observed", "accepted", "conflicted", "reversed"), reverses_event_id: n(str),
  postings: arr(obj({ book_account: str, commodity: str, amount: dec })),
  instrument_movements: arr(obj({ instrument_id: id, quantity: dec })),
  lot_policy_id: n(id), economic_annotations: arr(str),
});
/** At least two postings (ledger_event minItems); a correction, and only a correction, names the
 * event it reverses (the journal enforces kind reversal iff `reverses`, qw_domain.postings). */
const isLedgerEvent = ((v: unknown) => {
  if (!ledgerEvent(v)) return false;
  const e = v as LedgerEvent;
  return e.postings.length >= 2 && (e.kind === "correction") === (e.reverses_event_id !== null);
}) as Guard<LedgerEvent>;
export const isLedgerPage = covered(pageOf(isLedgerEvent, () => ["lot_policy_id"]));

/** readProposal (0.4.0): the item carries its own coverage, which names each of its nulls. */
export const isProposalDetail = ((v: unknown) => {
  if (!isProposal(v)) return false;
  const own = (v as { coverage?: unknown }).coverage;
  return own !== undefined && coverage(own) && proposalPage({ items: [v], next_cursor: null, coverage: own });
}) as Guard<Proposal & { coverage: Coverage }>;

// listExperiments (openapi.yaml 1.0.0, experiment.schema.json as amended by project contract 0.6.0:
// item coverage, states blocked and insufficient_evidence, parent_optimizer_version null except for
// the optimizer). A build that does not serve it shows its not_found plainly. The schema carries ids
// and lineage fields only: no scorecard, verdict, budget amount, approval or installation record.
export const TARGETS = ["personal_adaptation", "research", "strategy", "evaluator", "application_code", "recursive_optimizer"] as const;
export type Target = (typeof TARGETS)[number];
export const EXPERIMENT_STATES = ["proposed", "authorized", "running", "evaluating", "awaiting_review", "rejected",
  "approved", "promoted", "rolled_back", "failed", "cancelled", "blocked", "insufficient_evidence"] as const;
export interface Experiment {
  id: string; target: Target; scope: "tenant" | "installation"; scope_id: string;
  state: (typeof EXPERIMENT_STATES)[number]; baseline_version: string; candidate_version: string; hypothesis: string;
  protocol_id: string; protected_assessor_version: string; data_snapshot_ids: string[]; budget_reservation_id: string;
  sandbox_profile_id: string; candidate_hash: string; evaluation_receipt_ids: string[];
  parent_optimizer_version: string | null; next_cycle_run_id: string | null; requires_human_promotion: true;
  release_receipt_id: string | null; coverage: Coverage;
}
const isExperiment = obj({
  id, target: oneOf(...TARGETS), scope: oneOf("tenant", "installation"), scope_id: id,
  state: oneOf(...EXPERIMENT_STATES), baseline_version: id, candidate_version: id, hypothesis: str, protocol_id: id,
  protected_assessor_version: id, data_snapshot_ids: arr(id), budget_reservation_id: id, sandbox_profile_id: id,
  candidate_hash: hash, evaluation_receipt_ids: arr(id), parent_optimizer_version: n(id), next_cycle_run_id: n(str),
  requires_human_promotion: (v) => v === true, release_receipt_id: n(str), coverage: (v) => v !== undefined && coverage(v),
}) as Guard<Experiment>;
/** Only the optimizer has a parent optimizer, and the item's coverage names the parent exactly when it
 * is null; coverage is complete exactly when it gives no reason. */
const consistent = (e: Experiment) =>
  (e.target === "recursive_optimizer") === (e.parent_optimizer_version !== null) &&
  (e.parent_optimizer_version === null) === (reasonFor(e.coverage.reasons, e.id, "parent_optimizer_version") !== undefined) &&
  (e.coverage.status === "complete") === (e.coverage.reasons.length === 0);
/** Experiment ids are unique within a page, so lineage links cannot be ambiguous. */
export const isExperimentPage = ((v: unknown) => {
  if (!covered(pageOf(isExperiment))(v) || !v.items.every(consistent)) return false;
  const ids = v.items.map((e) => e.id);
  return new Set(ids).size === ids.length;
}) as Guard<Page<Experiment> & { coverage: Coverage }>;

// Governance reads (T055 governance panels): listReleases (project contract 0.12.0, releasePage),
// listDeploymentEvents (0.13.0, deploymentEventPage), readApprovalClasses (0.7.0, approval_classes)
// and listBudgets (openapi.yaml BudgetSummaryPage with the C-02 coverage). The same minimal shape
// checks: each refuses what the views could not show honestly.
export const APPROVAL_CLASSES = ["maintainer_release", "operator_install", "user_strategy_adoption"] as const;
const component = (v: unknown) => typeof v === "string" && v.length <= 161 &&
  /^[a-z_]{1,32}:[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(v);
const int1 = (v: unknown) => typeof v === "number" && Number.isInteger(v) && v >= 1;
const unique = (xs: readonly string[]) => new Set(xs).size === xs.length;
const uniq = (c: (v: unknown) => boolean) => (v: unknown) => arr(c)(v) && unique(v as string[]);
const itemCoverage = (v: unknown) => v !== undefined && coverage(v);
/** Coverage is complete exactly when it gives no reason. */
const honest = (cv: Coverage) => (cv.status === "complete") === (cv.reasons.length === 0);
/** Every null among `fields` is named by the item's own coverage. */
const named = (item: object, cv: Coverage, fields: string[]) => {
  const r = item as Record<string, unknown>;
  return fields.every((f) => r[f] !== null || reasonFor(cv.reasons, String(r.id), f) !== undefined);
};

export interface Release {
  id: string; component: string; target: Target; change_kind: string; experiment_id: string | null;
  candidate_id: string | null; verdict_id: string | null; rollback_version_id: string; candidate_hash: string;
  artifact_hash: string; protocol_hash: string; manifest_hash: string;
  required_approvals: (typeof APPROVAL_CLASSES)[number][];
  compatibility: { min_schema: number; max_schema: number; writes_schema: number };
  strategy_version: string | null; strategy_material_hash: string | null; verdict_source: "caller_supplied";
  signature: { key_id: string | null; algorithm: string; signature_reference: string;
    verification: "verified" | "refused"; reasons: string[] };
  install: { state: "installed" | "head" | "withdrawn" | "paused"; installed_at: string; runnable: boolean;
    self_approved: boolean; reasons: string[] };
  receipt_status: "unavailable"; approved_at: string | null; user_adoption_required: boolean;
  tenant_adoption: { state: "not_adopted" | "active" | "paused"; version: string | null; reasons: string[] } | null;
  coverage: Coverage;
}
const release = obj({
  id, component, target: oneOf(...TARGETS), change_kind: oneOf("configuration", "strategy", "application", "optimizer"),
  experiment_id: n(id), candidate_id: n(id), verdict_id: n(id), rollback_version_id: id, candidate_hash: hash,
  artifact_hash: hash, protocol_hash: hash, manifest_hash: hash,
  required_approvals: (v) => uniq(oneOf(...APPROVAL_CLASSES))(v) && (v as string[]).length >= 1,
  compatibility: obj({ min_schema: int1, max_schema: int1, writes_schema: int1 }),
  strategy_version: n(str), strategy_material_hash: n(hash), verdict_source: oneOf("caller_supplied"),
  signature: obj({ key_id: n(id), algorithm: (v) => typeof v === "string" && /^[a-z0-9][a-z0-9_.-]{0,63}$/.test(v),
    signature_reference: (v) => typeof v === "string" && /^sig-sha256\.[0-9a-f]{32}$/.test(v),
    verification: oneOf("verified", "refused"), reasons: arr(str) }),
  install: obj({ state: oneOf("installed", "head", "withdrawn", "paused"), installed_at: instant, runnable: bool,
    self_approved: bool, reasons: arr(str) }),
  receipt_status: oneOf("unavailable"), approved_at: n(instant), user_adoption_required: bool,
  tenant_adoption: n(obj({ state: oneOf("not_adopted", "active", "paused"), version: n(str), reasons: arr(str) })),
  coverage: itemCoverage,
});
/** Verified and runnable exactly when no reason is given; every withheld or absent field is named;
 * a null approval time is named `unavailable:receipts` (no promotion receipt is stored). */
const isRelease = ((v: unknown) => {
  if (!release(v)) return false;
  const r = v as Release;
  const cv = r.coverage;
  return honest(cv) && (r.signature.verification === "verified") === (r.signature.reasons.length === 0) &&
    r.install.runnable === (r.install.reasons.length === 0) &&
    named(r, cv, ["experiment_id", "candidate_id", "verdict_id", "strategy_version", "strategy_material_hash",
      "tenant_adoption"]) &&
    (r.signature.key_id !== null || reasonFor(cv.reasons, r.id, "signature/key_id") !== undefined) &&
    (r.approved_at !== null || reasonFor(cv.reasons, r.id, "receipts") !== undefined);
}) as Guard<Release>;
const uniqueIds = (items: { id: string }[]) => unique(items.map((x) => x.id));
export const isReleasePage = ((v: unknown) =>
  covered(pageOf(isRelease))(v) && uniqueIds(v.items)) as Guard<Page<Release> & { coverage: Coverage }>;

export interface DeploymentEvent {
  id: string; kind: "install" | "rollback" | "pause"; component: string; at: string; release_id: string | null;
  from_release_id: string | null; to_release_id: string | null; reasons: string[]; schema_after: number;
  operator_id: string | null; step_up_receipt_id: string | null; coverage: Coverage;
}
const deploymentEvent = obj({
  id, kind: oneOf("install", "rollback", "pause"), component, at: instant, release_id: n(id),
  from_release_id: n(id), to_release_id: n(id), reasons: arr(str), schema_after: int1, operator_id: n(id),
  step_up_receipt_id: n(id), coverage: itemCoverage,
});
/** Decision 63: a rollback names from and to only; install and pause name the release. Only a
 * pause gives reasons. Every null is named. */
const isDeploymentEvent = ((v: unknown) => {
  if (!deploymentEvent(v)) return false;
  const e = v as DeploymentEvent;
  const rollback = e.kind === "rollback";
  return honest(e.coverage) && (e.release_id === null) === rollback && (e.from_release_id !== null) === rollback &&
    (e.to_release_id !== null) === rollback && (e.kind === "pause") === (e.reasons.length > 0) &&
    named(e, e.coverage, ["release_id", "from_release_id", "to_release_id", "operator_id", "step_up_receipt_id"]);
}) as Guard<DeploymentEvent>;
export const isDeploymentEventPage = ((v: unknown) =>
  covered(pageOf(isDeploymentEvent))(v) && uniqueIds(v.items)) as Guard<Page<DeploymentEvent> & { coverage: Coverage }>;

export const APPROVAL_REASONS = ["role_not_assigned", "role_not_served", "self_hosted_release_not_configured",
  "step_up_required", "operation_not_served"] as const;
export interface ApprovalClass {
  class: (typeof APPROVAL_CLASSES)[number]; authority_scope: string; held: boolean;
  step_up: { required: true; satisfied: boolean; expires_at: string | null };
  served_operations: string[]; reasons: (typeof APPROVAL_REASONS)[number][];
}
export interface ApprovalClasses {
  deployment: "hosted" | "self_hosted"; evaluated_at: string; advisory: true; classes: ApprovalClass[]; coverage: Coverage;
}
const approvalClasses = obj({
  deployment: oneOf("hosted", "self_hosted"), evaluated_at: instant, advisory: (v) => v === true,
  classes: arr(obj({
    class: oneOf(...APPROVAL_CLASSES),
    authority_scope: oneOf("hosted_maintainer", "self_hosted_operator", "user_strategy_adoption"), held: bool,
    step_up: obj({ required: (v) => v === true, satisfied: bool, expires_at: n(instant) }),
    served_operations: uniq(id), reasons: uniq(oneOf(...APPROVAL_REASONS)),
  })),
  coverage: itemCoverage,
});
/** Exactly the three classes in contract order; a class not held gives a reason; a satisfied
 * step-up has an expiry. */
export const isApprovalClasses = ((v: unknown) => {
  if (!approvalClasses(v)) return false;
  const a = v as ApprovalClasses;
  return honest(a.coverage) && a.classes.length === APPROVAL_CLASSES.length &&
    a.classes.every((k, i) => k.class === APPROVAL_CLASSES[i] && (k.held || k.reasons.length > 0) &&
      (!k.step_up.satisfied || k.step_up.expires_at !== null));
}) as Guard<ApprovalClasses>;

export interface BudgetSummary {
  scope_id: string; period: string; category: string; cap_usd: Dec; spent_usd: Dec; reserved_usd: Dec;
  available_usd: Dec;
}
const nonneg = (v: unknown) => dec(v) && !(v as string).startsWith("-");
const isBudget = obj({
  scope_id: id, period: id, category: id, cap_usd: nonneg, spent_usd: nonneg, reserved_usd: nonneg,
  available_usd: dec,
}) as Guard<BudgetSummary>;
/** One summary per month and category, so reasons keyed `<period>:<category>` are unambiguous. */
export const isBudgetPage = ((v: unknown) => covered(pageOf(isBudget))(v) &&
  unique(v.items.map((b) => `${b.period}:${b.category}`))) as Guard<Page<BudgetSummary> & { coverage: Coverage }>;
