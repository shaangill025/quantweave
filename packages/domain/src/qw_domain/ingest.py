"""Rights gate and exact JSON loading shared by data ingestion entry points (T023).

- Ingesting provider content stores it (`Use.RETENTION` for `retain_for`) and
  normalizes it into derived records (`Use.DERIVED_DATA`). Both must be allowed by
  `rights.check_use` at the receipt time, or ingestion raises `IngestDenied` with
  every reason; unknown rights deny (T021 fail-closed).
- `load_exact_json` never produces a float: JSON numbers keep their text as `Decimal`
  (integers as `int`), NaN/Infinity and duplicate object keys are refused.
Stdlib only.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from qw_domain.instants import ensure_aware_utc
from qw_domain.rights import DenyReason, Registry, Use, UseScope, check_use

INGEST_USES = (Use.RETENTION, Use.DERIVED_DATA)


class IngestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.detail = code, message


class IngestDenied(PermissionError):
    def __init__(self, feed_id: str, reasons: tuple[DenyReason, ...]) -> None:
        codes = ", ".join(f"{r.code.value}@{r.subject}" for r in reasons)
        super().__init__(f"ingestion of {feed_id} denied: {codes}")
        self.reasons = reasons


@dataclass(frozen=True, slots=True)
class IngestRights:
    """Who ingests which feed, when (server receipt time) and under which terms."""

    registry: Registry
    tenant_id: str
    feed_id: str
    received_at: datetime
    scope: UseScope
    jurisdiction: str
    retain_for: timedelta

    def __post_init__(self) -> None:
        object.__setattr__(self, "received_at", ensure_aware_utc(self.received_at))


def require_ingest(rights: IngestRights) -> str:
    """Allow ingestion or raise `IngestDenied`; returns the feed revision hash."""
    if type(rights) is not IngestRights:
        raise TypeError("ingestion needs IngestRights")
    reasons: list[DenyReason] = []
    feed_hash: str | None = None
    for use in INGEST_USES:
        keep = rights.retain_for if use is Use.RETENTION else None
        decision = check_use(
            rights.registry,
            rights.tenant_id,
            rights.feed_id,
            use,
            rights.received_at,
            scope=rights.scope,
            jurisdiction=rights.jurisdiction,
            retain_for=keep,
        )
        reasons.extend(decision.reasons)
        feed_hash = decision.feed_hash
    if reasons or feed_hash is None:
        raise IngestDenied(rights.feed_id, tuple(reasons))
    return feed_hash


def _no_constant(name: str) -> object:
    raise IngestError("json_constant", f"{name} is not a JSON number")


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise IngestError("duplicate_key", f"JSON key {key[:64]!r} repeats")
        out[key] = value
    return out


def load_exact_json(
    text: str | bytes, error: type[IngestError] = IngestError
) -> object:
    """Parse raw JSON text; failures raise `error` (a subclass of IngestError)."""
    if not isinstance(text, str | bytes):
        raise TypeError("payload must be raw JSON text, never pre-parsed objects")
    try:
        return json.loads(
            text,
            parse_float=Decimal,
            parse_constant=_no_constant,
            object_pairs_hook=_no_duplicates,
        )
    except IngestError as exc:
        raise error(exc.code, exc.detail) from None
    except (ValueError, RecursionError) as exc:  # JSONDecodeError, int digit cap
        raise error("json", f"malformed JSON: {exc}") from None
