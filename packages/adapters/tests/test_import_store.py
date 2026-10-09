"""Persisted CSV import previews and their commit (0008_import_preview,
qw_adapters.import_store) on PostgreSQL 16.

Runtime connections are throwaway non-superuser LOGIN roles in qw_app / qw_worker
(conftest); the superuser `conn` only migrates, observes or plays a tampering owner
where a test says so. Files, accounts and symbols are SYNTHETIC
(docs/spec/tests/fixtures/canonical_transactions.csv). The reference is the in-memory
domain import (`csv_import.preview` + `commit`) and NUM01 in numerical_oracles.json.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import TupleRow
from qw_adapters import import_store as ims
from qw_adapters import journal_store as js
from qw_adapters import tenancy as tn
from qw_adapters.tenancy import TenantTx, tenant_transaction
from qw_domain import csv_import as ci
from qw_domain import sources as src
from qw_domain.corporate_actions import SourceRef
from qw_domain.decimals import Money, Quantity
from qw_domain.identity import InstrumentId, Listing, Mic, SecurityMaster, Ticker
from qw_domain.journal import Journal, materialize
from qw_domain.postings import Header, SourceObservation, deposit

Conn = psycopg.Connection[TupleRow]
FIXTURES = Path(__file__).resolve().parents[3] / "docs/spec/tests/fixtures"
FIXTURE = (FIXTURES / "canonical_transactions.csv").read_bytes()
ORACLES = {
    o["id"]: o
    for o in json.loads((FIXTURES / "numerical_oracles.json").read_text())["oracles"]
}
SYN = InstrumentId(uuid.UUID("00000000-0000-4000-8000-00000000005a"))
ACCT = "acct-syn-1"  # SYN-ACCOUNT at SYN-BROKER maps here
ACCT2 = "acct-syn-2"  # SYN-ACCOUNT2 maps here
START = datetime(2025, 1, 1, tzinfo=UTC)
IMPORT_ROWS = ("ledger_event", "posting", "unit_posting", "source_record")


def make_tenant(conn: Conn, name: str) -> uuid.UUID:
    with tn.provision_tenant(conn) as tx:
        user = tn.create_user(tx, f"SYNTHETIC {name}")
        tn.add_membership(tx, user, tn.MembershipRole.TENANT_OWNER)
        return tx.tenant_id


def world(
    tenant: uuid.UUID, listed: bool = True
) -> tuple[ci.ImportRequest, src.SourceMap, SecurityMaster]:
    t = str(tenant)
    smap = src.SourceMap()
    smap.add_account(src.UnderlyingAccount(ACCT, t, "SYN", "cash", "USD"))
    smap.add_source(src.Source("SYN-BROKER", t, src.SourceKind.BROKER_EXPORT))
    smap.remap("SYN-BROKER", "SYN-ACCOUNT", [src.MappedInterval(ACCT, START)], START)
    smap.add_account(src.UnderlyingAccount(ACCT2, t, "SYN", "cash", "USD"))
    smap.remap("SYN-BROKER", "SYN-ACCOUNT2", [src.MappedInterval(ACCT2, START)], START)
    master = SecurityMaster()
    if listed:
        master.add_listing(Listing(SYN, Mic("XNYS"), Ticker("SYNTH"), date(2025, 1, 1)))
    request = ci.ImportRequest(t, ci.FileKind.TRANSACTIONS, ci.ColumnMapping.identity())
    return request, smap, master


def in_tx[T](conn: Conn, tenant: uuid.UUID, fn: Callable[[TenantTx], T]) -> T:
    with tenant_transaction(conn, tenant) as tx:
        return fn(tx)


def run(conn: Conn, tenant: uuid.UUID, stmt: str, *params: object) -> None:
    in_tx(conn, tenant, lambda tx: tx.conn.execute(stmt, params))


def save(
    conn: Conn, tenant: uuid.UUID, data: bytes = FIXTURE, **kw: timedelta
) -> tuple[ci.ImportPreview, ims.CommitToken]:
    request, smap, master = world(tenant)
    return in_tx(
        conn, tenant, lambda tx: ims.save_preview(tx, data, request, smap, master, **kw)
    )


def commit(
    conn: Conn, tenant: uuid.UUID, token: ims.CommitToken, listed: bool = True
) -> ci.CommitResult:
    _, smap, master = world(tenant, listed)
    return in_tx(conn, tenant, lambda tx: ims.commit_preview(tx, token, smap, master))


def one(conn: Conn, query: str, *params: object) -> object:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def rows(conn: Conn, tenant: uuid.UUID) -> dict[str, object]:
    """Row counts per import-written table, read by the superuser (no RLS)."""
    sql = "SELECT count(*) FROM app.{} WHERE tenant_id = %s"
    return {t: one(conn, sql.format(t), tenant) for t in IMPORT_ROWS}


def status(conn: Conn, token: ims.CommitToken) -> object:
    sql = "SELECT status::text FROM app.import_preview WHERE preview_id = %s"
    return one(conn, sql, token.preview_id)


NONE = dict.fromkeys(IMPORT_ROWS, 0)


@pytest.mark.db
def test_commit_round_trips_and_equals_the_domain_commit(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant = make_tenant(app_conn, "A")
    p, token = save(worker_conn, tenant)
    assert p.state == "validated" and dict(p.expected_revisions) == {ACCT: 1}
    assert token.wire() == f"{token.preview_id}:{token.content_hash}"
    assert ims.CommitToken.from_wire(token.wire()) == token
    assert rows(conn, tenant) == NONE  # a saved preview writes no journal rows
    result = commit(worker_conn, tenant, token)
    assert result == ci.CommitResult("committed", 3, 0)
    # Reference: the same file through the in-memory domain import.
    request, smap, master = world(tenant)
    ref = Journal()
    ref_p = ci.preview(FIXTURE, request, ref, smap, master, START)
    assert ci.commit(ref_p, ref, START, str(tenant)) == result
    stored = in_tx(worker_conn, tenant, lambda tx: js.load_journal(tx, ACCT))
    assert stored.events() == ref.events()
    exp, pos = ORACLES["NUM01"]["expected"], materialize(stored.events())
    assert pos.cash == {(ACCT, "USD"): Money.of(exp["cash"], "USD")}
    assert pos.holding(ACCT, SYN).quantity == Quantity(exp["remaining_qty"])
    assert pos.holding(ACCT, SYN).cost == Money.of(exp["remaining_cost"], "USD")
    assert pos.realized == {(ACCT, "USD"): Money.of(exp["realized"], "USD")}
    assert rows(conn, tenant) == {
        "ledger_event": 3, "source_record": 3,
        "posting": sum(len(e.money) for e in ref.events()),
        "unit_posting": sum(len(e.units) for e in ref.events()),
    }  # fmt: skip
    created = one(conn, "SELECT created_at FROM app.import_preview WHERE "
                  "preview_id = %s", token.preview_id)  # fmt: skip
    observed = conn.execute(
        "SELECT DISTINCT observed_at FROM app.source_record WHERE tenant_id = %s",
        (tenant,),
    ).fetchall()
    assert observed == [(created,)]  # the preview's time, not the commit's
    assert status(conn, token) == "committed"


@pytest.mark.db
def test_post_between_preview_and_commit_conflicts_with_no_rows(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant = make_tenant(app_conn, "A")
    _, token = save(worker_conn, tenant)
    interloper = deposit(
        Header(ACCT, START, SourceRef("SYN-MANUAL", "M1")), Money.of(5, "USD")
    )
    in_tx(app_conn, tenant, lambda tx: js.post(tx, interloper))
    result = commit(worker_conn, tenant, token)
    assert result.outcome == "conflict" and "revision" in result.reason
    assert rows(conn, tenant) == NONE | {"ledger_event": 1, "posting": 2}
    assert status(conn, token) == "open"


@pytest.mark.db
def test_recommit_is_idempotent(app_conn: Conn, worker_conn: Conn, conn: Conn) -> None:
    tenant = make_tenant(app_conn, "A")
    _, token = save(worker_conn, tenant)
    first = commit(worker_conn, tenant, token)
    after = rows(conn, tenant)
    assert commit(worker_conn, tenant, token) == first
    assert rows(conn, tenant) == after
    # A second preview of the same file is all duplicates and posts nothing.
    p2, token2 = save(worker_conn, tenant)
    assert p2.count(ci.RowStatus.DUPLICATE) == 3 and dict(p2.expected_revisions) == {
        ACCT: 4
    }
    assert commit(worker_conn, tenant, token2) == ci.CommitResult("committed", 0, 3)
    assert rows(conn, tenant) == after


@pytest.mark.db
def test_concurrent_commits_of_one_preview_commit_once(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant = make_tenant(app_conn, "A")
    _, token = save(worker_conn, tenant)
    _, smap, master = world(tenant)
    holding, go = threading.Event(), threading.Event()
    results: list[ci.CommitResult] = []

    def first() -> None:  # commits, then holds its transaction open
        with tenant_transaction(worker_conn, tenant) as tx:
            results.append(ims.commit_preview(tx, token, smap, master))
            holding.set()
            go.wait(10)

    thread = threading.Thread(target=first)
    thread.start()
    assert holding.wait(10)
    second = threading.Thread(
        target=lambda: results.append(commit(app_conn, tenant, token))
    )
    second.start()
    deadline = time.monotonic() + 10
    while conn.execute(
        "SELECT count(*) FROM pg_locks WHERE NOT granted AND locktype = 'transactionid'"
    ).fetchone() == (0,):
        assert time.monotonic() < deadline, "the second commit never waited"
        time.sleep(0.01)
    go.set()
    thread.join(30)
    second.join(30)
    assert results == [ci.CommitResult("committed", 3, 0)] * 2
    assert rows(conn, tenant)["ledger_event"] == 3


@pytest.mark.db
def test_other_tenant_and_wrong_token_are_refused(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    a = make_tenant(app_conn, "A")
    b = make_tenant(app_conn, "B")
    _, token = save(worker_conn, a)
    refused = commit(worker_conn, b, token)  # B's own world, A's preview id
    assert (refused.outcome, refused.reason) == ("rejected", "unknown_preview")
    forged = ims.CommitToken(token.preview_id, "0" * 64)
    refused = commit(worker_conn, a, forged)
    assert (refused.outcome, refused.reason) == ("rejected", "token_mismatch")
    assert rows(conn, a) == NONE == rows(conn, b)
    assert status(conn, token) == "open"
    _, token_b = save(worker_conn, b)  # same file and request, other tenant
    assert token_b.content_hash != token.content_hash
    with pytest.raises(ValueError, match="tenant"):
        request, smap, master = world(a)
        in_tx(worker_conn, b, lambda tx: ims.save_preview(
            tx, FIXTURE, request, smap, master))  # fmt: skip
    for bad in ("x", f"{token.preview_id}:{'A' * 64}", f"nope:{'0' * 64}"):
        with pytest.raises(ValueError):
            ims.CommitToken.from_wire(bad)


@pytest.mark.db
def test_expired_and_superseded_previews_are_refused(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant = make_tenant(app_conn, "A")
    _, short = save(worker_conn, tenant, ttl=timedelta(microseconds=1))
    refused = commit(worker_conn, tenant, short)
    assert (refused.outcome, refused.reason) == ("rejected", "expired")
    assert status(conn, short) == "expired"
    _, old = save(worker_conn, tenant)
    _, new = save(worker_conn, tenant)  # re-preview of the same account
    assert status(conn, old) == "superseded"
    refused = commit(worker_conn, tenant, old)
    assert (refused.outcome, refused.reason) == ("rejected", "superseded")
    assert rows(conn, tenant) == NONE
    assert commit(worker_conn, tenant, new).outcome == "committed"
    assert commit(worker_conn, tenant, old).reason == "superseded"


@pytest.mark.db
def test_tampered_inputs_and_changed_reference_data_are_refused(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant = make_tenant(app_conn, "A")
    _, token = save(worker_conn, tenant)
    # The owner (here the superuser) bypasses the guard trigger in replica mode.
    conn.execute("SET session_replication_role = replica")
    conn.execute(
        "UPDATE app.import_preview SET file_bytes = convert_to(replace(convert_from("
        "file_bytes, 'UTF8'), ',1000,', ',2000,'), 'UTF8') WHERE preview_id = %s",
        (token.preview_id,),
    )
    conn.execute("RESET session_replication_role")
    refused = commit(worker_conn, tenant, token)
    assert (refused.outcome, refused.reason) == ("rejected", "hash_mismatch")
    assert rows(conn, tenant) == NONE and status(conn, token) == "open"
    # The security master changed after the preview: re-derived rows differ.
    _, token = save(worker_conn, tenant)
    result = commit(worker_conn, tenant, token, listed=False)
    assert (result.outcome, result.reason) == ("conflict", "preview_changed")
    assert rows(conn, tenant) == NONE


@pytest.mark.db
def test_a_write_phase_conflict_rolls_back_every_row(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant = make_tenant(app_conn, "A")
    # S1 (the last row) was already received with other content, e.g. by ingest.
    held = SourceObservation(
        ACCT, SourceRef("SYN-BROKER", "S1"), START, START, {"kind": "sell"}
    )
    in_tx(app_conn, tenant, lambda tx: js.observe(tx, held))
    p, token = save(worker_conn, tenant)
    assert p.state == "validated"  # the preview does not read source records
    result = commit(worker_conn, tenant, token)
    assert result.outcome == "conflict"
    assert rows(conn, tenant) == NONE | {"source_record": 1}  # D1, B1 rolled back
    assert status(conn, token) == "open"


@pytest.mark.db
def test_a_preview_needing_resolution_cannot_commit(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant = make_tenant(app_conn, "A")
    bad = FIXTURE.replace(b",1000,", b",1000.5x,")
    p, token = save(worker_conn, tenant, bad)
    assert p.state == "needs_resolution"
    refused = commit(worker_conn, tenant, token)
    assert refused == ci.CommitResult("rejected", reason="preview is needs_resolution")
    assert rows(conn, tenant) == NONE


@pytest.mark.db
def test_preview_rows_are_guarded(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant = make_tenant(app_conn, "A")
    _, token = save(worker_conn, tenant)
    commit(worker_conn, tenant, token)
    other = make_tenant(app_conn, "B")
    seen = in_tx(app_conn, other, lambda tx: tx.conn.execute(
        "SELECT count(*) FROM app.import_preview").fetchone())  # fmt: skip
    assert seen == (0,)
    for stmt in (
        "UPDATE app.import_preview SET content_hash = repeat('0', 64)",
        "UPDATE app.import_preview SET created_at = now()",
        "DELETE FROM app.import_preview",
        "TRUNCATE app.import_preview",
    ):
        with pytest.raises(errors.InsufficientPrivilege):
            run(app_conn, tenant, stmt)
    for stmt in (  # a settled status never moves; the guard applies to every role
        "UPDATE app.import_preview SET status = 'open', committed_at = NULL, "
        "posted_count = NULL, duplicate_count = NULL",
        "UPDATE app.import_preview SET status = 'expired', committed_at = NULL, "
        "posted_count = NULL, duplicate_count = NULL",
    ):
        with pytest.raises(errors.RaiseException, match="settled"):
            run(app_conn, tenant, stmt)
    with pytest.raises(errors.RaiseException, match="immutable"):
        conn.execute("UPDATE app.import_preview SET request = '{}'")
    with pytest.raises(errors.RaiseException, match="append-only"):
        conn.execute("DELETE FROM app.import_preview")
    _, fresh = save(worker_conn, tenant, FIXTURE.replace(b"D1", b"D9"))
    with pytest.raises(errors.CheckViolation):  # committed needs its result
        run(app_conn, tenant, "UPDATE app.import_preview SET status = 'committed' "
            "WHERE preview_id = %s", fresh.preview_id)  # fmt: skip


@pytest.mark.db
def test_import_preview_catalogue(conn: Conn) -> None:
    from qw_adapters.migrations import migrate

    migrate(conn)
    funcs = conn.execute(
        "SELECT p.prosecdef, p.proconfig FROM pg_proc p WHERE "
        "p.pronamespace = 'app'::regnamespace AND p.proname = 'import_preview_guard'"
    ).fetchall()
    assert funcs == [(False, ["search_path=pg_catalog"])]  # invoker rights
    definers = one(
        conn,
        "SELECT count(*) FROM pg_proc WHERE pronamespace = "
        "'app'::regnamespace AND prosecdef AND proname LIKE 'import%%'",
    )
    assert definers == 0
    table, column = (
        "has_table_privilege(%s, 'app.import_preview', %s)",
        ("has_column_privilege(%s, 'app.import_preview', %s, 'UPDATE')"),
    )
    for role in ("qw_app", "qw_worker", "qw_ingest", "qw_assessor", "qw_release"):
        for priv in ("DELETE", "TRUNCATE"):
            assert one(conn, f"SELECT {table}", role, priv) is False, (role, priv)
        for col in ("content_hash", "file_bytes", "request", "created_at"):
            redactor = (role, col) == ("qw_worker", "file_bytes")  # retention job
            assert one(conn, f"SELECT {column}", role, col) is redactor, (role, col)
    for role, expected in (("qw_ingest", False), ("qw_worker", True)):
        assert one(conn, f"SELECT {table}", role, "SELECT") is expected, role


MIB8 = 8 * 1024 * 1024


@pytest.mark.db
def test_oversize_files_and_limits_raise_before_the_database(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant = make_tenant(app_conn, "A")
    request, smap, master = world(tenant)
    big = FIXTURE + b"x" * (MIB8 + 1 - len(FIXTURE))
    assert len(big) == MIB8 + 1
    wide = dataclasses.replace(request, limits=ci.Limits(max_bytes=MIB8 + 1))
    for data, req in ((big, request), (FIXTURE, wide)):
        with (
            pytest.raises(ValueError, match="8 MiB"),
            tenant_transaction(worker_conn, tenant) as tx,
        ):
            ims.save_preview(tx, data, req, smap, master)
    assert one(conn, "SELECT count(*) FROM app.import_preview") == 0


def redact(conn: Conn, tenant: uuid.UUID, token: ims.CommitToken) -> None:
    in_tx(conn, tenant, lambda tx: ims.redact_settled_preview(tx, token.preview_id))


def stored_bytes(conn: Conn, token: ims.CommitToken) -> object:
    sql = "SELECT file_bytes FROM app.import_preview WHERE preview_id = %s"
    return one(conn, sql, token.preview_id)


@pytest.mark.db
def test_only_settled_previews_are_redacted(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant = make_tenant(app_conn, "A")
    _, token = save(worker_conn, tenant)
    with pytest.raises(ValueError, match="open"):
        redact(worker_conn, tenant, token)
    with pytest.raises(errors.RaiseException, match="settled"):  # below the adapter
        run(worker_conn, tenant, "UPDATE app.import_preview SET file_bytes = NULL")
    conn.execute("SET session_replication_role = replica")  # past the trigger
    with pytest.raises(errors.CheckViolation):
        conn.execute("UPDATE app.import_preview SET file_bytes = NULL")
    conn.execute("RESET session_replication_role")
    assert stored_bytes(conn, token) is not None
    with pytest.raises(errors.InsufficientPrivilege):  # not the retention role
        run(app_conn, tenant, "UPDATE app.import_preview SET file_bytes = NULL")
    first = commit(worker_conn, tenant, token)
    redact(worker_conn, tenant, token)
    redact(worker_conn, tenant, token)  # idempotent
    assert stored_bytes(conn, token) is None
    assert (
        commit(worker_conn, tenant, token)
        == first
        == ci.CommitResult("committed", 3, 0)
    )
    with pytest.raises(errors.RaiseException, match="immutable"):  # no re-filling
        run(worker_conn, tenant, "UPDATE app.import_preview SET file_bytes = 'x'")
    with pytest.raises(LookupError):
        in_tx(worker_conn, tenant, lambda tx: ims.redact_settled_preview(
            tx, uuid.uuid4()))  # fmt: skip
    _, short = save(worker_conn, tenant, ttl=timedelta(microseconds=1))
    assert commit(worker_conn, tenant, short).reason == "expired"
    redact(worker_conn, tenant, short)
    refused = commit(worker_conn, tenant, short)
    assert (refused.outcome, refused.reason) == ("rejected", "expired")


@pytest.mark.db
def test_an_unvalidated_preview_supersedes_nothing(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant = make_tenant(app_conn, "A")
    _, token = save(worker_conn, tenant)
    p, _ = save(worker_conn, tenant, FIXTURE.replace(b",1000,", b",1000.5x,"))
    assert p.state == "needs_resolution"
    assert status(conn, token) == "open"
    assert commit(worker_conn, tenant, token).outcome == "committed"


@pytest.mark.db
def test_unseen_previews_commit_once_and_others_are_not_superseded(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant = make_tenant(app_conn, "A")
    other = make_tenant(app_conn, "B")
    request, smap, master = world(tenant)
    with tenant_transaction(worker_conn, tenant) as tx1:  # neither sees the other
        _, first = ims.save_preview(tx1, FIXTURE, request, smap, master)
        with tenant_transaction(app_conn, tenant) as tx2:
            _, second = ims.save_preview(tx2, FIXTURE, request, smap, master)
    assert (status(conn, first), status(conn, second)) == ("open", "open")
    _, disjoint = save(
        worker_conn, tenant, FIXTURE.replace(b"SYN-ACCOUNT,", b"SYN-ACCOUNT2,")
    )
    save(worker_conn, other)
    assert (status(conn, first), status(conn, second)) == ("open", "open")
    assert commit(worker_conn, tenant, second).outcome == "committed"
    late = commit(worker_conn, tenant, first)
    assert late.outcome == "conflict" and "revision" in late.reason
    assert commit(worker_conn, tenant, disjoint) == ci.CommitResult("committed", 3, 0)
    assert rows(conn, tenant)["ledger_event"] == 6
