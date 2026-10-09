"""Reference FX rates: ingestion, point-in-time lookup and conversion (T023 inc. 2).

Spec §5 (`fx_to_reporting`; Bank of Canada data is a reference input, not an
executable FX price), §6 (time/series/term checks, FX reference age).
- A rate is `rate` units of `quote` per one unit of `base`, as published for one
  series. Base and quote come from a configured `FxSeriesSpec`, never from the
  series name. The spec also fixes the UTC time of the daily observation and the
  publication lag; both are deployment conventions that need qualification (T004).
- Every observation and derived rate is reference-only (`executable` is False).
  `convert` serves valuation and reporting only; no other purpose is accepted.
- `direct` returns the latest observation of one feed published at or before `at`
  (feeds are never mixed); a pair with
  none is `fx_missing` or `fx_not_yet_published`, never 1 or 0. A correction is a
  new observation for the same date with a later publication time.
- Inverse and cross rates exist only when asked for: `inverse(r)` is the exact
  reciprocal 1/r; `cross(a, b)` is the exact product a x b for a.quote == b.base.
  A derived rate keeps its legs: it is observed when its oldest leg was and
  published when its newest leg was, so a stale leg makes it stale.
- `convert` multiplies exactly and rounds once at the money scale (1e-12) by a named
  `Rounding` policy. A rate older than `max_age` or published after `at` blocks it.
LIMITATIONS: no persistence or fetch adapter; no source precedence between feeds
(the caller names the feed); observations carry no tenant and `direct` does not
re-check retention or revocation (persistence must). Stdlib only.
"""

import decimal
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from typing import ClassVar

from qw_domain.decimals import (
    DOMAIN_CONTEXT,
    DecimalValueError,
    FxRate,
    Money,
    MoneyAmount,
    Rounding,
    safe_repr,
)
from qw_domain.ingest import IngestError, IngestRights, load_exact_json, require_ingest
from qw_domain.instants import ensure_aware_utc
from qw_domain.valuation import Unavailable

_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)
_SERIES = re.compile(r"[A-Za-z0-9_]{1,64}", re.ASCII)


class FxError(IngestError):
    pass


def _fail(code: str, value: object) -> FxError:
    return FxError(code, safe_repr(value))


@dataclass(frozen=True, slots=True)
class FxSeriesSpec:
    series_id: str
    base: str
    quote: str
    observed_time: time  # UTC time of day the daily rate refers to
    publication_lag: timedelta  # from observation to publication

    def __post_init__(self) -> None:
        if not _SERIES.fullmatch(self.series_id):
            raise _fail("series", self.series_id)
        if not all(_CURRENCY.fullmatch(c) for c in (self.base, self.quote)):
            raise _fail("currency", (self.base, self.quote))
        if self.base == self.quote or self.observed_time.tzinfo is not None:
            raise _fail("series_spec", self)
        if self.publication_lag < timedelta(0):
            raise _fail("publication_lag", self.publication_lag)


@dataclass(frozen=True, slots=True)
class FxObservation:
    series_id: str
    base: str
    quote: str
    rate: FxRate
    observed_at: datetime
    published_at: datetime
    received_at: datetime
    feed_id: str
    feed_hash: str
    executable: ClassVar[bool] = False


def parse_valet(
    text: str | bytes, rights: IngestRights, specs: Mapping[str, FxSeriesSpec]
) -> tuple[FxObservation, ...]:
    """Parse a Valet-shaped payload {"observations": [{"d": D, series: {"v": "x"}}]}.
    An absent or empty value is no observation; anything else unexpected refuses."""
    feed_hash = require_ingest(rights)
    data = load_exact_json(text, FxError)
    rows = data.get("observations") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise _fail("valet_shape", type(rows).__name__)
    out: dict[tuple[str, date], FxObservation] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("d"), str):
            raise _fail("valet_shape", row)
        try:
            day = date.fromisoformat(row["d"])
        except ValueError:
            raise _fail("date", row["d"]) from None
        for series, cell in row.items():
            if series == "d":
                continue
            spec = specs.get(series)
            if spec is None:
                raise _fail("unknown_series", series)
            value = cell.get("v") if isinstance(cell, dict) else cell
            if value is None or value == "":
                continue
            try:
                rate = FxRate(value) if type(value) is str else None
            except DecimalValueError:
                rate = None
            if rate is None:
                raise _fail("rate", value)
            observed = datetime.combine(day, spec.observed_time, UTC)
            published = observed + spec.publication_lag
            if published > rights.received_at:
                raise _fail("published_after_receipt", (series, day))
            if (series, day) in out:
                raise _fail("duplicate", (series, day))
            out[series, day] = FxObservation(
                series, spec.base, spec.quote, rate, observed, published,
                rights.received_at, rights.feed_id, feed_hash,
            )  # fmt: skip
    return tuple(out.values())


@dataclass(frozen=True, slots=True)
class FxBook:
    observations: tuple[FxObservation, ...] = ()

    def add(self, new: Iterable[FxObservation]) -> "FxBook":
        """Append-only; the same series, date and publication with another rate is
        refused."""
        out = list(self.observations)
        index = {
            (o.feed_id, o.series_id, o.observed_at, o.published_at): o for o in out
        }
        for obs in new:
            if type(obs) is not FxObservation:
                raise TypeError("FxBook holds FxObservation records")
            old = index.get(
                (obs.feed_id, obs.series_id, obs.observed_at, obs.published_at)
            )
            if old is None:
                index[
                    (obs.feed_id, obs.series_id, obs.observed_at, obs.published_at)
                ] = obs
                out.append(obs)
            elif old.rate != obs.rate:
                raise _fail("fx_conflict", (obs.series_id, obs.observed_at))
        return FxBook(tuple(out))


class Derivation(StrEnum):
    DIRECT = "direct"
    INVERSE = "inverse"
    CROSS = "cross"


@dataclass(frozen=True, slots=True)
class ReferenceRate:
    base: str
    quote: str
    exact: Fraction  # quote units per one base unit
    legs: tuple[FxObservation, ...]
    derivation: Derivation
    executable: ClassVar[bool] = False

    @property
    def observed_at(self) -> datetime:
        return min(leg.observed_at for leg in self.legs)

    @property
    def published_at(self) -> datetime:
        return max(leg.published_at for leg in self.legs)


def direct(
    book: FxBook, base: str, quote: str, at: datetime, *, feed_id: str
) -> ReferenceRate | Unavailable:
    """The latest `feed_id` observation of exactly base/quote published by `at`."""
    at = ensure_aware_utc(at)
    want = (feed_id, base, quote)
    pair = [o for o in book.observations if (o.feed_id, o.base, o.quote) == want]
    known = [o for o in pair if o.published_at <= at]
    if not known:
        code = "fx_not_yet_published" if pair else "fx_missing"
        return Unavailable(code, f"{base}/{quote}")
    best = max(known, key=lambda o: (o.observed_at, o.published_at))
    return ReferenceRate(
        base, quote, Fraction(best.rate.value), (best,), Derivation.DIRECT
    )


def inverse(rate: ReferenceRate) -> ReferenceRate:
    flip = {
        Derivation.DIRECT: Derivation.INVERSE,
        Derivation.INVERSE: Derivation.DIRECT,
    }
    exact = Fraction(rate.exact.denominator, rate.exact.numerator)
    kind = flip.get(rate.derivation, Derivation.CROSS)
    return ReferenceRate(rate.quote, rate.base, exact, rate.legs, kind)


def cross(first: ReferenceRate, second: ReferenceRate) -> ReferenceRate:
    """base/via x via/quote = base/quote."""
    if first.quote != second.base or first.base == second.quote:
        raise _fail("cross_legs", (first.base, first.quote, second.base, second.quote))
    exact = first.exact * second.exact
    legs = first.legs + second.legs
    return ReferenceRate(first.base, second.quote, exact, legs, Derivation.CROSS)


class Purpose(StrEnum):
    VALUATION = "valuation"
    REPORTING = "reporting"


def _round(value: Fraction, rounding: Rounding, currency: str) -> Money:
    scaled = value * 10**12
    q, r = divmod(scaled.numerator, scaled.denominator)  # floor
    if r and rounding.mode == decimal.ROUND_HALF_EVEN:
        q += 2 * r > scaled.denominator or (2 * r == scaled.denominator and q % 2 == 1)
    elif r and (rounding.mode == decimal.ROUND_UP) == (q >= 0):
        q += 1  # away from zero for ROUND_UP, toward zero for ROUND_DOWN
    with localcontext(DOMAIN_CONTEXT):
        return Money(MoneyAmount(Decimal(q).scaleb(-12)), currency)


def convert(
    amount: Money,
    rate: ReferenceRate,
    *,
    at: datetime,
    max_age: timedelta,
    purpose: Purpose,
    rounding: Rounding,
) -> Money | Unavailable:
    """Reference conversion for valuation/reporting; never an executable price."""
    if type(purpose) is not Purpose:
        raise _fail("purpose", purpose)
    if type(amount) is not Money or type(rate) is not ReferenceRate:
        raise TypeError("convert needs Money and a ReferenceRate")
    if (
        type(rounding) is not Rounding
        or type(max_age) is not timedelta
        or max_age < timedelta(0)
    ):
        raise TypeError("convert needs a Rounding and a non-negative max_age")
    if amount.currency != rate.base:
        raise _fail("currency", (amount.currency, rate.base))
    at = ensure_aware_utc(at)
    pair = f"{rate.base}/{rate.quote}"
    if rate.published_at > at:
        return Unavailable("fx_not_yet_published", pair)
    if at - rate.observed_at > max_age:
        return Unavailable("fx_stale", pair)
    return _round(Fraction(amount.amount.value) * rate.exact, rounding, rate.quote)
