// Read-only sources for the Research and Simulation views (T054).
// Contract routes used (openapi.yaml 1.0.0): GET /claims/{id}, /evidence/{id},
// /calculations/{id}, /instruments/{id}/option-terms and /simulations. apps/api serves none of
// them yet, so against the real API they fail as `not_found` and the views say so.
// The option scenario grid and the differentiated scorecard have NO contract route or schema.
// Their shapes below follow qw_domain.option_scenarios / option_greeks and qw_domain.scorecard,
// and the HTTP source answers `no_contract_route` for them without sending a request.
import { checks as c, getJson, type Fetcher, type Guard, type Loaded, type Money, type Page } from "./api";
import { parseDecimalString, type DecimalString as Dec } from "./wire";

type Instant = string;
export type Verification =
  | "pending" | "supported" | "calculation_checked" | "assumption_dependent" | "contested" | "unsupported" | "unavailable";
export interface Claim {
  id: string; kind: "reported_fact" | "calculation" | "assumption" | "forecast" | "interpretation"; text: string;
  instrument_ids: string[]; evidence_ids: string[]; calculation_ids: string[]; material: boolean;
  verification: Verification; counterevidence_ids: string[]; valid_at: Instant;
}
export type Availability = "available" | "restricted" | "redacted" | "deleted" | "unavailable";
export interface Evidence {
  id: string; source_id: string; locator: string; passage: string | null; published_at: Instant | null;
  retrieved_at: Instant; rights_profile_id: string; availability: Availability; instrument_ids: string[];
}
export interface Calculation {
  id: string; formula_id: string; formula_version: string; result: Dec | null; unit: string;
  status: "ok" | "insufficient_inputs" | "ambiguous" | "unsupported" | "undefined"; warnings: string[];
  inputs: { name: string; value: Dec; unit: string; source_record_id: string }[]; computed_at: Instant;
}
export interface OptionTerms {
  id: string; instrument_id: string; underlying_id: string; expiry: string; strike: Money; right: "call" | "put";
  exercise_style: "american" | "european" | "unknown"; settlement: "physical" | "cash" | "unknown";
  multiplier: Dec | null; deliverables: { instrument_id: string; quantity: Dec }[]; adjusted: boolean;
  terms_verified: boolean; source_record_id: string;
}
export interface SimulationSummary {
  id: string; name: string; execution_model_id: string; benchmark_policy_id: string;
  mode: "manual" | "approved_policy_shadow"; separate_from_actual: true;
}
export interface Gap { code: string; reason: string }
interface Shock { spot_shock: Dec; vol_shift: Dec; elapsed_days: number }
export interface Point extends Shock {
  spot: Dec; at_expiry: boolean; premium_value: Dec; delta_units: Dec; pnl: Dec;
}
export interface ScenarioGrid {
  instrument_id: string; currency: string; model: string; model_applicable: boolean;
  quote: { source_id: string; observed_at: Instant; freshness_status: "current_under_policy" | "stale" | "unknown";
    rights: "permitted" | "restricted" | "unknown" };
  base: Point; points: (Point | (Shock & { unavailable: Gap }))[];
  greeks: { model: string; applicable_to_style: boolean; values: { name: string; value: Dec; unit: string }[] } | null;
  collateral: Money | null; worst_loss: Dec | null; complete: boolean;
}
export type Origin = "user" | "ai" | "benchmark";
export type RecordKind = "actual" | "virtual" | "recommendation" | "baseline";
export interface ScoreRow {
  origin: Origin; record: RecordKind; ref: string; label: string;
  sample_size: number | null; min_sample: number | null; net_gain: Money | null; mwr: Dec | null;
  interval: { low: Dec; high: Dec; level: Dec } | null; unavailable: Gap | null;
}
export interface Scorecard {
  simulation_id: string; plan_id: string; plan_version: number; plan_frozen_at: Instant; currency: string;
  start: string; end: string; interpretation: string; rows: ScoreRow[];
}

const { str, id, int, dec, instant, oneOf, nullable: n, arr, obj, money, coverage } = c;
const bool = (v: unknown) => typeof v === "boolean";
const date = (v: unknown) => typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v);
const gap = obj({ code: str, reason: str });
export const isClaim = obj({
  id, kind: oneOf("reported_fact", "calculation", "assumption", "forecast", "interpretation"), text: str,
  instrument_ids: arr(id), evidence_ids: arr(id), calculation_ids: arr(id), material: bool,
  verification: oneOf("pending", "supported", "calculation_checked", "assumption_dependent", "contested", "unsupported", "unavailable"),
  counterevidence_ids: arr(id), valid_at: instant,
}) as Guard<Claim>;
export const isEvidence = obj({
  id, source_id: id, locator: str, passage: n(str), published_at: n(instant), retrieved_at: instant,
  rights_profile_id: id, availability: oneOf("available", "restricted", "redacted", "deleted", "unavailable"),
  instrument_ids: arr(id),
}) as Guard<Evidence>;
export const isCalculation = obj({
  id, formula_id: id, formula_version: str, result: n(dec), unit: str, warnings: arr(str), computed_at: instant,
  status: oneOf("ok", "insufficient_inputs", "ambiguous", "unsupported", "undefined"),
  inputs: arr(obj({ name: str, value: dec, unit: str, source_record_id: id })),
}) as Guard<Calculation>;
export const isOptionTerms = obj({
  id, instrument_id: id, underlying_id: id, expiry: date, strike: money, right: oneOf("call", "put"),
  exercise_style: oneOf("american", "european", "unknown"), settlement: oneOf("physical", "cash", "unknown"),
  multiplier: n(dec), deliverables: arr(obj({ instrument_id: id, quantity: dec })), adjusted: bool,
  terms_verified: bool, source_record_id: id,
}) as Guard<OptionTerms>;
const isSimulation = obj({
  id, name: str, execution_model_id: id, benchmark_policy_id: id, mode: oneOf("manual", "approved_policy_shadow"),
  separate_from_actual: (v) => v === true,
});
export const isSimulationPage = ((v: unknown) =>
  obj({ items: arr(isSimulation), next_cursor: n(str) })(v) && coverage((v as { coverage?: unknown }).coverage)
) as Guard<Page<SimulationSummary>>;
const shock = { spot_shock: dec, vol_shift: dec, elapsed_days: int };
const point = obj({ ...shock, spot: dec, at_expiry: bool, premium_value: dec, delta_units: dec, pnl: dec });
export const isScenarioGrid = obj({
  instrument_id: id, currency: str, model: str, model_applicable: bool,
  quote: obj({ source_id: id, observed_at: instant, freshness_status: oneOf("current_under_policy", "stale", "unknown"),
    rights: oneOf("permitted", "restricted", "unknown") }),
  base: point, points: arr((v) => point(v) || obj({ ...shock, unavailable: gap })(v)),
  greeks: n(obj({ model: str, applicable_to_style: bool, values: arr(obj({ name: str, value: dec, unit: str })) })),
  collateral: n(money), worst_loss: n(dec), complete: bool,
}) as Guard<ScenarioGrid>;
/** Which record kinds each origin may carry: real and virtual results never share a row. */
const ALLOWED: Record<Origin, RecordKind[]> = {
  user: ["actual", "virtual"], ai: ["virtual", "recommendation"], benchmark: ["baseline"],
};
const scoreRow = obj({
  origin: oneOf("user", "ai", "benchmark"), record: oneOf("actual", "virtual", "recommendation", "baseline"),
  ref: id, label: str, net_gain: n(money), mwr: n(dec),
  sample_size: n((v) => int(v) && (v as number) >= 0), min_sample: n((v) => int(v) && (v as number) >= 1),
  interval: n(obj({ low: dec, high: dec, level: dec })), unavailable: n(gap),
});
export const isScorecard = ((v: unknown) => {
  if (!obj({ simulation_id: id, plan_id: id, plan_version: int, plan_frozen_at: instant, currency: str,
    start: date, end: date, interpretation: str, rows: arr(scoreRow) })(v)) return false;
  const s = v as Scorecard;
  return s.rows.every((r) => ALLOWED[r.origin].includes(r.record) &&
    (r.net_gain === null || r.net_gain.currency === s.currency) &&
    (r.interval === null || decLe(r.interval.low, r.interval.high)) &&
    // A missing result must carry its reason; a reason never accompanies a result.
    (r.mwr === null) === (r.unavailable !== null) && (r.net_gain !== null || r.unavailable !== null));
}) as Guard<Scorecard>;

export interface ResearchSource {
  claim(id: string): Promise<Loaded<Claim>>;
  evidence(id: string): Promise<Loaded<Evidence>>;
  calculation(id: string): Promise<Loaded<Calculation>>;
  optionTerms(instrumentId: string): Promise<Loaded<OptionTerms>>;
  scenarios(instrumentId: string): Promise<Loaded<ScenarioGrid>>;
  simulations(): Promise<Loaded<Page<SimulationSummary>>>;
  scorecard(simulationId: string): Promise<Loaded<Scorecard>>;
}
const fail = <T>(code: string, detail: string): Promise<Loaded<T>> =>
  Promise.resolve({ status: "failed", code, detail, correlationId: null });
export const isRecordId = (v: string) => id(v);

/** Checks an already-received body against a guard, as getJson does for HTTP bodies. */
export const checked = <T>(body: unknown, guard: Guard<T>, now: () => Date): Loaded<T> =>
  guard(body) ? { status: "ok", data: body, retrievedAt: now() }
    : { status: "failed", code: "invalid_response", detail: "The response did not match the contract.", correlationId: null };

export function httpSource(fetcher: Fetcher, now: () => Date): ResearchSource {
  const one = <T>(prefix: string, suffix: string, guard: Guard<T>) => (key: string) =>
    isRecordId(key) ? getJson(fetcher, `${prefix}/${encodeURIComponent(key)}${suffix}`, guard, now)
      : fail<T>("invalid_id", "The id is not a contract record id.");
  const unserved = <T>(what: string) => () =>
    fail<T>("no_contract_route", `The API contract has no route for ${what} yet.`);
  return {
    claim: one("/claims", "", isClaim),
    evidence: one("/evidence", "", isEvidence),
    calculation: one("/calculations", "", isCalculation),
    optionTerms: one("/instruments", "/option-terms", isOptionTerms),
    simulations: () => getJson(fetcher, "/simulations?limit=200", isSimulationPage, now),
    scenarios: unserved("option scenario grids"),
    scorecard: unserved("differentiated scorecards"),
  };
}

/** Codes for every missing or unverified term; any code blocks payoffs, Greeks and sizing. */
export function termBlockers(t: OptionTerms): string[] {
  return [
    !t.terms_verified && "terms_unverified",
    t.multiplier === null && "multiplier_unknown",
    t.exercise_style === "unknown" && "exercise_style_unknown",
    t.settlement === "unknown" && "settlement_unknown",
    t.deliverables.length === 0 && "deliverables_unknown",
    t.adjusted && "adjusted_contract_unsupported",
  ].filter((x): x is string => x !== false);
}
export function quoteBlockers(q: ScenarioGrid["quote"]): string[] {
  return [
    q.freshness_status !== "current_under_policy" && `quote_${q.freshness_status}`,
    q.rights !== "permitted" && `quote_rights_${q.rights}`,
  ].filter((x): x is string => x !== false);
}

/** A ratio shown as a percentage by moving the decimal point two places (strings only). */
export function percent(ratio: Dec): string {
  parseDecimalString(ratio); // throws on anything that is not a wire decimal
  const negative = ratio.startsWith("-");
  const [whole = "0", frac = ""] = (negative ? ratio.slice(1) : ratio).split(".");
  const padded = frac.padEnd(2, "0");
  const intPart = (whole + padded.slice(0, 2)).replace(/^0+(?=\d)/, "");
  const fracPart = padded.slice(2).replace(/0+$/, "");
  const body = fracPart ? `${intPart}.${fracPart}` : intPart;
  return `${negative && /[1-9]/.test(body) ? "-" : ""}${body}%`;
}

/** Exact a <= b for decimal strings: both are scaled to integers with BigInt. */
export function decLe(a: Dec, b: Dec): boolean {
  const scale = Math.max(...[a, b].map((d) => (d.split(".")[1] ?? "").length));
  const big = (d: Dec) => {
    const [whole = "0", frac = ""] = d.replace("-", "").split(".");
    const v = BigInt(whole + frac.padEnd(scale, "0"));
    return d.startsWith("-") ? -v : v;
  };
  return big(a) <= big(b);
}
