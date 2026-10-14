import { describe, expect, it, vi } from "vitest";
import { accountPage, actionable, getJson, isAccount, isSnapshot, pageOf, isPolicy, proposalPage, type Fetcher, type Proposal } from "./api";

const json = (status: number, body: unknown): Promise<Response> =>
  Promise.resolve(new Response(JSON.stringify(body), { status }));
const at = new Date("2026-10-10T12:00:00Z");

// SYNTHETIC snapshot following account_snapshot.schema.json.
const snapshot = {
  id: "snap-SYNTHETIC-1", account_id: "acct-b", revision: 7,
  effective_at: "2026-10-09T16:00:00-04:00", received_at: "2026-10-09T20:05:00Z",
  freshness_status: "stale", reconciliation: "conflicted", cash_basis: "broker_available",
  available_cash: [{ amount: "9007199254740993.01", currency: "CAD" }, { amount: "0.5", currency: "USD" }],
  positions: [{ instrument_id: "inst-SYNTHETIC-X", quantity: "12.5", economic_cost: null, lot_coverage: "unknown" }],
  unresolved_ids: ["obs-SYNTHETIC-1"],
};

describe("getJson", () => {
  it("requests same-origin under /api/v1 and returns typed data with the request time", async () => {
    const fetcher = vi.fn<Fetcher>(() => json(200, snapshot));
    const result = await getJson(fetcher, "/accounts/acct-b/snapshot", isSnapshot, () => at);
    expect(fetcher).toHaveBeenCalledWith("/api/v1/accounts/acct-b/snapshot", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    expect(result).toEqual({ status: "ok", data: snapshot, retrievedAt: at });
  });

  it("keeps the problem code, detail and correlation id of an error", async () => {
    const problem = {
      type: "/problems/not_found", title: "Not Found", status: 404, code: "not_found",
      detail: "No such operation.", correlation_id: "corr-SYNTHETIC-9", retryable: false,
      retry_after_s: null, field_errors: [], reasons: [], error_schema: "1",
    };
    const result = await getJson(() => json(404, problem), "/proposals", isSnapshot);
    expect(result).toEqual({
      status: "failed", code: "not_found", detail: "No such operation.", correlationId: "corr-SYNTHETIC-9",
    });
  });

  it.each([
    ["an error without a problem body", () => json(502, { oops: true }), "http_502"],
    ["a network failure", () => Promise.reject(new TypeError("offline")), "api_unreachable"],
    ["a non-JSON success", () => Promise.resolve(new Response("<html>", { status: 200 })), "invalid_response"],
    ["a float amount", () => json(200, { ...snapshot, available_cash: [{ amount: 0.5, currency: "USD" }] }), "invalid_response"],
    ["an exponent amount", () => json(200, { ...snapshot, available_cash: [{ amount: "5e-1", currency: "USD" }] }), "invalid_response"],
    ["a timestamp without offset", () => json(200, { ...snapshot, effective_at: "2026-10-09T16:00:00" }), "invalid_response"],
    ["an unknown freshness state", () => json(200, { ...snapshot, freshness_status: "fresh" }), "invalid_response"],
  ] as const)("fails closed on %s", async (_label, fetcher, code) => {
    const result = await getJson(fetcher as Fetcher, "/x", isSnapshot);
    expect(result.status).toBe("failed");
    expect(result.status === "failed" && result.code).toBe(code);
  });

  it.each(["..", ".", "a/b", ""])("refuses account id %j before it reaches a path (review N1)", (id) => {
    const acct = { id, name: "n", broker: "b", account_type: "t", reporting_currency: "CAD",
      analysis_scope: "complete_declared", revision: 1, source_ids: [], status: "active", cash_basis: "unknown" };
    expect(isAccount(acct)).toBe(false);
    expect(isAccount({ ...acct, id: "acct-1.x" })).toBe(true);
  });

  it("checks page shape and optional coverage", () => {
    const guard = pageOf(isPolicy);
    expect(guard({ items: [], next_cursor: null })).toBe(true);
    expect(guard({ items: [], next_cursor: null, coverage: { status: "partial", reasons: ["r"] } })).toBe(true);
    expect(guard({ items: [], next_cursor: null, coverage: { status: "full", reasons: [] } })).toBe(false);
    expect(guard({ items: [] })).toBe(false);
  });
});

describe("project contract 0.2.0 nulls", () => {
  const acct = { id: "acct-j", name: null, broker: "b", account_type: "t", reporting_currency: "CAD",
    analysis_scope: "complete_declared", revision: 1, source_ids: [], status: "active", cash_basis: "unknown" };
  const page = (reasons: string[]) => ({ items: [acct], next_cursor: null, coverage: { status: "partial", reasons } });

  it("accepts a null only when coverage names it, for all items or one id", () => {
    expect(accountPage(page(["unavailable:name"]))).toBe(true);
    expect(accountPage(page(["unavailable:acct-j/name"]))).toBe(true);
    expect(accountPage(page(["unavailable:acct-x/name", "unavailable:broker"]))).toBe(false);
    expect(accountPage({ items: [acct], next_cursor: null })).toBe(false);
    expect(accountPage({ ...page(["unavailable:name"]), items: [{ ...acct, id: 7 }] })).toBe(false);
  });

  it("checks snapshot nulls against its own coverage and ledger cash as decimal strings", () => {
    const s = { ...snapshot, reconciliation: null, available_cash: null, unresolved_ids: null,
      ledger_cash: [{ amount: "1.5", currency: "CAD" }],
      coverage: { status: "partial", reasons: ["unavailable:reconciliation", "unavailable:available_cash", "unavailable:unresolved_ids"] } };
    expect(isSnapshot(s)).toBe(true);
    expect(isSnapshot({ ...s, coverage: { status: "partial", reasons: ["unavailable:reconciliation"] } })).toBe(false);
    expect(isSnapshot({ ...s, ledger_cash: [{ amount: 1.5, currency: "CAD" }] })).toBe(false);
  });

  it("needs a reason for an active proposal's null receipt and is then not actionable", () => {
    const p = { id: "p-1", version: 1, account_id: "a", sleeve_id: "s", origin: "rules", mode: "rules_only",
      state: "active", action: "buy", horizon: "long_term", expires_at: "2026-10-11T00:00:00Z", legs: [],
      execution_state: "none", reason_codes: [], final_check_receipt_id: null,
      qualification: { operational: "passed", investment_evidence: "none", user_eligibility: "eligible", reason_codes: [] } };
    const of = (reasons: string[]) => ({ items: [p], next_cursor: null, coverage: { status: "partial", reasons } });
    expect(proposalPage(of([]))).toBe(false);
    expect(proposalPage(of(["unavailable:final_check_receipt_id"]))).toBe(true);
    expect(proposalPage({ ...of([]), items: [{ ...p, state: "candidate" }] })).toBe(true); // native null
    expect(actionable(p as Proposal)).toBe(false);
    expect(actionable({ ...p, final_check_receipt_id: "fc-1" } as Proposal)).toBe(true);
    expect(actionable({ ...p, final_check_receipt_id: "fc-1", qualification: null } as Proposal)).toBe(false);
  });
});
