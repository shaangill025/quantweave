"""Reference FX: rights-gated ingestion, point-in-time lookup, explicit inverse and
cross rates, and valuation/reporting conversion (T023 increment 2; R004 CAD/USD).

SYNTHETIC rates in a Bank-of-Canada-Valet-shaped payload; no network. Series
time-of-day and publication lag are SYNTHETIC conventions, not qualified provider
facts. Expected amounts are hand-computed (checked with bc, scale 30) or come from
numerical_oracles.json (NUM04, read from the file).
"""

import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from ingest_rights_helper import qualified_rights
from qw_domain.decimals import Money, Rounding
from qw_domain.ingest import IngestDenied, IngestRights
from qw_domain.reference_fx import (
    Derivation,
    FxBook,
    FxError,
    FxSeriesSpec,
    Purpose,
    ReferenceRate,
    convert,
    cross,
    direct,
    inverse,
    parse_valet,
)
from qw_domain.rights import Registry, Use
from qw_domain.valuation import Unavailable

FIXTURE = Path(__file__).resolve().parents[3] / "docs/spec/tests/fixtures"
FEED = "feed-synth-fx"
RECEIVED = datetime(2026, 10, 9, 12, tzinfo=UTC)
LAG, AGE = timedelta(hours=1), timedelta(days=4)
SPECS = {
    "FXUSDCAD": FxSeriesSpec("FXUSDCAD", "USD", "CAD", time(20, 30), LAG),
    "FXEURUSD": FxSeriesSpec("FXEURUSD", "EUR", "USD", time(20, 30), LAG),
}


def rights(received: datetime = RECEIVED, missing: Use | None = None) -> IngestRights:
    return qualified_rights(FEED, received, missing)


def valet(*obs: tuple[str, str, str]) -> str:
    rows = ", ".join(f'{{"d": "{d}", "{s}": {{"v": "{v}"}}}}' for d, s, v in obs)
    return f'{{"seriesDetail": {{}}, "observations": [{rows}]}}'


def book(*obs: tuple[str, str, str]) -> FxBook:
    return FxBook().add(parse_valet(valet(*obs), rights(), SPECS))


def published(day: int) -> datetime:  # 2026-10-<day> 20:30Z observation + 1h lag
    return datetime(2026, 10, day, 21, 30, tzinfo=UTC)


def rate(b: FxBook, base: str, quote: str, at: datetime) -> ReferenceRate:
    got = direct(b, base, quote, at, feed_id=FEED)
    assert isinstance(got, ReferenceRate), got
    return got


def conv(
    amount: Money,
    r: ReferenceRate,
    at: datetime,
    rounding: Rounding = Rounding.PROCEEDS,
    purpose: Purpose = Purpose.REPORTING,
) -> Money | Unavailable:
    return convert(amount, r, at=at, max_age=AGE, purpose=purpose, rounding=rounding)


def test_parse_valet_keeps_exact_rate_times_and_reference_only() -> None:
    text = valet(("2026-10-08", "FXUSDCAD", "1.371234567890123456"))
    (obs,) = parse_valet(text, rights(), SPECS)
    assert (obs.base, obs.quote) == ("USD", "CAD")
    assert obs.rate.value == Decimal("1.371234567890123456")
    assert obs.observed_at == datetime(2026, 10, 8, 20, 30, tzinfo=UTC)
    assert (obs.published_at, obs.received_at) == (published(8), RECEIVED)
    assert obs.executable is False and obs.feed_id == FEED and obs.feed_hash
    assert rate(FxBook().add([obs]), "USD", "CAD", RECEIVED).executable is False


def test_lookup_uses_only_published_rates_and_never_invents_pairs() -> None:
    b = book(("2026-10-07", "FXUSDCAD", "1.3700"), ("2026-10-08", "FXUSDCAD", "1.3712"))
    assert rate(b, "USD", "CAD", published(8) - timedelta(microseconds=1)).exact == (
        Decimal("1.3700")
    )
    assert rate(b, "USD", "CAD", published(8)).exact == Decimal("1.3712")
    early = published(7) - timedelta(seconds=1)
    assert direct(b, "USD", "CAD", early, feed_id=FEED) == Unavailable(
        "fx_not_yet_published", "USD/CAD"
    )
    # No implicit inverse, cross or other feed: missing stays missing, never 1 or 0.
    for base, quote, feed in (
        ("CAD", "USD", FEED),
        ("EUR", "CAD", FEED),
        ("USD", "CAD", "feed-other"),
    ):
        got = direct(b, base, quote, published(8), feed_id=feed)
        assert got == Unavailable("fx_missing", f"{base}/{quote}")


def test_convert_hand_values_inverse_cross_and_named_rounding() -> None:
    b = book(("2026-10-08", "FXUSDCAD", "1.3712"), ("2026-10-08", "FXEURUSD", "1.1"))
    at = published(8)
    usdcad = rate(b, "USD", "CAD", at)
    # 1234.56 x 1.3712 = 1692.828672 exactly
    assert conv(Money.of("1234.56", "USD"), usdcad, at) == Money.of(
        "1692.828672", "CAD"
    )
    assert conv(Money.of("-2", "USD"), usdcad, at) == Money.of("-2.7424", "CAD")
    # Inverse is explicit: CAD 100 / 1.3712 = 72.928821470245040840..., rounded once.
    cadusd = inverse(usdcad)
    assert (cadusd.base, cadusd.quote, cadusd.derivation) == (
        "CAD",
        "USD",
        Derivation.INVERSE,
    )
    cad = Money.of("100", "CAD")
    assert conv(cad, cadusd, at) == Money.of("72.928821470245", "USD")
    assert conv(cad, cadusd, at, Rounding.COST) == Money.of("72.928821470246", "USD")
    assert conv(cad, cadusd, at, Rounding.DISPLAY) == Money.of("72.928821470245", "USD")
    assert inverse(cadusd).derivation is Derivation.DIRECT
    # Cross via USD: 1.1 x 1.3712 = 1.50832 CAD per EUR; EUR 10 -> CAD 15.0832.
    eurcad = cross(rate(b, "EUR", "USD", at), usdcad)
    assert (eurcad.base, eurcad.quote, eurcad.exact) == (
        "EUR",
        "CAD",
        Fraction("1.50832"),
    )
    assert conv(Money.of("10", "EUR"), eurcad, at) == Money.of("15.0832", "CAD")
    # CAD -> EUR through both inverses: 250 / (1.3712 x 1.1) = 165.7473215232841...
    cadeur = cross(cadusd, inverse(rate(b, "EUR", "USD", at)))
    assert conv(Money.of("250", "CAD"), cadeur, at) == Money.of(
        "165.747321523284", "EUR"
    )
    with pytest.raises(FxError, match="cross_legs"):
        cross(usdcad, usdcad)
    with pytest.raises(FxError, match="currency"):
        conv(Money.of("1", "EUR"), usdcad, at)


def test_num04_currency_effect_through_reference_conversion() -> None:
    o = next(
        x
        for x in json.loads((FIXTURE / "numerical_oracles.json").read_text())["oracles"]
        if x["id"] == "NUM04"
    )
    local, fx = Decimal(o["inputs"]["local_return"]), Decimal(o["inputs"]["fx_return"])
    r0 = Decimal("1.3712")
    b = book(
        ("2026-10-07", "FXUSDCAD", str(r0)),
        ("2026-10-08", "FXUSDCAD", str(r0 * (1 + fx))),
    )
    start = conv(
        Money.of("100", "USD"), rate(b, "USD", "CAD", published(7)), published(7)
    )
    end = conv(
        Money.of(100 * (1 + local), "USD"),
        rate(b, "USD", "CAD", published(8)),
        published(8),
    )
    assert isinstance(start, Money) and isinstance(end, Money)
    expected = o["expected"]
    assert end.amount.value == start.amount.value * (
        1 + Decimal(expected["base_return"])
    )
    assert local * fx == Decimal(expected["interaction"])


def test_stale_future_and_non_reporting_use_block() -> None:
    b = book(("2026-10-01", "FXUSDCAD", "1.3712"))
    observed = datetime(2026, 10, 1, 20, 30, tzinfo=UTC)
    r = rate(b, "USD", "CAD", observed + AGE)
    usd = Money.of("1", "USD")
    assert conv(usd, r, observed + AGE) == Money.of("1.3712", "CAD")  # exactly max_age
    tick = timedelta(microseconds=1)
    assert conv(usd, r, observed + AGE + tick) == Unavailable("fx_stale", "USD/CAD")
    early = published(1) - timedelta(seconds=1)
    assert conv(usd, r, early) == Unavailable("fx_not_yet_published", "USD/CAD")
    for purpose in ("execution", "order_sizing", "valuation"):  # str, not Purpose
        with pytest.raises(FxError, match="purpose"):
            conv(usd, r, observed + timedelta(1), purpose=purpose)  # type: ignore[arg-type]
    # A cross is as old as its oldest leg.
    eur = rate(book(("2026-10-08", "FXEURUSD", "1.1")), "EUR", "USD", published(8))
    got = conv(Money.of("1", "EUR"), cross(eur, r), published(8))
    assert got == Unavailable("fx_stale", "EUR/CAD")


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (valet(("2026-10-08", "FXGBPCAD", "1.8")), "unknown_series"),
        (valet(("2026-10-09", "FXUSDCAD", "1.3712")), "published_after_receipt"),
        (valet(("2026-10-08", "FXUSDCAD", "0")), "rate"),
        (valet(("2026-10-08", "FXUSDCAD", "-1.3")), "rate"),
        (
            valet(("2026-10-08", "FXUSDCAD", "1.3"), ("2026-10-08", "FXUSDCAD", "1.4")),
            "duplicate",
        ),
        ('{"observations": [{"d": "2026-10-08", "FXUSDCAD": {"v": 1.3712}}]}', "rate"),
        ('{"observations": [{"d": "2026-02-30", "FXUSDCAD": {"v": "1.3"}}]}', "date"),
        ('{"observations": {}}', "valet_shape"),
        ('{"observations": [{"d": "2026-10-08", "FXUSDCAD": {"v": "1.3"', "json"),
    ],
)
def test_bad_fx_payloads_are_refused(text: str, code: str) -> None:
    with pytest.raises(FxError, match=code):
        parse_valet(text, rights(), SPECS)


def test_absent_value_is_missing_not_zero_or_one() -> None:
    text = (
        '{"observations": [{"d": "2026-10-08"}, '
        '{"d": "2026-10-07", "FXUSDCAD": {"v": ""}}]}'
    )
    b = FxBook().add(parse_valet(text, rights(), SPECS))
    assert direct(b, "USD", "CAD", RECEIVED, feed_id=FEED) == Unavailable(
        "fx_missing", "USD/CAD"
    )


def test_corrected_rate_is_a_new_version_and_same_version_conflict_refused() -> None:
    first = book(("2026-10-08", "FXUSDCAD", "1.3712"))
    later = {
        "FXUSDCAD": FxSeriesSpec(
            "FXUSDCAD", "USD", "CAD", time(20, 30), LAG + timedelta(1)
        )
    }
    got = datetime(2026, 10, 10, tzinfo=UTC)
    fix = parse_valet(valet(("2026-10-08", "FXUSDCAD", "1.3715")), rights(got), later)
    b = first.add(fix)
    assert rate(b, "USD", "CAD", published(8)).exact == Decimal("1.3712")
    assert rate(b, "USD", "CAD", published(9)).exact == Decimal("1.3715")
    assert len(b.observations) == 2 and b.add(b.observations) == b
    clash = parse_valet(valet(("2026-10-08", "FXUSDCAD", "1.3799")), rights(), SPECS)
    with pytest.raises(FxError, match="fx_conflict"):
        first.add(clash)


@pytest.mark.parametrize(
    "args",
    [
        ("FXUSDUSD", "USD", "USD", time(20), LAG),
        ("FXUSDCAD", "USD", "cad", time(20), LAG),
        ("FXUSDCAD", "USD", "CAD", time(20, tzinfo=UTC), LAG),
        ("FXUSDCAD", "USD", "CAD", time(20), -LAG),
    ],
)
def test_series_spec_must_be_explicit_and_sane(
    args: tuple[str, str, str, time, timedelta],
) -> None:
    with pytest.raises(FxError):
        FxSeriesSpec(*args)


@pytest.mark.parametrize("missing", [Use.RETENTION, Use.DERIVED_DATA, None])
def test_fx_ingestion_fails_closed_without_rights(missing: Use | None) -> None:
    denied = (
        rights(missing=missing)
        if missing
        else qualified_rights(FEED, RECEIVED, None, Registry())
    )
    with pytest.raises(IngestDenied):
        parse_valet(valet(("2026-10-08", "FXUSDCAD", "1.3")), denied, SPECS)
