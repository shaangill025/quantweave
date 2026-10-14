// @vitest-environment jsdom
// T054 Research (evidence drilldown, option scenarios) and Simulation (virtual mode, scorecards).
// Every record below is SYNTHETIC. The option scenario grid and the scorecard have no contract
// route yet, so they reach the views only through the typed source interface in these tests.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { App } from "./main";
import type { Fetcher } from "./api";
import {
  checked, httpSource, isScenarioGrid, isScorecard, isSimulationPage, percent, quoteBlockers, termBlockers,
  type OptionTerms, type ResearchSource,
} from "./sources";

const setHash = (hash: string) => { window.history.replaceState(null, "", hash || window.location.pathname); };
const go = (hash: string) => { setHash(hash); fireEvent(window, new HashChangeEvent("hashchange")); };
afterEach(() => { cleanup(); setHash(""); document.title = "quantweave"; });

const NOW = new Date("2026-10-14T12:00:00Z");
const now = () => NOW;
const problem404 = {
  type: "/problems/not_found", title: "Not Found", status: 404, code: "not_found", detail: "No such operation.",
  correlation_id: "corr-SYNTHETIC", retryable: false, retry_after_s: null, field_errors: [], reasons: [], error_schema: "1",
};
const H = "a".repeat(64);
const evidence = (id: string, extra: object = {}) => ({
  id, source_id: `src-${id}`, locator: `SYNTHETIC filing ${id}`, passage: `SYNTHETIC passage ${id}`,
  content_hash: H, published_at: "2026-10-01T09:00:00-04:00", retrieved_at: "2026-10-02T10:00:00Z",
  rights_profile_id: "rights-SYNTHETIC", availability: "available", source_relationship_group: "grp-1",
  retention_class: "standard", instrument_ids: ["inst-SYNTHETIC"], ...extra,
});
const terms = (extra: Partial<Record<keyof OptionTerms, unknown>> = {}) => ({
  id: "ot-1", instrument_id: "opt-1", underlying_id: "inst-SYNTHETIC", expiry: "2026-12-18",
  strike: { amount: "50", currency: "USD" }, right: "call", exercise_style: "american", settlement: "physical",
  multiplier: "100", deliverables: [{ instrument_id: "inst-SYNTHETIC", quantity: "100" }], adjusted: false,
  terms_verified: true, source_record_id: "rec-SYNTHETIC", ...extra,
});
const ROUTES: Record<string, unknown> = {
  "/api/v1/claims/clm-1": {
    id: "clm-1", kind: "forecast", text: "SYNTHETIC: revenue grows next year", instrument_ids: ["inst-SYNTHETIC"],
    evidence_ids: ["ev-1", "ev-gone"], calculation_ids: ["calc-1"], material: true, verification: "contested",
    counterevidence_ids: ["ev-contra"], valid_at: "2026-10-10T00:00:00Z",
  },
  "/api/v1/evidence/ev-1": evidence("ev-1"),
  "/api/v1/evidence/ev-gone": evidence("ev-gone", { passage: null, availability: "deleted", published_at: null }),
  "/api/v1/evidence/ev-contra": evidence("ev-contra", { passage: "SYNTHETIC contrary passage" }),
  "/api/v1/calculations/calc-1": {
    id: "calc-1", formula_id: "growth", formula_version: "1", unit: "ratio", status: "insufficient_inputs",
    result: null, warnings: ["SYNTHETIC: one period missing"], input_snapshot_hash: H, computed_at: "2026-10-10T00:00:00Z",
    inputs: [{ name: "revenue_2025", value: "1200.50", unit: "USD", source_record_id: "ev-1" }],
  },
  "/api/v1/instruments/opt-1/option-terms": terms(),
  "/api/v1/instruments/opt-bad/option-terms": terms({
    instrument_id: "opt-bad", multiplier: null, exercise_style: "unknown", deliverables: [], terms_verified: false,
  }),
  "/api/v1/simulations?limit=200": { next_cursor: null, items: [
    { id: "sim-1", name: "SYNTHETIC Virtual Growth", execution_model_id: "exec-next-open-v1",
      benchmark_policy_id: "bm-60-40-v2", mode: "manual", separate_from_actual: true },
  ] },
};
const fakeFetch: Fetcher = (input) => {
  const body = ROUTES[input];
  return Promise.resolve(new Response(JSON.stringify(body ?? problem404), { status: body ? 200 : 404 }));
};
const point = (spot_shock: string, at_expiry: boolean, pnl: string) => ({
  spot_shock, vol_shift: "0", elapsed_days: at_expiry ? 65 : 0, spot: "50", at_expiry,
  premium_value: "325.10", delta_units: "55.2", pnl,
});
const GRID = (instrument_id: string) => ({
  instrument_id, currency: "USD", model: "black_scholes_merton", model_applicable: false,
  quote: { source_id: "feed-SYNTHETIC", observed_at: "2026-10-14T11:59:00Z", freshness_status: "current_under_policy", rights: "permitted" },
  base: point("0", false, "0"),
  points: [point("-0.10", true, "-325.10"),
    { spot_shock: "0.10", vol_shift: "0.05", elapsed_days: 7, unavailable: { code: "vol_unavailable", reason: "No IV for SYNTHETIC leg" } }],
  greeks: { model: "black_scholes_merton", applicable_to_style: false, values: [
    { name: "delta", value: "0.552", unit: "per 1 USD move in the underlying" }] },
  collateral: null, worst_loss: null, complete: false,
});
const row = (origin: string, record: string, ref: string, extra: object = {}) => ({
  origin, record, ref, label: `SYNTHETIC ${ref}`, sample_size: 40, min_sample: 20,
  net_gain: { amount: "1500.25", currency: "CAD" }, mwr: "0.0412",
  interval: { low: "0.012", high: "0.07", level: "0.9" }, unavailable: null, ...extra,
});
const SCORECARD = {
  simulation_id: "sim-1", plan_id: "plan-SYNTHETIC", plan_version: 2, plan_frozen_at: "2026-01-01T00:00:00Z",
  currency: "CAD", start: "2026-01-01", end: "2026-09-30", interpretation: "descriptive; no significance or edge claim",
  rows: [
    row("user", "actual", "acct-real", { interval: null }),
    row("user", "virtual", "sim-1", { mwr: null, net_gain: null, unavailable: { code: "no_unique_root", reason: "MWR has no unique root" } }),
    row("ai", "recommendation", "rec-log", { sample_size: 3, mwr: "0.9" }),
    row("benchmark", "baseline", "bm-60-40-v2"),
  ],
};
const source = (extra: Partial<ResearchSource> = {}): ResearchSource => ({
  ...httpSource(fakeFetch, now),
  scenarios: (id) => Promise.resolve(checked(GRID(id), isScenarioGrid, now)),
  scorecard: () => Promise.resolve(checked(SCORECARD, isScorecard, now)),
  ...extra,
});
const open = (label: string, value: string) => {
  const input = screen.getByLabelText(label);
  fireEvent.change(input, { target: { value } });
  const form = input.closest("form");
  if (!form) throw new Error("no form");
  fireEvent.submit(form);
};

describe("helpers (hand-computed oracles)", () => {
  it("shows a ratio as a percentage by moving the decimal point, without number conversion", () => {
    const cases: [string, string][] = [["0.0412", "4.12%"], ["-0.5", "-50%"], ["1", "100%"], ["0.00001", "0.001%"],
      ["12.3456", "1234.56%"], ["0", "0%"], ["-0.0001", "-0.01%"]];
    for (const [ratio, shown] of cases) expect(percent(ratio as never)).toBe(shown);
    for (const bad of ["", "-", "1e-2", "+0.5"]) expect(() => percent(bad as never)).toThrow();
  });

  it("blocks a quote unless it is current and rights-permitted", () => {
    const q = (freshness_status: string, rights: string) =>
      ({ source_id: "f", observed_at: "2026-10-14T11:59:00Z", freshness_status, rights }) as never;
    expect(quoteBlockers(q("current_under_policy", "permitted"))).toEqual([]);
    expect(quoteBlockers(q("unknown", "permitted"))).toEqual(["quote_unknown"]);
    expect(quoteBlockers(q("stale", "restricted"))).toEqual(["quote_stale", "quote_rights_restricted"]);
    expect(quoteBlockers(q("current_under_policy", "unknown"))).toEqual(["quote_rights_unknown"]);
  });

  it("blocks scenarios for each missing or unverified contract term instead of defaulting it", () => {
    expect(termBlockers(terms() as OptionTerms)).toEqual([]);
    const bad = terms({ terms_verified: false, multiplier: null, exercise_style: "unknown", settlement: "unknown", deliverables: [], adjusted: true });
    expect(termBlockers(bad as OptionTerms)).toEqual([
      "terms_unverified", "multiplier_unknown", "exercise_style_unknown", "settlement_unknown", "deliverables_unknown",
      "adjusted_contract_unsupported",
    ]);
  });
});

describe("source", () => {
  it("uses only contract routes and answers no_contract_route for the rest without a request", async () => {
    const fetcher = vi.fn<Fetcher>(fakeFetch);
    const s = httpSource(fetcher, now);
    expect((await s.claim("clm-1")).status).toBe("ok");
    expect(fetcher).toHaveBeenLastCalledWith("/api/v1/claims/clm-1", expect.anything());
    const missing = await s.evidence("ev-none");
    expect(missing.status === "failed" && missing.code).toBe("not_found");
    const calls = fetcher.mock.calls.length;
    for (const r of [await s.scenarios("opt-1"), await s.scorecard("sim-1"), await s.claim("a/b")]) {
      expect(r.status).toBe("failed");
    }
    const sc = await s.scorecard("sim-1");
    expect(sc.status === "failed" && sc.code).toBe("no_contract_route");
    expect(fetcher.mock.calls.length).toBe(calls);
  });

  it("refuses malformed or mixed records", () => {
    expect(isSimulationPage({ next_cursor: null, items: [{ id: "s", name: "n", execution_model_id: "e",
      benchmark_policy_id: "b", mode: "manual", separate_from_actual: false }] })).toBe(false);
    const mixed = { ...SCORECARD, rows: [row("benchmark", "virtual", "x")] };
    expect(isScorecard(mixed)).toBe(false);
    expect(isScorecard({ ...SCORECARD, rows: [row("user", "actual", "x", { net_gain: { amount: "1", currency: "USD" } })] })).toBe(false);
    expect(isScorecard({ ...SCORECARD, rows: [row("user", "actual", "x", { mwr: 0.04 })] })).toBe(false);
    expect(isScorecard({ ...SCORECARD, rows: [row("user", "actual", "x", { mwr: null })] })).toBe(false); // null needs a reason
    expect(isScenarioGrid({ ...GRID("opt-1"), base: { ...point("0", false, "0"), pnl: 0 } })).toBe(false);
    const one = (extra: object) => isScorecard({ ...SCORECARD, rows: [row("user", "actual", "x", extra)] });
    expect(one({ sample_size: 0 })).toBe(true); // n = 0 is a valid, insufficient sample
    expect(one({ sample_size: -1 })).toBe(false);
    expect(one({ min_sample: 0 })).toBe(false);
    expect(one({ interval: { low: "0.07", high: "0.012", level: "0.9" } })).toBe(false); // low > high
    expect(one({ interval: { low: "-0.5", high: "0.10", level: "0.9" } })).toBe(true);
    expect(one({ interval: { low: "0.1", high: "0.10", level: "0.9" } })).toBe(true);
  });
});

describe("evidence drilldown (AT019, AT085)", () => {
  it("shows claim, sources, freshness, contrary evidence and limitations as text", async () => {
    setHash("#/research");
    render(<App fetcher={fakeFetch} now={now} research={source()} />);
    open("Claim id", "clm-1");
    const panel = await screen.findByRole("region", { name: /Claim clm-1/ });
    await waitFor(() => { expect(panel.textContent).toContain("SYNTHETIC passage ev-1"); });
    const text = panel.textContent;
    expect(text).toContain("[Forecast: uncertain, not a reported fact]");
    expect(text).toContain("[Contested: contrary evidence exists]");
    expect(text).toContain("not a forecast of investment outcome");
    expect(text).toContain("Published 2026-10-01 13:00 UTC; retrieved 2026-10-02 10:00 UTC");
    expect(text).toContain("[Deleted: the source snapshot is no longer held");
    expect(text).toContain("Passage: Unavailable (deleted)");
    expect(text).toContain("Published: Unavailable (publication_time_unknown)");
    const contra = within(panel).getByRole("list", { name: "Contrary evidence" });
    await waitFor(() => { expect(contra.textContent).toContain("SYNTHETIC contrary passage"); });
    await waitFor(() => { expect(panel.textContent).toContain("Result: Unavailable (insufficient_inputs)"); });
    expect(panel.textContent).toContain("1,200.50 USD");
    expect(panel.textContent).not.toMatch(/confidence|\d%/i);
  });

  it("rejects an id that is not a contract id before any request", () => {
    const fetcher = vi.fn<Fetcher>(fakeFetch);
    setHash("#/research");
    render(<App fetcher={fetcher} now={now} research={httpSource(fetcher, now)} />);
    open("Claim id", "../x");
    expect(screen.getByLabelText("Claim id").getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByText(/not a valid record id/)).toBeTruthy();
    expect(fetcher).not.toHaveBeenCalledWith(expect.stringContaining("claims"), expect.anything());
  });
});

describe("option scenarios (AT017, AT089)", () => {
  it("blocks payoffs and Greeks when terms are missing, and never shows them as zero", async () => {
    const scenarios = vi.fn((id: string) => source().scenarios(id));
    setHash("#/research");
    render(<App fetcher={fakeFetch} now={now} research={source({ scenarios })} />);
    open("Option instrument id", "opt-bad");
    const blocked = (await screen.findByText(/\[Scenarios blocked\]/)).closest("p");
    expect(blocked?.textContent).toMatch(/multiplier unknown.*exercise style unknown.*deliverables unknown/);
    expect(screen.getByText(/Multiplier/).closest("tr")?.textContent).toContain("Unavailable (multiplier_unknown)");
    expect(scenarios).not.toHaveBeenCalled();
    expect(screen.queryByRole("table", { name: /Scenario grid/ })).toBeNull();
  });

  it("shows decimal-string scenarios, gaps, model applicability and units", async () => {
    setHash("#/research");
    render(<App fetcher={fakeFetch} now={now} research={source()} />);
    open("Option instrument id", "opt-1");
    const grid = await screen.findByRole("table", { name: /Scenario grid for opt-1/ });
    const rows = within(grid).getAllByRole("row");
    expect(rows[2]?.textContent).toContain("Expiry payoff (exact, no model)");
    expect(rows[2]?.textContent).toContain("-325.10");
    expect(rows[3]?.textContent).toContain("Unavailable (vol_unavailable) No IV for SYNTHETIC leg");
    const main = screen.getByRole("main").textContent;
    expect(main).toContain("Model black scholes merton is not applicable to this exercise style");
    expect(main).toContain("0.552 per 1 USD move in the underlying");
    expect(main).toContain("Worst loss: Unavailable (incomplete_grid)");
    expect(main).toContain("[Incomplete grid]");
  });

  it.each([
    ["stale", "permitted", "quote stale"], ["unknown", "permitted", "quote unknown"],
    ["current_under_policy", "restricted", "quote rights restricted"],
    ["current_under_policy", "unknown", "quote rights unknown"],
  ])("blocks scenarios on a quote that is %s with rights %s", async (freshness_status, rights, text) => {
    // The quote arrives inside the grid, so the grid is requested once and then withheld.
    const grid = { ...GRID("opt-1"), quote: { ...GRID("opt-1").quote, freshness_status, rights } };
    const scenarios = vi.fn(() => Promise.resolve(checked(grid, isScenarioGrid, now)));
    setHash("#/research");
    render(<App fetcher={fakeFetch} now={now} research={source({ scenarios })} />);
    open("Option instrument id", "opt-1");
    expect((await screen.findByText(/\[Scenarios blocked\]/)).closest("p")?.textContent).toContain(text);
    expect(scenarios).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("table", { name: /Scenario grid|Greeks/ })).toBeNull();
    expect(screen.getByRole("main").textContent).not.toMatch(/325\.10|0\.552/);
  });

  it("marks an incomplete grid and withholds its worst loss and collateral", async () => {
    const grid = { ...GRID("opt-1"), worst_loss: "-5", collateral: { amount: "0", currency: "USD" } };
    setHash("#/research");
    render(<App fetcher={fakeFetch} now={now} research={source({ scenarios: () => Promise.resolve(checked(grid, isScenarioGrid, now)) })} />);
    open("Option instrument id", "opt-1");
    await screen.findByRole("table", { name: /Scenario grid for opt-1/ });
    const main = screen.getByRole("main").textContent;
    expect(main).toContain("[Incomplete grid]");
    expect(main).toContain("Worst loss: Unavailable (incomplete_grid)");
    expect(main).toContain("Collateral required (separate from premium P&L): Unavailable (incomplete_grid)");
    expect(main).not.toMatch(/-5 USD|: 0 USD/);
  });
});

describe("virtual mode and scorecards (AT063, AT064, AT090)", () => {
  it("keeps the SIMULATED label across navigation until the user leaves", async () => {
    setHash("#/simulation");
    render(<App fetcher={fakeFetch} now={now} research={source()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Work with SYNTHETIC Virtual Growth in simulation mode" }));
    for (const hash of ["#/accounts", "#/research", "#/decisions", "#/overview", "#/simulation"]) {
      go(hash);
      await screen.findByRole("heading", { level: 1 });
      expect(screen.getByRole("status", { name: "Mode" }).textContent).toMatch(/^\[SIMULATED\] Simulation mode: SYNTHETIC Virtual Growth/);
      expect(document.title).toBe("[SIMULATED] quantweave");
    }
    fireEvent.click(screen.getByRole("button", { name: "Leave simulation mode" }));
    expect(screen.getByRole("status", { name: "Mode" }).textContent).toBe("Mode: real-account analysis.");
    expect(document.title).toBe("quantweave");
  });

  it("separates user, AI and benchmark rows, never totals them, and states sample and uncertainty", async () => {
    setHash("#/simulation");
    render(<App fetcher={fakeFetch} now={now} research={source()} />);
    fireEvent.click(await screen.findByRole("button", { name: /Work with SYNTHETIC Virtual Growth/ }));
    const user = await screen.findByRole("table", { name: /^User decisions/ });
    const ai = screen.getByRole("table", { name: /^AI recommendations/ });
    const bm = screen.getByRole("table", { name: /^Benchmarks/ });
    const [, actual, virtual] = within(user).getAllByRole("row");
    expect(actual?.textContent).toContain("[Real account]");
    expect(actual?.textContent).toContain("1,500.25 CAD");
    expect(actual?.textContent).toContain("No interval: uncertainty not estimated");
    expect(virtual?.textContent).toContain("[SIMULATED]");
    expect(virtual?.textContent).toContain("Unavailable (no_unique_root) MWR has no unique root");
    expect(ai.textContent).toContain("[Insufficient evidence] n = 3, below the minimum of 20; no result shown");
    expect(ai.textContent).not.toContain("90%");
    expect(bm.textContent).toContain("4.12%");
    expect(bm.textContent).toContain("90% interval 1.2% to 7%");
    expect(screen.getByRole("main").textContent).not.toMatch(/total/i);
    expect(screen.getByRole("main").textContent).toContain("Plan plan-SYNTHETIC v2, frozen 2026-01-01 00:00 UTC");
  });

  it("offers no order controls in research or simulation views (AT056)", async () => {
    for (const hash of ["#/research", "#/simulation"]) {
      setHash(hash);
      render(<App fetcher={fakeFetch} now={now} research={source()} />);
      await screen.findByRole("heading", { level: 1 });
      if (hash === "#/simulation") await screen.findByRole("table", { name: /Virtual portfolios/ });
      for (const b of screen.getAllByRole("button")) expect(b.textContent).not.toMatch(/buy|sell|submit|order|trade/i);
      cleanup();
    }
  });
});
