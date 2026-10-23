"""Stored forms of the strategy study inputs (owner decisions 72 and 74: search
space and evaluation protocol) and the re-derived candidate manifest (decision 78).
Wires are strict: exact keys, decimal strings, `Z` instants, JSON ints for counts,
and canonical only. The literal hashes were computed at 8a258eb, before these wire
forms existed, so they prove `content_hash` did not move.

All data is SYNTHETIC; expected values are hand-written.
"""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from qw_domain import improvement as im
from qw_domain import strategy_improvement as si
from qw_domain.backtest import (
    Baseline,
    BaselineKind,
    ClaimMetric,
    CostModel,
    DeclaredClaim,
    Direction,
    EvaluationProtocol,
)
from qw_domain.research_data import ResearchError, digest
from qw_domain.strategy_registry import ParamValue, StrategyRegistry
from test_backtest import protocol
from test_improvement import EXP, PRODUCER
from test_strategy_improvement import BASE, CAND, EVAL, SPACE, STRATS, iproto
from test_trial_ledger import T0

D = Decimal
WIDE = si.SearchSpace(
    "space-synth-w", BASE.strategy_id, "0.1.0", BASE.material_hash,
    (si.Dimension("lookback", 60, 200), si.Dimension("band", D("0.020"), D("0.10")),
     si.Dimension("review", allowed=("weekly", "monthly")), si.Dimension("long_only")),
    5, 1,
)  # fmt: skip
ODD = protocol(
    costs=CostModel(D("0.0010"), D("5E-4"), D("0")), z=D("1.960"),
    claim=DeclaredClaim(ClaimMetric.EXCESS_MEAN, Direction.NEGATIVE, "bl-cash"),
    baselines=(Baseline("bl-cash", BaselineKind.CASH, None, D("0.00010")),
               Baseline("bl-hold", BaselineKind.BUY_AND_HOLD, "px-synth-1", D("-0"))),
    declared_at=datetime(2024, 3, 1, 9, 30, 0, 250, tzinfo=UTC), hac_lags=0,
    fill_lag_bars=2,
)  # fmt: skip


def test_content_hashes_are_pinned_from_before_the_wire_forms() -> None:
    assert SPACE.content_hash == (
        "25c6d2bed39b5cdb105794bb6e53783142af55845074dc6f14de8f1ba3df1c1d"
    )
    assert WIDE.content_hash == (
        "688d1fcc7c98b9ca64a8b8c1ea70a7a4279f5511fee0bdd4abf301d5336e5d1c"
    )
    assert EVAL.content_hash == (
        "933e1c243efa968fd55a309af8c0e593a9cfd4b91854243439b3758ebc82bbbc"
    )
    assert ODD.content_hash == (
        "5569f789ca070bf3241565a21b6b825d3afbb40fade38c13f672cc8545cc514d"
    )


# --- search space -----------------------------------------------------------------


def test_search_space_round_trips_through_json() -> None:
    for s in (SPACE, WIDE):
        w = json.loads(json.dumps(s.to_wire()))
        back = si.SearchSpace.from_wire(w)
        assert back.to_wire() == s.to_wire()
        assert back.content_hash == s.content_hash
    assert si.SearchSpace.from_wire(SPACE.to_wire()) == SPACE


def test_search_space_wire_is_name_ordered_with_decimal_string_bounds() -> None:
    w: Any = WIDE.to_wire()
    assert [d["name"] for d in w["dimensions"]] == [
        "band", "long_only", "lookback", "review"]  # fmt: skip
    assert w["dimensions"][0] == {"name": "band", "minimum": "0.020",
                                  "maximum": "0.10", "allowed": []}  # fmt: skip
    assert w["dimensions"][1] == {"name": "long_only", "minimum": None,
                                  "maximum": None, "allowed": []}  # fmt: skip
    assert w["dimensions"][2] == {"name": "lookback", "minimum": "60",
                                  "maximum": "200", "allowed": []}  # fmt: skip
    assert w["dimensions"][3]["allowed"] == ["weekly", "monthly"]
    assert (w["max_trials"], w["min_prospective_scored"]) == (5, 1)
    back = {d.name: d for d in si.SearchSpace.from_wire(w).dimensions}
    assert type(back["lookback"].minimum) is Decimal and back["lookback"].minimum == 60
    assert str(back["band"].minimum) == "0.020"  # the exponent is kept


def test_equal_dimensions_have_one_wire() -> None:
    """5 and Decimal(5) compare and hash alike, so they store alike (no spurious
    conflict in a store keyed by content hash); mixed bound types store too."""
    i, d = si.Dimension("lookback", 60, 200), si.Dimension("lookback", D(60), D(200))
    mixed = si.Dimension("lookback", 60, D("200"))
    assert i == d == mixed and i.wire() == d.wire() == mixed.wire()
    assert i.to_wire() == d.to_wire() == mixed.to_wire()
    back = si.Dimension.from_wire(mixed.to_wire())
    assert back == mixed and back.wire() == mixed.wire()
    assert type(back.minimum) is Decimal and type(back.maximum) is Decimal
    assert all(back.admits(v) == mixed.admits(v) for v in (59, 60, 200, 201))


def test_a_loaded_space_checks_and_derives_like_the_declared_one() -> None:
    """Decimal bounds from the wire against an integer parameter."""
    loaded = si.SearchSpace.from_wire(json.loads(json.dumps(SPACE.to_wire())))
    assert loaded == SPACE and loaded.content_hash == SPACE.content_hash
    loaded.check_base(BASE)
    si.StrategyStudy(iproto(), loaded, EVAL, BASE)  # the frozen hash still matches
    for v in (60, 63, 200):
        assert si.derive(BASE, loaded, {"lookback": v}, "cand-x") == si.derive(
            BASE, SPACE, {"lookback": v}, "cand-x")  # fmt: skip
    for v in (59, 201):
        for space in (SPACE, loaded):
            with pytest.raises(im.ImprovementError, match="outside_search_space"):
                si.derive(BASE, space, {"lookback": v}, "cand-x")
    m = si.candidate_manifest(BASE, loaded, CAND)
    assert m == STRATS.manifest(BASE.strategy_id, CAND.candidate_id)
    assert m.material_hash == CAND.artifact_hash
    wide = si.SearchSpace.from_wire(WIDE.to_wire())
    wide.check_base(BASE)
    c, strats = _wide_candidate()
    assert si.candidate_manifest(BASE, wide, c) == strats.manifest(
        BASE.strategy_id, "cand-w")  # fmt: skip


@pytest.mark.parametrize(
    ("dim", "ok"),
    [
        (si.Dimension("lookback", D("60.0"), D("200")), True),
        (si.Dimension("lookback", 60, D("200")), True),
        (si.Dimension("lookback", D("60.5"), D("200")), False),  # not a whole number
        (si.Dimension("lookback", D("10"), D("200")), False),  # manifest min 20
        (si.Dimension("lookback", D("60"), D("253")), False),  # manifest max 252
        (si.Dimension("band", D("0.02"), D("0.1")), True),
        (si.Dimension("band", D("0.001"), D("0.1")), False),
    ],
)  # fmt: skip
def test_check_base_compares_bounds_by_value(dim: si.Dimension, ok: bool) -> None:
    space = replace(SPACE, dimensions=(dim,))
    if ok:
        space.check_base(BASE)
        return
    with pytest.raises(im.ImprovementError, match="dimension_bounds"):
        space.check_base(BASE)


def _dim(**kw: object) -> dict[str, object]:
    return {"name": "lookback", "minimum": "60", "maximum": "200",
            "allowed": []} | kw  # fmt: skip


BAD_DIMENSIONS: list[object] = [
    None, [], _dim(extra=1), _dim(bounds="integer"),
    {k: v for k, v in _dim().items() if k != "allowed"},
    _dim(minimum=60), _dim(minimum=60.0), _dim(minimum=True), _dim(minimum="+60"),
    _dim(minimum=" 60"), _dim(minimum="6_0"), _dim(minimum="060"),
    _dim(minimum=None), _dim(maximum=None),
    _dim(minimum="2E-2", maximum="0.1"), _dim(minimum="+0.02", maximum="0.1"),
    _dim(minimum="0.020 ", maximum="0.1"), _dim(minimum="NaN", maximum="0.1"),
    _dim(minimum="0.02", maximum="Infinity"), _dim(minimum=0.02, maximum="0.1"),
    _dim(minimum=["60"]), _dim(minimum=None, maximum=None, allowed="weekly"),
    _dim(minimum=None, maximum=None, allowed=[1]),
    _dim(minimum="300"), _dim(name=7), _dim(name="risk_limit"),
]  # fmt: skip


@pytest.mark.parametrize("w", BAD_DIMENSIONS)
def test_dimension_refuses_non_canonical_or_mistyped_wire(w: object) -> None:
    with pytest.raises(im.ImprovementError):
        si.Dimension.from_wire(w)


def _space(**kw: object) -> dict[str, Any]:
    return SPACE.to_wire() | kw


SPACE_DIMS: Any = SPACE.to_wire()["dimensions"]


BAD_SPACES: list[object] = [
    None, [], _space(extra=1),
    {k: v for k, v in SPACE.to_wire().items() if k != "max_trials"},
    _space(max_trials=True), _space(max_trials=3.0), _space(max_trials="3"),
    _space(min_prospective_scored=False), _space(dimensions={}),
    _space(dimensions=list(reversed(SPACE_DIMS))),
    _space(base_material_hash="A" * 64), _space(space_id=None),
]  # fmt: skip


@pytest.mark.parametrize("w", BAD_SPACES)
def test_search_space_refuses_non_canonical_or_mistyped_wire(w: object) -> None:
    with pytest.raises(im.ImprovementError):
        si.SearchSpace.from_wire(w)


# --- evaluation protocol ----------------------------------------------------------


def test_protocol_round_trips_and_its_wire_is_the_hash_body() -> None:
    for p in (EVAL, ODD):
        w = json.loads(json.dumps(p.to_wire()))
        back = EvaluationProtocol.from_wire(w)
        assert back == p and back.to_wire() == w
        assert back.content_hash == p.content_hash == digest(w)


def test_protocol_wire_keeps_decimal_exponents_and_utc() -> None:
    w = ODD.to_wire()
    assert w["costs"] == {"commission_rate": "0.0010", "half_spread": "0.0005",
                          "slippage_rate": "0"}  # fmt: skip
    assert w["z"] == "1.960"
    assert w["baselines"] == [
        ["bl-cash", "cash", None, "0.00010"],
        ["bl-hold", "buy_and_hold", "px-synth-1", "-0"],
    ]
    assert w["declared_at"] == "2024-03-01T09:30:00.000250Z"
    assert w["claim"] == {"metric": "excess_mean_return", "direction": "negative",
                          "baseline_id": "bl-cash"}  # fmt: skip
    back = EvaluationProtocol.from_wire(w)
    assert str(back.costs.commission_rate) == "0.0010" and str(back.z) == "1.960"


def test_approved_by_is_carried_as_asserted_and_bound_by_the_hash() -> None:
    """Owner decision 72: caller-asserted, unverified; the wire keeps it as given."""
    w = EVAL.to_wire()
    assert w["approved_by"] == "owner-synth"
    other = EvaluationProtocol.from_wire(w | {"approved_by": "someone-else"})
    assert other.approved_by == "someone-else"
    assert other.content_hash != EVAL.content_hash


def _proto(**kw: object) -> dict[str, Any]:
    return copy.deepcopy(ODD.to_wire()) | kw


def _nested(key: str, **kw: object) -> dict[str, Any]:
    w = _proto()
    w[key] = w[key] | kw
    return w


BAD_PROTOCOLS: list[object] = [
    None, [], _proto(extra=1),
    {k: v for k, v in ODD.to_wire().items() if k != "approved_by"},
    _proto(fill_lag_bars=True), _proto(fill_lag_bars=2.0), _proto(fill_lag_bars="2"),
    _proto(hac_lags=False), _proto(z=1.96), _proto(z="1.96E0"), _proto(z="+1.960"),
    _proto(z=" 1.960"), _proto(z="1_960"),
    _proto(declared_at="2024-03-01T09:30:00.000250+00:00"),
    _proto(declared_at="2024-03-01T11:30:00.000250+02:00"),
    _proto(declared_at="2024-03-01T09:30:00.00025Z"),
    _proto(declared_at="2024-03-01T09:30:00.000250"), _proto(declared_at=0),
    _proto(baselines={}), _proto(baselines=[]),
    _proto(baselines=[["bl-cash", "cash", None]]),
    _proto(baselines=[["bl-cash", "cash", None, 0.0001]]),
    _proto(baselines=[["bl-cash", "cash", None, "1E-4"]]),
    _proto(baselines=[["bl-cash", "rent", None, "0"]]),
    _proto(baselines=[["bl-cash", "cash", 5, "0"]]),
    _nested("costs", extra="0"), _nested("costs", commission_rate=0.001),
    _nested("costs", commission_rate="1E-3"), _nested("costs", half_spread="-0.1"),
    _nested("claim", extra=None), _nested("claim", metric="sharpe"),
    _nested("claim", direction=["negative"]), _nested("claim", baseline_id=5),
    _proto(costs=None), _proto(claim=[]), _proto(approved_by=7),
]  # fmt: skip


@pytest.mark.parametrize("w", BAD_PROTOCOLS)
def test_protocol_refuses_non_canonical_or_mistyped_wire(w: object) -> None:
    with pytest.raises(ResearchError):
        EvaluationProtocol.from_wire(w)


def test_parts_refuse_non_canonical_wire_on_their_own() -> None:
    with pytest.raises(ResearchError, match="is not a canonical decimal"):
        CostModel.from_wire({"commission_rate": "0.001", "half_spread": "5e-4",
                             "slippage_rate": "0"})  # fmt: skip
    with pytest.raises(ResearchError, match="exactly the cost keys"):
        CostModel.from_wire({"commission_rate": "0.001", "half_spread": "0"})
    with pytest.raises(ResearchError, match="baseline_wire"):
        Baseline.from_wire(("bl-cash", "cash", None, "0"))
    with pytest.raises(ResearchError, match="claim_wire"):
        DeclaredClaim.from_wire({"metric": "net_mean_return",
                                 "direction": "positive"})  # fmt: skip
    assert CostModel.from_wire(ODD.costs.to_wire()) == ODD.costs


# --- candidate manifest (owner decision 78) ---------------------------------------


def test_candidate_manifest_equals_the_registered_derivation() -> None:
    m = si.candidate_manifest(BASE, SPACE, CAND)
    registered = STRATS.manifest(BASE.strategy_id, CAND.candidate_id)
    assert m == registered
    assert m.version == CAND.candidate_id
    assert (m.material_hash, m.content_hash, m.config_hash({})) == (
        registered.material_hash, registered.content_hash,
        registered.config_hash({}),
    )  # fmt: skip
    assert m.material_hash == CAND.artifact_hash


def _wide_candidate() -> tuple[im.Candidate, StrategyRegistry]:
    values: dict[str, ParamValue] = {"band": D("0.070"), "review": "monthly",
                                     "long_only": False}  # fmt: skip
    derived = si.derive(BASE, WIDE, values, "cand-w")
    c = si.strategy_candidate(BASE, WIDE, derived, candidate_id="cand-w",
                              experiment_id=EXP, parent_id="0.1.0", producer=PRODUCER,
                              runtime_versions={"engine": "v1"},
                              created_at=T0 + timedelta(minutes=5))  # fmt: skip
    return c, StrategyRegistry().register(BASE).register(derived)


def test_candidate_manifest_parses_every_kind_and_passes_only_the_diff() -> None:
    c, strats = _wide_candidate()
    assert dict(c.payload) == {"band": "0.07", "long_only": "false",
                               "lookback": "126", "review": "monthly"}  # fmt: skip
    m = si.candidate_manifest(BASE, WIDE, c)
    registered = strats.manifest(BASE.strategy_id, "cand-w")
    assert m == registered and m.material_hash == registered.material_hash
    assert m.content_hash == registered.content_hash
    si.check_derived(BASE, WIDE, m)
    # SPACE declares neither band nor long_only: the same payload re-derived
    # against SPACE is refused, not silently passed.
    with pytest.raises(im.ImprovementError, match="outside_search_space"):
        si.candidate_manifest(BASE, SPACE, c)


def _payload(**kw: str) -> im.Candidate:
    return replace(CAND, payload=dict(CAND.payload) | kw)


@pytest.mark.parametrize(
    ("candidate", "code"),
    [
        (replace(CAND, payload=dict(CAND.payload) | {"extra": "1"}),
         "payload_mismatch"),
        (replace(CAND, payload={k: v for k, v in CAND.payload.items()
                                if k != "review"}), "payload_mismatch"),
        (_payload(lookback="063"), "payload_mismatch"),
        (_payload(lookback="+63"), "payload_mismatch"),
        (_payload(lookback="63.0"), "payload_mismatch"),
        (_payload(band="0.050"), "payload_mismatch"),
        (_payload(band="5E-2"), "payload_mismatch"),
        (_payload(band="NaN"), "outside_search_space"),
        (_payload(long_only="True"), "payload_mismatch"),
        (_payload(lookback="300"), "outside_search_space"),
        (_payload(lookback="250"), "outside_search_space"),
        (_payload(band="0.06"), "outside_search_space"),
        (_payload(review="daily"), "outside_search_space"),
        (_payload(lookback="64"), "candidate_mismatch"),
        (_payload(lookback="126"), "no_change"),
        (replace(CAND, artifact_hash="b" * 64), "candidate_mismatch"),
        (replace(CAND, target=im.Target.RESEARCH), "target"),
    ],
)  # fmt: skip
def test_candidate_manifest_refuses(candidate: im.Candidate, code: str) -> None:
    with pytest.raises(im.ImprovementError) as e:
        si.candidate_manifest(BASE, SPACE, candidate)
    assert e.value.code == code


def test_candidate_manifest_refuses_a_different_base() -> None:
    other = replace(BASE, implementation_hash="c" * 64)
    with pytest.raises(im.ImprovementError, match="space_mismatch"):
        si.candidate_manifest(other, SPACE, CAND)
