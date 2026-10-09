"""Aware UTC instants (T008 review C-05). Wire timestamps carry an offset."""

import re
from datetime import UTC, datetime

# C-05 layer 1 with ASCII digits. Used with fullmatch, so `$` cannot match before "\n".
TIMESTAMP_PATTERN = (
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
    r"T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,6})?"
    r"(Z|\+([01][0-9]|2[0-3]):[0-5][0-9]|-(?!00:00)([01][0-9]|2[0-3]):[0-5][0-9])$"
)
_TIMESTAMP = re.compile(TIMESTAMP_PATTERN, re.ASCII)


class InstantError(ValueError):
    """A naive, malformed, unknown-offset or impossible timestamp."""


def parse_instant(text: str) -> datetime:
    """Parse RFC 3339 with an explicit offset into an aware UTC datetime."""
    if not isinstance(text, str):
        raise TypeError(f"timestamp must be a string, not {type(text).__name__}")
    if _TIMESTAMP.fullmatch(text) is None:
        raise InstantError(
            f"timestamp {text!r} is not RFC 3339 with an explicit offset"
        )
    try:  # layer 2: calendar validity (2026-02-30, year 0) and UTC range
        return ensure_aware_utc(datetime.fromisoformat(text))
    except (ValueError, OverflowError) as exc:
        raise InstantError(
            f"timestamp {text!r} is not a valid instant: {exc}"
        ) from None


def ensure_aware_utc(value: datetime) -> datetime:
    """Reject naive datetimes and normalize aware ones to UTC."""
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, not {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise InstantError("naive datetime: an explicit offset is required")
    return value.astimezone(UTC)


def format_instant(value: datetime) -> str:
    """Server wire form: UTC with a `Z` suffix and microseconds only when non-zero."""
    return ensure_aware_utc(value).isoformat().replace("+00:00", "Z")
