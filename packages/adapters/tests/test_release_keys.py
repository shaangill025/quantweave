"""Owner decision 45 (2026-10-22): the operator setup that backs the
release reads. `0035_release_keys.sql` and qw_adapters.release_store: the operator
(qw_release) records the deployment and the trusted maintainer keys, and revokes a
key with an append-only, DB-clock revocation. Only public key material is ever
stored; a test-only key stores none (its verifier is an in-process test hook). The
worker (qw_worker) reads the keys and verifies releases against them; qw_app has no
privilege. SYNTHETIC fixtures and the test-only HMAC key of test_release."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
import test_release as tr
from psycopg.conninfo import make_conninfo
from qw_adapters import release_store as rs
from qw_adapters import tenancy as tn
from qw_domain import release as rl

Conn = psycopg.Connection[Any]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


st = _module(
    "qw_release_store_tests", Path(__file__).with_name("test_release_store.py")
)
DEP, install, record, setup, signed = st.DEP, st.install, st.record, st.setup, st.signed
KEY, REL, T0 = tr.KEY, tr.REL, tr.TRUST.valid_from
HOOK = {KEY.key_id: KEY}  # the in-process test verifier, never stored
TEST_KEY = rs.KeyRecord(KEY.key_id, "maint-synth", KEY.algorithm, T0, None, None,
                        test_only=True)  # fmt: skip
KEY_TABLES = ("release_trusted_key", "release_key_revocation")
SECRET = KEY.secret.decode()


def test_a_key_is_valid_only_inside_its_window() -> None:
    """Domain: `valid_until` ends a key for signatures approved at or after it;
    revocation ends it at the read time (both `key_not_valid`)."""
    sr = signed()
    assert rl.verify_release(sr, tr.KEYS, REL, True) == []
    ended = {KEY.key_id: replace(tr.TRUST, valid_until=REL)}
    assert rl.verify_release(sr, ended, REL, True) == ["key_not_valid"]
    later = {KEY.key_id: replace(tr.TRUST, valid_until=REL + timedelta(seconds=1))}
    assert rl.verify_release(sr, later, REL + timedelta(days=9), True) == []
    with pytest.raises(rl.ReleaseError, match="key_window"):
        replace(tr.TRUST, valid_until=T0)


@pytest.mark.db
def test_the_operator_records_keys_and_no_secret_is_stored(
    release_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    assert rs.record_key(release_conn, TEST_KEY) is True
    assert rs.record_key(release_conn, TEST_KEY) is False  # the same key again
    with pytest.raises(rs.ReleaseStoreError, match="key_conflict"):
        rs.record_key(release_conn, replace(TEST_KEY, principal_id="maint-other"))
    dump = conn.execute("SELECT string_agg(row_to_json(k)::text, ' ') FROM "
                        "app.release_trusted_key k").fetchone()  # fmt: skip
    assert dump is not None and SECRET not in dump[0]
    assert KEY.secret.hex() not in dump[0]
    row = conn.execute("SELECT public_key, test_only FROM app.release_trusted_key"
                       ).fetchone()  # fmt: skip
    assert row == (None, True)  # a test key stores no material at all
    for bad, code in (  # only reviewed asymmetric schemes may store material
        (replace(TEST_KEY, key_id="k-hmac", test_only=False, algorithm="hmac-sha256",
                 public_key="c2VjcmV0"), "algorithm_not_production"),
        (replace(TEST_KEY, key_id="k-asym", test_only=False, algorithm="ed25519",
                 public_key="MCowBQYDK2VwAyEA"), "algorithm_not_production"),
        (replace(TEST_KEY, key_id="k-test", public_key="any"), "test_key_material"),
        (replace(TEST_KEY, key_id="k-none", test_only=False), "public_key_required"),
        (replace(TEST_KEY, key_id="bad id"), "key_id"),
        (replace(TEST_KEY, key_id="k-win", valid_until=T0), "key_window"),
    ):  # fmt: skip
        with pytest.raises((rs.ReleaseStoreError, rl.ReleaseError), match=code):
            rs.record_key(release_conn, bad)
    count = conn.execute("SELECT count(*) FROM app.release_trusted_key").fetchone()
    assert count == (1,)
    keys = rs.stored_keys(worker_conn, HOOK)  # the worker reads, verifies
    assert keys == {KEY.key_id: tr.TRUST}
    assert rs.stored_keys(worker_conn) == {}  # no hook: a test key is not trusted


@pytest.mark.db
def test_a_stored_public_key_has_no_verifier_until_t009(
    release_conn: Conn, worker_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRODUCTION_ALGORITHMS is empty until T009 reviews a scheme; were one listed,
    its public key is stored, but with no verifier installed nothing verifies."""
    monkeypatch.setattr(rl, "PRODUCTION_ALGORITHMS", frozenset({"synthetic-asym"}))
    pub = replace(TEST_KEY, key_id="k-pub", algorithm="synthetic-asym",
                  public_key="SYNTHETIC-public-key", test_only=False)  # fmt: skip
    assert rs.record_key(release_conn, pub) is True
    k = rs.stored_keys(worker_conn, HOOK)["k-pub"]
    assert (k.algorithm, k.test_only, k.principal_id) == ("synthetic-asym", False,
                                                          "maint-synth")  # fmt: skip
    assert k.verifier.verify(b"m", "s") is False


@pytest.mark.db
def test_releases_verify_against_the_stored_keys_and_revocation_is_forward(
    release_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    rs.record_deployment(release_conn, DEP)  # the operator records the deployment
    rs.record_key(release_conn, TEST_KEY)
    keys = rs.stored_keys(worker_conn, HOOK)
    assert record(worker_conn, signed(), keys=keys) is True
    setup(release_conn, worker_conn, signed("rel-2", (1, 2, 2)))
    install(worker_conn, "rel-1")
    with pytest.raises(rs.ReleaseStoreError, match="not_found"):
        rs.revoke_key(release_conn, "k-none", "SYNTHETIC: unknown")
    for bad in ("", "x" * 501, "a\x07b"):
        with pytest.raises(rs.ReleaseStoreError, match="reason"):
            rs.revoke_key(release_conn, KEY.key_id, bad)
    at = rs.revoke_key(release_conn, KEY.key_id, "SYNTHETIC: rotated")
    with pytest.raises(rs.ReleaseStoreError, match="already_revoked"):
        rs.revoke_key(release_conn, KEY.key_id, "SYNTHETIC: again")
    after = rs.stored_keys(worker_conn, HOOK)
    assert after[KEY.key_id].revoked_at == at and isinstance(at, datetime)
    lg = rs.load(worker_conn, DEP.deployment_id, after)  # earlier installs replay
    assert lg is not None and lg.head(tr.COMP) == "rel-1"
    assert "key_not_valid" in lg.runnable("rel-1", at)  # but cannot run any more
    with pytest.raises(rs.ReleaseStoreError, match="unverified"):
        rs.install(worker_conn, DEP.deployment_id, after, "ev-1", "rel-2",
                   "operator-synth", "stepup-op-1", strategies=tr.NO)  # fmt: skip
    row = conn.execute("SELECT reason FROM app.release_key_revocation").fetchone()
    assert row == ("SYNTHETIC: rotated",)


@pytest.mark.db
def test_roles_rls_append_only_and_verified_key_reads(
    release_conn: Conn, worker_conn: Conn, app_conn: Conn, conn: Conn
) -> None:
    rs.record_key(release_conn, TEST_KEY)
    rs.revoke_key(release_conn, KEY.key_id, "SYNTHETIC: rotated")
    rows = conn.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity, "
        "has_any_column_privilege('qw_app', oid, 'SELECT'), "
        "has_table_privilege('qw_worker', oid, 'SELECT'), "
        "has_any_column_privilege('qw_worker', oid, 'INSERT'), "
        "has_any_column_privilege('qw_release', oid, 'INSERT') FROM pg_class "
        "WHERE relnamespace = 'app'::regnamespace AND relname = ANY (%s)",
        (list(KEY_TABLES),)).fetchall()  # fmt: skip
    assert sorted(rows) == sorted((t, True, True, False, True, False, True)
                                  for t in KEY_TABLES)  # fmt: skip
    for t in KEY_TABLES:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_conn.execute(f"SELECT 1 FROM app.{t}")
        same = f"UPDATE app.{t} SET event_hash = event_hash"
        gone = f"DELETE FROM app.{t}"
        for c, sql in ((worker_conn, gone), (conn, gone), (release_conn, same)):
            with pytest.raises(psycopg.Error):
                c.execute(sql)  # append-only, even for the owner
    stale = ("INSERT INTO app.release_trusted_key (key_id, principal_id, algorithm, "
             "public_key, test_only, valid_from, valid_until, recorded_at, "
             "event_hash) VALUES ('k-2', 'maint-synth', 'hmac-sha256-test', NULL, "
             "true, now(), NULL, now() - interval '1 day', "
             "repeat('a', 64))")  # fmt: skip
    material = stale.replace("NULL, true", "'secret', true").replace(
        "now() - interval '1 day'", "clock_timestamp()")  # fmt: skip
    for sql in (stale, material):
        with pytest.raises(psycopg.errors.CheckViolation):
            release_conn.execute(sql)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        worker_conn.execute(material.replace("'secret', true", "NULL, true"))

    def raw(sql: str) -> None:  # committed, triggers off (replica role)
        with conn.transaction():
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute(sql)

    for table, change in (
        ("release_trusted_key", "principal_id = 'maint-other'"),
        ("release_trusted_key", "valid_from = valid_from - interval '1 day'"),
        ("release_key_revocation", "revoked_at = revoked_at + interval '1 day'"),
        ("release_key_revocation", "reason = 'forged'"),
    ):
        conn.execute(f"CREATE TABLE public.keep AS SELECT * FROM app.{table}")
        raw(f"UPDATE app.{table} SET {change}")
        with pytest.raises(rs.ReleaseStoreError, match="integrity"):
            rs.stored_keys(worker_conn, HOOK)
        raw(f"DELETE FROM app.{table}")
        raw(f"INSERT INTO app.{table} SELECT * FROM public.keep")
        conn.execute("DROP TABLE public.keep")
    assert rs.stored_keys(worker_conn, HOOK)[KEY.key_id].revoked_at is not None


@pytest.mark.db
def test_the_operator_role_is_exclusive(
    runtime_urls: dict[str, str], conn: Conn
) -> None:
    for role in ("qw_app", "qw_worker", "qw_assessor"):
        with (
            pytest.raises(tn.TenancyError, match="exactly"),
            tn.runtime_connection(runtime_urls[role], role="qw_release"),
        ):
            pass
    conn.execute("CREATE ROLE qwtest_mixed_release LOGIN NOBYPASSRLS "
                 "IN ROLE qw_release, qw_worker")  # fmt: skip
    try:
        mixed = make_conninfo(runtime_urls["qw_app"], user="qwtest_mixed_release")
        for declared in (None, "qw_release", "qw_worker"):
            with (
                pytest.raises(tn.TenancyError, match="no other runtime role"),
                tn.runtime_connection(mixed, role=declared),
            ):
                pass
    finally:
        conn.execute("DROP ROLE qwtest_mixed_release")


@pytest.mark.db
def test_decision_47_only_the_operator_records_a_deployment(
    release_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    """Owner decision 47 (2026-10-22, "Operator only"): the worker, where untrusted
    generated code runs, cannot write who can see release history; it still reads
    the deployment its ledger replay needs."""
    with pytest.raises(rs.ReleaseStoreError, match="database"):
        rs.record_deployment(worker_conn, DEP)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        worker_conn.execute(
            "INSERT INTO app.release_deployment (deployment_id, edition, operators, "
            "tenants, initial_schema, allows_test_keys, recorded_at, event_hash, "
            "solo_maintainer) VALUES ('deploy-w', 'hosted', '{op}', '{}', 1, false, "
            "clock_timestamp(), repeat('a', 64), false)")  # fmt: skip
    assert conn.execute("SELECT count(*) FROM app.release_deployment").fetchone() == (
        0,
    )
    grants = conn.execute(
        "SELECT r, has_any_column_privilege(r, 'app.release_deployment', 'INSERT'), "
        "has_table_privilege(r, 'app.release_deployment', 'SELECT') FROM unnest("
        "ARRAY['qw_worker', 'qw_release', 'qw_app']) r ORDER BY r"
    ).fetchall()  # fmt: skip
    assert grants == [("qw_app", False, False), ("qw_release", True, True),
                      ("qw_worker", False, True)]  # fmt: skip
    policies = conn.execute(
        "SELECT policyname, cmd, roles::text FROM pg_policies WHERE schemaname = "
        "'app' AND tablename = 'release_deployment' ORDER BY 1"
    ).fetchall()  # fmt: skip
    assert policies == [
        ("history_reader", "SELECT", "{qw_migrate}"),  # 0037's definer reads
        ("installation_operator", "ALL", "{qw_release}"),
        ("installation_worker_read", "SELECT", "{qw_worker}"),
    ]
    assert rs.record_deployment(release_conn, DEP) is True
    lg = rs.load(worker_conn, DEP.deployment_id, tr.KEYS)  # the worker still reads
    assert lg is not None and lg.deployment == DEP
