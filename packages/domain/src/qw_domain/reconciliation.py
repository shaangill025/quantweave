"""Observation reconciliation, cash states and open-order commitments (T017).

Spec §4 "Idempotency and conflict resolution", "Cash and commitments"; §6 freshness;
§7 "Portfolio-feasible sets"; T008 C-13, C-14, F-05. Conventions (`RECONCILE_METHOD`):
- An observation is a `SourceSnapshot` (CSV snapshot file today; a read-only broker
  connector later, T016) mapped to its account by `SourceMap`. It is compared with the
  journal reconstruction as of the snapshot's own time, using journal knowledge as of
  the evaluation time. Nothing here writes the journal: corrections stay explicit
  reversals through `Journal`.
- An account event effective after the snapshot makes it `stale`: newer user
  transactions are listed, never erased. A snapshot older than `max_age` is stale too.
- Differences are typed and classified against caller tolerances. A currency with no
  tolerance is exact. An instrument the journal does not hold is always material. An
  incomplete snapshot is silent about items it omits and never reconciles an account.
- Snapshot cash is compared with trade-date journal cash; whether a broker reports
  settled or total cash needs per-source qualification (T005).
- Cash per (account, currency): a trade's cash settles at the session close of the
  `lag`-th trading day after its local trade date, by caller-supplied terms per
  instrument (no regulation is encoded). Other cash events settle when effective. A
  reversed event and its reversal are both left out. available = settled + unsettled
  debits - open-order encumbrances; unsettled credits (sale proceeds) never count.
  spendable = available - pending withdrawals. `risk.evaluate` subtracts pending
  withdrawals itself, so `RiskInputs.available_cash` is `available`, not `spendable`.
- Open orders are recorded facts (user-declared or snapshot-reported), never
  submitted. One order ref counts once; differing copies leave cash unknown. A buy
  holds its reported hold, else quantity x limit + fee reserve (rounded up); a sell
  holds units and no cash. Any missing input leaves that currency's figures None with
  a reason. Only a broker or aggregator observation can confirm cash for sizing.
Stdlib only.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from qw_domain.calendars import CalendarError, ExchangeCalendar
from qw_domain.decimals import (
    DOMAIN_CONTEXT,
    Money,
    MoneyAmount,
    PositiveQuantity,
    Price,
    Quantity,
    Rounding,
    quantize,
)
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.journal import Journal, materialize
from qw_domain.postings import BookAccount, JournalEvent, UnitAccount
from qw_domain.risk import RiskInputs
from qw_domain.sources import SourceKind, SourceMap, SourceSnapshot

RECONCILE_METHOD = "observation_reconciliation/1"
_BROKER = frozenset(
    {SourceKind.BROKER_API, SourceKind.BROKER_EXPORT, SourceKind.AGGREGATOR}
)
_ZERO = Decimal(0)
_QUANTUM = Decimal("1e-12")
_UNSETTLED = {True: "unsettled_credits", False: "unsettled_debits"}

type CashBasis = Literal["broker_available", "user_reported"]


class ReconcileError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class DifferenceKind(StrEnum):
    MISSING_POSITION = "missing_position"
    QUANTITY_MISMATCH = "quantity_mismatch"
    CASH_MISMATCH = "cash_mismatch"
    UNKNOWN_INSTRUMENT = "unknown_instrument"


class Severity(StrEnum):
    MATERIAL = "material"
    IMMATERIAL = "immaterial"


class ReconStatus(StrEnum):
    RECONCILED = "reconciled"
    IMMATERIAL_DIFFERENCE = "immaterial_difference"
    MATERIAL_CONFLICT = "material_conflict"
    STALE = "stale"
    INCOMPLETE = "incomplete"


_USABLE = frozenset({ReconStatus.RECONCILED, ReconStatus.IMMATERIAL_DIFFERENCE})
_UNRECONCILED = frozenset({ReconStatus.MATERIAL_CONFLICT, ReconStatus.INCOMPLETE})


@dataclass(frozen=True, slots=True)
class Tolerance:
    """Materiality bounds from the adopted policy or the caller; never defaulted."""

    units: Quantity
    cash: Mapping[str, Money]

    def __post_init__(self) -> None:
        bad = [c for c, m in self.cash.items() if m.currency != c or m.amount.value < 0]
        if self.units.value < 0 or bad:
            raise ValueError(
                f"tolerance must be non-negative and keyed by currency {bad}"
            )
        object.__setattr__(self, "cash", MappingProxyType(dict(self.cash)))


@dataclass(frozen=True, slots=True)
class Difference:
    kind: DifferenceKind
    instrument_id: InstrumentId | None  # None for cash
    currency: str | None  # cash currency
    journal: Decimal
    observed: Decimal
    delta: Decimal  # observed - journal
    severity: Severity
    note: str = ""


def snapshot_ref(s: SourceSnapshot) -> str:
    return f"snapshot:{s.source_id}/{s.account_ref}@{format_instant(s.as_of)}"


@dataclass(frozen=True, slots=True)
class Reconciliation:
    account_id: str
    snapshot_ref: str
    source_kind: SourceKind
    observed_at: datetime  # the snapshot's as-of time, never its receipt time
    received_at: datetime | None
    as_of: datetime
    status: ReconStatus
    differences: tuple[Difference, ...]
    stale_reasons: tuple[str, ...]
    unreconciled_events: tuple[str, ...]  # account events newer than the snapshot
    cash_basis: CashBasis
    reported_cash: Mapping[str, Money]
    journal_revision: int
    method: str = RECONCILE_METHOD

    @property
    def sizing_blocked(self) -> bool:
        return self.status not in _USABLE

    @property
    def observation_age(self) -> timedelta:
        return self.as_of - self.observed_at

    @property
    def ingestion_delay(self) -> timedelta | None:
        return None if self.received_at is None else self.received_at - self.observed_at


def _severity(delta: Decimal, bound: Decimal | None) -> tuple[Severity, str]:
    if bound is None:
        return Severity.MATERIAL, "no_tolerance"
    return (Severity.MATERIAL if abs(delta) > bound else Severity.IMMATERIAL), ""


def reconcile(
    journal: Journal,
    source_map: SourceMap,
    snapshot: SourceSnapshot,
    account_id: str,
    tolerance: Tolerance,
    as_of: datetime,
    max_age: timedelta,
    received_at: datetime | None = None,
) -> Reconciliation:
    """Compare `snapshot` with the journal as of the snapshot's time. Pure. A snapshot
    after `as_of` (not yet known) raises, as does a negative `max_age`."""
    t = ensure_aware_utc(as_of)
    if max_age < timedelta(0):
        raise ReconcileError("max_age", "must not be negative")
    if snapshot.as_of > t:
        raise ReconcileError("snapshot_after_as_of", "not yet known at as_of")
    source = source_map.source(snapshot.source_id)
    if source is None:
        raise ReconcileError("unknown_source", snapshot.source_id)
    ref = (snapshot.source_id, snapshot.account_ref, snapshot.as_of)
    acct = source_map.resolve(*ref, t)
    if acct is None or (acct.account_id, acct.tenant_id) != (
        account_id,
        source.tenant_id,
    ):
        raise ReconcileError("unmapped", f"{snapshot.account_ref} is not {account_id}")
    events = [e for e in journal.events(t) if e.account_id == account_id]
    pos = materialize(events, snapshot.as_of)
    held = {k[1]: h.quantity.value for k, h in pos.holdings.items() if h.quantity.value}
    seen: dict[InstrumentId, Decimal] = {}
    for line in snapshot.holdings:
        if line.instrument_id in seen:
            raise ReconcileError("ambiguous_line", f"{line.instrument_id.to_wire()}")
        seen[line.instrument_id] = line.quantity.value
    diffs: list[Difference] = []
    for iid in sorted(held.keys() | seen.keys()):
        j, o = held.get(iid, _ZERO), seen.get(iid)
        if o is None and not snapshot.complete:
            continue
        o = _ZERO if o is None else o
        if j == o:
            continue
        with localcontext(DOMAIN_CONTEXT):
            delta = o - j
        kind = (
            DifferenceKind.QUANTITY_MISMATCH if o else DifferenceKind.MISSING_POSITION
        )
        sev, note = _severity(delta, tolerance.units.value)
        if not j:  # the journal holds none: an identity gap, never immaterial
            kind, sev, note = DifferenceKind.UNKNOWN_INSTRUMENT, Severity.MATERIAL, ""
        diffs.append(Difference(kind, iid, None, j, o, delta, sev, note))
    book = {c: m.amount.value for (a, c), m in pos.cash.items() if a == account_id}
    reported = {m.currency: m for m in snapshot.cash}
    for cur in sorted(book.keys() | reported.keys()):
        if cur not in reported and not snapshot.complete:
            continue
        j = book.get(cur, _ZERO)
        o = reported[cur].amount.value if cur in reported else _ZERO
        if j != o:
            with localcontext(DOMAIN_CONTEXT):
                delta = o - j
            bound = tolerance.cash.get(cur)
            sev, note = _severity(delta, None if bound is None else bound.amount.value)
            kind = DifferenceKind.CASH_MISMATCH
            diffs.append(Difference(kind, None, cur, j, o, delta, sev, note))
    newer = tuple(e.event_id for e in events if snapshot.as_of < e.effective_at <= t)
    stale = (("journal_newer_than_snapshot",) if newer else ()) + (
        ("snapshot_age",) if t - snapshot.as_of > max_age else ()
    )
    if any(d.severity is Severity.MATERIAL for d in diffs):
        status = ReconStatus.MATERIAL_CONFLICT
    elif stale:
        status = ReconStatus.STALE
    elif not snapshot.complete:
        status = ReconStatus.INCOMPLETE
    else:
        status = ReconStatus.IMMATERIAL_DIFFERENCE if diffs else ReconStatus.RECONCILED
    return Reconciliation(
        account_id, snapshot_ref(snapshot), source.kind, snapshot.as_of,
        None if received_at is None else ensure_aware_utc(received_at), t, status,
        tuple(diffs), stale, newer,
        "broker_available" if source.kind in _BROKER else "user_reported",
        MappingProxyType(reported), 1 + len(events),
    )  # fmt: skip


# ---- cash states and open orders


@dataclass(frozen=True, slots=True)
class SettlementTerms:
    """Caller-supplied convention: trading-day lag on the instrument's calendar."""

    calendar: ExchangeCalendar
    lag_trading_days: int

    def __post_init__(self) -> None:
        if type(self.lag_trading_days) is not int or self.lag_trading_days < 0:
            raise ValueError("lag_trading_days must be an int >= 0")

    def settles_at(self, traded_at: datetime) -> datetime:
        cal = self.calendar
        day = ensure_aware_utc(traded_at).astimezone(cal.tz).date()
        session = cal.session_for(cal.add_trading_days(day, self.lag_trading_days))
        if session is None:
            raise CalendarError(f"no session on the settlement day after {day}")
        return session.close_at


_ECONOMICS = (
    "account_id", "instrument_id", "side", "quantity", "currency", "limit_price",
    "fee_reserve", "reported_hold", "expires_at",
)  # fmt: skip


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderOrigin(StrEnum):
    USER_DECLARED = "user_declared"
    SNAPSHOT_REPORTED = "snapshot_reported"


@dataclass(frozen=True, slots=True)
class OpenOrder:
    """An externally placed open order, recorded as a fact. Never submitted."""

    order_ref: str
    account_id: str
    instrument_id: InstrumentId
    side: OrderSide
    quantity: PositiveQuantity
    currency: str
    limit_price: Price | None
    fee_reserve: Money
    origin: OrderOrigin
    source_id: str
    observed_at: datetime
    expires_at: datetime | None = None
    reported_hold: Money | None = None  # the broker's own cash hold, when reported

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", ensure_aware_utc(self.observed_at))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", ensure_aware_utc(self.expires_at))
        for m in (self.fee_reserve, self.reported_hold):
            if m is not None and (m.currency != self.currency or m.amount.value < 0):
                raise ValueError("holds and fees are >= 0 in the order currency")

    def economics(self) -> tuple[object, ...]:
        return tuple(getattr(self, f) for f in _ECONOMICS)

    def encumbrance(self) -> Money | None:
        """Cash held by this order; a sell holds none. None: unknown."""
        if self.side is OrderSide.SELL:
            return Money.of(0, self.currency)
        if self.reported_hold is not None:
            return self.reported_hold
        if self.limit_price is None:
            return None
        with localcontext(DOMAIN_CONTEXT):
            gross = self.quantity.value * self.limit_price.value
            gross += self.fee_reserve.amount.value
        amount = quantize(MoneyAmount, gross, quantum=_QUANTUM, rounding=Rounding.COST)
        return Money(amount, self.currency)


@dataclass(frozen=True, slots=True)
class PendingWithdrawal:
    ref: str
    account_id: str
    amount: Money
    recorded_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "recorded_at", ensure_aware_utc(self.recorded_at))
        if self.amount.amount.value <= 0:
            raise ValueError("a pending withdrawal is positive")


@dataclass(frozen=True, slots=True)
class CashState:
    account_id: str
    currency: str
    as_of: datetime
    settled: Money | None
    unsettled_credits: Money | None  # never spendable
    unsettled_debits: Money | None  # <= 0, already deducted from available
    reported: Money | None  # from the reconciled observation, informative
    external_encumbrances: Money | None
    pending_withdrawals: tuple[Money, ...]
    available: Money | None  # before pending withdrawals (the RiskInputs figure)
    spendable: Money | None  # after pending withdrawals
    reasons: tuple[str, ...]
    provenance: Mapping[str, tuple[str, ...]]
    included: tuple[str, ...] = ("settled", "unsettled_debits", "external_encumbrances")


def _active[T: (OpenOrder, PendingWithdrawal)](
    items: Iterable[T], account_id: str, t: datetime
) -> list[T]:
    def live(x: T) -> bool:
        if isinstance(x, PendingWithdrawal):
            return x.recorded_at <= t
        return x.observed_at <= t and (x.expires_at is None or t < x.expires_at)

    return [x for x in items if x.account_id == account_id and live(x)]


def _trade_instrument(e: JournalEvent) -> InstrumentId | None:
    return next(
        (u.instrument_id for u in e.units if u.account is UnitAccount.POSITION), None
    )


def cash_states(
    journal: Journal,
    account_id: str,
    as_of: datetime,
    settlement: Mapping[InstrumentId, SettlementTerms],
    observation: Reconciliation | None = None,
    open_orders: Iterable[OpenOrder] = (),
    withdrawals: Iterable[PendingWithdrawal] = (),
) -> dict[str, CashState]:
    """Settled, unsettled, reported and available cash per currency of one account,
    from journal knowledge as of `as_of`. Pure."""
    t = ensure_aware_utc(as_of)
    if observation is not None and observation.account_id != account_id:
        raise ValueError("observation is for another account")
    events = [e for e in journal.events(t) if e.account_id == account_id]
    reversed_ids = {e.reverses for e in events if e.reverses is not None}
    sums: dict[str, dict[str, Decimal]] = {}
    prov: dict[str, dict[str, list[str]]] = {}
    reasons: dict[str, list[str]] = {}

    def note(cur: str, reason: str) -> None:
        reasons.setdefault(cur, []).append(reason)

    def trace(cur: str, part: str, ref: str) -> None:
        prov.setdefault(cur, {}).setdefault(part, []).append(ref)

    for e in events:
        if e.effective_at > t:
            continue
        cash = [p.amount for p in e.money if p.account is BookAccount.CASH]
        if not cash or e.event_id in reversed_ids or e.reverses in reversed_ids:
            continue
        iid, settle = _trade_instrument(e), e.effective_at
        if iid is not None:
            terms = settlement.get(iid)
            try:
                if terms is None:
                    raise CalendarError(f"settlement_terms_missing:{iid.to_wire()}")
                settle = terms.settles_at(e.effective_at)
            except CalendarError as err:
                for m in cash:
                    note(m.currency, str(err))
                continue
        for m in cash:
            v, unsettled = m.amount.value, settle > t
            part = "settled" if not unsettled else _UNSETTLED[v > 0]
            row = sums.setdefault(m.currency, {})
            with localcontext(DOMAIN_CONTEXT):
                row[part] = row.get(part, _ZERO) + v
            trace(m.currency, part, f"event:{e.event_id}")
    holds: dict[str, Decimal] = {}
    first: dict[str, OpenOrder] = {}
    for o in _active(open_orders, account_id, t):
        prior = first.setdefault(o.order_ref, o)
        if prior is not o:
            if prior.economics() != o.economics():
                note(o.currency, f"open_order_disagreement:{o.order_ref}")
            continue
        hold = o.encumbrance()
        if hold is None:
            note(o.currency, f"open_order_hold_unknown:{o.order_ref}")
        elif o.side is OrderSide.BUY:
            with localcontext(DOMAIN_CONTEXT):
                holds[o.currency] = holds.get(o.currency, _ZERO) + hold.amount.value
            trace(o.currency, "external_encumbrances", f"order:{o.order_ref}")
    pending: dict[str, list[Money]] = {}
    for w in _active(withdrawals, account_id, t):
        pending.setdefault(w.amount.currency, []).append(w.amount)
        trace(w.amount.currency, "pending_withdrawals", f"withdrawal:{w.ref}")
    reported = dict(observation.reported_cash) if observation is not None else {}
    out: dict[str, CashState] = {}
    for cur in sorted(
        sums.keys() | reasons.keys() | holds.keys() | pending.keys() | reported.keys()
    ):
        row, why = sums.get(cur, {}), tuple(reasons.get(cur, ()))
        p = {k: tuple(v) for k, v in prov.get(cur, {}).items()}
        if cur in reported and observation is not None:
            p["reported"] = (observation.snapshot_ref,)
        out_w = tuple(pending.get(cur, ()))
        figures: list[Money | None] = [None] * 6
        if not why:  # any missing input leaves every derived figure unknown
            parts = ("settled", "unsettled_credits", "unsettled_debits")
            settled, credits, debits = (Money.of(row.get(k, _ZERO), cur) for k in parts)
            enc = Money.of(holds.get(cur, _ZERO), cur)
            avail = settled + debits - enc
            out_sum = sum(out_w, start=Money.of(0, cur))
            figures = [settled, credits, debits, enc, avail, avail - out_sum]
        settled_, credits_, debits_, enc_, avail_, spend_ = figures
        out[cur] = CashState(
            account_id, cur, t, settled_, credits_, debits_, reported.get(cur), enc_,
            out_w, avail_, spend_, why, MappingProxyType(p),
        )  # fmt: skip
    return out


def unit_availability(
    journal: Journal, account_id: str, as_of: datetime, open_orders: Iterable[OpenOrder]
) -> dict[InstrumentId, Quantity]:
    """Held units net of open sell orders (a negative figure is a conflict to show)."""
    t = ensure_aware_utc(as_of)
    events = [e for e in journal.events(t) if e.account_id == account_id]
    pos = materialize(events, t)
    out = {k[1]: h.quantity for k, h in pos.holdings.items() if h.quantity.value}
    seen: set[str] = set()
    for o in _active(open_orders, account_id, t):
        if o.side is OrderSide.SELL and o.order_ref not in seen:
            seen.add(o.order_ref)
            held = out.get(o.instrument_id, Quantity(0))
            out[o.instrument_id] = held - Quantity(o.quantity.value)
    return out


# ---- risk inputs (R012): the account fields `risk.evaluate` consumes


@dataclass(frozen=True, slots=True)
class RiskAccountInputs:
    account_observed_at: datetime | None
    reconciled: bool | None
    available_cash: Money | None
    pending_withdrawals: tuple[Money, ...]
    reasons: tuple[str, ...] = ()


def risk_account_inputs(
    observation: Reconciliation | None, cash: CashState | None
) -> RiskAccountInputs:
    """Freshness, reconciliation and cash for one account and currency. A missing
    observation, newer unreconciled events, a blocking status, a user-reported cash
    basis or unknown cash each leave the field that blocks sizing unset."""
    pair = (observation.account_id, observation.as_of) if observation else None
    if cash is not None and pair is not None and (cash.account_id, cash.as_of) != pair:
        raise ValueError("cash state is for another account or time")
    reasons: list[str] = []
    withdrawals = cash.pending_withdrawals if cash is not None else ()
    if observation is None:
        return RiskAccountInputs(None, None, None, withdrawals, ("no_observation",))
    seen: datetime | None = observation.observed_at
    if observation.unreconciled_events:
        seen = None
        reasons.append("journal_newer_than_snapshot")
    # Staleness reaches risk through the observed time and its age (ACCOUNT_STALE);
    # only a conflict or an incomplete view is unreconciled (ACCOUNT_CONFLICT).
    ok = observation.status not in _UNRECONCILED
    if observation.sizing_blocked:
        reasons.append(f"status:{observation.status.value}")
    if observation.cash_basis != "broker_available":  # spec §4: no claimed buying power
        reasons.append("cash_basis_user_reported")
    if cash is None:
        reasons.append("cash_state_missing")
    elif cash.available is None:
        reasons.extend(cash.reasons)
    broker = observation.cash_basis == "broker_available"
    usable = not observation.sizing_blocked and broker and cash is not None
    available = cash.available if usable and cash is not None else None
    return RiskAccountInputs(seen, ok, available, withdrawals, tuple(reasons))


def with_account_state(inputs: RiskInputs, fields: RiskAccountInputs) -> RiskInputs:
    return replace(
        inputs,
        account_observed_at=fields.account_observed_at,
        reconciled=fields.reconciled,
        available_cash=fields.available_cash,
        pending_withdrawals=fields.pending_withdrawals,
    )
