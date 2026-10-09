"""MCQ onboarding catalogue and validated, versioned responses (T019).

Spec §2 ONB01 to ONB21 and `config/onboarding_questions.json` (catalogue version 1).
The config supplies ids and labels; the typed response schemas are versioned here
(`RESPONSE_SCHEMA_VERSION`). Numbers are decimal strings. Amounts carry a currency;
limits carry a denominator, a measurement window and a sizing scope (account,
currency, optional sleeve). Sub-fields such as `ONB06.need` are conditionally visible,
and answering a hidden question is rejected. `not_sure` is accepted except for ONB21:
adoption is never a response, only an explicit receipt (`qw_domain.policy`). Stdlib.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from types import MappingProxyType

from qw_domain.decimals import DecimalValueError, Money, MoneyAmount, Ratio, safe_repr
from qw_domain.scopes import OutsideContext, ScopeKind
from qw_domain.sources import ID_PATTERN

CATALOGUE_VERSION = "1"
RESPONSE_SCHEMA_VERSION = "onb-responses/1"
NOT_SURE = "not_sure"
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)


class OnboardingError(ValueError):
    """A rejected catalogue or response; `code` names the reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class Kind(StrEnum):
    CHOICE = "choice"
    MULTI = "multi"  # unordered selection
    RANKED = "ranked"  # ordered selection, first is the top priority
    IDS = "ids"
    RATIO = "ratio"
    DATE = "date"
    MONEY = "money"
    DATED_MONEY = "dated_money"
    ALLOCATIONS = "allocations"
    LIMITS = "limits"
    ADOPTION = "adoption"


class LimitMetric(StrEnum):
    """`config/risk_policy_draft.json` limits plus the ONB14 extras."""

    CASH_RESERVE = "cash_reserve"
    ISSUER_CONCENTRATION = "issuer_concentration"
    SECTOR_CONCENTRATION = "sector_concentration"
    PLANNED_TRADE_LOSS = "planned_trade_loss"
    STRESS_LOSS = "stress_loss"
    LOSS_PAUSE = "loss_pause"
    POSITION_CONCENTRATION = "position_concentration"
    DRAWDOWN = "drawdown"


@dataclass(frozen=True, slots=True)
class SizingScope:
    account_id: str
    currency: str
    sleeve_id: str | None

    def covers(self, other: "SizingScope") -> bool:
        """An account-wide scope covers each of its sleeves in the same currency."""
        same = (self.account_id, self.currency) == (other.account_id, other.currency)
        return same and self.sleeve_id in (None, other.sleeve_id)

    def __str__(self) -> str:
        return f"{self.account_id}/{self.currency}/{self.sleeve_id or '*'}"


@dataclass(frozen=True, slots=True)
class Limit:
    """A user-entered limit: a Ratio of `denominator` (unit "ratio") or an amount in
    the scope currency (unit "amount")."""

    metric: LimitMetric
    value: Ratio | MoneyAmount
    unit: str
    denominator: str
    window: str
    scope: SizingScope

    def to_wire(self) -> dict[str, str | None]:
        s = self.scope
        return {
            "metric": self.metric.value,
            "value": self.value.to_wire(),
            "unit": self.unit,
            "denominator": self.denominator,
            "account_id": s.account_id,
            "currency": s.currency,
            "sleeve_id": s.sleeve_id,
            "window": self.window,
        }


@dataclass(frozen=True, slots=True)
class Allocation:
    scope: SizingScope
    amount: Money


@dataclass(frozen=True, slots=True)
class DatedAmount:
    amount: Money
    on: date


type AnswerValue = str | tuple[object, ...] | Ratio | date | Money | DatedAmount


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    label: str
    kind: Kind
    choices: tuple[str, ...]
    parent: str | None = None
    visible_when: frozenset[str] = frozenset()


_C, _M = Kind.CHOICE, Kind.MULTI
_SCHEMAS: dict[str, tuple[Kind, str]] = {
    "ONB01": (_C, " ".join(ScopeKind)),
    "ONB02": (_C, "canada united_states other_unknown"),
    "ONB03": (_C, "CAD USD"),
    "ONB04": (
        Kind.RANKED,
        "capital_preservation growth income active_trading learning",
    ),
    "ONB05": (_C, "lt_1y 1y_3y 3y_7y gt_7y"),
    "ONB06": (_C, "none_known periodic known_dated_need uncertain"),
    "ONB07": (
        _C,
        "essential_spending_affected goal_delayed financially_manageable uncertain",
    ),
    "ONB08": (_C, "reduce review hold add unsure"),
    "ONB09": (_C, "beginner stocks_etfs options active_trader"),
    "ONB10": (_C, "monthly weekly daily regular_session"),
    "ONB11": (_M, "stocks etfs options"),
    "ONB12": (_M, "long_term swing_position same_day"),
    "ONB13": (Kind.ALLOCATIONS, ""),
    "ONB14": (Kind.LIMITS, ""),
    "ONB15": (_M, "primary_reporting social"),
    "ONB16": (_C, "rules_only ai_enabled"),
    "ONB17": (Kind.IDS, ""),
    "ONB18": (Kind.MONEY, ""),
    "ONB19": (_M, "email push in_app"),
    "ONB20": (_C, " ".join(OutsideContext)),
    "ONB21": (Kind.ADOPTION, ""),
}
# Conditional visibility: id -> (kind, parent, parent answers that show it).
_CONDITIONAL: dict[str, tuple[Kind, str, str]] = {
    "ONB04.target_return": (Kind.RATIO, "ONB04", "growth active_trading"),
    "ONB05.goal_date": (Kind.DATE, "ONB05", _SCHEMAS["ONB05"][1]),
    "ONB06.need": (Kind.DATED_MONEY, "ONB06", "known_dated_need"),
    "ONB17": (Kind.IDS, "ONB16", "ai_enabled"),
    "ONB18": (Kind.MONEY, "ONB16", "ai_enabled"),
}


@dataclass(frozen=True, slots=True)
class Responses:
    """Validated answers, keyed by question id, under one response schema version."""

    schema_version: str
    answers: Mapping[str, AnswerValue]

    def choice(self, qid: str) -> str | None:
        value = self.answers.get(qid)
        return value if isinstance(value, str) else None

    def selected(self, qid: str) -> tuple[str, ...]:
        return tuple(x for x in self._items(qid) if isinstance(x, str))

    def ratio(self, qid: str) -> Ratio | None:
        value = self.answers.get(qid)
        return value if isinstance(value, Ratio) else None

    def dated_need(self, qid: str) -> DatedAmount | None:
        value = self.answers.get(qid)
        return value if isinstance(value, DatedAmount) else None

    def limits(self) -> tuple[Limit, ...]:
        return tuple(x for x in self._items("ONB14") if isinstance(x, Limit))

    def allocations(self) -> tuple[Allocation, ...]:
        return tuple(x for x in self._items("ONB13") if isinstance(x, Allocation))

    def _items(self, qid: str) -> tuple[object, ...]:
        value = self.answers.get(qid)
        return value if isinstance(value, tuple) else ()


@dataclass(frozen=True, slots=True)
class Catalogue:
    version: str
    response_schema_version: str
    questions: Mapping[str, Question]

    def validate(self, raw: Mapping[str, object]) -> Responses:
        """Parse wire answers (JSON values) and enforce conditional visibility."""
        parsed: dict[str, AnswerValue] = {}
        for qid, value in raw.items():
            if (question := self.questions.get(qid)) is None:
                raise OnboardingError("unknown_question", safe_repr(qid))
            parsed[qid] = _parse(question, value)
        for qid in parsed:
            q = self.questions[qid]
            if q.parent is not None and not _shown(
                q.visible_when, parsed.get(q.parent)
            ):
                raise OnboardingError("hidden_question", f"{qid} is not shown")
        return Responses(self.response_schema_version, MappingProxyType(parsed))


def _shown(when: frozenset[str], parent: AnswerValue | None) -> bool:
    picked = (parent,) if isinstance(parent, str) else parent
    return isinstance(picked, tuple) and any(x in when for x in picked)


def load_catalogue(config: Mapping[str, object]) -> Catalogue:
    """Bind `onboarding_questions.json` to the typed response schemas. Drift in ids,
    order, version or flags is rejected rather than guessed."""
    questions = config.get("questions")
    if config.get("version") != CATALOGUE_VERSION or not isinstance(questions, list):
        raise OnboardingError("catalogue_version", "expected catalogue version 1")
    ids = [q.get("id") if isinstance(q, dict) else None for q in questions]
    if ids != list(_SCHEMAS) or not config.get(
        "not_sure_allowed_except_explicit_adoption"
    ):
        raise OnboardingError("catalogue_ids", "ids must be ONB01..ONB21 in order")
    out: dict[str, Question] = {}
    for q in questions:
        if q.get("answer_version_required") is not True or not isinstance(
            q.get("input"), str
        ):
            raise OnboardingError("catalogue_flags", safe_repr(q.get("id")))
        kind, choices = _SCHEMAS[q["id"]]
        out[q["id"]] = Question(q["id"], q["input"], kind, tuple(choices.split()))
    for qid, (kind, parent, when) in _CONDITIONAL.items():
        label = out[qid].label if qid in out else qid
        out[qid] = Question(qid, label, kind, (), parent, frozenset(when.split()))
    return Catalogue(CATALOGUE_VERSION, RESPONSE_SCHEMA_VERSION, MappingProxyType(out))


def _parse(question: Question, raw: object) -> AnswerValue:
    if question.kind is Kind.ADOPTION:
        raise OnboardingError("adoption_is_explicit", "ONB21 needs an adoption receipt")
    if raw == NOT_SURE:
        return NOT_SURE
    try:
        return _PARSERS[question.kind](question, raw)
    except DecimalValueError as exc:
        raise OnboardingError(f"decimal:{exc.code}", f"{question.id}: {exc}") from None
    except TypeError as exc:
        raise OnboardingError("type", f"{question.id}: {exc}") from None


def _fail(code: str, q: Question, raw: object) -> OnboardingError:
    return OnboardingError(code, f"{q.id}: {safe_repr(raw)}")


def _choice(q: Question, raw: object) -> str:
    if not isinstance(raw, str) or raw not in q.choices:
        raise _fail("choice", q, raw)
    return raw


def _id(q: Question, raw: object) -> str:
    if not isinstance(raw, str) or ID_PATTERN.fullmatch(raw) is None:
        raise _fail("id", q, raw)
    return raw


def _currency(q: Question, raw: object) -> str:
    if not isinstance(raw, str) or _CURRENCY.fullmatch(raw) is None:
        raise _fail("currency", q, raw)
    return raw


def _selection(q: Question, raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise _fail("empty", q, raw)
    items = tuple(_choice(q, x) if q.choices else _id(q, x) for x in raw)
    if len(set(items)) != len(items):
        raise _fail("duplicate", q, raw)
    return tuple(sorted(items)) if q.kind is Kind.MULTI else items


def _obj(
    q: Question, raw: object, need: set[str], allow: frozenset[str] = frozenset()
) -> dict[str, object]:
    if not isinstance(raw, dict) or not need <= set(raw) <= need | allow:
        raise _fail("shape", q, raw)
    return raw


def _positive[T: Ratio | MoneyAmount](q: Question, value: T) -> T:
    if value.value <= 0:
        raise _fail("positive", q, value)
    return value


def _money(q: Question, raw: object) -> Money:
    obj = _obj(q, raw, {"amount", "currency"})
    if not isinstance(obj["amount"], str):
        raise TypeError("amount must be a decimal string")
    amount = _positive(q, MoneyAmount.from_wire(obj["amount"]))
    return Money(amount, _currency(q, obj["currency"]))


def _date(q: Question, raw: object) -> date:
    if (
        not isinstance(raw, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw, re.ASCII) is None
    ):
        raise _fail("date", q, raw)
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise _fail("date", q, raw) from None


def _dated(q: Question, raw: object) -> DatedAmount:
    obj = _obj(q, raw, {"amount", "currency", "date"})
    money = _money(q, {"amount": obj["amount"], "currency": obj["currency"]})
    return DatedAmount(money, _date(q, obj["date"]))


def _scope(q: Question, obj: Mapping[str, object]) -> SizingScope:
    sleeve = obj.get("sleeve_id")
    return SizingScope(
        _id(q, obj["account_id"]),
        _currency(q, obj["currency"]),
        None if sleeve is None else _id(q, sleeve),
    )


_SLEEVE = frozenset({"sleeve_id"})


def _allocations(q: Question, raw: object) -> tuple[Allocation, ...]:
    if not isinstance(raw, list) or not raw:
        raise _fail("empty", q, raw)
    out: list[Allocation] = []
    for item in raw:
        obj = _obj(q, item, {"account_id", "currency", "amount"}, _SLEEVE)
        scope = _scope(q, obj)
        money = _money(q, {"amount": obj["amount"], "currency": scope.currency})
        out.append(Allocation(scope, money))
    if len({a.scope for a in out}) != len(out):
        raise _fail("duplicate", q, "scope")
    return tuple(out)


_LIMIT_KEYS = {"metric", "value", "unit", "denominator", "account_id", "currency"}


def _limits(q: Question, raw: object) -> tuple[Limit, ...]:
    if not isinstance(raw, list):
        raise TypeError("expected a list of limits")
    out: list[Limit] = []
    for item in raw:
        obj = _obj(q, item, _LIMIT_KEYS | {"window"}, _SLEEVE)
        metric, unit, text = obj["metric"], obj["unit"], obj["value"]
        if metric not in set(LimitMetric):
            raise _fail("metric", q, metric)
        if unit not in ("ratio", "amount"):
            raise _fail("unit", q, unit)
        if obj["denominator"] in ("", None):
            raise _fail("denominator", q, "missing")
        if not isinstance(text, str):
            raise TypeError("limit value must be a decimal string")
        value = (
            Ratio.from_wire(text) if unit == "ratio" else MoneyAmount.from_wire(text)
        )
        out.append(
            Limit(
                LimitMetric(str(metric)),
                _positive(q, value),
                str(unit),
                _id(q, obj["denominator"]),
                _id(q, obj["window"]),
                _scope(q, obj),
            )
        )
    if len({(x.metric, x.scope) for x in out}) != len(out):
        raise _fail("duplicate", q, "metric and scope")
    return tuple(out)


def _ratio(q: Question, raw: object) -> Ratio:
    if not isinstance(raw, str):
        raise TypeError("ratio must be a decimal string")
    return Ratio.from_wire(raw)


_PARSERS: dict[Kind, Callable[[Question, object], AnswerValue]] = {
    Kind.CHOICE: _choice,
    Kind.MULTI: _selection,
    Kind.RANKED: _selection,
    Kind.IDS: _selection,
    Kind.RATIO: _ratio,
    Kind.DATE: _date,
    Kind.MONEY: _money,
    Kind.DATED_MONEY: _dated,
    Kind.ALLOCATIONS: _allocations,
    Kind.LIMITS: _limits,
}
