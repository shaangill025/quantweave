"""Aware-UTC instants (T008 review C-05, R080). Expected values are hand-computed."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_domain.instants import (
    TIMESTAMP_PATTERN,
    InstantError,
    ensure_aware_utc,
    format_instant,
    parse_instant,
)

PROFILE = settings(derandomize=True, database=None, max_examples=300)

ACCEPTED = {
    "2026-10-08T10:00:00Z": datetime(2026, 10, 8, 10, tzinfo=UTC),
    "2026-10-08T12:30:00+02:30": datetime(2026, 10, 8, 10, tzinfo=UTC),
    "2026-10-08T04:00:00.5-06:00": datetime(2026, 10, 8, 10, 0, 0, 500000, tzinfo=UTC),
    "2026-12-31T23:59:59.999999-01:00": datetime(
        2027, 1, 1, 0, 59, 59, 999999, tzinfo=UTC
    ),
    "2028-02-29T00:00:00+00:00": datetime(2028, 2, 29, tzinfo=UTC),
}
REJECTED = [
    "2026-10-08T10:00:00",  # no offset (AT080)
    "2026-10-08T10:00:00-00:00",  # unknown local offset
    "2026-02-30T00:00:00Z",  # passes the pattern, impossible date
    "2026-02-29T00:00:00Z", "2026-13-01T00:00:00Z", "2026-10-08T24:00:00Z",
    "2026-10-08T23:59:60Z", "2026-10-08T10:00:00.1234567Z", "2026-10-08T10:00:00+25:99",
    "2026-10-08T10:00:00z", "2026-10-08 10:00:00Z", "2026-10-08T10:00Z", "2026-10-08",
    "2026-10-08T10:00:00Z\n", "\u0662026-10-08T10:00:00Z", "0000-01-01T00:00:00Z",
    "0001-01-01T00:00:00+01:00", "not a date", "",
]  # fmt: skip


@pytest.mark.parametrize(("text", "expected"), ACCEPTED.items())
def test_parse_normalizes_to_utc(text: str, expected: datetime) -> None:
    parsed = parse_instant(text)
    assert parsed == expected
    assert parsed.tzinfo is UTC


@pytest.mark.parametrize("text", REJECTED)
def test_parse_rejects(text: str) -> None:
    with pytest.raises(InstantError):
        parse_instant(text)


def test_parse_rejects_non_strings() -> None:
    for value in (1_760_000_000, 1.5, None, datetime(2026, 1, 1, tzinfo=UTC)):
        with pytest.raises(TypeError):
            parse_instant(value)  # type: ignore[arg-type]


def test_ensure_aware_utc() -> None:
    with pytest.raises(InstantError):
        ensure_aware_utc(datetime(2026, 10, 8, 10))  # noqa: DTZ001
    plus5 = datetime(2026, 10, 8, 15, tzinfo=timezone(timedelta(hours=5)))
    assert ensure_aware_utc(plus5) == datetime(2026, 10, 8, 10, tzinfo=UTC)
    assert ensure_aware_utc(plus5).tzinfo is UTC


def test_format_emits_z() -> None:
    assert (
        format_instant(datetime(2026, 10, 8, 10, tzinfo=UTC)) == "2026-10-08T10:00:00Z"
    )
    later = datetime(2026, 10, 8, 3, 0, 0, 120, tzinfo=timezone(timedelta(hours=-7)))
    assert format_instant(later) == "2026-10-08T10:00:00.000120Z"
    with pytest.raises(InstantError):
        format_instant(datetime(2026, 10, 8))  # noqa: DTZ001


offsets = st.integers(min_value=-(23 * 60 + 59), max_value=23 * 60 + 59).map(
    lambda m: timezone(timedelta(minutes=m))
)


@PROFILE
@given(
    st.datetimes(datetime(1900, 1, 2), datetime(9999, 12, 30)),  # noqa: DTZ001
    offsets,
)
def test_property_round_trip(naive: datetime, tz: timezone) -> None:
    aware = naive.replace(tzinfo=tz)
    text = format_instant(aware)
    assert text.endswith("Z")
    assert parse_instant(text) == aware
    assert parse_instant(aware.isoformat()) == aware


@PROFILE
@given(st.text(max_size=40))
def test_property_parse_never_crashes(text: str) -> None:
    try:
        parsed = parse_instant(text)
    except InstantError:
        return
    assert parsed.tzinfo is UTC


def test_schema_format_checker_rejects_offsetless_timestamp() -> None:
    """C-05 layer 1: with rfc3339-validator installed, `date-time` is enforced."""
    # Fail closed if the checker is missing.
    assert "date-time" in FormatChecker.checkers
    format_only = Draft202012Validator(
        {"type": "string", "format": "date-time"}, format_checker=FormatChecker()
    )
    assert format_only.is_valid("2026-10-08T10:00:00Z")
    assert not format_only.is_valid("2026-10-08T10:00:00")
    assert not format_only.is_valid("not a date")
    assert not format_only.is_valid(1_760_000_000)


def test_schema_layers_are_weaker_than_the_parser() -> None:
    """Pattern plus format accept two inputs only the parser rejects (C-05 layer 2)."""
    schema = {"type": "string", "format": "date-time", "pattern": TIMESTAMP_PATTERN}
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert all(validator.is_valid(text) for text in ACCEPTED)
    schema_only = {text for text in REJECTED if validator.is_valid(text)}
    # jsonschema uses re.search, so `$` matches before a final newline; year 1 at
    # +01:00 precedes the UTC range. parse_instant rejects both.
    assert schema_only == {"2026-10-08T10:00:00Z\n", "0001-01-01T00:00:00+01:00"}


def test_utc_overflow_is_an_instant_error() -> None:
    minus5 = timezone(timedelta(hours=-5))
    plus1 = timezone(timedelta(hours=1))
    for value in (
        datetime(9999, 12, 31, 23, tzinfo=minus5),
        datetime(1, 1, 1, tzinfo=plus1),
    ):
        with pytest.raises(InstantError):
            format_instant(value)
        with pytest.raises(InstantError):
            ensure_aware_utc(value)


def test_error_messages_cap_input_repr() -> None:
    with pytest.raises(InstantError) as info:
        parse_instant("2026-10-08T10:00:00" + "x" * 10_000)
    assert len(str(info.value)) < 200
