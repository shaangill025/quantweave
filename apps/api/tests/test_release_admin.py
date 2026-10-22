"""`qw-release-admin` (owner decision 45, 2026-10-22): the operator
command that records this installation's deployment and its trusted maintainer
keys, on a migrated PostgreSQL 16 database. It runs only on an exactly-qw_release
login (as #140 declared the worker's and bootstrap's roles), stores public key
material only, and a test-only key stores none. Every id and key is SYNTHETIC."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from qw_api import Settings
from qw_api import release_admin as ra

ROOT = Path(__file__).parents[3]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls = api.runtime_urls
DEPLOY = ["record-deployment", "--deployment-id", "deploy-synth", "--edition",
          "self_hosted", "--operator", "operator-synth", "--initial-schema", "1",
          "--allow-test-keys"]  # fmt: skip
TEST_KEY = ["add-key", "--key-id", "maint-key-synth-1", "--principal", "maint-synth",
            "--algorithm", "hmac-sha256-test", "--valid-from",
            "2026-10-01T00:00:00Z", "--test-only"]  # fmt: skip


def run(monkeypatch: pytest.MonkeyPatch, url: str | None, *argv: str) -> int:
    if url is None:
        monkeypatch.delenv("QW_RELEASE_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("QW_RELEASE_DATABASE_URL", url)
    return ra.main(list(argv))


@pytest.mark.db
def test_the_operator_records_the_deployment_and_its_keys(
    runtime_urls: dict[str, str], conn: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:  # fmt: skip
    url, tenant = runtime_urls["qw_release"], str(uuid.uuid4())
    assert run(monkeypatch, url, *DEPLOY, "--tenant", tenant) == 0
    assert run(monkeypatch, url, *DEPLOY, "--tenant", tenant) == 0  # unchanged
    assert "unchanged" in capsys.readouterr().out
    assert run(monkeypatch, url, *DEPLOY, "--tenant", str(uuid.uuid4())) == 1
    assert "deployment_frozen" in capsys.readouterr().err
    row = conn.execute("SELECT edition, operators, tenants, allows_test_keys, "
                       "solo_maintainer FROM app.release_deployment"
                       ).fetchone()  # fmt: skip
    assert row == ("self_hosted", ["operator-synth"], [tenant], True, False)
    assert run(monkeypatch, url, *TEST_KEY) == 0
    assert "no key material is stored" in capsys.readouterr().out
    assert conn.execute("SELECT public_key, test_only FROM app.release_trusted_key"
                        ).fetchall() == [(None, True)]  # fmt: skip
    private = tmp_path / "maint.pem"  # SYNTHETIC: a private key is never stored
    private.write_text(
        "-----BEGIN PRIVATE KEY-----\nSYNTHETIC\n-----END PRIVATE KEY-----\n"
    )
    public = tmp_path / "maint.pub"
    public.write_text("SYNTHETIC-public-key\n")
    asym = ["add-key", "--key-id", "maint-key-synth-2", "--principal", "maint-synth",
            "--algorithm", "ed25519", "--valid-from",
            "2026-10-01T00:00:00Z"]  # fmt: skip
    for path, code in ((private, "private_key_material"),
                       (public, "algorithm_not_production")):  # fmt: skip
        assert run(monkeypatch, url, *asym, "--public-key-file", str(path)) == 1
        assert code in capsys.readouterr().err
    revoke = ["revoke-key", "--key-id", "maint-key-synth-1", "--reason",
              "SYNTHETIC: rotated"]  # fmt: skip
    assert run(monkeypatch, url, *revoke) == 0
    assert run(monkeypatch, url, *revoke) == 1
    assert "already_revoked" in capsys.readouterr().err
    assert conn.execute("SELECT count(*) FROM app.release_trusted_key").fetchone() == (
        1,
    )


@pytest.mark.db
def test_only_an_exact_release_login_and_valid_input_reach_the_store(
    runtime_urls: dict[str, str], conn: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    for role in ("qw_app", "qw_worker", "qw_assessor"):
        assert run(monkeypatch, runtime_urls[role], *TEST_KEY) == 1
        assert "exactly a member of qw_release" in capsys.readouterr().err
    assert run(monkeypatch, None, *TEST_KEY) == 2
    url = runtime_urls["qw_release"]
    for bad in (["--valid-from", "2026-10-01T00:00:00"],  # no offset
                ["--valid-from", "2026-10-01T00:00:00Z", "--valid-until",
                 "2026-09-01T00:00:00Z"]):  # fmt: skip
        args = [
            a for a in TEST_KEY if a not in ("--valid-from", "2026-10-01T00:00:00Z")
        ]
        assert run(monkeypatch, url, *args, *bad) in (1, 2)
    with pytest.raises(SystemExit):  # material and test-only are exclusive
        run(monkeypatch, url, *TEST_KEY, "--public-key-file", "/dev/null")
    assert conn.execute("SELECT count(*) FROM app.release_trusted_key").fetchone() == (
        0,
    )
    assert conn.execute("SELECT count(*) FROM app.release_deployment").fetchone() == (
        0,
    )


def test_the_server_runs_as_an_operator_set_deployment() -> None:
    plain = Settings(frozenset({api.ORIGIN}), api.KEY)
    assert plain.release_deployment_id is None  # not set: no deployment
    set_ = Settings(
        frozenset({api.ORIGIN}), api.KEY, release_deployment_id="deploy-synth"
    )
    assert set_.release_deployment_id == "deploy-synth"
    for bad in ("", "bad id", "x" * 129):
        with pytest.raises(ValueError, match="release_deployment_id"):
            Settings(frozenset({api.ORIGIN}), api.KEY, release_deployment_id=bad)
