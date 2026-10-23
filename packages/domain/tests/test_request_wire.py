"""Owner decisions 43 and 67: the caller-supplied trial ledger and budget book are
stored in full with an assessment request, so both need a strict, round-tripping
wire form (decimal strings, UTC `Z` instants, canonical input only: anything
that does not re-serialize to the same wire is refused, never normalized). A
ledger read back replays every entry (forged grants fail) and keeps every
checkpoint. SYNTHETIC fixtures only (test_trial_ledger, test_budget)."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from qw_domain.budget import (
    BudgetBook,
    BudgetCaps,
    BudgetError,
    Category,
    Reservation,
)
from qw_domain.decimals import UsdBudget as Usd
from qw_domain.research_data import DatasetManifest, ResearchError, Window
from qw_domain.trial_ledger import Outcome, Role, Split, TrialLedger
from test_budget import ACCT, AT, CAPS, book, reserve
from test_research_data import iid
from test_trial_ledger import DEV, PROMO, TRAIN, ask, ledger, plan, trial


def full_ledger() -> TrialLedger:
    """A plan, granted and refused accesses (one naming instruments), a bound
    trial, a failed trial that never reached data, and a granted promotion read."""
    lg = ledger(plan(embargo=timedelta(days=29, microseconds=7)))
    lg, _ = ask(lg, 1, Split.TRAIN, TRAIN, instruments=frozenset({iid(1), iid(2)}))
    lg = lg.record(trial(1, Split.TRAIN, metrics={
        "net_return": Decimal("0.0125"), "zero": Decimal("0E-8"),
        "hit": Decimal("1.50")}))  # fmt: skip
    lg, _ = ask(lg, 2, Split.PROMOTION, PROMO)  # refused: assessor_only
    lg = lg.record(trial(3, Split.DEVELOPMENT, Outcome.FAILED, access_id=None))
    lg, _ = ask(lg, 4, Split.DEVELOPMENT, DEV)
    lg, _ = ask(lg, 5, Split.PROMOTION, PROMO, role=Role.ASSESSOR)
    return lg


def through_json(w: object) -> Any:
    return json.loads(json.dumps(w))


def test_ledger_round_trip_keeps_entries_checkpoints_and_decimals() -> None:
    lg = full_ledger()
    w: Any = lg.to_wire()
    back = TrialLedger.from_wire(through_json(w))
    assert back == lg and back.to_wire() == w
    n = len(lg.entries)
    assert n == 7 and [back.checkpoint(k) for k in range(1, n + 1)] == [
        lg.checkpoint(k) for k in range(1, n + 1)]  # fmt: skip
    kinds = [e[0] for e in w["entries"]]
    assert kinds == ["plan", "access", "trial", "access", "trial", "access", "access"]
    p = w["entries"][0][1]
    assert p["embargo_us"] == "2505600000007" and p["registered_at"].endswith("Z")
    assert p["dataset"] == lg.plan("plan-synth-1").dataset.to_wire()
    t = back.trials()[0]
    assert t.metrics_wire() == {"hit": "1.50", "net_return": "0.0125", "zero": "0E-8"}
    assert type(t.metrics["hit"]) is Decimal
    a = back.access("acc-5")
    assert (a.granted, a.seq, a.at.tzinfo) == (True, 1, lg.access("acc-5").at.tzinfo)
    assert back.access("acc-2").reasons == ("assessor_only",)
    assert back.access("acc-1").instruments == frozenset({iid(1), iid(2)})
    assert back.trial_count("fam-momentum") == lg.trial_count("fam-momentum")


def test_manifest_and_window_round_trip() -> None:
    m = ledger().plan("plan-synth-1").dataset
    assert DatasetManifest.from_wire(through_json(m.to_wire())) == m
    assert Window.from_wire(TRAIN.to_wire()) == TRAIN
    for bad in (["2020-01-01T00:00:00+00:00", "2022-01-01T00:00:00Z"],
                ["2020-01-01T00:00:00Z"], "2020", [1, 2]):  # fmt: skip
        with pytest.raises(ResearchError):
            Window.from_wire(bad)


def _edit(w: Any, path: tuple[Any, ...], value: object) -> Any:
    w = through_json(w)
    target = w
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return w


LEDGER_BAD = [
    (("tenant_id",), 7),
    (("extra",), "x"),
    (("entries", 0, 0), "plan2"),
    (("entries", 0, 1, "embargo_us"), 2505600000007),  # a JSON number
    (("entries", 0, 1, "embargo_us"), "02505600000007"),
    (("entries", 0, 1, "registered_at"), "2025-01-09T00:00:00+00:00"),
    (("entries", 0, 1, "max_promotion_accesses"), True),
    (("entries", 0, 1, "dataset", "content_hash"), KeyError),
    (("entries", 0, 1, "dataset", "observed", 0), "2019-01-01T00:00:00.000000Z"),
    (("entries", 0, 1, "holdout_instruments"), [iid(3).to_wire()] * 2),
    (("entries", 1, 1, 12), "0"),  # seq as a string
    (("entries", 1, 1, 10), 1),  # granted as a number
    (("entries", 1, 1, 10), False),  # a forged refusal: does not replay
    (("entries", 3, 1, 10), True),  # a forged grant
    (("entries", 2, 1, "metrics", "hit"), 1.5),  # a float
    (("entries", 2, 1, "metrics", "hit"), "1.5e0"),
    (("entries", 2, 1, "metrics", "hit"), "NaN"),
    (("entries", 2, 1, "access_id"), KeyError),
    (("entries", 4, 1, "outcome"), "crashed"),
]


@pytest.mark.parametrize(("path", "value"), LEDGER_BAD)
def test_ledger_wire_is_strict(path: tuple[Any, ...], value: object) -> None:
    with pytest.raises(ResearchError):
        TrialLedger.from_wire(_edit(full_ledger().to_wire(), path, value))


def test_ledger_wire_refuses_reordered_entries() -> None:
    w = through_json(full_ledger().to_wire())
    w["entries"][3], w["entries"][4] = w["entries"][4], w["entries"][3]
    with pytest.raises(ResearchError) as e:
        TrialLedger.from_wire(w)
    assert e.value.code == "time_order"


def full_book() -> BudgetBook:
    """Reservations in every status, a dispatched one with sends, a settlement."""
    b = book()
    for rid, amount in (("r-open", "1.25"), ("r-sent", "2"), ("r-done", "0.5"),
                        ("r-free", "3")):  # fmt: skip
        b, refused = reserve(b, rid, amount)
        assert refused is None
    soon = AT + timedelta(minutes=1)
    b, refused = b.reserve("r-late", Category.RESEARCH, Usd("0.000001"), ACCT, 7,
                           AT, soon)  # fmt: skip
    assert refused is None
    b = b.begin_send("r-sent", AT, 3).end_send("r-sent").begin_send("r-sent", AT, 3)
    b = b.dispatched("r-done", AT).settle("r-done", Usd("0.4321"), AT)
    b = b.release("r-free").dispatched("r-late", AT)
    return b.expire_due(soon)  # only r-late is due


def test_book_round_trip_keeps_every_reservation_field() -> None:
    b = full_book()
    w: Any = b.to_wire()
    back = BudgetBook.from_wire(through_json(w))
    assert back == b and back.to_wire() == w and back.version == b.version
    assert {r.reservation_id: r.status.value for r in back.reservations} == {
        "r-open": "reserved", "r-sent": "reserved", "r-done": "settled",
        "r-free": "released", "r-late": "expired"}  # fmt: skip
    sent = back.get("r-sent")
    assert (sent.dispatched, sent.sends, sent.returned) == (True, 2, 1)
    assert back.get("r-done").actual == Usd("0.4321")
    assert w["reservations"][0]["deadline"] == "2026-10-09T15:05:00Z"
    assert back.summary() == b.summary()
    none = BudgetCaps(CAPS.inference, CAPS.improvement, None)
    empty = BudgetBook.open("tenant-synth-a", AT, none)
    assert BudgetBook.from_wire(through_json(empty.to_wire())) == empty


def test_full_reservation_wire_is_not_the_schema_wire() -> None:
    r = full_book().get("r-sent")
    assert Reservation.from_book_wire(r.to_book_wire()) == r
    assert set(r.to_book_wire()) - set(r.to_wire()) >= {"deadline", "sends"}


BOOK_BAD = [
    (("version",), True),
    (("version",), "3"),
    (("scope_id",), "bad scope"),
    (("caps", "inference"), "100.00"),  # not canonical
    (("caps", "inference"), 100),
    (("caps", "extra"), None),
    (("reservations", 0, "reserved"), "1.250"),
    (("reservations", 0, "reserved"), 1.25),
    (("reservations", 0, "created_at"), "2026-10-09T17:00:00+02:00"),
    (("reservations", 0, "status"), "paid"),
    (("reservations", 0, "dispatched"), 0),
    (("reservations", 0, "actual"), "1"),  # an actual on an open reservation
    (("reservations", 1, "sends"), "2"),
    (("reservations", 1, "returned"), 3),  # more returns than sends
    (("reservations", 2, "actual"), KeyError),
    (("reservations", 2, "actual"), "-1"),
    (("reservations", 4, "category"), "Research"),
]


@pytest.mark.parametrize(("path", "value"), BOOK_BAD)
def test_book_wire_is_strict(path: tuple[Any, ...], value: object) -> None:
    with pytest.raises((BudgetError, ValueError)):
        BudgetBook.from_wire(_edit(full_book().to_wire(), path, value))


def test_book_wire_refuses_a_duplicated_reservation() -> None:
    w = through_json(full_book().to_wire())
    w["reservations"].append(w["reservations"][0])
    with pytest.raises(BudgetError, match="duplicate"):
        BudgetBook.from_wire(w)


def test_a_changed_book_has_a_different_wire() -> None:
    b = full_book()
    later = replace(b, version=b.version + 1)
    assert later.to_wire() != b.to_wire()
    assert BudgetBook.from_wire(later.to_wire()) != b
