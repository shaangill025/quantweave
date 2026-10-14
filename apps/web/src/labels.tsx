// Shared display pieces. Every state is text (no colour-only cue) and every figure names its
// scope; an unavailable value is shown with its reason, never as 0 or blank.
import type { ReactNode } from "react";
import type { Loaded, Money, Page } from "./api";
import { formatDecimal, isDecimalString } from "./wire";

/** An offset timestamp normalised to UTC for display, e.g. "2026-10-10 14:05 UTC". */
export function utc(instant: string): string {
  const d = new Date(instant);
  if (isNaN(d.getTime())) return "unknown time";
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export const Unavailable = ({ reason }: { reason: string }) => <span>Unavailable ({reason})</span>;

/** One amount in one currency. The caller states the scope; amounts are never combined. */
export function MoneyText({ money }: { money: Money }) {
  if (!isDecimalString(money.amount)) return <Unavailable reason="invalid_decimal" />;
  return <span>{`${formatDecimal(money.amount)} ${money.currency}`}</span>;
}

export const Tag = ({ children }: { children: ReactNode }) => <strong>[{children}]</strong>;

export const Retrieved = ({ at, label = "" }: { at: Date; label?: string }) => (
  <p className="retrieved">{label}Retrieved {utc(at.toISOString())} (request time, not source time)</p>
);

/** Renders a loading or failed resource as an explicit state; never as an empty list. */
export function Resource<T>({
  label,
  state,
  children,
}: {
  label: string;
  state: Loaded<T> | undefined;
  children: (data: T, retrievedAt: Date) => ReactNode;
}) {
  if (state === undefined) return <p role="status">Loading {label}…</p>;
  if (state.status === "failed") {
    return (
      <div role="alert">
        <p>
          {label}: <Unavailable reason={state.code} /> {state.detail}
        </p>
        {state.code === "not_found" && (
          <p>Not connected: this API build does not serve {label.toLowerCase()} yet.</p>
        )}
        {state.correlationId && <p>Correlation id {state.correlationId}</p>}
      </div>
    );
  }
  return <>{children(state.data, state.retrievedAt)}</>;
}

/** True only for a full first page: no further cursor and no partial coverage. */
export const complete = (p: Page<unknown>) => p.next_cursor === null && p.coverage?.status !== "partial";

export function Completeness({ page, label = "" }: { page: Page<unknown>; label?: string }) {
  if (complete(page)) return null;
  return (
    <p>
      {label}
      {page.coverage?.status === "partial" && <><Tag>Partial list</Tag> {page.coverage.reasons.map(humanize).join(", ")} </>}
      {page.next_cursor !== null && "Only the first page is shown; more records exist."}
    </p>
  );
}

/** Focuses an in-page target without changing the URL hash (the hash belongs to the router). */
export function focusTarget(e: { preventDefault: () => void; currentTarget: HTMLAnchorElement }) {
  e.preventDefault();
  document.getElementById(e.currentTarget.hash.slice(1))?.focus();
}

export const humanize = (code: string): string => code.replaceAll("_", " ");
