// Strategies and Settings (capabilities) views, spec 13. Read-only: no adopt, enable or order
// control. Status codes are shown exactly as served, each with a plain reading that never
// improves on the code (not_tested is "Not tested", none is "No investment evidence").
import { useEffect, useRef, useState } from "react";
import { reasonFor } from "./api";
import { CoverageReasons, More, Resource, Retrieved, Tag, Unavailable, lookup } from "./labels";
import { isCapabilities, isStrategy, isStrategyPage, type Strategy } from "./reads";
import type { ViewProps } from "./ResearchViews";
import { Head, useLoad, type Env } from "./views";

const OPERATIONAL: Record<string, string> = {
  not_tested: "Not tested", passed: "Operational checks passed (not investment evidence)",
  failed: "Failed operational checks", suspended: "Suspended",
};
const EVIDENCE: Record<string, string> = {
  none: "No investment evidence", limited: "Limited evidence", historical: "Historical evidence only",
  prospective_limited: "Limited prospective evidence", qualified_for_declared_claim: "Qualified for its declared claim only",
};
const UNRECOGNISED = "Unrecognised status: not relied on";
const Status = ({ s, gate }: { s: Strategy; gate?: string }) => (
  <>
    <Tag>operational_status: {s.operational_status}</Tag> {lookup(OPERATIONAL, s.operational_status) ?? UNRECOGNISED};{" "}
    <Tag>evidence_status: {s.evidence_status}</Tag> {lookup(EVIDENCE, s.evidence_status) ?? UNRECOGNISED};{" "}
    <Tag>action_eligible: {String(s.action_eligible)}</Tag> {s.action_eligible ? "Eligible for action" : "Not eligible for action"}
    {!s.action_eligible && gate !== undefined && <>: <Unavailable reason={gate} /></>}
  </>
);
const FAMILY = "none:family/";

export function Strategies({ env }: ViewProps) {
  const list = useLoad(env, "/strategies?limit=200", isStrategyPage);
  const [open, setOpen] = useState<string>();
  return (
    <Resource label="Strategies" state={list}>
      {(page, at) => {
        const missing = page.coverage.reasons.filter((r) => r.startsWith(FAMILY)).map((r) => r.slice(FAMILY.length));
        return (
          <>
            <Retrieved at={at} />
            {missing.length > 0 && (
              <p><Tag>Families not installed</Tag> {missing.join(", ")}. No strategy of these families can run or propose.</p>
            )}
            <CoverageReasons coverage={page.coverage} label="Strategy list coverage" />
            {page.items.length === 0 ? <p>No strategy is installed.</p> : (
              <table>
                <caption>Installed strategies (latest registered version of each)</caption>
                <Head cols={["Strategy", "Family", "Version", "Status", "Detail"]} />
                <tbody>
                  {page.items.map((s) => (
                    <tr key={s.id}>
                      <th scope="row">{s.id}</th><td>{s.family}</td><td>{s.version}</td><td><Status s={s} /></td>
                      <td><button type="button" onClick={() => { setOpen(s.id); }}>Show strategy {s.id}</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <More page={page} />
            {open && <StrategyDetail key={open} env={env} id={open} reasons={page.coverage.reasons} />}
          </>
        );
      }}
    </Resource>
  );
}

/** The detail route has no coverage, so its unavailable fields are named by the list's reasons. */
function StrategyDetail({ env, id, reasons }: { env: Env; id: string; reasons: string[] }) {
  const state = useLoad(env, `/strategies/${encodeURIComponent(id)}`, isStrategy);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, [id]);
  const list = (ids: string[]) => ids.join(", ") || "none declared";
  const why = (field: string) => reasonFor(reasons, id, field) ?? `${field}_unrecorded`;
  return (
    <section aria-labelledby="strategy-title">
      <h2 id="strategy-title" tabIndex={-1} ref={heading}>Strategy {id}</h2>
      <Resource label="Strategy" state={state}>
        {(s, at) => s.id !== id ? <p role="alert"><Unavailable reason="record_id_mismatch" /></p> : (
          <>
            <Retrieved at={at} />
            <p><Status s={s} gate={why("action_gate")} /></p>
            <ul aria-label={`Strategy ${id} facts`}>
              <li>Family: {s.family}; version {s.version}; research protocol {s.research_protocol_id}.</li>
              <li>Implementation hash {s.implementation_hash}; configuration hash {s.configuration_hash}.</li>
              <li>Required feeds: {list(s.required_feed_spec_ids)}.</li>
              <li>Policy requirements: {list(s.policy_requirements)}.</li>
              <li>Approved by: {s.approved_by ?? "Unavailable: release approval is not modelled"}.</li>
              <li>
                Source decisions:{" "}
                {s.source_decisions.join(", ") || <Unavailable reason={why("source_decisions")} />}.
              </li>
            </ul>
            <p>This record has no coverage of its own; the reasons shown come from the strategy list.</p>
          </>
        )}
      </Resource>
    </section>
  );
}

export function Settings({ env }: ViewProps) {
  const caps = useLoad(env, "/capabilities", isCapabilities);
  return (
    <>
      <h2>Capabilities</h2>
      <Resource label="Capabilities" state={caps}>
        {(d, at) => (
          <>
            <Retrieved at={at} />
            <p>Deployment: {d.deployment === "hosted" ? "Hosted" : "Self-hosted"} ({d.deployment}).</p>
            <table>
              <caption>Feature status: a feature is enabled only when every gate passes</caption>
              <Head cols={["Feature", "Status", "Reason codes"]} />
              <tbody>
                {d.features.map((f) => (
                  <tr key={f.id}>
                    <th scope="row">{f.id}</th>
                    <td><Tag>{f.enabled ? "Enabled" : "Unavailable"}</Tag></td>
                    <td>
                      {f.reason_codes.length === 0 ? "None" : (
                        <ul aria-label={`Reasons for ${f.id}`}>{[...new Set(f.reason_codes)].map((r) => <li key={r}><code>{r}</code></li>)}</ul>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </Resource>
    </>
  );
}
