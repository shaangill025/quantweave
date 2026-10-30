"""File-only secrets and passfile-only database connection strings for the service
entry points (T060; owner decisions 106-108).

A secret is read only from the file a `*_FILE` variable names: an absolute path to
a regular file owned by this process's user, with no group or other permission bits
(mode & 0o077 == 0), of 1 to MAX_SECRET_BYTES bytes. The same secret given inline
(say QW_SECRET_KEY) refuses the start. A database connection string is a libpq one
whose `passfile=` names such a file; it may not carry a password (key/value or URL
form), an SSL key password or a service reference, and libpq's own PGPASSWORD,
PGSERVICE and PGSERVICEFILE are refused too. libpq re-opens the passfile by path
when it connects, after this check, so the passfile may not be a symlink and its
parent directory may not be group or world writable (no one else can swap it).
A file whose reported size is 0 is refused, which also rejects procfs and sysfs
files such as /proc/self/environ. Errors name the variable and the reason, never a
value. These files are unencrypted on disk (decision 107).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Mapping

import psycopg
from psycopg.conninfo import conninfo_to_dict

from qw_adapters import tenancy

MAX_SECRET_BYTES = 16 * 1024
# Read by libpq from the environment: each could carry a password inline.
LIBPQ_INLINE = ("PGPASSWORD", "PGSERVICE", "PGSERVICEFILE")
_REFUSED_KEYS = ("password", "sslpassword", "service", "servicefile")


class ConfigError(Exception):
    """A setting is missing or unsafe. Carries the variable name and a reason only."""

    def __init__(self, variable: str, reason: str) -> None:
        super().__init__(f"{variable}: {reason}")
        self.variable, self.reason = variable, reason


class InlineSecretError(ConfigError):
    """A secret was given inline in the environment instead of through a file."""


def refuse_inline(environ: Mapping[str, str], names: Iterable[str]) -> None:
    for name in names:
        if name in environ:
            hint = "libpq passfile" if name.startswith("PG") else f"{name}_FILE"
            raise InlineSecretError(name, f"inline secrets are refused; use a {hint}")


def check_secret_file(variable: str, path: str, *, by_path: bool = False) -> bytes:
    """The bytes of `path` after the file checks; ConfigError otherwise. `by_path`:
    another reader (libpq) re-opens the path later, so it may not be a symlink and
    its directory may not be writable by group or others."""
    if not os.path.isabs(path):
        raise ConfigError(variable, "must be an absolute path")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if by_path:
        try:
            if stat.S_ISLNK(os.lstat(path).st_mode):
                raise ConfigError(variable, "the file is a symlink")
            if os.stat(os.path.dirname(path)).st_mode & 0o022:
                raise ConfigError(variable, "its directory is group or world writable")
        except OSError as exc:
            why = f"cannot stat the file ({exc.strerror})"
            raise ConfigError(variable, why) from None
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ConfigError(variable, f"cannot open the file ({exc.strerror})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(variable, "not a regular file")
        if st.st_size == 0:  # also procfs/sysfs files, which report 0
            raise ConfigError(variable, "the file is empty (or reports no size)")
        if st.st_uid != os.geteuid():
            raise ConfigError(variable, "the file is not owned by this user")
        if st.st_mode & 0o077:
            raise ConfigError(variable, "the file is group or world accessible")
        data = b""
        while len(data) <= MAX_SECRET_BYTES and (chunk := os.read(fd, 4096)):
            data += chunk
    finally:
        os.close(fd)
    if not data:
        raise ConfigError(variable, "the file is empty")
    if len(data) > MAX_SECRET_BYTES:
        raise ConfigError(variable, f"the file exceeds {MAX_SECRET_BYTES} bytes")
    return data


def read_secret_file(
    environ: Mapping[str, str], variable: str, *, required: bool = True
) -> bytes | None:
    """The checked bytes of the file `environ[variable]` names; None when unset and
    not required."""
    path = environ.get(variable)
    if path is None:
        if required:
            raise ConfigError(variable, "not set")
        return None
    return check_secret_file(variable, path)


def secret_text(variable: str, data: bytes) -> bytes:
    """A text key: one trailing newline dropped, then visible ASCII only."""
    text = data.removesuffix(b"\n").removesuffix(b"\r")
    if not text or not all(0x21 <= b <= 0x7E for b in text):
        raise ConfigError(variable, "the key must be one line of visible ASCII")
    return text


def passfile_conninfo(environ: Mapping[str, str], variable: str) -> str:
    """`environ[variable]`, a libpq connection string that authenticates through a
    checked passfile and carries no secret itself."""
    refuse_inline(environ, LIBPQ_INLINE)
    conninfo = environ.get(variable)
    if not conninfo:
        raise ConfigError(variable, "not set")
    try:
        params = conninfo_to_dict(conninfo)
    except psycopg.Error:
        raise ConfigError(variable, "not a libpq connection string") from None
    for key in _REFUSED_KEYS:
        if key in params:
            raise InlineSecretError(variable, f"carries {key}=; use passfile= only")
    passfile = params.get("passfile")
    if not isinstance(passfile, str) or not passfile:
        raise ConfigError(variable, "needs passfile= (owner decision 107)")
    check_secret_file(f"{variable} passfile", passfile, by_path=True)
    return conninfo


class DatabaseUnavailable(Exception):
    """The startup connection failed; the message holds the error class or SQLSTATE
    only (the connection string holds no secret, but no server text is shown)."""


def check_runtime_login(conninfo: str, role: tenancy.RuntimeRole) -> None:
    """Connect once as exactly a `role` login (tenancy.TenancyError otherwise)."""
    try:
        with tenancy.runtime_connection(conninfo, role=role):
            pass
    except psycopg.Error as exc:
        why = exc.sqlstate or type(exc).__name__
        raise DatabaseUnavailable(f"database unavailable ({why})") from None
