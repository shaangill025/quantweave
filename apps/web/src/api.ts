// Read-only client for the contract endpoints the dashboard views use (openapi.yaml 1.0.0;
// schemas account, account_snapshot, proposal, policy, as amended by the project contract
// 0.2.0 in contracts/amendments.json: some fields may be null, and each such null must be
// named in the response's coverage.reasons or the response is refused). Responses are checked at runtime
// before display. Money and quantities stay decimal strings; nothing here does arithmetic.
import { isDecimalString, isProblem, type DecimalString } from "./wire";

export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>;

// Fields the views read, as named in the contract schemas (other fields are not typed here).
type Id = string;
type Instant = string; // RFC 3339 with offset, checked at runtime
export type CashBasis = "broker_available" | "reconciled_ledger" | "user_reported" | "unknown";
export interface Money { amount: DecimalString; currency: string }
export interface Coverage { status: "complete" | "partial"; reasons: string[] }
export interface Account {
  id: Id; name: string | null; broker: string | null; account_type: string | null;
  reporting_currency: string | null;
  analysis_scope: "complete_declared" | "partial_declared" | "hypothetical" | null;
  revision: number; source_ids: Id[]; status: "active" | "archived" | "conflicted" | null;
  cash_basis: CashBasis;
}
export interface Position {
  instrument_id: Id; quantity: DecimalString; economic_cost: Money | null;
  lot_coverage: "complete" | "partial" | "unknown";
}
export interface Snapshot {
  account_id: Id; revision: number; effective_at: Instant; received_at: Instant;
  freshness_status: "current_under_policy" | "stale" | "unknown";
  reconciliation: "reconciled" | "conflicted" | "partial" | null;
  available_cash: Money[] | null; cash_basis: CashBasis; positions: Position[];
  unresolved_ids: Id[] | null;
  ledger_cash?: Money[]; // trade-date journal cash, not available cash
  coverage?: Coverage;
}
export interface Leg {
  instrument_id: Id; side: "buy" | "sell" | "hold"; quantity: DecimalString | null;
  quantity_basis: "sized" | "unsized" | "not_applicable"; estimated_cash_effect: Money | null;
}
export interface Proposal {
  id: Id; version: number; account_id: Id; sleeve_id: Id | null; origin: string | null;
  mode: "rules_only" | "ai_enabled"; state: string; action: string; horizon: string | null;
  expires_at: Instant; legs: Leg[]; execution_state: string; reason_codes: string[];
  final_check_receipt_id: string | null;
  qualification: {
    operational: string; investment_evidence: string; user_eligibility: string;
    reason_codes: string[];
  } | null;
}
export interface Limit { metric: string; value: DecimalString; unit: string; denominator: Id; scope_id: Id }
export interface Policy {
  id: Id; version: number; scope_account_ids: Id[]; numerical_limits: Limit[];
  status: "draft" | "awaiting_adoption" | "adopted" | "superseded" | "paused";
  accepted_by: string | null; accepted_at: Instant | null;
}
export interface Page<T> { items: T[]; next_cursor: string | null; coverage?: Coverage }

/** The coverage reason naming a null field (`<kind>:<field>` or `<kind>:<id>/<field>`). */
export const reasonFor = (reasons: string[] | undefined, itemId: string, field: string) =>
  reasons?.find((r) => {
    const named = r.slice(r.indexOf(":") + 1);
    return r.includes(":") && (named === field || named === `${itemId}/${field}`);
  });
/** Fields the project contract made nullable (only those the views read). */
export const ACCOUNT_NULLABLE = ["name", "broker", "account_type", "reporting_currency", "analysis_scope", "status"];
export const SNAPSHOT_NULLABLE = ["reconciliation", "available_cash", "unresolved_ids"];
export const PROPOSAL_NULLABLE = ["sleeve_id", "origin", "horizon", "qualification"];
/** True when every null among `fields` is named by a reason. */
const explained = (item: object, fields: string[], reasons: string[] | undefined) => {
  const v = item as Record<string, unknown>;
  return fields.every((f) => v[f] !== null || reasonFor(reasons, String(v.id), f) !== undefined);
};
/** Active needs a final-check receipt and a qualification, or a reason why they are null. */
const proposalFields = (p: object) =>
  (p as { state: unknown }).state === "active"
    ? [...PROPOSAL_NULLABLE, "final_check_receipt_id"]
    : PROPOSAL_NULLABLE;
/** A proposal is actionable only when active with a final-check receipt and qualification. */
export const actionable = (p: Proposal) =>
  p.state === "active" && p.final_check_receipt_id !== null && p.qualification !== null;

export type Loaded<T> =
  | { status: "ok"; data: T; retrievedAt: Date }
  | { status: "failed"; code: string; detail: string; correlationId: string | null };

// Minimal runtime shape checks: required fields and types the views read. Extra fields are
// ignored here; full contract validation stays with the API.
type Check = (v: unknown) => boolean;
export type Guard<T> = (v: unknown) => v is T;
const str: Check = (v) => typeof v === "string";
// Contract id pattern: also keeps "", "." and ".." out of request paths built from ids.
const id: Check = (v) => typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(v);
const int: Check = (v) => typeof v === "number" && Number.isInteger(v);
const dec: Check = isDecimalString;
const instant: Check = (v) =>
  typeof v === "string" && /(Z|[+-]\d\d:\d\d)$/.test(v) && !isNaN(new Date(v).getTime());
const oneOf = (...values: string[]): Check => (v) => typeof v === "string" && values.includes(v);
const nullable = (c: Check): Check => (v) => v === null || c(v);
const arr = (c: Check): Check => (v) => Array.isArray(v) && v.every(c);
const obj =
  (spec: Record<string, Check>): Check =>
  (v) =>
    typeof v === "object" &&
    v !== null &&
    !Array.isArray(v) &&
    Object.entries(spec).every(([k, c]) => k in v && c((v as Record<string, unknown>)[k]));

const coverage: Check = (v) => v === undefined || obj({ status: oneOf("complete", "partial"), reasons: arr(str) })(v);
const cashBasis = oneOf("broker_available", "reconciled_ledger", "user_reported", "unknown");
const money = obj({ amount: dec, currency: (v) => typeof v === "string" && /^[A-Z]{3}$/.test(v) });
const n = nullable;
export const isAccount = obj({
  id, name: n(str), broker: n(str), account_type: n(str), reporting_currency: n(str),
  analysis_scope: n(oneOf("complete_declared", "partial_declared", "hypothetical")),
  revision: int, source_ids: arr(str), status: n(oneOf("active", "archived", "conflicted")),
  cash_basis: cashBasis,
}) as Guard<Account>;
const snapshotShape = obj({
  account_id: str, revision: int, effective_at: instant, received_at: instant,
  freshness_status: oneOf("current_under_policy", "stale", "unknown"),
  reconciliation: n(oneOf("reconciled", "conflicted", "partial")),
  available_cash: n(arr(money)), cash_basis: cashBasis, unresolved_ids: n(arr(str)),
  positions: arr(obj({
    instrument_id: str, quantity: dec, economic_cost: nullable(money),
    lot_coverage: oneOf("complete", "partial", "unknown"),
  })),
});
export const isSnapshot = ((v: unknown) => {
  if (!snapshotShape(v)) return false;
  const s = v as Snapshot & { ledger_cash?: unknown; coverage?: unknown };
  return (s.ledger_cash === undefined || arr(money)(s.ledger_cash)) && coverage(s.coverage) &&
    explained({ ...s, id: s.account_id }, SNAPSHOT_NULLABLE, s.coverage?.reasons);
}) as Guard<Snapshot>;
export const isProposal = obj({
  id: str, version: int, account_id: str, sleeve_id: n(str), origin: n(str),
  mode: oneOf("rules_only", "ai_enabled"), state: str, action: str, horizon: n(str),
  expires_at: instant, execution_state: str, reason_codes: arr(str),
  final_check_receipt_id: n(str),
  qualification: n(obj({
    operational: str, investment_evidence: str, user_eligibility: str, reason_codes: arr(str),
  })),
  legs: arr(obj({
    instrument_id: str, side: oneOf("buy", "sell", "hold"), quantity: nullable(dec),
    quantity_basis: oneOf("sized", "unsized", "not_applicable"),
    estimated_cash_effect: nullable(money),
  })),
}) as Guard<Proposal>;
export const isPolicy = obj({
  id: str, version: int, scope_account_ids: arr(str),
  status: oneOf("draft", "awaiting_adoption", "adopted", "superseded", "paused"),
  numerical_limits: arr(obj({ metric: str, value: dec, unit: str, denominator: str, scope_id: str })),
  accepted_by: nullable(str), accepted_at: nullable(instant),
}) as Guard<Policy>;
/** A page guard; `nulls` names the item's nullable fields, each null needing a reason. */
export const pageOf = <T extends object>(
  item: Guard<T>,
  nulls: (item: T) => string[] = () => [],
): Guard<Page<T>> => (v): v is Page<T> => {
  if (!obj({ items: arr(item), next_cursor: nullable(str) })(v)) return false;
  const page = v as Page<T>;
  if (!coverage(page.coverage)) return false;
  return page.items.every((i) => explained(i, nulls(i), page.coverage?.reasons));
};
export const accountPage = pageOf(isAccount, () => ACCOUNT_NULLABLE);
export const proposalPage = pageOf(isProposal, proposalFields);

/** GET a same-origin API path; every failure is a typed result, never a synthetic success. */
export async function getJson<T>(
  fetcher: Fetcher,
  path: string,
  guard: Guard<T>,
  now: () => Date = () => new Date(),
): Promise<Loaded<T>> {
  const fail = (code: string, detail: string, correlationId: string | null = null): Loaded<T> => ({
    status: "failed", code, detail, correlationId,
  });
  let response: Response;
  try {
    response = await fetcher(`/api/v1${path}`, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
  } catch {
    return fail("api_unreachable", "The API could not be reached.");
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  if (!response.ok) {
    if (isProblem(body)) return fail(body.code, body.detail, body.correlation_id);
    return fail(`http_${String(response.status)}`, "The API answered without a problem body.");
  }
  if (!guard(body)) return fail("invalid_response", "The response did not match the contract.");
  return { status: "ok", data: body, retrievedAt: now() };
}
/** Shape checks reused by other read-only clients (sources.ts). */
export const checks = { str, id, int, dec, instant, oneOf, nullable, arr, obj, money, coverage };
