"""Model gateway admission, fallbacks, metering, output checks and provenance (T037).

SYNTHETIC routes, feeds, tenants, credentials and prices only. No real provider or
model route exists or is approved here (T006 has not run). The adapter is an
in-memory fake; nothing leaves the process. Expected costs are hand-computed.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from qw_domain import rights as rg
from qw_domain.budget import BudgetBook, BudgetCaps, BudgetError, Category, Status
from qw_domain.decimals import UsdBudget as Usd
from qw_domain.gateway import (
    Admission,
    Bounds,
    DataClass,
    Denial,
    Dispatched,
    DispatchTicket,
    FallbackPermission,
    GatewayContext,
    GatewayError,
    Kind,
    ModelRequest,
    Outcome,
    OutputSchema,
    ProviderResponse,
    Qualification,
    Rates,
    Reason,
    Refused,
    Route,
    UsageReport,
    admit,
    begin,
    dispatch,
    isolation,
    meter,
)
from qw_domain.instants import InstantError

AT = datetime(2026, 10, 9, 15, tzinfo=UTC)
TENANT, FEED, ACCT = "tenant-synth-a", "feed-synth-eod", "provider-acct-synth"
CAPS = BudgetCaps(Usd("100"), Usd("20"), "owner-auth-synth-1")
PERMIT = rg.Evidence(rg.EvidenceKind.PERMISSION, "perm", AT - timedelta(1), "counsel")
GRANT = rg.Grant(rg.RightState.GRANTED, PERMIT, AT + timedelta(days=365))
BOUNDS = Bounds(8000, 2000, timedelta(seconds=30), 2)
RATES = Rates(Usd("3"), Usd("15"), "rate-card-synth-1")  # USD per million tokens
SCHEMA = OutputSchema("schema-synth-1", (("summary", Kind.TEXT),
    ("target_weight", Kind.DECIMAL), ("confident", Kind.BOOLEAN)))  # fmt: skip
VALID = '{"summary": "SYNTHETIC answer", "target_weight": "0.25", "confident": true}'


def registry(model_processing: bool = True) -> rg.Registry:
    full: dict[rg.Use, rg.Grant] = {u: GRANT for u in rg.Use if u != rg.Use.RETENTION}
    uses = dict(full)
    if not model_processing:  # the tenant's entitlement denies model processing
        uses[rg.Use.MODEL_PROCESSING] = rg.Grant(rg.RightState.DENIED, PERMIT)
    juris, scope = {"CA-ON": GRANT}, {rg.UseScope.PERSONAL: GRANT}
    used, early = frozenset({rg.Use.MODEL_PROCESSING}), AT - timedelta(hours=36)
    f = rg.FeedHistory.new(FEED, "provider-synth").revise(
        "synthetic_eod", used, rg.RightsProfile(full, scope, juris), early
    )
    auth = rg.Evidence(rg.EvidenceKind.AUTH, "auth-synth", early)
    work = rg.Evidence(rg.EvidenceKind.WORKLOAD, "work-synth", early)
    f = f.transition(rg.FeedStatus.CONNECTED, "op", (auth,), early)
    f = f.transition(rg.FeedStatus.QUALIFIED, "op", (work, PERMIT), AT - timedelta(1))
    e = rg.EntitlementHistory.new(TENANT, FEED).revise(
        rg.EntitlementSource.INSTALLATION_LICENSE, True,
        rg.RightsProfile(uses, scope, juris), None, early,
    )  # fmt: skip
    reg = rg.Registry().with_provider(rg.Provider("provider-synth", "SYNTHETIC"))
    return reg.with_feed(f).with_entitlement(e)


def ctx(reg: rg.Registry | None = None, consent: str | None = "consent-synth") -> (
    GatewayContext
):  # fmt: skip
    scope, grace = rg.UseScope.PERSONAL, timedelta(hours=1)
    return GatewayContext(reg or registry(), scope, "CA-ON", consent, grace)


def route(rid: str = "route-a", **kw: object) -> Route:
    base = Route(rid, "family-1", "model-ref-synth", "runtime-synth-1", ACCT,
        "cred-ref-synth", frozenset(Category),
        DataClass.TENANT_FINANCIAL, True, True, True, frozenset({"calculator"}),
        BOUNDS, RATES, Qualification.QUALIFIED, "qual-evidence-synth")  # fmt: skip
    return replace(base, **kw)  # type: ignore[arg-type]


def request(**kw: object) -> ModelRequest:
    base = ModelRequest("req-1", TENANT, Category.RESEARCH,
        DataClass.TENANT_FINANCIAL, frozenset({FEED}), 4000, 1000,
        timedelta(seconds=20), 2, frozenset({"calculator"}), SCHEMA,
        "SYNTHETIC prompt about holdings")  # fmt: skip
    return replace(base, **kw)  # type: ignore[arg-type]


def book(caps: BudgetCaps = CAPS) -> BudgetBook:
    return BudgetBook.open(TENANT, AT, caps)


def admitted(*chain: Route, **kw: object) -> Admission:
    a = admit(request(), chain, ctx(), book(), AT, **kw)  # type: ignore[arg-type]
    assert isinstance(a, Admission), a
    return a


def refused(req: ModelRequest, *chain: Route, **kw: object) -> set[str]:
    c, b = kw.pop("context", ctx()), kw.pop("budget", book())
    a = admit(req, chain, c, b, AT, **kw)  # type: ignore[arg-type]
    assert isinstance(a, Refused), a
    denials = [d for _, ds in a.tried for d in ds] + list(a.denials)
    return {f"{d.reason}:{d.detail}" if d.detail else d.reason for d in denials}


def permit(*routes: str) -> FallbackPermission:
    return FallbackPermission("user-fallback-ok-synth", frozenset(routes))


def test_worst_case_is_reserved_before_dispatch() -> None:
    a = admitted(route())
    # 2 attempts x (4000 x 3 + 1000 x 15) / 1e6 = 2 x 0.027 = USD0.054
    r = a.book.get("req-1")
    assert r.reserved == Usd("0.054") and r.status is Status.RESERVED
    assert not r.dispatched and r.deadline == AT + timedelta(seconds=40, hours=1)
    assert (a.route_id, a.skipped, a.fallback_ref) == ("route-a", (), None)
    half = Rates(Usd("0.5"), Usd("0"), "rate-half")  # 1 x 0.5 / 1e6 = 0.0000005
    assert half.cost(1, 1, 0) == Usd("0.000001")  # rounded up, never down
    assert half.cost(1, 3, 0) == Usd("0.000002")  # 0.0000015 up, not half-even


def test_the_default_is_fail_closed() -> None:
    assert admit(request(), (), ctx(), book(), AT) == Refused(
        (Denial(Reason.NO_ROUTE),), ()
    )
    r = route()  # a route is unqualified and unpriced unless stated
    bare = Route(r.route_id, r.family_id, r.model_ref, r.runtime_version, ACCT,
        r.credential_ref, r.purposes, r.max_data, True, True, True, r.allowed_tools,
        BOUNDS)  # fmt: skip
    assert (bare.qualification, bare.qualification_ref, bare.rates) == (
        Qualification.UNQUALIFIED, None, None)  # fmt: skip
    out = admit(request(), (bare,), ctx(), book(), AT)
    assert isinstance(out, Refused) and out.denials == (
        Denial(Reason.NO_QUALIFIED_ROUTE),
    )
    assert out.tried == (("route-a", (Denial(Reason.NOT_QUALIFIED),
                                      Denial(Reason.RATE_UNKNOWN))),)  # fmt: skip


def test_unqualified_or_unpriced_routes_block() -> None:
    for q in (Qualification.UNQUALIFIED, Qualification.SUSPENDED):
        assert "route_not_qualified" in refused(request(), route(qualification=q))
    assert "rate_unknown" in refused(request(), route(rates=None))
    with pytest.raises(GatewayError):
        route(qualification_ref=None)  # qualified needs evidence
    with pytest.raises(GatewayError):
        Rates(Usd("0"), Usd("0"), "rate-free")  # never implies an unmetered API


def test_rights_consent_and_route_terms_block_tenant_data() -> None:
    no_rights = ctx(registry(model_processing=False))
    assert "rights:feed-synth-eod:right_denied" in refused(
        request(), route(), context=no_rights
    )
    other = request(feeds=frozenset({FEED, "feed-synth-other"}))
    assert "rights:feed-synth-other:not_qualified" in refused(other, route())
    no_consent = ctx(consent=None)
    assert "ai_consent_missing" in refused(request(), route(), context=no_consent)
    weak = route(financial_use=False, training_opt_out=False,
                 retention_approved=False)  # fmt: skip
    assert {"route_financial_use", "route_training", "route_retention"} <= refused(
        request(), weak
    )
    public = request(data=DataClass.NONE, feeds=frozenset())
    assert isinstance(admit(public, (weak,), no_consent, book(), AT), Admission)
    assert "data_class" in refused(request(), route(max_data=DataClass.NONE))


def test_request_bounds_and_purposes() -> None:
    narrow = route(purposes=frozenset({Category.RESEARCH}))
    for req, code in (
        (request(input_tokens=8001), "input_tokens"),
        (request(max_output_tokens=2001), "output_tokens"),
        (request(wall=timedelta(seconds=31)), "wall_time"),
        (request(attempts=3), "attempts"),
        (request(tools=frozenset({"shell"})), "tools"),
        (request(category=Category.IMPROVEMENT), "purpose"),
    ):
        assert refused(req, narrow) == {"no_qualified_route", code}


def test_budget_refusals_are_typed() -> None:
    unauthorized = book(BudgetCaps(Usd("100"), Usd("20"), None))
    assert "budget:spending_not_authorized" in refused(
        request(), route(), budget=unauthorized
    )
    pricey = route(rates=Rates(Usd("30000"), Usd("0"), "rate-c"))
    # 2 x 4000 x 30000 / 1e6 = USD240 > USD100
    assert "budget:inference_cap" in refused(request(), pricey)


def test_fallback_needs_an_explicit_permission_naming_the_route() -> None:
    primary, backup = route(qualification=Qualification.SUSPENDED), route("route-b")
    assert "fallback_not_permitted" in refused(request(), primary, backup)
    assert "fallback_not_permitted" in refused(
        request(), primary, backup, fallback=permit("route-c")
    )
    a = admitted(primary, backup, fallback=permit("route-b"))
    assert (a.route_id, a.fallback_ref) == ("route-b", "user-fallback-ok-synth")
    assert a.skipped == (("route-a", (Denial(Reason.NOT_QUALIFIED),)),)


def test_fallback_cannot_downgrade_privacy_or_qualification() -> None:
    primary = route(qualification=Qualification.SUSPENDED)
    public = request(data=DataClass.NONE, feeds=frozenset())
    ok = permit("route-b")
    for weaker in (
        {"training_opt_out": False},
        {"retention_approved": False},
        {"financial_use": False},
        {"max_data": DataClass.NONE},
    ):  # each passes the public request's own checks, but weakens the primary's
        codes = refused(public, primary, route("route-b", **weaker), fallback=ok)
        assert "fallback_privacy_downgrade" in codes
    assert isinstance(admit(public, (primary, route("route-b")), ctx(), book(), AT,
                            fallback=ok), Admission)  # fmt: skip
    unq = route("route-b", qualification=Qualification.UNQUALIFIED)
    assert "route_not_qualified" in refused(public, primary, unq, fallback=ok)


def test_fallback_must_satisfy_the_same_bounds_rights_and_budget() -> None:
    primary, ok = route(qualification=Qualification.UNQUALIFIED), permit("route-b")
    small = route("route-b", bounds=replace(BOUNDS, max_output_tokens=500))
    assert "output_tokens" in refused(request(), primary, small, fallback=ok)
    pricey = route("route-b", rates=Rates(Usd("30000"), Usd("0"), "rate-c"))
    assert "budget:inference_cap" in refused(request(), primary, pricey, fallback=ok)
    no_rights = ctx(registry(model_processing=False))
    codes = refused(request(), primary, route("route-b"), context=no_rights,
                    fallback=ok)  # fmt: skip
    assert codes == {"rights:feed-synth-eod:right_denied"}


def usage(i: int = 1234, o: int = 567, **kw: object) -> UsageReport:
    base = UsageReport("req-1", ACCT, "auth-synth", i, o)
    return replace(base, **kw)  # type: ignore[arg-type]


class FakeAdapter:
    """SYNTHETIC in-memory provider: no network. None in `texts` raises."""

    def __init__(self, *texts: str | None, report: UsageReport | None = None) -> None:
        self.texts, self.report = list(texts), report or usage()
        self.calls, self.seen, self.deadline = 0, "", AT
        self.last: tuple[BudgetBook, DispatchTicket] | None = None  # set by `send`

    def complete(
        self, req: ModelRequest, route: Route, deadline: datetime
    ) -> ProviderResponse:
        self.calls += 1
        self.seen, self.deadline = route.credential_ref, deadline
        if (text := self.texts.pop(0)) is None:
            raise TimeoutError
        return ProviderResponse(text, self.report, "model-ref-synth-0101")


def clock(*times: datetime) -> Callable[[], datetime]:
    return iter(times or (AT, AT + timedelta(seconds=7))).__next__


def send(
    a: Admission, fake: FakeAdapter, req: ModelRequest | None = None,
    book: BudgetBook | None = None, tick: Callable[[], datetime] | None = None,
) -> Dispatched:  # fmt: skip
    req = req or request()
    b, ticket = begin(a, req, book or a.book, AT)  # installed by compare-and-set
    fake.last = (b, ticket)
    return dispatch(a, route(), req, b, ticket, fake, tick or clock())


def replay(a: Admission, fake: FakeAdapter, book: BudgetBook | None = None) -> None:
    assert fake.last is not None
    calls, (begun, ticket) = fake.calls, fake.last
    with pytest.raises(GatewayError) as e:
        dispatch(a, route(), request(), book or begun, ticket, fake, clock())
    assert e.value.code == "not_begun" and fake.calls == calls


def test_dispatch_settles_validates_and_records_provenance() -> None:
    a, fake = admitted(route()), FakeAdapter(VALID)
    d = send(a, fake)
    assert (fake.seen, fake.calls) == ("cred-ref-synth", 1)
    assert fake.deadline == AT + timedelta(seconds=20)  # sent + wall per attempt
    # 1234 x 3 / 1e6 + 567 x 15 / 1e6 = 0.003702 + 0.008505 = USD0.012207
    assert d.run.cost == d.book.get("req-1").actual == Usd("0.012207")
    assert d.book.get("req-1").status is Status.SETTLED
    assert d.output == {"summary": "SYNTHETIC answer", "target_weight": "0.25",
                        "confident": True}  # fmt: skip
    run = d.run.to_wire()
    assert run["outcome"] == "completed" and run["output_problem"] is None
    assert (run["model_requested"], run["model_returned"]) == (
        "model-ref-synth", "model-ref-synth-0101")  # fmt: skip
    assert run["output_hash"] == hashlib.sha256(VALID.encode()).hexdigest()
    assert run["prompt_hash"] == hashlib.sha256(
        b"SYNTHETIC prompt about holdings").hexdigest()  # fmt: skip
    assert (run["dispatched_at"], run["finished_at"], run["cost"]) == (
        "2026-10-09T15:00:00Z", "2026-10-09T15:00:07Z", "0.012207")  # fmt: skip
    logged = json.dumps(run) + repr(d.run) + repr(d.book)
    assert all(t not in logged for t in ("holdings", "answer"))
    with pytest.raises(GatewayError) as e:
        send(a, fake, request(attempts=1))
    assert e.value.code == "admission_mismatch"


def test_each_send_consumes_exactly_one_begun_attempt() -> None:
    a, fake, req = admitted(route()), FakeAdapter(None, None), request()
    with pytest.raises(GatewayError) as e:  # only `begin` issues tickets
        DispatchTicket("req-1", 1, a.book.version + 1)
    assert e.value.code == "forged_ticket"
    b1, t1 = begin(a, req, a.book, AT)
    with pytest.raises(BudgetError) as be:  # one send in flight at a time
        begin(a, req, b1, AT)
    assert be.value.code == "send_in_flight"
    other = replace(b1, version=b1.version + 1)  # not the book `begin` produced
    with pytest.raises(GatewayError):
        dispatch(a, route(), req, other, t1, fake, clock())
    first = dispatch(a, route(), req, b1, t1, fake, clock())
    assert first.run.outcome is Outcome.UNKNOWN and fake.calls == 1
    fake.last = (b1, t1)
    replay(a, fake)  # a used ticket
    replay(a, fake, first.book)  # a returned attempt
    second = send(a, fake, book=first.book)  # the retry: attempt 2 of 2
    assert second.run.outcome is Outcome.UNKNOWN and fake.calls == 2
    with pytest.raises(BudgetError) as be:
        begin(a, req, second.book, AT)
    assert be.value.code == "attempts_exhausted"
    again = admit(req, (route(),), ctx(), second.book, AT)  # a retried admission
    assert again == Refused((Denial(Reason.NO_QUALIFIED_ROUTE),),
        (("route-a", (Denial(Reason.BUDGET, "reservation_closed"),)),))  # fmt: skip


def test_no_send_after_settlement_or_past_the_deadline() -> None:
    a, fake = admitted(route()), FakeAdapter(VALID)
    done = send(a, fake)
    replay(a, fake, done.book)
    with pytest.raises(BudgetError):
        begin(a, request(), done.book, AT)
    late, deadline = FakeAdapter(VALID), a.book.get("req-1").deadline
    with pytest.raises(GatewayError) as e:
        send(a, late, tick=clock(deadline))
    assert e.value.code == "deadline_passed"
    assert (fake.calls, late.calls) == (1, 0)


def test_a_settlement_refused_by_the_book_keeps_the_reservation() -> None:
    d = send(admitted(route()), FakeAdapter(VALID),
             tick=clock(AT, AT - timedelta(seconds=1)))  # fmt: skip
    assert (d.run.outcome, d.run.output_problem, d.output) == (
        Outcome.UNKNOWN, "report_before_reservation", None)  # fmt: skip
    assert d.book.get("req-1").status is Status.RESERVED


def test_usage_above_the_request_bounds_is_billed_and_flagged() -> None:
    d = send(admitted(route()), FakeAdapter(VALID, report=usage(9000)))
    # 9000 x 3 / 1e6 + 567 x 15 / 1e6 = 0.027 + 0.008505 = USD0.035505
    assert d.book.get("req-1").actual == d.run.cost == Usd("0.035505")
    assert d.run.output_problem == "usage_over_bounds"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("Sure! Here is the JSON", "not_json"),
        ('["SYNTHETIC"]', "not_object"),
        (VALID.replace('"0.25"', "0.25"), "float_number"),
        (VALID.replace('"0.25"', "NaN"), "float_number"),
        (VALID.replace('"0.25"', '"1e3"'), "type:target_weight"),
        (VALID.replace("true", '"yes"'), "type:confident"),
        (VALID.replace("}", ', "extra": 1}'), "unexpected:extra"),
        (VALID.replace(', "confident": true', ""), "missing:confident"),
        (VALID.replace("}", ', "confident": false}'), "duplicate_key"),
    ],
)
def test_invalid_output_is_billed_but_never_used(text: str, problem: str) -> None:
    d = send(admitted(route()), FakeAdapter(text))
    assert d.output is None and d.run.outcome is Outcome.OUTPUT_INVALID
    assert d.run.output_problem == problem
    assert d.book.get("req-1").actual == Usd("0.012207")


def test_timeout_keeps_the_reservation_and_late_reports_need_authentication() -> None:
    a = admitted(route())
    d = send(a, FakeAdapter(None))
    assert d.output is None and d.run.outcome is Outcome.UNKNOWN
    assert (d.run.cost, d.run.output_hash) == (None, None)
    assert d.book.get("req-1").status is Status.RESERVED
    late = d.book.expire_due(d.book.get("req-1").deadline)
    assert late.get("req-1").actual == Usd("0.054")  # worst case
    for bad, code in (
        (usage(auth_ref=None), "reporter_not_authenticated"),
        (usage(reporter_ref="provider-acct-other"), "reporter_mismatch"),
        (usage(reservation_id="req-2"), "reporter_mismatch"),
    ):
        with pytest.raises(GatewayError) as e:
            meter(a, route(), late, bad, AT + timedelta(hours=2))
        assert e.value.code == code
    with pytest.raises(GatewayError):
        meter(a, route("route-b"), late, usage(), AT + timedelta(hours=2))
    fixed, cost = meter(a, route(), late, usage(), AT + timedelta(hours=2))
    assert cost == fixed.get("req-1").actual == Usd("0.012207")


def test_an_unauthenticated_report_in_the_response_keeps_the_reservation() -> None:
    a, fake = admitted(route()), FakeAdapter(VALID, report=usage(auth_ref=None))
    d = send(a, fake)
    assert d.output is None and d.run.outcome is Outcome.UNKNOWN
    assert d.run.output_problem == "reporter_not_authenticated"
    r = d.book.get("req-1")
    assert (r.status, r.dispatched, r.actual) == (Status.RESERVED, True, None)
    replay(a, fake, d.book)  # a repeat is not a new attempt
    assert fake.calls == 1


def test_a_retry_after_an_unknown_outcome_keeps_its_worst_case() -> None:
    a, fake = admitted(route()), FakeAdapter(None, VALID)
    first = send(a, fake)
    second = send(a, fake, book=first.book)
    # reported 0.012207 + attempt 1 at its worst case (4000 x 3 + 1000 x 15) / 1e6
    # = 0.027, so 0.039207 of the 0.054 reserved
    assert second.book.get("req-1").actual == Usd("0.039207")
    assert second.run.cost == Usd("0.012207")  # this call's own reported cost


def test_prompt_content_never_appears_in_records() -> None:
    req = request()
    texts = repr(req) + json.dumps(req.to_wire()) + repr(admitted(route()))
    assert "holdings" not in texts and req.prompt_hash in texts
    with pytest.raises(GatewayError):
        route(credential_ref="sk-SYNTHETIC with spaces")  # an opaque id only


def test_isolation_is_reported_accurately() -> None:
    assert isolation(route(), route("route-b")) == "same_model_separate_context"
    other = route("route-b", family_id="family-2")
    assert isolation(route(), other) == "cross_model"


def test_malformed_inputs_rejected() -> None:
    with pytest.raises(InstantError):
        admit(request(), (route(),), ctx(), book(), datetime(2026, 10, 9))  # noqa: DTZ001
    with pytest.raises(GatewayError):
        admit(request(tenant_id="tenant-synth-b"), (route(),), ctx(), book(), AT)
    with pytest.raises(TypeError):
        Rates(1.5, Usd("1"), "r")  # type: ignore[arg-type]
    for fields in ((), (("a", Kind.TEXT), ("a", Kind.TEXT)), (("a", "text"),)):
        with pytest.raises(GatewayError):
            OutputSchema("schema-x", fields)  # type: ignore[arg-type]
