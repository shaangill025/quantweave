"""Instrument identity: ids, listings, provider ids and point-in-time resolution.

T011 (R004, T008 C-08/C-27). A ticker is an alias with an effective-dated validity
interval on one exchange (ISO 10383 MIC); it is never an identifier. Intervals are
half-open `[valid_from, valid_to)`: on a rename effective D the old ticker resolves up
to D-1 and the new one from D. Resolution returns `found`, `ambiguous` or `unknown`
and never guesses.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Self
from uuid import UUID

from qw_domain.decimals import safe_repr
from qw_domain.instants import require_date


class IdentityError(ValueError):
    """Malformed identifier or a rejected identity change."""


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MIC = re.compile(r"[A-Z0-9]{4}", re.ASCII)
_TICKER = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,15}", re.ASCII)
_NAMESPACE = re.compile(r"[a-z][a-z0-9_]{0,31}", re.ASCII)
_EXTERNAL = re.compile(r"[\x21-\x7e]{1,128}", re.ASCII)


def _check(pattern: re.Pattern[str], value: object, what: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise IdentityError(f"{what} {safe_repr(value)} is malformed")


@dataclass(frozen=True, slots=True, order=True)
class InstrumentId:
    """Internal canonical instrument id: a UUID, never derived from a ticker."""

    uuid: UUID

    @classmethod
    def from_wire(cls, text: str) -> Self:
        _check(_UUID, text, "instrument id")
        return cls(UUID(text))

    def to_wire(self) -> str:
        return str(self.uuid)


@dataclass(frozen=True, slots=True, order=True)
class Mic:
    """ISO 10383 market identifier code, e.g. XNYS."""

    code: str

    def __post_init__(self) -> None:
        _check(_MIC, self.code, "MIC")


@dataclass(frozen=True, slots=True, order=True)
class Ticker:
    symbol: str

    def __post_init__(self) -> None:
        _check(_TICKER, self.symbol, "ticker")


@dataclass(frozen=True, slots=True)
class ProviderId:
    """An external identifier inside a provider namespace (alpaca, snaptrade, ...)."""

    namespace: str
    external_id: str

    def __post_init__(self) -> None:
        _check(_NAMESPACE, self.namespace, "provider namespace")
        _check(_EXTERNAL, self.external_id, "provider external id")


class _Dated:
    """Half-open validity [valid_from, valid_to); valid_to None is open-ended."""

    valid_from: date
    valid_to: date | None

    def _check_dates(self) -> None:
        require_date(self.valid_from, "valid_from")
        if self.valid_to is not None:
            require_date(self.valid_to, "valid_to")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise IdentityError(f"empty validity interval: {self!r}")

    def valid_on(self, day: date) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day < self.valid_to)

    def dates_overlap(self, other: "_Dated") -> bool:
        return (self.valid_to is None or other.valid_from < self.valid_to) and (
            other.valid_to is None or self.valid_from < other.valid_to
        )


@dataclass(frozen=True, slots=True)
class Listing(_Dated):
    """`ticker` on `mic` names `instrument_id` for dates in [valid_from, valid_to)."""

    instrument_id: InstrumentId
    mic: Mic
    ticker: Ticker
    valid_from: date
    valid_to: date | None = None

    def __post_init__(self) -> None:
        self._check_dates()

    def overlaps(self, other: "Listing") -> bool:
        return self.mic == other.mic and self.dates_overlap(other)


@dataclass(frozen=True, slots=True)
class ProviderLink(_Dated):
    provider_id: ProviderId
    instrument_id: InstrumentId
    valid_from: date
    valid_to: date | None = None

    def __post_init__(self) -> None:
        self._check_dates()


@dataclass(frozen=True, slots=True)
class Resolution:
    status: Literal["found", "ambiguous", "unknown"]
    instrument_id: InstrumentId | None = None
    candidates: tuple[InstrumentId, ...] = ()

    def __post_init__(self) -> None:
        cands, iid = self.candidates, self.instrument_id
        ok = {
            "found": iid is not None and cands == (iid,),
            "ambiguous": iid is None
            and len(cands) >= 2
            and list(cands) == sorted(set(cands)),
            "unknown": iid is None and cands == (),
        }.get(self.status, False)
        if not ok or type(cands) is not tuple:
            raise IdentityError(f"inconsistent resolution: {self!r}")

    @classmethod
    def of(cls, ids: set[InstrumentId]) -> "Resolution":
        if len(ids) == 1:
            return cls.found(next(iter(ids)))
        return cls("ambiguous", None, tuple(sorted(ids))) if ids else cls("unknown")

    @classmethod
    def found(cls, instrument_id: InstrumentId) -> "Resolution":
        return cls("found", instrument_id, (instrument_id,))


@dataclass
class SecurityMaster:
    """In-memory listing and provider-id registry. Every change is all-or-nothing.

    Rejected: a (MIC, ticker) naming two instruments over overlapping dates; one
    instrument with two tickers on one MIC at once; a provider id mapped to two
    instruments over overlapping dates.
    """

    _listings: list[Listing] = field(default_factory=list)
    _links: list[ProviderLink] = field(default_factory=list)

    def listings(self) -> tuple[Listing, ...]:
        return tuple(self._listings)

    def copy(self) -> "SecurityMaster":
        return SecurityMaster(list(self._listings), list(self._links))

    def _conflict(self, new: Listing, ignore: Listing | None = None) -> None:
        for old in self._listings:
            same = old.ticker == new.ticker or old.instrument_id == new.instrument_id
            if old is not ignore and same and old.overlaps(new):
                raise IdentityError(f"listing overlap: {new!r} with {old!r}")

    def add_listing(self, listing: Listing) -> None:
        self._conflict(listing)
        self._listings.append(listing)

    def rename(
        self, instrument_id: InstrumentId, mic: Mic, new: Ticker, effective: date
    ) -> None:
        """Close the open-ended listing at `effective` and list `new` from it."""
        require_date(effective, "effective")
        old = [
            x
            for x in self._listings
            if (x.instrument_id, x.mic, x.valid_to) == (instrument_id, mic, None)
            and x.valid_from < effective
        ]
        if not old:
            raise IdentityError(f"no listing of {instrument_id} on {mic} to rename")
        if old[0].ticker == new:
            raise IdentityError(f"rename of {instrument_id} to the same ticker")
        closed = Listing(
            instrument_id, mic, old[0].ticker, old[0].valid_from, effective
        )
        renamed = Listing(instrument_id, mic, new, effective)
        self._conflict(closed, ignore=old[0])
        self._conflict(renamed, ignore=old[0])
        self._listings[self._listings.index(old[0])] = closed
        self._listings.append(renamed)

    def resolve(self, ticker: Ticker, mic: Mic | None, as_of: date) -> Resolution:
        """Ticker on a MIC (or on any MIC when `mic` is None) as of a date."""
        return Resolution.of(
            {
                x.instrument_id
                for x in self._listings
                if x.ticker == ticker
                and (mic is None or x.mic == mic)
                and x.valid_on(as_of)
            }
        )

    def ticker_as_of(
        self, instrument_id: InstrumentId, mic: Mic, as_of: date
    ) -> Ticker | None:
        """The alias in force (T008 C-27), or None."""
        for x in self._listings:
            if (x.instrument_id, x.mic) == (instrument_id, mic) and x.valid_on(as_of):
                return x.ticker
        return None

    def add_provider_link(self, link: ProviderLink) -> None:
        for old in self._links:
            if old.provider_id == link.provider_id and old.dates_overlap(link):
                raise IdentityError(f"provider id overlap: {link.provider_id!r}")
        self._links.append(link)

    def resolve_provider(self, provider_id: ProviderId, as_of: date) -> Resolution:
        return Resolution.of(
            {
                x.instrument_id
                for x in self._links
                if x.provider_id == provider_id and x.valid_on(as_of)
            }
        )
