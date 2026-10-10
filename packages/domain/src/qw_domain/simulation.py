"""Virtual portfolios, virtual orders and fills (T041 increment 1; spec 14, R063,
R098). Lifecycle events (dividends, splits, option expiry and assignment) follow in
increment 2. A simulator ledger only: nothing here reaches a broker or the real
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
  then capped by the units and cash held as of the fill stamp (entries effective
  at or before it), so nothing is sold or spent before it exists. No short sales.
- Expiry: no fill is stamped at or after `expires_at` (`expired_in_bar`).
Time order: `record` refuses an entry earlier than the ledger's latest effective
time (`time_order`), so a ledger is append-only in time; fills in a bar are recorded
in stamp order. Orders are evaluated together, bar by bar, from the portfolio before
them: never re-simulate orders already recorded in that portfolio.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from typing import Literal

from qw_domain.bars import Bar, BarBook
from qw_domain.decimals import DOMAIN_CONTEXT, Money, PositiveQuantity, Price
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc
from qw_domain.journal import Positions
from qw_domain.risk import Side

_Z = Decimal(0)


class SimError(ValueError):
    """An invalid simulator record or operation."""


class EntryKind(StrEnum):
    CAPITAL = "capital"
    SEED = "seed"
    FILL = "fill"


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
        self, attr: str, iid: InstrumentId | None = None, at: datetime | None = None
    ) -> Decimal:
        """Total of `attr` over entries effective at or before `at` (default all)."""
        with localcontext(DOMAIN_CONTEXT):
            return sum(
                (getattr(e, attr) for e in self.entries
                 if (iid is None or e.instrument_id == iid)
                 and (at is None or e.effective_at <= at)),
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

    def units(self, iid: InstrumentId, at: datetime | None = None) -> Decimal:
        return self._sum("units", iid, at)

    def instruments(self) -> tuple[InstrumentId, ...]:
        found = {e.instrument_id for e in self.entries if e.instrument_id is not None}
        return tuple(sorted(found, key=lambda i: i.to_wire()))


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
    for lv in live:
        if lv.order.unsupported():
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
            room, short = _lots(p.units(iid, filled_at), Decimal(1), m.lot), "units"
        else:
            room = _lots(p.cash(filled_at) - fixed, px + per, m.lot)
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
