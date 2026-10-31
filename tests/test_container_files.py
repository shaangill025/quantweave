"""Static checks of the self-hosted container files (T060; owner decisions 13,
106-109). They parse deploy/Dockerfile and deploy/compose.yaml; they do not build
or run an image (see the T060 receipt for what was and was not run)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "deploy" / "Dockerfile").read_text()
COMPOSE: dict[str, Any] = yaml.safe_load((ROOT / "deploy" / "compose.yaml").read_text())
SERVICES: dict[str, dict[str, Any]] = COMPOSE["services"]
APP = ("db-init", "migrate", "api", "worker")
INLINE = {"QW_SECRET_KEY", "QW_NOTIFICATION_KEYS", "PGPASSWORD", "PGSERVICE",
          "PGSERVICEFILE", "POSTGRES_PASSWORD"}  # fmt: skip


def test_base_images_are_pinned() -> None:
    rootfs = json.loads((ROOT / "tools" / "candidate_rootfs.json").read_text())
    python = re.search(r"^ARG PYTHON_IMAGE=(\S+)$", DOCKERFILE, re.M)
    assert python is not None
    assert python[1] == (
        "docker.io/library/python:3.13-slim@" + rootfs["base_manifest_digest"]
    )
    assert re.search(r"^ARG NODE_IMAGE$", DOCKERFILE, re.M)  # no default
    assert "${QW_NODE_IMAGE:?" in SERVICES["api"]["build"]["args"]["NODE_IMAGE"]
    assert SERVICES["db"]["image"].startswith("${QW_POSTGRES_IMAGE:?")
    froms = re.findall(r"^FROM (\S+)", DOCKERFILE, re.M)
    assert froms == ["${NODE_IMAGE}", "${PYTHON_IMAGE}", "${PYTHON_IMAGE}"]


def test_tool_versions_match_ci() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    web = (ROOT / ".github" / "workflows" / "web.yml").read_text()
    uv = re.search(r"'uv==([0-9.]+) --hash=sha256:[0-9a-f]{64}'", DOCKERFILE)
    assert uv is not None and f'version: "{uv[1]}"' in ci
    assert 'node-version: "22.22.0"' in web and "node:22.22.0" in DOCKERFILE
    assert "uv sync --locked --no-dev" in DOCKERFILE


def test_build_tools_are_hash_pinned() -> None:
    """No unlocked download builds the image: uv_build is pre-installed by hash
    and used without build isolation; the pnpm tarball is checked by sha512."""
    build = re.search(r"'uv-build==([0-9.]+) --hash=sha256:[0-9a-f]{64}'", DOCKERFILE)
    assert build is not None
    assert "--require-hashes \\\n      -r /tmp/uv-build.txt" in DOCKERFILE
    assert "--no-build-isolation" in DOCKERFILE
    manager = json.loads((ROOT / "package.json").read_text())["packageManager"]
    version = manager.removeprefix("pnpm@")
    assert re.search(
        rf"npm pack --silent pnpm@{re.escape(version)} \\\n && echo '[0-9a-f]{{128}}"
        rf"  pnpm-{re.escape(version)}\.tgz' \| sha512sum -c -",
        DOCKERFILE,
    )
    assert "corepack" not in DOCKERFILE


def test_final_stage_is_not_root_and_holds_no_secret() -> None:
    final = DOCKERFILE.rsplit("\nFROM ", 1)[1]
    assert re.findall(r"^USER (\S+)$", final, re.M) == ["65534:65534"]
    assert not re.search(r"secret|password|pgpass", final.split("\n", 1)[1], re.I)
    uids = dict(re.findall(r"(qw-[a-z]+):(\d+)", DOCKERFILE))
    want = {"api": "qw-api", "worker": "qw-worker", "db-init": "qw-dbinit",
            "migrate": "qw-migrate"}  # fmt: skip
    for service, user in want.items():
        assert SERVICES[service]["user"] == f"{uids[user]}:{uids[user]}"
    assert len(set(uids.values())) == len(uids) == 4


def test_services_are_hardened_and_take_secrets_only_as_files() -> None:
    used: list[str] = []
    for name in APP:
        svc = SERVICES[name]
        assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"]
        assert svc["security_opt"] == ["no-new-privileges:true"]
        env: dict[str, str] = svc["environment"]
        assert not INLINE & set(env)
        for key, value in env.items():
            if key.endswith("_DATABASE_URL"):
                assert "passfile=/run/secrets/" in value and "password" not in value
            if key.endswith("_FILE") or key.startswith("QW_DBINIT_PASSFILE_"):
                assert value.startswith("/run/secrets/")
        used += svc["secrets"]
    used += SERVICES["db"]["secrets"]
    assert sorted(used) == sorted(COMPOSE["secrets"])  # each in exactly one service
    for spec in COMPOSE["secrets"].values():
        assert spec["file"].startswith("${QW_SECRETS_DIR:?")
    assert SERVICES["db"]["environment"]["POSTGRES_PASSWORD_FILE"].startswith(
        "/run/secrets/"
    )
    assert "ports" not in SERVICES["db"]
    assert SERVICES["api"]["ports"] == ["127.0.0.1:8000:8000"]


def test_db_init_runs_only_on_request_and_migrate_gates_the_runtime() -> None:
    assert SERVICES["db-init"]["profiles"] == ["init"]
    assert "profiles" not in SERVICES["migrate"]
    assert (
        "qw_migrate_login"
        in SERVICES["migrate"]["environment"]["QW_MIGRATE_DATABASE_URL"]
    )
    for name in ("api", "worker"):
        assert SERVICES[name]["depends_on"] == {
            "migrate": {"condition": "service_completed_successfully"}
        }


def test_db_is_hardened_and_checked_over_tcp() -> None:
    db = SERVICES["db"]
    tcp = ["CMD", "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "quantweave"]
    assert db["healthcheck"]["test"] == tcp
    assert db["user"] == "999:999" and db["cap_drop"] == ["ALL"]
    assert db["security_opt"] == ["no-new-privileges:true"]


def test_api_serves_the_built_web_app_and_checks_a_real_route() -> None:
    """Decision 109: the web build is copied to the directory the api serves; the
    health check names the real liveness route (all API routes are under
    /api/v1, and anything else is a static path)."""
    api = SERVICES["api"]
    copied = re.search(r"^COPY --from=web /src/apps/web/dist (\S+)$", DOCKERFILE, re.M)
    assert copied is not None
    assert copied[1] == api["environment"]["QW_WEB_DIR"] == "/opt/qw/web"
    assert "Not served" not in DOCKERFILE
    probe = api["healthcheck"]["test"][-1]
    assert "'http://127.0.0.1:8000/api/v1/health/live'" in probe
    for name in APP:
        if name != "api":
            assert "QW_WEB_DIR" not in SERVICES[name]["environment"]


def test_tmpfs_and_networks() -> None:
    for name in APP:
        (tmp,) = SERVICES[name]["tmpfs"]
        path, options = tmp.split(":", 1)
        assert path == "/tmp" and {"noexec", "nosuid", "nodev"} <= set(
            options.split(",")
        )
    assert COMPOSE["networks"]["backend"] == {"internal": True}
    assert "internal" not in COMPOSE["networks"]["frontend"]
    for name in ("db", "db-init", "migrate", "worker"):
        assert SERVICES[name]["networks"] == ["backend"]
    assert SERVICES["api"]["networks"] == ["backend", "frontend"]
