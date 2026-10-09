"""Calculation records (T018 increment 3; spec §5, T008 C-15). SYNTHETIC inputs only.

The contract projection is validated against docs/spec/contracts/schemas/
calculation.schema.json. NUM03, NUM04 and NUM05 supply figures; the others are
hand-computed in comments.
"""

import dataclasses
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_domain.benchmark import (
    BenchmarkKind,
    BenchmarkLevel,
    BenchmarkPolicy,
    BenchmarkSeries,
    BenchmarkSimulation,
    Reinvestment,
    simulate_benchmark,
)
from qw_domain.calculation import (
    CalcInput,
    CalcStatus,
    CalculationRecord,
    InputKind,
    record_calculation,
    record_figure,
)
from qw_domain.decimals import Money, Price, Ratio
from qw_domain.instants import TIMESTAMP_PATTERN, InstantError
from qw_domain.returns import (
    TWR_METHOD,
    DatedFlow,
    ExternalFlow,
    FlowValuation,
    fx_attribution,
    modified_dietz,
    money_weighted_return,
    time_weighted_return,
)
from qw_domain.valuation import Unavailable

PROFILE = settings(derandomize=True, database=None, max_examples=60, deadline=None)
ROOT = Path(__file__).resolve().parents[3] / "docs/spec"
SCHEMA = json.loads((ROOT / "contracts/schemas/calculation.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
ORACLES = {
    o["id"]: o
    for o in json.loads((ROOT / "tests/fixtures/numerical_oracles.json").read_text())[
        "oracles"
    ]
}
T0 = datetime(2026, 1, 2, 21, tzinfo=UTC)
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


def inputs(start: str = "1000", rev: int = 3) -> list[CalcInput]:
    return [
        CalcInput.journal_revision("acct-1", rev),
        CalcInput.money("value_start", Money.of(start, "USD"), "val-1", as_of=T0),
        CalcInput.decimal("mark", Price("50.10"), "USD", "mark-9", as_of=T0),
        CalcInput.on_date("period_start", date(2026, 1, 2), "acct-1"),
        CalcInput.reference("benchmark_series", "SYN-TR@2026.1", "series", "bm-1"),
    ]


TWR_021 = Ratio("0.21")


def record(
    outcome: Ratio | Money | Unavailable = TWR_021, **kw: object
) -> CalculationRecord:
    args: dict[str, object] = {
        "unit": "ratio", "rounding": "display_half_even:1e-18", "as_of": T0,
        "computed_at": NOW, "inputs": inputs(), "coverage": "complete",
    }  # fmt: skip
    args.update(kw)
    return record_calculation(TWR_METHOD, outcome=outcome, **args)  # type: ignore[arg-type]


def test_num03_record_matches_the_contract_schema() -> None:
    inp, exp = ORACLES["NUM03"]["inputs"], ORACLES["NUM03"]["expected"]

    def usd(v: str) -> Money:
        return Money.of(v, "USD")

    twr = time_weighted_return([
        FlowValuation(T0, usd(inp["start"]), usd("0")),
        FlowValuation(T0 + timedelta(days=1), usd(inp["before_flow"]),
                      usd(inp["contribution"])),
        FlowValuation(T0 + timedelta(days=2), usd(inp["end"]), usd("0")),
    ])  # fmt: skip
    rec = record_figure(twr, inputs(), as_of=T0, computed_at=NOW, coverage="complete")
    wire = rec.to_contract()
    VALIDATOR.validate(wire)
    assert wire["result"] == exp["return"] == "0.21"
    assert wire["status"] == "ok" and wire["unit"] == "ratio"
    assert (wire["formula_id"], wire["formula_version"]) == ("chain_linked_twr", "1")
    assert wire["id"] == f"calc:{rec.content_hash}"
    assert wire["computed_at"] == "2026-10-09T12:00:00Z"
    assert re.fullmatch(TIMESTAMP_PATTERN, str(wire["computed_at"]))
    # Decimal-valued inputs only; dates and references are in the hashes.
    ins = wire["inputs"]
    assert isinstance(ins, list)
    assert [i["name"] for i in ins] == ["journal_revision", "value_start", "mark"]
    assert ins[2] == {
        "name": "mark", "value": "50.1", "unit": "USD", "source_record_id": "mark-9"
    }  # fmt: skip


def test_full_record_carries_c15_fields() -> None:
    full = record().canonical()
    assert full["as_of"] == "2026-01-02T21:00:00Z" and full["coverage"] == "complete"
    assert full["rounding"] == "display_half_even:1e-18"
    assert full["format"] == "calculation_record/1" and "computed_at" not in full
    ins = full["inputs"]
    assert isinstance(ins, list)
    by_name = {i["name"]: i for i in ins}
    assert by_name["value_start"]["as_of"] == "2026-01-02T21:00:00Z"
    assert by_name["value_start"]["kind"] == "money"
    assert by_name["period_start"] == {
        "name": "period_start", "kind": "date", "value": "2026-01-02",
        "unit": "date", "source_record_id": "acct-1", "as_of": None,
    }  # fmt: skip


def test_unavailable_has_no_result() -> None:
    rec = record(Unavailable("nonpositive_denominator", "start value 0"))
    wire = rec.to_contract()
    VALIDATOR.validate(wire)
    assert wire["result"] is None and wire["status"] == "undefined"
    assert "unavailable:nonpositive_denominator" in wire["warnings"]  # type: ignore[operator]
    assert record(Unavailable("ambiguous", "x")).status is CalcStatus.AMBIGUOUS
    assert record(Unavailable("mark_missing", "x")).status is CalcStatus.INSUFFICIENT
    rec = record(Money.of("1098", "USD"), unit="USD")
    assert rec.to_contract()["result"] == "1098"
    with pytest.raises(ValueError, match="unit"):
        record(Money.of("1098", "USD"), unit="CAD")


def test_hashes() -> None:
    base = record()
    assert base.content_hash == record(computed_at=NOW + timedelta(days=1)).content_hash
    assert base.input_snapshot_hash == record(Ratio("0.3")).input_snapshot_hash
    assert base.content_hash != record(Ratio("0.3")).content_hash
    assert base.input_snapshot_hash != record(inputs=inputs(rev=4)).input_snapshot_hash
    moved = [
        *inputs()[:2],
        CalcInput.decimal(
            "mark", Price("50.1"), "USD", "mark-9", as_of=T0 + timedelta(seconds=1)
        ),
    ]
    assert base.input_snapshot_hash != record(inputs=moved).input_snapshot_hash
    assert base.content_hash != record(coverage="partial").content_hash
    assert base.content_hash != record(rounding="cost_up:1e-12").content_hash


@PROFILE
@given(st.integers(-(10**12), 10**12), st.integers(0, 6), st.integers(0, 6))
def test_hash_is_stable_across_decimal_representations(
    n: int, scale: int, pad: int
) -> None:
    value = Decimal(n).scaleb(-scale)
    padded = value.quantize(Decimal(1).scaleb(-(scale + pad)))  # trailing zeros
    exponent = Decimal(f"{n}E-{scale}")
    hashes = {
        record(inputs=[CalcInput.decimal("x", v, "ratio", "src")]).content_hash
        for v in (value, padded, exponent, value.normalize())
    }
    assert len(hashes) == 1


def test_rejections() -> None:
    with pytest.raises(TypeError):
        CalcInput.decimal("x", 1.5, "ratio", "src")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        CalcInput.decimal("x", Decimal("NaN"), "ratio", "src")
    with pytest.raises(InstantError):
        CalcInput.money("v", Money.of("1", "USD"), "src", as_of=datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(InstantError):
        record(as_of=datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="source"):
        CalcInput.reference("x", "v", "u", "bad id with spaces")
    with pytest.raises(ValueError, match="duplicate"):
        record(inputs=[*inputs(), *inputs()[:1]])
    with pytest.raises(ValueError, match="method"):
        record_calculation("no-version", [], Ratio("0"), unit="ratio", rounding="r",
                           as_of=T0, computed_at=NOW, coverage="complete")  # fmt: skip
    with pytest.raises(ValueError, match="revision"):
        CalcInput.journal_revision("acct-1", 0)
    rec = record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.result = "0.5"  # type: ignore[misc]


def test_hash_one_point_zero_equals_one_point_zero_zero() -> None:
    a = record(inputs=[CalcInput.decimal("x", Decimal("1.0"), "ratio", "src")])
    b = record(inputs=[CalcInput.decimal("x", Decimal("1.00"), "ratio", "src")])
    assert a.content_hash == b.content_hash
    assert a.input_snapshot_hash == b.input_snapshot_hash
    assert a.to_contract()["inputs"] == [
        {"name": "x", "value": "1", "unit": "ratio", "source_record_id": "src"}
    ]


def figure(fig: object, **kw: object) -> dict[str, object]:
    ins = inputs()[:4]  # benchmark figures add their own series reference
    rec = record_figure(fig, ins, as_of=T0, computed_at=NOW, coverage="complete", **kw)  # type: ignore[arg-type]
    wire = rec.to_contract()
    VALIDATOR.validate(wire)
    return wire


def usd(v: str) -> Money:
    return Money.of(v, "USD")


def mwr_flows(*items: tuple[int, str]) -> list[DatedFlow]:
    return [DatedFlow(date(2025, 1, 1) + timedelta(days=d), usd(a)) for d, a in items]


def test_mwr_records() -> None:
    wire = figure(money_weighted_return(mwr_flows((0, "-1000"), (365, "1100"))))
    assert wire["status"] == "ok" and wire["formula_id"] == "mwr_bisection_act365f"
    assert abs(Decimal(str(wire["result"])) - Decimal("0.1")) < Decimal("1e-18")
    assert "uniqueness_rule:descartes" in wire["warnings"]  # type: ignore[operator]
    cfs = ORACLES["NUM05"]["inputs"]["annual_cashflows"]
    wire = figure(
        money_weighted_return(mwr_flows(*zip((0, 365, 730), cfs, strict=True)))
    )
    assert wire["status"] == "ambiguous" and wire["result"] is None
    warnings = wire["warnings"]
    assert isinstance(warnings, list) and "unavailable:ambiguous" in warnings
    roots = [w for w in warnings if w.startswith("candidate_period_return:")]
    assert len(roots) == len(ORACLES["NUM05"]["expected"]["roots"])  # two roots


def test_dietz_and_fx_records() -> None:
    # 100/1050, as in test_returns: 0.095238095238095238.
    md = modified_dietz(usd("1000"), usd("1200"), date(2025, 1, 1), date(2025, 1, 31),
                        [ExternalFlow(date(2025, 1, 16), usd("100"))])  # fmt: skip
    wire = figure(md)
    assert wire["result"] == "0.095238095238095238"
    assert wire["formula_id"] == "modified_dietz_approx"
    assert "approximate" in wire["warnings"]  # type: ignore[operator]
    inp, exp = ORACLES["NUM04"]["inputs"], ORACLES["NUM04"]["expected"]
    wire = figure(fx_attribution(Ratio(inp["local_return"]), Ratio(inp["fx_return"])))
    assert (
        wire["result"] == exp["base_return"]
        and wire["formula_id"] == "fx_multiplicative"
    )


def bench(version: str) -> BenchmarkSimulation:
    d0, d1 = date(2026, 1, 5), date(2026, 2, 4)
    levels = tuple(
        BenchmarkLevel(d, Price(v), datetime(d.year, d.month, d.day, 21, tzinfo=UTC))
        for d, v in ((d0, "100"), (d1, "112.5"))
    )
    policy = BenchmarkPolicy(
        "bm", "1", "USD", BenchmarkKind.TOTAL_RETURN_INDEX, "SYN-TR",
        datetime(2025, 12, 1, tzinfo=UTC), series_version=version,
        reinvestment=Reinvestment.TOTAL_RETURN,
    )  # fmt: skip
    series = BenchmarkSeries("SYN-TR", version, "USD", levels)
    out = simulate_benchmark(policy, series, usd("1000"), d0, d1, [])
    assert isinstance(out, BenchmarkSimulation)
    return out


def test_benchmark_record_references_series_id_and_version() -> None:
    # 10 units of a level going 100 -> 112.5: ending value 1125.
    sim = bench("2026.1")
    wire = figure(sim)
    assert (wire["result"], wire["unit"]) == ("1125", "USD")
    assert wire["formula_id"] == "benchmark_flow_simulation"
    rec = record_figure(sim, [], as_of=T0, computed_at=NOW, coverage="complete")
    full = {i.name: i for i in rec.inputs}
    assert full["benchmark_series"].value == "SYN-TR@2026.1"
    assert full["benchmark_series"].source_record_id == "SYN-TR"
    assert full["benchmark_policy"].value == "bm@1"
    assert full["level:2026-01-05"].value == "100"
    assert full["level:2026-01-05"].as_of == datetime(2026, 1, 5, 21, tzinfo=UTC)
    assert rec.rounding == "floor:1e-12"
    other = record_figure(
        bench("2026.2"), [], as_of=T0, computed_at=NOW, coverage="complete"
    )
    assert other.input_snapshot_hash != rec.input_snapshot_hash
    assert not isinstance(sim.twr, Unavailable)
    twr = record_figure(
        sim.twr, rec.inputs, as_of=T0, computed_at=NOW, coverage="complete"
    )
    assert twr.result == "0.125" and twr.rounding == "half_even:1e-18"


def test_record_figure_rejections() -> None:
    gap = Unavailable("missing_valuation_interval", "x")
    with pytest.raises(ValueError, match="method"):
        record_figure(gap, [], as_of=T0, computed_at=NOW, coverage="complete")
    rec = record_figure(
        gap, [], as_of=T0, computed_at=NOW, coverage="complete", method=TWR_METHOD
    )
    assert rec.status is CalcStatus.INSUFFICIENT and rec.result is None
    md = modified_dietz(usd("1"), usd("1"), date(2025, 1, 1), date(2025, 1, 2), [])
    with pytest.raises(ValueError, match="method"):
        record_figure(
            md, [], as_of=T0, computed_at=NOW, coverage="complete", method=TWR_METHOD
        )
    with pytest.raises(TypeError):
        record_figure(Ratio("0.1"), [], as_of=T0, computed_at=NOW, coverage="complete")  # type: ignore[arg-type]


def test_projection_states_what_the_schema_cannot_hold() -> None:
    # S1: as-of, rounding and coverage; S2: date and reference inputs, input as-of.
    warnings = record(coverage="partial").to_contract()["warnings"]
    assert isinstance(warnings, list)
    for w in (
        "as_of:2026-01-02T21:00:00Z",
        "rounding:display_half_even:1e-18",
        "coverage:partial",
        "ref:period_start=2026-01-02",
        "ref:benchmark_series=SYN-TR@2026.1",
        "input_as_of:value_start=2026-01-02T21:00:00Z",
    ):
        assert w in warnings
    VALIDATOR.validate(record(coverage="partial").to_contract())
    full = record_figure(bench("2026.1"), [], as_of=T0, computed_at=NOW,
                         coverage="complete").to_contract()  # fmt: skip
    assert "ref:benchmark_policy=bm@1" in full["warnings"]  # type: ignore[operator]
    assert "ref:benchmark_series=SYN-TR@2026.1" in full["warnings"]  # type: ignore[operator]
    assert "coverage:complete" in full["warnings"]  # type: ignore[operator]


def test_coverage_is_required() -> None:
    with pytest.raises(TypeError):
        record_calculation(TWR_METHOD, [], Ratio("0"), unit="ratio", rounding="r",
                           as_of=T0, computed_at=NOW)  # type: ignore[call-arg]  # fmt: skip
    with pytest.raises(TypeError):
        record_figure(Ratio("0"), [], as_of=T0, computed_at=NOW)  # type: ignore[call-arg, arg-type]


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (InputKind.DECIMAL, "1.0"), (InputKind.DECIMAL, "01"),
        (InputKind.DECIMAL, "-0"),
        (InputKind.DECIMAL, "1E+2"), (InputKind.DECIMAL, "1" * 61),
        (InputKind.MONEY, "2.50"), (InputKind.DATE, "2026-1-2"),
        (InputKind.DATE, "2026-02-30"),
    ],
)  # fmt: skip
def test_non_canonical_values_are_rejected(kind: InputKind, value: str) -> None:
    with pytest.raises(ValueError, match="canonical"):
        CalcInput("x", kind, value, "u", "src")
    assert CalcInput("x", InputKind.DECIMAL, "-1.5", "u", "src").value == "-1.5"


def test_huge_decimals_are_rejected_before_formatting() -> None:
    for v in ("1E+100000000", "1E-100000000"):  # refused from the exponent
        with pytest.raises(ValueError, match="decimal input over 60"):
            CalcInput.decimal("x", Decimal(v), "ratio", "src")
    with pytest.raises(ValueError, match="over 60"):  # 61 digits, refused on length
        CalcInput.decimal("x", Decimal("1" * 61), "ratio", "src")
    assert CalcInput.decimal("x", Decimal("1.000E+3"), "u", "src").value == "1000"


def test_colliding_benchmark_series_input_is_rejected() -> None:
    clash = [CalcInput.reference("benchmark_series", "X@1", "series", "bm-1")]
    with pytest.raises(ValueError, match="duplicate"):
        record_figure(bench("2026.1"), clash, as_of=T0, computed_at=NOW,
                      coverage="complete")  # fmt: skip


def test_policy_codes_are_unsupported_and_as_of_precedes_computation() -> None:
    for c in ("benchmark_selected_late", "benchmark_version_mismatch",
              "flow_outside_period"):  # fmt: skip
        assert record(Unavailable(c, "x")).status is CalcStatus.UNSUPPORTED
    with pytest.raises(ValueError, match="computed_at"):
        record(as_of=NOW + timedelta(seconds=1))
