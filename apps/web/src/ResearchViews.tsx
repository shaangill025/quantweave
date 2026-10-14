// Research (evidence drilldown, option scenarios) and Simulation (virtual portfolios,
// differentiated scorecards) views, spec 13. Read-only: no order, exercise or trade control.
// Uncertainty is always text (a [Tag], an interval or a reason), never colour or a model score.
import { useEffect, useId, useState, type ReactNode } from "react";
import type { Loaded } from "./api";
import { Completeness, MoneyText, Resource, Retrieved, Tag, Unavailable, humanize, utc } from "./labels";
import {
  isRecordId, percent, quoteBlockers, termBlockers, type Calculation, type Claim, type Evidence, type Origin,
  type RecordKind, type ResearchSource, type ScenarioGrid, type ScoreRow, type SimulationSummary,
} from "./sources";
import type { Env } from "./views";
import { formatDecimal, type DecimalString } from "./wire";

export interface SimMode { current: SimulationSummary | null; set: (s: SimulationSummary | null) => void }
export interface ViewProps { env: Env; research: ResearchSource; sim: SimMode }

/** Loads once per key; the source object is stable for the app's lifetime. */
function useSourced<T>(key: string, load: () => Promise<Loaded<T>>) {
  const [state, setState] = useState<Loaded<T>>();
  useEffect(() => {
    let live = true;
    setState(undefined);
    void load().then((s) => { if (live) setState(s); });
    return () => { live = false; };
  }, [key]); // keyed by record id: `load` closes over a stable source
  return state;
}
const fmt = (d: DecimalString) => formatDecimal(d);
const Head = ({ cols }: { cols: string[] }) => (
  <thead><tr>{cols.map((col) => <th key={col} scope="col">{col}</th>)}</tr></thead>
);
const Mismatch = () => <p role="alert"><Unavailable reason="record_id_mismatch" /></p>;

function IdForm({ label, onOpen }: { label: string; onOpen: (id: string) => void }) {
  const field = useId();
  const [value, setValue] = useState("");
  const [error, setError] = useState(false);
  const submit = (e: { preventDefault: () => void }) => {
    e.preventDefault();
    const ok = isRecordId(value);
    setError(!ok);
    if (ok) onOpen(value);
  };
  return (
    <form onSubmit={submit}>
      <label htmlFor={field}>{label}</label>{" "}
      <input id={field} value={value} aria-invalid={error} aria-describedby={error ? `${field}-error` : undefined}
        onChange={(e) => { setValue(e.target.value); }} />{" "}
      <button type="submit">Open</button>
      {error && <p id={`${field}-error`} role="alert">{label}: not a valid record id.</p>}
    </form>
  );
}

const KIND: Record<Claim["kind"], string> = {
  reported_fact: "Reported fact", calculation: "Calculation", assumption: "Assumption: not a fact",
  forecast: "Forecast: uncertain, not a reported fact", interpretation: "Interpretation: a judgement, not a fact",
};
const VERIFY: Record<Claim["verification"], string> = {
  pending: "Pending verification: not reviewed", supported: "Supported by cited evidence",
  calculation_checked: "Calculation checked", assumption_dependent: "Depends on stated assumptions",
  contested: "Contested: contrary evidence exists", unsupported: "Unsupported: do not rely on it",
  unavailable: "Verification unavailable",
};
const LIMITATION: Record<Evidence["availability"], string | null> = {
  available: null, restricted: "Restricted: the passage is withheld under the source's rights",
  redacted: "Redacted: part of the source is withheld",
  deleted: "Deleted: the source snapshot is no longer held; the claim cannot be re-checked against it",
  unavailable: "Unavailable: the source cannot be read now",
};

export function Research({ research }: ViewProps) {
  const [claim, setClaim] = useState<string>();
  const [option, setOption] = useState<string>();
  return (
    <>
      <section aria-labelledby="drill-title">
        <h2 id="drill-title">Evidence drilldown</h2>
        <IdForm label="Claim id" onOpen={setClaim} />
        {claim && <ClaimPanel key={claim} id={claim} research={research} />}
      </section>
      <section aria-labelledby="options-title">
        <h2 id="options-title">Option contract scenarios</h2>
        <IdForm label="Option instrument id" onOpen={setOption} />
        {option && <OptionPanel key={option} id={option} research={research} />}
      </section>
    </>
  );
}

function ClaimPanel({ id, research }: { id: string; research: ResearchSource }) {
  const state = useSourced(id, () => research.claim(id));
  const list = (name: string, all: string[], none: string, item: (i: string) => ReactNode) => {
    const ids = [...new Set(all)]; // unique ids keep list keys stable
    return <>
      <h4>{name} ({ids.length})</h4>
      {ids.length === 0 ? <p>{none}</p> : <ul aria-label={name}>{ids.map((i) => <li key={i}>{item(i)}</li>)}</ul>}
    </>;
  };
  return (
    <section aria-label={`Claim ${id}`}>
      <h3>Claim {id}</h3>
      <Resource label="Claim" state={state}>
        {(c, at) => c.id !== id ? <Mismatch /> : (
          <>
            <Retrieved at={at} />
            <p>
              <Tag>{KIND[c.kind]}</Tag> <Tag>{VERIFY[c.verification]}</Tag>{" "}
              <Tag>{c.material ? "Material to the decision" : "Not material"}</Tag>
            </p>
            <p>Evidence support says how well sources back this claim; it is not a forecast of investment outcome.</p>
            <blockquote>{c.text}</blockquote>
            <p>Valid at {utc(c.valid_at)}. Instruments: {c.instrument_ids.join(", ") || "none"}.</p>
            {list("Supporting sources", c.evidence_ids, "No supporting source is cited.",
              (e) => <EvidenceItem id={e} research={research} />)}
            {list("Contrary evidence", c.counterevidence_ids,
              "None recorded. An absence of contrary evidence is not confirmation.",
              (e) => <EvidenceItem id={e} research={research} />)}
            {list("Calculations", c.calculation_ids, "No calculation is cited.",
              (k) => <CalculationItem id={k} research={research} />)}
          </>
        )}
      </Resource>
    </section>
  );
}

function EvidenceItem({ id, research }: { id: string; research: ResearchSource }) {
  const state = useSourced(id, () => research.evidence(id));
  return (
    <Resource label={`Evidence ${id}`} state={state}>
      {(e) => e.id !== id ? <Mismatch /> : (
        <>
          <p>Source {e.source_id}, {e.locator} (rights profile {e.rights_profile_id}).</p>
          {LIMITATION[e.availability] && <p><Tag>{LIMITATION[e.availability]}</Tag></p>}
          {e.passage === null ? <p>Passage: <Unavailable reason={e.availability === "available" ? "no_passage" : e.availability} /></p>
            : <blockquote>{e.passage}</blockquote>}
          <p>
            {e.published_at === null ? <>Published: <Unavailable reason="publication_time_unknown" />; retrieved {utc(e.retrieved_at)}</>
              : `Published ${utc(e.published_at)}; retrieved ${utc(e.retrieved_at)}`}
          </p>
        </>
      )}
    </Resource>
  );
}

const CALC_STATUS: Record<Calculation["status"], string> = {
  ok: "Computed", insufficient_inputs: "Insufficient inputs", ambiguous: "Ambiguous: no single answer",
  unsupported: "Unsupported case", undefined: "Undefined for these inputs",
};
function CalculationItem({ id, research }: { id: string; research: ResearchSource }) {
  const state = useSourced(id, () => research.calculation(id));
  return (
    <Resource label={`Calculation ${id}`} state={state}>
      {(k) => k.id !== id ? <Mismatch /> : (
        <>
          <p>
            {k.formula_id} v{k.formula_version}: <Tag>{CALC_STATUS[k.status]}</Tag> Result:{" "}
            {k.result === null ? <Unavailable reason={k.status} /> : `${fmt(k.result)} ${k.unit}`} (computed {utc(k.computed_at)})
          </p>
          <table>
            <caption>Inputs to {k.formula_id}</caption>
            <Head cols={["Input", "Value", "Source record"]} />
            <tbody>
              {k.inputs.map((i) => (
                <tr key={i.name}><th scope="row">{i.name}</th><td>{fmt(i.value)} {i.unit}</td><td>{i.source_record_id}</td></tr>
              ))}
            </tbody>
          </table>
          {k.warnings.length > 0 && <ul aria-label="Warnings">{[...new Set(k.warnings)].map((w) => <li key={w}>{w}</li>)}</ul>}
        </>
      )}
    </Resource>
  );
}

function OptionPanel({ id, research }: { id: string; research: ResearchSource }) {
  const state = useSourced(id, () => research.optionTerms(id));
  return (
    <Resource label="Option terms" state={state}>
      {(t, at) => {
        if (t.instrument_id !== id) return <Mismatch />;
        const blockers = termBlockers(t);
        const rows: [string, ReactNode][] = [
          ["Underlying", t.underlying_id], ["Right", t.right], ["Strike", <MoneyText money={t.strike} />],
          ["Expiry", t.expiry], ["Exercise style", t.exercise_style], ["Settlement", t.settlement],
          ["Multiplier", t.multiplier === null ? <Unavailable reason="multiplier_unknown" /> : fmt(t.multiplier)],
          ["Deliverables", t.deliverables.length === 0 ? <Unavailable reason="deliverables_unknown" />
            : t.deliverables.map((d) => `${fmt(d.quantity)} ${d.instrument_id}`).join(", ")],
          ["Adjusted", t.adjusted ? "Yes (nonstandard)" : "No"], ["Terms verified", t.terms_verified ? "Yes" : "No"],
        ];
        return (
          <>
            <Retrieved at={at} />
            <table>
              <caption>Contract terms for {id} (source record {t.source_record_id})</caption>
              <tbody>{rows.map(([k, v]) => <tr key={k}><th scope="row">{k}</th><td>{v}</td></tr>)}</tbody>
            </table>
            {blockers.length > 0 ? <Blocked codes={blockers} />
              : <ScenarioPanel id={id} research={research} />}
          </>
        );
      }}
    </Resource>
  );
}
const Blocked = ({ codes }: { codes: string[] }) => (
  <p role="status">
    <Tag>Scenarios blocked</Tag> {codes.map(humanize).join(", ")}. Missing terms are never assumed, so payoffs,
    Greeks and sizing are not shown.
  </p>
);

function ScenarioPanel({ id, research }: { id: string; research: ResearchSource }) {
  const state = useSourced(id, () => research.scenarios(id));
  return (
    <Resource label="Option scenarios" state={state}>
      {(g) => {
        if (g.instrument_id !== id) return <Mismatch />;
        const blockers = quoteBlockers(g.quote);
        if (blockers.length > 0) return <Blocked codes={blockers} />;
        return <Grid g={g} />;
      }}
    </Resource>
  );
}
function Grid({ g }: { g: ScenarioGrid }) {
  const shock = (p: { spot_shock: DecimalString; vol_shift: DecimalString; elapsed_days: number }) =>
    `Spot ${percent(p.spot_shock)}, volatility ${percent(p.vol_shift)}, ${String(p.elapsed_days)} days`;
  const rows = [g.base, ...g.points];
  return (
    <>
      <p>
        Quote {g.quote.source_id} observed {utc(g.quote.observed_at)} <Tag>Current under freshness policy</Tag>.
        Values in {g.currency}.{" "}
        {g.model_applicable ? `Model ${humanize(g.model)} applies.`
          : <Tag>Model {humanize(g.model)} is not applicable to this exercise style; estimates are not authoritative</Tag>}
      </p>
      <table>
        <caption>Scenario grid for {g.instrument_id}: expiry rows are exact intrinsic values; other rows are model estimates</caption>
        <Head cols={["Scenario", "Basis", "Underlying", "Premium value", "Delta-equivalent units", "Package P&L"]} />
        <tbody>
          {rows.map((p, i) => (
            <tr key={i === 0 ? "base" : `${p.spot_shock}|${p.vol_shift}|${String(p.elapsed_days)}|${"unavailable" in p ? "gap" : String(p.at_expiry)}`}>
              <th scope="row">{i === 0 ? "Base" : shock(p)}</th>
              {"unavailable" in p ? (
                <td colSpan={5}><Unavailable reason={p.unavailable.code} /> {p.unavailable.reason}</td>
              ) : (
                <>
                  <td>{p.at_expiry ? "Expiry payoff (exact, no model)" : "Current-value estimate (model)"}</td>
                  <td>{fmt(p.spot)}</td><td>{fmt(p.premium_value)}</td><td>{fmt(p.delta_units)}</td><td>{fmt(p.pnl)}</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {!g.complete && <p><Tag>Incomplete grid</Tag> Some scenarios are unavailable, so no summary figure is shown.</p>}
      <p>
        Worst loss:{" "}
        {!g.complete || g.worst_loss === null ? <Unavailable reason="incomplete_grid" /> : `${fmt(g.worst_loss)} ${g.currency}`}
      </p>
      <p>
        Collateral required (separate from premium P&L):{" "}
        {!g.complete ? <Unavailable reason="incomplete_grid" />
          : g.collateral === null ? <Unavailable reason="collateral_not_checked" /> : <MoneyText money={g.collateral} />}
      </p>
      {g.greeks === null ? <p>Greeks: <Unavailable reason="not_computed" /></p> : (
        <table>
          <caption>
            Greeks ({humanize(g.greeks.model)}
            {g.greeks.applicable_to_style ? ")" : "; not applicable to this exercise style)"}
          </caption>
          <Head cols={["Greek", "Value and unit"]} />
          <tbody>
            {g.greeks.values.map((v) => <tr key={v.name}><th scope="row">{v.name}</th><td>{fmt(v.value)} {v.unit}</td></tr>)}
          </tbody>
        </table>
      )}
    </>
  );
}

const MODE = { manual: "Manual virtual decisions", approved_policy_shadow: "Shadow tracking of an approved policy" };
export function Simulation({ research, sim }: ViewProps) {
  const list = useSourced("simulations", () => research.simulations());
  return (
    <>
      <p>
        <Tag>SIMULATED</Tag> Virtual portfolios are hypothetical and separate from real accounts. Fills are modelled
        under the named execution model; nothing here reaches a broker.
      </p>
      <Resource label="Virtual portfolios" state={list}>
        {(page, at) => (
          <>
            <Retrieved at={at} />
            <Completeness page={page} />
            {page.items.length === 0 ? <p>No virtual portfolio exists.</p> : (
              <table>
                <caption>Virtual portfolios (SIMULATED, separate from real accounts)</caption>
                <Head cols={["Portfolio", "Mode", "Execution model (fill assumptions)", "Benchmark policy", "Simulation mode"]} />
                <tbody>
                  {page.items.map((s) => (
                    <tr key={s.id}>
                      <th scope="row">{s.name}</th><td>{MODE[s.mode]}</td><td>{s.execution_model_id}</td>
                      <td>{s.benchmark_policy_id}</td>
                      <td>
                        {sim.current?.id === s.id ? "Selected"
                          : <button type="button" onClick={() => { sim.set(s); }}>Work with {s.name} in simulation mode</button>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </Resource>
      {sim.current && <ScorecardPanel key={sim.current.id} id={sim.current.id} research={research} />}
    </>
  );
}

const GROUP: Record<Origin, string> = {
  user: "User decisions", ai: "AI recommendations", benchmark: "Benchmarks",
};
const RECORD: Record<RecordKind, string> = {
  actual: "Real account", virtual: "SIMULATED", recommendation: "Recommendation record", baseline: "Benchmark",
};
function ScorecardPanel({ id, research }: { id: string; research: ResearchSource }) {
  const state = useSourced(id, () => research.scorecard(id));
  return (
    <section aria-labelledby="score-title">
      <h2 id="score-title">Scorecards</h2>
      <Resource label="Scorecards" state={state}>
        {(s, at) => s.simulation_id !== id ? <Mismatch /> : (
          <>
            <Retrieved at={at} />
            <p>
              Plan {s.plan_id} v{s.plan_version}, frozen {utc(s.plan_frozen_at)}, covers {s.start} to {s.end} in{" "}
              {s.currency}: {s.interpretation}. Each row stands alone; rows are never added together, and simulated
              results are not actual returns.
            </p>
            {(["user", "ai", "benchmark"] as const).map((o) => (
              <table key={o}>
                <caption>{GROUP[o]} ({s.currency})</caption>
                <Head cols={["Record", "Reference", "Sample", "Net gain", "Money-weighted return", "Uncertainty"]} />
                <tbody>
                  {s.rows.filter((r) => r.origin === o).map((r) => <Row key={`${r.record}:${r.ref}`} r={r} />)}
                </tbody>
              </table>
            ))}
          </>
        )}
      </Resource>
    </section>
  );
}
function Row({ r }: { r: ScoreRow }) {
  const n = r.sample_size;
  const short = n === null || r.min_sample === null || n < r.min_sample;
  const why = r.unavailable ? <><Unavailable reason={r.unavailable.code} /> {r.unavailable.reason}</> : null;
  return (
    <tr>
      <th scope="row"><Tag>{RECORD[r.record]}</Tag> {r.label}</th>
      <td>{r.ref}</td>
      <td>{n === null ? <Unavailable reason="sample_size_unknown" /> : `n = ${String(n)}`}</td>
      {short ? (
        <td colSpan={3}>
          <Tag>Insufficient evidence</Tag>{" "}
          {n === null ? "Sample size unknown" : r.min_sample === null ? "No minimum sample was predeclared"
            : `n = ${String(n)}, below the minimum of ${String(r.min_sample)}`}; no result shown.
        </td>
      ) : (
        <>
          <td>{r.net_gain ? <MoneyText money={r.net_gain} /> : why}</td>
          <td>{r.mwr === null ? why : percent(r.mwr)}</td>
          <td>
            {r.mwr === null ? "Not applicable" : r.interval
              ? `${percent(r.interval.level)} interval ${percent(r.interval.low)} to ${percent(r.interval.high)}`
              : "No interval: uncertainty not estimated; a point description only"}
          </td>
        </>
      )}
    </tr>
  );
}
