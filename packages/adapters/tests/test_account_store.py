"""0055, 0056 and qw_adapters.account_store on PostgreSQL 16 as non-owner qw_app,
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
from qw_domain.decimals import Money, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.sources import (
    HoldingLine,
    MappedInterval,
    ScopeError,
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
DAY = timedelta(days=1)


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
    "UPDATE app.account SET broker = 'Interactive Brokers' WHERE account_id = 'SYN-A'",
    "UPDATE app.account_revision SET recorded_at = recorded_at - interval '1 second'",
    "UPDATE app.account_revision SET previous_hash = repeat('0', 64)",
    "UPDATE app.account_revision SET revision = 3",
    "UPDATE app.account_revision SET account_id = 'SYN-A'",
    "INSERT INTO app.account_revision SELECT tenant_id, account_id, 3, lifecycle, "
    "recorded_at, row_hash, content_hash, row_hash FROM app.account_revision",
])  # fmt: skip
def test_tamper_is_integrity(app_conn: Conn, conn: Conn, tenant: Any,
                             sql: str) -> None:  # fmt: skip
    t = tenant()
    seed(app_conn, t)
    new = (MappedInterval("SYN-A", V0, AT), MappedInterval("SYN-B", AT))
    call(app_conn, t, "remap", "SYN-BRK", "1001", new, 1)
    call(app_conn, t, "archive_account", "SYN-B", 1)
    call(app_conn, t, "load")
    tamper(conn, sql)
    assert code(lambda: call(app_conn, t, "load")) == "integrity"
    assert code(lambda: call(app_conn, t, "read_accounts", (), AT, DAY)) == (
        "integrity"
    )
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


E_ACUTE = "\N{LATIN SMALL LETTER E WITH ACUTE}"
# the parity table: value, accepted (decisions 102 and 103)
DISPLAY = [
    ("Interactive Brokers", True), ("TFSA - Self-directed", True), ("A", True),
    ("x" * 80, True), ("RRSP (spousal) #2", True),
    (f"Cr{E_ACUTE}dit Agricole", True),
    (f"Soci{E_ACUTE}t{E_ACUTE} G{E_ACUTE}n{E_ACUTE}rale", True),
    ("Caisse Desjardins \N{LATIN CAPITAL LETTER E WITH ACUTE}pargne", True),
    (E_ACUTE * 80, True), ("\N{LATIN CAPITAL LETTER A WITH GRAVE}", True),
    ("\N{LATIN SMALL LETTER Y WITH DIAERESIS}", True),
    ("\N{LATIN CAPITAL LETTER A WITH MACRON}\N{LATIN SMALL LETTER LONG S}", True),
    ("", False), (" ", False), (" IB", False), ("IB ", False),
    ("Interactive  Brokers", False), ("a" * 81, False), ("a" * 128, False),
    (E_ACUTE * 81, False), ("IB\tX", False), ("IB\n", False), ("IB\x7f", False),
    ("IB\x1bX", False), ("Cre\N{COMBINING ACUTE ACCENT}dit", False),
    ("\N{ANGSTROM SIGN}", False), ("x\N{COMBINING ACUTE ACCENT}", False),
    ("\N{CYRILLIC CAPITAL LETTER BYELORUSSIAN-UKRAINIAN I}nteractive", False),
    ("\N{GREEK CAPITAL LETTER ALPHA}B", False),
    ("Interactive\N{NO-BREAK SPACE}Brokers", False), ("IB\N{THIN SPACE}X", False),
    ("2\N{MULTIPLICATION SIGN}3", False), ("2\N{DIVISION SIGN}3", False),
    ("\N{FULLWIDTH LATIN CAPITAL LETTER I}B", False),
    ("IB\N{ZERO WIDTH SPACE}", False), ("I\N{SOFT HYPHEN}B", False),
    ("\N{LATIN SMALL LETTER B WITH STROKE}", False),
    ("\N{LATIN CAPITAL LETTER A WITH DOT BELOW}", False),
    ("\N{MATHEMATICAL BOLD CAPITAL A}", False),
]  # fmt: skip


def _domain_ok(value: str) -> bool:
    try:
        UnderlyingAccount("SYN-D", "t", value, value, "USD")
    except ScopeError:
        return False
    return True


def test_display_rule_parity_over_every_code_point(app_conn: Conn) -> None:
    """Decision 103: app.account_display_text agrees with the domain for every
    code point but NUL and surrogates, alone and between letters, on this
    database (encoding and collation recorded)."""
    row = app_conn.execute("SELECT current_setting('server_encoding'), "
                           "datcollate, datctype FROM pg_database "
                           "WHERE datname = current_database()").fetchone()  # fmt: skip
    assert row is not None and row[0] == "UTF8"
    values = [v for c in range(1, 0x110000) if not 0xD800 <= c <= 0xDFFF
              for v in (chr(c), f"a{chr(c)}b")]  # fmt: skip
    got = app_conn.execute("SELECT array_agg(app.account_display_text(v) ORDER BY n) "
                           "FROM unnest(%s::text[]) WITH ORDINALITY u (v, n)",
                           (values,)).fetchone()  # fmt: skip
    assert got is not None
    diff = [v for v, ok in zip(values, got[0], strict=True) if ok != _domain_ok(v)]
    assert diff == []
    accepted = {ord(v) for v, ok in zip(values, got[0], strict=True)
                if ok and len(v) == 1 and ord(v) > 0x7E}  # fmt: skip
    assert accepted == set(range(0xC0, 0x180)) - {0xD7, 0xF7}


def test_broker_and_account_type_are_one_display_rule_in_domain_and_sql(
    app_conn: Conn, tenant: Any
) -> None:
    """Decisions 102 and 103: the domain and 0056's raw-insert CHECKs accept and
    refuse exactly the parity table; account ids keep their stricter rule."""
    t, n = tenant(), iter(range(10**6))

    def domain_ok(broker: str, atype: str) -> bool:
        try:
            UnderlyingAccount("SYN-D", str(t), broker, atype, "USD")
        except ScopeError:
            return False
        return True

    def sql_ok(broker: str, atype: str) -> bool:
        try:
            with tn.tenant_transaction(app_conn, t) as tx:
                tx.conn.execute(
                    "INSERT INTO app.account (tenant_id, account_id, name, broker, "
                    "account_type, reporting_currency, analysis_scope, content_hash) "
                    "VALUES (%s, %s, 'SYNTHETIC', %s, %s, 'USD', 'hypothetical', %s)",
                    (t, f"SYN-RAW-{next(n)}", broker, atype, "0" * 64))  # fmt: skip
        except errors.CheckViolation:
            return False
        return True

    for value, want in DISPLAY:
        for pair in ((value, "margin"), ("IB", value)):
            assert (domain_ok(*pair), sql_ok(*pair)) == (want, want), pair
    with pytest.raises(ScopeError, match="account_id"):
        UnderlyingAccount("SYN D", str(t), "IB", "cash", "USD")
    u = tenant()
    for aid, broker, atype in (
        ("SYN-IB", "Interactive Brokers", "TFSA - Self-directed"),
        ("SYN-FR", DISPLAY[6][0], DISPLAY[7][0]),  # Societe Generale, Caisse ...
    ):  # fmt: skip
        a = UnderlyingAccount(aid, str(u), broker, atype, "CAD")
        call(app_conn, u, "register_account", a, "SYNTHETIC", "partial_declared")
        assert call(app_conn, u, "load").accounts[aid].account == a
    with pytest.raises(ScopeError) as err:
        UnderlyingAccount("SYN-X", str(u), "Cre\N{COMBINING ACUTE ACCENT}dit", "cash",
                          "CAD")  # fmt: skip
    assert err.value.code == "not_nfc"


def test_store_normalizes_caller_text_to_nfc(
    app_conn: Conn, conn: Conn, tenant: Any
) -> None:
    """Decision 103 as written: the store normalizes broker and account type to
    NFC before the domain value and the hashes; anything that bypasses the store
    (a raw insert) is still refused."""
    t = tenant()
    nfc = f"Cr{E_ACUTE}dit Agricole"
    decomposed = "Cre\N{COMBINING ACUTE ACCENT}dit Agricole"
    assert nfc != decomposed and len(decomposed) == len(nfc) + 1
    with tn.tenant_transaction(app_conn, t) as tx:
        built = st.account_from_text(tx, "SYN-CA", decomposed, decomposed, "CAD")
        direct = UnderlyingAccount("SYN-CA", str(t), nfc, nfc, "CAD")
        assert built == direct and built.institution == nfc
        assert code(lambda: st.account_from_text(tx, "SYN-CA", " x", "cash",
                                                 "CAD")) == "institution"  # fmt: skip
    rec, wrote = call(app_conn, t, "register_account", built, "SYNTHETIC",
                      "partial_declared")  # fmt: skip
    assert wrote and rec.account.institution == nfc
    row = conn.execute("SELECT broker, account_type, content_hash FROM app.account "
                       "WHERE tenant_id = %s AND account_id = 'SYN-CA'",
                       (t,)).fetchone()  # fmt: skip
    assert row is not None and row[:2] == (nfc, nfc)  # stored precomposed
    want = st._account_content(tn.TenantTx(conn, t), direct, "SYNTHETIC",
                               "partial_declared")  # fmt: skip
    assert row[2] == want  # the hash of registering the NFC form directly
    assert call(app_conn, t, "register_account", direct, "SYNTHETIC",
                "partial_declared") == (rec, False)  # fmt: skip
    for broker, atype in ((decomposed, "cash"), ("IB", decomposed)):
        with pytest.raises(errors.CheckViolation), \
                tn.tenant_transaction(app_conn, t) as tx:  # fmt: skip
            tx.conn.execute(
                "INSERT INTO app.account (tenant_id, account_id, name, broker, "
                "account_type, reporting_currency, analysis_scope, content_hash) "
                "VALUES (%s, 'SYN-RAW', 'x', %s, %s, 'CAD', 'hypothetical', %s)",
                (t, broker, atype, "0" * 64))  # fmt: skip


def test_archive_is_compare_and_set_final_and_as_of(
    app_conn: Conn, worker_conn: Conn, conn: Conn, tenant: Any
) -> None:
    t = tenant()
    seed(app_conn, t)
    before = clock(app_conn)
    for bad in (0, -1, "1", True, None, 1.0):
        assert code(lambda b=bad: call(app_conn, t, "archive_account", "SYN-A",  # type: ignore[misc]
                                       b)) == "expected"  # fmt: skip
    assert code(lambda: call(app_conn, t, "archive_account", "SYN-NONE", 1)) == (
        "unknown_account"
    )
    assert code(lambda: call(app_conn, t, "archive_account", "SYN-A", 2)) == "stale"
    assert code(lambda: call(worker_conn, t, "archive_account", "SYN-A", 1)) == (
        "forbidden"
    )
    rec, wrote = call(app_conn, t, "archive_account", "SYN-A", 1)
    assert (rec.revision, rec.lifecycle, wrote) == (2, "archived", True)
    assert call(app_conn, t, "archive_account", "SYN-A", 1) == (rec, False)  # retry
    assert code(lambda: call(app_conn, t, "archive_account", "SYN-A", 2)) == (
        "archived"
    )  # final: no unarchive, no second archive
    then, now = call(worker_conn, t, "load", before), call(worker_conn, t, "load")
    assert (then.accounts["SYN-A"].revision, then.accounts["SYN-A"].lifecycle) == (
        1, "active")  # fmt: skip
    assert now.accounts["SYN-A"] == rec
    assert now.accounts["SYN-B"].lifecycle == "active"
    again = call(app_conn, t, "register_account", acct(t, "SYN-A"), "SYNTHETIC SYN-A",
                 "complete_declared")  # fmt: skip
    assert again == (rec, False)
    with tn.tenant_transaction(app_conn, t) as tx:
        head = st._account_head(tx, "SYN-A")
        b1 = st._account_head(tx, "SYN-B")
    cases = [("SYN-A", 3, "archived", head, "archived"),
             ("SYN-B", 3, "archived", b1, "next revision"),
             ("SYN-B", 2, "archived", head, "next revision"),
             ("SYN-NONE", 2, "archived", b1, "next revision"),
             ("SYN-B", 2, "active", b1, "lifecycle_check")]  # fmt: skip
    for aid, rev, life, prev, match in cases:
        with pytest.raises(errors.CheckViolation, match=match), \
                tn.tenant_transaction(app_conn, t) as tx:  # fmt: skip
            tx.conn.execute(
                "INSERT INTO app.account_revision (tenant_id, account_id, revision, "
                "lifecycle, previous_hash, content_hash) VALUES (%s, %s, %s, %s, %s, "
                "%s)", (t, aid, rev, life, prev, "0" * 64))  # fmt: skip
    for stmt in ("UPDATE app.account_revision SET lifecycle = 'archived'",
                 "DELETE FROM app.account_revision"):  # fmt: skip
        with pytest.raises(errors.InsufficientPrivilege), \
                tn.tenant_transaction(app_conn, t):  # fmt: skip
            app_conn.execute(stmt)
    with pytest.raises(errors.RaiseException):  # even the owner's session
        conn.execute("DELETE FROM app.account_revision")
    assert call(app_conn, t, "load").accounts["SYN-A"] == rec
    # 0056's guard counts account revisions in the tenant's latest-row floor
    ahead = clock(app_conn) + timedelta(hours=1)
    c = st._lifecycle_content(tn.TenantTx(conn, t), "SYN-B", 2, "archived", b1)
    tamper(conn, "INSERT INTO app.account_revision (tenant_id, account_id, revision, "
                 "lifecycle, previous_hash, content_hash, recorded_at, row_hash) "
                 f"VALUES ('{t}', 'SYN-B', 2, 'archived', '{b1}', '{c}', "
                 f"'{ahead.isoformat()}', '{st.seal(c, ahead)}')")  # fmt: skip
    assert call(app_conn, t, "load").accounts["SYN-B"].lifecycle == "archived"
    src, _ = call(app_conn, t, "add_source",
                  Source("SYN-NEXT", str(t), SourceKind.MANUAL), "x")  # fmt: skip
    assert src.recorded_at >= ahead


def cash_snap(src: str, ref: str, *cash: Money, qty: str = "50",
              complete: bool = True) -> SourceSnapshot:  # fmt: skip
    return SourceSnapshot(src, ref, AT, (HoldingLine(SYNTH, "USD", Quantity(qty)),),
                          cash, complete)  # fmt: skip


def test_read_accounts_derives_status_and_cash_basis(
    app_conn: Conn, worker_conn: Conn, tenant: Any
) -> None:
    """Decision 101: derived on every read from a consolidation, never stored."""
    t = tenant()
    seed(app_conn, t)
    call(app_conn, t, "add_source", Source("SYN-ME", str(t), SourceKind.MANUAL), "me")
    call(app_conn, t, "remap", "SYN-ME", "b", (MappedInterval("SYN-B", V0),), 0)

    def read(*snaps: SourceSnapshot, known: datetime | None = None) -> Any:
        got = call(worker_conn, t, "read_accounts", snaps, AT, DAY, known)
        return {a: (s.status, s.cash_basis) for a, s in got.items()}, got

    # no snapshot: nothing to consolidate, so the basis is unknown (fail closed)
    states, got = read()
    assert states == {"SYN-A": ("active", "unknown"), "SYN-B": ("active", "unknown")}
    assert got["SYN-A"].view.status == "no_data" and got["SYN-A"].view.sizing_blocked
    assert got["SYN-A"].source_ids == ("SYN-BRK", "SYN-YAHOO")
    assert got["SYN-B"].source_ids == ("SYN-BRK", "SYN-ME")
    usd, cad = Money.of("10", "USD"), Money.of("5", "CAD")
    # one shared basis: broker_available; a mirror lag is a conflict, basis unknown
    ok = cash_snap("SYN-BRK", "1001", usd, cad)
    assert read(ok)[0]["SYN-A"] == ("active", "broker_available")
    lag = cash_snap("SYN-YAHOO", "pf-1", usd, cad, qty="40", complete=False)
    assert read(ok, lag)[0]["SYN-A"] == ("conflicted", "unknown")
    # SYN-B: broker cash in USD, the user's report alone in CAD: mixed, so unknown
    mixed = (cash_snap("SYN-BRK", "1002", usd, complete=False),
             cash_snap("SYN-ME", "b", usd, cad))  # fmt: skip
    states, got = read(*mixed)
    assert got["SYN-B"].view.status == "reconciled"
    assert dict(got["SYN-B"].view.cash_basis) == {
        "USD": "broker_available", "CAD": "user_reported"}  # fmt: skip
    assert states["SYN-B"] == ("active", "unknown")
    assert read(cash_snap("SYN-ME", "b", cad))[0]["SYN-B"] == (
        "active", "user_reported")  # fmt: skip
    # archived wins over conflicted; as of before the archive the account is active
    before = clock(app_conn)
    call(app_conn, t, "archive_account", "SYN-A", 1)
    assert read(ok, lag)[0]["SYN-A"] == ("archived", "unknown")
    assert read(ok)[0]["SYN-A"] == ("archived", "broker_available")
    # review finding: the view of an archived account may be unblocked; the
    # state's sizing permission never is
    arch, live = read(ok)[1]["SYN-A"], read(ok, known=before)[1]["SYN-A"]
    assert not arch.view.sizing_blocked and arch.sizing_blocked
    assert not live.view.sizing_blocked and not live.sizing_blocked
    assert read()[1]["SYN-B"].sizing_blocked  # no_data, active: still blocked
    assert read(ok, lag, known=before)[0]["SYN-A"] == ("conflicted", "unknown")
    assert read(ok, known=before)[1]["SYN-A"].record.revision == 1
    assert read(ok, known=before - timedelta(days=3650))[0] == {}
    # source_ids follow each reference's latest revision only
    call(app_conn, t, "remap", "SYN-ME", "b", (MappedInterval("SYN-A", V0),), 1)
    got = read()[1]
    assert got["SYN-A"].source_ids == ("SYN-BRK", "SYN-ME", "SYN-YAHOO")
    assert got["SYN-B"].source_ids == ("SYN-BRK",)


def test_a_forged_account_revision_with_valid_hashes_is_integrity(
    app_conn: Conn, conn: Conn, tenant: Any
) -> None:
    """Hash-only: a forger can seal any row, but not a second archive or a row
    whose previous hash breaks the chain."""
    for aid, rev, forged_previous in (("SYN-A", 3, False), ("SYN-B", 2, True)):
        t = tenant()
        seed(app_conn, t)
        rec, _ = call(app_conn, t, "archive_account", "SYN-A", 1)
        with tn.tenant_transaction(app_conn, t) as tx:
            prev = "0" * 64 if forged_previous else st._account_head(tx, aid)
        at = rec.recorded_at + timedelta(seconds=1)
        c = st._lifecycle_content(tn.TenantTx(conn, t), aid, rev, "archived", prev)
        tamper(conn, "INSERT INTO app.account_revision (tenant_id, account_id, "
                     "revision, lifecycle, previous_hash, content_hash, recorded_at, "
                     f"row_hash) VALUES ('{t}', '{aid}', {rev}, 'archived', '{prev}', "
                     f"'{c}', '{at.isoformat()}', '{st.seal(c, at)}')")  # fmt: skip
        assert code(lambda t=t: call(app_conn, t, "load")) == "integrity"  # type: ignore[misc]
