// Governance reads in the improvement view (spec 11, 13, 15; T055 governance panels). Read-only panels
// for GET /releases and /deployment/events (project contract 0.12.0, 0.13.0), GET
// /session/approval-classes (0.7.0) and GET /budgets (T037 increment 6). Each panel loads and
// fails on its own. Nothing here signs, verifies, installs, adopts, rolls back or spends: the
// server's reported state is shown as reported, and money stays a decimal string.
import { reasonFor, type Coverage } from "./api";
import { CoverageReasons, More, MoneyText, Resource, Retrieved, Tag, Unavailable, humanize, lookup, utc } from "./labels";
import {
  isApprovalClasses, isBudgetPage, isDeploymentEventPage, isReleasePage,
  type ApprovalClasses, type BudgetSummary, type DeploymentEvent, type Release,
} from "./reads";
import { Head, useLoad, type Env } from "./views";

const INSTALL: Record<Release["install"]["state"], string> = {
  head: "Head: the component's current release", installed: "Installed, not the component's head",
  withdrawn: "Withdrawn: rolled back from; not running", paused: "Paused: suspended",
};
const ADOPTION = { not_adopted: "Not adopted by your tenant", active: "Active for your tenant", paused: "Paused for your tenant" };

/** A field as served, or its null with the coverage reason that names it. */
function Field({ value, cv, id, field }: { value: string | null; cv: Coverage; id: string; field: string }) {
  if (value !== null) return <code>{value}</code>;
  return <Unavailable reason={reasonFor(cv.reasons, id, field) ?? `unnamed_null:${field}`} />;
}
const Codes = ({ codes, label }: { codes: string[]; label: string }) =>
  codes.length === 0 ? null : <ul aria-label={label}>{codes.map((r, i) => <li key={`${String(i)}:${r}`}><code>{r}</code></li>)}</ul>;

function ReleaseRow({ r }: { r: Release }) {
  const cv = r.coverage;
  const s = r.signature;
  return (
    <tr>
      <th scope="row">{r.id}</th>
      <td><code>{r.component}</code>; target {r.target}; change {r.change_kind}</td>
      <td>
        <Tag>{r.install.state === "withdrawn" ? "Withdrawn" : `install: ${r.install.state}`}</Tag> {INSTALL[r.install.state]}.
        Installed {utc(r.install.installed_at)}.{" "}
        {r.install.runnable ? "Runnable at read time (server)." : <><Tag>Not runnable</Tag> at read time (server):</>}
        <Codes codes={r.install.reasons} label={`Install reasons for ${r.id}`} />
        {r.install.self_approved ? <> <Tag>Self-approved</Tag> as recorded by the server.</> : " Not self-approved (as recorded by the server)."}
      </td>
      <td>
        <Tag>{s.verification === "verified" ? "Server check at read time: verified" : "Server check at read time: refused"}</Tag>{" "}
        Reported by the server when this page was read, not checked by this app. Algorithm <code>{s.algorithm}</code>;
        reference <code>{s.signature_reference}</code>; key <Field value={s.key_id} cv={cv} id={r.id} field="signature/key_id" />.
        <Codes codes={s.reasons} label={`Signature reasons for ${r.id}`} />
      </td>
      <td>
        Required: {r.required_approvals.join(", ")}. Approved at:{" "}
        {r.approved_at === null ? <Field value={null} cv={cv} id={r.id} field="receipts" /> : `${utc(r.approved_at)} (as recorded)`}.
        Promotion receipt: <Tag>receipt_status: {r.receipt_status}</Tag> not stored, so no approval is shown as proven.
        Verdict source {r.verdict_source}; experiment <Field value={r.experiment_id} cv={cv} id={r.id} field="experiment_id" />;
        verdict <Field value={r.verdict_id} cv={cv} id={r.id} field="verdict_id" />
      </td>
      <td>
        {r.user_adoption_required ? "Your adoption is required" : "No user adoption required"}.{" "}
        {r.tenant_adoption === null ? <Field value={null} cv={cv} id={r.id} field="tenant_adoption" /> : (
          <>
            {ADOPTION[r.tenant_adoption.state]}; version {r.tenant_adoption.version ?? "none"}
            <Codes codes={r.tenant_adoption.reasons} label={`Adoption reasons for ${r.id}`} />
          </>
        )}
      </td>
      <td>
        Rollback target <code>{r.rollback_version_id}</code>; schemas {r.compatibility.min_schema} to {r.compatibility.max_schema},
        writes {r.compatibility.writes_schema}. Candidate <code>{r.candidate_hash}</code>; artifact <code>{r.artifact_hash}</code>;
        protocol <code>{r.protocol_hash}</code>; manifest <code>{r.manifest_hash}</code>
      </td>
      <td><CoverageReasons coverage={cv} label={`Reasons for ${r.id}`} /></td>
    </tr>
  );
}

function EventRow({ e }: { e: DeploymentEvent }) {
  const f = (v: string | null, field: string) => <Field value={v} cv={e.coverage} id={e.id} field={field} />;
  return (
    <tr>
      <th scope="row">{e.id}</th>
      <td>{e.kind}</td>
      <td><code>{e.component}</code></td>
      <td>{utc(e.at)}</td>
      <td>
        {e.kind === "rollback"
          ? <>Withdrew {f(e.from_release_id, "from_release_id")}; restored {f(e.to_release_id, "to_release_id")}</>
          : <>{e.kind === "pause" ? "Paused" : "Installed"} {f(e.release_id, "release_id")}</>}
        <Codes codes={e.reasons} label={`Pause reasons for ${e.id}`} />
      </td>
      <td>{e.schema_after}</td>
      <td>Operator {f(e.operator_id, "operator_id")}; step-up receipt {f(e.step_up_receipt_id, "step_up_receipt_id")}</td>
    </tr>
  );
}

const NO_HISTORY = "No release history for this session: the server reports no recorded deployment this tenant belongs to.";

export function Releases({ env }: { env: Env }) {
  const releases = useLoad(env, "/releases?limit=200", isReleasePage);
  const events = useLoad(env, "/deployment/events?limit=200", isDeploymentEventPage);
  return (
    <section aria-label="Releases and installs">
      <h2>Releases and installs</h2>
      <Resource label="Releases" state={releases} notFound={NO_HISTORY}>
        {(page, at) => (
          <>
            <Retrieved at={at} label="Releases: " />
            <CoverageReasons coverage={page.coverage} label="Release list coverage" />
            {page.items.length === 0 ? <p>No release is installed in this deployment.</p> : (
              <table>
                <caption>Installed releases (server-reported at read time; not verified by this app)</caption>
                <Head cols={["Release", "Component", "Install", "Signature", "Approvals and receipt", "Adoption", "Hashes and rollback target", "Reasons"]} />
                <tbody>{page.items.map((r) => <ReleaseRow key={r.id} r={r} />)}</tbody>
              </table>
            )}
            <More page={page} />
          </>
        )}
      </Resource>
      <Resource label="Deployment events" state={events} notFound={NO_HISTORY}>
        {(page, at) => (
          <>
            <Retrieved at={at} label="Deployment events: " />
            <CoverageReasons coverage={page.coverage} label="Deployment event coverage" />
            {page.items.length === 0 ? <p>No install, rollback or pause is recorded.</p> : (
              <table>
                <caption>Install, rollback and pause history (ledger order)</caption>
                <Head cols={["Event", "Kind", "Component", "At", "Releases", "Schema after", "Identities"]} />
                <tbody>{page.items.map((e) => <EventRow key={e.id} e={e} />)}</tbody>
              </table>
            )}
            <More page={page} />
          </>
        )}
      </Resource>
    </section>
  );
}

function ApprovalPanel({ a, at }: { a: ApprovalClasses; at: Date }) {
  const served = a.classes.filter((k) => k.served_operations.length > 0);
  return (
    <>
      <Retrieved at={at} label="Approval classes: " />
      <p role="status" aria-label="Governance actions">
        {served.length === 0
          ? <><Tag>No governance action is served</Tag> No approval class has a served operation, so none can be used here.</>
          : <>
              <Tag>Operations listed by the server</Tag>{" "}
              {served.map((k) => `${k.class}: ${k.served_operations.join(", ")}`).join("; ")}. This view still has no control for them.
            </>}
      </p>
      <p>
        Advisory only ({a.deployment}, evaluated {utc(a.evaluated_at)} by the server): held never means an action will succeed,
        and the server re-checks every action.
      </p>
      <table>
        <caption>Your approval classes</caption>
        <Head cols={["Class", "Authority scope", "Held (advisory)", "Step-up", "Served operations", "Reasons"]} />
        <tbody>
          {a.classes.map((k) => (
            <tr key={k.class}>
              <th scope="row">{k.class}</th>
              <td>{k.authority_scope}</td>
              <td>{k.held ? "Held (advisory, not a grant)" : <Tag>Not held</Tag>}</td>
              <td>
                {k.step_up.satisfied && k.step_up.expires_at !== null
                  ? `Satisfied until ${utc(k.step_up.expires_at)}` : "Not satisfied"}
              </td>
              <td>{k.served_operations.join(", ") || "None served"}</td>
              <td><Codes codes={k.reasons} label={`Reasons for ${k.class}`} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      <CoverageReasons coverage={a.coverage} label="Approval classes coverage" />
    </>
  );
}

export function Approvals({ env }: { env: Env }) {
  const classes = useLoad(env, "/session/approval-classes", isApprovalClasses);
  return (
    <section aria-label="Approval classes">
      <h2>Your release authority</h2>
      <Resource label="Approval classes" state={classes}>{(a, at) => <ApprovalPanel a={a} at={at} />}</Resource>
    </section>
  );
}

/** The page reasons for one item, keyed `<kind>:<period>:<category>/<detail>` (T037 increment 6). */
const reasonsOf = (cv: Coverage, b: BudgetSummary) =>
  cv.reasons.filter((r) => r.includes(":") && r.slice(r.indexOf(":") + 1).startsWith(`${b.period}:${b.category}/`));
const kindOf = (r: string) => r.slice(0, r.indexOf(":"));
const CATEGORY: Record<string, string> = { inference: "Inference pool (covers every category)" };
const usd = (amount: BudgetSummary["cap_usd"]) => <MoneyText money={{ amount, currency: "USD" }} />;

function BudgetRow({ b, why, blocked }: { b: BudgetSummary; why: string[]; blocked: boolean }) {
  const kinds = new Set(why.map(kindOf));
  return (
    <tr>
      <th scope="row">{lookup(CATEGORY, b.category) ?? humanize(b.category)} (<code>{b.category}</code>)</th>
      <td>{usd(b.cap_usd)}{kinds.has("shared") && <> <Tag>Shared pool cap</Tag> the inference pool&apos;s cap, not this category&apos;s</>}</td>
      <td>{usd(b.spent_usd)}</td>
      <td>{usd(b.reserved_usd)}</td>
      <td>
        {blocked
          ? <><Tag>Not spendable</Tag> blocked: every new reservation is refused. Recorded headroom {usd(b.available_usd)} cannot be used.</>
          : <>{usd(b.available_usd)}{b.available_usd.startsWith("-") && <> <Tag>Overrun</Tag></>}{kinds.has("shared") && " (the pool's headroom)"}</>}
        {kinds.has("saturated") && <> <Tag>Totals at bound</Tag> the true totals exceed what can be shown.</>}
      </td>
      <td><Codes codes={why} label={`Reasons for ${b.period} ${b.category}`} /></td>
    </tr>
  );
}

export function Budgets({ env }: { env: Env }) {
  const budgets = useLoad(env, "/budgets?limit=200", isBudgetPage);
  return (
    <section aria-label="Budgets">
      <h2>Monthly budgets</h2>
      <Resource label="Budgets" state={budgets}>
        {(page, at) => {
          const periods = [...new Set(page.items.map((b) => b.period))];
          return (
            <>
              <Retrieved at={at} label="Budgets: " />
              <p>Any reason makes a budget page partial; read the reason kinds (blocked, saturated, shared) for each book.</p>
              <CoverageReasons coverage={page.coverage} label="Budget coverage" />
              {periods.length === 0 && <p>No budget book is recorded. No spend is shown, not zero spend.</p>}
              {periods.map((p) => {
                const rows = page.items.filter((b) => b.period === p);
                const why = rows.map((b) => reasonsOf(page.coverage, b));
                const blocks = [...new Set(why.flat().filter((r) => kindOf(r) === "blocked"))];
                return (
                  <div key={p}>
                    <h3>Budget book {p} (UTC month), scope {[...new Set(rows.map((b) => b.scope_id))].join(", ")}</h3>
                    {blocks.length > 0 && (
                      <div role="alert" aria-label={`Budget ${p} blocked`}>
                        <p><Tag>Blocked: not spendable</Tag> The server refuses every new reservation in this book, whatever available says:</p>
                        <Codes codes={blocks} label={`Block reasons for ${p}`} />
                      </div>
                    )}
                    <table>
                      <caption>Budget {p} in USD (decimal amounts as served)</caption>
                      <Head cols={["Category", "Cap", "Spent", "Reserved", "Available", "Reasons"]} />
                      <tbody>
                        {rows.map((b, i) => <BudgetRow key={b.category} b={b} why={why[i] ?? []} blocked={blocks.length > 0} />)}
                      </tbody>
                    </table>
                  </div>
                );
              })}
              <More page={page} />
            </>
          );
        }}
      </Resource>
    </section>
  );
}
