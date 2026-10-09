"""Scoped risk pauses with explicit, append-only resumption (T020; spec §5 "Drawdown
and loss pauses", §7 "Scoped loss pauses"; R069; T008 F-13).

- A pause covers a tenant (all of its actions), an account, a sleeve or a strategy.
  While active it blocks new risk in that scope; factual monitoring and independently
  checked reductions continue (`qw_domain.risk`). It is not a broker instruction and
  never liquidates anything.
- Nothing clears a pause except an explicit `Resume`: no expiry, no midnight reset,
  and deposits cannot erase a breach. A resume names the principal, a reason, a
  step-up receipt and an aware UTC time. A material pause, or one caused by stale or
  unreconciled data, also needs a reconciled account revision id. Only the ids' form
  is checked here; verifying them against T010/T017 records is remaining work.
- `PauseBook.events` only grows; state is derived from it. Every construction,
  including a direct one, replays and validates the whole chain.
- Drawdown uses a unitised series (value per unit), so external cash flows neither
  create nor erase a loss: D = 1 - V/H.
Stdlib only.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from fractions import Fraction

from qw_domain.decimals import DOMAIN_CONTEXT, Price, Ratio
from qw_domain.instants import ensure_aware_utc
from qw_domain.sources import ID_PATTERN


class PauseError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class ScopeKind(StrEnum):
    TENANT = "tenant"  # every action of the book's tenant ("all")
    ACCOUNT = "account"
    SLEEVE = "sleeve"
    STRATEGY = "strategy"


class PauseTrigger(StrEnum):
    DRAWDOWN = "drawdown_breach"
    PLANNED_LOSS = "planned_loss_breach"
    LOSS_LIMIT = "loss_limit_breach"
    STALE = "stale_data"
    RECONCILIATION = "reconciliation_flag"
    USER = "user_pause"


_NEEDS_RECONCILED = {PauseTrigger.STALE, PauseTrigger.RECONCILIATION}


def _id(value: object, code: str) -> None:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise PauseError(code, "a well-formed id is required")


@dataclass(frozen=True, slots=True)
class PauseScope:
    kind: ScopeKind
    scope_id: str

    def __post_init__(self) -> None:
        if type(self.kind) is not ScopeKind:
            raise TypeError("kind must be a ScopeKind")
        _id(self.scope_id, "scope_id")


@dataclass(frozen=True, slots=True)
class Pause:
    pause_id: str
    scope: PauseScope
    trigger: PauseTrigger
    triggered_at: datetime
    evidence_ref: str  # breach receipt, stale-data or reconciliation flag id
    material: bool

    def __post_init__(self) -> None:
        _id(self.pause_id, "pause_id")
        _id(self.evidence_ref, "evidence_ref")
        object.__setattr__(self, "triggered_at", ensure_aware_utc(self.triggered_at))


@dataclass(frozen=True, slots=True)
class Resume:
    pause_id: str
    principal_id: str
    reason: str
    step_up_receipt_id: str
    resumed_at: datetime
    reconciled_revision: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "resumed_at", ensure_aware_utc(self.resumed_at))


@dataclass(frozen=True, slots=True)
class PauseBook:
    tenant_id: str
    events: tuple[Pause | Resume, ...] = ()

    def __post_init__(self) -> None:
        """Replay the whole chain, so a directly built book is checked too."""
        _id(self.tenant_id, "tenant_id")
        paused: dict[str, Pause] = {}
        done: set[str] = set()
        for e in self.events:
            if type(e) is Pause:
                if e.pause_id in paused:
                    raise PauseError("duplicate", e.pause_id)
                if e.scope.kind is ScopeKind.TENANT and e.scope.scope_id != (
                    self.tenant_id
                ):
                    raise PauseError("tenant_scope", "names another tenant")
                paused[e.pause_id] = e
            elif type(e) is Resume:
                target = paused.get(e.pause_id)
                if target is None or e.pause_id in done:
                    raise PauseError("not_active", "no active pause with that id")
                _check_resume(target, e)
                done.add(e.pause_id)
            else:
                raise TypeError("events are Pause or Resume")

    def active(self) -> tuple[Pause, ...]:
        done = {e.pause_id for e in self.events if isinstance(e, Resume)}
        return tuple(
            e for e in self.events if isinstance(e, Pause) and e.pause_id not in done
        )

    def pause(self, p: Pause) -> "PauseBook":
        return PauseBook(self.tenant_id, (*self.events, p))

    def resume(self, r: Resume) -> "PauseBook":
        return PauseBook(self.tenant_id, (*self.events, r))

    def blocking(
        self, tenant_id: str, account_id: str, sleeve_id: str | None, strategy_id: str
    ) -> tuple[Pause, ...]:
        """Active pauses whose scope covers an action with these identifiers."""
        keys = {
            (ScopeKind.TENANT, tenant_id),
            (ScopeKind.ACCOUNT, account_id),
            (ScopeKind.STRATEGY, strategy_id),
        }
        if sleeve_id is not None:
            keys.add((ScopeKind.SLEEVE, sleeve_id))
        return tuple(
            p for p in self.active() if (p.scope.kind, p.scope.scope_id) in keys
        )


def _check_resume(target: Pause, r: Resume) -> None:
    _id(r.principal_id, "principal")
    _id(r.step_up_receipt_id, "step_up")
    if not isinstance(r.reason, str) or not r.reason.strip():
        raise PauseError("reason", "a resume reason is required")
    if r.resumed_at < target.triggered_at:
        raise PauseError("before_trigger", "resume precedes the trigger")
    if target.material or target.trigger in _NEEDS_RECONCILED:
        # Only the id's form is checked here; matching it to a fresh reconciled
        # account revision (T017) and the step-up id to T010 records is caller work.
        _id(r.reconciled_revision, "reconciled_state_required")  # None fails


def unitized_drawdown(high_water: Price, value: Price) -> Ratio:
    """D = 1 - V/H on value per unit, rounded up (toward the larger loss)."""
    if type(high_water) is not Price or type(value) is not Price:
        raise TypeError("unit values must be Price")
    if high_water.value <= 0 or value.value < 0:
        raise ValueError("unit values must be positive")
    if value.value > high_water.value:
        raise ValueError("value is above the high-water mark")
    exact = 1 - Fraction(Fraction(value.value), Fraction(high_water.value))
    return _ratio_up(exact)


def _ratio_up(exact: Fraction) -> Ratio:
    scaled = -((-exact.numerator * 10**18) // exact.denominator)  # ceiling
    with localcontext(DOMAIN_CONTEXT):
        return Ratio(Decimal(scaled).scaleb(-18))


def drawdown_breached(drawdown: Ratio, limit: Ratio) -> bool:
    return drawdown.value >= limit.value
