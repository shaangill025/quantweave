// Improvement and governance view (spec 13, spec 11; T055 increment 1). Read-only: the API
// serves no promote, approve, install, adopt or rollback operation, so none is rendered, and
// this view only sends GET requests. The server stays the authority for every approval.
// Data: GET /capabilities and GET /experiments (project contract 0.6.0: each item names its reasons),
// plus the read-only governance panels of GovernanceViews (releases, events, approval classes, budgets).
import type { Loaded } from "./api";
import { Approvals, Budgets, Releases } from "./GovernanceViews";
import { CoverageReasons, More, Resource, Retrieved, Tag, Unavailable, humanize, lookup } from "./labels";
import { TARGETS, isCapabilities, isExperimentPage, type Capabilities, type Experiment, type Target } from "./reads";
import type { ViewProps } from "./ResearchViews";
import { Head, useLoad } from "./views";

const TARGET_LABEL: Record<Target, string> = {
  personal_adaptation: "User adaptation", research: "Research and retrieval", strategy: "Strategy and ranking",
  evaluator: "Decision evaluator", application_code: "Application and agent code", recursive_optimizer: "Optimizer recursion",
};
/** The served capability feature specific to improving a target. */
const FEATURE: Partial<Record<Target, string>> = {
  application_code: "code_improvement", recursive_optimizer: "optimizer_recursion",
};
/** A served user-facing feature related to a target. It is not an improvement gate: when it is
 * not open, states past the gate are not relied on; when it is open, they stay unverified. */
const RELATED: Partial<Record<Target, string>> = {
  research: "ai_research", evaluator: "ai_evaluator", strategy: "strategy_proposals",
};
const STATE: Record<string, string> = {
  proposed: "Proposed", authorized: "Authorized, not run", running: "Running", evaluating: "Under independent evaluation",
  awaiting_review: "Awaiting human review (pending)", rejected: "Rejected", approved: "Release approved, not installed",
  promoted: "Promoted", rolled_back: "Rolled back", failed: "Failed", cancelled: "Cancelled",
  blocked: "Blocked: not run or not assessed; not a success",
  insufficient_evidence: "Insufficient evidence: not a pass; may be re-assessed while its holdout read is unused",
};
/** States that need the sandbox, assessment or promotion gate to have opened first. */
const GATED = new Set(["running", "evaluating", "awaiting_review", "approved", "promoted", "rolled_back"]);
/** Stalled states: shown with their reasons, never as a success, and not relied on behind a closed gate. */
const STALLED = new Set(["blocked", "insufficient_evidence"]);
const SCOPE = { tenant: "private (tenant)", installation: "shared (installation)" };
export const NOT_PROVEN = "Not proven";
const NO_RECEIPT_READ = "no_contract_read:evaluation_receipt";

type Gate = { feature: string; specific: boolean } & (
  | { kind: "open" } | { kind: "none" }
  | { kind: "closed"; reasons: string[] } | { kind: "unknown"; reason: string });

function gateFor(target: Target, caps: Loaded<Capabilities> | undefined): Gate {
  const specific = FEATURE[target] !== undefined;
  const feature = FEATURE[target] ?? RELATED[target];
  if (feature === undefined) return { kind: "none", feature: "", specific };
  if (caps === undefined) return { kind: "unknown", reason: "capabilities_loading", feature, specific };
  if (caps.status === "failed") return { kind: "unknown", reason: caps.code, feature, specific };
  const f = caps.data.features.find((x) => x.id === feature);
  if (f === undefined) return { kind: "unknown", reason: "feature_not_served", feature, specific };
  return f.enabled ? { kind: "open", feature, specific }
    : { kind: "closed", reasons: [...new Set(f.reason_codes)], feature, specific };
}

function GateText({ target, gate }: { target: Target; gate: Gate }) {
  if (gate.kind === "none") return <>No served capability gate for this target; states below are unverified.</>;
  const name = <code>{gate.feature}</code>;
  const lead = gate.specific ? null : <>No served gate specific to improvement for this target. Related feature: </>;
  if (gate.kind === "open") {
    return <>{lead}<Tag>Enabled</Tag> {name}: capability gate qualified; recorded states are not independently verified here.</>;
  }
  if (gate.kind === "unknown") return <>{lead}<Tag>Gate unknown</Tag> {name}: <Unavailable reason={gate.reason} /></>;
  return (
    <>
      {lead}<Tag>Blocked</Tag> {name} is unavailable:{" "}
      <ul aria-label={`Blocking reasons for ${target}`}>{gate.reasons.map((r) => <li key={r}><code>{r}</code></li>)}</ul>
    </>
  );
}

/** A gated or stalled state is shown but never relied on while its (or a related) gate is closed or unknown. */
const relied = (e: Experiment, gate: Gate) =>
  !(GATED.has(e.state) || STALLED.has(e.state)) || gate.kind === "open" || gate.kind === "none";
function StateText({ e, gate }: { e: Experiment; gate: Gate }) {
  const label = lookup(STATE, e.state) ?? `Unknown state (${e.state})`;
  const gated = GATED.has(e.state);
  const stalled = STALLED.has(e.state);
  return (
    <>
      <Tag>state: {e.state}</Tag> {label}
      {!relied(e, gate) && <>. <strong>Not relied on:</strong> a capability gate it depends on is not open{stalled ? "" : ", so this state cannot be real here"}.</>}
      {(gated || stalled) && relied(e, gate) && <>. As recorded by the server; unverified.</>}
      <CoverageReasons coverage={e.coverage} label={`Reasons for ${e.id}`} />
    </>
  );
}

export function Improvement({ env }: ViewProps) {
  const caps = useLoad(env, "/capabilities", isCapabilities);
  const list = useLoad(env, "/experiments?limit=200", isExperimentPage);
  const exps = list?.status === "ok" ? list.data.items : null;
  const gates = Object.fromEntries(TARGETS.map((t) => [t, gateFor(t, caps)])) as Record<Target, Gate>;
  const count = (t: Target) => {
    if (list === undefined) return "Loading experiments…";
    if (list.status === "failed") return <Unavailable reason={list.code} />;
    const mine = list.data.items.filter((e) => e.target === t);
    if (mine.length === 0) return "None recorded on this page";
    const by = new Map<string, number>();
    for (const e of mine) by.set(e.state, (by.get(e.state) ?? 0) + 1);
    return [...by].map(([s, k]) => `${String(k)} ${humanize(s)}`).join(", ");
  };
  const recursive = exps?.filter((e) => e.target === "recursive_optimizer") ?? [];
  const opt = gates.recursive_optimizer;
  return (
    <>
      <p>Read-only. Controlled fixtures demonstrate machinery, not investment returns.</p>
      {caps?.status === "ok" && <Retrieved at={caps.retrievedAt} label="Capabilities: " />}
      <table>
        <caption>The six improvement targets and their state</caption>
        <Head cols={["Target", "Capability gate", "Experiments"]} />
        <tbody>
          {TARGETS.map((t) => (
            <tr key={t}>
              <th scope="row">{TARGET_LABEL[t]} (<code>{t}</code>)</th>
              <td><GateText target={t} gate={gates[t]} /></td>
              <td>{count(t)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>Recursive optimizer: V0 to V1</h2>
      <p role="status" aria-label="Recursion status">
        <Tag>{NOT_PROVEN}</Tag> A V0 optimizer proposing V1, independent assessment, human promotion and V1 running the next
        cycle has not been proven here.
      </p>
      <ul aria-label="Why recursion is not proven">
        {opt.kind === "closed" && <li>The real path is blocked: {opt.reasons.map((r) => <code key={r}>{r} </code>)}</li>}
        {opt.kind === "unknown" && <li>The real path cannot be checked: <Unavailable reason={opt.reason} /></li>}
        <li>
          This app cannot verify the lineage receipts: no served operation reads the promotion receipt or the next-cycle
          runner receipt; the release panel&apos;s hashes are the server&apos;s report, not verified here. Recorded fields below
          are not proof.
        </li>
      </ul>

      <h2>Experiments and lineage</h2>
      <Resource label="Experiments" state={list}>
        {(page, at) => (
          <>
            <Retrieved at={at} />
            <CoverageReasons coverage={page.coverage} label="Experiment list coverage" />
            {page.items.length === 0 ? <p>No experiment is recorded.</p> : (
              <table>
                <caption>Experiments (recorded fields; scorecards and verdicts are not served)</caption>
                <Head cols={["Experiment", "Target", "Scope", "State", "Lineage", "Assessment", "Scorecard and verdict", "Release"]} />
                <tbody>
                  {page.items.map((e) => (
                    <tr key={e.id}>
                      <th scope="row">{e.id}</th>
                      <td>{e.target}</td>
                      <td>{SCOPE[e.scope]} {e.scope_id}</td>
                      <td><StateText e={e} gate={gates[e.target]} /></td>
                      <td>
                        {e.baseline_version} to {e.candidate_version}; optimizer {e.parent_optimizer_version ?? "none (not an optimizer experiment)"}; candidate hash{" "}
                        <code>{e.candidate_hash}</code>
                      </td>
                      <td>Protected assessor {e.protected_assessor_version}; protocol {e.protocol_id}; sandbox {e.sandbox_profile_id}</td>
                      <td>
                        Evaluation receipts: {e.evaluation_receipt_ids.join(", ") || "none"}.{" "}
                        <Unavailable reason={NO_RECEIPT_READ} />
                      </td>
                      <td>{e.release_receipt_id ?? "No release receipt"}; human promotion required</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <More page={page} />
          </>
        )}
      </Resource>
      {recursive.length > 0 && (
        <table>
          <caption>Optimizer lineage (recorded, not proof)</caption>
          <Head cols={["Experiment", "Run by optimizer", "Proposes", "State", "Next cycle run", "Later experiments run by this candidate"]} />
          <tbody>
            {recursive.map((e) => {
              const children = recursive.filter((c) => c.parent_optimizer_version === e.candidate_version).map((c) => c.id);
              return (
                <tr key={e.id}>
                  <th scope="row">{e.id}</th>
                  <td>{e.parent_optimizer_version}</td>
                  <td>{e.baseline_version} to {e.candidate_version}</td>
                  <td><StateText e={e} gate={opt} /></td>
                  <td>{e.next_cycle_run_id ?? "None recorded"}</td>
                  <td>{children.join(", ") || "None recorded"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <h2>Approvals and rollback</h2>
      <p>
        This app version has no promote, approve, install, adopt or rollback control: none of these approvals has a served
        API operation yet. Maintainer release approval, operator installation and your strategy adoption are separate
        approvals; a chat reply or planning delegation is none of them. The server decides who may act; this page only
        reflects what it serves.
      </p>
      <Approvals env={env} />
      <Releases env={env} />
      <Budgets env={env} />

      <h2>Limitations</h2>
      <ul aria-label="Improvement limitations">
        <li>Scorecards, verdicts, assessment access records and failed trial details are not served: an experiment names its evaluation receipts by id only.</li>
        <li>Budgets are served per tenant and UTC month; an experiment names only its reservation id, so no per-experiment amount or spend is shown.</li>
        <li>Release signatures are checked by the server at read time; this app does not verify them. Promotion receipts are not stored, and approver, key and operator identities are withheld.</li>
        <li>Release, event and budget lists show their first page only.</li>
        <li>Only code and optimizer improvement have a served improvement gate. Research, evaluator and strategy are checked against a related user-facing feature only, and user adaptation against none, so their recorded states cannot be verified here.</li>
        <li>Only the first page of experiments is read.</li>
      </ul>
    </>
  );
}
