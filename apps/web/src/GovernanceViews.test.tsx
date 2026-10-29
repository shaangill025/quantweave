// @vitest-environment jsdom
// T055 governance panels: the read-only governance panels in the Improvement view. Every record below is
// SYNTHETIC. Shapes follow project contract 0.13.0 (contracts/amendments.json: releasePage,
// deploymentEventPage, approval_classes) and openapi.yaml BudgetSummaryPage with the C-02 coverage,
// with the reasons apps/api serves (qw_api.releases, qw_api.routes approval_classes, qw_api.budgets).
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { App } from "./main";
import type { Fetcher } from "./api";
import { isApprovalClasses, isBudgetPage, isDeploymentEventPage, isReleasePage } from "./reads";

const setHash = (hash: string) => { window.history.replaceState(null, "", hash || window.location.pathname); };
afterEach(() => { cleanup(); setHash(""); });
const now = () => new Date("2026-10-29T12:00:00Z");
const problem = (status: number, code: string, detail = "SYNTHETIC failure.") => ({
  type: `/problems/${code}`, title: code, status, code, detail, correlation_id: "corr-SYNTHETIC",
  retryable: false, retry_after_s: null, field_errors: [], reasons: [], error_schema: "1",
});
const hex = (c: string, n = 64) => c.repeat(n);
// What qw_api.releases names for a tenant member: hidden identities, no receipts, no strategy.
const HIDDEN = ["none:signature/key_id", "unavailable:candidate_id", "unavailable:experiment_id",
  "unavailable:receipts", "unavailable:verdict_id"];
const NOT_STRATEGY = ["none:strategy_material_hash", "none:strategy_version", "none:tenant_adoption"];
const paged = (items: { id: string; coverage: { reasons: string[] } }[]) =>
  items.flatMap((x) => x.coverage.reasons.map((r) => `${r.slice(0, r.indexOf(":"))}:${x.id}/${r.slice(r.indexOf(":") + 1)}`));
const rel = (id: string, extra: object = {}) => ({
  id, component: "application_code:application_code", target: "application_code", change_kind: "application", experiment_id: null,
  candidate_id: null, verdict_id: null, rollback_version_id: "REL-SYN-0", candidate_hash: hex("a"),
  artifact_hash: hex("b"), protocol_hash: hex("c"), manifest_hash: hex("d"),
  required_approvals: ["maintainer_release", "operator_install"],
  compatibility: { min_schema: 1, max_schema: 2, writes_schema: 2 }, strategy_version: null,
  strategy_material_hash: null, verdict_source: "caller_supplied",
  signature: { key_id: null, algorithm: "test-hmac-sha256", signature_reference: `sig-sha256.${hex("e", 32)}`,
    verification: "verified", reasons: [] },
  install: { state: "head", installed_at: "2026-10-22T10:00:00+02:00", runnable: true, self_approved: false, reasons: [] },
  receipt_status: "unavailable", approved_at: null, user_adoption_required: false, tenant_adoption: null,
  coverage: { status: "partial", reasons: [...HIDDEN, ...NOT_STRATEGY].sort() }, ...extra,
});
const RELS = [
  rel("REL-SYN-1", { install: { state: "withdrawn", installed_at: "2026-10-21T09:00:00Z", runnable: false,
    self_approved: true, reasons: ["withdrawn"] } }),
  rel("REL-SYN-2", { rollback_version_id: "REL-SYN-1",
    signature: { key_id: null, algorithm: "test-hmac-sha256", signature_reference: `sig-sha256.${hex("f", 32)}`,
      verification: "refused", reasons: ["key_revoked:SYNTHETIC"] } }),
  rel("REL-SYN-3", { component: "strategy:income-SYN", target: "strategy", change_kind: "strategy",
    required_approvals: ["maintainer_release", "operator_install", "user_strategy_adoption"],
    strategy_version: "2.0.0", strategy_material_hash: hex("9"), user_adoption_required: true,
    tenant_adoption: { state: "not_adopted", version: null, reasons: ["adoption_required"] },
    coverage: { status: "partial", reasons: [...HIDDEN, "none:tenant_adoption/version"].sort() } }),
];
const RELEASES = { items: RELS, next_cursor: null, coverage: { status: "partial", reasons: paged(RELS) } };
const WHO = ["none:operator_id", "none:step_up_receipt_id"];
const ev = (id: string, kind: string, none: string[], extra: object) => ({
  id, kind, component: "application_code:application_code", at: "2026-10-22T10:00:00Z", release_id: null, from_release_id: null,
  to_release_id: null, reasons: [], schema_after: 2, operator_id: null, step_up_receipt_id: null, ...extra,
  coverage: { status: "partial", reasons: [...WHO, ...none.map((f) => `none:${f}`)].sort() },
});
const UNMOVED = ["from_release_id", "to_release_id"];
const EVS = [
  ev("EV-SYN-1", "install", UNMOVED, { release_id: "REL-SYN-1" }),
  ev("EV-SYN-2", "rollback", ["release_id"], { from_release_id: "REL-SYN-1", to_release_id: "REL-SYN-0" }),
  ev("EV-SYN-3", "pause", UNMOVED, { release_id: "REL-SYN-0", reasons: ["REL-SYN-1:withdrawn"] }),
];
const EVENTS = { items: EVS, next_cursor: null, coverage: { status: "partial", reasons: paged(EVS) } };
// A tenant member on hosted, as qw_api.routes serves it: nothing held, nothing served.
const cls = (k: string, scope: string, reasons: string[], extra: object = {}) => ({
  class: k, authority_scope: scope, held: false, step_up: { required: true, satisfied: false, expires_at: null },
  served_operations: [], reasons, ...extra,
});
const CLASSES = { deployment: "hosted", evaluated_at: "2026-10-29T11:59:00Z", advisory: true, classes: [
  cls("maintainer_release", "hosted_maintainer", ["role_not_served", "step_up_required", "operation_not_served"]),
  cls("operator_install", "hosted_maintainer", ["role_not_served", "step_up_required", "operation_not_served"]),
  cls("user_strategy_adoption", "user_strategy_adoption", ["role_not_assigned", "step_up_required", "operation_not_served"]),
], coverage: { status: "complete", reasons: [] } };
// Three books: October saturated (authorized), September revoked, August never authorized.
const CATS = ["inference", "monitoring", "verification", "research", "improvement"];
const BOUND = "99999999999999.999999";
const book = (period: string, amounts: Partial<Record<string, string[]>> = {}) => CATS.map((category) => {
  const [cap, spent, reserved, available] = amounts[category] ?? [category === "improvement" ? "20" : "100", "4", "6", "90"];
  return { scope_id: "T-SYN", period, category, cap_usd: cap, spent_usd: spent, reserved_usd: reserved, available_usd: available };
});
const reasons = (period: string, kind: string, detail: string, cats = CATS) =>
  cats.map((c) => `${kind}:${period}:${c}/${detail}`);
const SHARED = (p: string) => reasons(p, "shared", "cap_is_inference_pool", ["monitoring", "verification", "research"]);
const BUDGETS = {
  items: [
    ...book("2026-10", { inference: ["100", BOUND, "0", "-99999999999999.999999"], improvement: ["20", "0", "0", "-99999999999999.999999"] }),
    ...book("2026-09"), ...book("2026-08"),
  ],
  next_cursor: null,
  coverage: { status: "partial", reasons: [
    ...SHARED("2026-10"), ...reasons("2026-10", "saturated", "totals_at_bound", ["inference", "improvement"]),
    ...SHARED("2026-09"), ...reasons("2026-09", "blocked", "spending_authorization_revoked"),
    ...SHARED("2026-08"), ...reasons("2026-08", "blocked", "spending_not_authorized"),
  ] },
};

type Body = unknown;
const ROUTES: Record<string, Body> = {
  "/api/v1/releases?limit=200": RELEASES, "/api/v1/deployment/events?limit=200": EVENTS,
  "/api/v1/session/approval-classes": CLASSES, "/api/v1/budgets?limit=200": BUDGETS,
};
const fetcher = (r: Record<string, Body>, fail: Record<string, [number, string, string?]> = {}) =>
  vi.fn<Fetcher>((input) => {
    const f = fail[input];
    if (f) return Promise.resolve(new Response(JSON.stringify(problem(f[0], f[1], f[2])), { status: f[0] }));
    const body = r[input];
    if (body === undefined) return Promise.resolve(new Response(JSON.stringify(problem(404, "not_found")), { status: 404 }));
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
  });
const show = async (r: Record<string, Body> = ROUTES, fail: Record<string, [number, string, string?]> = {}) => {
  setHash("#/improvement");
  const f = fetcher(r, fail);
  render(<App fetcher={f} now={now} />);
  await screen.findByRole("table", { name: /six improvement targets/ });
  return f;
};
const section = (name: string) => screen.getByRole("region", { name });
const rowOf = (table: HTMLElement, name: RegExp) =>
  within(table).getAllByRole("row").find((tr) => name.test(within(tr).queryByRole("rowheader")?.textContent ?? ""));

describe("the fixtures conform to the guards", () => {
  it("accepts every SYNTHETIC page", () => {
    expect(isReleasePage(RELEASES)).toBe(true);
    expect(isDeploymentEventPage(EVENTS)).toBe(true);
    expect(isApprovalClasses(CLASSES)).toBe(true);
    expect(isBudgetPage(BUDGETS)).toBe(true);
  });
});

describe("releases and installs", () => {
  it("shows the server's read-time state, hashes, rollback target and withdrawn release, never as proof", async () => {
    await show();
    const panel = section("Releases and installs");
    const table = await within(panel).findByRole("table", { name: /Installed releases/ });
    const withdrawn = rowOf(table, /REL-SYN-1/)?.textContent ?? "";
    expect(withdrawn).toContain("[Withdrawn] Withdrawn: rolled back from; not running");
    expect(withdrawn).toContain("[Not runnable]");
    expect(withdrawn).toContain("withdrawn");
    expect(withdrawn).toContain("[Self-approved]");
    expect(withdrawn).toContain("2026-10-21 09:00 UTC");
    const head = rowOf(table, /REL-SYN-2/)?.textContent ?? "";
    expect(withdrawn).not.toContain("Not self-approved");
    expect(head).toContain("[install: head] Head: the component's current release");
    expect(head).toContain("Not self-approved (as recorded by the server)");
    expect(head).not.toContain("[Self-approved]");
    expect(head).toContain("[Server check at read time: refused]");
    expect(head).toContain("key_revoked:SYNTHETIC");
    expect(head).toContain("Rollback target REL-SYN-1");
    expect(head).toContain(hex("d"));
    const ok = rowOf(table, /REL-SYN-3/)?.textContent ?? "";
    expect(ok).toContain("[Server check at read time: verified] Reported by the server when this page was read, not checked by this app");
    expect(ok).toContain("2026-10-22 08:00 UTC"); // +02:00 normalised to UTC
    expect(ok).toContain("Your adoption is required");
    expect(ok).toContain("Not adopted by your tenant; version none");
    for (const text of [withdrawn, head, ok]) {
      expect(text).toContain("[receipt_status: unavailable] not stored, so no approval is shown as proven");
      expect(text).toContain("Approved at: Unavailable (unavailable:receipts)");
      expect(text).toContain("experiment Unavailable (unavailable:experiment_id)");
      expect(text).toContain("key Unavailable (none:signature/key_id)");
      expect(text).not.toMatch(/\[Proven\]|verified by this app/);
    }
    expect(rowOf(table, /REL-SYN-1/)?.textContent).toContain("Unavailable (none:tenant_adoption)");
    expect(within(panel).getByText(/^Releases: Retrieved 2026-10-29 12:00 UTC/)).toBeTruthy();
  });

  it("shows a paused release as paused and not runnable", async () => {
    const paused = rel("REL-SYN-4", { install: { state: "paused", installed_at: "2026-10-23T09:00:00Z", runnable: false,
      self_approved: false, reasons: ["paused"] } });
    const page = { items: [paused], next_cursor: null, coverage: { status: "partial", reasons: paged([paused]) } };
    expect(isReleasePage(page)).toBe(true);
    await show({ ...ROUTES, "/api/v1/releases?limit=200": page });
    const table = await within(section("Releases and installs")).findByRole("table", { name: /Installed releases/ });
    const text = rowOf(table, /REL-SYN-4/)?.textContent ?? "";
    expect(text).toContain("[install: paused] Paused: suspended");
    expect(text).toContain("[Not runnable]");
    expect(text).not.toContain("Head:");
  });

  it("shows install, rollback and pause history with withheld identities named", async () => {
    await show();
    const events = await within(section("Releases and installs")).findByRole("table", { name: /Install, rollback and pause/ });
    expect(rowOf(events, /EV-SYN-1/)?.textContent).toContain("Installed REL-SYN-1");
    expect(rowOf(events, /EV-SYN-2/)?.textContent).toContain("Withdrew REL-SYN-1; restored REL-SYN-0");
    const pause = rowOf(events, /EV-SYN-3/)?.textContent ?? "";
    expect(pause).toContain("Paused REL-SYN-0");
    expect(pause).toContain("REL-SYN-1:withdrawn");
    expect(pause).toContain("Operator Unavailable (none:operator_id); step-up receipt Unavailable (none:step_up_receipt_id)");
  });

  it("says no history for this session on a 404, never that the route is not served", async () => {
    await show(ROUTES, { "/api/v1/releases?limit=200": [404, "not_found", "No release history for this session."],
      "/api/v1/deployment/events?limit=200": [404, "not_found", "No release history for this session."] });
    const panel = section("Releases and installs");
    await within(panel).findAllByRole("alert");
    expect(panel.textContent).toContain("Releases: Unavailable (not_found) No release history for this session.");
    expect(panel.textContent).toContain("the server reports no recorded deployment this tenant belongs to");
    expect(panel.textContent).not.toContain("Not connected");
    expect(within(panel).queryByRole("table")).toBeNull();
  });
});

describe("approval classes", () => {
  it("shows that no governance action is served and nothing is held", async () => {
    await show();
    const panel = section("Approval classes");
    const status = await within(panel).findByRole("status", { name: "Governance actions" });
    expect(status.textContent).toContain("[No governance action is served]");
    const table = within(panel).getByRole("table", { name: "Your approval classes" });
    for (const k of ["maintainer_release", "operator_install", "user_strategy_adoption"]) {
      const text = rowOf(table, new RegExp(k))?.textContent ?? "";
      expect(text).toContain("[Not held]");
      expect(text).toContain("None served");
      expect(text).toContain("operation_not_served");
      expect(text).toContain("Not satisfied");
    }
    expect(rowOf(table, /maintainer_release/)?.textContent).toContain("role_not_served");
    expect(rowOf(table, /user_strategy_adoption/)?.textContent).toContain("role_not_assigned");
    expect(panel.textContent).toContain("held never means an action will succeed");
    expect(panel.textContent).toContain("evaluated 2026-10-29 11:59 UTC");
  });

  it("keeps held advisory for an owner with a step-up, still with no control", async () => {
    const owner = { ...CLASSES, classes: CLASSES.classes.map((k) => k.class === "user_strategy_adoption"
      ? { ...k, held: true, step_up: { required: true, satisfied: true, expires_at: "2026-10-29T12:10:00Z" },
        reasons: ["operation_not_served"] } : k) };
    expect(isApprovalClasses(owner)).toBe(true);
    await show({ ...ROUTES, "/api/v1/session/approval-classes": owner });
    const table = await within(section("Approval classes")).findByRole("table", { name: "Your approval classes" });
    const text = rowOf(table, /user_strategy_adoption/)?.textContent ?? "";
    expect(text).toContain("Held (advisory, not a grant)");
    expect(text).toContain("Satisfied until 2026-10-29 12:10 UTC");
    expect(screen.getByRole("status", { name: "Governance actions" }).textContent).toContain("No governance action is served");
    expect(within(screen.getByRole("main")).queryAllByRole("button")).toHaveLength(0);
  });
});

describe("served operations", () => {
  it("lists served operations per class, never merged, and still renders no control", async () => {
    const some = { ...CLASSES, classes: CLASSES.classes.map((k) => k.class === "user_strategy_adoption"
      ? { ...k, served_operations: ["adoptStrategy"], reasons: ["role_not_assigned", "step_up_required"] }
      : k.class === "operator_install" ? { ...k, served_operations: ["installRelease", "rollbackRelease"],
        reasons: ["role_not_served", "step_up_required"] } : k) };
    expect(isApprovalClasses(some)).toBe(true);
    await show({ ...ROUTES, "/api/v1/session/approval-classes": some });
    const panel = section("Approval classes");
    const status = await within(panel).findByRole("status", { name: "Governance actions" });
    expect(status.textContent).toContain(
      "[Operations listed by the server] operator_install: installRelease, rollbackRelease; user_strategy_adoption: adoptStrategy.");
    expect(status.textContent).toContain("This view still has no control for them.");
    expect(status.textContent).not.toContain("No governance action is served");
    const table = within(panel).getByRole("table", { name: "Your approval classes" });
    expect(rowOf(table, /operator_install/)?.textContent).toContain("installRelease, rollbackRelease");
    expect(rowOf(table, /user_strategy_adoption/)?.textContent).toContain("adoptStrategy");
    expect(rowOf(table, /user_strategy_adoption/)?.textContent).not.toContain("installRelease");
    expect(rowOf(table, /maintainer_release/)?.textContent).toContain("None served");
    const main = screen.getByRole("main");
    expect(within(main).queryAllByRole("button")).toHaveLength(0);
    expect(main.querySelectorAll("form, input, select, textarea")).toHaveLength(0);
  });
});

describe("budgets", () => {
  it("shows blocked, revoked, saturated and shared books prominently; a blocked book never looks spendable", async () => {
    await show();
    const panel = section("Budgets");
    const oct = await within(panel).findByRole("table", { name: /Budget 2026-10/ });
    const pool = rowOf(oct, /Inference pool/)?.textContent ?? "";
    expect(pool).toContain("99,999,999,999,999.999999 USD"); // decimal string, grouped, never a float
    expect(pool).toContain("[Totals at bound]");
    expect(pool).toContain("[Overrun]");
    expect(pool).not.toContain("Not spendable");
    expect(rowOf(oct, /\(research\)/)?.textContent).toContain("[Shared pool cap]");
    expect(rowOf(oct, /\(improvement\)/)?.textContent).not.toContain("Shared pool cap");
    expect(within(panel).queryByRole("alert", { name: "Budget 2026-10 blocked" })).toBeNull();
    for (const [p, why] of [["2026-09", "spending_authorization_revoked"], ["2026-08", "spending_not_authorized"]] as const) {
      const alert = within(panel).getByRole("alert", { name: `Budget ${p} blocked` });
      expect(alert.textContent).toContain("[Blocked: not spendable]");
      expect(alert.textContent).toContain(`blocked:${p}:inference/${why}`);
      const table = within(panel).getByRole("table", { name: new RegExp(`Budget ${p}`) });
      for (const row of within(table).getAllByRole("row").slice(1)) {
        const cells = within(row).getAllByRole("cell");
        const available = cells[3]?.textContent ?? "";
        expect(available).toMatch(/^\[Not spendable\] blocked: every new reservation is refused/);
        expect(available).toContain("cannot be used");
      }
    }
    expect(panel.textContent).toContain("read the reason kinds (blocked, saturated, shared)");
    expect(within(panel).getByRole("list", { name: "Budget coverage reasons" }).textContent).toContain("saturated:2026-10:inference/totals_at_bound");
  });

  it("shows no book as no spend, never as zero", async () => {
    await show({ ...ROUTES, "/api/v1/budgets?limit=200": { items: [], next_cursor: null, coverage: { status: "complete", reasons: [] } } });
    const panel = section("Budgets");
    expect((await within(panel).findByText(/No budget book is recorded/)).textContent).toContain("not zero spend");
    expect(within(panel).queryByRole("table")).toBeNull();
  });
});

describe("each panel fails closed on its own", () => {
  const cases: [string, string, RegExp][] = [
    ["/api/v1/releases?limit=200", "Releases and installs", /Installed releases/],
    ["/api/v1/deployment/events?limit=200", "Releases and installs", /Install, rollback and pause/],
    ["/api/v1/session/approval-classes", "Approval classes", /Your approval classes/],
    ["/api/v1/budgets?limit=200", "Budgets", /Budget 2026-10/],
  ];
  it.each(cases)("%s failing leaves the other panels intact", async (path, name, table) => {
    await show(ROUTES, { [path]: [503, "dependency_unavailable"] });
    const panel = section(name);
    expect((await within(panel).findByRole("alert")).textContent).toContain("Unavailable (dependency_unavailable)");
    expect(within(panel).queryByRole("table", { name: table })).toBeNull();
    for (const [, other, t] of cases.filter(([p]) => p !== path)) {
      expect(await within(section(other)).findByRole("table", { name: t })).toBeTruthy();
    }
  });

  it("refuses a payload that does not match the contract, per panel", async () => {
    const stored = { ...RELEASES, items: [rel("REL-SYN-9", { receipt_status: "stored" })] };
    await show({ ...ROUTES, "/api/v1/releases?limit=200": stored });
    const panel = section("Releases and installs");
    expect((await within(panel).findByRole("alert")).textContent).toContain("Unavailable (invalid_response)");
    expect(await within(section("Budgets")).findByRole("table", { name: /Budget 2026-10/ })).toBeTruthy();
  });
});

describe("governance guards fail closed", () => {
  const relPage = (r: object) => ({ items: [r], next_cursor: null, coverage: { status: "partial", reasons: ["x:y"] } });
  const evPage = (e: object) => ({ items: [e], next_cursor: null, coverage: { status: "partial", reasons: ["x:y"] } });
  const budgets = (items: object[]) => ({ items, next_cursor: null, coverage: { status: "partial", reasons: ["x:y"] } });
  const b0 = book("2026-10")[0] ?? {};
  const bad: [string, () => boolean][] = [
    ["a stored receipt status", () => isReleasePage(relPage(rel("R", { receipt_status: "stored" })))],
    ["a null approval time not named", () => isReleasePage(relPage(rel("R", { coverage: { status: "partial",
      reasons: HIDDEN.filter((r) => r !== "unavailable:receipts").concat(NOT_STRATEGY) } })))],
    ["a withheld id not named", () => isReleasePage(relPage(rel("R", { coverage: { status: "partial",
      reasons: HIDDEN.filter((r) => r !== "unavailable:experiment_id").concat(NOT_STRATEGY) } })))],
    ["verified with reasons", () => isReleasePage(relPage(rel("R", { signature: { ...rel("R").signature, reasons: ["x"] } })))],
    ["runnable with reasons", () => isReleasePage(relPage(rel("R", { install: { ...rel("R").install, reasons: ["x"] } })))],
    ["an unknown install state", () => isReleasePage(relPage(rel("R", { install: { ...rel("R").install, state: "promoted" } })))],
    ["no required approval", () => isReleasePage(relPage(rel("R", { required_approvals: [] })))],
    ["a key-bearing signature reference", () => isReleasePage(relPage(rel("R", { signature: { ...rel("R").signature,
      signature_reference: "sig.KEY-1.abc" } })))],
    ["duplicate release ids", () => isReleasePage({ ...RELEASES, items: [rel("R"), rel("R")] })],
    ["a rollback naming release_id", () => isDeploymentEventPage(evPage({ ...EVS[1], release_id: "REL-SYN-1" }))],
    ["a pause without reasons", () => isDeploymentEventPage(evPage({ ...EVS[2], reasons: [] }))],
    ["an install with reasons", () => isDeploymentEventPage(evPage({ ...EVS[0], reasons: ["x"] }))],
    ["an unnamed null operator", () => isDeploymentEventPage(evPage({ ...EVS[0], coverage: { status: "partial",
      reasons: ["none:from_release_id", "none:to_release_id", "none:step_up_receipt_id"] } }))],
    ["two approval classes", () => isApprovalClasses({ ...CLASSES, classes: CLASSES.classes.slice(0, 2) })],
    ["classes out of order", () => isApprovalClasses({ ...CLASSES, classes: [...CLASSES.classes].reverse() })],
    ["not held without a reason", () => isApprovalClasses({ ...CLASSES, classes: CLASSES.classes.map((k) => ({ ...k, reasons: [] })) })],
    ["a satisfied step-up without expiry", () => isApprovalClasses({ ...CLASSES, classes: CLASSES.classes.map((k) =>
      ({ ...k, step_up: { required: true, satisfied: true, expires_at: null } })) })],
    ["advisory false", () => isApprovalClasses({ ...CLASSES, advisory: false })],
    ["an unknown class reason", () => isApprovalClasses({ ...CLASSES, classes: CLASSES.classes.map((k) => ({ ...k, reasons: ["granted"] })) })],
    ["a JSON number amount", () => isBudgetPage(budgets([{ ...b0, cap_usd: 100 }]))],
    ["a negative spend", () => isBudgetPage(budgets([{ ...b0, spent_usd: "-1" }]))],
    ["an exponent amount", () => isBudgetPage(budgets([{ ...b0, available_usd: "1e3" }]))],
    ["a duplicate month and category", () => isBudgetPage(budgets([b0, b0]))],
    ["a budget page without coverage", () => isBudgetPage({ items: [b0], next_cursor: null })],
  ];
  it.each(bad)("refuses %s", (_name, check) => { expect(check()).toBe(false); });
});
