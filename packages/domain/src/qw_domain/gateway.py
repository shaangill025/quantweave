"""Model gateway decision (T037 increment 2; spec §10, §12, §15; R018, R025, R027,
R074, R085, R095). Domain only: no adapter, network or persistence.
- Fail closed: a route is unqualified and unpriced unless it carries qualification
  evidence and authorized rates. T006 has not run, so no route is qualified; an
  empty chain is `no_route`.
- Admission: `model_processing` rights for every feed and tenant AI consent, then per
  route qualification, rates, purpose, data class, financial-use, training and
  retention terms, tools and request bounds. It then reserves the worst case,
  attempts x (input x input rate + output x output rate) / 1e6 rounded up to the
  micro-dollar, in the `BudgetBook` before dispatch.
- Fallback needs an explicit user permission naming the route. A fallback passes the
  same checks and may not weaken the primary route's privacy terms.
- `begin` starts one attempt (at most `attempts`, one in flight), installed by
  compare-and-set; its single-use ticket allows one adapter call before the
  attempt's deadline. Settlement needs a usage report authenticated as
  the admitted route's provider account. A timeout, adapter error or rejected
  report keeps the reservation open until a report or its deadline.
- Output is used only when it matches the request's declared structure. Every
  dispatched call yields a content-free `ModelRun` (data dictionary `model_run`).
"""

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import NoReturn, Protocol

from qw_domain.budget import BudgetBook, BudgetError, Category, check_id
from qw_domain.decimals import DOMAIN_CONTEXT, Rounding, UsdBudget, quantize
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.rights import Registry, Use, UseScope, check_use

MICRO = Decimal("0.000001")


class GatewayError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _id(value: object, what: str) -> str:
    try:
        return check_id(value, what)
    except ValueError as exc:
        raise GatewayError("malformed", str(exc)) from None


def _count(value: object, what: str, least: int = 1) -> None:
    if type(value) is not int or value < least:
        raise GatewayError("malformed", f"{what} must be an int >= {least}")


def _wall(value: object) -> None:
    if type(value) is not timedelta or value <= timedelta(0):
        raise GatewayError("malformed", "wall time must be a positive timedelta")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Reason(StrEnum):
    NO_ROUTE = "no_route"
    NO_QUALIFIED_ROUTE = "no_qualified_route"
    AI_CONSENT = "ai_consent_missing"
    RIGHTS = "rights"  # detail: "<feed id>:<rights deny code>"
    NOT_QUALIFIED = "route_not_qualified"
    RATE_UNKNOWN = "rate_unknown"
    PURPOSE = "purpose"
    DATA_CLASS = "data_class"
    FINANCIAL_USE = "route_financial_use"
    TRAINING = "route_training"
    RETENTION = "route_retention"
    TOOLS = "tools"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    WALL_TIME = "wall_time"
    ATTEMPTS = "attempts"
    FALLBACK_NOT_PERMITTED = "fallback_not_permitted"
    FALLBACK_DOWNGRADE = "fallback_privacy_downgrade"
    BUDGET = "budget"  # detail: the budget book's refusal code


@dataclass(frozen=True, slots=True)
class Denial:
    reason: Reason
    detail: str = ""


class DataClass(StrEnum):  # ordered: a route allows up to its `max_data`
    NONE = "none"  # no tenant data: public or synthetic content only
    TENANT_FINANCIAL = "tenant_financial"


_RANK = {DataClass.NONE: 0, DataClass.TENANT_FINANCIAL: 1}


class Qualification(StrEnum):
    UNQUALIFIED = "unqualified"
    QUALIFIED = "qualified"
    SUSPENDED = "suspended"


@dataclass(frozen=True, slots=True)
class Rates:  # USD per million tokens, from an authorized rate record
    input_per_mtok: UsdBudget
    output_per_mtok: UsdBudget
    rate_ref: str

    def __post_init__(self) -> None:
        for r in (self.input_per_mtok, self.output_per_mtok):
            if type(r) is not UsdBudget:
                raise TypeError("rates are UsdBudget")
            if r.value < 0:
                raise GatewayError("rates", "rates are non-negative")
        if self.input_per_mtok.value == self.output_per_mtok.value == 0:
            raise GatewayError("rates", "a metered route is never priced at zero")
        _id(self.rate_ref, "rate_ref")

    def cost(self, attempts: int, tokens_in: int, tokens_out: int) -> UsdBudget:
        """attempts x (in x input rate + out x output rate) / 1e6, rounded up."""
        a, i, o = Decimal(attempts), Decimal(tokens_in), Decimal(tokens_out)
        with localcontext(DOMAIN_CONTEXT):
            per = i * self.input_per_mtok.value + o * self.output_per_mtok.value
            exact = (a * per).scaleb(-6)
        return quantize(UsdBudget, exact, quantum=MICRO, rounding=Rounding.COST)


@dataclass(frozen=True, slots=True)
class Bounds:
    max_input_tokens: int
    max_output_tokens: int
    max_wall: timedelta
    max_attempts: int  # billed attempts including retries

    def __post_init__(self) -> None:
        for name in ("max_input_tokens", "max_output_tokens", "max_attempts"):
            _count(getattr(self, name), name)
        _wall(self.max_wall)


class Kind(StrEnum):
    TEXT = "text"
    DECIMAL = "decimal_string"  # money and quantities are decimal strings
    INTEGER = "integer"
    BOOLEAN = "boolean"


_DECIMAL = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?", re.ASCII)
_KINDS: dict[Kind, Callable[[object], bool]] = {
    Kind.TEXT: lambda v: isinstance(v, str),
    Kind.DECIMAL: lambda v: isinstance(v, str) and bool(_DECIMAL.fullmatch(v)),
    Kind.INTEGER: lambda v: type(v) is int,
    Kind.BOOLEAN: lambda v: type(v) is bool,
}


class _Rejected(ValueError):
    pass


def _no_float(text: str) -> NoReturn:
    raise _Rejected("float_number")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    if len({k for k, _ in pairs}) != len(pairs):
        raise _Rejected("duplicate_key")
    return dict(pairs)


@dataclass(frozen=True, slots=True)
class OutputSchema:
    """Declared output structure: a JSON object with exactly these fields."""

    schema_id: str
    fields: tuple[tuple[str, Kind], ...]

    def __post_init__(self) -> None:
        _id(self.schema_id, "schema_id")
        names = [n for n, _ in self.fields]
        if not names or len(set(names)) != len(names):
            raise GatewayError("schema", "fields are non-empty and unique")
        for name, kind in self.fields:
            _id(name, "field")
            if not isinstance(kind, Kind):
                raise GatewayError("schema", "field kinds are Kind")

    def check(self, text: str) -> tuple[dict[str, object] | None, str | None]:
        """(document, None) when valid, else (None, the first problem)."""
        try:
            doc = json.loads(text, parse_float=_no_float, parse_constant=_no_float,
                             object_pairs_hook=_pairs)  # fmt: skip
        except _Rejected as exc:
            return None, str(exc)
        except ValueError:
            return None, "not_json"
        if not isinstance(doc, dict):
            return None, "not_object"
        if extra := sorted(set(doc) - {n for n, _ in self.fields}):
            return None, f"unexpected:{extra[0]}"
        for name, kind in self.fields:
            if name not in doc:
                return None, f"missing:{name}"
            if not _KINDS[kind](doc[name]):
                return None, f"type:{name}"
        return doc, None


@dataclass(frozen=True, slots=True)
class ModelRequest:
    request_id: str  # also the reservation id: a retry never resets it
    tenant_id: str
    category: Category
    data: DataClass
    feeds: frozenset[str]  # every feed whose content the prompt carries
    input_tokens: int  # upper bound of the prompt
    max_output_tokens: int
    wall: timedelta  # per attempt
    attempts: int
    tools: frozenset[str]
    output: OutputSchema
    prompt: str = field(repr=False)

    def __post_init__(self) -> None:
        for v, what in ((self.request_id, "request_id"), (self.tenant_id, "tenant"),
                        *((f, "feed_id") for f in self.feeds)):  # fmt: skip
            _id(v, what)
        if not (isinstance(self.category, Category)
                and isinstance(self.data, DataClass)
                and isinstance(self.output, OutputSchema)
                and isinstance(self.prompt, str)):  # fmt: skip
            raise GatewayError("malformed", "category, data, output and prompt")
        for name in ("input_tokens", "max_output_tokens", "attempts"):
            _count(getattr(self, name), name)
        _wall(self.wall)

    @property
    def prompt_hash(self) -> str:
        return _sha256(self.prompt)

    def to_wire(self) -> dict[str, object]:
        """Content-free input manifest: the prompt appears only as its SHA-256."""
        d = asdict(self) | {"feeds": sorted(self.feeds), "tools": sorted(self.tools)}
        del d["prompt"], d["wall"]
        return d | {
            "wall_us": self.wall // timedelta(microseconds=1),
            "prompt_sha256": self.prompt_hash,
        }

    @property
    def request_hash(self) -> str:
        return _sha256(json.dumps(self.to_wire(), sort_keys=True,
                                  separators=(",", ":")))  # fmt: skip


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    family_id: str  # for evaluator isolation reporting (R025)
    model_ref: str  # the exact model identifier requested (opaque)
    runtime_version: str
    provider_account_ref: str
    credential_ref: str  # opaque: the adapter resolves it, never the domain
    purposes: frozenset[Category]
    max_data: DataClass
    financial_use: bool  # provider terms permit financial-advice use (R095)
    training_opt_out: bool  # tenant content excluded from provider training
    retention_approved: bool  # provider retention terms approved for tenant data
    allowed_tools: frozenset[str]
    bounds: Bounds
    rates: Rates | None = None  # None: no authorized rate information
    qualification: Qualification = Qualification.UNQUALIFIED
    qualification_ref: str | None = None

    def __post_init__(self) -> None:
        for name in ("route_id", "family_id", "model_ref", "runtime_version",
                     "provider_account_ref", "credential_ref"):  # fmt: skip
            _id(getattr(self, name), name)
        if self.qualification is Qualification.QUALIFIED:
            _id(self.qualification_ref, "qualification_ref")

    def denials(self, req: ModelRequest) -> list[Denial]:
        b, tenant_data = self.bounds, req.data is DataClass.TENANT_FINANCIAL
        checks = (
            (self.qualification is not Qualification.QUALIFIED, Reason.NOT_QUALIFIED),
            (self.rates is None, Reason.RATE_UNKNOWN),
            (req.category not in self.purposes, Reason.PURPOSE),
            (_RANK[req.data] > _RANK[self.max_data], Reason.DATA_CLASS),
            (tenant_data and not self.financial_use, Reason.FINANCIAL_USE),
            (tenant_data and not self.training_opt_out, Reason.TRAINING),
            (tenant_data and not self.retention_approved, Reason.RETENTION),
            (not req.tools <= self.allowed_tools, Reason.TOOLS),
            (req.input_tokens > b.max_input_tokens, Reason.INPUT_TOKENS),
            (req.max_output_tokens > b.max_output_tokens, Reason.OUTPUT_TOKENS),
            (req.wall > b.max_wall, Reason.WALL_TIME),
            (req.attempts > b.max_attempts, Reason.ATTEMPTS),
        )
        return [Denial(code) for failed, code in checks if failed]

    def weaker_than(self, primary: "Route") -> bool:
        """True when this route's privacy terms are weaker than `primary`'s."""
        return (
            _RANK[self.max_data] < _RANK[primary.max_data]
            or (primary.financial_use and not self.financial_use)
            or (primary.training_opt_out and not self.training_opt_out)
            or (primary.retention_approved and not self.retention_approved)
        )


@dataclass(frozen=True, slots=True)
class FallbackPermission:  # the user's explicit permission (§10)
    ref: str
    routes: frozenset[str]  # the fallback routes the user permitted

    def __post_init__(self) -> None:
        for v in (self.ref, *self.routes):
            _id(v, "fallback_permission")


@dataclass(frozen=True, slots=True)
class GatewayContext:
    registry: Registry
    scope: UseScope
    jurisdiction: str
    ai_consent_ref: str | None  # the tenant's AI-processing consent
    reconcile_grace: timedelta  # how long to wait for a usage report

    def __post_init__(self) -> None:
        _wall(self.reconcile_grace)


Tried = tuple[tuple[str, tuple[Denial, ...]], ...]  # (route id, denials) per route


@dataclass(frozen=True, slots=True)
class Admission:
    route_id: str
    reservation_id: str
    request_hash: str
    book: BudgetBook  # with the reservation; install by compare-and-set
    skipped: Tried  # routes passed over before this one
    fallback_ref: str | None
    admitted_at: datetime
    per_attempt: UsdBudget  # worst case of one attempt


@dataclass(frozen=True, slots=True)
class Refused:
    denials: tuple[Denial, ...]
    tried: Tried


def _rights(req: ModelRequest, ctx: GatewayContext, at: datetime) -> list[Denial]:
    out = []
    if req.data is DataClass.TENANT_FINANCIAL and ctx.ai_consent_ref is None:
        out.append(Denial(Reason.AI_CONSENT))
    for feed in sorted(req.feeds):
        d = check_use(ctx.registry, req.tenant_id, feed, Use.MODEL_PROCESSING, at,
                      scope=ctx.scope, jurisdiction=ctx.jurisdiction)  # fmt: skip
        codes = sorted({r.code.value for r in d.reasons})
        out += [Denial(Reason.RIGHTS, f"{feed}:{c}") for c in codes]
    return out


def admit(
    req: ModelRequest, chain: Sequence[Route], ctx: GatewayContext,
    book: BudgetBook, at: datetime, *, fallback: FallbackPermission | None = None,
) -> Admission | Refused:  # fmt: skip
    """Admit on the first route in `chain` that passes every check, reserving its
    worst case. A route after the first needs `fallback` naming it."""
    at = ensure_aware_utc(at)
    if req.tenant_id != book.scope_id:
        raise GatewayError("scope", "the budget book belongs to another scope")
    if not chain:
        return Refused((Denial(Reason.NO_ROUTE),), ())
    if denied := _rights(req, ctx, at):  # route-independent: never routed around
        return Refused(tuple(denied), ())
    tried: list[tuple[str, tuple[Denial, ...]]] = []
    for i, route in enumerate(chain):
        if i and fallback is None:
            return Refused((Denial(Reason.FALLBACK_NOT_PERMITTED),), tuple(tried))
        denials = route.denials(req)
        if i and fallback is not None and route.route_id not in fallback.routes:
            denials.append(Denial(Reason.FALLBACK_NOT_PERMITTED))
        if i and route.weaker_than(chain[0]):
            denials.append(Denial(Reason.FALLBACK_DOWNGRADE))
        if not denials and route.rates is not None:
            cost = route.rates.cost(req.attempts, req.input_tokens,
                                    req.max_output_tokens)  # fmt: skip
            deadline = at + req.wall * req.attempts + ctx.reconcile_grace
            new, why = book.reserve(req.request_id, req.category, cost,
                route.provider_account_ref, req.max_output_tokens, at,
                deadline)  # fmt: skip
            if why is None:
                ref = fallback.ref if i and fallback is not None else None
                one = route.rates.cost(1, req.input_tokens, req.max_output_tokens)
                return Admission(route.route_id, req.request_id, req.request_hash,
                                 new, tuple(tried), ref, at, one)  # fmt: skip
            denials = [Denial(Reason.BUDGET, why.code)]
        tried.append((route.route_id, tuple(denials)))
    return Refused((Denial(Reason.NO_QUALIFIED_ROUTE),), tuple(tried))


@dataclass(frozen=True, slots=True)
class UsageReport:  # provider-reported usage for one call
    reservation_id: str
    reporter_ref: str  # the provider account the report came from
    auth_ref: str | None  # how the adapter authenticated the report; None: not
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        _id(self.reservation_id, "reservation_id")
        _id(self.reporter_ref, "reporter_ref")
        _count(self.input_tokens, "input_tokens", 0)
        _count(self.output_tokens, "output_tokens", 0)


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    text: str = field(repr=False)
    usage: UsageReport
    model_returned: str | None  # as reported by the provider


class ProviderAdapter(Protocol):
    def complete(
        self, req: ModelRequest, route: Route, deadline: datetime
    ) -> ProviderResponse: ...  # gives up at `deadline` (sent + wall, capped)


def meter(
    a: Admission, route: Route, book: BudgetBook, usage: UsageReport, at: datetime
) -> tuple[BudgetBook, UsdBudget]:
    """Settle the admitted reservation from an authenticated usage report (cost
    rounded up to the micro-dollar), returning that cost. Every other begun
    attempt has an unknown outcome and is charged at `per_attempt` (§10). A late
    report after expiry replaces the worst-case charge, so it must come from the
    admitted route's account."""
    if route.route_id != a.route_id or route.rates is None:
        raise GatewayError("admission_mismatch", "meter on the admitted route")
    if usage.auth_ref is None:
        raise GatewayError("reporter_not_authenticated", a.reservation_id)
    if (usage.reporter_ref, usage.reservation_id) != (
        route.provider_account_ref, a.reservation_id,
    ):  # fmt: skip
        raise GatewayError("reporter_mismatch", a.reservation_id)
    cost = route.rates.cost(1, usage.input_tokens, usage.output_tokens)
    others = max(book.get(a.reservation_id).sends - 1, 0)
    with localcontext(DOMAIN_CONTEXT):
        charge = UsdBudget(cost.value + others * a.per_attempt.value)
    return book.settle(a.reservation_id, charge, ensure_aware_utc(at)), cost


class Outcome(StrEnum):
    COMPLETED = "completed"
    OUTPUT_INVALID = "output_invalid"  # billed, but the output is not used
    UNKNOWN = "unknown"  # timeout or adapter error: reservation kept open


@dataclass(frozen=True, slots=True)
class ModelRun:  # data dictionary `model_run`: no prompt, response or credential
    run_id: str
    tenant_id: str
    route_id: str
    provider_account_ref: str
    model_requested: str
    model_returned: str | None
    runtime_version: str
    prompt_hash: str
    request_hash: str  # hash of the content-free input manifest
    output_hash: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost: UsdBudget | None
    fallback_ref: str | None
    dispatched_at: datetime
    finished_at: datetime
    outcome: Outcome
    output_problem: str | None

    def to_wire(self) -> dict[str, object]:
        return asdict(self) | {
            "cost": None if self.cost is None else self.cost.to_wire(),
            "dispatched_at": format_instant(self.dispatched_at),
            "finished_at": format_instant(self.finished_at),
            "outcome": self.outcome.value,
        }


@dataclass(frozen=True, slots=True)
class Dispatched:
    book: BudgetBook
    run: ModelRun
    output: dict[str, object] | None  # only a structurally valid output


_ISSUER = object()


@dataclass(slots=True, eq=False)
class DispatchTicket:  # one begun attempt on one book version; issued by `begin`
    reservation_id: str
    attempt: int
    version: int
    issuer: object = field(default=None, repr=False)
    used: bool = False

    def __post_init__(self) -> None:
        if self.issuer is not _ISSUER:
            raise GatewayError("forged_ticket", self.reservation_id)


def begin(
    a: Admission, req: ModelRequest, book: BudgetBook, at: datetime
) -> tuple[BudgetBook, DispatchTicket]:
    """Begin one attempt (at most `req.attempts`, one in flight at a time).
    PRECONDITION: the caller installs the returned book by compare-and-set
    (`budget.apply`) before `dispatch`; only then is each attempt begun once."""
    if req.request_hash != a.request_hash:
        raise GatewayError("admission_mismatch", "request differs")
    new = book.begin_send(a.reservation_id, at, req.attempts)
    n = new.get(a.reservation_id).sends
    return new, DispatchTicket(a.reservation_id, n, new.version, _ISSUER)


def dispatch(
    a: Admission, route: Route, req: ModelRequest, book: BudgetBook,
    ticket: DispatchTicket, adapter: ProviderAdapter, clock: Callable[[], datetime],
) -> Dispatched:  # fmt: skip
    """Make the one call `ticket` allows, then meter, validate and record
    provenance. Refused before the call unless the ticket is unused and `book` is
    the version `begin` produced, or at or past the deadline. An adapter error or
    a refused settlement returns the attempt and keeps the reservation.
    PRECONDITION: at most once across processes needs persistence to hold the
    begun attempt as an exclusive claim (row lock or lease) across the call; the
    domain cannot detect a replayed post-begin book."""
    if req.request_hash != a.request_hash or route.route_id != a.route_id:
        raise GatewayError("admission_mismatch", "request or route differs")
    r = book.get(a.reservation_id)
    if ticket.used or (ticket.reservation_id, ticket.version) != (
        r.reservation_id, book.version,
    ):  # only the book `begin` returned has this version  # fmt: skip
        raise GatewayError("not_begun", a.reservation_id)
    ticket.used = True
    sent = ensure_aware_utc(clock())
    if sent >= r.deadline:  # the begun attempt stays open; `expire_due` charges it
        raise GatewayError("deadline_passed", a.reservation_id)
    run = ModelRun(a.reservation_id, req.tenant_id, route.route_id,
        route.provider_account_ref, route.model_ref, None, route.runtime_version,
        req.prompt_hash, a.request_hash, None, None, None, None, a.fallback_ref,
        sent, sent, Outcome.UNKNOWN, None)  # fmt: skip
    returned = book.end_send(a.reservation_id)
    try:
        response = adapter.complete(req, route, min(sent + req.wall, r.deadline))
    except Exception:  # outcome unknown: the provider may still bill
        done = ensure_aware_utc(clock())
        return Dispatched(returned, replace(run, finished_at=done), None)
    done, used = ensure_aware_utc(clock()), response.usage
    try:
        settled, cost = meter(a, route, book, used, done)
    except (GatewayError, BudgetError) as exc:
        return Dispatched(returned, replace(run, finished_at=done,
                                            output_problem=exc.code), None)  # fmt: skip
    doc, problem = req.output.check(response.text)
    over = (used.input_tokens > req.input_tokens
            or used.output_tokens > req.max_output_tokens)  # fmt: skip
    run = replace(run, model_returned=response.model_returned,
        output_hash=_sha256(response.text), input_tokens=used.input_tokens,
        output_tokens=used.output_tokens, cost=cost, finished_at=done,
        outcome=Outcome.COMPLETED if doc is not None else Outcome.OUTPUT_INVALID,
        output_problem=problem or ("usage_over_bounds" if over else None))  # fmt: skip
    return Dispatched(settled, run, doc)


def isolation(proposer: Route, evaluator: Route) -> str:
    """How a decision evaluator is separated from its proposer (R025)."""
    if proposer.family_id == evaluator.family_id:
        return "same_model_separate_context"
    return "cross_model"
