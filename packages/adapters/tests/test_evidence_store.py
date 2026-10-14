"""Evidence store and claim graph (0012_evidence, qw_adapters.evidence_store) on
PostgreSQL 16, as the non-superuser qw_app/qw_worker logins of the conftest harness.
The superuser `conn` only observes or plays a tampering owner where a test says so.
Every source, passage, claim, feed and tenant is SYNTHETIC; feeds are qualified
through the real T021 registry with SYNTHETIC evidence. Expectations are
hand-written from the scenario below, not computed by the store."""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from ingest_rights_helper import KEEP, REGION, UNTIL, qualified_registry
from psycopg import errors
from psycopg.rows import TupleRow
from qw_adapters import evidence_store as es
from qw_adapters import tenancy as tn
from qw_domain.evidence import (
    ClaimKind,
    EvidenceStore,
    Invalid,
    Passage,
    Reading,
    Snapshot,
    SourceKind,
    Status,
)
from qw_domain.filings import Period
from qw_domain.identity import InstrumentId
from qw_domain.ingest import IngestRights
from qw_domain.rights import Registry, Use, UseScope

Conn = psycopg.Connection[TupleRow]
T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)  # receipt; db tests: clock_relative
E1 = InstrumentId(uuid.UUID("00000000-0000-4000-8000-00000000e001"))
FY25 = Period(date(2025, 1, 1), date(2025, 12, 31))
REV = "us-gaap:Revenues"
RELEASE = "SYNTHETIC release: Revenues were 100 USD for FY2025."
WIRE = "SYNTHETIC   RELEASE:  revenues were 100 usd for fy2025."  # same origin
NOTE = "SYNTHETIC filing note: total revenue 100 USD in fiscal 2025."
MEMO = "SYNTHETIC memo: revenue 100 USD."  # kept one day only
BLOG = "SYNTHETIC independent estimate: revenue 120 USD."


@pytest.fixture(autouse=True)
def clock_relative(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For db tests, receipt (T0) is 30 days before the database clock and grants end
    ten years after it, since the triggers compare retention with now(): the memo
    (kept one day) has expired and the KEEP (365 days) sources are retained on any
    date. Pure tests keep the fixed T0."""
    if request.node.get_closest_marker("db") is None:
        return
    row = request.getfixturevalue("conn").execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    now: datetime = row[0].astimezone(UTC).replace(microsecond=0)
    monkeypatch.setitem(globals(), "T0", now - timedelta(days=30))
    monkeypatch.setitem(globals(), "UNTIL", now + timedelta(days=3650))


def h(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def reading(value: str) -> Reading:
    return Reading(E1, REV, FY25, "USD", Decimal(value))


def registry(tenant: str, missing: Use | None = None) -> Registry:
    return qualified_registry(("feed-a", "feed-b"), missing, tenant, UNTIL)


def scenario(tenant: str) -> EvidenceStore:
    """c-1 (100 USD) cites the release, the note and the memo; c-2 (120 USD)
    cites an independent estimate, so the key is contested until c-1 is preferred
    at hour 3. The wire copy (feed-b) is deleted at hour 4, the note withdrawn at
    hour 5, and the memo's text purged after its one-day retention."""
    reg, s, day, primary = registry(tenant), EvidenceStore(), timedelta(1), "primary"
    for sid, text, quote, value, feed, keep, kind in (
        ("s-release", RELEASE, RELEASE, "100", "feed-a", KEEP, primary),
        ("s-wire", WIRE, WIRE, "100", "feed-b", KEEP, primary),
        ("s-note", NOTE, "total revenue 100 USD", "100", "feed-a", KEEP, primary),
        ("s-memo", MEMO, MEMO, "100", "feed-a", day, primary),
        ("s-blog", BLOG, "revenue 120 USD", "120", "feed-a", KEEP, "independent"),
    ):
        rights = IngestRights(reg, tenant, feed, T0, UseScope.PERSONAL, REGION, keep)
        s.add_source(rights, sid, SourceKind(kind), text, T0)
        pid = "p-" + sid[2:]
        s.add_passage(tenant, pid, sid, text.index(quote), quote, reading(value), T0)
    s.state_claim(tenant, "c-1", ClaimKind.REPORTED_FACT, reading("100"), h(1))
    s.state_claim(tenant, "c-2", ClaimKind.REPORTED_FACT, reading("120"), h(1))
    for pid in ("p-release", "p-wire", "p-note", "p-memo"):
        s.cite(tenant, "c-1", pid, h(2))
    s.cite(tenant, "c-2", "p-blog", h(2))
    members = s.contradictions(tenant, h(3), reg)[0].members
    s.resolve(tenant, members, "c-1", "analyst-syn", h(3), reg)
    s.delete(tenant, "s-wire", h(4))
    s.withdraw(tenant, "s-note", h(5))
    s.purge_expired(tenant, T0 + timedelta(days=2))
    return s


def tenant(c: Conn) -> uuid.UUID:
    with tn.provision_tenant(c) as tx:
        return tx.tenant_id


def put(c: Conn, t: uuid.UUID, store: EvidenceStore) -> None:
    with tn.tenant_transaction(c, t) as tx:
        es.save(tx, store)


def get(c: Conn, t: uuid.UUID) -> EvidenceStore:
    with tn.tenant_transaction(c, t) as tx:
        return es.load(tx)


def stored(c: Conn, t: uuid.UUID) -> EvidenceStore:
    """The scenario saved in tenant `t`."""
    store = scenario(str(t))
    put(c, t, store)
    return store


@pytest.mark.db
def test_round_trip_rebuilds_the_same_store(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    a = tenant(app_conn)
    ta, reg, later = str(a), registry(str(a)), T0 + timedelta(days=3)
    original = stored(worker_conn, a)
    loaded = get(app_conn, a)
    assert loaded.snapshot(ta) == original.snapshot(ta)
    one, two = (loaded.support(ta, c, later, reg) for c in ("c-1", "c-2"))
    # c-1: only the release still counts; the deleted wire copy shares its origin.
    assert (one.status, one.independent_sources, one.replay_limited) == (
        Status.SUPPORTED, 1, True)  # fmt: skip
    assert sorted(one.invalid.values()) == sorted(
        [Invalid.DELETED, Invalid.WITHDRAWN, Invalid.RETENTION_EXPIRED])  # fmt: skip
    assert two.status is Status.CONTESTED  # the resolution preferred c-1's value
    assert loaded.support(ta, "c-2", h(2.5), reg).status is Status.CONTESTED
    assert loaded.passage_text(ta, "p-wire") is None  # deleted: text dropped
    assert loaded.passage_text(ta, "p-memo") is None  # purged after retention
    assert loaded.passage_text(ta, "p-note") == "total revenue 100 USD"
    put(app_conn, a, loaded)  # saving again adds nothing
    with pytest.raises(es.EvidenceError, match="time_order"):
        loaded.state_claim(ta, "c-3", ClaimKind.REPORTED_FACT, reading("1"), h(1))
    loaded.state_claim(ta, "c-1", ClaimKind.REPORTED_FACT, reading("101"), later)
    put(app_conn, a, loaded)
    assert get(app_conn, a).claim_at(ta, "c-1", later) is not None
    assert get(app_conn, a).snapshot(ta) == loaded.snapshot(ta)
    # In the tables: no text for the deleted or purged copies, tombstones kept.
    held = conn.execute(
        "SELECT source_id FROM app.evidence_content WHERE tenant_id = %s ORDER BY 1",
        (a,),
    ).fetchall()
    assert [r[0] for r in held] == ["s-blog", "s-note", "s-release"]
    events = conn.execute(
        "SELECT kind, subject FROM app.evidence_event WHERE tenant_id = %s ORDER BY 1",
        (a,),
    ).fetchall()
    note_origin = hashlib.sha256(" ".join(NOTE.casefold().split()).encode())
    assert events == [("deleted", "s-wire"), ("purged", "s-memo"),
                      ("withdrawn", note_origin.hexdigest())]  # fmt: skip
    rows = conn.execute(
        "SELECT count(*) FROM app.evidence_record WHERE tenant_id = %s", (a,)
    ).fetchone()
    assert rows == (5,)  # every passage row, with its hash, remains


@pytest.mark.db
def test_other_tenants_see_nothing_and_cannot_insert(app_conn: Conn) -> None:
    a, b = tenant(app_conn), tenant(app_conn)
    stored(app_conn, a)
    assert get(app_conn, b).snapshot(str(b)) == EvidenceStore().snapshot(str(b))
    with tn.tenant_transaction(app_conn, b) as tx:
        for table in es.TABLES:
            seen = tx.conn.execute(f"SELECT count(*) FROM app.{table}").fetchone()
            assert seen == (0,), table
    with (
        pytest.raises(errors.InsufficientPrivilege, match="row-level security"),
        tn.tenant_transaction(app_conn, a) as tx,
    ):
        tx.conn.execute(
            "INSERT INTO app.claim (tenant_id, id, revision, kind, stated, known_at) "
            "VALUES (%s, 'c-x', 1, 'reported_fact', '{}', %s)",
            (b, T0),
        )


@pytest.mark.db
def test_rows_are_append_only(app_conn: Conn) -> None:
    a = tenant(app_conn)
    stored(app_conn, a)
    for table in es.TABLES:
        for statement in (f"UPDATE app.{table} SET tenant_id = tenant_id",
                          f"TRUNCATE app.{table}"):  # fmt: skip
            with (
                pytest.raises(errors.InsufficientPrivilege),
                tn.tenant_transaction(app_conn, a) as tx,
            ):
                tx.conn.execute(statement)
    # Text with neither a tombstone nor an ended retention cannot be removed.
    with (
        pytest.raises(errors.InsufficientPrivilege, match="no tombstone"),
        tn.tenant_transaction(app_conn, a) as tx,
    ):
        tx.conn.execute("DELETE FROM app.evidence_content WHERE source_id = 's-note'")
    assert get(app_conn, a).passage_text(str(a), "p-release") == RELEASE


@pytest.mark.db
@pytest.mark.parametrize(
    "forge",
    [
        "UPDATE app.evidence_content SET content = replace(content, '100', '900')",
        "UPDATE app.evidence_record SET reading = jsonb_set(reading, '{value}', "
        "'\"900\"') WHERE id = 'p-release'",
        "UPDATE app.evidence_record SET start_offset = 1 WHERE id = 'p-release'",
        "UPDATE app.claim SET stated = jsonb_set(stated, '{value}', '\"120\"') "
        "WHERE id = 'c-1'",
        "UPDATE app.claim_evidence SET linked_at = %(t0)s - interval '1 hour'",
        "UPDATE app.claim_evidence SET linked_at = %(t0)s + interval '4.5 hours' "
        "WHERE evidence_id = 'p-wire'",  # cited after its deletion at hour 4
        "UPDATE app.evidence_source SET origin = repeat('a', 64) "
        "WHERE id = 's-release'",
        "DELETE FROM app.evidence_event WHERE kind = 'deleted'",
        "UPDATE app.claim_resolution SET preferred = 'c-9'",
    ],
)
def test_tampered_rows_fail_load(app_conn: Conn, conn: Conn, forge: str) -> None:
    a = tenant(app_conn)
    stored(app_conn, a)
    conn.execute("SET session_replication_role = replica")  # a tampering owner
    conn.execute(forge, {"t0": T0} if "%(t0)s" in forge else None)
    with pytest.raises(es.EvidenceStoreError, match="integrity"):
        get(app_conn, a)


@pytest.mark.db
def test_hostile_inserts_fail_load(app_conn: Conn) -> None:
    """Rows a hostile qw_app writes past the adapter: a citation whose passage
    states another value, and text re-inserted after its tombstone."""
    a = tenant(app_conn)
    stored(app_conn, a)
    body = json.dumps([str(a), "c-2", 1, "p-release"])  # a well-formed citation id
    with tn.tenant_transaction(app_conn, a) as tx:
        tx.conn.execute(
            "INSERT INTO app.claim_evidence (tenant_id, id, claim_id, revision, "
            "evidence_id, linked_at) VALUES (%s, %s, 'c-2', 1, 'p-release', %s)",
            (a, f"cit-{hashlib.sha256(body.encode()).hexdigest()[:32]}", h(6)),
        )
    with pytest.raises(es.EvidenceStoreError, match="cit-"):
        get(app_conn, a)  # c-2 states 120; the release passage states 100


@pytest.mark.db
def test_text_after_its_tombstone_is_refused(app_conn: Conn, conn: Conn) -> None:
    """qw_app cannot re-insert removed text; an owner bypassing triggers can, and
    then the load fails closed."""
    b = tenant(app_conn)
    stored(app_conn, b)
    sql = ("INSERT INTO app.evidence_content (tenant_id, source_id, content) "
           "VALUES (%s, 's-wire', %s)")  # fmt: skip
    with (
        pytest.raises(errors.InsufficientPrivilege, match="removed"),
        tn.tenant_transaction(app_conn, b) as tx,
    ):
        tx.conn.execute(sql, (b, WIRE))
    conn.execute("SET session_replication_role = replica")  # a tampering owner
    conn.execute(sql, (b, WIRE))
    with pytest.raises(es.EvidenceStoreError, match="removed text held"):
        get(app_conn, b)


@pytest.mark.db
def test_bogus_events_cannot_brick_the_tenant(app_conn: Conn) -> None:
    """The reviewer's round 2 probe: a purge event on retained text, a tombstone
    that leaves its text in place, and backdated events are all refused, so the
    tenant still loads."""
    a = tenant(app_conn)
    stored(app_conn, a)
    release = get(app_conn, a).source(str(a), "s-release")
    event = "INSERT INTO app.evidence_event (tenant_id, kind, subject, at) VALUES "
    for args, why in [
        (("purged", "s-note", h(60)), "retained"),
        (("deleted", "s-note", h(60)), "text held"),  # no DELETE of the text
        (("deleted", "s-note", T0 - timedelta(1)), "before receipt"),
        (("withdrawn", release.origin, T0 - timedelta(1)), "before receipt"),
    ]:
        with (
            pytest.raises(errors.InsufficientPrivilege, match=why),
            tn.tenant_transaction(app_conn, a) as tx,
        ):
            tx.conn.execute(event + "(%s, %s, %s, %s)", (a, *args))
    assert get(app_conn, a).passage_text(str(a), "p-note") == "total revenue 100 USD"


@pytest.mark.db
def test_writers_by_default_take_the_lock_exclusively(
    app_conn: Conn, worker_conn: Conn
) -> None:
    """Two writers loading and saving at once are serialized, not deadlocked."""
    a = tenant(app_conn)
    stored(app_conn, a)
    gate, done = threading.Barrier(2, timeout=2), {}

    def write(name: str, c: Conn) -> None:
        with tn.tenant_transaction(c, a) as tx:
            store = es.load(tx)
            with contextlib.suppress(threading.BrokenBarrierError):
                gate.wait()  # the second writer is waiting on the lock: no barrier
            later = T0 + timedelta(days=4)
            store.state_claim(
                str(a), name, ClaimKind.REPORTED_FACT, reading("5"), later
            )
            es.save(tx, store)
        done[name] = True

    threads = [threading.Thread(target=write, args=(n, c))
               for n, c in (("c-x", app_conn), ("c-y", worker_conn))]  # fmt: skip
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert done == {"c-x": True, "c-y": True}
    assert {"c-x", "c-y"} <= {
        r.claim_id for r in get(app_conn, a).snapshot(str(a)).revisions
    }


@pytest.mark.db
def test_a_stale_store_cannot_overwrite_newer_rows(app_conn: Conn) -> None:
    a = tenant(app_conn)
    stored(app_conn, a)
    first, second = get(app_conn, a), get(app_conn, a)
    later = T0 + timedelta(days=3)
    first.state_claim(str(a), "c-new", ClaimKind.REPORTED_FACT, reading("1"), later)
    put(app_conn, a, first)
    second.state_claim(str(a), "c-other", ClaimKind.REPORTED_FACT, reading("2"), later)
    with pytest.raises(es.EvidenceStoreError, match="integrity"):
        put(app_conn, a, second)
    assert get(app_conn, a).snapshot(str(a)) == first.snapshot(str(a))


def test_reading_json_is_decimal_text() -> None:
    body = es.reading_json(reading("100.50"))
    assert (body["value"], body["start"], es.reading_of(body)) == (
        "100.50", "2025-01-01", reading("100.50"))  # fmt: skip
    for value in (100.5, "NaN", "1e2"):  # a float, a non-finite, a non-canonical
        with pytest.raises(es.EvidenceStoreError, match="integrity"):
            es.reading_of({**body, "value": value})


def _late(snap: Snapshot, sid: str, at: datetime) -> Snapshot:
    p = Passage(snap.tenant_id, "p-late", sid, 0, 5, "0" * 64, reading("100"), at)
    return replace(snap, passages=(*snap.passages, p))


# Live-store chronology a stored snapshot must keep.
FORGERIES: dict[str, Callable[[Snapshot], Snapshot]] = {
    "passage after citation": lambda s: replace(s, passages=tuple(
        replace(p, known_at=h(2.5)) if p.passage_id == "p-release" else p
        for p in s.passages)),
    "passage after removal": lambda s: _late(s, "s-wire", h(4.5)),  # deleted at 4
    "passage after removal ": lambda s: _late(s, "s-memo", T0 + timedelta(3)),
    "resolution before members": lambda s: replace(s, resolutions=(
        replace(s.resolutions[0], at=h(0.5)),)),
    "withdrawn group unknown": lambda s: replace(
        s, withdrawn={**s.withdrawn, "f" * 64: h(6)}),
    "event before receipt": lambda s: replace(
        s, deleted={**s.deleted, "s-wire": T0 - timedelta(1)}),
    "purged before expiry": lambda s: replace(
        s, purged={**s.purged, "s-memo": T0 + timedelta(hours=1)}),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(FORGERIES))
def test_restore_keeps_live_store_chronology(name: str) -> None:
    snap = scenario("tenant-syn").snapshot("tenant-syn")
    assert EvidenceStore.restore(snap).snapshot("tenant-syn") == snap
    with pytest.raises(es.EvidenceError, match=name.strip()):
        EvidenceStore.restore(FORGERIES[name](snap))


@pytest.mark.db
def test_no_passage_or_removal_event_without_held_text(
    app_conn: Conn, conn: Conn
) -> None:
    """The reviewer's reproduction: a qw_app passage stating 999 on the deleted
    copy, cited by a new claim, is refused at insert (its text cannot be checked).
    Removal events must name a source of this tenant whose text is still held."""
    a = tenant(app_conn)
    stored(app_conn, a)
    fake = json.dumps(es.reading_json(reading("999")))
    inserts = [
        ("INSERT INTO app.evidence_record (tenant_id, id, source_id, start_offset, "
         "end_offset, hash, reading, known_at) VALUES (%s, 'p-fake', 's-wire', 0, 5, "
         "%s, %s::jsonb, %s)", ("1" * 64, fake, h(0.5))),
        ("INSERT INTO app.evidence_event (tenant_id, kind, subject, at) "
         "VALUES (%s, 'deleted', 'ghost', %s)", (h(9),)),
        ("INSERT INTO app.evidence_event (tenant_id, kind, subject, at) "
         "VALUES (%s, 'purged', 's-wire', %s)", (h(9),)),
        ("INSERT INTO app.evidence_event (tenant_id, kind, subject, at) "
         "VALUES (%s, 'withdrawn', %s, %s)", ("f" * 64, h(9))),
    ]  # fmt: skip
    for sql, args in inserts:
        with (
            pytest.raises(errors.InsufficientPrivilege, match=r"no held text|no group"),
            tn.tenant_transaction(app_conn, a) as tx,
        ):
            tx.conn.execute(sql, (a, *args))
    # A purge event for text still within retention (planted by an owner past the
    # insert trigger) still cannot remove it: the delete trigger checks the clock.
    conn.execute("SET session_replication_role = replica")
    conn.execute("INSERT INTO app.evidence_event (tenant_id, kind, subject, at) "
                 "VALUES (%s, 'purged', 's-note', %s)", (a, h(9)))  # fmt: skip
    conn.execute("SET session_replication_role = origin")
    with (
        pytest.raises(errors.InsufficientPrivilege, match="no tombstone"),
        tn.tenant_transaction(app_conn, a) as tx,
    ):
        tx.conn.execute("DELETE FROM app.evidence_content WHERE source_id = 's-note'")


@pytest.mark.db
def test_load_is_one_consistent_snapshot(
    app_conn: Conn, worker_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer deleting a source while a reader is between its SELECTs waits for
    the reader (shared lock) instead of tearing its snapshot."""
    a = tenant(app_conn)
    stored(worker_conn, a)
    ta, real = str(a), es._rows
    newer = get(worker_conn, a)
    newer.delete(ta, "s-note", h(100))
    writer = threading.Thread(target=put, args=(worker_conn, a, newer))

    def hooked(tx: tn.TenantTx, sql: str) -> list[tuple[object, ...]]:
        out = real(tx, sql)
        if "evidence_content" in sql and not writer.is_alive():
            writer.start()
            writer.join(0.5)
            assert writer.is_alive()  # blocked until the reader commits
        return out

    monkeypatch.setattr(es, "_rows", hooked)
    assert get(app_conn, a).passage_text(ta, "p-note") == "total revenue 100 USD"
    monkeypatch.setattr(es, "_rows", real)
    writer.join(10)
    assert get(app_conn, a).passage_text(ta, "p-note") is None


@pytest.mark.db
def test_database_failures_are_store_errors(
    app_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(tx: tn.TenantTx, sql: str) -> list[tuple[object, ...]]:
        raise errors.QueryCanceled("SYNTHETIC cancellation")

    a = tenant(app_conn)
    monkeypatch.setattr(es, "_rows", fail)
    for call in (lambda tx: es.load(tx), lambda tx: es.save(tx, scenario(str(a)))):
        with (
            pytest.raises(es.EvidenceStoreError, match="database"),
            tn.tenant_transaction(app_conn, a) as tx,
        ):
            call(tx)


@pytest.mark.db
def test_a_back_dated_withdrawal_fails_closed_at_load(app_conn: Conn) -> None:
    """Accepted by the triggers (dated at receipt), but it precedes existing
    citations, so the load refuses the tenant rather than serving it."""
    a = tenant(app_conn)
    stored(app_conn, a)
    origin = get(app_conn, a).source(str(a), "s-release").origin
    with tn.tenant_transaction(app_conn, a) as tx:
        tx.conn.execute("INSERT INTO app.evidence_event (tenant_id, kind, "
                        "subject, at) VALUES (%s, 'withdrawn', %s, %s)",
                        (a, origin, T0))  # fmt: skip
    with pytest.raises(es.EvidenceStoreError, match="integrity: cit-"):
        get(app_conn, a)
