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
