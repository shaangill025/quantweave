// Guards for reads apps/api serves since T021, T027 and T053-api: GET /capabilities,
// /strategies, /strategies/{id}, /accounts/{id}/ledger and /proposals/{id} (openapi.yaml 1.0.0
// as amended by project contract 0.4.0, owner-confirmed: proposal coverage, nullable lot_policy_id).
// Each refuses a payload the views could not show honestly; decimals stay strings.
import { checks as c, isProposal, pageOf, proposalPage, type Coverage, type Guard, type Page, type Proposal } from "./api";
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
