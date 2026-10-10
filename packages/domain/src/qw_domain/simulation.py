"""Virtual portfolios, virtual orders and fills, and lifecycle events (T041; spec 14,
R063, R098). A simulator ledger only: nothing here reaches a broker or the real
journal.

Segregation: `VirtualEntry` and `VirtualPortfolio` are separate types; the real
`Journal` refuses them (it takes only `JournalEvent`) and `record` refuses anything
but a `VirtualEntry` of the same account. Every entry, order and fill carries a
constant virtual marker. A real snapshot seeds a portfolio by copy and is never
mutated. Each entry satisfies cash + cost = capital + realized + income - fees, so the
portfolio does too. Cost excludes fees; sales remove average cost.

Fill policy (one declared `ExecutionModel`): an order is live from submission +
latency. Only bars of the model's feed known at `as_of` (latest version) are used.
- A bar wholly after the live time: market fills at the open; a stop fills at the
  open when the open is through the stop, else at the stop; a limit fills at the
  open when the open is at or better than the limit, else at the limit only if the
  range strictly penetrates it (a touch is no fill). Open fills are stamped at the
  bar start, others at the bar end. Slippage is adverse; a limit is never violated.
- A bar containing the live time (order and bar collide): market and stop fill at
  the bar's worst extreme, stamped at the bar end and labelled ambiguous; a limit
  takes no fill from it (`submission_inside_bar` note).
- One-cancels-other group: if several trigger in one bar the worse price for the
  side fills first, labelled ambiguous; any fill cancels the rest of the group.
- Quantity per bar: participation x volume in whole lots (zero volume, no fill),
  then capped by the free units (held less short-call cover) and free cash (cash
  less put collateral) as of the fill stamp (entries effective at or before it),
  so nothing is sold or spent before it exists. No short sales. Orders on a
  virtual option contract are unsupported (options are written by `write_option`).
- Expiry: no fill is stamped at or after `expires_at` (`expired_in_bar`).
Time order: `record` refuses an entry earlier than the ledger's latest effective
time (`time_order`), so a ledger is append-only in time; fills in a bar are recorded
in stamp order. Orders are evaluated together, bar by bar, from the portfolio before
them: never re-simulate orders already recorded in that portfolio.

Lifecycle events (each returns a new portfolio or a typed `Unavailable`; a replay of
identical inputs returns the same portfolio and a conflicting replay of the same
event raises; every entry goes through `record`, so back-dated events raise):
- Cash dividend: once known, its pay date known and both pay and ex dates reached,
  units held before the ex-date's exchange-local midnight (the holder of record
  under T+1) earn amount x units as income, stamped at the later of the two local
  midnights (a due-bill special dividend goes ex after it pays).
- Split: units held before the effective date's local midnight scale exactly by the
  ratio; cost is unchanged. A fractional result would be cash in lieu, whose amount
  the corporate-action model leaves unknown (`CashInLieu`), so it is refused.
- A split, or a special dividend (which adjusts deliverables), on an underlying with
  open virtual options is refused (`option_adjustment_required`).
- Options: `write_option` sells to open a fully covered call or cash-secured put on
  verified physical terms (`contract_terms`) at a fresh positive bid mark; the
  write's own premium is not counted as its collateral (afterwards it is free cash)
  and the fee may not leave free cash negative. `settle_expiry` settles all open
  contracts of one contract id at the expiry session close, from a last or reference
  settlement mark observed between that close and the next session open, through
  the T034 lifecycle and `expiry_expectation`: worthless, or assignment delivering
  units at the strike; an uncertain or unavailable outcome changes nothing. Premium
  is a liability (negative cost) until expiry or assignment realizes it. Early
  assignment is not modelled.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from typing import Literal

from qw_domain.bars import Bar, BarBook
from qw_domain.calendars import CalendarCoverageError, ExchangeCalendar
from qw_domain.corporate_actions import CashDividend, Split
from qw_domain.decimals import DOMAIN_CONTEXT, Money, PositiveQuantity, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc
from qw_domain.journal import Positions
from qw_domain.option_lifecycle import (
    Event,
    Expectation,
    LifecycleEvent,
    Position,
    expiry_expectation,
)
from qw_domain.option_risk import check_mark, contract_terms
from qw_domain.options import OptionContract, OptionRight, Settlement
from qw_domain.risk import Side
from qw_domain.valuation import Mark, MarkKind, Unavailable

_Z = Decimal(0)


class SimError(ValueError):
    """An invalid simulator record or operation."""


class EntryKind(StrEnum):
    CAPITAL = "capital"
    SEED = "seed"
    FILL = "fill"
    DIVIDEND = "dividend"
    SPLIT = "split"
    OPTION_OPEN = "option_open"
    OPTION_CLOSE = "option_close"
    DELIVERY = "delivery"


@dataclass(frozen=True, slots=True)
class VirtualEntry:
    """One signed change to a virtual book, in the portfolio currency."""

    entry_id: str
    kind: EntryKind
    effective_at: datetime
    instrument_id: InstrumentId | None
    account_id: str
    cash: Decimal = _Z
    units: Decimal = _Z
    cost: Decimal = _Z
    realized: Decimal = _Z
    income: Decimal = _Z
    fees: Decimal = _Z
    capital: Decimal = _Z
    ref: str = ""
    virtual: Literal[True] = True

    def __post_init__(self) -> None:
        if self.virtual is not True:
            raise SimError("a simulator entry is always virtual")
        if not self.entry_id or not self.account_id or type(self.kind) is not EntryKind:
            raise SimError("entry id, account and kind are required")
        object.__setattr__(self, "effective_at", ensure_aware_utc(self.effective_at))
        nums = (
            self.cash,
            self.units,
            self.cost,
            self.realized,
            self.income,
            self.capital,
        )
        if not all(type(v) is Decimal and v.is_finite() for v in (*nums, self.fees)):
            raise TypeError("entry amounts must be finite Decimals")
        if self.fees < 0:
            raise SimError("fees cannot be negative")
        with localcontext(DOMAIN_CONTEXT):
            lhs = self.cash + self.cost
            rhs = self.capital + self.realized + self.income - self.fees
        if lhs != rhs:
            raise SimError(f"entry {self.entry_id} does not balance")


def _round12(v: Fraction) -> Decimal:
    with localcontext(DOMAIN_CONTEXT):
        return Decimal(round(v * 10**12)).scaleb(-12)


def _ceil12(v: Fraction) -> Decimal:
    with localcontext(DOMAIN_CONTEXT):
        return Decimal(-((-v * 10**12) // 1)).scaleb(-12)


def _share(total: Decimal, part: Decimal, whole: Decimal) -> Decimal:
    """`total * part / whole` to 1e-12 (exact when everything is removed)."""
    if part == whole:
        return total
    return _round12(Fraction(Fraction(total) * Fraction(part), Fraction(whole)))


def _lots(num: Decimal, den: Decimal, lot: Decimal) -> Decimal:
    if num <= 0:
        return _Z
    with localcontext(DOMAIN_CONTEXT):
        return Decimal(Fraction(num) // (Fraction(den) * Fraction(lot))) * lot


@dataclass(frozen=True, slots=True)
class VirtualPortfolio:
    account_id: str
    currency: str
    entries: tuple[VirtualEntry, ...] = ()
    options: tuple[Position, ...] = ()  # written (short) options, T034 lifecycle
    virtual: Literal[True] = True

    @classmethod
    def open(cls, account_id: str, capital: Money, at: datetime) -> "VirtualPortfolio":
        if type(capital) is not Money or capital.amount.value <= 0:
            raise SimError("virtual capital must be positive Money")
        c = capital.amount.value
        e = VirtualEntry(f"capital:{account_id}", EntryKind.CAPITAL, at, None,
                         account_id, cash=c, capital=c)  # fmt: skip
        return cls(account_id, capital.currency).record(e)

    @classmethod
    def seed(
        cls, account_id: str, cash: Money, real: Positions, real_account: str,
        at: datetime,
    ) -> "VirtualPortfolio":  # fmt: skip
        """Copy one real account's holdings (units and cost) from a read-only
        snapshot; virtual capital is `cash` plus the copied cost."""
        p = cls.open(account_id, cash, at)
        for (acct, iid), h in sorted(real.holdings.items(), key=lambda kv: str(kv[0])):
            if acct != real_account or h.quantity.value == 0:
                continue
            if h.quantity.value < 0 or h.cost is None:
                raise SimError("seed needs long holdings with known cost")
            if h.cost.currency != p.currency:
                raise SimError("seed holding in another currency")
            q, c = h.quantity.value, h.cost.amount.value
            p = p.record(
                VirtualEntry(
                    f"seed:{iid.to_wire()}",
                    EntryKind.SEED,
                    at,
                    iid,
                    account_id,
                    units=q,
                    cost=c,
                    capital=c,
                )
            )
        return p

    def record(self, entry: VirtualEntry) -> "VirtualPortfolio":
        if type(entry) is not VirtualEntry:
            raise TypeError("a virtual portfolio records only VirtualEntry")
        if entry.account_id != self.account_id:
            raise SimError("entry belongs to another virtual account")
        for e in self.entries:
            if e.entry_id == entry.entry_id:
                if e != entry:
                    raise SimError("entry id reused with a different payload")
                return self
        if self.entries and entry.effective_at < max(
            e.effective_at for e in self.entries
        ):
            raise SimError("time_order: entry earlier than the ledger's latest")
        return replace(self, entries=(*self.entries, entry))

    def _sum(
        self, attr: str, iid: InstrumentId | None = None, at: datetime | None = None,
        before: datetime | None = None,
    ) -> Decimal:  # fmt: skip
        """Total of `attr` over entries effective at or before `at` and strictly
        before `before` (default all)."""
        with localcontext(DOMAIN_CONTEXT):
            return sum(
                (getattr(e, attr) for e in self.entries
                 if (iid is None or e.instrument_id == iid)
                 and (at is None or e.effective_at <= at)
                 and (before is None or e.effective_at < before)),
                _Z,
            )  # fmt: skip

    def cash(self, at: datetime | None = None) -> Decimal:
        return self._sum("cash", at=at)

    def capital(self) -> Decimal:
        return self._sum("capital")

    def realized(self) -> Decimal:
        return self._sum("realized")

    def income(self) -> Decimal:
        return self._sum("income")

    def fees(self) -> Decimal:
        return self._sum("fees")

    def cost(self, iid: InstrumentId) -> Decimal:
        return self._sum("cost", iid)

    def units(
        self, iid: InstrumentId, at: datetime | None = None,
        before: datetime | None = None,
    ) -> Decimal:  # fmt: skip
        return self._sum("units", iid, at, before)

    def instruments(self) -> tuple[InstrumentId, ...]:
        found = {e.instrument_id for e in self.entries if e.instrument_id is not None}
        return tuple(sorted(found, key=lambda i: i.to_wire()))

    def option(self, contract_id: InstrumentId) -> Position:
        for p in self.options:
            if p.contract.contract_id == contract_id:
                return p
        raise SimError("no virtual option position for that contract")

    def _cover(
        self, right: OptionRight, iid: InstrumentId | None, at: datetime | None
    ) -> Decimal:
        """Put collateral (strike cash) or units covering short calls on `iid`, for
        contracts open as of `at` (from the ledger), rounded up."""
        total = Fraction()
        for p in self.options:
            c = p.contract
            n = -Fraction(self.units(c.contract_id, at))
            other = right is OptionRight.CALL and c.underlying_id != iid
            if c.right is not right or n <= 0 or other:
                continue
            terms = contract_terms(c)
            if isinstance(terms, Unavailable):  # fail closed, never count zero
                raise SimError(f"open option without usable terms: {terms.code}")
            put = right is OptionRight.PUT
            total += (terms.strike_cash if put else terms.units) * n
        return _ceil12(total)

    def reserved(self, at: datetime | None = None) -> Decimal:
        return self._cover(OptionRight.PUT, None, at)

    def free_units(self, iid: InstrumentId, at: datetime | None = None) -> Decimal:
        with localcontext(DOMAIN_CONTEXT):
            return self.units(iid, at) - self._cover(OptionRight.CALL, iid, at)

    def _has(self, entry_id: str) -> bool:
        return any(e.entry_id == entry_id for e in self.entries)

    def _optioned(self, iid: InstrumentId) -> bool:
        return any(
            p.contract.underlying_id == iid and self.units(p.contract.contract_id)
            for p in self.options
        )


# ---- orders and fills


class OrderStyle(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"  # in the contract, not modelled yet: unsupported


class OrderStatus(StrEnum):
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    UNFILLED = "unfilled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class VirtualOrder:
    order_id: str
    instrument_id: InstrumentId
    side: Side
    quantity: PositiveQuantity
    style: OrderStyle
    submitted_at: datetime
    limit_price: Price | None = None
    stop_price: Price | None = None
    expires_at: datetime | None = None
    proposal_id: str | None = None
    oco_group: str | None = None
    broker_submission: Literal[False] = False

    def __post_init__(self) -> None:
        if self.broker_submission is not False:
            raise SimError("a virtual order never has a broker submission")
        if type(self.quantity) is not PositiveQuantity or not self.order_id:
            raise SimError("order id and a PositiveQuantity are required")
        object.__setattr__(self, "submitted_at", ensure_aware_utc(self.submitted_at))
        if self.expires_at is not None:
            exp = ensure_aware_utc(self.expires_at)
            if exp <= self.submitted_at:
                raise SimError("an order must expire after submission")
            object.__setattr__(self, "expires_at", exp)

    def unsupported(self) -> bool:
        lim, stop = self.limit_price is not None, self.stop_price is not None
        return {
            OrderStyle.MARKET: lim or stop,
            OrderStyle.LIMIT: not lim or stop,
            OrderStyle.STOP: lim or not stop,
            OrderStyle.STOP_LIMIT: True,
        }[self.style]


@dataclass(frozen=True, slots=True)
class ExecutionModel:
    model_id: str
    feed_id: str
    latency: timedelta
    participation: Decimal  # share of bar volume, 0 < p <= 1
    lot: Decimal  # quantity increment, a positive power of ten
    slippage: Decimal  # adverse price change per unit, >= 0
    fee_per_fill: Money
    fee_per_unit: Money

    def __post_init__(self) -> None:
        ok = (
            self.latency >= timedelta(0)
            and _Z < self.participation <= 1
            and self.lot > 0
            and self.slippage >= 0
            and self.fee_per_fill.amount.value >= 0
            and self.fee_per_unit.amount.value >= 0
            and self.fee_per_fill.currency == self.fee_per_unit.currency
        )
        if not ok:
            raise SimError("invalid execution model")


@dataclass(frozen=True, slots=True)
class VirtualFill:
    fill_id: str
    order_id: str
    filled_at: datetime
    known_at: datetime  # knowledge time of the bar the fill came from
    bar_start: datetime
    quantity: Decimal
    price: Decimal
    fees: Decimal
    execution_model_id: str
    assumptions: tuple[str, ...]
    ambiguous: bool
    actual_execution: Literal[False] = False


@dataclass(frozen=True, slots=True)
class OrderResult:
    order: VirtualOrder
    status: OrderStatus
    fills: tuple[VirtualFill, ...]
    remaining: Decimal
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SimRun:
    portfolio: VirtualPortfolio
    results: tuple[OrderResult, ...]


@dataclass(slots=True)
class _Live:
    order: VirtualOrder
    remaining: Decimal
    fills: list[VirtualFill] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cancelled: bool = False

    def note(self, code: str) -> None:
        if code not in self.notes:
            self.notes.append(code)


def _visible(
    book: BarBook, feed: str, ids: set[InstrumentId], as_of: datetime
) -> list[Bar]:
    latest: dict[tuple[InstrumentId, datetime, datetime], Bar] = {}
    for b in book.bars:
        if b.feed_id == feed and b.instrument_id in ids and b.knowledge_at <= as_of:
            k = (b.instrument_id, b.start, b.end)
            if k not in latest or b.knowledge_at >= latest[k].knowledge_at:
                latest[k] = b
    return sorted(latest.values(), key=lambda b: (b.start, b.instrument_id.to_wire()))


def _trigger(
    o: VirtualOrder, b: Bar, live_at: datetime, slip: Decimal
) -> tuple[Decimal, datetime, tuple[str, ...]] | str | None:
    """(price, filled_at, assumptions), a note code, or None when not triggered."""
    inside = b.start < live_at
    op, hi, lo = b.ohlc.open.value, b.ohlc.high.value, b.ohlc.low.value
    buy = o.side is Side.BUY
    worst = hi if buy else lo
    if o.style is OrderStyle.MARKET:
        base, at_open = (worst, False) if inside else (op, True)
    elif o.style is OrderStyle.STOP:
        assert o.stop_price is not None
        s = o.stop_price.value
        if not (hi >= s if buy else lo <= s):
            return None
        gap = op >= s if buy else op <= s
        base, at_open = (worst, False) if inside else (op if gap else s, gap)
    else:
        assert o.limit_price is not None
        lim = o.limit_price.value
        gap = op <= lim if buy else op >= lim
        if not (gap or (lo < lim if buy else hi > lim)):
            return None
        if inside:
            return "submission_inside_bar"
        base, at_open = (op if gap else lim), gap
    with localcontext(DOMAIN_CONTEXT):
        px = base + slip if buy else base - slip
    if o.style is OrderStyle.LIMIT:
        assert o.limit_price is not None
        lim = o.limit_price.value
        px = min(px, lim) if buy else max(px, lim)
    if px <= 0:
        return "nonpositive_price"
    notes = ("submission_inside_bar",) if inside else ()
    return px, b.start if at_open else b.end, notes


def simulate(
    portfolio: VirtualPortfolio,
    orders: Sequence[VirtualOrder],
    book: BarBook,
    model: ExecutionModel,
    as_of: datetime,
) -> SimRun:
    as_of = ensure_aware_utc(as_of)
    if model.fee_per_fill.currency != portfolio.currency:
        raise SimError("fees in another currency than the portfolio")
    if len({o.order_id for o in orders}) != len(orders):
        raise SimError("duplicate order id")
    live = [_Live(o, o.quantity.value) for o in orders]
    contracts = {pos.contract.contract_id for pos in portfolio.options}
    for lv in live:
        if lv.order.unsupported() or lv.order.instrument_id in contracts:
            lv.note("order_terms_unsupported")
    p = portfolio
    ids = {o.instrument_id for o in orders}
    for b in _visible(book, model.feed_id, ids, as_of):
        hits: list[tuple[int, Decimal, datetime, tuple[str, ...]]] = []
        for i, lv in enumerate(live):
            o = lv.order
            t0 = o.submitted_at + model.latency
            if (
                lv.notes[:1] == ["order_terms_unsupported"] or lv.cancelled
                or not lv.remaining or o.instrument_id != b.instrument_id
                or b.end <= t0 or (o.expires_at is not None and b.start >= o.expires_at)
            ):  # fmt: skip
                continue
            got = _trigger(o, b, t0, model.slippage)
            if isinstance(got, str):
                lv.note(got)
            elif got is not None and o.expires_at and got[1] >= o.expires_at:
                lv.note("expired_in_bar")
            elif got is not None:
                hits.append((i, *got))
        chosen: list[tuple[int, Decimal, datetime, tuple[str, ...]]] = []
        groups: dict[str, list[tuple[int, Decimal, datetime, tuple[str, ...]]]] = {}
        for h in hits:
            g = live[h[0]].order.oco_group
            (groups.setdefault(g, []) if g else chosen).append(h)
        for hs in groups.values():
            buy = live[hs[0][0]].order.side is Side.BUY
            w = max(hs, key=lambda h: h[1]) if buy else min(hs, key=lambda h: h[1])
            extra = ("stop_and_target_same_bar",) if len(hs) > 1 else ()
            chosen.append((w[0], w[1], w[2], w[3] + extra))
        for i, px, filled_at, notes in sorted(chosen, key=lambda c: (c[2], c[0])):
            p = _fill(p, live, i, b, px, filled_at, notes, model)
    results = []
    for lv in live:
        o, done = (
            lv.order,
            lv.order.expires_at is not None and lv.order.expires_at <= as_of,
        )
        if lv.notes[:1] == ["order_terms_unsupported"]:
            status = OrderStatus.UNSUPPORTED
        elif not lv.remaining:
            status = OrderStatus.FILLED
        elif lv.cancelled:
            status = OrderStatus.CANCELLED
        elif lv.fills:
            status = OrderStatus.PARTIALLY_FILLED
        else:
            status = OrderStatus.EXPIRED if done else OrderStatus.SUBMITTED
        results.append(OrderResult(o, status, tuple(lv.fills), lv.remaining,
                                   tuple(lv.notes)))  # fmt: skip
    return SimRun(p, tuple(results))


def _fill(
    p: VirtualPortfolio, live: list[_Live], i: int, b: Bar, px: Decimal,
    filled_at: datetime, notes: tuple[str, ...], m: ExecutionModel,
) -> VirtualPortfolio:  # fmt: skip
    lv = live[i]
    o, iid = lv.order, lv.order.instrument_id
    fixed, per = m.fee_per_fill.amount.value, m.fee_per_unit.amount.value
    with localcontext(DOMAIN_CONTEXT):
        cap = _lots(b.volume.value * m.participation, Decimal(1), m.lot)
        if not cap:
            lv.note("no_volume")
            return p
        qty = min(lv.remaining, cap)
        if qty < lv.remaining:
            lv.note("participation_cap")
        if o.side is Side.SELL:
            free = p.free_units(iid, filled_at)
            room, short = _lots(free, Decimal(1), m.lot), "units"
        else:
            free = p.cash(filled_at) - p.reserved(filled_at)
            room = _lots(free - fixed, px + per, m.lot)
            short = "cash"
        if room < qty:
            lv.note(f"insufficient_virtual_{short}")
            qty = room
        if qty <= 0:
            return p
        fees, gross = fixed + per * qty, px * qty
        n = len(lv.fills) + 1
        fid = f"{o.order_id}:fill:{n}"
        if o.side is Side.BUY:
            e = VirtualEntry(fid, EntryKind.FILL, filled_at, iid, p.account_id,
                             cash=-gross - fees, units=qty, cost=gross, fees=fees,
                             ref=o.order_id)  # fmt: skip
        else:
            out = _share(p.cost(iid), qty, p.units(iid))
            e = VirtualEntry(
                fid,
                EntryKind.FILL,
                filled_at,
                iid,
                p.account_id,
                cash=gross - fees,
                units=-qty,
                cost=-out,
                realized=gross - out,
                fees=fees,
                ref=o.order_id,
            )
        lv.remaining -= qty
    amb = bool(notes)
    lv.fills.append(VirtualFill(fid, o.order_id, filled_at, b.knowledge_at, b.start,
                                qty, px, fees, m.model_id, notes, amb))  # fmt: skip
    if o.oco_group:
        for other in live:
            if other is not lv and other.order.oco_group == o.oco_group:
                other.cancelled = True
    return p.record(e)


# ---- lifecycle events


def _local_midnight(cal: ExchangeCalendar, d: date) -> datetime:
    return datetime.combine(d, time(), tzinfo=cal.tz).astimezone(UTC)


def apply_dividend(
    p: VirtualPortfolio, action: CashDividend, cal: ExchangeCalendar, as_of: datetime
) -> VirtualPortfolio | Unavailable:
    as_of, iid, eid = ensure_aware_utc(as_of), action.instrument_id, action.event_id
    if action.known_at > as_of:
        return Unavailable("action_not_known", eid)
    if action.pay_date is None:
        return Unavailable("pay_date_unknown", eid)
    if action.currency != p.currency:
        return Unavailable("currency_mismatch", action.currency)
    pay = _local_midnight(cal, action.pay_date)
    if pay > as_of:
        return Unavailable("pay_date_not_reached", action.pay_date.isoformat())
    ex = _local_midnight(cal, action.ex_date)
    if ex > as_of:  # a due-bill special dividend goes ex after it pays
        return Unavailable("ex_date_not_reached", action.ex_date.isoformat())
    units = p.units(iid, before=ex)
    if units <= 0:
        return Unavailable("no_entitlement", eid)
    with localcontext(DOMAIN_CONTEXT):
        amt = units * action.amount_per_share.amount.value
    e = VirtualEntry(f"div:{eid}", EntryKind.DIVIDEND, max(pay, ex), iid,
                     p.account_id, cash=amt, income=amt,
                     ref=f"{eid}:v{action.version}")  # fmt: skip
    if action.special and p._optioned(iid) and not p._has(e.entry_id):
        return Unavailable("option_adjustment_required", eid)
    return p.record(e)


def apply_split(
    p: VirtualPortfolio, action: Split, cal: ExchangeCalendar, as_of: datetime
) -> VirtualPortfolio | Unavailable:
    as_of, iid, eid = ensure_aware_utc(as_of), action.instrument_id, action.event_id
    cut = _local_midnight(cal, action.effective)
    if action.known_at > as_of or cut > as_of:
        return Unavailable("action_not_effective", eid)
    held = p.units(iid, before=cut)
    if held <= 0:
        return Unavailable("no_entitlement", eid)
    new = Fraction(held) * action.ratio.fraction()
    if new.denominator != 1:
        return Unavailable("cash_in_lieu_terms_unknown", eid)
    with localcontext(DOMAIN_CONTEXT):
        delta = Decimal(new.numerator) - held
    e = VirtualEntry(f"split:{eid}", EntryKind.SPLIT, cut, iid, p.account_id,
                     units=delta, ref=f"{eid}:v{action.version}")  # fmt: skip
    if p._optioned(iid) and not p._has(e.entry_id):
        return Unavailable("option_adjustment_required", eid)
    return p.record(e)


def _expiry_close(cal: ExchangeCalendar, c: OptionContract) -> datetime | Unavailable:
    day = c.expiry.session_date
    if c.expiry.calendar_id != cal.calendar_id:
        return Unavailable("calendar_mismatch", c.expiry.calendar_id.code)
    try:
        session = cal.session_for(day)
    except CalendarCoverageError:
        session = None
    if session is None:
        return Unavailable("expiry_session_unknown", day.isoformat())
    return session.close_at


def write_option(
    p: VirtualPortfolio, contract: OptionContract, contracts: int, premium: Mark,
    fee: Money, cal: ExchangeCalendar, at: datetime, entry_id: str,
    max_age: timedelta,
) -> VirtualPortfolio | Unavailable:  # fmt: skip
    """Sell to open `contracts` fully covered calls or cash-secured puts."""
    at, cid = ensure_aware_utc(at), contract.contract_id
    if type(contracts) is not int or contracts < 1:
        raise SimError("contracts must be a positive int")
    terms = contract_terms(contract)
    if isinstance(terms, Unavailable):
        return terms
    if contract.settlement is not Settlement.PHYSICAL:
        return Unavailable("settlement_unsupported", contract.settlement.value)
    if terms.currency != p.currency or fee.currency != p.currency:
        return Unavailable("currency_mismatch", terms.currency)
    if premium.kind is not MarkKind.BID or premium.price.value <= 0:
        return Unavailable("premium_not_bid", "a sold option is marked at the bid")
    if stale := check_mark(premium, cid, p.currency, at, max_age, "premium"):
        return stale
    close = _expiry_close(cal, contract)
    if isinstance(close, Unavailable):
        return close
    if at >= close:
        return Unavailable("expiry_passed", contract.expiry.session_date.isoformat())
    assert contract.multiplier is not None  # implied by contract_terms
    with localcontext(DOMAIN_CONTEXT):
        credit = premium.price.value * contract.multiplier.value * contracts
        f = fee.amount.value
    ref = f"{premium.source}@{premium.observed_at.isoformat()}"
    e = VirtualEntry(entry_id, EntryKind.OPTION_OPEN, at, cid, p.account_id,
                     cash=credit - f, units=Decimal(-contracts), cost=-credit,
                     fees=f, ref=ref)  # fmt: skip
    if p._has(entry_id):
        return p.record(e)  # identical replay is a no-op, a conflict raises
    if any(o.contract.contract_id == cid for o in p.options):
        return Unavailable("position_exists", "one virtual position per contract")
    n, free = Fraction(contracts), Fraction(p.cash(at) - p.reserved(at))
    if contract.right is OptionRight.CALL:
        need = terms.units * n
        if Fraction(p.free_units(contract.underlying_id, at)) < need:
            return Unavailable("cover_insufficient", f"call needs {need} free units")
    else:
        free -= terms.strike_cash * n  # this write's premium is not collateral
        if free < 0:
            return Unavailable("cover_insufficient", "put needs free strike cash")
    if free + Fraction(credit - f) < 0:
        return Unavailable(
            "fee_exceeds_premium", "the write would leave free cash negative"
        )
    pos = Position(contract, long=False, open_quantity=n)
    return replace(p.record(e), options=(*p.options, pos))


def settle_expiry(
    p: VirtualPortfolio, contract_id: InstrumentId, settlement: Mark,
    auto_exercise_min: Price | None, cal: ExchangeCalendar, as_of: datetime,
    max_age: timedelta,
) -> VirtualPortfolio | Unavailable:  # fmt: skip
    """Settle the open contracts of one contract id at the expiry close."""
    as_of = ensure_aware_utc(as_of)
    pos = p.option(contract_id)
    c, key = pos.contract, contract_id.to_wire()
    ref = f"{settlement.source}:{settlement.price.value}@{settlement.observed_at}"
    for e in p.entries:
        if e.entry_id == f"{key}:settle":
            if e.ref != ref:
                raise SimError("expiry already settled with a different settlement")
            return p
    close = _expiry_close(cal, c)
    if isinstance(close, Unavailable):
        return close
    if as_of < close:
        return Unavailable("expiry_not_reached", c.expiry.session_date.isoformat())
    if settlement.kind not in (MarkKind.LAST, MarkKind.REFERENCE):
        return Unavailable(
            "settlement_kind_invalid", "settlement must be a last or reference price"
        )
    if settlement.observed_at < close:
        return Unavailable(
            "settlement_before_expiry", "settlement observed before the expiry close"
        )
    try:
        reopen = cal.next_open(close)
    except CalendarCoverageError:
        return Unavailable("expiry_session_unknown", "no session after the expiry")
    if settlement.observed_at >= reopen:  # no post-expiry quote decides assignment
        return Unavailable(
            "settlement_outside_window",
            "settlement observed after the next session open",
        )
    open_n = pos.open_quantity
    view = expiry_expectation(c, Quantity(-open_n.numerator), settlement,
                              auto_exercise_min, as_of, max_age)  # fmt: skip
    if isinstance(view, Unavailable):
        return view
    if view.expectation is Expectation.UNCERTAIN:
        return Unavailable(
            "assignment_uncertain", "in the money below the auto-exercise threshold"
        )
    assigned = view.expectation is Expectation.ASSIGNMENT
    if not assigned and view.expectation is not Expectation.WORTHLESS:
        return Unavailable("unsupported_expectation", view.expectation.value)
    pos = pos.apply(LifecycleEvent(f"{key}:expiry", Event.EXPIRY_REACHED, close,
                                   "simulation"))  # fmt: skip
    kind = Event.ASSIGNMENT_REPORTED if assigned else Event.EXPIRED_WORTHLESS_REPORTED
    qty = Quantity(open_n.numerator) if assigned else None
    pos = pos.apply(LifecycleEvent(f"{key}:{kind.value}", kind, close, "simulation",
                                   qty))  # fmt: skip
    liability = p.cost(contract_id)  # negative: premium received
    q = p.record(VirtualEntry(f"{key}:settle", EntryKind.OPTION_CLOSE, close,
                              contract_id, p.account_id, units=_round12(open_n),
                              cost=-liability, realized=-liability,
                              ref=ref))  # fmt: skip
    q = replace(q, options=tuple(pos if o.contract.contract_id == contract_id else o
                                 for o in q.options))  # fmt: skip
    if not assigned:
        return q
    du, dc, und = _round12(view.units), _round12(view.cash), c.underlying_id
    with localcontext(DOMAIN_CONTEXT):
        if du < 0:  # deliver the covering units at the strike
            if q.units(und) < -du:
                raise SimError("covering units are missing")
            out = _share(q.cost(und), -du, q.units(und))
            e = VirtualEntry(f"{key}:delivery", EntryKind.DELIVERY, close, und,
                             q.account_id, cash=dc, units=du, cost=-out,
                             realized=dc - out, ref=ref)  # fmt: skip
        else:  # receive the units and pay the strike from the collateral
            if q.cash() < -dc:
                raise SimError("put collateral is missing")
            e = VirtualEntry(f"{key}:delivery", EntryKind.DELIVERY, close, und,
                             q.account_id, cash=dc, units=du, cost=-dc,
                             ref=ref)  # fmt: skip
    return q.record(e)
