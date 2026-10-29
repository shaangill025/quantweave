// @vitest-environment jsdom
// T055 increment 1: the read-only Improvement view. Every record below is SYNTHETIC. Capability
// shapes follow apps/api/src/qw_api/capabilities.py and qw_domain.capabilities (code_improvement and
// optimizer_recursion); experiments follow docs/spec/contracts experiment.schema.json and its
// experiment_recursive.valid.json example as amended by project contract 0.6.0 (item coverage, nullable
// parent_optimizer_version, blocked and insufficient_evidence), inside the page with coverage.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { App } from "./main";
import type { Fetcher } from "./api";
import { isExperimentPage } from "./reads";

const setHash = (hash: string) => { window.history.replaceState(null, "", hash || window.location.pathname); };
afterEach(() => { cleanup(); setHash(""); });
const now = () => new Date("2026-10-17T12:00:00Z");
const problem = (status: number, code: string) => ({
  type: `/problems/${code}`, title: code, status, code, detail: "SYNTHETIC failure.", correlation_id: "corr-SYNTHETIC",
  retryable: false, retry_after_s: null, field_errors: [], reasons: [], error_schema: "1",
});
const IMPROVE = ["experiment_budget_unqualified", "human_promotion_unqualified", "independent_assessment_unqualified",
  "model_route_unqualified", "sandbox_unqualified"];
const caps = (enabled: boolean) => ({ deployment: "self_hosted", features: [
  { id: "ai_evaluator", enabled, reason_codes: enabled ? [] : ["evaluator_independence_unqualified"] },
  { id: "ai_research", enabled, reason_codes: enabled ? [] : ["model_route_unqualified"] },
  { id: "strategy_proposals", enabled, reason_codes: enabled ? [] : ["strategy_unqualified"] },
  { id: "code_improvement", enabled, reason_codes: enabled ? [] : IMPROVE },
  { id: "optimizer_recursion", enabled, reason_codes: enabled ? [] : IMPROVE },
] });
const exp = (id: string, extra: object = {}) => ({
  id, target: "recursive_optimizer", scope: "installation", scope_id: "INSTALL-SYN", state: "proposed",
  baseline_version: "OPT-V0", candidate_version: "OPT-V1", hypothesis: "SYNTHETIC: prefer date-error cases.",
  protocol_id: "PROTO-SYN", protected_assessor_version: "ASSESSOR-V1", data_snapshot_ids: ["DATA-SYN"],
  budget_reservation_id: "BUD-SYN", sandbox_profile_id: "SANDBOX-UNQUALIFIED", candidate_hash: "a".repeat(64),
  evaluation_receipt_ids: [], parent_optimizer_version: "OPT-V0", next_cycle_run_id: null,
  requires_human_promotion: true, release_receipt_id: null,
  coverage: { status: "partial", reasons: ["evidence:synthetic_only"] }, ...extra,
});
const NO_PARENT = { parent_optimizer_version: null,
  coverage: { status: "partial", reasons: ["evidence:synthetic_only", "none:parent_optimizer_version"] } };
// EXP-SYN-2 claims V1 was promoted and ran the next cycle: a gate-lifted claim while the gate is closed.
const EXPS = { next_cursor: null, coverage: { status: "partial", reasons: ["synthetic:fixture"] }, items: [
  exp("EXP-SYN-1"),
  exp("EXP-SYN-2", { state: "promoted", baseline_version: "OPT-V1", candidate_version: "OPT-V2",
    parent_optimizer_version: "OPT-V1", evaluation_receipt_ids: ["EVAL-SYN-1"], release_receipt_id: "REL-SYN-1",
    next_cycle_run_id: "RUN-SYN-1" }),
  exp("EXP-SYN-3", { target: "research", scope: "tenant", scope_id: "T-SYN", state: "awaiting_review", ...NO_PARENT }),
] };
// 0.6.0 stalled states, as apps/api serves them: blocked code target and insufficient research evidence.
const STALLED = { ...EXPS, items: [
  exp("EXP-SYN-4", { state: "blocked", coverage: { status: "partial",
    reasons: ["blocked:sandbox_unqualified", "evidence:synthetic_only"] } }),
  exp("EXP-SYN-5", { target: "research", scope: "tenant", scope_id: "T-SYN", state: "insufficient_evidence",
    ...NO_PARENT, coverage: { status: "partial", reasons: ["insufficient:no_qualifying_trial",
      "none:parent_optimizer_version"] } }),
] };
const HANG = Symbol("hang");
const routes = (opts: { enabled?: boolean; experiments?: boolean; stalled?: boolean; capsDown?: boolean; capsHang?: boolean;
  omit?: string[]; } = {}) => {
  const r: Record<string, unknown> = {};
  const c = caps(opts.enabled ?? false);
  if (!opts.capsDown) r["/api/v1/capabilities"] = { ...c, features: c.features.filter((f) => !opts.omit?.includes(f.id)) };
  if (opts.capsHang) r["/api/v1/capabilities"] = HANG;
  if (opts.experiments) r["/api/v1/experiments?limit=200"] = opts.stalled ? STALLED : EXPS;
  return r;
};
const fetcher = (r: Record<string, unknown>) => vi.fn<Fetcher>((input) => {
  const body = r[input];
  if (body === HANG) return new Promise<Response>(() => undefined); // still loading
  const fail = input.endsWith("/capabilities") ? problem(500, "internal_error") : problem(404, "not_found");
  return Promise.resolve(new Response(JSON.stringify(body ?? fail), { status: body ? 200 : fail.status }));
});
const show = async (r: Record<string, unknown>) => {
  setHash("#/improvement");
  const f = fetcher(r);
  render(<App fetcher={f} now={now} />);
  await screen.findByRole("table", { name: /six improvement targets/ });
  return f;
};
const rowOf = (table: HTMLElement, name: RegExp) =>
  within(table).getAllByRole("row").find((tr) => name.test(within(tr).queryByRole("rowheader")?.textContent ?? ""));

describe("improvement targets", () => {
  it("lists all six targets and shows the blocked gates with their reasons", async () => {
    await show(routes());
    const table = screen.getByRole("table", { name: /six improvement targets/ });
    expect(within(table).getAllByRole("rowheader").map((h) => h.textContent)).toEqual([
      "User adaptation (personal_adaptation)", "Research and retrieval (research)", "Strategy and ranking (strategy)",
      "Decision evaluator (evaluator)", "Application and agent code (application_code)", "Optimizer recursion (recursive_optimizer)",
    ]);
    for (const t of [/application_code/, /recursive_optimizer/]) {
      const text = rowOf(table, t)?.textContent ?? "";
      expect(text).toContain("[Blocked]");
      for (const r of IMPROVE) expect(text).toContain(r);
    }
    const research = rowOf(table, /\(research\)/)?.textContent ?? "";
    expect(research).toContain("No served gate specific to improvement");
    expect(research).toContain("ai_research");
    expect(research).toContain("[Blocked]");
    expect(research).toContain("model_route_unqualified");
    expect(rowOf(table, /\(evaluator\)/)?.textContent).toContain("ai_evaluator");
    expect(rowOf(table, /\(strategy\)/)?.textContent).toContain("strategy_proposals");
    expect(rowOf(table, /personal_adaptation/)?.textContent).toContain("No served capability gate");
    // listExperiments is in the contract but not served: said plainly, never an empty list.
    expect(rowOf(table, /\(research\)/)?.textContent).toContain("Unavailable (not_found)");
    expect(screen.getByRole("main").textContent).toContain("Not connected: this API build does not serve experiments yet.");
    expect(screen.getByRole("main").textContent).not.toContain("No experiment is recorded");
  });

  it("marks a gate unknown, not open, when capabilities cannot be read", async () => {
    await show(routes({ capsDown: true, experiments: true }));
    const table = screen.getByRole("table", { name: /six improvement targets/ });
    expect(rowOf(table, /recursive_optimizer/)?.textContent).toContain("[Gate unknown]");
    expect(rowOf(table, /recursive_optimizer/)?.textContent).toContain("Unavailable (internal_error)");
    const lineage = await screen.findByRole("table", { name: /Optimizer lineage/ });
    expect(rowOf(lineage, /EXP-SYN-2/)?.textContent).toContain("Not relied on");
  });
});

describe("gates fail closed", () => {
  it("marks the gates unknown when capabilities omit the improvement features", async () => {
    await show(routes({ experiments: true, enabled: true, omit: ["code_improvement", "optimizer_recursion", "ai_research"] }));
    const table = screen.getByRole("table", { name: /six improvement targets/ });
    for (const t of [/application_code/, /recursive_optimizer/, /\(research\)/]) {
      expect(rowOf(table, t)?.textContent).toContain("[Gate unknown]");
      expect(rowOf(table, t)?.textContent).toContain("feature_not_served");
    }
    const lineage = await screen.findByRole("table", { name: /Optimizer lineage/ });
    expect(rowOf(lineage, /EXP-SYN-2/)?.textContent).toContain("Not relied on");
    const all = screen.getByRole("table", { name: /^Experiments/ });
    expect(rowOf(all, /EXP-SYN-3/)?.textContent).toContain("Not relied on");
  });

  it("marks the gates unknown while capabilities are still loading", async () => {
    await show(routes({ experiments: true, capsHang: true }));
    const table = screen.getByRole("table", { name: /six improvement targets/ });
    expect(rowOf(table, /recursive_optimizer/)?.textContent).toContain("[Gate unknown]");
    expect(rowOf(table, /recursive_optimizer/)?.textContent).toContain("capabilities_loading");
    expect(rowOf(table, /\(research\)/)?.textContent).toContain("capabilities_loading");
    const lineage = await screen.findByRole("table", { name: /Optimizer lineage/ });
    expect(rowOf(lineage, /EXP-SYN-2/)?.textContent).toContain("Not relied on");
    const all = screen.getByRole("table", { name: /^Experiments/ });
    expect(rowOf(all, /EXP-SYN-3/)?.textContent).toContain("Not relied on");
  });
});

describe("recursive demonstration", () => {
  it("reads not proven, with the blocking reason, while the real path is blocked", async () => {
    await show(routes());
    expect(screen.getByRole("status", { name: "Recursion status" }).textContent).toContain("[Not proven]");
    const why = screen.getByRole("list", { name: "Why recursion is not proven" }).textContent;
    expect(why).toContain("The real path is blocked");
    expect(why).toContain("sandbox_unqualified");
    expect(why).toContain("cannot verify the lineage receipts");
  });

  it("never shows a gate-lifted promotion as real and shows V0 to V1 lineage as recorded fields", async () => {
    await show(routes({ experiments: true }));
    const lineage = await screen.findByRole("table", { name: /Optimizer lineage \(recorded, not proof\)/ });
    const v0 = rowOf(lineage, /EXP-SYN-1/)?.textContent ?? "";
    expect(v0).toContain("OPT-V0 to OPT-V1");
    expect(v0).toContain("EXP-SYN-2"); // the later experiment run by OPT-V1
    expect(v0).not.toContain("Not relied on"); // proposed needs no open gate
    const v1 = rowOf(lineage, /EXP-SYN-2/)?.textContent ?? "";
    expect(v1).toContain("[state: promoted]");
    expect(v1).toContain("Not relied on");
    expect(v1).toContain("RUN-SYN-1");
    expect(screen.getByRole("status", { name: "Recursion status" }).textContent).toContain("[Not proven]");
    const all = screen.getByRole("table", { name: /^Experiments/ });
    expect(rowOf(all, /EXP-SYN-2/)?.textContent).toContain("Unavailable (no_contract_read:evaluation_receipt)");
    expect(rowOf(all, /EXP-SYN-2/)?.textContent).toContain("EVAL-SYN-1");
    expect(rowOf(all, /EXP-SYN-3/)?.textContent).toContain("private (tenant)");
    // The related ai_research gate is closed, so the research review state is not relied on.
    expect(rowOf(all, /EXP-SYN-3/)?.textContent).toContain("Not relied on");
    expect(screen.getByRole("list", { name: "Experiment list coverage reasons" }).textContent).toContain("synthetic:fixture");
  });

  it("stays not proven even with every gate open, because no receipt can be verified", async () => {
    await show(routes({ enabled: true, experiments: true }));
    const lineage = await screen.findByRole("table", { name: /Optimizer lineage/ });
    const v1 = rowOf(lineage, /EXP-SYN-2/)?.textContent ?? "";
    expect(v1).not.toContain("Not relied on");
    expect(v1).toContain("unverified");
    const table = screen.getByRole("table", { name: /six improvement targets/ });
    expect(rowOf(table, /recursive_optimizer/)?.textContent).toContain("recorded states are not independently verified here");
    expect(table.textContent).not.toContain("every recorded gate passed");
    const all = screen.getByRole("table", { name: /^Experiments/ });
    expect(rowOf(all, /EXP-SYN-3/)?.textContent).toContain("unverified");
    expect(rowOf(all, /EXP-SYN-3/)?.textContent).not.toContain("Not relied on");
    expect(screen.getByRole("status", { name: "Recursion status" }).textContent).toContain("[Not proven]");
    expect(screen.getByRole("main").textContent).not.toMatch(/\bProven\b|recursion (is )?proven/);
  });
});

describe("approval separation", () => {
  it("renders no governance control and sends only GET requests", async () => {
    const f = await show(routes({ experiments: true }));
    await screen.findByRole("table", { name: /Optimizer lineage/ });
    const main = screen.getByRole("main");
    const controls = within(main).queryAllByRole("button").concat(within(main).queryAllByRole("link"));
    for (const c of controls) expect(c.textContent).not.toMatch(/promot|approv|install|adopt|roll ?back|cancel|sign/i);
    expect(main.querySelectorAll("form, input, select, textarea")).toHaveLength(0);
    expect(main.textContent).toContain("no promote, approve, install, adopt or rollback control");
    expect(f.mock.calls.length).toBeGreaterThan(0);
    for (const [path, init] of f.mock.calls) {
      expect(init?.method ?? "GET").toBe("GET");
      expect(init?.body).toBeUndefined();
      expect(path).toMatch(/^\/api\/v1\/(capabilities|experiments\?limit=200|releases\?limit=200|deployment\/events\?limit=200|session\/approval-classes|budgets\?limit=200)$/);
    }
    expect(new Set(f.mock.calls.map(([p]) => p)).size).toBe(6);
  });
});

describe("experiment page guard fails closed", () => {
  const bad: [string, unknown][] = [
    ["human promotion not required", { ...EXPS, items: [exp("E", { requires_human_promotion: false })] }],
    ["unknown target", { ...EXPS, items: [exp("E", { target: "risk_limits" })] }],
    ["unknown state", { ...EXPS, items: [exp("E", { state: "installed" })] }],
    ["short candidate hash", { ...EXPS, items: [exp("E", { candidate_hash: "abc" })] }],
    ["unknown scope", { ...EXPS, items: [exp("E", { scope: "global" })] }],
    ["duplicate experiment id", { ...EXPS, items: [exp("E"), exp("E")] }],
    ["page without coverage", { ...EXPS, coverage: undefined }],
    ["missing lineage field", { ...EXPS, items: [{ ...exp("E"), parent_optimizer_version: undefined }] }],
    ["item without coverage", { ...EXPS, items: [{ ...exp("E"), coverage: undefined }] }],
    ["optimizer without a parent", { ...EXPS, items: [exp("E", NO_PARENT)] }],
    ["parent on another target", { ...EXPS, items: [exp("E", { target: "research" })] }],
    ["null parent not named", { ...EXPS, items: [exp("E", { ...NO_PARENT, target: "research", coverage: {
      status: "partial", reasons: ["evidence:synthetic_only"] } })] }],
    ["a set parent named as absent", { ...EXPS, items: [exp("E", { coverage: { status: "partial",
      reasons: ["none:parent_optimizer_version"] } })] }],
    ["complete coverage with reasons", { ...EXPS, items: [exp("E", { coverage: { status: "complete",
      reasons: ["blocked:sandbox_unqualified"] } })] }],
  ];
  it.each(bad)("refuses %s", (_name, v) => {
    expect(isExperimentPage(EXPS)).toBe(true);
    expect(isExperimentPage(STALLED)).toBe(true);
    expect(isExperimentPage(v)).toBe(false);
  });
});

describe("stalled experiments (0.6.0)", () => {
  it("reads blocked and insufficient evidence honestly, with reasons, and not relied on behind a closed gate", async () => {
    await show(routes({ experiments: true, stalled: true }));
    const all = await screen.findByRole("table", { name: /^Experiments/ });
    const blocked = rowOf(all, /EXP-SYN-4/)?.textContent ?? "";
    expect(blocked).toContain("[state: blocked] Blocked: not run or not assessed; not a success");
    expect(blocked).toContain("Not relied on");
    expect(blocked).not.toContain("cannot be real here");
    expect(blocked).toContain("blocked:sandbox_unqualified");
    const thin = rowOf(all, /EXP-SYN-5/)?.textContent ?? "";
    expect(thin).toContain("Insufficient evidence: not a pass");
    expect(thin).toContain("Not relied on");
    expect(thin).toContain("insufficient:no_qualifying_trial");
    expect(thin).toContain("optimizer none (not an optimizer experiment)");
    for (const text of [blocked, thin]) expect(text.replaceAll("not a success", "")).not.toMatch(/accept|passed|success|approved/i);
    expect(screen.getByRole("status", { name: "Recursion status" }).textContent).toContain("[Not proven]");
  });

  it("stays unverified, never a success, with every gate open", async () => {
    await show(routes({ experiments: true, stalled: true, enabled: true }));
    const all = await screen.findByRole("table", { name: /^Experiments/ });
    for (const id of [/EXP-SYN-4/, /EXP-SYN-5/]) {
      const text = rowOf(all, id)?.textContent ?? "";
      expect(text).not.toContain("Not relied on");
      expect(text).toContain("unverified");
      expect(text).toMatch(/not a success|not a pass/);
    }
  });
});
