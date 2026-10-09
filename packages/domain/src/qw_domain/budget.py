"""Monthly inference budget pools and reserve-before-dispatch (T037 increment 1).

Spec §10/§15 budgets, §11 improvement cap, config `budgets_and_slos.json`, schema
`budget_reservation`, T008 F-18 (reserved + spent <= cap under concurrency).
- One `BudgetBook` per scope and UTC month ("YYYY-MM"). Caps come from config:
  inference covers every category; improvement has its own cap inside it. The spec
  package authorizes no spending, so without an owner authorization reference every
  reservation is refused.
- Reserve the caller's worst case before dispatch; a same-id retry with the same
  terms returns the book unchanged while the reservation is still open and unsent
  (otherwise `reservation_closed`). `settle` records the metered actual (an overrun
  is kept as reported and blocks further reservations). `release` is only for an
  undispatched reservation. A dispatched one stays reserved until a usage report or
  its deadline; `expire_due` then charges the full reservation (`expired`), and a
  later report may still settle it.
- A charge belongs to the month of its reservation, whenever the usage report
  arrives (pending owner/spec confirmation of the billing-month rule).
- The book is immutable and versioned: `apply` installs a new book by
  compare-and-set on `version`; persistence does it in one transaction.
Stdlib only.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from qw_domain.decimals import UsdBudget, safe_repr
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.sources import ID_PATTERN

ZERO = UsdBudget(0)


class BudgetError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def check_id(value: object, what: str) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise BudgetError(what, f"{safe_repr(value)} is malformed")
    return value


class Category(StrEnum):  # schema `budget_reservation.category`, priority order
    MONITORING = "monitoring"
    VERIFICATION = "verification"
    RESEARCH = "research"
    IMPROVEMENT = "improvement"


class Status(StrEnum):
    RESERVED = "reserved"
    SETTLED = "settled"
    RELEASED = "released"
    EXPIRED = "expired"  # deadline passed unreconciled: charged at the reservation


def period_of(at: datetime) -> str:
    return ensure_aware_utc(at).strftime("%Y-%m")


def _budget(config: Mapping[str, object], key: str) -> UsdBudget:
    if key not in config:
        raise BudgetError("config", f"missing {key}")
    if not isinstance(value := config[key], str):
        raise TypeError(f"{key} must be a decimal string")
    return UsdBudget(value)


@dataclass(frozen=True, slots=True)
class BudgetCaps:
    inference: UsdBudget  # every category
    improvement: UsdBudget  # inside `inference`
    authorization_ref: str | None  # owner spending authorization; None blocks all

    def __post_init__(self) -> None:
        if {type(self.inference), type(self.improvement)} != {UsdBudget}:
            raise TypeError("caps are UsdBudget")
        if self.improvement < ZERO or self.inference < self.improvement:
            raise BudgetError("caps", "0 <= improvement <= inference")
        if self.authorization_ref is not None:
            check_id(self.authorization_ref, "authorization_ref")

    @classmethod
    def from_config(
        cls, config: Mapping[str, object], *, authorization_ref: str | None
    ) -> "BudgetCaps":
        """Caps from `budgets_and_slos.json` (decimal strings)."""
        pilot = _budget(config, "pilot_monthly_usd")
        return cls(pilot, _budget(config, "improvement_monthly_usd_max"),
                   authorization_ref)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Refusal:
    code: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Reservation:
    reservation_id: str
    scope_id: str
    period: str
    category: Category
    reserved: UsdBudget
    provider_account_ref: str
    max_output_tokens: int
    created_at: datetime
    deadline: datetime  # reconciliation deadline for a dispatched call
    status: Status = Status.RESERVED
    dispatched: bool = False
    actual: UsdBudget | None = None

    @property
    def charge(self) -> tuple[UsdBudget, UsdBudget]:
        """(reserved, spent) contribution."""
        if self.status is Status.RESERVED:
            return self.reserved, ZERO
        return ZERO, self.actual or ZERO

    def to_wire(self) -> dict[str, object]:  # schema `budget_reservation`
        actual = None if self.actual is None else self.actual.to_wire()
        return {
            "id": self.reservation_id, "scope_id": self.scope_id,
            "period": self.period, "category": self.category.value,
            "currency": "USD", "reserved_amount": self.reserved.to_wire(),
            "actual_amount": actual, "status": self.status.value,
            "provider_account_reference": self.provider_account_ref,
            "max_output_tokens": self.max_output_tokens,
            "created_at": format_instant(self.created_at),
        }  # fmt: skip


@dataclass(frozen=True, slots=True)
class Summary:  # openapi `BudgetSummary`
    category: Category | None
    cap: UsdBudget
    reserved: UsdBudget
    spent: UsdBudget
    available: UsdBudget  # negative after a reported overrun


def _open(r: Reservation, *allowed: Status) -> Reservation:
    if r.status not in allowed:
        raise BudgetError("not_reserved", r.status.value)
    return r


@dataclass(frozen=True, slots=True)
class BudgetBook:
    scope_id: str
    period: str
    caps: BudgetCaps
    reservations: tuple[Reservation, ...] = ()
    version: int = 0

    def __post_init__(self) -> None:
        check_id(self.scope_id, "scope_id")
        if type(self.caps) is not BudgetCaps:
            raise TypeError("caps must be BudgetCaps")

    @classmethod
    def open(cls, scope_id: str, at: datetime, caps: BudgetCaps) -> "BudgetBook":
        return cls(scope_id, period_of(at), caps)

    def find(self, reservation_id: str) -> Reservation | None:
        return next((r for r in self.reservations
                     if r.reservation_id == reservation_id), None)  # fmt: skip

    def get(self, reservation_id: str) -> Reservation:
        if (r := self.find(reservation_id)) is None:
            raise BudgetError("unknown", safe_repr(reservation_id))
        return r

    def summary(self, category: Category | None = None) -> Summary:
        """Totals for the book, or one category. Every category but improvement
        draws on the shared inference cap, so its `available` is the book's."""
        reserved = spent = ZERO
        for r in self.reservations:
            if category is None or r.category is category:
                held, used = r.charge
                reserved, spent = reserved + held, spent + used
        if category is Category.IMPROVEMENT:
            cap = self.caps.improvement
            free = min(cap - reserved - spent, self.summary().available)
        else:
            cap = self.caps.inference
            free = self.summary().available if category else cap - reserved - spent
        return Summary(category, cap, reserved, spent, free)

    def _with(self, r: Reservation) -> "BudgetBook":
        rid, old = r.reservation_id, self.reservations
        rows = tuple(r if x.reservation_id == rid else x for x in old)
        rows = (*rows, r) if self.find(rid) is None else rows
        return replace(self, reservations=rows, version=self.version + 1)

    def reserve(
        self, reservation_id: str, category: Category, amount: UsdBudget,
        provider_account_ref: str, max_output_tokens: int, at: datetime,
        deadline: datetime,
    ) -> tuple["BudgetBook", Refusal | None]:  # fmt: skip
        at, deadline = ensure_aware_utc(at), ensure_aware_utc(deadline)
        check_id(reservation_id, "reservation_id")
        check_id(provider_account_ref, "provider_account_ref")
        if type(amount) is not UsdBudget:
            raise TypeError("amount must be UsdBudget")
        if amount.value <= 0 or not isinstance(category, Category) or deadline <= at:
            raise BudgetError("reservation", "positive amount, category, deadline")
        if type(max_output_tokens) is not int or max_output_tokens < 1:
            raise BudgetError("max_output_tokens", "a positive int")
        new = Reservation(reservation_id, self.scope_id, self.period, category,
            amount, provider_account_ref, max_output_tokens, at, deadline)  # fmt: skip
        if (old := self.find(reservation_id)) is not None:
            if old.status is not Status.RESERVED or old.dispatched:
                return self, Refusal("reservation_closed", reservation_id)
            terms = (category, amount, provider_account_ref, max_output_tokens)
            if (old.category, old.reserved, old.provider_account_ref,
                    old.max_output_tokens) == terms:  # fmt: skip
                return self, None  # idempotent retry keeps the original record
            return self, Refusal("duplicate_reservation", reservation_id)
        if self.caps.authorization_ref is None:
            return self, Refusal("spending_not_authorized")
        if period_of(at) != self.period:
            return self, Refusal("wrong_period", period_of(at))
        if self.summary().available < amount:
            return self, Refusal("inference_cap", self.summary().available.to_wire())
        if category is Category.IMPROVEMENT and (
            self.summary(category).available < amount
        ):
            return self, Refusal("improvement_cap")
        return self._with(new), None

    def dispatched(self, reservation_id: str, at: datetime) -> "BudgetBook":
        """Mark sent; refused at or after the deadline or once spending is no
        longer authorized."""
        at, r = ensure_aware_utc(at), _open(self.get(reservation_id), Status.RESERVED)
        if self.caps.authorization_ref is None:
            raise BudgetError("spending_not_authorized")
        if at >= r.deadline:
            raise BudgetError("deadline_passed", r.reservation_id)
        return self if r.dispatched else self._with(replace(r, dispatched=True))

    def release(self, reservation_id: str) -> "BudgetBook":
        """Only for a reservation never dispatched: a sent call may still bill."""
        r = _open(self.get(reservation_id), Status.RESERVED)
        if r.dispatched:
            raise BudgetError("dispatched", "settle or expire a dispatched call")
        return self._with(replace(r, status=Status.RELEASED))

    def settle(
        self, reservation_id: str, actual: UsdBudget, at: datetime
    ) -> "BudgetBook":
        """Record a provider usage report: reserved and dispatched, or expired.
        The charge stays in this book's month even when `at` is in a later one."""
        at = ensure_aware_utc(at)
        if type(actual) is not UsdBudget or actual.value < 0:
            raise BudgetError("actual", "a non-negative UsdBudget")
        r = _open(self.get(reservation_id), Status.RESERVED, Status.EXPIRED)
        if not r.dispatched:
            raise BudgetError("not_dispatched", r.reservation_id)
        if at < r.created_at:
            raise BudgetError("report_before_reservation", r.reservation_id)
        return self._with(replace(r, status=Status.SETTLED, actual=actual))

    def expire_due(self, at: datetime) -> "BudgetBook":
        """Charge every dispatched, unreconciled reservation past its deadline at
        the full reservation; release undispatched ones past it."""
        at, book = ensure_aware_utc(at), self
        for r in self.reservations:
            if r.status is Status.RESERVED and at >= r.deadline:
                book = book._with(
                    replace(r, status=Status.EXPIRED, actual=r.reserved)
                    if r.dispatched else replace(r, status=Status.RELEASED)
                )  # fmt: skip
        return book


def apply(current: BudgetBook, new: BudgetBook, expected_version: int) -> BudgetBook:
    """Compare-and-set: install `new`, computed from version `expected_version`,
    only if `current` is still that version. The persistence layer does the same
    in one transaction (`UPDATE ... WHERE version = expected`)."""
    if current.version != expected_version or new.version not in (
        expected_version,
        expected_version + 1,
    ):
        raise BudgetError("stale_version", f"{current.version} != {expected_version}")
    return new
