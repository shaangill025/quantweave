"""0055 and qw_adapters.account_store on PostgreSQL 16 as non-owner qw_app,
qw_worker, qw_assessor and qw_release logins; the superuser `conn` only observes or
tampers. SYNTHETIC tenants, accounts, sources and holdings; recorded times are the
database clock, valid times and snapshot times are fixed."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.types.json import Jsonb
from qw_adapters import account_store as st
from qw_adapters import tenancy as tn
from qw_domain.decimals import Quantity
from qw_domain.identity import InstrumentId
from qw_domain.sources import (
    HoldingLine,
    MappedInterval,
    Source,
    SourceKind,
    SourceSnapshot,
    UnderlyingAccount,
    consolidate,
)

pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
SYNTH = InstrumentId(uuid.UUID("00000000-0000-4000-8000-0000000000a1"))
V0 = datetime(2026, 1, 1, tzinfo=UTC)  # valid (effective) time, not knowledge time
AT = datetime(2026, 10, 1, 15, tzinfo=UTC)


@pytest.fixture
def tenant(app_conn: Conn) -> Callable[[], uuid.UUID]:
    def make() -> uuid.UUID:
        with tn.provision_tenant(app_conn) as tx:
            return tx.tenant_id

    return make


def call(conn: Conn, t: uuid.UUID, fn: str, *args: Any) -> Any:
    with tn.tenant_transaction(conn, t) as tx:
        return getattr(st, fn)(tx, *args)


def code(fn: Callable[[], object]) -> str:
    with pytest.raises(st.AccountStoreError) as err:
        fn()
    return err.value.code


def acct(t: uuid.UUID, aid: str) -> UnderlyingAccount:
    return UnderlyingAccount(aid, str(t), "SYN-BROKER-CO", "margin", "USD")


def seed(conn: Conn, t: uuid.UUID) -> None:
    """SYN-A via a broker export and a Yahoo-style mirror; SYN-B via the broker."""
    for aid in ("SYN-A", "SYN-B"):
        call(conn, t, "register_account", acct(t, aid), f"SYNTHETIC {aid}",
             "complete_declared")  # fmt: skip
    call(conn, t, "add_source", Source("SYN-BRK", str(t), SourceKind.BROKER_EXPORT),
         "syn-broker")  # fmt: skip
    call(conn, t, "add_source", Source("SYN-YAHOO", str(t), SourceKind.MIRROR),
         "yahoo")  # fmt: skip
    for src, ref, aid in (("SYN-BRK", "1001", "SYN-A"), ("SYN-YAHOO", "pf-1", "SYN-A"),
                          ("SYN-BRK", "1002", "SYN-B")):  # fmt: skip
        call(conn, t, "remap", src, ref, (MappedInterval(aid, V0),), 0)


def clock(conn: Conn) -> datetime:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def snap(src: str, ref: str, qty: str) -> SourceSnapshot:
    return SourceSnapshot(src, ref, AT, (HoldingLine(SYNTH, "USD", Quantity(qty)),),
                          (), True)  # fmt: skip


def test_round_trip_retries_and_as_of_loads(app_conn: Conn, tenant: Any) -> None:
    t = tenant()
    assert call(app_conn, t, "load").accounts == {}
    seed(app_conn, t)
    reg = call(app_conn, t, "load")
    assert sorted(reg.accounts) == ["SYN-A", "SYN-B"]
    assert reg.accounts["SYN-A"].account == acct(t, "SYN-A")
    assert reg.accounts["SYN-A"].name == "SYNTHETIC SYN-A"
    assert reg.sources["SYN-YAHOO"].source.kind is SourceKind.MIRROR
    assert reg.sources["SYN-YAHOO"].provider == "yahoo"
    assert [r.revision for r in reg.revisions] == [1, 1, 1]
    assert reg.source_map.resolve("SYN-YAHOO", "pf-1", AT) == acct(t, "SYN-A")
    # the same request again writes nothing; another one under the id is refused
    _, wrote = call(app_conn, t, "register_account", acct(t, "SYN-A"),
                    "SYNTHETIC SYN-A", "complete_declared")  # fmt: skip
    assert not wrote
    renamed = (acct(t, "SYN-A"), "renamed", "complete_declared")
    assert code(lambda: call(app_conn, t, "register_account", *renamed)) == (
        "duplicate"
    )
    assert (
        code(
            lambda: call(
                app_conn,
                t,
                "add_source",
                Source("SYN-YAHOO", str(t), SourceKind.BROKER_API),
                "yahoo",
            )
        )
        == "duplicate"
    )
    # move reference 1001 to SYN-B from AT: revision 2; a retry returns it
    mid = clock(app_conn)
    new = (MappedInterval("SYN-A", V0, AT), MappedInterval("SYN-B", AT))
    r2, wrote = call(app_conn, t, "remap", "SYN-BRK", "1001", new, 1)
    assert (r2.revision, wrote) == (2, True)
    assert call(app_conn, t, "remap", "SYN-BRK", "1001", new[::-1], 1) == (r2, False)
    assert code(lambda: call(app_conn, t, "remap", "SYN-BRK", "1001",
                             new[:1], 1)) == "stale"  # fmt: skip
    after = AT + timedelta(days=1)
    now, then = call(app_conn, t, "load"), call(app_conn, t, "load", mid)
    assert now.source_map.resolve("SYN-BRK", "1001", after).account_id == "SYN-B"
    assert then.source_map.resolve("SYN-BRK", "1001", after).account_id == "SYN-A"
    assert [r.revision for r in then.revisions] == [1, 1, 1]
    assert now.revisions[-1] == r2 and r2.recorded_at > mid
    first = min(r.recorded_at for r in now.accounts.values())
    early = call(app_conn, t, "load", first - timedelta(microseconds=1))
    assert (early.accounts, early.sources, early.revisions) == ({}, {}, ())
    # the domain refuses before anything is written
    assert (
        code(
            lambda: call(
                app_conn,
                t,
                "remap",
                "SYN-BRK",
                "1003",
                (MappedInterval("SYN-NONE", V0),),
                0,
            )
        )
        == "unknown_account"
    )
    assert (
        code(
            lambda: call(
                app_conn, t, "remap", "SYN-NONE", "x", (MappedInterval("SYN-A", V0),), 0
            )
        )
        == "unknown_source"
    )
    overlap = (MappedInterval("SYN-A", V0), MappedInterval("SYN-B", AT))
    assert code(lambda: call(app_conn, t, "remap", "SYN-BRK", "1003",
                             overlap, 0)) == "overlap"  # fmt: skip
    assert call(app_conn, t, "load").revisions == now.revisions


def test_stored_mirror_dedupes_once_and_genuine_accounts_stay_distinct(
    app_conn: Conn, worker_conn: Conn, tenant: Any
) -> None:
    """The T013 verification line on stored data, read back by qw_worker."""
    t = tenant()
    seed(app_conn, t)
    reg = call(worker_conn, t, "load")
    same = [snap("SYN-BRK", "1001", "50"), snap("SYN-YAHOO", "pf-1", "50"),
            snap("SYN-BRK", "1002", "50")]  # fmt: skip
    views = consolidate(reg.source_map, str(t), same, AT, timedelta(days=1)).accounts
    units = {a: v.units.get((SYNTH, "USD")) for a, v in views.items()}
    assert units == {"SYN-A": Quantity("50"), "SYN-B": Quantity("50")}
    total = sum((q.value for q in units.values() if q is not None), Quantity(0).value)
    assert total == Quantity("100").value  # not 150 (mirror added) and not 50
    assert {v.status for v in views.values()} == {"reconciled"}
    assert views["SYN-A"].sources == ("SYN-BRK", "SYN-YAHOO")
    lag = [snap("SYN-BRK", "1001", "50"), snap("SYN-YAHOO", "pf-1", "40")]
    a = consolidate(reg.source_map, str(t), lag, AT, timedelta(days=1)).accounts
    assert a["SYN-A"].units[(SYNTH, "USD")] == Quantity("50")  # broker outranks
    assert a["SYN-A"].status == "conflicted" and a["SYN-A"].sizing_blocked


def test_tenants_are_isolated(app_conn: Conn, tenant: Any) -> None:
    t, b = tenant(), tenant()
    seed(app_conn, t)
    reg = call(app_conn, b, "load")
    assert (reg.accounts, reg.sources, reg.revisions) == ({}, {}, ())
    assert code(lambda: call(app_conn, b, "register_account", acct(t, "SYN-C"),
                             "x", "hypothetical")) == "tenant"  # fmt: skip
    assert (
        code(
            lambda: call(
                app_conn,
                b,
                "remap",
                "SYN-BRK",
                "1001",
                (MappedInterval("SYN-A", V0),),
                0,
            )
        )
        == "unknown_source"
    )
    call(app_conn, b, "add_source", Source("SYN-BRK", str(b), SourceKind.MANUAL), "me")
    assert (
        code(
            lambda: call(
                app_conn,
                b,
                "remap",
                "SYN-BRK",
                "1001",
                (MappedInterval("SYN-A", V0),),
                0,
            )
        )
        == "unknown_account"
    )
    with (
        pytest.raises(errors.CheckViolation, match="intervals"),
        tn.tenant_transaction(app_conn, b) as tx,
    ):  # another tenant's account
        raw_revision(tx, "SYN-BRK", "1001", 1, [iv("SYN-A")])
    with pytest.raises(errors.InsufficientPrivilege, match="row-level security"), \
            tn.tenant_transaction(app_conn, b):  # fmt: skip
        app_conn.execute(
            "INSERT INTO app.data_source (tenant_id, source_id, kind, provider, "
            "content_hash) VALUES (%s, 'SYN-X', 'manual', 'x', %s)",
            (t, "0" * 64),
        )
    assert call(app_conn, t, "load").sources["SYN-BRK"].source.kind is (
        SourceKind.BROKER_EXPORT
    )


def iv(aid: str, start: str = "2026-01-01T00:00:00Z",
       end: str | None = None) -> dict[str, Any]:  # fmt: skip
    return {"account_id": aid, "valid_from": start, "valid_to": end}


def raw_revision(tx: tn.TenantTx, src: str, ref: str, rev: int,
                 ivs: list[dict[str, Any]],
                 previous: str | None = None) -> None:  # fmt: skip
    tx.conn.execute(
        "INSERT INTO app.source_mapping_revision (tenant_id, source_id, account_ref, "
        "revision, intervals, previous_hash, content_hash) VALUES "
        "(%s, %s, %s, %s, %s, %s, %s)",
        (tx.tenant_id, src, ref, rev, Jsonb(ivs), previous, "0" * 64),
    )


def tamper(conn: Conn, sql: str) -> None:
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(sql)


@pytest.mark.parametrize("sql", [
    "UPDATE app.account SET name = 'SYNTHETIC other' WHERE account_id = 'SYN-A'",
    "UPDATE app.account SET reporting_currency = 'CAD' WHERE account_id = 'SYN-B'",
    "UPDATE app.account SET recorded_at = recorded_at - interval '1 second'",
    "UPDATE app.data_source SET kind = 'broker_api' WHERE source_id = 'SYN-YAHOO'",
    "UPDATE app.data_source SET content_hash = row_hash WHERE source_id = 'SYN-BRK'",
    "UPDATE app.source_mapping_revision SET intervals = jsonb_set(intervals, "
    "'{0,account_id}', '\"SYN-B\"') WHERE account_ref = 'pf-1'",
    "UPDATE app.source_mapping_revision SET recorded_at = recorded_at - "
    "interval '1 day' WHERE account_ref = 'pf-1'",
    "DELETE FROM app.source_mapping_revision WHERE account_ref = '1001' "
    "AND revision = 1",
    "UPDATE app.source_mapping_revision SET revision = 3 "
    "WHERE account_ref = '1001' AND revision = 2",
])  # fmt: skip
def test_tamper_is_integrity(app_conn: Conn, conn: Conn, tenant: Any,
                             sql: str) -> None:  # fmt: skip
    t = tenant()
    seed(app_conn, t)
    new = (MappedInterval("SYN-A", V0, AT), MappedInterval("SYN-B", AT))
    call(app_conn, t, "remap", "SYN-BRK", "1001", new, 1)
    call(app_conn, t, "load")
    tamper(conn, sql)
    assert code(lambda: call(app_conn, t, "load")) == "integrity"
    later = (MappedInterval("SYN-B", V0),)
    assert code(lambda: call(app_conn, t, "remap", "SYN-BRK", "1002", later, 1)) == (
        "integrity"
    )


def test_roles_without_grants_are_refused(
    app_conn: Conn, worker_conn: Conn, assessor_conn: Conn, release_conn: Conn,
    tenant: Any,
) -> None:  # fmt: skip
    t = tenant()
    seed(app_conn, t)
    for c in (assessor_conn, release_conn):
        assert code(lambda c=c: call(c, t, "load")) == "forbidden"  # type: ignore[misc]
    assert len(call(worker_conn, t, "load").revisions) == 3
    assert code(lambda: call(worker_conn, t, "register_account", acct(t, "SYN-W"),
                             "x", "hypothetical")) == "forbidden"  # fmt: skip
    one = (MappedInterval("SYN-A", V0),)
    assert code(lambda: call(worker_conn, t, "remap", "SYN-BRK", "1009", one, 0)) == (
        "forbidden"
    )
    for c in (app_conn, worker_conn):
        for stmt in ("UPDATE app.account SET name = 'x'", "DELETE FROM app.account",
                     "TRUNCATE app.data_source",
                     "UPDATE app.source_mapping_revision SET recorded_at = now()",
                     "DELETE FROM app.source_mapping_revision"):  # fmt: skip
            with pytest.raises(errors.InsufficientPrivilege), \
                    tn.tenant_transaction(c, t):  # fmt: skip
                c.execute(stmt)


def test_remap_refuses_bad_arguments_with_named_codes(
    app_conn: Conn, tenant: Any
) -> None:
    t = tenant()
    seed(app_conn, t)
    one = (MappedInterval("SYN-A", V0),)

    def remap(ivs: Any, expected: Any) -> str:
        return code(lambda: call(app_conn, t, "remap", "SYN-BRK", "1009", ivs,
                                 expected))  # fmt: skip

    for bad in (-1, "0", 1.0, True, None):
        assert remap(one, bad) == "expected"
    for ivs in ("SYN-A", [iv("SYN-A")], [one[0], "x"], None, iter(one)):
        assert remap(ivs, 0) == "intervals"
    cap, day = st.MAX_INTERVALS, timedelta(days=1)
    many = [MappedInterval("SYN-A", V0 + 2 * n * day, V0 + (2 * n + 1) * day)
            for n in range(cap + 1)]  # fmt: skip
    assert remap(many, 0) == "too_many_intervals"
    rev, wrote = call(app_conn, t, "remap", "SYN-BRK", "1009", many[:cap], 0)
    assert (len(rev.intervals), wrote) == (cap, True)  # 0055 accepts the cap
    assert len(call(app_conn, t, "load").revisions) == 4


def test_knowledge_time_is_stamped_by_the_database(
    app_conn: Conn, conn: Conn, tenant: Any
) -> None:
    """A raw writer cannot choose recorded_at: qw_app has no grant on it, and even
    the owner's backdated value is replaced by the server time, so a past as-of
    view is unchanged (the review's reproduction, now refused)."""
    t = tenant()
    seed(app_conn, t)
    at = clock(app_conn)
    before = call(app_conn, t, "load", at)
    back = at - timedelta(milliseconds=500)
    for col, value in (("recorded_at", back), ("row_hash", "0" * 64)):
        with pytest.raises(errors.InsufficientPrivilege), \
                tn.tenant_transaction(app_conn, t):  # fmt: skip
            app_conn.execute(
                "INSERT INTO app.data_source (tenant_id, source_id, kind, provider, "
                f"content_hash, {col}) VALUES (%s, 'SYN-X', 'manual', 'x', %s, %s)",
                (t, "0" * 64, value),
            )
    src = Source("SYN-LATE", str(t), SourceKind.BROKER_API)
    content = st._source_content(tn.TenantTx(conn, t), src, "x")
    row = conn.execute(
        "INSERT INTO app.data_source (tenant_id, source_id, kind, provider, "
        "content_hash, recorded_at, row_hash) VALUES (%s, 'SYN-LATE', 'broker_api', "
        "'x', %s, %s, %s) RETURNING recorded_at, row_hash",
        (t, content, back, "0" * 64)).fetchone()  # fmt: skip
    assert row is not None and row[0] > at and row[1] == st.seal(content, row[0])
    after = call(app_conn, t, "load", at)
    assert (after.accounts, after.sources, after.revisions) == (
        before.accounts, before.sources, before.revisions)  # fmt: skip
    assert call(app_conn, t, "load").sources["SYN-LATE"].recorded_at == row[0]
    # a clock behind the tenant's latest row (planted, sealed) never orders before it
    ahead = row[0] + timedelta(hours=1)
    src = Source("SYN-AHEAD", str(t), SourceKind.MANUAL)
    content = st._source_content(tn.TenantTx(conn, t), src, "x")
    tamper(conn, "INSERT INTO app.data_source (tenant_id, source_id, kind, provider, "
                 f"content_hash, recorded_at, row_hash) VALUES ('{t}', 'SYN-AHEAD', "
                 f"'manual', 'x', '{content}', '{ahead.isoformat()}', "
                 f"'{st.seal(content, ahead)}')")  # fmt: skip
    rec, _ = call(app_conn, t, "add_source",
                  Source("SYN-NEXT", str(t), SourceKind.MANUAL), "x")  # fmt: skip
    assert rec.recorded_at >= ahead


def test_revisions_are_append_only_and_chained(
    app_conn: Conn, conn: Conn, tenant: Any
) -> None:
    t = tenant()
    seed(app_conn, t)
    rev1 = call(app_conn, t, "load").revisions[0]
    with tn.tenant_transaction(app_conn, t) as tx:
        h1 = st._hash_of(tx, rev1.source_id, rev1.account_ref, 1)
    first, new = (rev1.source_id, rev1.account_ref), ("SYN-BRK", "1009")
    june = "2026-06-01T00:00:00Z"
    cases: list[tuple[tuple[str, str], int, list[dict[str, Any]], str | None, str]] = [
        (new, 2, [iv("SYN-A")], h1, "next revision"),
        (first, 2, [iv("SYN-A")], None, "next revision"),
        (first, 3, [iv("SYN-A")], h1, "next revision"),
        (new, 1, [iv("SYN-A"), iv("SYN-B", june)], None, "intervals"),
        (new, 1, [iv("SYN-B", june), iv("SYN-A", end="2026-02-01T00:00:00Z")], None,
         "intervals"),
        (new, 1, [iv("SYN-A", "2026-01-01T00:00:00.000000Z")], None, "intervals"),
        (new, 1, [{**iv("SYN-A"), "secret": "x"}], None, "intervals"),
        (new, 1, [iv("SYN-A", end="2026-01-01T00:00:00Z")], None, "intervals"),
    ]  # fmt: skip
    for (src, ref), rev, ivs, prev, match in cases:
        with pytest.raises(errors.CheckViolation, match=match), \
                tn.tenant_transaction(app_conn, t) as tx:  # fmt: skip
            raw_revision(tx, src, ref, rev, ivs, prev)
    with pytest.raises(errors.RaiseException):  # even the owner's session
        conn.execute("UPDATE app.source_mapping_revision SET revision = revision")
    with pytest.raises(errors.RaiseException):
        conn.execute("DELETE FROM app.account")
    assert len(call(app_conn, t, "load").revisions) == 3


def test_a_rewritten_revision_with_a_valid_hash_breaks_the_chain(
    app_conn: Conn, conn: Conn, tenant: Any
) -> None:
    """Hash-only, so a forger can recompute both hashes; the next revision still
    names the original row hash."""
    t = tenant()
    seed(app_conn, t)
    new = (MappedInterval("SYN-A", V0, AT), MappedInterval("SYN-B", AT))
    call(app_conn, t, "remap", "SYN-BRK", "1001", new, 1)
    old = call(app_conn, t, "load").source_map.revisions("SYN-BRK", "1001")[0]
    forged = (MappedInterval("SYN-B", V0),)
    c = st._revision_content(tn.TenantTx(conn, t), "SYN-BRK", "1001", 1, forged, None)
    tamper(
        conn,
        "UPDATE app.source_mapping_revision SET intervals = "
        "jsonb_set(intervals, '{0,account_id}', '\"SYN-B\"'), "
        f"content_hash = '{c}', row_hash = '{st.seal(c, old.recorded_at)}' "
        "WHERE account_ref = '1001' AND revision = 1",
    )
    assert code(lambda: call(app_conn, t, "load")) == "integrity"
