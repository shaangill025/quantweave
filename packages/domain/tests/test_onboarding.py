"""MCQ onboarding catalogue and response validation (T019, R016).

SYNTHETIC answers with made-up ids; the catalogue is
docs/spec/config/onboarding_questions.json.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from qw_domain.decimals import Money, Ratio
from qw_domain.onboarding import (
    Catalogue,
    LimitMetric,
    OnboardingError,
    SizingScope,
    load_catalogue,
)

CONFIG = Path(__file__).resolve().parents[3] / "docs/spec/config"


def config() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (CONFIG / "onboarding_questions.json").read_text()
    )
    return data


@pytest.fixture(scope="module")
def cat() -> Catalogue:
    return load_catalogue(config())


def limit(
    metric: str = "stress_loss", value: str = "0.05", **kw: str
) -> dict[str, str]:
    return {
        "metric": metric,
        "value": value,
        "unit": "ratio",
        "denominator": "account_nav",
        "account_id": "acct-1",
        "currency": "CAD",
        "window": "rolling_30d",
        **kw,
    }


def test_catalogue_binds_stable_ids_labels_and_versions(cat: Catalogue) -> None:
    top = [q for q in cat.questions if "." not in q]
    assert top == [f"ONB{i:02d}" for i in range(1, 22)]
    assert cat.version == "1" and cat.response_schema_version == "onb-responses/1"
    assert cat.questions["ONB02"].label == "Residency"
    assert cat.questions["ONB06.need"].parent == "ONB06"
    for drift in ("pop", "swap", "version"):
        bad = config()
        if drift == "pop":
            bad["questions"].pop()
        elif drift == "swap":
            bad["questions"][0], bad["questions"][1] = (
                bad["questions"][1],
                bad["questions"][0],
            )
        else:
            bad["version"] = "2"
        with pytest.raises(OnboardingError, match="catalogue"):
            load_catalogue(bad)


def test_typed_answers_parse_to_domain_values(cat: Catalogue) -> None:
    r = cat.validate(
        {
            "ONB04": ["growth", "capital_preservation"],
            "ONB04.target_return": "0.07",
            "ONB06": "known_dated_need",
            "ONB06.need": {
                "amount": "20000.50",
                "currency": "CAD",
                "date": "2027-03-01",
            },
            "ONB11": ["options", "stocks"],
            "ONB13": [
                {
                    "account_id": "acct-1",
                    "currency": "USD",
                    "sleeve_id": "t",
                    "amount": "900",
                }
            ],
            "ONB14": [limit(), limit("cash_reserve", "500", unit="amount")],
        }
    )
    assert r.selected("ONB04") == (
        "growth",
        "capital_preservation",
    )  # ranked order kept
    assert r.selected("ONB11") == ("options", "stocks")
    assert r.ratio("ONB04.target_return") == Ratio("0.07")
    need = r.dated_need("ONB06.need")
    assert need is not None and need.amount == Money.of("20000.50", "CAD")
    (alloc,) = r.allocations()
    assert alloc.scope == SizingScope(
        "acct-1", "USD", "t"
    ) and alloc.amount == Money.of(900, "USD")
    assert [x.metric for x in r.limits()] == [
        LimitMetric.STRESS_LOSS,
        LimitMetric.CASH_RESERVE,
    ]
    assert r.limits()[1].to_wire()["value"] == "500"


@pytest.mark.parametrize(
    ("qid", "raw", "code"),
    [
        ("ONB03", "EUR", "choice"),
        ("ONB11", ["stocks", "stocks"], "duplicate"),
        ("ONB04", [], "empty"),
        ("ONB05.goal_date", "2027-02-30", "date"),
        ("ONB13", [{"account_id": "a", "currency": "CAD", "amount": 5000}], "type"),
        ("ONB13", [{"account_id": "a", "currency": "CAD", "amount": "1e3"}], "decimal"),
        ("ONB13", [{"account_id": "a", "currency": "CAD", "amount": "0"}], "positive"),
        ("ONB14", [limit(denominator="")], "denominator"),
        ("ONB14", [{k: v for k, v in limit().items() if k != "currency"}], "shape"),
        ("ONB14", [{k: v for k, v in limit().items() if k != "window"}], "shape"),
        ("ONB14", [limit(unit="amount", currency="cad")], "currency"),
        ("ONB14", [limit(unit="percent")], "unit"),
        ("ONB14", [limit("made_up_metric")], "metric"),
        ("ONB14", [limit(value="-0.1")], "positive"),
        ("ONB14", [limit(), limit(value="0.2")], "duplicate"),
        ("ONB99", "x", "unknown_question"),
        ("ONB21", "adopt", "adoption_is_explicit"),
        ("ONB21", "not_sure", "adoption_is_explicit"),
    ],
)
def test_invalid_responses_rejected(
    cat: Catalogue, qid: str, raw: Any, code: str
) -> None:
    answers = {"ONB05": "gt_7y", qid: raw}
    with pytest.raises(OnboardingError) as err:
        cat.validate(answers)
    assert code in err.value.code


def test_hidden_questions_rejected(cat: Catalogue) -> None:
    need = {"amount": "20000", "currency": "CAD", "date": "2027-03-01"}
    hidden: list[dict[str, Any]] = [
        {"ONB06": "none_known", "ONB06.need": need},
        {"ONB06.need": need},  # parent unanswered
        {"ONB06": "not_sure", "ONB06.need": need},
        {"ONB16": "rules_only", "ONB17": ["provider-a"]},
        {"ONB16": "rules_only", "ONB18": {"amount": "20", "currency": "USD"}},
        {"ONB04": ["income"], "ONB04.target_return": "0.08"},
    ]
    for answers in hidden:
        with pytest.raises(OnboardingError, match="hidden_question"):
            cat.validate(answers)
    shown = cat.validate({"ONB16": "ai_enabled", "ONB17": ["provider-a"]})
    assert shown.selected("ONB17") == ("provider-a",)


def test_not_sure_allowed_except_adoption(cat: Catalogue) -> None:
    r = cat.validate({"ONB07": "not_sure", "ONB14": "not_sure"})
    assert r.choice("ONB07") == "not_sure" and r.limits() == ()
