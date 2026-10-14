// Overview, Accounts and Decisions views (spec 13). Read-only: there is no control that
// places, amends or cancels a broker order anywhere in the app.
import { useEffect, useRef, useState } from "react";
import {
  getJson, isAccount, isPolicy, isProposal, isSnapshot, pageOf,
  type Account, type Fetcher, type Guard, type Loaded, type Proposal,
} from "./api";
import { LimitDraft } from "./LimitDraft";
import { Completeness, MoneyText, complete, Resource, Retrieved, Tag, Unavailable, humanize, utc } from "./labels";
import { formatDecimal } from "./wire";

export interface Env {
  fetcher: Fetcher;
  now: () => Date;
}
const accountPage = pageOf(isAccount);
const policyPage = pageOf(isPolicy);
const proposalPage = pageOf(isProposal);

function useLoad<T>(env: Env, path: string, guard: Guard<T>) {
  const [state, setState] = useState<Loaded<T>>();
  useEffect(() => {
    let live = true;
    setState(undefined);
    void getJson(env.fetcher, path, guard, env.now).then((s) => {
      if (live) setState(s);
    });
    return () => {
      live = false;
    };
  }, [env, path, guard]);
  return state;
}

const SCOPE = {
  complete_declared: "Complete for declared holdings", hypothetical: "Hypothetical",
  partial_declared: "Partial: conclusions cover declared holdings only",
};
const ACCOUNT_STATUS = { active: "Active", archived: "Archived", conflicted: "Reconciliation conflict" };
const BASIS = {
  broker_available: "broker-reported available", reconciled_ledger: "reconciled ledger",
  user_reported: "user-reported, unconfirmed", unknown: "unknown basis, unconfirmed",
};
const FRESHNESS = { current_under_policy: "Current under freshness policy", stale: "STALE", unknown: "Freshness unknown" };
const RECONCILIATION = {
  reconciled: "Reconciled", partial: "Partially reconciled",
  conflicted: "Contradictory sources: reconciliation conflict, sizing blocked",
};
const POLICY_STATUS = {
  draft: "Draft", awaiting_adoption: "Awaiting your adoption (pending)", adopted: "Adopted",
  superseded: "Superseded", paused: "Paused: new risk blocked",
};
const PROPOSAL_STATE: Record<string, string> = {
  candidate: "Candidate (pending)", verifying: "Verifying (pending)",
  awaiting_review: "Awaiting review (pending)", ready_for_final_check: "Awaiting final check (pending)",
  active: "Active", research_only: "Research only, not actionable", rejected: "Rejected",
  expired: "Expired", invalidated: "Invalidated", dismissed: "Dismissed",
  superseded: "Superseded by a newer version",
};
const LIVE = ["candidate", "verifying", "awaiting_review", "ready_for_final_check", "active"];
const stateLabel = (s: string) => PROPOSAL_STATE[s] ?? `Unknown state (${s})`;
/** A live proposal whose expiry has passed on this clock: shown, never treated as actionable. */
const pastExpiry = (p: Proposal, now: Date) =>
  LIVE.includes(p.state) && new Date(p.expires_at).getTime() <= now.getTime();

const Head = ({ cols }: { cols: string[] }) => (
  <thead><tr>{cols.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
);

export function Overview({ env }: { env: Env }) {
  const accounts = useLoad(env, "/accounts?limit=200", accountPage);
  const policies = useLoad(env, "/policies?limit=100", policyPage);
  const proposals = useLoad(env, "/proposals?limit=200", proposalPage);
  const items: string[] = [];
  const now = env.now();
  if (accounts?.status === "ok") {
    for (const a of accounts.data.items) {
      if (a.status === "conflicted") items.push(`${a.name}: reconciliation conflict between sources.`);
      if (a.analysis_scope === "partial_declared") items.push(`${a.name}: partial portfolio; outside holdings are unknown, not zero.`);
    }
  }
  if (policies?.status === "ok") {
    for (const p of policies.data.items) {
      if (p.status === "awaiting_adoption") items.push(`Policy version ${String(p.version)} awaits your adoption.`);
      if (p.status === "paused") items.push(`Policy version ${String(p.version)} is paused: new risk blocked.`);
    }
  }
  if (proposals?.status === "ok") {
    for (const p of proposals.data.items) {
      const what = `${humanize(p.action)} proposal for ${p.account_id}`;
      if (pastExpiry(p, now)) items.push(`${what}: past expiry, awaiting server expiry.`);
      else if (LIVE.includes(p.state) && p.state !== "active") items.push(`${what}: ${stateLabel(p.state)}.`);
    }
  }
  const sources = [accounts, policies, proposals];
  const settled = sources.every((s) => s?.status === "ok" && complete(s.data));
  return (
    <>
      <h2>Needs attention</h2>
      {items.length > 0 ? (
        <ul>{items.map((t) => <li key={t}>{t}</li>)}</ul>
      ) : (
        settled && <p>Nothing in the loaded records needs attention.</p>
      )}
      {sources.some((s) => s?.status === "failed") && <p>Some records could not be loaded, so this list may be incomplete.</p>}
      <h2>Record sources</h2>
      <Resource label="Accounts" state={accounts}>{(d, at) => <><Retrieved label="Accounts: " at={at} /><Completeness label="Accounts: " page={d} /></>}</Resource>
      <Resource label="Policies" state={policies}>{(d, at) => <><Retrieved label="Policies: " at={at} /><Completeness label="Policies: " page={d} /></>}</Resource>
      <Resource label="Proposals" state={proposals}>{(d, at) => <><Retrieved label="Proposals: " at={at} /><Completeness label="Proposals: " page={d} /></>}</Resource>
    </>
  );
}

function AccountTable({ title, rows, onOpen }: { title: string; rows: Account[]; onOpen: (a: Account) => void }) {
  if (rows.length === 0) return null;
  return (
    <table>
      <caption>{title}</caption>
      <Head cols={["Account", "Scope", "Status", "Cash basis", "Sources", "Snapshot"]} />
      <tbody>
        {rows.map((a) => (
          <tr key={a.id}>
            <th scope="row">{a.name} ({a.broker}, {a.account_type})</th>
            <td>{SCOPE[a.analysis_scope]}</td>
            <td>{a.status === "conflicted" ? <Tag>{ACCOUNT_STATUS.conflicted}</Tag> : ACCOUNT_STATUS[a.status]}</td>
            <td>{BASIS[a.cash_basis]}</td>
            <td>{a.source_ids.length ? a.source_ids.join(", ") : <Unavailable reason="no_sources" />}</td>
            <td>
              <button type="button" onClick={() => { onOpen(a); }}>Show snapshot for {a.name}</button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SnapshotPanel({ env, account }: { env: Env; account: Account }) {
  const path = `/accounts/${encodeURIComponent(account.id)}/snapshot`;
  const state = useLoad(env, path, isSnapshot);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, [account.id]); // keyboard users land on the panel
  const mismatch = state?.status === "ok" && state.data.account_id !== account.id;
  return (
    <section aria-labelledby="snapshot-title">
      <h2 id="snapshot-title" tabIndex={-1} ref={heading}>Snapshot: {account.name}</h2>
      {mismatch ? (
        <p role="alert"><Unavailable reason="snapshot_account_mismatch" /></p>
      ) : (
        <Resource label="Snapshot" state={state}>
          {(s, at) => (
            <>
              <Retrieved at={at} />
              <p>
                <Tag>{FRESHNESS[s.freshness_status]}</Tag> <Tag>{RECONCILIATION[s.reconciliation]}</Tag>
              </p>
              <p>As of {utc(s.effective_at)}; received {utc(s.received_at)}; account revision {s.revision}.</p>
              <p>
                Unresolved items: {s.unresolved_ids.length ? s.unresolved_ids.join(", ") : "none reported"}
              </p>
              <table>
                <caption>
                  Available cash in {account.name} only, per currency ({BASIS[s.cash_basis]}
                  {s.freshness_status === "stale" ? "; stale" : ""}). Not usable by other accounts.
                </caption>
                <Head cols={["Currency", "Available"]} />
                <tbody>
                  {s.available_cash.length === 0 ? (
                    <tr><td colSpan={2}><Unavailable reason="no_cash_observation" /></td></tr>
                  ) : (
                    s.available_cash.map((m) => (
                      <tr key={m.currency}><th scope="row">{m.currency}</th><td><MoneyText money={m} /></td></tr>
                    ))
                  )}
                </tbody>
              </table>
              <table>
                <caption>Positions in {account.name} as of {utc(s.effective_at)}</caption>
                <Head cols={["Instrument", "Quantity", "Economic cost", "Lot coverage"]} />
                <tbody>
                  {s.positions.length === 0 && <tr><td colSpan={4}>No positions reported in this snapshot.</td></tr>}
                  {s.positions.map((p, i) => (
                    <tr key={`${String(i)}:${p.instrument_id}`}>
                      <th scope="row">{p.instrument_id}</th>
                      <td>{formatDecimal(p.quantity)}</td>
                      <td>{p.economic_cost ? <MoneyText money={p.economic_cost} /> : <Unavailable reason="cost_unknown" />}</td>
                      <td>{humanize(p.lot_coverage)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </Resource>
      )}
    </section>
  );
}

export function Accounts({ env }: { env: Env }) {
  const accounts = useLoad(env, "/accounts?limit=200", accountPage);
  const [open, setOpen] = useState<Account | null>(null);
  return (
    <Resource label="Accounts" state={accounts}>
      {(page, at) => (
        <>
          <Retrieved at={at} />
          {page.items.length === 0 && <p>No accounts are declared yet.</p>}
          <AccountTable title="Real accounts" rows={page.items.filter((a) => a.analysis_scope !== "hypothetical")} onOpen={setOpen} />
          <AccountTable title="Hypothetical accounts (not real holdings)" rows={page.items.filter((a) => a.analysis_scope === "hypothetical")} onOpen={setOpen} />
          <Completeness page={page} />
          {open && <SnapshotPanel key={open.id} env={env} account={open} />}
        </>
      )}
    </Resource>
  );
}

function ProposalCard({ p, now }: { p: Proposal; now: Date }) {
  const id = `proposal-${p.id}-${String(p.version)}`;
  const q = p.qualification;
  return (
    <article aria-labelledby={id}>
      <h3 id={id}>
        {humanize(p.action)} · {p.legs.map((l) => l.instrument_id).join(", ") || "no instrument"} · {humanize(p.horizon)}
      </h3>
      <p>
        <Tag>{stateLabel(p.state)}</Tag> {pastExpiry(p, now) && <Tag>Past expiry, not actionable</Tag>} Expires{" "}
        {utc(p.expires_at)}. Version {p.version}.
      </p>
      <p>
        Account {p.account_id}, sleeve {p.sleeve_id}. Origin {humanize(p.origin)};{" "}
        {p.mode === "ai_enabled" ? "AI-enabled" : "rules-only"} mode. Execution: {humanize(p.execution_state)}.
      </p>
      <p>
        Qualification: operational {humanize(q.operational)}; investment evidence{" "}
        {humanize(q.investment_evidence)}; eligibility {humanize(q.user_eligibility)}.
      </p>
      <p>Reasons: {[...p.reason_codes, ...q.reason_codes].join(", ") || "none given"}</p>
      <table>
        <caption>Legs (estimated cash effect is for account {p.account_id} only)</caption>
        <Head cols={["Instrument", "Side", "Quantity", "Estimated cash effect"]} />
        <tbody>
          {p.legs.map((l, i) => (
            <tr key={`${String(i)}:${l.instrument_id}`}>
              <th scope="row">{l.instrument_id}</th>
              <td>{l.side}</td>
              <td>{l.quantity === null ? <Unavailable reason={l.quantity_basis} /> : formatDecimal(l.quantity)}</td>
              <td>{l.estimated_cash_effect ? <MoneyText money={l.estimated_cash_effect} /> : <Unavailable reason="not_estimated" />}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </article>
  );
}

export function Decisions({ env }: { env: Env }) {
  const policies = useLoad(env, "/policies?limit=100", policyPage);
  const proposals = useLoad(env, "/proposals?limit=200", proposalPage);
  return (
    <>
      <h2>Policy versions</h2>
      <Resource label="Policies" state={policies}>
        {(page, at) => (
          <>
            <Retrieved at={at} />
            {page.items.length === 0 && <p>No policy exists yet: sizing stays blocked until one is adopted.</p>}
            <ul>
              {page.items.map((p) => (
                <li key={p.id}>
                  <Tag>{POLICY_STATUS[p.status]}</Tag> Version {p.version}, accounts {p.scope_account_ids.join(", ")}.{" "}
                  {p.accepted_at ? `Adopted ${utc(p.accepted_at)} by ${p.accepted_by ?? "unknown"}.` : "Not adopted."}
                  {p.numerical_limits.length === 0 ? (
                    <p>No numerical limits: sizing is blocked for this scope.</p>
                  ) : (
                    <ul aria-label={`Limits of policy version ${String(p.version)}`}>
                      {p.numerical_limits.map((l) => (
                        <li key={`${l.metric}:${l.scope_id}`}>
                          {humanize(l.metric)}: {formatDecimal(l.value)} {l.unit} of {humanize(l.denominator)}, scope {l.scope_id}
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
            <Completeness page={page} />
          </>
        )}
      </Resource>
      <LimitDraft />
      <h2>Proposals</h2>
      <p>Proposals are analysis only. Execution happens outside this app; nothing here places an order.</p>
      <Resource label="Proposals" state={proposals}>
        {(page, at) => (
          <>
            <Retrieved at={at} />
            {page.items.length === 0 && <p>No proposals.</p>}
            {page.items.map((p) => <ProposalCard key={`${p.id}:${String(p.version)}`} p={p} now={env.now()} />)}
            <Completeness page={page} />
          </>
        )}
      </Resource>
    </>
  );
}
