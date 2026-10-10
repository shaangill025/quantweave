// Accessible numerical control for a draft policy limit (ONB14 shape: metric, value, unit,
// denominator, currency). The value is a text input checked as a decimal string by string
// rules only; it is never parsed to a number and has no default. The API has no route to
// propose a policy version yet, so a valid draft is only shown back, never saved.
import { useRef, useState, type SyntheticEvent } from "react";
import { focusTarget } from "./labels";
import { isDecimalString } from "./wire";

const METRICS = [
  "cash_reserve", "issuer_concentration", "sector_concentration", "planned_trade_loss",
  "stress_loss", "loss_pause", "position_concentration", "drawdown",
] as const;
// Display-side bounds mirror qw_domain.decimals Ratio (12, 18) and MoneyAmount (26, 12);
// the server stays authoritative.
const BOUNDS = { ratio: [12, 18], amount: [26, 12] } as const;

export interface LimitInput {
  metric: string; value: string; unit: "ratio" | "amount"; currency: string; denominator: string;
}
export interface FieldProblem { field: keyof LimitInput; message: string }

export function validateLimit(input: LimitInput): FieldProblem[] {
  const out: FieldProblem[] = [];
  if (!(METRICS as readonly string[]).includes(input.metric)) {
    out.push({ field: "metric", message: "Choose a metric." });
  }
  const [intMax, scaleMax] = BOUNDS[input.unit];
  const [whole = "", fraction = ""] = input.value.split(".");
  if (input.value === "") {
    out.push({ field: "value", message: "Enter a limit value; there is no default." });
  } else if (!isDecimalString(input.value)) {
    out.push({ field: "value", message: "Use digits with an optional point, such as 0.05." });
  } else if (input.value.startsWith("-") || !/[1-9]/.test(input.value)) {
    out.push({ field: "value", message: "The limit must be greater than zero." });
  } else if (whole.length > intMax || fraction.length > scaleMax) {
    const message = `At most ${String(intMax)} whole digits and ${String(scaleMax)} decimals.`;
    out.push({ field: "value", message });
  }
  if (input.unit === "amount" && !/^[A-Z]{3}$/.test(input.currency)) {
    out.push({ field: "currency", message: "An amount needs a three-letter currency code." });
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(input.denominator)) {
    out.push({ field: "denominator", message: "Name what the limit is measured against." });
  }
  return out;
}

const EMPTY: LimitInput = { metric: "", value: "", unit: "ratio", currency: "", denominator: "" };

export function LimitDraft() {
  const [input, setInput] = useState<LimitInput>(EMPTY);
  const [problems, setProblems] = useState<FieldProblem[]>([]);
  const [accepted, setAccepted] = useState<LimitInput | null>(null);
  const summary = useRef<HTMLDivElement>(null);
  const set = (field: keyof LimitInput) => (e: { target: { value: string } }) => {
    setInput({ ...input, [field]: e.target.value });
    setAccepted(null);
  };
  const errorOf = (field: keyof LimitInput) => problems.find((p) => p.field === field);
  const describe = (field: keyof LimitInput) => ({
    id: `limit-${field}`,
    "aria-invalid": errorOf(field) ? true : undefined,
    "aria-describedby": errorOf(field) ? `limit-${field}-error` : undefined,
  });
  const error = (field: keyof LimitInput) => {
    const p = errorOf(field);
    return p ? <span id={`limit-${field}-error`}>{p.message}</span> : null;
  };
  const submit = (e: SyntheticEvent) => {
    e.preventDefault();
    const found = validateLimit(input);
    setProblems(found);
    setAccepted(found.length ? null : input);
    if (found.length) queueMicrotask(() => summary.current?.focus());
  };

  return (
    <form aria-labelledby="limit-draft-title" noValidate onSubmit={submit}>
      <h3 id="limit-draft-title">Draft a numerical limit</h3>
      <p>Local check only: proposing a policy version is not connected, so nothing is saved.</p>
      <div ref={summary} tabIndex={-1} role="alert">
        {problems.length > 0 && (
          <>
            <p>{`Fix ${String(problems.length)} problem${problems.length > 1 ? "s" : ""}:`}</p>
            <ul>
              {problems.map((p) => (
                <li key={p.field}>
                  <a href={`#limit-${p.field}`} onClick={focusTarget}>{p.message}</a>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
      <label htmlFor="limit-metric">Metric</label>
      <select {...describe("metric")} value={input.metric} onChange={set("metric")}>
        <option value="">Choose…</option>
        {METRICS.map((m) => (
          <option key={m} value={m}>{m.replaceAll("_", " ")}</option>
        ))}
      </select>
      {error("metric")}
      <label htmlFor="limit-value">Limit value (decimal, no default)</label>
      <input {...describe("value")} type="text" inputMode="decimal" autoComplete="off" value={input.value} onChange={set("value")} />
      {error("value")}
      <fieldset>
        <legend>Unit</legend>
        {(["ratio", "amount"] as const).map((u) => (
          <label key={u}>
            <input type="radio" name="limit-unit" value={u} checked={input.unit === u} onChange={() => { setInput({ ...input, unit: u }); setAccepted(null); setProblems(problems.filter((p) => p.field !== "currency")); }} />
            {u === "ratio" ? "Ratio of the denominator" : "Amount in a currency"}
          </label>
        ))}
      </fieldset>
      {input.unit === "amount" && (
        <>
          <label htmlFor="limit-currency">Currency</label>
          <input {...describe("currency")} type="text" value={input.currency} onChange={set("currency")} />
          {error("currency")}
        </>
      )}
      <label htmlFor="limit-denominator">Denominator</label>
      <input {...describe("denominator")} type="text" value={input.denominator} onChange={set("denominator")} />
      {error("denominator")}
      <button type="submit">Check draft</button>
      <p aria-live="polite">
        {accepted &&
          `Draft is well formed: ${accepted.metric} ${accepted.value} ${
            accepted.unit === "ratio" ? "ratio" : accepted.currency
          } of ${accepted.denominator}. Not saved and not adopted.`}
      </p>
    </form>
  );
}
