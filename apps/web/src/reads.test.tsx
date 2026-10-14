// @vitest-environment jsdom
// Web increment 3: views over the reads apps/api serves (capabilities, strategies, ledger,
// proposal detail, simulated fills and results). Every record below is SYNTHETIC; shapes follow
// apps/api/src/qw_api/{capabilities,strategies,reads,simulations}.py and contracts/amendments.json.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { App } from "./main";
import type { Fetcher } from "./api";
import { isCapabilities, isLedgerPage, isProposalDetail, isStrategy, isStrategyPage } from "./reads";
import { httpSource, isFillPage, resultPageFor } from "./sources";

const setHash = (hash: string) => { window.history.replaceState(null, "", hash || window.location.pathname); };
afterEach(() => { cleanup(); setHash(""); document.title = "quantweave"; });
const now = () => new Date("2026-10-14T12:00:00Z");
const problem404 = {
  type: "/problems/not_found", title: "Not Found", status: 404, code: "not_found", detail: "No such operation.",
  correlation_id: "corr-SYNTHETIC", retryable: false, retry_after_s: null, field_errors: [], reasons: [], error_schema: "1",
};
const H = "a".repeat(64);
const SIM = "simulated:separate_from_actual";
// Beyond double precision: Number() would print 9007199254740992 / 0.30000000000000004-style values.
const BIG = "9007199254740993.000000000001";

const CAPS = { deployment: "self_hosted", features: [
  { id: "ai_research", enabled: false, reason_codes: ["evaluation_evidence_unqualified", "model_route_unqualified"] },
  { id: "provider_data_display", enabled: true, reason_codes: [] },
] };
const strat = (id: string, extra: object = {}) => ({
  id, family: "allocation", version: "1.0.0", implementation_hash: H, configuration_hash: "b".repeat(64),
  required_feed_spec_ids: ["feed-SYNTHETIC"], policy_requirements: ["horizon:long_term", "instrument:equity"],
  research_protocol_id: "proto-SYNTHETIC", operational_status: "not_tested", evidence_status: "none",
  action_eligible: false, approved_by: null, source_decisions: [], ...extra,
});
const MISSING = ["quality_value", "income", "swing_momentum", "opening_range_breakout", "ai_thesis"].map((f) => `none:family/${f}`);
const STRATS = { items: [strat("strat-alloc-SYNTHETIC")], next_cursor: null, coverage: { status: "partial",
  reasons: ["default:configuration_hash", "unavailable:qualification", "unavailable:action_gate",
  "unavailable:source_decisions", ...MISSING] } };
const posting = (book_account: string, commodity: string, amount: string) => ({ book_account, commodity, amount });
const event = (id: string, extra: object = {}) => ({
  id, account_id: "acct-a", tenant_id: "t-SYNTHETIC", kind: "buy", effective_at: "2026-10-01T14:30:00-04:00",
  recorded_at: "2026-10-01T18:31:00Z", source_id: "src-SYNTHETIC", source_record_id: `row-${id}`,
  idempotency_key: `record/src-SYNTHETIC/row-${id}`, status: "accepted", reverses_event_id: null,
  postings: [posting("position:inst-SYNTHETIC", "USD", BIG), posting("cash", "USD", `-${BIG}`)],
  instrument_movements: [{ instrument_id: "inst-SYNTHETIC", quantity: "0.000000000001" }],
  lot_policy_id: null, economic_annotations: ["lot_policy:economic_average_cost/1"], ...extra,
});
const LEDGER_REASONS = ["unavailable:lot_policy_id", "unreconciled:status_is_journal_acceptance",
  "mapped:ev-3/kind=reversal->correction"];
const LEDGER = { next_cursor: null, coverage: { status: "partial", reasons: LEDGER_REASONS }, items: [
  event("ev-1"), event("ev-2", { status: "reversed" }),
  event("ev-3", { kind: "correction", reverses_event_id: "ev-2", idempotency_key: "reversal/ev-2",
    postings: [posting("position:inst-SYNTHETIC", "USD", `-${BIG}`), posting("cash", "USD", BIG)] }),
] };
const account = { id: "acct-a", name: "SYNTHETIC Alpha", broker: null, account_type: null, reporting_currency: null,
  analysis_scope: null, revision: 3, source_ids: ["src-SYNTHETIC"], status: null, cash_basis: "unknown" };
const ACCOUNT_REASONS = ["broker", "account_type", "reporting_currency", "analysis_scope", "status"].map((f) => `unavailable:${f}`);
const proposal = (extra: object = {}) => ({
  id: "p-1", version: 1, account_id: "acct-a", sleeve_id: null, origin: null, mode: "rules_only", state: "candidate",
  action: "buy", horizon: "long_term", expires_at: "2026-10-20T00:00:00Z", execution_state: "none",
  reason_codes: ["RISK_NOT_RECHECKED"], final_check_receipt_id: null, qualification: null,
  legs: [{ instrument_id: "inst-SYNTHETIC", side: "buy", quantity: "10", quantity_basis: "sized",
    estimated_cash_effect: { amount: "-1234.5", currency: "USD" } }], ...extra,
});
const P_REASONS = ["unavailable:origin", "unavailable:qualification", "unavailable:final_check_receipt_id",
  "none:p-1/sleeve_id", "not_rechecked:risk_evaluation_and_pauses"];
const fill = (id: string, extra: object = {}) => ({
  id, simulated_order_id: "vo-1", filled_at: "2026-10-02T13:30:00Z", quantity: "10",
  price: { amount: "50.125", currency: "USD" }, fees: { amount: "1.00", currency: "USD" }, data_record_ids: [],
  assumption_ids: ["next_open"], execution_model_id: "exec-next-open-v1", actual_execution: false, ...extra,
});
const calc = (id: string, formula_id: string, result: string | null, extra: object = {}) => ({
  id, formula_id, formula_version: "1", inputs: [], result, unit: "USD", status: result === null ? "insufficient_inputs" : "ok",
  warnings: [SIM, "rounding:exact:ledger_sum_1e-12", "ref:simulation=sim-1", "ref:simulation_digest=" + H], input_snapshot_hash: H, computed_at: "2026-10-03T00:00:00Z", ...extra,
});
const RESULTS = { next_cursor: null, coverage: { status: "partial", reasons: [SIM, "unverified:execution_model_id",
  "unavailable:market_value", "unavailable:scorecard"] }, items: [
  calc("calc:1", "virtual_realized_net", "-1.000000000001"),
  calc("calc:2", "virtual_realized_gain_before_fees", BIG),
  calc("calc:3", "virtual_market_value", null, { warnings: [SIM, "unavailable:missing_market_inputs", "ref:simulation=sim-1"] }),
] };
const FILLS = { next_cursor: null, coverage: { status: "partial", reasons: [SIM, "unverified:execution_model_id",
  "unavailable:data_record_ids"] }, items: [fill("vf-1")] };
const page = (items: unknown[], reasons: string[] = []) =>
  ({ items, next_cursor: null, coverage: { status: reasons.length ? "partial" : "complete", reasons } });
const ROUTES: Record<string, unknown> = {
  "/api/v1/capabilities": CAPS,
  "/api/v1/strategies?limit=200": STRATS,
  "/api/v1/strategies/strat-alloc-SYNTHETIC": strat("strat-alloc-SYNTHETIC"),
  "/api/v1/accounts?limit=200": page([account], ACCOUNT_REASONS),
  "/api/v1/accounts/acct-a/ledger?limit=200": LEDGER,
  "/api/v1/policies?limit=100": page([]),
  "/api/v1/proposals?limit=200": page([proposal()], P_REASONS),
  "/api/v1/proposals/p-1": { ...proposal(), coverage: { status: "partial", reasons: P_REASONS } },
  "/api/v1/simulations?limit=200": page([{ id: "sim-1", name: "SYNTHETIC Virtual Growth",
    execution_model_id: "exec-next-open-v1", benchmark_policy_id: "bm-1", mode: "manual", separate_from_actual: true }], [SIM]),
  "/api/v1/simulations/sim-1/fills?limit=200": FILLS,
  "/api/v1/simulations/sim-1/results?limit=200": RESULTS,
};
const isResultPage = resultPageFor("sim-1");
const fetcher = (routes = ROUTES) => vi.fn<Fetcher>((input) => {
  const body = routes[input];
  return Promise.resolve(new Response(JSON.stringify(body ?? problem404), { status: body ? 200 : 404 }));
});
const show = (hash: string, f = fetcher()) => {
  setHash(hash);
  render(<App fetcher={f} now={now} research={httpSource(f, now)} />);
  return f;
};

describe("guards fail closed", () => {
  const cases: [string, (v: unknown) => boolean, unknown, unknown][] = [
    ["capability enabled as a string", isCapabilities, CAPS, { ...CAPS, features: [{ id: "x", enabled: "false", reason_codes: ["r"] }] }],
    ["capability enabled with a reason", isCapabilities, CAPS, { ...CAPS, features: [{ id: "x", enabled: true, reason_codes: ["r"] }] }],
    ["capability unavailable without a reason", isCapabilities, CAPS, { ...CAPS, features: [{ id: "x", enabled: false, reason_codes: [] }] }],
    ["duplicate feature id", isCapabilities, CAPS, { ...CAPS, features: [CAPS.features[0], CAPS.features[0]] }],
    ["unknown deployment", isCapabilities, CAPS, { ...CAPS, deployment: "cloud" }],
    ["action_eligible as a string", isStrategy, strat("s"), strat("s", { action_eligible: "false" })],
    ["unknown operational status", isStrategy, strat("s"), strat("s", { operational_status: "ok" })],
    ["short hash", isStrategy, strat("s"), strat("s", { implementation_hash: "abc" })],
    ["strategy page without coverage", isStrategyPage, STRATS, { ...STRATS, coverage: undefined }],
    ["posting amount as a number", isLedgerPage, LEDGER, { ...LEDGER, items: [event("e", { postings: [posting("a", "USD", 12.5 as never), posting("b", "USD", "-12.5")] })] }],
    ["posting amount in exponent form", isLedgerPage, LEDGER, { ...LEDGER, items: [event("e", { postings: [posting("a", "USD", "1e3"), posting("b", "USD", "-1000")] })] }],
    ["movement quantity as a number", isLedgerPage, LEDGER, { ...LEDGER, items: [event("e", { instrument_movements: [{ instrument_id: "i", quantity: 1 }] })] }],
    ["single posting", isLedgerPage, LEDGER, { ...LEDGER, items: [event("e", { postings: [posting("a", "USD", "1")] })] }],
    ["unknown ledger status", isLedgerPage, LEDGER, { ...LEDGER, items: [event("e", { status: "settled" })] }],
    ["null lot policy without its reason", isLedgerPage, LEDGER, { ...LEDGER, coverage: { status: "partial", reasons: [] } }],
    ["non-correction naming a reversed event", isLedgerPage, LEDGER, { ...LEDGER, items: [event("e", { reverses_event_id: "ev-0" })] }],
    ["correction naming no reversed event", isLedgerPage, LEDGER, { ...LEDGER, items: [event("e", { kind: "correction" })] }],
    ["ledger page without coverage", isLedgerPage, LEDGER, { ...LEDGER, coverage: undefined }],
    ["proposal detail without coverage", isProposalDetail, ROUTES["/api/v1/proposals/p-1"], proposal()],
    ["proposal detail with no nulls and no coverage", isProposalDetail, ROUTES["/api/v1/proposals/p-1"],
      proposal({ sleeve_id: "core", origin: "rules", qualification: { operational: "passed", investment_evidence: "none",
        user_eligibility: "not_evaluated", reason_codes: [] } })],
    ["proposal detail null without its reason", isProposalDetail, ROUTES["/api/v1/proposals/p-1"],
      { ...proposal(), coverage: { status: "partial", reasons: P_REASONS.filter((r) => r !== "unavailable:origin") } }],
    ["fill price as a number", isFillPage, FILLS, { ...FILLS, items: [fill("f", { price: { amount: 50.125, currency: "USD" } })] }],
    ["fill marked as actual execution", isFillPage, FILLS, { ...FILLS, items: [fill("f", { actual_execution: true })] }],
    ["negative fill quantity", isFillPage, FILLS, { ...FILLS, items: [fill("f", { quantity: "-1" })] }],
    ["fill page not marked simulated", isFillPage, FILLS, { ...FILLS, coverage: { status: "partial", reasons: [] } }],
    ["result as a number", isResultPage, RESULTS, { ...RESULTS, items: [calc("c", "virtual_fees", 1.5 as never)] }],
    ["ok result without a value", isResultPage, RESULTS, { ...RESULTS, items: [calc("c", "virtual_fees", null, { status: "ok" })] }],
    ["result not marked simulated", isResultPage, RESULTS, { ...RESULTS, items: [calc("c", "virtual_fees", "1", { warnings: ["ref:simulation=sim-1"] })] }],
    ["result of another simulation", isResultPage, RESULTS, { ...RESULTS, items: [calc("c", "virtual_fees", "1", { warnings: [SIM, "ref:simulation=sim-2"] })] }],
    ["result naming no simulation", isResultPage, RESULTS, { ...RESULTS, items: [calc("c", "virtual_fees", "1", { warnings: [SIM] })] }],
    ["result page without coverage", isResultPage, RESULTS, { ...RESULTS, coverage: undefined }],
  ];
  it.each(cases)("refuses %s", (_name, guard, good, bad) => {
    expect(guard(good)).toBe(true);
    expect(guard(bad)).toBe(false);
  });
});

describe("capabilities (Settings)", () => {
  it("names every reason code as text next to each feature", async () => {
    show("#/settings");
    const table = await screen.findByRole("table", { name: /Feature status/ });
    const [, ai, display] = within(table).getAllByRole("row");
    expect(ai?.textContent).toContain("[Unavailable]");
    expect(ai?.textContent).toContain("evaluation_evidence_unqualified");
    expect(ai?.textContent).toContain("model_route_unqualified");
    expect(display?.textContent).toContain("[Enabled]");
    expect(screen.getByRole("main").textContent).toContain("Deployment: Self-hosted (self_hosted)");
  });
});

describe("strategies", () => {
  it("shows statuses literally, never upgraded, and names every missing family", async () => {
    show("#/strategies");
    const table = await screen.findByRole("table", { name: /Installed strategies/ });
    const text = table.textContent;
    expect(text).toContain("[operational_status: not_tested] Not tested");
    expect(text).toContain("[evidence_status: none] No investment evidence");
    expect(text).toContain("[action_eligible: false] Not eligible for action");
    expect(text).not.toMatch(/passed|qualified|Eligible for action/);
    const reasons = screen.getByRole("list", { name: "Strategy list coverage reasons" }).textContent;
    for (const r of [...MISSING, "unavailable:action_gate"]) expect(reasons).toContain(r);
    expect(screen.getByRole("main").textContent).toContain(
      "[Families not installed] quality_value, income, swing_momentum, opening_range_breakout, ai_thesis");
    fireEvent.click(screen.getByRole("button", { name: "Show strategy strat-alloc-SYNTHETIC" }));
    const detail = await screen.findByRole("region", { name: /Strategy strat-alloc-SYNTHETIC/ });
    await within(detail).findByText(/proto-SYNTHETIC/);
    expect(detail.textContent).toContain("[action_eligible: false]");
    expect(detail.textContent).toContain("Approved by: Unavailable: release approval is not modelled");
    expect(detail.textContent).toContain("Source decisions: Unavailable (unavailable:source_decisions)");
    expect(detail.textContent).toContain("Not eligible for action: Unavailable (unavailable:action_gate)");
  });
});

describe("ledger", () => {
  const openLedger = async (f = fetcher()) => {
    show("#/accounts", f);
    fireEvent.click(await screen.findByRole("button", { name: "Show ledger for SYNTHETIC Alpha" }));
    return screen.findByRole("table", { name: /Journal events for SYNTHETIC Alpha/ });
  };
  it("puts the unreconciled caveat next to every accepted status and keeps amounts as sent", async () => {
    const table = await openLedger();
    const [, buy, reversed, correction] = within(table).getAllByRole("row");
    expect(buy?.textContent).toContain("[Accepted by the journal: not reconciled] unreconciled:status_is_journal_acceptance");
    expect(buy?.textContent).toContain(`position:inst-SYNTHETIC: ${BIG} USD`);
    expect(buy?.textContent).toContain(`cash: -${BIG} USD`);
    expect(buy?.textContent).toContain("0.000000000001 inst-SYNTHETIC");
    expect(buy?.textContent).toContain("Lot policy: Unavailable (unavailable:lot_policy_id)");
    expect(reversed?.textContent).toContain("[Reversed by a correction]");
    expect(correction?.textContent).toContain("[Correction] of ev-2");
    const reasons = screen.getByRole("list", { name: "Ledger coverage reasons" }).textContent;
    for (const r of LEDGER_REASONS) expect(reasons).toContain(r);
  });
  it("never shows accepted without the caveat, even when coverage omits the code", async () => {
    const routes = { ...ROUTES, "/api/v1/accounts/acct-a/ledger?limit=200":
      { ...LEDGER, coverage: { status: "partial", reasons: ["unavailable:lot_policy_id"] } } };
    const table = await openLedger(fetcher(routes));
    for (const row of within(table).getAllByRole("row")) {
      if (row.textContent.includes("Accepted")) expect(row.textContent).toContain("not reconciled");
    }
    expect(table.textContent).toContain("[Accepted by the journal: not reconciled]");
  });
});

describe("proposal detail", () => {
  it("shows the proposal's own coverage reasons", async () => {
    show("#/decisions");
    fireEvent.click(await screen.findByRole("button", { name: "Show detail for proposal p-1" }));
    const detail = await screen.findByRole("region", { name: /Proposal detail: p-1/ });
    const reasons = await within(detail).findByRole("list", { name: "Proposal coverage reasons" });
    for (const r of P_REASONS) expect(reasons.textContent).toContain(r);
    expect(detail.textContent).toContain("sleeve Unavailable (none:p-1/sleeve_id)");
  });
});

describe("simulated fills and results", () => {
  it("are requested only in simulation mode and are labelled SIMULATED", async () => {
    const f = show("#/simulation");
    await screen.findByRole("table", { name: /Virtual portfolios/ });
    expect(f.mock.calls.map(([u]) => u).filter((u) => /fills|results/.test(u))).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Work with SYNTHETIC Virtual Growth in simulation mode" }));
    const fills = await screen.findByRole("table", { name: /^\[SIMULATED\] Modelled fills/ });
    expect(fills.textContent).toContain("50.125 USD");
    expect(fills.textContent).toContain("Unavailable (unavailable:data_record_ids)");
    const results = await screen.findByRole("table", { name: /^\[SIMULATED\] Ledger results/ });
    const [, net, gross, market] = within(results).getAllByRole("row");
    expect(net?.textContent).toContain("Realized net P&L (fees in cost and deducted from proceeds)");
    expect(net?.textContent).toContain("-1.000000000001 USD");
    expect(gross?.textContent).toContain("Realized gain before fees (fees not deducted)");
    expect(gross?.textContent).toContain("9,007,199,254,740,993.000000000001 USD");
    expect(market?.textContent).toContain("[Insufficient inputs] (insufficient_inputs)");
    expect(market?.textContent).toContain("Unavailable (missing_market_inputs)");
    for (const h of ["fills-title", "results-title"]) expect(document.getElementById(h)?.textContent).toMatch(/^\[SIMULATED\]/);
  });
});

describe("hostile or mismatched payloads", () => {
  it("reads a __proto__ evidence status as unrecognised instead of crashing", async () => {
    const bad = strat("strat-x", { evidence_status: JSON.parse('"__proto__"') as string });
    const err = vi.spyOn(console, "error").mockImplementation(() => undefined);
    show("#/strategies", fetcher({ ...ROUTES, "/api/v1/strategies?limit=200": { ...STRATS, items: [bad] } }));
    const table = await screen.findByRole("table", { name: /Installed strategies/ });
    expect(table.textContent).toContain("[evidence_status: __proto__] Unrecognised status: not relied on");
    expect(err).not.toHaveBeenCalled();
    err.mockRestore();
  });
  it("shows inherited-name formula ids and proposal states as plain codes", async () => {
    const items = ["constructor", "toString"].map((f, i) => calc(`calc:x${String(i)}`, f, "1"));
    const routes = { ...ROUTES, "/api/v1/simulations/sim-1/results?limit=200": { ...RESULTS, items },
      "/api/v1/proposals?limit=200": page([proposal({ state: "constructor" })], P_REASONS) };
    show("#/simulation", fetcher(routes));
    fireEvent.click(await screen.findByRole("button", { name: /Work with SYNTHETIC Virtual Growth/ }));
    const results = await screen.findByRole("table", { name: /^\[SIMULATED\] Ledger results/ });
    const [, a, b] = within(results).getAllByRole("row");
    expect(a?.querySelector("th")?.textContent).toBe("constructor (constructor v1)");
    expect(b?.querySelector("th")?.textContent).toBe("toString (toString v1)");
    cleanup();
    show("#/decisions", fetcher(routes));
    expect(await screen.findByText("[Unknown state (constructor)]")).toBeTruthy();
  });
  it("refuses a ledger item from another account", async () => {
    const routes = { ...ROUTES, "/api/v1/accounts/acct-a/ledger?limit=200":
      { ...LEDGER, items: [event("ev-1"), event("ev-x", { account_id: "acct-other" })] } };
    show("#/accounts", fetcher(routes));
    fireEvent.click(await screen.findByRole("button", { name: "Show ledger for SYNTHETIC Alpha" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Unavailable (ledger_account_mismatch)");
    expect(screen.queryByRole("table", { name: /Journal events/ })).toBeNull();
  });
  it("refuses a strategy detail for another id", async () => {
    const routes = { ...ROUTES, "/api/v1/strategies/strat-alloc-SYNTHETIC": strat("strat-other") };
    show("#/strategies", fetcher(routes));
    fireEvent.click(await screen.findByRole("button", { name: "Show strategy strat-alloc-SYNTHETIC" }));
    const detail = await screen.findByRole("region", { name: /Strategy strat-alloc-SYNTHETIC/ });
    expect((await within(detail).findByRole("alert")).textContent).toBe("Unavailable (record_id_mismatch)");
    expect(detail.textContent).not.toContain("strat-other");
  });
});
