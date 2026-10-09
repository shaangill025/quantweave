"""Planning commitments: joint feasibility of published proposals (T026).

SYNTHETIC inputs only. Base case as in test_proposals.py: qualified available cash
5000 CAD, buy 80 at an ask of 50 with no costs (4000), generous adopted policy
(reserve 1, ratio limits 1, allocation 100000). Expected numbers are worked by hand
in the comments or come from numerical_oracles.json (NUM06).
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import permutations
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.commitments import (
    CommitmentBook,
    plan_committed,
    publish_committed,
    sync,
)
from qw_domain.decimals import Money, PositiveQuantity, Price, Quantity, Ratio
from qw_domain.identity import InstrumentId
from qw_domain.onboarding import load_catalogue
from qw_domain.policy import (
    AccountFacts,
    AdoptionConsent,
    OptionPermission,
    PolicyHistory,
    build_draft,
)
from qw_domain.proposals import (
    TRANSITIONS,
    Binding,
    Code,
    Current,
    ExecutionState,
    Observation,
    Proposal,
    ProposalError,
    ProposalState,
    ProposalVersion,
    cash_requirement,
    dismiss,
    policy_mode,
    revise,
    step,
)
from qw_domain.risk import ProposedAction, RiskCode, RiskInputs, Side, evaluate
from qw_domain.risk_pauses import PauseBook
from qw_domain.valuation import Mark, MarkKind

SPEC = Path(__file__).resolve().parents[3] / "docs/spec"
AT = datetime(2026, 10, 9, 15, tzinfo=UTC)
T = timedelta
TENANT = "tenant-synth"
IID, OTHER = InstrumentId(UUID(int=1)), InstrumentId(UUID(int=2))
S = ProposalState


def cad(x: str) -> Money:
    return Money.of(x, "CAD")


def lim(metric: str, value: str, unit: str = "ratio") -> dict[str, str]:
    return {
        "metric": metric, "value": value, "unit": unit, "denominator": "account_nav",
        "window": "per_trade", "account_id": "acct-1", "currency": "CAD",
    }  # fmt: skip


LIMITS = {
    "cash_reserve": lim("cash_reserve", "1", "amount"),
    "issuer_concentration": lim("issuer_concentration", "1"),
    "sector_concentration": lim("sector_concentration", "1"),
    "planned_trade_loss": lim("planned_trade_loss", "100000", "amount"),
    "stress_loss": lim("stress_loss", "1"),
    "loss_pause": lim("loss_pause", "0.10"),
}
ANSWERS: dict[str, Any] = {
    "ONB01": "selected_accounts", "ONB03": "CAD", "ONB04": ["growth"],
    "ONB05": "gt_7y", "ONB06": "none_known", "ONB07": "financially_manageable",
    "ONB10": "weekly", "ONB11": ["stocks", "etfs"], "ONB12": ["long_term"],
    "ONB13": [{"account_id": "acct-1", "currency": "CAD", "amount": "100000"}],
    "ONB16": "rules_only",
}  # fmt: skip


def adopt(**limits: str) -> PolicyHistory:
    chosen = {**LIMITS, **{k: lim(k, v) for k, v in limits.items()}}
    raw = (SPEC / "config/onboarding_questions.json").read_text()
    answers = {**ANSWERS, "ONB14": list(chosen.values())}
    responses = load_catalogue(json.loads(raw)).validate(answers)
    facts = (AccountFacts("acct-1", OptionPermission.GRANTED, False),)
    draft = build_draft(TENANT, responses, facts, as_of=date(2026, 10, 9))
    consent = AdoptionConsent("user-1", "stepup-1", AT, frozenset(), "")
    return PolicyHistory.new("pol-1", TENANT).propose(draft).adopt(1, consent)


H = adopt()
MARK = Mark(IID, Price("50"), "CAD", MarkKind.ASK, AT - T(seconds=1), "feed")
INPUTS = RiskInputs(
    as_of=AT, market_max_age=T(minutes=15), fx_max_age=T(hours=1),
    account_max_age=T(seconds=300), account_observed_at=AT - T(seconds=60),
    reconciled=True, available_cash=cad("5000"), allocation_used=cad("0"),
    marks={IID: MARK}, fx={}, denominators={"account_nav": cad("20000")},
    held_units={IID: Quantity(0)}, position_values={OTHER: cad("14000")},
    issuer_exposure={"issuer-x": cad("500")}, sector_exposure={"sector-y": cad("2000")},
    stress_shocks={IID: Ratio("-0.20"), OTHER: Ratio("-0.05")},
)  # fmt: skip
SELL_INPUTS = replace(INPUTS, held_units={IID: Quantity(100)})
BUY = ProposedAction(
    tenant_id=TENANT, account_id="acct-1", currency="CAD", sleeve_id=None,
    strategy_id="strat-1", denominator="account_nav", instrument=IID,
    issuer_id="issuer-x", sector_id="sector-y", horizon="long_term", side=Side.BUY,
    quantity=PositiveQuantity(80), lot=PositiveQuantity(1), stop=Price(48),
    fixed_cost=cad("0"), unit_cost=cad("0"), leveraged_or_inverse=False,
)  # fmt: skip
NO_PAUSES = PauseBook(TENANT)
REVS = {"acct-1": 7}
OBS = (Observation("mark:1", MARK.observed_at, T(minutes=15)),)
BOOK = CommitmentBook(TENANT, "acct-1", "CAD", cad("5000"))


def cur(
    at: datetime = AT, history: PolicyHistory = H, inputs: RiskInputs = INPUTS
) -> Current:
    return Current(at, history, inputs, REVS, NO_PAUSES)


def ver(
    pid: str = "prop-a", qty: int = 80, n: int = 1, history: PolicyHistory = H,
    inputs: RiskInputs = INPUTS, **kw: Any,
) -> ProposalVersion:  # fmt: skip
    action = replace(BUY, quantity=PositiveQuantity(qty), **kw.pop("action", {}))
    ev = evaluate(history, action, inputs, NO_PAUSES)
    fields: dict[str, Any] = {
        "trigger_at": AT - T(seconds=5), "received_at": AT - T(seconds=4),
        "created_at": AT, "expires_at": AT + T(minutes=30),
        "mode": policy_mode(history), "alternatives_group_id": None, **kw,
    }  # fmt: skip
    req = cash_requirement(action, MARK.price)
    return ProposalVersion(pid, n, action, Binding.of(ev, REVS, OBS), req, **fields)


def ready(v: ProposalVersion, c: Current) -> Proposal:
    p = Proposal.new(v, c)
    for dst in (S.VERIFYING, S.READY_FOR_FINAL_CHECK):
        p = step(p, dst, c).proposal
    return p


def pub(
    v: ProposalVersion, book: CommitmentBook, c: Current | None = None
) -> tuple[Proposal, CommitmentBook]:
    c = c or cur()
    d, book, _ = publish_committed(ready(v, c), v.version, c, book)
    assert d.accepted, d.reasons
    return d.proposal, book


def codes(reasons: Any) -> set[Code]:
    return {r.code for r in reasons}


@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")])
def test_num06_two_independent_4000_buys_cannot_both_publish(
    order: tuple[str, str],
) -> None:
    raw = (SPEC / "tests/fixtures/numerical_oracles.json").read_text()
    num06 = next(o for o in json.loads(raw)["oracles"] if o["id"] == "NUM06")
    assert num06["inputs"] == {"available": "5000", "proposals": ["4000", "4000"]}
    first, second = (ver(f"prop-{k}") for k in order)
    _, book = pub(first, BOOK)  # each is individually feasible
    d, after, ev = publish_committed(ready(second, cur()), 1, cur(), book)
    assert num06["expected"]["jointly_feasible"] is False and not d.accepted
    assert d.proposal.state(1) is S.READY_FOR_FINAL_CHECK and after == book
    assert codes(d.reasons) == {Code.CAPITAL_CONFLICT, Code.RISK_BLOCKED}
    # Risk re-run with 4000 deducted: 1000 - 50q >= reserve 1 -> q <= 19.98 -> 19.
    assert ev is not None and ev.alternative == PositiveQuantity(19)
    assert book.reserved() == cad("4000") and len(book.commitments) == 1


def test_alternatives_reserve_the_maximum_and_a_choice_is_final() -> None:
    a = ver("prop-a", alternatives_group_id="grp-1")
    b = ver("prop-b", alternatives_group_id="grp-1")
    pa, book = pub(a, BOOK)
    pb, book = pub(b, book)  # alternatives: max(4000, 4000), not the sum
    assert book.reserved() == cad("4000") and len(book.commitments) == 2
    c = ver("prop-c", qty=30)  # 4000 + 1500 > 5000
    over, same, _ = publish_committed(ready(c, cur()), 1, cur(), book)
    assert not over.accepted and same == book
    with pytest.raises(ProposalError, match="siblings_missing"):
        plan_committed(pa, 1, cur(), book)
    d, book = plan_committed(pa, 1, cur(), book, {"prop-b": pb})
    assert d.accepted and d.siblings["prop-b"].state(1) is S.INVALIDATED
    assert [(x.proposal_id, x.planned) for x in book.commitments] == [("prop-a", True)]
    # Case C: a late sibling cannot join a group whose choice is made.
    late, same, _ = publish_committed(
        ready(ver("prop-d", qty=10, alternatives_group_id="grp-1"), cur()),
        1,
        cur(),
        book,
    )
    assert not late.accepted and codes(late.reasons) == {Code.ALTERNATIVE_SELECTED}
    assert same == book


def test_at049_unexecuted_sales_create_no_cash() -> None:
    c = cur(inputs=SELL_INPUTS)
    s = ver("prop-s", qty=60, inputs=SELL_INPUTS, action={"side": Side.SELL})
    assert s.requirement == cad("0")
    _, book = pub(s, BOOK, c)
    assert book.reserved() == cad("0") and book.available == cad("5000")
    _, book = pub(ver("prop-a", inputs=SELL_INPUTS), book, c)
    b = ver("prop-b", inputs=SELL_INPUTS)
    d, _, _ = publish_committed(ready(b, c), 1, c, book)
    assert not d.accepted and Code.CAPITAL_CONFLICT in codes(d.reasons)
    s2 = ver("prop-s2", qty=60, inputs=SELL_INPUTS, action={"side": Side.SELL})
    d2, _, ev = publish_committed(ready(s2, c), 1, c, book)  # 60 + 60 > 100 held
    assert not d2.accepted and ev is not None
    assert RiskCode.SHORT_NOT_PERMITTED in {r.code for r in ev.reasons}


@pytest.mark.parametrize(
    ("limits", "code"),
    [
        # issuer: 0.15 x 20000 = 3000; 500 + 2000 = 2500 alone, 4500 together
        ({"issuer_concentration": "0.15"}, RiskCode.CONCENTRATION_LIMIT),
        # stress: 0.06 x 20000 = 1200; 700 + 0.2 x 2000 = 1100 alone,
        # 700 + 0.2 x 4000 = 1500 together
        ({"stress_loss": "0.06"}, RiskCode.STRESS_LIMIT),
    ],
)
def test_joint_feasibility_covers_concentration_and_stress(
    limits: dict[str, str], code: RiskCode
) -> None:
    h = adopt(**limits)
    c = cur(history=h)
    _, book = pub(ver("prop-a", qty=40, history=h), BOOK, c)
    b = ver("prop-b", qty=40, history=h)
    d, _, ev = publish_committed(ready(b, c), 1, c, book)
    assert not d.accepted and codes(d.reasons) == {Code.RISK_BLOCKED}
    assert ev is not None and code in {r.code for r in ev.reasons}
    assert book.reserved() == cad("2000")  # cash alone would have admitted both


HEDGE = Mark(OTHER, Price("50"), "CAD", MarkKind.BID, AT - T(seconds=1), "feed")
HEDGED = replace(
    INPUTS, marks={IID: MARK, OTHER: HEDGE},
    held_units={IID: Quantity(0), OTHER: Quantity(280)},
    stress_shocks={IID: Ratio("-0.20"), OTHER: Ratio("0.05")},
)  # fmt: skip


@pytest.mark.parametrize("sale_first", [True, False])
def test_a_pending_hedge_sale_is_never_counted_on(sale_first: bool) -> None:
    # Stress limit 0.01 x 20000 = 200. OTHER: 280 x 50 = 14000 with a +5% shock.
    # Buy 80 at 50 alone: loss 0.2 x 4000 - 0.05 x 14000 = 100 <= 200, passes.
    # Selling all 280 alone removes only a gain: passes. Both: 800 > 200.
    h = adopt(stress_loss="0.01")
    c = cur(history=h, inputs=HEDGED)
    hedge = {"side": Side.SELL, "instrument": OTHER, "stop": None}
    sale = ver("prop-s", qty=280, history=h, inputs=HEDGED, action=hedge)
    buy = ver("prop-b", history=h, inputs=HEDGED)
    first, second = (sale, buy) if sale_first else (buy, sale)
    _, book = pub(first, BOOK, c)
    d, after, ev = publish_committed(ready(second, c), 1, c, book)
    assert not d.accepted and codes(d.reasons) == {Code.RISK_BLOCKED} and after == book
    assert ev is not None and RiskCode.STRESS_LIMIT in {r.code for r in ev.reasons}
    assert d.reasons[0].detail.startswith("stress_worst_case:") is sale_first


HID = InstrumentId(UUID(int=3))
MIXED = replace(
    INPUTS,
    marks={IID: MARK, OTHER: HEDGE, HID: replace(HEDGE, instrument_id=HID)},
    held_units={IID: Quantity(0), OTHER: Quantity(280), HID: Quantity(140)},
    position_values={OTHER: cad("14000"), HID: cad("7000")},
    stress_shocks={IID: Ratio("-0.20"), OTHER: Ratio("-0.05"), HID: Ratio("0.10")},
)  # fmt: skip


@pytest.mark.parametrize("order", list(permutations(("sale-n", "sale-h", "buy"))))
def test_only_hedge_sales_are_executed_for_the_worst_stress(
    order: tuple[str, ...],
) -> None:
    # Limit 0.045 x 20000 = 900. Losses: buy 0.2 x 4000 = 800, OTHER 0.05 x 14000
    # = 700, HID gain 0.10 x 7000 = 700. Buy alone: 800 + 700 - 700 = 800, passes.
    # Only the hedge HID sold: 1500 blocks; only OTHER sold: 100; both: 800. So the
    # buy and the hedge sale never both publish, whatever the order.
    h = adopt(stress_loss="0.045")
    c = cur(history=h, inputs=MIXED)
    sell = {"side": Side.SELL, "stop": None}
    vs = {
        "sale-n": ver("sale-n", 280, history=h, inputs=MIXED,
                      action={**sell, "instrument": OTHER}),
        "sale-h": ver("sale-h", 140, history=h, inputs=MIXED,
                      action={**sell, "instrument": HID}),
        "buy": ver("buy", history=h, inputs=MIXED),
    }  # fmt: skip
    book = BOOK
    for k in order:
        _, book, _ = publish_committed(ready(vs[k], c), 1, c, book)
    admitted = {x.proposal_id for x in book.commitments}
    first = next(k for k in order if k != "sale-n")
    assert admitted == {"sale-n", first}


HEDGE_BUY = replace(
    INPUTS,
    marks={IID: MARK, HID: replace(MARK, instrument_id=HID)},
    held_units={IID: Quantity(0), OTHER: Quantity(280), HID: Quantity(0)},
    stress_shocks={IID: Ratio("-0.20"), OTHER: Ratio("-0.05"), HID: Ratio("0.10")},
    issuer_exposure={"issuer-x": cad("500"), "issuer-h": cad("0")},
    sector_exposure={"sector-y": cad("2000"), "sector-h": cad("0")},
)  # fmt: skip


@pytest.mark.parametrize("order", list(permutations(("hedge", "b", "c"))))
def test_a_pending_hedge_buy_is_never_counted_on(order: tuple[str, ...]) -> None:
    # Limit 0.05 x 20000 = 1000. OTHER loses 700. B and C (20 x 50 each at -20%)
    # lose 200 each; the hedge buy (40 x 50 at +10%) gains 200. If the hedge never
    # fills, B and C together lose 1100, so they never both publish.
    h = adopt(stress_loss="0.05")
    c = cur(history=h, inputs=HEDGE_BUY)
    into_h = {"instrument": HID, "issuer_id": "issuer-h", "sector_id": "sector-h"}
    vs = {
        "hedge": ver("hedge", 40, history=h, inputs=HEDGE_BUY, action=into_h),
        "b": ver("b", 20, history=h, inputs=HEDGE_BUY),
        "c": ver("c", 20, history=h, inputs=HEDGE_BUY),
    }
    book = BOOK
    for k in order:
        _, book, _ = publish_committed(ready(vs[k], c), 1, c, book)
    first = next(k for k in order if k != "hedge")
    assert {x.proposal_id for x in book.commitments} == {"hedge", first}


def test_a_sold_instrument_without_a_shock_fails_closed() -> None:
    h = adopt(stress_loss="0.045")
    c = cur(history=h, inputs=MIXED)
    hedge = {"side": Side.SELL, "stop": None, "instrument": HID}
    _, book = pub(ver("sale-h", 140, history=h, inputs=MIXED, action=hedge), BOOK, c)
    shocks = {IID: Ratio("-0.20"), OTHER: Ratio("-0.05")}  # HID's shock now missing
    gap = replace(MIXED, stress_shocks=shocks)
    ev = evaluate(h, BUY, gap, NO_PAUSES)  # HID stays in the state: blocks
    assert RiskCode.STRESS_SHOCK_MISSING in {r.code for r in ev.reasons}
    c2 = cur(history=h, inputs=gap)
    d, after, _ = publish_committed(
        ready(ver("buy", history=h, inputs=gap), c2), 1, c2, book
    )
    assert not d.accepted and d.proposal.state(1) is S.REJECTED and after == book


def test_a_partial_hedge_sale_without_a_mark_removes_the_listed_share() -> None:
    # Limit 0.0525 x 20000 = 1050. HID's mark is gone and its listed value rose to
    # 8400 (60 a unit); the sale of 70 was admitted at 50. Removing 70 x 50 = 3500
    # leaves a gain of 490 (loss 800 + 700 - 490 = 1010, passes); the listed share
    # 8400 x 70 / 140 = 4200 leaves 420 (loss 1080), which blocks.
    h = adopt(stress_loss="0.0525")
    hedge = {"side": Side.SELL, "stop": None, "instrument": HID}
    c = cur(history=h, inputs=MIXED)
    _, book = pub(ver("sale-h", 70, history=h, inputs=MIXED, action=hedge), BOOK, c)
    values = {OTHER: cad("14000"), HID: cad("8400")}
    later = replace(MIXED, marks={IID: MARK, OTHER: HEDGE}, position_values=values)
    c2 = cur(history=h, inputs=later)
    d, _, ev = publish_committed(
        ready(ver("buy", history=h, inputs=later), c2), 1, c2, book
    )
    assert not d.accepted and ev is not None
    assert RiskCode.STRESS_LIMIT in {r.code for r in ev.reasons}


def test_reservation_uses_the_current_price_when_higher() -> None:
    up = replace(INPUTS, marks={IID: replace(MARK, price=Price("54"))})
    _, book = pub(ver(), BOOK, cur(inputs=up))
    assert book.reserved() == cad("4320")  # case E: 80 x 54, not the bound 4000
    down = replace(INPUTS, marks={IID: replace(MARK, price=Price("49"))})
    _, book = pub(ver(), BOOK, cur(inputs=down))
    assert book.reserved() == cad("4000")  # never below the bound requirement


def test_book_and_inputs_must_agree_on_cash() -> None:
    other = cur(inputs=replace(INPUTS, available_cash=cad("6000")))
    v = ver()
    d, _, _ = publish_committed(ready(v, cur()), 1, other, BOOK)
    assert not d.accepted and codes(d.reasons) >= {Code.CASH_UNCONFIRMED}
    with pytest.raises(ProposalError, match="book"):
        publish_committed(ready(v, cur()), 1, cur(), replace(BOOK, account_id="acct-2"))


def test_closing_paths_release_the_commitment() -> None:
    # Case A: revise an active version, then the new one expires through step().
    p, book = pub(ver(), BOOK)
    p = revise(p, ver(n=2, expires_at=AT + T(minutes=10)), cur()).proposal
    assert sync(book, p).reserved() == cad("0")  # superseded releases
    d = step(p, S.VERIFYING, cur(AT + T(minutes=20)))
    assert d.proposal.state(2) is S.EXPIRED and sync(book, d.proposal).commitments == ()
    # Case B: the revised version is rejected at publish; nothing stays reserved.
    p, book = pub(ver(), BOOK)
    blocked = ver(n=2, qty=1000)  # 50000 > allocation headroom and cash: BLOCK
    assert blocked.binding.risk_outcome.value == "block"
    p = revise(p, replace(blocked, requirement=cad("0")), cur()).proposal
    for dst in (S.VERIFYING, S.READY_FOR_FINAL_CHECK):
        p = step(p, dst, cur()).proposal
    r, book, _ = publish_committed(p, 2, cur(), book)  # caller never synced
    assert r.proposal.state(2) is S.REJECTED and book.commitments == ()
    p, book = pub(ver(), BOOK)
    gone = dismiss(p, 1, cur())
    assert sync(book, gone.proposal).commitments == ()


def test_groups_stay_in_one_book() -> None:
    a = ver("prop-a", alternatives_group_id="grp-1")
    away = ver("prop-b", alternatives_group_id="grp-1", action={"account_id": "a2"})
    pa, book = pub(a, BOOK)
    pb = Proposal.new(away, cur())
    with pytest.raises(ProposalError, match="group_scope"):  # case D
        plan_committed(pa, 1, cur(), book, {"prop-b": pb})
    d, book = plan_committed(pa, 1, cur(), book)  # no sibling in this book
    assert d.accepted and d.proposal.execution(1) is ExecutionState.PLANNED


OPS = st.lists(
    st.tuples(
        st.integers(0, 2),
        st.sampled_from(["ready", "publish", "plan", "dismiss", "revise", "late"]),
    ),
    max_size=14,
)


@settings(max_examples=60, deadline=None, derandomize=True, database=None)
@given(OPS, st.lists(st.integers(1, 100), min_size=3, max_size=3))
def test_every_commitment_belongs_to_a_live_latest_version(
    ops: list[tuple[int, str]], sizes: list[int]
) -> None:
    groups = ["grp-1", "grp-1", None]
    ps = [
        Proposal.new(ver(f"prop-{i}", qty=q, alternatives_group_id=g), cur())
        for i, (q, g) in enumerate(zip(sizes, groups, strict=True))
    ]
    book, at = BOOK, AT
    for i, op in ops:
        at += T(minutes=40) if op == "late" else T(seconds=1)
        c, p = cur(at), ps[i]
        n = p.latest.version
        sibs = {
            f"prop-{j}": ps[j] for j in range(3)
            if j != i and groups[j] and groups[j] == groups[i]
        }  # fmt: skip
        if op in ("ready", "late"):
            for dst in (S.VERIFYING, S.READY_FOR_FINAL_CHECK):
                if dst in TRANSITIONS[p.state(n)]:
                    p = step(p, dst, c).proposal
        elif op == "publish":
            d, book, _ = publish_committed(p, n, c, book)
            p = d.proposal
        elif op == "plan":
            d, book = plan_committed(p, n, c, book, sibs)
            p = d.proposal
            for k, s in d.siblings.items():
                ps[int(k.split("-")[1])] = s
        elif op == "dismiss":
            p = dismiss(p, n, c).proposal
        else:
            q, g = sizes[i], groups[i]
            nxt = ver(f"prop-{i}", qty=q, n=n + 1, alternatives_group_id=g)
            p = revise(p, nxt, c).proposal
        ps[i] = p
        book = sync(book, *ps)
        latest = {x.latest.proposal_id: x for x in ps}
        for cm in book.commitments:
            owner = latest[cm.proposal_id]
            assert cm.version == owner.latest.version
            assert owner.state(cm.version) is S.ACTIVE
        assert book.reserved() <= book.available
        planned = [cm for cm in book.commitments if cm.planned and cm.group_id]
        assert len({cm.group_id for cm in planned}) == len(planned)
