"""Policy versions and adoption receipts (0009_policy, qw_adapters.policy_store) on
PostgreSQL 16, as the non-superuser qw_app login of the conftest harness. The
superuser `conn` only observes or plays a tampering owner where a test says so. All
answers, accounts and users are SYNTHETIC. The oracle for persisted histories is the
in-memory domain (`qw_domain.policy.PolicyHistory`)."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import TupleRow
from qw_adapters import policy_store as ps
from qw_adapters import tenancy as tn
from qw_domain.onboarding import load_catalogue
from qw_domain.policy import (
    REQUIRED_METRICS,
    AdoptionConsent,
    ConflictCode,
    DraftPolicy,
    PolicyError,
    PolicyHistory,
    build_draft,
)

Conn = psycopg.Connection[TupleRow]
CONFIG = Path(__file__).resolve().parents[3] / "docs/spec/config"
CATALOGUE = load_catalogue(
    json.loads((CONFIG / "onboarding_questions.json").read_text())
)
UNKNOWN = ConflictCode.OPTION_PERMISSION_UNKNOWN
WINDOW = timedelta(minutes=10)


def draft(tenant: uuid.UUID, objectives: tuple[str, ...] = ("growth",)) -> DraftPolicy:
    """SYNTHETIC answers. Options with no account facts raise the acknowledgeable
    unknown-permission conflict and an exclusion, so receipts carry both."""
    limits = [
        {"metric": m, "value": "0.05", "unit": "ratio", "window": "rolling_30d",
         "denominator": "account_nav", "account_id": "acct-syn-1", "currency": "CAD"}
        for m in sorted(REQUIRED_METRICS)
    ] + [{"metric": "drawdown", "value": "2500.5", "unit": "amount", "sleeve_id": "s1",
          "window": "rolling_30d", "denominator": "sleeve_nav",
          "account_id": "acct-syn-1", "currency": "CAD"}]  # fmt: skip
    answers: dict[str, Any] = {
        "ONB01": "selected_accounts", "ONB03": "CAD", "ONB04": list(objectives),
        "ONB05": "gt_7y", "ONB06": "none_known", "ONB07": "financially_manageable",
        "ONB10": "regular_session", "ONB11": ["stocks", "options"],
        "ONB12": ["long_term", "same_day"], "ONB14": limits, "ONB16": "rules_only",
        "ONB13": [{"account_id": "acct-syn-1", "currency": "CAD", "amount": "5000"}],
    }  # fmt: skip
    responses = CATALOGUE.validate(answers)
    return build_draft(str(tenant), responses, (), as_of=date(2026, 10, 9),
                       synthetic=True)  # fmt: skip


@dataclass(frozen=True)
class Ctx:
    tenant: uuid.UUID
    owner: uuid.UUID
    session: uuid.UUID  # the owner's, stepped up
    other: uuid.UUID
    other_session: uuid.UUID  # another member's, stepped up
    policy: uuid.UUID


@pytest.fixture
def ctx(app_conn: Conn) -> Ctx:
    with tn.provision_tenant(app_conn) as tx:
        ids: list[uuid.UUID] = []
        for role in (tn.MembershipRole.TENANT_OWNER, tn.MembershipRole.TENANT_MEMBER):
            user = tn.create_user(tx, f"SYNTHETIC {role}")
            tn.add_membership(tx, user, role)
            session, _ = tn.create_session(tx, user, timedelta(hours=1))
            tn.mark_step_up(tx, session)
            ids += [user, session]
    owner, session, other, other_session = ids
    return Ctx(tx.tenant_id, owner, session, other, other_session, uuid.uuid4())


def propose(conn: Conn, c: Ctx, d: DraftPolicy | None = None) -> PolicyHistory:
    with tn.tenant_transaction(conn, c.tenant) as tx:
        return ps.propose(tx, c.policy, d or draft(c.tenant), c.owner)


def adopt(
    conn: Conn, c: Ctx, version: int = 1, content_hash: str | None = None, **kw: Any
) -> PolicyHistory:
    args: dict[str, Any] = {
        "principal": c.owner, "session_id": c.session, "window": WINDOW,
        "acknowledged": frozenset({UNKNOWN}), "text": "SYNTHETIC: I adopt this",
    } | kw  # fmt: skip
    with tn.tenant_transaction(conn, c.tenant) as tx:
        found = ps.load(tx, c.policy)
        digest = content_hash or (found.versions[-1].content_hash if found else "0")
        return ps.adopt(tx, c.policy, version, digest, **args)


def load(conn: Conn, c: Ctx, tenant: uuid.UUID | None = None) -> PolicyHistory | None:
    with tn.tenant_transaction(conn, tenant or c.tenant) as tx:
        return ps.load(tx, c.policy)


def rows(conn: Conn, table: str) -> int:
    found = conn.execute(f"SELECT count(*) FROM app.{table}").fetchone()
    return int(found[0]) if found else -1


@pytest.mark.db
def test_round_trip_matches_the_domain_and_appends_only_on_change(
    app_conn: Conn, conn: Conn, ctx: Ctx
) -> None:
    d1, d2 = draft(ctx.tenant), draft(ctx.tenant, ("growth", "income"))
    first = propose(app_conn, ctx, d1)
    assert propose(app_conn, ctx, d1) == first == load(app_conn, ctx)
    assert rows(conn, "policy_version") == 1  # same content hash: no new version
    propose(app_conn, ctx, d2)
    adopted = adopt(app_conn, ctx, version=2)
    oracle = PolicyHistory.new(str(ctx.policy), str(ctx.tenant)).propose(d1).propose(d2)
    assert adopted.versions == oracle.versions
    assert load(app_conn, ctx) == adopted
    assert [adopted.status(v) for v in (1, 2)] == ["superseded", "adopted"]
    [receipt] = adopted.receipts
    assert receipt.acknowledged == (UNKNOWN,) and receipt.label == "SYNTHETIC"
    assert receipt.limits == d2.limits and receipt.exclusions == d2.exclusions
    assert receipt.principal_id == str(ctx.owner)
    stored = conn.execute(
        "SELECT signed_at, created_at, step_up_at, step_up_receipt_id, receipt_hash, "
        "limits -> 0 ->> 'value' FROM app.policy_adoption "
        "JOIN app.policy_version USING (tenant_id, policy_id, version)"
    ).fetchone()
    assert stored is not None
    signed, created, step_up, receipt_id, digest, value = stored
    assert receipt.signed_at == signed and receipt.signed_at.tzinfo is UTC
    assert signed >= created and digest == receipt.receipt_hash
    micros = (step_up - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)
    assert receipt_id == receipt.step_up_receipt_id == f"{ctx.session}.{micros}"
    assert isinstance(value, str)  # decimal strings, never JSON numbers
    with tn.tenant_transaction(app_conn, ctx.tenant) as tx:
        assert ps.list_policy_ids(tx, 10) == [ctx.policy]


@pytest.mark.db
def test_tables_are_append_only_with_server_assigned_times(
    app_conn: Conn, conn: Conn, ctx: Ctx
) -> None:
    propose(app_conn, ctx)
    adopt(app_conn, ctx)
    for table in ("policy_version", "policy_adoption"):
        for statement in (f"UPDATE app.{table} SET tenant_id = tenant_id",
                          f"DELETE FROM app.{table}"):  # fmt: skip
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app_conn.execute(statement)
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                conn.execute(statement)  # the superuser too
    for column in (
        "app.policy_version (created_at)",
        "app.policy_adoption (signed_at)",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_conn.execute(f"INSERT INTO {column} VALUES (now())")
    assert (rows(conn, "policy_version"), rows(conn, "policy_adoption")) == (1, 1)


@pytest.mark.db
def test_database_refuses_gaps_stale_and_foreign_step_up_adoptions(
    app_conn: Conn, conn: Conn, ctx: Ctx
) -> None:
    history = propose(app_conn, ctx)
    version = history.versions[0]
    content = json.dumps(ps.draft_to_json(version.draft))
    insert = (
        "INSERT INTO app.policy_version (tenant_id, policy_id, version, content_hash, "
        "content, label, created_by) VALUES (%s, %s, 3, %s, %s, 'SYNTHETIC', %s)"
    )
    with (
        pytest.raises(psycopg.errors.RaiseException, match="latest \\+ 1"),
        tn.tenant_transaction(app_conn, ctx.tenant),
    ):
        app_conn.execute(insert, (ctx.tenant, ctx.policy, version.content_hash,
                                  content, ctx.owner))  # fmt: skip
    step_up = conn.execute(
        "SELECT step_up_at FROM app.session WHERE id = %s", (ctx.other_session,)
    ).fetchone()
    assert step_up is not None
    forged = (  # the owner as principal, presenting another member's step-up
        "INSERT INTO app.policy_adoption (tenant_id, policy_id, version, policy_hash, "
        "label, principal_id, session_id, step_up_at, step_up_receipt_id, limits, "
        "acknowledged, exclusions, acknowledgement_text, receipt_hash) VALUES "
        "(%s, %s, 1, %s, 'SYNTHETIC', %s, %s, %s, %s, '[{}]', '[]', '[]', 'x', %s)"
    )
    receipt_id = ps.step_up_receipt_id(ctx.other_session, step_up[0])
    with (
        pytest.raises(psycopg.errors.RaiseException, match="no valid step-up"),
        tn.tenant_transaction(app_conn, ctx.tenant),
    ):
        app_conn.execute(forged, (ctx.tenant, ctx.policy, version.content_hash,
                                  ctx.owner, ctx.other_session, step_up[0],
                                  receipt_id, "0" * 64))  # fmt: skip
    assert rows(conn, "policy_adoption") == 0


@pytest.mark.db
def test_adoption_refusals_write_nothing(app_conn: Conn, conn: Conn, ctx: Ctx) -> None:
    def refused(code: str, **kw: Any) -> None:
        with pytest.raises((ps.PolicyStoreError, PolicyError)) as caught:
            adopt(app_conn, ctx, **kw)
        assert getattr(caught.value, "code", None) == code

    refused("not_found")
    propose(app_conn, ctx)
    refused("step_up_required", session_id=ctx.other_session)  # another user's
    refused("step_up_required", principal=ctx.other, session_id=ctx.session)
    refused("step_up_required", window=timedelta(microseconds=1))  # expired
    with tn.tenant_transaction(app_conn, ctx.tenant) as tx:
        fresh, _ = tn.create_session(tx, ctx.owner, timedelta(hours=1))
    refused("step_up_required", session_id=fresh)  # never stepped up
    refused("stale", content_hash="f" * 64)
    refused("unacknowledged_conflict", acknowledged=frozenset())
    adopt(app_conn, ctx)
    refused("already_adopted")
    propose(app_conn, ctx, draft(ctx.tenant, ("income",)))
    refused("not_latest", version=1)
    assert rows(conn, "policy_adoption") == 1


@pytest.mark.db
def test_concurrent_adoptions_of_one_version_admit_exactly_one(
    runtime_urls: dict[str, str], app_conn: Conn, conn: Conn, ctx: Ctx
) -> None:
    propose(app_conn, ctx)
    barrier, outcomes = threading.Barrier(4), []

    def attempt() -> None:
        with tn.runtime_connection(runtime_urls["qw_app"]) as c:
            barrier.wait()
            try:
                adopt(c, ctx)
                outcomes.append("ok")
            except PolicyError as exc:
                outcomes.append(exc.code)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["already_adopted"] * 3 + ["ok"]
    assert rows(conn, "policy_adoption") == 1


@pytest.mark.db
def test_other_tenants_see_nothing_and_tampering_fails_load(
    app_conn: Conn, conn: Conn, ctx: Ctx
) -> None:
    propose(app_conn, ctx)
    adopt(app_conn, ctx)
    with tn.provision_tenant(app_conn) as other:
        assert ps.load(other, ctx.policy) is None
        assert ps.list_policy_ids(other, 10) == []
    conn.execute("SET session_replication_role = replica")  # a tampering owner
    conn.execute("UPDATE app.policy_adoption SET acknowledgement_text = 'forged'")
    with pytest.raises(ps.PolicyStoreError, match="integrity"):
        load(app_conn, ctx)
    conn.execute(
        "UPDATE app.policy_adoption SET acknowledgement_text = 'SYNTHETIC: I adopt "
        "this'; UPDATE app.policy_version SET content = jsonb_set(content, "
        "'{limits,0,value}', '\"0.5\"')"
    )
    with pytest.raises(ps.PolicyStoreError, match="integrity"):
        load(app_conn, ctx)


@pytest.mark.db
def test_inadmissible_receipt_with_valid_hashes_fails_load(
    app_conn: Conn, ctx: Ctx
) -> None:
    """A hostile qw_app writes a receipt that skips the domain's acknowledgement
    rule but carries correct hashes (reviewer probe): load must refuse it."""
    history = propose(app_conn, ctx)
    with tn.tenant_transaction(app_conn, ctx.tenant) as tx:
        row = tx.conn.execute(
            "SELECT step_up_at, now() FROM app.session WHERE id = %s", (ctx.session,)
        ).fetchone()
        assert row is not None
        consent = AdoptionConsent(str(ctx.owner), ps.step_up_receipt_id(
            ctx.session, row[0]), row[1], frozenset({UNKNOWN}), "x")  # fmt: skip
        forged = replace(history.adopt(1, consent).receipts[0], acknowledged=())
        ps._insert_receipt(tx, ctx.policy, ctx.owner, ctx.session, row[0], forged)
    with pytest.raises(ps.PolicyStoreError, match="integrity"):
        load(app_conn, ctx)


@pytest.mark.db
def test_adoption_holds_the_session_so_a_revoke_waits(
    runtime_urls: dict[str, str], app_conn: Conn, ctx: Ctx
) -> None:
    propose(app_conn, ctx)
    with (
        tn.runtime_connection(runtime_urls["qw_app"]) as other,
        tn.tenant_transaction(app_conn, ctx.tenant),
    ):
        adopt_tx = tn.TenantTx(app_conn, ctx.tenant)
        digest = ps.load(adopt_tx, ctx.policy).versions[0].content_hash  # type: ignore[union-attr]
        ps.adopt(adopt_tx, ctx.policy, 1, digest, principal=ctx.owner,
                 session_id=ctx.session, window=WINDOW,
                 acknowledged=frozenset({UNKNOWN}), text="SYNTHETIC")  # fmt: skip
        with (
            pytest.raises(psycopg.errors.LockNotAvailable),
            tn.tenant_transaction(other, ctx.tenant) as tx,
        ):
            tx.conn.execute("SET LOCAL lock_timeout = '200ms'")
            tn.revoke_session(tx, ctx.session)
