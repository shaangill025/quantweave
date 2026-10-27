"""The optimizer's protected promotion fixture (0051; T050 increment 8; decisions
41, 87), on a qw_assessor connection only (else `forbidden`). `preregister`
stores the plan and the promotion fixture it binds before a logged protocol names
the plan (`plan_frozen`); it refuses `not_promotion`, `answer_key_changed`,
`evidence_class` and, for a plan holding another fixture, `fixture_exists` (an
identical one writes nothing). `load` rebuilds, re-encodes and re-hashes both
(`integrity`), or returns None: never a substitute. `fixture_wire` and
`fixture_from_wire` are the fixture's strict JSON form. LIMITATION: hash-only."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain import research_improvement as ri
from qw_domain.evidence import SourceKind
from qw_domain.filings import Period
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant, parse_instant
from qw_domain.research_data import EvidenceClass, ResearchError, digest
from qw_domain.trial_ledger import SplitPlan

from qw_adapters import optimizer_store as os_

Conn = psycopg.Connection[Any]
Error = os_.OptimizerStoreError
_FIX = ("fixture_id", "split", "evidence_class", "tenant_id", "loaded_at",
        "evaluated_at", "docs", "cases")  # fmt: skip
_BAD = (ValueError, TypeError, KeyError, IndexError, InvalidOperation, ResearchError)


def fixture_wire(f: ri.Fixture) -> dict[str, object]:
    return {
        "fixture_id": f.fixture_id, "split": f.split.value,
        "evidence_class": f.evidence_class.value, "tenant_id": f.tenant_id,
        "loaded_at": format_instant(f.loaded_at),
        "evaluated_at": format_instant(f.evaluated_at),
        "docs": [[d.doc_id, d.feed_id, d.kind.value, d.entity.to_wire(), d.attribute,
                  d.period.to_wire(), d.unit, d.text, d.published_on.isoformat(),
                  str(d.value)] for d in f.docs],
        "cases": [[c.case_id, c.entity.to_wire(), c.attribute, c.period.to_wire(),
                   format_instant(c.as_of),
                   str(c.max_age // timedelta(microseconds=1)),
                   None if c.answer is None else str(c.answer), sorted(c.refs)]
                  for c in f.cases],
    }  # fmt: skip


def _num(x: object) -> Decimal:
    if type(x) is not str or not (d := Decimal(x)).is_finite():
        raise ValueError("a finite decimal string")
    return d


def _s(*xs: object) -> bool:
    if not all(type(x) is str for x in xs):
        raise TypeError("strings")
    return True


def _us(x: object) -> timedelta:
    if type(x) is not str or not (x.isascii() and x.isdigit()):
        raise ValueError("a microsecond count")
    return timedelta(microseconds=int(x))


def fixture_from_wire(w: object) -> ri.Fixture:
    """The strict inverse of `fixture_wire`: anything else is a ValueError."""
    if type(w) is not dict or sorted(w) != sorted(_FIX):
        raise ValueError("fixture keys")
    if type(w["docs"]) is not list or type(w["cases"]) is not list:
        raise ValueError("fixture lists")
    _s(w["fixture_id"], w["tenant_id"])
    docs = tuple(ri.Doc(i, feed, SourceKind(k), InstrumentId.from_wire(e), a,
                        Period.from_wire(p), u, t, date.fromisoformat(pub), _num(v))
                 for i, feed, k, e, a, p, u, t, pub, v in w["docs"]
                 if _s(i, feed, a, u, t))  # fmt: skip
    cases = tuple(ri.Case(i, InstrumentId.from_wire(e), a, Period.from_wire(p),
                          parse_instant(at), _us(us),
                          None if ans is None else _num(ans), frozenset(refs))
                  for i, e, a, p, at, us, ans, refs in w["cases"]
                  if _s(i, a, *refs))  # fmt: skip
    f = ri.Fixture(w["fixture_id"], ri.Split(w["split"]),
                   EvidenceClass(w["evidence_class"]), w["tenant_id"],
                   parse_instant(w["loaded_at"]), parse_instant(w["evaluated_at"]),
                   docs, cases)  # fmt: skip
    if fixture_wire(f) != w:
        raise ValueError("fixture wire is not canonical")
    return f


def fixture_hash(installation: str, plan_id: str, body: object, at: datetime) -> str:
    return digest(["optimizer_promotion_fixture", installation, plan_id, body,
                   format_instant(at)])  # fmt: skip


def _database(exc: psycopg.Error) -> Error:
    msg = (exc.diag.message_primary or "").rpartition(": ")[2]
    if isinstance(exc, psycopg.errors.CheckViolation) and msg == "plan_frozen":
        return Error(msg)
    if isinstance(exc, psycopg.errors.UniqueViolation):
        return Error("fixture_exists", type(exc).__name__)
    return os_._database(exc)


def _rows(conn: Conn, installation_id: str, plan_id: str) -> list[Any]:
    try:
        return conn.execute(
            "SELECT body, fixture_hash, recorded_at, event_hash FROM "
            "app.optimizer_promotion_fixture WHERE installation_id = %s AND "
            "plan_id = %s", (installation_id, plan_id)).fetchall()  # fmt: skip
    except psycopg.Error as exc:
        raise _database(exc) from None


def load(conn: Conn, installation_id: str, plan_id: str,
         ) -> tuple[SplitPlan, ri.Fixture] | None:  # fmt: skip
    """The plan's stored promotion fixture, rebuilt and re-hashed; None if none."""
    rows = _rows(conn, installation_id, plan_id)
    if not rows:
        return None
    body, h, at, ev = rows[0]
    try:
        at = ensure_aware_utc(at)
        plan, fixture = SplitPlan.from_wire(body["plan"]), fixture_from_wire(
            body["fixture"])  # fmt: skip
        if (plan.to_wire() != body["plan"] or plan.plan_id != plan_id
                or not fixture.content_hash == plan.protocol_hash == h
                or fixture.split is not ri.Split.PROMOTION
                or ev != fixture_hash(installation_id, plan_id, body, at)):  # fmt: skip
            raise ValueError(plan_id)
    except _BAD as exc:
        raise Error("integrity", type(exc).__name__) from None
    return plan, fixture


def preregister(conn: Conn, installation_id: str, plan: SplitPlan,
                fixture: ri.Fixture) -> bool:  # fmt: skip
    """Preregister `fixture` for `plan` (qw_assessor); True when written."""
    if type(plan) is not SplitPlan or type(fixture) is not ri.Fixture:
        raise TypeError("a SplitPlan and a research Fixture")
    if fixture.split is not ri.Split.PROMOTION:
        raise Error("not_promotion", fixture.fixture_id)
    if fixture.content_hash != plan.protocol_hash:
        raise Error("answer_key_changed", fixture.fixture_id)
    if fixture.evidence_class is not plan.dataset.evidence_class:
        raise Error("evidence_class", fixture.evidence_class.value)
    body = {"plan": plan.to_wire(), "fixture": fixture_wire(fixture)}
    with conn.transaction():
        at = os_._now(conn, f"optimizer:{installation_id}")
        if _rows(conn, installation_id, plan.plan_id):
            if load(conn, installation_id, plan.plan_id) == (plan, fixture):
                return False
            raise Error("fixture_exists", plan.plan_id)
        h = fixture_hash(installation_id, plan.plan_id, body, at)
        try:
            with conn.transaction():
                conn.execute("INSERT INTO app.optimizer_promotion_fixture "
                             "(installation_id, plan_id, fixture_hash, body, "
                             "recorded_at, event_hash) VALUES (%s, %s, %s, %s, %s, %s)",
                             (installation_id, plan.plan_id, fixture.content_hash,
                              Jsonb(body), at, h))  # fmt: skip
        except psycopg.Error as exc:
            raise _database(exc) from None
    return True
