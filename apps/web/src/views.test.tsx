// @vitest-environment jsdom
// Keyboard journeys use focus() plus the events a keyboard produces on native controls
// (Enter/Space on a button or link fire click; typing fires change; Enter in a form submits).
// @testing-library/user-event is not a dependency, so Tab order is checked through DOM order
// and tabindex rather than simulated key presses.
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { App } from "./main";
import { validateLimit, type LimitInput } from "./LimitDraft";
import type { Fetcher } from "./api";

// replaceState changes the hash without queueing a hashchange event that could land in a
// later test; navigation itself is then driven by an explicit hashchange event.
const setHash = (hash: string) => {
  window.history.replaceState(null, "", hash || window.location.pathname);
};
afterEach(() => {
  cleanup();
  setHash("");
});

const NOW = new Date("2026-10-10T12:00:00Z");
const now = () => NOW;
const problem404 = {
  type: "/problems/not_found", title: "Not Found", status: 404, code: "not_found",
  detail: "No such operation.", correlation_id: "corr-SYNTHETIC-404", retryable: false,
  retry_after_s: null, field_errors: [], reasons: [], error_schema: "1",
};
const qualification = {
  operational: "passed", investment_evidence: "limited", user_eligibility: "eligible",
  policy_version_id: "pol-SYNTHETIC:2", reason_codes: [],
};
// SYNTHETIC records following the contract schemas; not real accounts or proposals.
const account = (id: string, name: string, extra: object) => ({
  id, owner_tenant_id: "t-SYNTHETIC", name, broker: "SYNTHETIC broker", account_type: "margin",
  reporting_currency: "CAD", analysis_scope: "complete_declared", revision: 3,
  source_ids: [`src-${id}`], status: "active", cash_basis: "broker_available", ...extra,
});
const proposal = (id: string, state: string, expires: string, extra: object = {}) => ({
  id, version: 1, content_hash: "a".repeat(64), tenant_id: "t-SYNTHETIC", account_id: "acct-a",
  sleeve_id: "core", origin: "rules", mode: "rules_only", state, action: "buy", horizon: "long_term",
  trigger_at: "2026-10-10T10:00:00Z", received_at: "2026-10-10T10:00:01Z", created_at: "2026-10-10T10:00:02Z",
  expires_at: expires, binding: {}, conditions: [], invalidation_conditions: [], claim_ids: [],
  evidence_ids: [], calculation_ids: [], review_id: null, final_check_receipt_id: null, qualification,
  risk_summary: "SYNTHETIC", alternatives_group_id: null, joint_set_id: null, execution_state: "none",
  reason_codes: [],
  legs: [{ instrument_id: `inst-${id}`, side: "buy", quantity: "10", quantity_basis: "sized",
           estimated_cash_effect: { amount: "-1234.5", currency: "CAD" } }],
  ...extra,
});
const ROUTES: Record<string, [number, unknown]> = {
  "/api/v1/accounts?limit=200": [200, { next_cursor: null, items: [
    account("acct-a", "SYNTHETIC Alpha", {}),
    account("acct-b", "SYNTHETIC Beta", { status: "conflicted", analysis_scope: "partial_declared" }),
    account("acct-h", "SYNTHETIC Paper", { analysis_scope: "hypothetical" }),
  ] }],
  "/api/v1/accounts/acct-b/snapshot": [200, {
    id: "snap-1", account_id: "acct-b", revision: 7, effective_at: "2026-10-09T16:00:00-04:00",
    received_at: "2026-10-09T20:05:00Z", freshness_status: "stale", reconciliation: "conflicted",
    available_cash: [{ amount: "9007199254740993.01", currency: "CAD" }, { amount: "0.5", currency: "USD" }],
    cash_basis: "broker_available", unresolved_ids: ["obs-SYNTHETIC-1"],
    positions: [{ instrument_id: "inst-SYNTHETIC-X", quantity: "12.5", economic_cost: null, lot_coverage: "unknown" }],
  }],
  "/api/v1/accounts/acct-a/snapshot": [200, {
    id: "snap-2", account_id: "acct-a", revision: 2, effective_at: "2026-10-10T11:59:00Z",
    received_at: "2026-10-10T11:59:30Z", freshness_status: "current_under_policy", reconciliation: "reconciled",
    available_cash: [], cash_basis: "unknown", unresolved_ids: [], positions: [],
  }],
  "/api/v1/policies?limit=100": [200, { next_cursor: null, coverage: { status: "complete", reasons: [] }, items: [
    { id: "pol-1", version: 2, tenant_id: "t", scope_account_ids: ["acct-a"], status: "awaiting_adoption",
      numerical_limits: [{ metric: "cash_reserve", value: "0.05", unit: "ratio", denominator: "account_value", scope_id: "acct-a:CAD" }],
      accepted_by: null, accepted_at: null },
    { id: "pol-2", version: 1, tenant_id: "t", scope_account_ids: ["acct-b"], status: "adopted",
      numerical_limits: [], accepted_by: "user-SYNTHETIC", accepted_at: "2026-10-01T09:00:00+02:00" },
  ] }],
  "/api/v1/proposals?limit=200": [200, { next_cursor: "more", items: [
    proposal("p-review", "awaiting_review", "2026-10-11T00:00:00Z"),
    proposal("p-expired", "expired", "2026-10-09T00:00:00Z"),
    proposal("p-super", "superseded", "2026-10-11T00:00:00Z"),
    proposal("p-late", "active", "2026-10-10T11:00:00Z", {
      legs: [{ instrument_id: "inst-p-late", side: "buy", quantity: null, quantity_basis: "unsized", estimated_cash_effect: null }],
    }),
  ] }],
};
const fakeFetch = (overrides: Record<string, [number, unknown]> = {}): Fetcher => (input) => {
  const [status, body] = overrides[input] ?? ROUTES[input] ?? [404, problem404];
  return Promise.resolve(new Response(JSON.stringify(body), { status }));
};
const go = (hash: string) => {
  setHash(hash);
  fireEvent(window, new HashChangeEvent("hashchange"));
};

describe("navigation and focus", () => {
  it("moves focus to the view heading after keyboard navigation and marks the current page", async () => {
    render(<App fetcher={fakeFetch()} now={now} />);
    const link = screen.getByRole("link", { name: "Decisions" });
    link.focus();
    go("#/decisions");
    const h1 = await screen.findByRole("heading", { level: 1, name: "Decisions" });
    expect(document.activeElement).toBe(h1);
    expect(link.getAttribute("aria-current")).toBe("page");
    go("#/research");
    expect(await screen.findByText(/not connected yet/)).toBeTruthy();
  });

  it("uses only native focusable controls in DOM order, each with a name", async () => {
    setHash("#/decisions");
    const { container } = render(<App fetcher={fakeFetch()} now={now} />);
    await screen.findByText(/Awaiting review \(pending\)/);
    expect(container.querySelectorAll("[tabindex]:not([tabindex='-1'])")).toHaveLength(0);
    for (const role of ["button", "textbox", "combobox", "radio", "link"]) {
      const all = screen.queryAllByRole(role);
      expect(all.length).toBeGreaterThan(0);
      expect(screen.queryAllByRole(role, { name: /\S/ })).toEqual(all);
    }
    for (const input of container.querySelectorAll("input, select")) {
      expect(input.closest("label") ?? container.querySelector(`label[for="${input.id}"]`)).not.toBeNull();
    }
  });
});

describe("in-page links (review S1)", () => {
  it("skip link and a stray in-page hash keep the view and focus main", () => {
    setHash("#/decisions");
    render(<App fetcher={fakeFetch()} now={now} />);
    fireEvent.click(screen.getByRole("link", { name: "Skip to main content" }));
    expect(document.activeElement).toBe(screen.getByRole("main"));
    go("#main");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Decisions");
  });

  it("an error-summary link focuses its field without leaving the draft", async () => {
    setHash("#/decisions");
    render(<App fetcher={fakeFetch()} now={now} />);
    fireEvent.submit(screen.getByRole("form", { name: "Draft a numerical limit" }));
    fireEvent.click(await screen.findByRole("link", { name: "Choose a metric." }));
    expect(document.activeElement?.id).toBe("limit-metric");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Decisions");
    expect(screen.getByText(/Fix 3 problems/)).toBeTruthy();
  });
});

describe("Overview", () => {
  it("lists contradictory, partial and pending records and shows a missing endpoint as not connected", async () => {
    render(<App fetcher={fakeFetch({ "/api/v1/proposals?limit=200": [404, problem404] })} now={now} />);
    const main = screen.getByRole("main");
    await within(main).findByText(/Policy version 2 awaits your adoption/);
    expect(main.textContent).toContain("SYNTHETIC Beta: reconciliation conflict between sources.");
    expect(main.textContent).toContain("partial portfolio; outside holdings are unknown, not zero");
    expect(main.textContent).toContain("Proposals: Unavailable (not_found)");
    expect(main.textContent).toContain("Not connected");
    expect(main.textContent).not.toContain("Nothing in the loaded records needs attention");
    expect(main.textContent).toContain("could not be loaded, so this list may be incomplete");
  });

  it("gives an all-clear only when every source is complete (review S2)", async () => {
    const empty = { items: [], next_cursor: null };
    const all = { "/api/v1/accounts?limit=200": [200, empty], "/api/v1/policies?limit=100": [200, empty],
      "/api/v1/proposals?limit=200": [200, empty] } as Record<string, [number, unknown]>;
    render(<App fetcher={fakeFetch(all)} now={now} />);
    expect(await screen.findByText("Nothing in the loaded records needs attention.")).toBeTruthy();
    cleanup();
    const partial = { ...empty, coverage: { status: "partial", reasons: ["shard_SYNTHETIC_down"] } };
    render(<App fetcher={fakeFetch({ ...all, "/api/v1/policies?limit=100": [200, partial],
      "/api/v1/proposals?limit=200": [200, { ...empty, next_cursor: "more" }] })} now={now} />);
    const main = screen.getByRole("main");
    await waitFor(() => { expect(main.textContent).toContain("Policies: [Partial list] shard SYNTHETIC down"); });
    expect(main.textContent).toContain("Proposals: Only the first page is shown");
    expect(main.textContent).not.toContain("Nothing in the loaded records needs attention");
  });

  it("lists proposals awaiting review and live proposals past expiry", async () => {
    render(<App fetcher={fakeFetch()} now={now} />);
    const main = screen.getByRole("main");
    await within(main).findByText(/awaiting server expiry/);
    expect(main.textContent).toContain("buy proposal for acct-a: Awaiting review (pending).");
    expect(main.textContent).not.toMatch(/p-expired|superseded/i);
  });
});

describe("Accounts", () => {
  it("keeps hypothetical accounts apart and opens a snapshot by keyboard with stale and conflict labels", async () => {
    setHash("#/accounts");
    render(<App fetcher={fakeFetch()} now={now} />);
    const real = await screen.findByRole("table", { name: "Real accounts" });
    const paper = screen.getByRole("table", { name: /Hypothetical accounts/ });
    expect(within(real).queryByText(/SYNTHETIC Paper/)).toBeNull();
    expect(within(paper).getByRole("rowheader").textContent).toMatch(/^SYNTHETIC Paper/);
    expect(within(real).getByText("[Reconciliation conflict]")).toBeTruthy();
    const open = screen.getByRole("button", { name: "Show snapshot for SYNTHETIC Beta" });
    open.focus();
    fireEvent.click(open);
    const heading = screen.getByRole("heading", { name: "Snapshot: SYNTHETIC Beta" });
    expect(document.activeElement).toBe(heading);
    await screen.findByText("[STALE]");
    expect(screen.getByText(/\[Contradictory sources: reconciliation conflict, sizing blocked\]/)).toBeTruthy();
    expect(screen.getByText(/As of 2026-10-09 20:00 UTC; received 2026-10-09 20:05 UTC/)).toBeTruthy();
    const cash = screen.getByRole("table", { name: /Available cash in SYNTHETIC Beta only/ });
    expect(within(cash).getByText("9,007,199,254,740,993.01 CAD")).toBeTruthy(); // 2^53+1 kept exact
    expect(within(cash).getByText("0.5 USD")).toBeTruthy();
    expect(cash.textContent).toContain("stale");
    expect(screen.getByText("Unavailable (cost_unknown)")).toBeTruthy();
    expect(screen.getByText(/obs-SYNTHETIC-1/)).toBeTruthy();
  });

  it("shows missing cash as unavailable, never zero", async () => {
    setHash("#/accounts");
    render(<App fetcher={fakeFetch()} now={now} />);
    fireEvent.click(await screen.findByRole("button", { name: "Show snapshot for SYNTHETIC Alpha" }));
    const cash = await screen.findByRole("table", { name: /Available cash in SYNTHETIC Alpha only/ });
    expect(within(cash).getByText("Unavailable (no_cash_observation)")).toBeTruthy();
    expect(cash.textContent).not.toMatch(/\b0(\.0+)?\b/);
    expect(cash.textContent).toContain("unknown basis, unconfirmed");
  });

  it("reports a snapshot that names another account instead of showing it", async () => {
    setHash("#/accounts");
    const wrong = { ...(ROUTES["/api/v1/accounts/acct-b/snapshot"]?.[1] as object), account_id: "acct-a" };
    render(<App fetcher={fakeFetch({ "/api/v1/accounts/acct-b/snapshot": [200, wrong] })} now={now} />);
    fireEvent.click(await screen.findByRole("button", { name: "Show snapshot for SYNTHETIC Beta" }));
    expect(await screen.findByText("Unavailable (snapshot_account_mismatch)")).toBeTruthy();
    expect(screen.queryByText(/9,007,199/)).toBeNull();
  });
});

describe("Decisions", () => {
  it("shows each proposal's server state, a missed deadline and unsized legs, with no order controls", async () => {
    setHash("#/decisions");
    render(<App fetcher={fakeFetch()} now={now} />);
    const card = (name: RegExp) => screen.getByRole("article", { name });
    await screen.findByRole("article", { name: /inst-p-review/ });
    expect(within(card(/inst-p-review/)).getByText("[Awaiting review (pending)]")).toBeTruthy();
    expect(within(card(/inst-p-expired/)).getByText("[Expired]")).toBeTruthy();
    expect(within(card(/inst-p-super/)).getByText("[Superseded by a newer version]")).toBeTruthy();
    const late = card(/inst-p-late/);
    expect(within(late).getByText("[Active]")).toBeTruthy();
    expect(within(late).getByText("[Past expiry, not actionable]")).toBeTruthy();
    expect(within(late).getByText("Unavailable (unsized)")).toBeTruthy();
    expect(within(late).getByText("Unavailable (not_estimated)")).toBeTruthy();
    expect(within(card(/inst-p-review/)).getByText("-1,234.5 CAD")).toBeTruthy();
    expect(screen.getByText("Only the first page is shown; more records exist.")).toBeTruthy();
    const names = screen.getAllByRole("button").map((b) => b.textContent);
    expect(names).toEqual(["Check draft"]);
    expect(names.join(" ")).not.toMatch(/buy|sell|submit|order|place|trade/i);
  });

  it("shows policy status, limits with denominator and scope, and blocked sizing without limits", async () => {
    setHash("#/decisions");
    render(<App fetcher={fakeFetch()} now={now} />);
    expect(await screen.findByText("[Awaiting your adoption (pending)]")).toBeTruthy();
    const limits = screen.getByRole("list", { name: "Limits of policy version 2" });
    expect(limits.textContent).toBe("cash reserve: 0.05 ratio of account value, scope acct-a:CAD");
    expect(screen.getByText(/Adopted 2026-10-01 07:00 UTC by user-SYNTHETIC/)).toBeTruthy();
    expect(screen.getByText("No numerical limits: sizing is blocked for this scope.")).toBeTruthy();
  });

  it("validates the limit draft by keyboard, announces errors and keeps the decimal verbatim", async () => {
    setHash("#/decisions");
    render(<App fetcher={fakeFetch()} now={now} />);
    const form = screen.getByRole("form", { name: "Draft a numerical limit" });
    const value = within(form).getByLabelText(/Limit value/);
    expect((value as HTMLInputElement).value).toBe(""); // no hidden default
    fireEvent.submit(form);
    const summary = within(form).getByRole("alert");
    await waitFor(() => { expect(document.activeElement).toBe(summary); });
    expect(summary.textContent).toContain("Fix 3 problems");
    expect(value.getAttribute("aria-invalid")).toBe("true");
    expect(document.getElementById(value.getAttribute("aria-describedby") ?? "")?.textContent).toMatch(/no default/);
    fireEvent.change(within(form).getByLabelText("Metric"), { target: { value: "planned_trade_loss" } });
    fireEvent.click(within(form).getByLabelText("Amount in a currency"));
    fireEvent.change(value, { target: { value: "250.000000000001" } });
    fireEvent.change(within(form).getByLabelText("Currency"), { target: { value: "CAD" } });
    fireEvent.change(within(form).getByLabelText("Denominator"), { target: { value: "per_trade" } });
    fireEvent.submit(form);
    expect(summary.textContent).toBe("");
    const live = form.querySelector("[aria-live='polite']");
    expect(live?.textContent).toBe(
      "Draft is well formed: planned_trade_loss 250.000000000001 CAD of per_trade. Not saved and not adopted.",
    );
    fireEvent.change(value, { target: { value: "-1" } }); // review N2: an edit clears the verdict
    expect(live?.textContent).toBe("");
  });
});

describe("validateLimit (display-side checks; server authoritative)", () => {
  const base: LimitInput = { metric: "drawdown", value: "0.1", unit: "ratio", currency: "", denominator: "nav" };
  it.each([
    ["0.1", "ratio", []],
    ["0.30000000000000000", "ratio", []], // 17 decimals, kept as typed
    ["1e3", "ratio", ["value"]],
    ["1,000", "amount", ["value", "currency"]],
    ["-0.5", "ratio", ["value"]],
    ["0.000", "ratio", ["value"]],
    [" 1", "ratio", ["value"]],
    ["1".repeat(13), "ratio", ["value"]],
    ["1".repeat(12), "ratio", []],
    ["1.0000000000001", "amount", ["value", "currency"]], // 13 decimals > MoneyAmount scale 12
  ] as const)("%j as %s -> %j", (value, unit, fields) => {
    expect(validateLimit({ ...base, value, unit }).map((p) => p.field)).toEqual(fields);
  });
  it("requires a known metric and a denominator", () => {
    expect(validateLimit({ ...base, metric: "vibes", denominator: "" }).map((p) => p.field)).toEqual(["metric", "denominator"]);
  });
});
