"""0019 and feedback_store as qw_app; the superuser `conn` observes or tampers.
SYNTHETIC data, DB-clock times, an in-memory `ImprovementEvidence` oracle."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, NamedTuple

import psycopg
import pytest
from psycopg.rows import TupleRow
from qw_adapters import feedback_store as fs
from qw_adapters import tenancy as tn
from qw_domain import feedback as fb

Conn = psycopg.Connection[TupleRow]
SUBJ = fb.Subject(fb.SubjectKind.SIMULATION, "SIM-syn-1", 1, "a" * 64)
CAT = fb.FeedbackCategory
REC, TERMS, H0 = "installation:synth-1", "terms-v1", "0" * 64
TABLES = ("feedback", "feedback_content", "feedback_consent",
          "feedback_consent_revocation", "feedback_tombstone")  # fmt: skip


class Ctx(NamedTuple):
    tenant: uuid.UUID
    me: uuid.UUID
    other: uuid.UUID


def tenant(conn: Conn) -> Ctx:
    with tn.provision_tenant(conn) as tx:
        ids = []
        for role in (tn.MembershipRole.TENANT_OWNER, tn.MembershipRole.TENANT_MEMBER):
            ids.append(tn.create_user(tx, f"SYNTHETIC {role}"))
            tn.add_membership(tx, ids[-1], role)
    return Ctx(tx.tenant_id, ids[0], ids[1])


@pytest.fixture
def ctx(app_conn: Conn) -> Ctx:
    return tenant(app_conn)


def note(conn: Conn, c: Ctx, key: str = "SYNTHETIC-key-0001", msg: str = "SYNTHETIC "
         "the cash figure is wrong", who: uuid.UUID | None = None,
         cat: fb.FeedbackCategory = CAT.INCORRECT_FACT) -> fb.Feedback:  # fmt: skip
    with tn.tenant_transaction(conn, c.tenant) as tx:
        return fs.record(tx, principal=who or c.me, key=key, subject=SUBJ,
                         category=cat, message=msg)  # fmt: skip


def grant(conn: Conn, c: Ctx, cid: str = "consent-1",
          who: uuid.UUID | None = None) -> fb.ConsentGrant:  # fmt: skip
    with tn.tenant_transaction(conn, c.tenant) as tx:
        return fs.grant(tx, principal=who or c.me, consent_id=cid, recipient=REC,
                        categories=frozenset({fb.RecordKind.FEEDBACK}),
                        terms_version=TERMS)  # fmt: skip


def call(conn: Conn, c: Ctx, fn: Any, *args: Any, **kw: Any) -> Any:
    with tn.tenant_transaction(conn, c.tenant) as tx:
        return fn(tx, *args, **kw)


def revoke(conn: Conn, c: Ctx, cid: str, who: uuid.UUID | None = None) -> Any:
    return call(conn, c, fs.revoke, principal=who or c.me, consent_id=cid)


def delete(conn: Conn, c: Ctx, rid: str, who: uuid.UUID | None = None) -> Any:
    return call(conn, c, fs.delete, principal=who or c.me, record_id=rid)


def load(conn: Conn, t: uuid.UUID) -> fb.ImprovementEvidence:
    with tn.tenant_transaction(conn, t) as tx:
        return fs.load(tx)


def views(ev: fb.ImprovementEvidence, t: uuid.UUID) -> tuple[Any, ...]:
    return ev.export_view(str(t)), ev.tombstones(str(t)), ev.consents(str(t))


def rows(conn: Conn, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM app.{table}").fetchone()[0])  # type: ignore[index]


def db_now(conn: Conn) -> Any:
    return conn.execute("SELECT clock_timestamp()").fetchone()[0]  # type: ignore[index]


def shared(ev: fb.ImprovementEvidence, c: Ctx, at: Any) -> fb.Selection:
    return ev.select(str(c.tenant), "exp-syn-1", fb.Purpose.SHARED_IMPROVEMENT,
                     fb.Access.DEVELOPMENT, at, recipient=REC,
                     terms_version=TERMS)  # fmt: skip


def refused(code: str, fn: Any) -> None:
    with pytest.raises((fs.FeedbackStoreError, fb.FeedbackError)) as caught:
        fn()
    assert getattr(caught.value, "code", None) == code


@pytest.mark.db
def test_round_trip_matches_the_domain_oracle(app_conn: Conn, conn: Conn,
                                              ctx: Ctx) -> None:  # fmt: skip
    before = db_now(conn)
    early = note(app_conn, ctx)
    g = grant(app_conn, ctx)
    late = note(app_conn, ctx, "SYNTHETIC-key-0002", cat=CAT.UNCLEAR)
    revoked_at, stone = revoke(app_conn, ctx, g.consent_id), delete(
        app_conn, ctx, early.record_id)  # fmt: skip
    after = db_now(conn)
    for at in (early.recorded_at, g.granted_at, late.recorded_at, revoked_at,
               stone.deleted_at):  # fmt: skip
        assert before <= at <= after  # the DB clock, not the caller's
    assert early.occurred_at == early.recorded_at and early.principal_id == str(ctx.me)
    assert (g.covers_prior, g.principal_id, g.expires_at) == (False, str(ctx.me), None)
    oracle = fb.ImprovementEvidence()
    oracle.record(early), oracle.grant(g), oracle.record(late)
    oracle.revoke(str(ctx.tenant), g.consent_id, revoked_at)
    oracle.delete(str(ctx.tenant), early.record_id, stone.deleted_at)
    loaded = load(app_conn, ctx.tenant)
    assert views(loaded, ctx.tenant) == views(oracle, ctx.tenant)
    assert views(loaded, ctx.tenant)[0] == ((late,), ())
    assert [rows(conn, t) for t in TABLES] == [2, 1, 1, 1, 1]


@pytest.mark.db
def test_same_key_replays_and_another_body_conflicts(app_conn: Conn, conn: Conn,
                                                     ctx: Ctx) -> None:  # fmt: skip
    first = note(app_conn, ctx)
    again = note(app_conn, ctx)
    assert again == first  # the stored record and its original times
    refused("conflict", lambda: note(app_conn, ctx, msg="SYNTHETIC other text"))
    refused("conflict", lambda: note(app_conn, ctx, cat=CAT.CLEAR))
    other = note(app_conn, ctx, who=ctx.other)  # the key is per principal
    assert other.record_id != first.record_id
    delete(app_conn, ctx, first.record_id)
    refused("deleted", lambda: note(app_conn, ctx))  # a deleted record stays deleted
    assert rows(conn, "feedback") == 2
    refused("message", lambda: note(app_conn, ctx, "SYNTHETIC-key-0003", "a\x00b"))


@pytest.mark.db
def test_consent_is_prospective_per_principal_and_revocation_survives_reload(
    app_conn: Conn, conn: Conn, ctx: Ctx
) -> None:
    before = note(app_conn, ctx)  # made before any consent
    g = grant(app_conn, ctx)
    after = note(app_conn, ctx, "SYNTHETIC-key-0002")
    theirs = note(app_conn, ctx, "SYNTHETIC-key-0003", who=ctx.other)
    sel = shared(load(app_conn, ctx.tenant), ctx, db_now(conn))
    assert [r.record_id for r in sel.records] == [after.record_id]
    assert dict(sel.excluded) == {before.record_id: "recorded_before_consent",
                                  theirs.record_id: "no_consent"}  # fmt: skip
    assert sel.consent_ids == (g.consent_id,)
    refused("forbidden", lambda: revoke(app_conn, ctx, g.consent_id, ctx.other))
    revoke(app_conn, ctx, g.consent_id)
    refused("revoked", lambda: revoke(app_conn, ctx, g.consent_id))
    reloaded = load(app_conn, ctx.tenant)
    later = shared(reloaded, ctx, db_now(conn))
    assert later.records == () and dict(later.excluded)[after.record_id] == "no_consent"
    personal = reloaded.select(str(ctx.tenant), "exp-syn-2",
                               fb.Purpose.PERSONAL_ADAPTATION, fb.Access.DEVELOPMENT,
                               db_now(conn))  # fmt: skip
    assert len(personal.records) == 3  # own-tenant adaptation needs no consent
    with (
        pytest.raises(psycopg.errors.CheckViolation),
        tn.tenant_transaction(conn, ctx.tenant) as tx,
    ):  # never retroactive, even for the superuser
        fs._insert(tx, "feedback_consent", consent_id="c-2", principal_id=ctx.me,
                   purpose="shared_improvement", recipient="r", terms_version="t",
                   data_categories=["feedback"], granted_at=db_now(conn),
                   covers_prior=True, event_hash=H0)  # fmt: skip


@pytest.mark.db
def test_deletion_tombstones_and_removes_content(app_conn: Conn, conn: Conn,
                                                 ctx: Ctx) -> None:  # fmt: skip
    kept, gone = note(app_conn, ctx), note(app_conn, ctx, "SYNTHETIC-key-0002", "wrong")
    salt = conn.execute(
        "SELECT salt FROM app.feedback_content WHERE record_id = %s", (gone.record_id,)
    ).fetchone()[0]  # type: ignore[index]
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="tombstone"):
        call(app_conn, ctx, lambda tx: tx.conn.execute(
            "DELETE FROM app.feedback_content"))  # fmt: skip
    refused("forbidden", lambda: delete(app_conn, ctx, gone.record_id, ctx.other))
    refused("not_found", lambda: delete(app_conn, ctx, "fb-unknown"))
    delete(app_conn, ctx, gone.record_id)
    held = conn.execute("SELECT record_id, message FROM app.feedback_content")
    assert held.fetchall() == [(kept.record_id, kept.message)]  # the text is gone
    [retained] = conn.execute(
        "SELECT record_hash FROM app.feedback WHERE record_id = %s", (gone.record_id,)
    ).fetchone() or [""]
    assert fs._record_hash(gone, "SYNTHETIC-key-0002", salt) == retained  # S1: only
    for guess in ("", H0):  # with the salt, which is gone with the content
        assert fs._record_hash(gone, "SYNTHETIC-key-0002", guess) != retained
    assert salt not in str([conn.execute(f"SELECT * FROM app.{t}").fetchall()
                            for t in TABLES])  # fmt: skip
    refused("deleted", lambda: delete(app_conn, ctx, gone.record_id))
    assert load(app_conn, ctx.tenant).export_view(str(ctx.tenant)) == ((kept,), ())
    for who, why in ((ctx.me, "disagree"), (ctx.other, "own principal")):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match=why):
            stone = fb.Tombstone(str(ctx.tenant), kept.record_id, db_now(conn))
            call(app_conn, ctx, fs._insert_tombstone, who, stone, H0)
    g = grant(app_conn, ctx)  # nor may another principal revoke a consent
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="own principal"):
        call(app_conn, ctx, fs._insert, "feedback_consent_revocation",
             consent_id=g.consent_id, principal_id=ctx.other,
             revoked_at=db_now(conn), event_hash=H0)  # fmt: skip


@pytest.mark.db
def test_restore_reapplies_tombstones_over_restored_content(
    app_conn: Conn, conn: Conn, ctx: Ctx
) -> None:
    """R052: content an older backup brings back stays unreadable and is removed."""
    gone = note(app_conn, ctx)
    [old] = conn.execute("SELECT * FROM app.feedback_content").fetchall()
    delete(app_conn, ctx, gone.record_id)
    conn.execute("SET session_replication_role = replica")  # a restored old copy
    conn.execute("INSERT INTO app.feedback_content VALUES (%s, %s, %s, %s, %s, %s, "
                 "%s, %s, %s, %s)", old)  # fmt: skip
    conn.execute("RESET session_replication_role")
    loaded = load(app_conn, ctx.tenant)
    assert loaded.export_view(str(ctx.tenant)) == ((), ())
    assert [s.record_id for s in loaded.tombstones(str(ctx.tenant))] == [gone.record_id]
    assert call(app_conn, ctx, fs.restore) == [gone.record_id]
    assert rows(conn, "feedback_content") == 0
    assert call(app_conn, ctx, fs.restore) == []


@pytest.mark.db
def test_tenants_are_isolated_even_for_a_superuser_load(
    app_conn: Conn, conn: Conn, ctx: Ctx
) -> None:
    b = tenant(app_conn)
    mine = note(app_conn, ctx)
    g = grant(app_conn, ctx)
    note(app_conn, b, msg="SYNTHETIC tenant B text")
    grant(app_conn, b)
    assert views(load(app_conn, ctx.tenant), ctx.tenant)[0] == ((mine,), ())
    assert rows(conn, "feedback") == 2  # the superuser bypasses RLS ...
    su = load(conn, ctx.tenant)  # ... and the store's own tenant filter still holds
    assert views(su, ctx.tenant) == views(load(app_conn, ctx.tenant), ctx.tenant)
    assert views(su, b.tenant) == (((), ()), (), ())
    assert [x.consent_id for x, _ in su.consents(str(ctx.tenant))] == [g.consent_id]
    refused("not_found", lambda: delete(app_conn, b, mine.record_id))
    refused("not_found", lambda: revoke(app_conn, ctx, "consent-unknown"))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # A's member, B's tenant
        note(app_conn, b, "SYNTHETIC-key-0009", who=ctx.me)


@pytest.mark.db
def test_tables_are_append_only_and_times_follow_the_db_clock(
    app_conn: Conn, conn: Conn, ctx: Ctx
) -> None:
    f = note(app_conn, ctx)
    g = grant(app_conn, ctx)
    revoke(app_conn, ctx, g.consent_id)
    delete(app_conn, ctx, f.record_id)
    note(app_conn, ctx, "SYNTHETIC-key-0002")
    for table in TABLES:
        statements = [f"UPDATE app.{table} SET tenant_id = tenant_id"]
        if table != "feedback_content":
            statements.append(f"DELETE FROM app.{table}")
        for statement in statements:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app_conn.execute(statement)
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                conn.execute(statement)  # the superuser too
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # server-assigned
        app_conn.execute("INSERT INTO app.feedback (retention_class) VALUES ('x')")
    for shift in (timedelta(seconds=-61), timedelta(seconds=5)):
        with (
            pytest.raises(psycopg.errors.CheckViolation, match="database clock"),
            tn.tenant_transaction(app_conn, ctx.tenant) as tx,
        ):
            late = fb.Tombstone(str(ctx.tenant), "x", db_now(tx.conn) + shift)
            fs._insert_tombstone(tx, ctx.me, late, H0)
    with (
        pytest.raises(psycopg.errors.CheckViolation, match="previous"),
        tn.tenant_transaction(app_conn, ctx.tenant) as tx,
    ):
        old = db_now(tx.conn) - timedelta(seconds=30)  # backdated, in the window
        fs._insert(tx, "feedback_consent", consent_id="c-old", principal_id=ctx.me,
                   purpose="shared_improvement", recipient="r",
                   data_categories=["feedback"], terms_version="t", granted_at=old,
                   event_hash=H0)  # fmt: skip
    assert [rows(conn, t) for t in TABLES] == [2, 1, 1, 1, 1]


@pytest.mark.db
@pytest.mark.parametrize("tamper", [
    "UPDATE app.feedback_content SET message = 'SYNTHETIC forged'",
    "UPDATE app.feedback_content SET subject_version = 2",
    "UPDATE app.feedback SET principal_id = %(other)s",
    "UPDATE app.feedback_consent SET recipient = 'elsewhere'",
    "UPDATE app.feedback_consent_revocation SET revoked_at = revoked_at + "
    "interval '1 day'",
    "UPDATE app.feedback_tombstone SET deleted_at = deleted_at - interval '1 hour'",
    "DELETE FROM app.feedback_content",  # text gone without a tombstone
    "DELETE FROM app.feedback WHERE record_id IN (SELECT record_id FROM "
    "app.feedback_tombstone)",  # an orphaned tombstone
])  # fmt: skip
def test_tampering_fails_load_closed(app_conn: Conn, conn: Conn, ctx: Ctx,
                                     tamper: str) -> None:  # fmt: skip
    gone = note(app_conn, ctx)
    g = grant(app_conn, ctx)
    note(app_conn, ctx, "SYNTHETIC-key-0002")
    revoke(app_conn, ctx, g.consent_id)
    delete(app_conn, ctx, gone.record_id)
    assert load(app_conn, ctx.tenant) is not None
    conn.execute("SET session_replication_role = replica")  # a tampering owner
    conn.execute(tamper, {"other": ctx.other})
    conn.execute("RESET session_replication_role")
    with pytest.raises(fs.FeedbackStoreError, match="integrity"):
        load(app_conn, ctx.tenant)
