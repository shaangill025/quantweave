"""Immutable calculation records (T018; spec §5 numeric-function outputs, T008 C-15).

A `CalculationRecord` holds one computed figure: typed inputs referenced by source
record id (journal revision per account, valuation inputs with their observation
time, benchmark series id/version), the method and version, the rounding policy, the
figure's as-of time and coverage, and the result or the `Unavailable` reason.
- Canonical form (`canonical`): JSON with decimal strings (no exponent, no trailing
  zeros, so 1.50, 1.5 and 15E-1 hash alike) and UTC `Z` timestamps. A `CalcInput`
  accepts only the canonical value shape for its kind (decimal "1", not "1.0"; ISO
  dates), and decimals of at most 60 characters (the schema's maxLength), checked
  from the exponent before formatting.
  `input_snapshot_hash` is the SHA-256 of the canonical inputs; `content_hash` of the
  whole canonical record. `computed_at` is excluded, so recomputing the same figure
  from the same inputs gives the same content hash and record id.
- `to_contract` projects onto contracts/schemas/calculation.schema.json, which has no
  fields yet for input kinds, input as-of times, coverage or rounding (C-15). Those
  stay in the canonical record and both hashes; only decimal and money inputs fit the
  schema's decimal-string `value`, so date and reference inputs are omitted from the
  projection's inputs. So that the projection never looks more complete than it
  is, its warnings always carry `as_of:<instant>`, `rounding:<rule>`,
  `coverage:<complete|partial>`, `ref:<name>=<value>` for each date or reference
  input (e.g. the benchmark series id@version) and `input_as_of:<name>=<instant>`.
  Extending the schema is an owner decision (T008 A-01: the project-owned
  contract), not made here.
- `record_figure` records a T018 result (TWR, MWR, Modified Dietz, FX attribution,
  benchmark simulation) with its own method and rounding. An ambiguous MWR is
  status ambiguous with its candidate roots as warnings; a benchmark adds its policy
  id@version, series id@version, period dates and each level used (with its
  observation time) to the inputs.
- Status: ok iff a result exists (C-15). Unavailable codes map to `undefined`,
  `ambiguous`, `unsupported` (including the benchmark policy codes: selected late,
  version mismatch, flow outside the period) or `insufficient_inputs`; the code is
  kept and appears in the projection's warnings as `unavailable:<code>`.
- `coverage` is a required argument, and `as_of` must not be after `computed_at`.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from qw_domain.benchmark import BenchmarkSimulation
from qw_domain.decimals import BoundedDecimal, Money
from qw_domain.instants import ensure_aware_utc, format_instant, require_date
from qw_domain.returns import (
    ApproximateReturn,
    FxAttribution,
    MoneyWeightedReturn,
    PeriodReturn,
)
from qw_domain.valuation import Unavailable

type Figure = (
    PeriodReturn
    | MoneyWeightedReturn
    | ApproximateReturn
    | FxAttribution
    | BenchmarkSimulation
)

RECORD_FORMAT = "calculation_record/1"
RATIO_ROUNDING = "half_even:1e-18"  # returns.py `Ratio` results
MONEY_FLOOR = "floor:1e-12"  # benchmark ending value, as in valuation
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", re.ASCII)
# The schema's decimal pattern, canonical (no trailing fractional zeros, no -0).
_CANONICAL = re.compile(r"(?!-0$)-?(0|[1-9][0-9]*)(\.[0-9]*[1-9])?", re.ASCII)
MAX_DECIMAL_CHARS = 60  # the schema's maxLength
_UNSUPPORTED = frozenset(
    {"benchmark_selected_late", "benchmark_version_mismatch", "flow_outside_period"}
)
_UNDEFINED = frozenset(
    {
        "nonpositive_denominator",
        "negative_end_value",
        "negative_units",
        "no_sign_change",
        "no_equity",
        "no_root_in_domain",
        "root_outside_domain",
        "return_below_minus_one",
    }
)


class InputKind(StrEnum):
    DECIMAL = "decimal"
    MONEY = "money"
    DATE = "date"
    REFERENCE = "reference"


class CalcStatus(StrEnum):
    OK = "ok"
    INSUFFICIENT = "insufficient_inputs"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"
    UNDEFINED = "undefined"


def _decimal_text(value: object) -> str:
    if isinstance(value, BoundedDecimal):
        return value.to_wire()
    if type(value) is not Decimal:
        raise TypeError(f"decimal input must be Decimal, not {type(value).__name__}")
    if not value.is_finite():
        raise ValueError("decimal input must be finite")
    _, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    zeros = len(digits) - len("".join(map(str, digits)).rstrip("0"))
    if not -MAX_DECIMAL_CHARS <= exponent + zeros <= MAX_DECIMAL_CHARS:
        raise ValueError(f"decimal input over {MAX_DECIMAL_CHARS} characters")
    text = format(value, "f")
    text = text.rstrip("0").rstrip(".") if "." in text else text
    return "0" if text in ("-0", "") else text


@dataclass(frozen=True, slots=True)
class CalcInput:
    """Use the constructors; `value` is already canonical text."""

    name: str
    kind: InputKind
    value: str
    unit: str
    source_record_id: str
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.unit or not self.value:
            raise ValueError("input needs a name, a value and a unit")
        if not isinstance(self.source_record_id, str) or not _ID.fullmatch(
            self.source_record_id
        ):
            raise ValueError("source_record_id must be a record id")
        if self.as_of is not None:
            object.__setattr__(self, "as_of", ensure_aware_utc(self.as_of))
        if type(self.kind) is not InputKind:
            raise TypeError("kind must be an InputKind")
        if self.kind in (InputKind.DECIMAL, InputKind.MONEY):
            if len(self.value) > MAX_DECIMAL_CHARS:
                raise ValueError(
                    f"non-canonical decimal over {MAX_DECIMAL_CHARS} chars"
                )
            if not _CANONICAL.fullmatch(self.value):
                raise ValueError(f"non-canonical decimal {self.value!r:.70}")
        if self.kind is InputKind.DATE:
            try:
                ok = date.fromisoformat(self.value).isoformat() == self.value
            except ValueError:
                ok = False
            if not ok:
                raise ValueError(f"non-canonical date {self.value!r:.30}")

    @classmethod
    def decimal(
        cls,
        name: str,
        value: BoundedDecimal | Decimal,
        unit: str,
        source: str,
        *,
        as_of: datetime | None = None,
    ) -> "CalcInput":
        return cls(name, InputKind.DECIMAL, _decimal_text(value), unit, source, as_of)

    @classmethod
    def money(
        cls, name: str, value: Money, source: str, *, as_of: datetime | None = None
    ) -> "CalcInput":
        if type(value) is not Money:
            raise TypeError("money input must be Money")
        text = value.amount.to_wire()
        return cls(name, InputKind.MONEY, text, value.currency, source, as_of)

    @classmethod
    def on_date(cls, name: str, value: date, source: str) -> "CalcInput":
        text = require_date(value, name).isoformat()
        return cls(name, InputKind.DATE, text, "date", source)

    @classmethod
    def reference(cls, name: str, value: str, unit: str, source: str) -> "CalcInput":
        return cls(name, InputKind.REFERENCE, value, unit, source)

    @classmethod
    def journal_revision(cls, account_id: str, revision: int) -> "CalcInput":
        if type(revision) is not int or revision < 1:
            raise ValueError("journal revision must be an int >= 1")
        return cls.decimal(
            "journal_revision", Decimal(revision), "revision", account_id
        )

    def canonical(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "value": self.value,
            "unit": self.unit,
            "source_record_id": self.source_record_id,
            "as_of": None if self.as_of is None else format_instant(self.as_of),
        }


def _sha256(body: object) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CalculationRecord:
    formula_id: str
    formula_version: str
    inputs: tuple[CalcInput, ...]
    result: str | None
    unit: str
    status: CalcStatus
    unavailable: Unavailable | None
    warnings: tuple[str, ...]
    rounding: str
    as_of: datetime
    coverage: Literal["complete", "partial"]
    computed_at: datetime

    @property
    def input_snapshot_hash(self) -> str:
        return _sha256([i.canonical() for i in self.inputs])

    def canonical(self) -> dict[str, object]:
        gap = self.unavailable
        return {
            "format": RECORD_FORMAT,
            "formula_id": self.formula_id,
            "formula_version": self.formula_version,
            "inputs": [i.canonical() for i in self.inputs],
            "result": self.result,
            "unit": self.unit,
            "status": self.status.value,
            "unavailable": None if gap is None else [gap.code, gap.reason],
            "warnings": list(self.warnings),
            "rounding": self.rounding,
            "as_of": format_instant(self.as_of),
            "coverage": self.coverage,
        }

    @property
    def content_hash(self) -> str:
        return _sha256(self.canonical())

    def to_contract(self) -> dict[str, object]:
        gap = (
            [] if self.unavailable is None else [f"unavailable:{self.unavailable.code}"]
        )
        held = [  # what the schema has no field for (module docstring)
            f"as_of:{format_instant(self.as_of)}",
            f"rounding:{self.rounding}",
            f"coverage:{self.coverage}",
            *(
                f"ref:{i.name}={i.value}"
                for i in self.inputs
                if i.kind in (InputKind.DATE, InputKind.REFERENCE)
            ),
            *(
                f"input_as_of:{i.name}={format_instant(i.as_of)}"
                for i in self.inputs
                if i.as_of is not None
            ),
        ]
        return {
            "id": f"calc:{self.content_hash}",
            "formula_id": self.formula_id,
            "formula_version": self.formula_version,
            "inputs": [
                {k: i.canonical()[k] for k in ("name", "value", "unit")}
                | {"source_record_id": i.source_record_id}
                for i in self.inputs
                if i.kind in (InputKind.DECIMAL, InputKind.MONEY)
            ],
            "result": self.result,
            "unit": self.unit,
            "status": self.status.value,
            "warnings": [*self.warnings, *gap, *held],
            "input_snapshot_hash": self.input_snapshot_hash,
            "computed_at": format_instant(self.computed_at),
        }


def _status(gap: Unavailable) -> CalcStatus:
    if gap.code in _UNDEFINED:
        return CalcStatus.UNDEFINED
    if gap.code.startswith("ambiguous"):
        return CalcStatus.AMBIGUOUS
    if gap.code.startswith("unsupported") or gap.code in _UNSUPPORTED:
        return CalcStatus.UNSUPPORTED
    return CalcStatus.INSUFFICIENT


def record_calculation(
    method: str,
    inputs: Sequence[CalcInput],
    outcome: BoundedDecimal | Money | Unavailable,
    *,
    unit: str,
    rounding: str,
    as_of: datetime,
    computed_at: datetime,
    warnings: Sequence[str] = (),
    coverage: Literal["complete", "partial"],
) -> CalculationRecord:
    """`method` is "<formula_id>/<version>", as the engine's *_METHOD constants."""
    formula_id, sep, version = method.partition("/")
    if not sep or not version or not _ID.fullmatch(formula_id):
        raise ValueError("method must be '<formula_id>/<version>'")
    ins = tuple(inputs)
    if any(type(i) is not CalcInput for i in ins):
        raise TypeError("inputs must be CalcInput")
    if len({i.name for i in ins}) != len(ins):
        raise ValueError("duplicate input name")
    if not rounding or coverage not in ("complete", "partial"):
        raise ValueError("rounding policy and coverage are required")
    if ensure_aware_utc(as_of) > ensure_aware_utc(computed_at):
        raise ValueError("as_of is after computed_at")
    result, status, gap = None, CalcStatus.OK, None
    if isinstance(outcome, Unavailable):
        status, gap = _status(outcome), outcome
    elif isinstance(outcome, Money):
        if outcome.currency != unit:
            raise ValueError("a money result's unit is its currency")
        result = outcome.amount.to_wire()
    elif isinstance(outcome, BoundedDecimal):
        result = outcome.to_wire()
    else:
        raise TypeError("outcome must be a decimal value type, Money or Unavailable")
    return CalculationRecord(
        formula_id,
        version,
        ins,
        result,
        unit,
        status,
        gap,
        tuple(warnings),
        rounding,
        ensure_aware_utc(as_of),
        coverage,
        ensure_aware_utc(computed_at),
    )


def benchmark_inputs(sim: BenchmarkSimulation) -> list[CalcInput]:
    """Policy and series by id@version, the period and every level used."""
    return [
        CalcInput.reference(
            "benchmark_policy", f"{sim.policy_id}@{sim.policy_version}",
            "benchmark_policy", sim.policy_id,
        ),
        CalcInput.reference(
            "benchmark_series", f"{sim.series_id}@{sim.series_version}",
            "benchmark_series", sim.series_id,
        ),
        CalcInput.on_date("benchmark_start", sim.start, sim.series_id),
        CalcInput.on_date("benchmark_end", sim.end, sim.series_id),
        *(
            CalcInput.decimal(
                f"level:{lv.on.isoformat()}", lv.level, "level", sim.series_id,
                as_of=lv.observed_at,
            )
            for lv in sim.levels_used
        ),
    ]  # fmt: skip


def record_figure(
    figure: Figure | Unavailable,
    inputs: Sequence[CalcInput],
    *,
    as_of: datetime,
    computed_at: datetime,
    method: str | None = None,
    unit: str = "ratio",
    coverage: Literal["complete", "partial"],
) -> CalculationRecord:
    """Record a T018 result. `method` is required for a bare `Unavailable` and must
    match the figure's own method otherwise."""
    ins, rounding, warnings = list(inputs), RATIO_ROUNDING, list[str]()
    outcome: BoundedDecimal | Money | Unavailable
    if isinstance(figure, Unavailable):
        if method is None:
            raise ValueError("an Unavailable figure needs its method")
        own, outcome = method, figure
    elif isinstance(figure, PeriodReturn | ApproximateReturn):
        own, outcome = figure.method, figure.value
        if isinstance(figure, ApproximateReturn):
            warnings += ["approximate", f"weighting:{figure.weighting}"]
    elif isinstance(figure, FxAttribution):
        own, outcome = figure.method, figure.reporting
    elif isinstance(figure, MoneyWeightedReturn):
        own = figure.method
        warnings += [*figure.warnings, f"uniqueness_rule:{figure.uniqueness_rule}"]
        if figure.period_return is None:
            outcome = Unavailable("ambiguous", "no unique root")
            warnings += [
                f"candidate_period_return:{r.period.to_wire()}" for r in figure.roots
            ]
        else:
            outcome = figure.period_return
    elif isinstance(figure, BenchmarkSimulation):
        own, outcome, rounding = figure.method, figure.ending_value, MONEY_FLOOR
        unit = figure.ending_value.currency
        ins += benchmark_inputs(figure)
    else:
        raise TypeError(f"no calculation record for {type(figure).__name__}")
    if method is not None and method != own:
        raise ValueError(f"method {method} does not match the figure's {own}")
    return record_calculation(
        own, ins, outcome, unit=unit, rounding=rounding, as_of=as_of,
        computed_at=computed_at, warnings=warnings, coverage=coverage,
    )  # fmt: skip
