"""SEC-shaped filings and reported facts with point-in-time knowledge (T023 inc. 1).

Spec §6 (SEC EDGAR: publication-aware units/restatements), §14 (publication and
availability timestamps), R071/R098. Pure parsing, normalization and queries.
- Payloads are raw JSON text loaded without floats (`ingest.load_exact_json`) after
  the rights gate (`ingest.require_ingest`) allows retention and derived data.
- A fact's value is the exact JSON number text as `Decimal`, at most 26 integer
  digits and 12 decimals after trailing zeros, counted from the digits without
  context rounding; anything else refuses the payload (never rounded). Zero forms
  such as `0E-30` and `-0` are accepted as zero.
- Units: an allowed ISO currency (`USD`), `shares`, `<currency>/shares` and `pure`.
  Any other unit series is refused and listed in `FactsIngest.refused`, never coerced.
- Publication (knowledge) time: max(submissions `acceptanceDateTime`, filed date D
  at 05:00Z) when the accession is known, so an early or false acceptance can never
  make a fact knowable before its filed date (`accepted_at` is kept for provenance);
  otherwise the conservative bound D + 1 day 05:00Z (at or after the end of D in New
  York under EST or EDT). Acceptance after our receipt, or outside
  [D-4 days 00:00Z, D+1 05:00Z], is refused (`acceptance_filed_mismatch`); the
  lower bound is a sanity check wide enough for next-business-day filing dates after
  weekends and holidays (accepted after 17:30 ET on a Friday before a Monday
  holiday: D is Tuesday) without a qualified EDGAR calendar.
- A CIK maps to an instrument only through `SecurityMaster` links in the `sec_cik`
  namespace; otherwise the resolution is `unknown` (one CIK may be ambiguous).
- `FactBook` is append-only. Each accession reporting a fact key is a version, so a
  restatement never overwrites. `as_of(t, basis)` returns per key the version with
  the latest knowledge time <= t: publication time (`PUBLICATION`, the backtest
  available-data convention) or max(publication, receipt) (`RECEIPT`, what this system
  had). Tied versions with different values are `conflicted`, never guessed.
LIMITATIONS: no persistence or fetch adapter; XBRL `frame` and dimensional facts are
not read; per-concept unit expectations (e.g. EPS in currency/shares) are not checked.
Facts carry no tenant and `as_of` does not re-check retention expiry or rights
revocation: the persistence increment must scope facts by tenant/feed entitlement
and purge or hide them when retention lapses or rights are revoked.
Stdlib only.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum

from qw_domain.decimals import safe_repr
from qw_domain.identity import ProviderId, Resolution, SecurityMaster
from qw_domain.ingest import IngestError, IngestRights, load_exact_json, require_ingest
from qw_domain.instants import InstantError, ensure_aware_utc, parse_instant

SEC_CIK_NAMESPACE = "sec_cik"
FILED_DATE_BOUND = timedelta(days=1, hours=5)
ACCEPTANCE_BEFORE_FILED = timedelta(days=4)  # sanity check: weekend + holiday
KNOWN_FROM = timedelta(hours=5)  # D 05:00Z = 00:00 EST / 01:00 EDT on the filed date
DEFAULT_CURRENCIES = frozenset({"USD", "CAD"})
_ACCN = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", re.ASCII)
_FORM = re.compile(r"[A-Z0-9][A-Z0-9/-]{0,15}", re.ASCII)
_TAXONOMY = re.compile(r"[a-z][a-z0-9-]{0,31}", re.ASCII)
_CONCEPT = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,255}", re.ASCII)
_FP = re.compile(r"FY|Q[1-4]|H[12]", re.ASCII)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)


class FilingsError(IngestError):
    pass


def _fail(code: str, value: object) -> FilingsError:
    return FilingsError(code, safe_repr(value))


def _obj(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise _fail(f"{what}_shape", type(value).__name__)
    return value


def _text(value: object, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise _fail(what, value)
    return value


def _date(value: object, what: str) -> date:
    try:
        return date.fromisoformat(_text(value, _DATE, what))
    except ValueError:
        raise _fail(what, value) from None


def _cik(value: object) -> str:
    """Submissions carry a 10-digit string, companyfacts an integer."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]{10}", value, re.ASCII):
        value = int(value)
    if type(value) is not int or not 0 < value < 10**10:
        raise _fail("cik", value)
    return f"{value:010d}"


class UnitKind(StrEnum):
    CURRENCY = "currency"
    SHARES = "shares"
    PER_SHARE = "per_share"
    PURE = "pure"


@dataclass(frozen=True, slots=True)
class Unit:
    kind: UnitKind
    currency: str | None
    raw: str


def parse_unit(raw: object, currencies: frozenset[str] = DEFAULT_CURRENCIES) -> Unit:
    if raw == "shares":
        return Unit(UnitKind.SHARES, None, "shares")
    if raw == "pure":
        return Unit(UnitKind.PURE, None, "pure")
    if isinstance(raw, str):
        cur, sep, per = raw.partition("/")
        if _CURRENCY.fullmatch(cur) and cur in currencies:
            if not sep:
                return Unit(UnitKind.CURRENCY, cur, raw)
            if per == "shares":
                return Unit(UnitKind.PER_SHARE, cur, raw)
    raise _fail("unit_unknown", raw)


@dataclass(frozen=True, slots=True, order=True)
class Period:
    """An instant (`start` None) or a duration [start, end] of reported dates."""

    start: date | None
    end: date

    def __post_init__(self) -> None:
        if not all(type(d) is date for d in (self.end, self.start or self.end)):
            raise TypeError("period bounds must be dates")
        if self.start is not None and self.start > self.end:
            raise _fail("period", self)


@dataclass(frozen=True, slots=True)
class Filing:
    cik: str
    accession: str
    form: str
    filing_date: date
    accepted_at: datetime


class PublicationBasis(StrEnum):
    ACCEPTANCE = "acceptance_time"
    FILED_DATE_BOUND = "filed_date_bound"


type FactKey = tuple[str, str, str, str, Period]  # cik, taxonomy, concept, unit, period


@dataclass(frozen=True, slots=True)
class Fact:
    cik: str
    instrument: Resolution
    taxonomy: str
    concept: str
    unit: Unit
    period: Period
    fy: int | None
    fp: str | None
    value: Decimal
    accession: str
    form: str
    filed: date
    published_at: datetime
    publication_basis: PublicationBasis
    received_at: datetime
    feed_id: str
    feed_hash: str
    accepted_at: datetime | None = None  # provenance; may precede `published_at`

    def __post_init__(self) -> None:
        if type(self.value) is not Decimal or not self.value.is_finite():
            raise TypeError("fact value must be a finite Decimal")
        for name in ("published_at", "received_at"):
            object.__setattr__(self, name, ensure_aware_utc(getattr(self, name)))
        if self.accepted_at is not None:
            object.__setattr__(self, "accepted_at", ensure_aware_utc(self.accepted_at))

    @property
    def key(self) -> FactKey:
        return (self.cik, self.taxonomy, self.concept, self.unit.raw, self.period)


@dataclass(frozen=True, slots=True)
class Refusal:
    concept: str
    unit: str
    code: str


@dataclass(frozen=True, slots=True)
class FactsIngest:
    facts: tuple[Fact, ...]
    refused: tuple[Refusal, ...]


def parse_submissions(text: str | bytes, rights: IngestRights) -> tuple[Filing, ...]:
    require_ingest(rights)
    data = _obj(load_exact_json(text, FilingsError), "submissions")
    cik = _cik(data.get("cik"))
    recent = _obj(_obj(data.get("filings"), "filings").get("recent"), "recent")
    names = ("accessionNumber", "filingDate", "acceptanceDateTime", "form")
    cols = [recent.get(n) for n in names]
    lists = [c for c in cols if isinstance(c, list)]
    if len(lists) != len(cols) or len({len(c) for c in lists}) != 1:
        raise _fail("submissions_shape", "missing or unequal columns")
    out: dict[str, Filing] = {}
    for accn, filed, accepted, form in zip(*lists, strict=True):
        try:
            at = parse_instant(accepted)
        except (InstantError, TypeError):
            raise _fail("accepted_at", accepted) from None
        if at > rights.received_at:
            raise _fail("published_after_receipt", accepted)
        filed_on = _date(filed, "filed")
        day = datetime.combine(filed_on, time(), UTC)
        if not day - ACCEPTANCE_BEFORE_FILED <= at <= day + FILED_DATE_BOUND:
            raise _fail("acceptance_filed_mismatch", accepted)
        accn = _text(accn, _ACCN, "accession")
        if accn in out:
            raise _fail("duplicate_accession", accn)
        form = _text(form, _FORM, "form")
        out[accn] = Filing(cik, accn, form, filed_on, at)
    return tuple(out.values())


def _value(raw: object) -> Decimal:
    if type(raw) is int:
        raw = Decimal(raw)
    if type(raw) is not Decimal or not raw.is_finite():
        raise _fail("value", raw)
    _, digits, exponent = raw.as_tuple()  # no context, so nothing rounds
    trailing = len(digits) - len("".join(map(str, digits)).rstrip("0"))
    if raw and (raw.adjusted() >= 26 or int(exponent) + trailing < -12):
        raise _fail("value_bounds", raw)
    return raw


@dataclass(frozen=True, slots=True)
class _Context:
    cik: str
    known: Mapping[str, Filing]
    master: SecurityMaster | None
    rights: IngestRights
    feed_hash: str


def _fact(row: object, ctx: _Context, tax: str, concept: str, unit: Unit) -> Fact:
    r = _obj(row, "fact")
    accn = _text(r.get("accn"), _ACCN, "accession")
    form = _text(r.get("form"), _FORM, "form")
    filed = _date(r.get("filed"), "filed")
    start = None if r.get("start") is None else _date(r["start"], "start")
    fy, fp = r.get("fy"), r.get("fp")
    if fy is not None and (type(fy) is not int or not 1900 <= fy <= 2200):
        raise _fail("fy", fy)
    fp = None if fp is None else _text(fp, _FP, "fp")
    filing = ctx.known.get(accn)
    day = datetime.combine(filed, time(), UTC)
    accepted = None
    if filing is None:
        basis, published = PublicationBasis.FILED_DATE_BOUND, day + FILED_DATE_BOUND
    elif (filing.form, filing.filing_date) != (form, filed):
        raise _fail("filing_mismatch", accn)
    else:
        accepted = filing.accepted_at
        basis, published = PublicationBasis.ACCEPTANCE, max(accepted, day + KNOWN_FROM)
    link = ProviderId(SEC_CIK_NAMESPACE, ctx.cik)
    found = Resolution("unknown")
    if ctx.master is not None:
        found = ctx.master.resolve_provider(link, filed)
    period = Period(start, _date(r.get("end"), "end"))
    value, at, feed = _value(r.get("val")), ctx.rights.received_at, ctx.rights.feed_id
    return Fact(
        ctx.cik, found, tax, concept, unit, period, fy, fp, value, accn, form, filed,
        published, basis, at, feed, ctx.feed_hash, accepted,
    )  # fmt: skip


def parse_companyfacts(
    text: str | bytes,
    rights: IngestRights,
    *,
    filings: Iterable[Filing] = (),
    master: SecurityMaster | None = None,
    currencies: frozenset[str] = DEFAULT_CURRENCIES,
) -> FactsIngest:
    """All-or-nothing, except that a unit series with an unknown unit is refused and
    listed while the other series load."""
    feed_hash = require_ingest(rights)
    data = _obj(load_exact_json(text, FilingsError), "companyfacts")
    cik = _cik(data.get("cik"))
    known = {f.accession: f for f in filings if f.cik == cik}
    ctx = _Context(cik, known, master, rights, feed_hash)
    facts: list[Fact] = []
    refused: list[Refusal] = []
    for tax, concepts in _obj(data.get("facts"), "facts").items():
        for concept, body in _obj(concepts, "concepts").items():
            _text(tax, _TAXONOMY, "taxonomy")
            _text(concept, _CONCEPT, "concept")
            units = _obj(_obj(body, "concept").get("units"), "units")
            for raw_unit, rows in units.items():
                try:
                    unit = parse_unit(raw_unit, currencies)
                except FilingsError as exc:
                    refused.append(Refusal(f"{tax}:{concept}", raw_unit, exc.code))
                    continue
                if not isinstance(rows, list):
                    raise _fail("units_shape", rows)
                facts.extend(_fact(r, ctx, tax, concept, unit) for r in rows)
    return FactsIngest(FactBook().add(facts).facts, tuple(refused))


class KnowledgeBasis(StrEnum):
    PUBLICATION = "publication"  # what was public: the backtest available-data rule
    RECEIPT = "receipt"  # what this system had: published and received


def known_at(fact: Fact, basis: KnowledgeBasis) -> datetime:
    if basis is KnowledgeBasis.PUBLICATION:
        return fact.published_at
    return max(fact.published_at, fact.received_at)


@dataclass(frozen=True, slots=True)
class AsOfView:
    facts: Mapping[FactKey, Fact]
    conflicted: frozenset[FactKey]


@dataclass(frozen=True, slots=True)
class FactBook:
    facts: tuple[Fact, ...] = ()

    def add(self, new: Iterable[Fact]) -> "FactBook":
        """Append versions. A known (key, accession) is kept as first received; the
        same version with another value is refused."""
        out = list(self.facts)
        index = {(f.key, f.accession): f for f in out}
        for fact in new:
            if type(fact) is not Fact:
                raise TypeError("FactBook holds Fact records")
            old = index.get((fact.key, fact.accession))
            if old is None:
                index[(fact.key, fact.accession)] = fact
                out.append(fact)
            elif old.value != fact.value:
                raise _fail("fact_conflict", fact.accession)
        return FactBook(tuple(out))

    def history(self, key: FactKey) -> tuple[Fact, ...]:
        mine = (f for f in self.facts if f.key == key)
        return tuple(sorted(mine, key=lambda f: (f.published_at, f.accession)))

    def as_of(self, at: datetime, basis: KnowledgeBasis) -> AsOfView:
        at = ensure_aware_utc(at)
        if type(basis) is not KnowledgeBasis:
            raise TypeError("as_of needs a KnowledgeBasis")
        latest: dict[FactKey, list[Fact]] = {}
        for fact in self.facts:
            t = known_at(fact, basis)
            if t > at:
                continue
            best = latest.setdefault(fact.key, [fact])
            if best[0] is not fact and t >= (bt := known_at(best[0], basis)):
                latest[fact.key] = [*best, fact] if t == bt else [fact]
        picked = {
            k: max(v, key=lambda f: f.accession)
            for k, v in latest.items()
            if len({f.value for f in v}) == 1
        }
        return AsOfView(picked, frozenset(latest.keys() - picked.keys()))
