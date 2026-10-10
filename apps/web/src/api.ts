// Read-only client for the contract endpoints the dashboard views use (openapi.yaml 1.0.0;
// schemas account, account_snapshot, proposal, policy). Responses are checked at runtime
// before display. Money and quantities stay decimal strings; nothing here does arithmetic.
import { isDecimalString, isProblem, type DecimalString } from "./wire";

export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>;

// Fields the views read, as named in the contract schemas (other fields are not typed here).
type Id = string;
type Instant = string; // RFC 3339 with offset, checked at runtime
export type CashBasis = "broker_available" | "reconciled_ledger" | "user_reported" | "unknown";
export interface Money { amount: DecimalString; currency: string }
export interface Account {
  id: Id; name: string; broker: string; account_type: string; reporting_currency: string;
  analysis_scope: "complete_declared" | "partial_declared" | "hypothetical";
  revision: number; source_ids: Id[]; status: "active" | "archived" | "conflicted";
  cash_basis: CashBasis;
}
export interface Position {
  instrument_id: Id; quantity: DecimalString; economic_cost: Money | null;
  lot_coverage: "complete" | "partial" | "unknown";
}
export interface Snapshot {
  account_id: Id; revision: number; effective_at: Instant; received_at: Instant;
  freshness_status: "current_under_policy" | "stale" | "unknown";
  reconciliation: "reconciled" | "conflicted" | "partial";
  available_cash: Money[]; cash_basis: CashBasis; positions: Position[]; unresolved_ids: Id[];
}
export interface Leg {
  instrument_id: Id; side: "buy" | "sell" | "hold"; quantity: DecimalString | null;
  quantity_basis: "sized" | "unsized" | "not_applicable"; estimated_cash_effect: Money | null;
}
export interface Proposal {
  id: Id; version: number; account_id: Id; sleeve_id: Id; origin: string;
  mode: "rules_only" | "ai_enabled"; state: string; action: string; horizon: string;
  expires_at: Instant; legs: Leg[]; execution_state: string; reason_codes: string[];
  qualification: {
    operational: string; investment_evidence: string; user_eligibility: string;
    reason_codes: string[];
  };
}
export interface Limit { metric: string; value: DecimalString; unit: string; denominator: Id; scope_id: Id }
export interface Policy {
  id: Id; version: number; scope_account_ids: Id[]; numerical_limits: Limit[];
  status: "draft" | "awaiting_adoption" | "adopted" | "superseded" | "paused";
  accepted_by: string | null; accepted_at: Instant | null;
}
export interface Page<T> {
  items: T[]; next_cursor: string | null;
  coverage?: { status: "complete" | "partial"; reasons: string[] };
}

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

const cashBasis = oneOf("broker_available", "reconciled_ledger", "user_reported", "unknown");
const money = obj({ amount: dec, currency: (v) => typeof v === "string" && /^[A-Z]{3}$/.test(v) });
export const isAccount = obj({
  id, name: str, broker: str, account_type: str, reporting_currency: str,
  analysis_scope: oneOf("complete_declared", "partial_declared", "hypothetical"),
  revision: int, source_ids: arr(str), status: oneOf("active", "archived", "conflicted"),
  cash_basis: cashBasis,
}) as Guard<Account>;
export const isSnapshot = obj({
  account_id: str, revision: int, effective_at: instant, received_at: instant,
  freshness_status: oneOf("current_under_policy", "stale", "unknown"),
  reconciliation: oneOf("reconciled", "conflicted", "partial"),
  available_cash: arr(money), cash_basis: cashBasis, unresolved_ids: arr(str),
  positions: arr(obj({
    instrument_id: str, quantity: dec, economic_cost: nullable(money),
    lot_coverage: oneOf("complete", "partial", "unknown"),
  })),
}) as Guard<Snapshot>;
export const isProposal = obj({
  id: str, version: int, account_id: str, sleeve_id: str, origin: str,
  mode: oneOf("rules_only", "ai_enabled"), state: str, action: str, horizon: str,
  expires_at: instant, execution_state: str, reason_codes: arr(str),
  qualification: obj({
    operational: str, investment_evidence: str, user_eligibility: str, reason_codes: arr(str),
  }),
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
export const pageOf = <T,>(item: Guard<T>): Guard<Page<T>> => (v): v is Page<T> =>
  obj({ items: arr(item), next_cursor: nullable(str) })(v) &&
  (!("coverage" in (v as object)) ||
    obj({ status: oneOf("complete", "partial"), reasons: arr(str) })(
      (v as { coverage: unknown }).coverage,
    ));

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
