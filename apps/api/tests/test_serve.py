"""`qw-api` (T060; owner decisions 106-107): Settings from the environment with
file-only secrets, and the console script really serving `/health/live` on an
ephemeral 127.0.0.1 port as a non-superuser qw_app login (real PostgreSQL 16).
Every key and password is a SYNTHETIC value in a temporary 0600 file."""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from qw_adapters.file_secrets import ConfigError, InlineSecretError
from qw_adapters.migrations import migrate
from qw_api import serve
from qw_domain.release import Edition

_path = Path(__file__).parents[3] / "packages" / "adapters" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("qw_pg_harness", _path)
assert _spec is not None and _spec.loader is not None
_harness: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
pg_admin_url, database_url, conn = (
    _harness.pg_admin_url, _harness.database_url, _harness.conn
)  # fmt: skip

CANARY = "SYNTHETIC-canary-api-7c2e4a6b8d0f1e3a5c7b9d1f3e5a7c9b"
KEY = "SYNTHETIC-qw-api-secret-key-000000000000"
SCRIPT = Path(sys.executable).parent / "qw-api"


def private(path: Path, text: str, mode: int = 0o600) -> str:
    path.write_text(text)
    path.chmod(mode)
    return str(path)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    passfile = private(tmp_path / "pgpass", f"*:*:*:qw_app_login:{CANARY}\n")
    return {
        "QW_SECRET_KEY_FILE": private(tmp_path / "key", KEY + "\n"),
        "QW_ALLOWED_ORIGINS": "https://pi.example, http://localhost:5173",
        "QW_DATABASE_URL": f"host=/run/pg dbname=qw user=qw passfile={passfile}",
    }


def test_settings_come_from_files_and_plain_variables(
    env: dict[str, str], tmp_path: Path
) -> None:
    keys = {"active": "k1", "seal_keys": {"k1": CANARY + "-seal"}, "id_key": CANARY}
    env["QW_NOTIFICATION_KEYS_FILE"] = private(tmp_path / "nk", json.dumps(keys))
    env |= {"QW_EDITION": "self_hosted", "QW_JURISDICTION": "CA-ON",
            "QW_RELEASE_DEPLOYMENT_ID": "pilot-1", "QW_TRUSTED_PROXIES": "10.0.0.0/8",
            "QW_API_PORT": "0"}  # fmt: skip
    config = serve.config_from_env(env)
    s = config.settings
    assert s.secret_key == KEY.encode() and config.port == 0
    assert s.allowed_origins == {"https://pi.example", "http://localhost:5173"}
    assert (s.deployment, s.jurisdiction, s.release_deployment_id) == (
        Edition.SELF_HOSTED, "CA-ON", "pilot-1"
    )  # fmt: skip
    assert s.trusted_proxies == {"10.0.0.0/8"} and s.notification_keys is not None
    assert s.notification_keys.id_key == CANARY.encode()
    assert CANARY not in repr(config) and KEY not in repr(config)
    assert KEY not in str(config)
    plain = ("QW_SECRET_KEY_FILE", "QW_ALLOWED_ORIGINS", "QW_DATABASE_URL")
    default = serve.config_from_env({k: env[k] for k in plain})
    assert (default.host, default.port) == ("127.0.0.1", 8000)
    assert default.settings.notification_keys is None
    assert default.settings.deployment is Edition.HOSTED


def refused(env: dict[str, str], error: type[ConfigError] = ConfigError) -> str:
    with pytest.raises(error) as info:
        serve.config_from_env(env)
    text = f"{info.value} {info.value!r}"
    assert CANARY not in text and KEY not in text
    return str(info.value)


def test_inline_secrets_are_refused(env: dict[str, str]) -> None:
    for name in ("QW_SECRET_KEY", "QW_NOTIFICATION_KEYS", "PGPASSWORD"):
        assert refused(env | {name: CANARY}, InlineSecretError).startswith(name)
    url = env["QW_DATABASE_URL"]
    for bad in (f"{url} password={CANARY}",
                f"postgresql://qw:{CANARY}@db/qw?{url.split()[-1]}"):  # fmt: skip
        refused(env | {"QW_DATABASE_URL": bad}, InlineSecretError)


def test_unsafe_files_and_bad_values_are_refused(
    env: dict[str, str], tmp_path: Path
) -> None:
    Path(env["QW_SECRET_KEY_FILE"]).chmod(0o640)
    assert "group or world" in refused(env)
    Path(env["QW_SECRET_KEY_FILE"]).chmod(0o600)
    assert "not set" in refused({k: v for k, v in env.items() if "KEY" not in k})
    assert "cannot open" in refused(env | {"QW_SECRET_KEY_FILE": str(tmp_path / "x")})
    short = private(tmp_path / "short", "SYNTHETIC-too-short")
    assert "32 bytes" in refused(env | {"QW_SECRET_KEY_FILE": short})
    passfile = conninfo_to_dict(env["QW_DATABASE_URL"])["passfile"]
    assert isinstance(passfile, str)
    Path(passfile).chmod(0o604)
    assert refused(env).startswith("QW_DATABASE_URL passfile")
    Path(passfile).chmod(0o600)
    for name, value in (("QW_API_HOST", "localhost"), ("QW_API_PORT", "65536"),
                        ("QW_API_PORT", "-1"), ("QW_EDITION", "cloud"),
                        ("QW_ALLOWED_ORIGINS", ""), ("QW_TRUSTED_PROXIES", "x/99"),
                        ("QW_DATABASE_URL", "host=/run/pg dbname=qw")):  # fmt: skip
        refused(env | {name: value})
    shared = {"active": "k1", "seal_keys": {"k1": KEY}, "id_key": CANARY}
    for text in (f"{{{CANARY}", json.dumps(shared), json.dumps([CANARY]),
                 json.dumps({**shared, "seal_keys": {"k1": 1}})):  # fmt: skip
        nk = private(tmp_path / "nk", text)
        assert refused(env | {"QW_NOTIFICATION_KEYS_FILE": nk}).startswith(
            ("QW_NOTIFICATION_KEYS_FILE", "settings")
        )  # fmt: skip


def run_script(env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(SCRIPT)], env={"PATH": os.environ.get("PATH", "")} | env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip


def test_the_script_refuses_an_inline_key_without_echoing_it(
    env: dict[str, str],
) -> None:
    proc = run_script(env | {"QW_SECRET_KEY": CANARY})
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 2 and CANARY not in out
    assert out.startswith("refused: QW_SECRET_KEY: inline secrets are refused")


def test_the_script_refuses_an_unreachable_database(
    env: dict[str, str], tmp_path: Path
) -> None:
    passfile = conninfo_to_dict(env["QW_DATABASE_URL"])["passfile"]
    url = f"host={tmp_path / 'no-socket'} dbname=qw user=qw passfile={passfile}"
    proc = run_script(env | {"QW_DATABASE_URL": url})
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 2
    assert out == "refused: database unavailable (OperationalError)\n"


@pytest.fixture
def app_login(conn: Any, database_url: str, pg_admin_url: str) -> Iterator[str]:
    """A migrated database and a qw_app login with a generated password."""
    migrate(conn)
    login, password = f"qwtest_api_{uuid.uuid4().hex[:12]}", uuid.uuid4().hex
    conn.execute(
        sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {} "
                "IN ROLE qw_app").format(sql.Identifier(login), sql.Literal(password))
    )  # fmt: skip
    yield f"{login}:{password}"
    conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s",
        (login,),
    )
    conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))


@pytest.mark.db
def test_qw_api_serves_liveness_on_an_ephemeral_loopback_port(
    env: dict[str, str], app_login: str, database_url: str, tmp_path: Path
) -> None:
    login = app_login.split(":")[0]
    passfile = private(tmp_path / "app.pgpass", f"*:*:*:{app_login}\n")
    base = {k: v for k, v in conninfo_to_dict(database_url).items() if k != "password"}
    url = make_conninfo("", **{**base, "user": login, "passfile": passfile})
    proc = run_script(env | {"QW_DATABASE_URL": url, "QW_API_PORT": "0"})
    lines: queue.Queue[str] = queue.Queue()
    assert proc.stdout is not None
    stdout = proc.stdout

    def pump() -> None:
        for line in stdout:
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()
    seen: list[str] = []
    try:
        while not (m := re.search(r"running on http://127\.0\.0\.1:(\d+)",
                                  "".join(seen))):  # fmt: skip
            seen.append(lines.get(timeout=60))
        api = f"http://127.0.0.1:{m.group(1)}/api/v1"
        live = httpx.get(f"{api}/health/live", timeout=10)
        assert (live.status_code, live.json()) == (200, {"status": "healthy"})
        assert (
            "server" not in live.headers and live.headers["cache-control"] == "no-store"
        )
        assert httpx.head(f"{api}/health/live", timeout=10).status_code == 200
        assert httpx.get(f"{api}/nope", timeout=10).status_code == 404
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    while not lines.empty():
        seen.append(lines.get())
    out = "".join(seen)
    # uvicorn shuts down gracefully, then re-raises the signal it caught.
    assert proc.returncode == -signal.SIGTERM, out
    assert "Application shutdown complete" in out and "Finished server process" in out
    assert "GET /api/v1/health/live 200" in out  # the app's own request line
    assert KEY not in out and CANARY not in out and app_login.split(":")[1] not in out


@pytest.mark.db
def test_qw_api_refuses_a_superuser_database_login(
    env: dict[str, str], database_url: str, tmp_path: Path, conn: Any
) -> None:
    migrate(conn)
    base = {k: v for k, v in conninfo_to_dict(database_url).items() if k != "password"}
    # A password-auth harness URL has its own passfile; trust auth ignores this one.
    unused = private(tmp_path / "su.pgpass", "*:*:*:*:unused\n")
    url = make_conninfo("", **{"passfile": unused, **base})
    proc = run_script(env | {"QW_DATABASE_URL": url})
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 2 and "refused:" in out and "superuser" in out
