"""Planning commitments and joint feasibility of published proposals (T026).

Spec §7 "Portfolio-feasible sets", §4 "Cash and commitments", §3 "Atomic final
decision"; T008 F-09. Conventions:
- A commitment is application planning state, never a broker reservation. A
  `CommitmentBook` per (tenant, account, currency) holds qualified available cash,
  which already excludes unexecuted sale proceeds, unconfirmed transfers and
  pending deposits, and one commitment per active proposal version.
- Admission (`publish_committed`) re-runs `risk.evaluate` on the combined state:
  every other commitment is applied to cash, allocation usage, held units, position
  values and issuer/sector exposure, so concentration and stress see the joint
  set. Two states are evaluated, and both must pass (worst case per check):
  - The pending state, for cash, allocation and concentration: every other buy
    is executed (value at the current mark; held units too, except when a sale is
    evaluated, since unexecuted buys are not sellable) and no other sale is, though
    other sales reduce held units when a sale is evaluated (no joint overselling).
  - The stress state. Stress is linear in position values and each commitment
    fills or not, so the worst case fixes each one independently: it counts as
    executed only if that raises the stress loss. A buy is executed only if its
    instrument's shock is <= 0; a sale only if the shock is > 0 (a hedge removed).
    An instrument without a shock is executed if bought and kept if sold, so it
    stays in the state and `risk.evaluate` blocks `STRESS_SHOCK_MISSING`.
  The stress state is evaluated only when it differs, i.e. some other commitment
  is in a positive-shock instrument.
  Every member of another alternatives group counts for exposure (conservative).
  Reserved cash (alternatives groups count their maximum, everything else adds)
  must stay within `available`. Admission is first come, first served.
- A buy reserves max(bound requirement, cost at the current ask/last mark).
- An alternatives group lives in one book (tenant, account, currency). Once a
  member is planned the group is closed to new members.
- Only an `active` latest version holds a commitment. `sync` drops every other one
  (superseded, expired, invalidated, rejected, dismissed); callers pass every
  lifecycle decision through it; the functions here sync what they return.
Stdlib only; the locked PostgreSQL publication is a later increment.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, Decimal, localcontext
from fractions import Fraction

from qw_domain.decimals import DOMAIN_CONTEXT, Money, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.proposals import (
    Code,
    Current,
    Decision,
    Proposal,
    ProposalError,
    ProposalState,
    ProposalVersion,
    Reason,
    cash_requirement,
    mark_planned,
    publish,
)
from qw_domain.risk import Outcome, ProposedAction, RiskEvaluation, RiskInputs, Side
from qw_domain.valuation import MarkKind


@dataclass(frozen=True, slots=True)
class Commitment:
    proposal_id: str
    version: int
    group_id: str | None
    amount: Money  # reserved cash
    action: ProposedAction
    price: Price | None  # mark at admission, to value the exposure without one
    planned: bool = False


def _money(exact: Decimal, currency: str) -> Money:
    with localcontext(DOMAIN_CONTEXT):
        amount = exact.quantize(Decimal(1).scaleb(-12), rounding=ROUND_CEILING)
    return Money.of(amount, currency)


def _ceil(exact: Fraction, currency: str) -> Money:
    scaled = exact * 10**12
    with localcontext(DOMAIN_CONTEXT):
        amount = Decimal(-(-scaled.numerator // scaled.denominator)).scaleb(-12)
    return Money.of(amount, currency)


def _value(q: Quantity | Decimal, price: Price, currency: str) -> Money:
    qty = q.value if isinstance(q, Quantity) else q
    with localcontext(DOMAIN_CONTEXT):
        return _money(qty * price.value, currency)


def _gain(x: RiskInputs, i: InstrumentId) -> bool:
    """`i` is a hedge: its scenario return is positive, so selling it (or a buy
    of it not filling) raises the stress loss."""
    shock = x.stress_shocks.get(i)
    return shock is not None and shock.value > 0


@dataclass(frozen=True, slots=True)
class CommitmentBook:
    tenant_id: str
    account_id: str
    currency: str
    available: Money  # qualified cash: no unexecuted sales, transfers or deposits
    commitments: tuple[Commitment, ...] = ()

    def __post_init__(self) -> None:
        if self.available.currency != self.currency or self.available.amount.value < 0:
            raise ProposalError("book", "available cash: non-negative, book currency")

    def reserved(self, *, skip: str | None = None) -> Money:
        """Ungrouped commitments add; each alternatives group reserves its maximum.
        `skip` leaves one group out."""
        total, groups = Money.of(0, self.currency), dict[str, Money]()
        for c in self.commitments:
            if c.group_id is None:
                total = total + c.amount
            elif c.group_id != skip:
                groups[c.group_id] = max(groups.get(c.group_id, c.amount), c.amount)
        return sum(groups.values(), total)

    def holds(self, v: ProposalVersion) -> bool:
        key = (v.proposal_id, v.version)
        return any((c.proposal_id, c.version) == key for c in self.commitments)

    def _combined(
        self, x: RiskInputs, others: tuple[Commitment, ...], reserved: Money,
        action: ProposedAction, stress: bool,
    ) -> RiskInputs:  # fmt: skip
        """`x` with the other commitments applied (the pending state, or with
        `stress` the stress worst case; see the module notes). No risk maths."""
        held, values = dict(x.held_units), dict(x.position_values)
        iss, sec = dict(x.issuer_exposure), dict(x.sector_exposure)
        zero = Money.of(0, self.currency)
        for c in others:
            o, i = c.action, c.action.instrument
            before = held.get(i)
            mark = x.marks.get(i)
            ok = mark is not None and mark.currency == self.currency
            price = mark.price if mark is not None and ok else c.price
            if price is None:
                raise ProposalError("commitment", "an admitted action has a price")
            value = _value(o.quantity.value, price, self.currency)
            if o.side is Side.SELL:
                fill = stress and _gain(x, i)
                left = None if before is None else before.value - o.quantity.value
                if left is not None and (action.side is Side.SELL or fill):
                    held[i] = Quantity(left)
                if fill and i in values:
                    # Remove at least the listed value's share of the units sold.
                    if before is not None and before.value > 0:
                        sold = Fraction(o.quantity.value)
                        held_before = Fraction(before.value)
                        listed = Fraction(values[i].amount.value)
                        part = Fraction(listed * sold, held_before)
                        value = max(value, _ceil(part, self.currency))
                    rest = values[i] - value
                    gone = (left is not None and left <= 0) or rest.amount.value < 0
                    values[i] = zero if gone else rest
                continue
            if stress and _gain(x, i):
                continue  # a pending hedge buy may never fill
            if action.side is Side.SELL and i == action.instrument:
                pass  # its value stays units x mark for the evaluated sale
            elif i in values:
                values[i] = values[i] + value
            elif i != action.instrument and (before is None or not before.value):
                values[i] = value
            if action.side is Side.BUY:  # unexecuted buys are not sellable units
                held[i] = Quantity((before.value if before else 0) + o.quantity.value)
            iss[o.issuer_id] = iss.get(o.issuer_id, zero) + value
            sec[o.sector_id] = sec.get(o.sector_id, zero) + value
        used = x.allocation_used
        return replace(
            x, available_cash=self.available - reserved,
            allocation_used=None if used is None else used + reserved,
            held_units=held, position_values=values, issuer_exposure=iss,
            sector_exposure=sec,
        )  # fmt: skip

    def admit(
        self, v: ProposalVersion, cur: Current
    ) -> tuple["CommitmentBook", tuple[Reason, ...], RiskEvaluation | None]:
        a, group = v.action, v.alternatives_group_id
        if v.scope != (self.tenant_id, self.account_id, self.currency):
            raise ProposalError("book", "version is outside this book's scope")
        if any(c.proposal_id == v.proposal_id and c.version >= v.version
               for c in self.commitments):  # fmt: skip
            raise ProposalError("duplicate", "this version is already committed")
        if cur.inputs.available_cash != self.available:
            return (
                self,
                (Reason(Code.CASH_UNCONFIRMED, "book and inputs differ"),),
                None,
            )
        if group and any(c.group_id == group and c.planned for c in self.commitments):
            return self, (Reason(Code.ALTERNATIVE_SELECTED, group),), None
        base = replace(self, commitments=tuple(
            c for c in self.commitments if c.proposal_id != v.proposal_id))  # fmt: skip
        others = tuple(
            c for c in base.commitments if group is None or c.group_id != group
        )
        mark = cur.inputs.marks.get(a.instrument)
        usable = mark is not None and mark.kind in (MarkKind.ASK, MarkKind.LAST)
        price = mark.price if mark is not None and usable else None
        need = v.requirement
        if price is not None and mark is not None and mark.currency == self.currency:
            need = max(need, cash_requirement(a, price))
        # Worst case per check: the pending state, then the stress state when it
        # differs (some other commitment in a positive-shock instrument).
        reserved = base.reserved(skip=group)
        hedges = any(_gain(cur.inputs, c.action.instrument) for c in others)
        reasons: list[Reason] = []
        ev = cur.evaluate(a, base._combined(cur.inputs, others, reserved, a, False))
        tag = ""
        if ev.outcome is not Outcome.BLOCK and hedges:
            ins = base._combined(cur.inputs, others, reserved, a, True)
            ev, tag = cur.evaluate(a, ins), "stress_worst_case:"
        if ev.outcome is Outcome.BLOCK:
            why = ",".join(r.code for r in ev.reasons)
            reasons.append(Reason(Code.RISK_BLOCKED, tag + why))
        if ev.policy_hash != v.binding.policy_hash:
            reasons.append(Reason(Code.POLICY_CHANGED))
        same = mark is not None and mark.currency == self.currency
        held_at = mark.price if mark is not None and same else None
        new = Commitment(v.proposal_id, v.version, group, need, a, held_at)
        book = replace(base, commitments=(*base.commitments, new))
        if self.available < book.reserved():
            reasons.append(Reason(Code.CAPITAL_CONFLICT, str(book.reserved())))
        return (self, tuple(reasons), ev) if reasons else (book, (), ev)


def sync(book: CommitmentBook, *proposals: Proposal) -> CommitmentBook:
    """Keep, for each given proposal, only the commitment of its active latest
    version; release everything else it holds."""
    kept = book.commitments
    for p in proposals:
        pid, v = p.latest.proposal_id, p.latest.version
        live = v if p.state(v) is ProposalState.ACTIVE else None
        kept = tuple(c for c in kept if c.proposal_id != pid or c.version == live)
    return replace(book, commitments=kept)


def publish_committed(
    p: Proposal, n: int, cur: Current, book: CommitmentBook
) -> tuple[Decision, CommitmentBook, RiskEvaluation | None]:
    """`proposals.publish` with admission into `book`: the final check and the
    commitment check both pass, or nothing is reserved."""
    seen: list[tuple[CommitmentBook, RiskEvaluation | None]] = []

    def admit(v: ProposalVersion, c: Current) -> tuple[Reason, ...]:
        admitted, reasons, ev = book.admit(v, c)
        seen.append((admitted, ev))
        return reasons

    d = publish(p, n, cur, admit)
    after, ev = seen[0] if seen else (book, None)
    return d, sync(after if d.accepted else book, d.proposal), ev


def plan_committed(
    p: Proposal, n: int, cur: Current, book: CommitmentBook,
    siblings: Mapping[str, Proposal] | None = None,
) -> tuple[Decision, CommitmentBook]:  # fmt: skip
    """`proposals.mark_planned` on a reserved version. Every other group member
    holding a commitment must be passed in `siblings`; their commitments are
    released and the chosen one is marked planned, closing the group."""
    given = dict(siblings or {})
    book = sync(book, p, *given.values())
    v, group = p.latest, p.latest.alternatives_group_id
    members = {c.proposal_id for c in book.commitments if group and c.group_id == group}
    if missing := sorted(members - {v.proposal_id} - set(given)):
        raise ProposalError("siblings_missing", ",".join(missing))
    if n == v.version and p.state(n) is ProposalState.ACTIVE and not book.holds(v):
        return Decision(
            False, p, (Reason(Code.CAPITAL_CONFLICT, "not reserved"),)
        ), book
    d = mark_planned(p, n, cur, given)
    if d.accepted:
        key = (v.proposal_id, v.version)
        book = replace(book, commitments=tuple(
            replace(c, planned=True) if (c.proposal_id, c.version) == key else c
            for c in book.commitments))  # fmt: skip
    return d, sync(book, d.proposal, *d.siblings.values())
