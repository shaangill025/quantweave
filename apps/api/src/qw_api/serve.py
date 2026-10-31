"""`qw-api`: build the API's Settings from the environment and serve it with uvicorn
(T060; owner decisions 106 and 107). Secrets come only from files: the
QW_SECRET_KEY_FILE key (one line of visible ASCII, at least 32 bytes) and the
optional QW_NOTIFICATION_KEYS_FILE keyring (JSON: {"active": kid, "seal_keys":
{kid: key}, "id_key": key}); QW_SECRET_KEY or QW_NOTIFICATION_KEYS set inline
refuses the start (`qw_adapters.file_secrets` has the file rules). QW_DATABASE_URL
is a passfile-only libpq connection string for exactly a qw_app login, checked
once before serving (unreachable: `refused: database unavailable (<class or
SQLSTATE>)`, exit 2). Plain settings: QW_ALLOWED_ORIGINS and QW_TRUSTED_PROXIES
(comma-separated), QW_EDITION (hosted | self_hosted, default hosted),
QW_JURISDICTION, QW_RELEASE_DEPLOYMENT_ID, QW_API_HOST (an IP address, default
127.0.0.1), QW_API_PORT (default 8000; 0 picks a free port) and QW_WEB_DIR (owner
decision 109: the built web app, an absolute directory with an index.html,
served on the API's origin; unset, nothing static is served). uvicorn runs with
no reload, no proxy-header rewriting (the app applies trusted_proxies itself), no
access log (it would hold query strings) and no server header.

`qw-api --check-config` reads and checks the same configuration (the file
owner and mode rules apply to the user it runs as) without connecting anywhere:
exit 0 and `config ok: qw-api`, or exit 2 and `refused: <variable>: <reason>`.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import uvicorn
from qw_adapters import tenancy
from qw_adapters.file_secrets import (
    LIBPQ_INLINE,
    ConfigError,
    DatabaseUnavailable,
    check_runtime_login,
    passfile_conninfo,
    read_secret_file,
    refuse_inline,
    secret_text,
)
from qw_adapters.notification_store import Keyring
from qw_domain.release import Edition

from qw_api.app import Settings, create_app
from qw_api.security import LocalPasswordAuthenticator

INLINE_SECRETS = ("QW_SECRET_KEY", "QW_NOTIFICATION_KEYS")
DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8000


@dataclass(frozen=True, slots=True)
class ServeConfig:
    settings: Settings  # its repr holds no key
    database_url: str  # holds no password: passfile= only
    host: str
    port: int


def _list(environ: Mapping[str, str], name: str) -> frozenset[str]:
    return frozenset(p.strip() for p in environ.get(name, "").split(",") if p.strip())


def _keyring(data: bytes) -> Keyring:
    name = "QW_NOTIFICATION_KEYS_FILE"
    try:
        doc = json.loads(data)
    except (UnicodeDecodeError, ValueError):
        raise ConfigError(name, "not a JSON keyring") from None
    if not isinstance(doc, dict) or set(doc) != {"active", "seal_keys", "id_key"}:
        raise ConfigError(name, "needs exactly active, seal_keys and id_key")
    seals, active, id_key = doc["seal_keys"], doc["active"], doc["id_key"]
    if not isinstance(seals, dict) or not all(
        isinstance(v, str) for v in (*seals.values(), active, id_key)
    ):
        raise ConfigError(name, "seal_keys maps kids to keys; keys are strings")
    keys = {str(k): secret_text(name, v.encode()) for k, v in seals.items()}
    try:
        return Keyring(active, keys, secret_text(name, id_key.encode()))
    except ValueError as exc:  # Keyring's messages name no key
        raise ConfigError(name, str(exc)) from None


def _web_dir(environ: Mapping[str, str]) -> str | None:
    name, path = "QW_WEB_DIR", environ.get("QW_WEB_DIR")
    if path is None:
        return None
    if not os.path.isabs(path):
        raise ConfigError(name, "must be an absolute path")
    if not os.path.isdir(path):
        raise ConfigError(name, "not a directory")
    index = os.path.realpath(os.path.join(path, "index.html"))
    if os.path.commonpath([index, os.path.realpath(path)]) != os.path.realpath(path):
        raise ConfigError(name, "index.html resolves outside the directory")
    if not os.path.isfile(index):
        raise ConfigError(name, "has no index.html file (build the web app)")
    return path


def config_from_env(environ: Mapping[str, str] = os.environ) -> ServeConfig:
    refuse_inline(environ, INLINE_SECRETS + LIBPQ_INLINE)
    raw_key = read_secret_file(environ, "QW_SECRET_KEY_FILE") or b""
    secret_key = secret_text("QW_SECRET_KEY_FILE", raw_key)
    keys = read_secret_file(environ, "QW_NOTIFICATION_KEYS_FILE", required=False)
    database_url = passfile_conninfo(environ, "QW_DATABASE_URL")
    host = environ.get("QW_API_HOST", DEFAULT_HOST)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError("QW_API_HOST", "must be an IP address") from None
    web_dir = _web_dir(environ)
    port_text = environ.get("QW_API_PORT", str(DEFAULT_PORT))
    if not (port_text.isascii() and port_text.isdigit() and int(port_text) < 65536):
        raise ConfigError("QW_API_PORT", "must be a port number")
    try:
        edition = Edition(environ.get("QW_EDITION", Edition.HOSTED.value))
    except ValueError:
        raise ConfigError("QW_EDITION", "must be hosted or self_hosted") from None
    try:
        settings = Settings(
            allowed_origins=_list(environ, "QW_ALLOWED_ORIGINS"),
            secret_key=secret_key,
            trusted_proxies=_list(environ, "QW_TRUSTED_PROXIES"),
            deployment=edition,
            jurisdiction=environ.get("QW_JURISDICTION") or None,
            notification_keys=None if keys is None else _keyring(keys),
            release_deployment_id=environ.get("QW_RELEASE_DEPLOYMENT_ID") or None,
            web_dir=web_dir,
        )
    except ValueError as exc:  # Settings' messages name no secret
        raise ConfigError("settings", str(exc)) from None
    return ServeConfig(settings, database_url, host, int(port_text))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qw-api", description=__doc__)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = config_from_env()
        if args.check_config:
            print("config ok: qw-api")
            return 0
        # fail fast: exactly a qw_app login, never a superuser, and reachable
        check_runtime_login(config.database_url, "qw_app")
    except (ConfigError, DatabaseUnavailable, tenancy.TenancyError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    url = config.database_url
    app = create_app(
        config.settings,
        lambda: tenancy.runtime_connection(url, role="qw_app"),
        LocalPasswordAuthenticator(),
    )
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    uvicorn.run(
        app, host=config.host, port=config.port, reload=False, workers=None,
        http="h11", ws="none", loop="asyncio", interface="asgi3",
        proxy_headers=False, server_header=False, access_log=False,
        log_level="info",
    )  # fmt: skip
    return 0
