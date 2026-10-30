"""File-only secrets and passfile-only connection strings (T060; owner decisions
106-107). Every value is a SYNTHETIC canary written to a temporary file; each
refusal is checked to name the variable and never echo the value."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from qw_adapters import file_secrets as fs

CANARY = "SYNTHETIC-canary-0f3c9e1a7b2d4c6e8a0b1c2d3e4f5a6b"


def write(tmp_path: Path, name: str, data: bytes, mode: int = 0o600) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    path.chmod(mode)
    return path


def refused(
    error: type[fs.ConfigError], call: object, *args: object, **kw: object
) -> fs.ConfigError:
    assert callable(call)
    with pytest.raises(error) as info:
        call(*args, **kw)
    exc = info.value
    assert CANARY not in str(exc) and CANARY not in repr(exc)
    assert CANARY not in exc.reason and CANARY not in exc.variable
    return exc


def test_reads_a_private_regular_file(tmp_path: Path) -> None:
    path = write(tmp_path, "key", CANARY.encode() + b"\n")
    env = {"QW_X_FILE": str(path)}
    data = fs.read_secret_file(env, "QW_X_FILE")
    assert data is not None and fs.secret_text("QW_X_FILE", data) == CANARY.encode()
    assert fs.read_secret_file({}, "QW_X_FILE", required=False) is None


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o610, 0o601, 0o644])
def test_group_or_world_bits_are_refused(tmp_path: Path, mode: int) -> None:
    env = {"QW_X_FILE": str(write(tmp_path, "key", CANARY.encode(), mode))}
    exc = refused(fs.ConfigError, fs.read_secret_file, env, "QW_X_FILE")
    assert exc.variable == "QW_X_FILE" and "group or world" in exc.reason


def test_unsafe_or_missing_files_are_refused(tmp_path: Path) -> None:
    cases = {
        "not set": {},
        "absolute": {"QW_X_FILE": "key"},
        "cannot open": {"QW_X_FILE": str(tmp_path / "absent")},
        "regular file": {"QW_X_FILE": str(tmp_path)},
        "empty": {"QW_X_FILE": str(write(tmp_path, "empty", b""))},
        "exceeds": {
            "QW_X_FILE": str(write(tmp_path, "big", b"a" * (fs.MAX_SECRET_BYTES + 1)))
        },
    }
    os.mkfifo(fifo := tmp_path / "fifo", 0o600)
    cases["regular file "] = {"QW_X_FILE": str(fifo)}  # opened without blocking
    for reason, env in cases.items():
        exc = refused(fs.ConfigError, fs.read_secret_file, env, "QW_X_FILE")
        assert reason.strip() in exc.reason, (reason, exc.reason)
    limit = write(tmp_path, "limit", b"a" * fs.MAX_SECRET_BYTES)
    assert fs.read_secret_file({"QW_X_FILE": str(limit)}, "QW_X_FILE")


def test_a_file_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(tmp_path, "key", CANARY.encode())
    monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)
    exc = refused(fs.ConfigError, fs.check_secret_file, "QW_X_FILE", str(path))
    assert "not owned" in exc.reason


def test_secret_text_is_one_visible_line() -> None:
    assert fs.secret_text("V", b"abc\r\n") == b"abc"
    for bad in (b"", b"\n", b"ab c", b"ab\ncd\n", b"\xe2\x82\xac", CANARY.encode()
                + b"\t"):  # fmt: skip
        refused(fs.ConfigError, fs.secret_text, "V", bad)


def test_inline_secrets_are_refused() -> None:
    for name in ("QW_SECRET_KEY", "PGPASSWORD"):
        exc = refused(fs.InlineSecretError, fs.refuse_inline, {name: CANARY}, [name])
        assert exc.variable == name and "inline" in exc.reason
    refused(fs.InlineSecretError, fs.refuse_inline, {"QW_SECRET_KEY": ""},
            ["QW_SECRET_KEY"])  # fmt: skip
    fs.refuse_inline({"QW_SECRET_KEY_FILE": "/x"}, ["QW_SECRET_KEY"])


def test_conninfo_needs_a_private_passfile_and_no_password(tmp_path: Path) -> None:
    passfile = write(tmp_path, "pgpass", f"*:*:*:qw:{CANARY}\n".encode())
    good = f"host=/run/pg dbname=qw user=qw passfile={passfile}"
    assert fs.passfile_conninfo({"QW_DB": good}, "QW_DB") == good
    url = f"postgresql://qw@db/qw?passfile={passfile}"
    assert fs.passfile_conninfo({"QW_DB": url}, "QW_DB") == url
    inline = [
        f"{good} password={CANARY}", f"{good} password=",
        f"{good} sslpassword={CANARY}",
        f"postgresql://qw:{CANARY}@db/qw?passfile={passfile}",
        f"postgresql://qw@db/qw?passfile={passfile}&password={CANARY}",
        f"{good} service=qw",
    ]  # fmt: skip
    for conninfo in inline:
        exc = refused(fs.InlineSecretError, fs.passfile_conninfo, {"QW_DB": conninfo},
                      "QW_DB")  # fmt: skip
        assert exc.variable == "QW_DB"
    for name in ("PGPASSWORD", "PGSERVICE", "PGSERVICEFILE"):
        env = {"QW_DB": good, name: CANARY}
        refused(fs.InlineSecretError, fs.passfile_conninfo, env, "QW_DB")
    passfile.chmod(0o644)
    loose = refused(fs.ConfigError, fs.passfile_conninfo, {"QW_DB": good}, "QW_DB")
    assert loose.variable == "QW_DB passfile"
    # servicefile= is refused by name, or as unknown by a libpq without it.
    for bad in ("", "host=db dbname=qw", f"host='{CANARY}", f"{CANARY} = = =",
                f"{good} servicefile=/etc/{CANARY}"):  # fmt: skip
        refused(fs.ConfigError, fs.passfile_conninfo, {"QW_DB": bad}, "QW_DB")


def test_procfs_files_reporting_no_size_are_refused() -> None:
    environ = "/proc/self/environ"  # readable by its owner, st_size 0
    for variable in ("QW_X_FILE", "QW_DB passfile"):
        exc = refused(fs.ConfigError, fs.check_secret_file, variable, environ)
        assert "reports no size" in exc.reason
    url = f"host=/run/pg dbname=qw passfile={environ}"
    refused(fs.ConfigError, fs.passfile_conninfo, {"QW_DB": url}, "QW_DB")


def test_a_passfile_is_not_a_symlink_nor_in_a_shared_directory(
    tmp_path: Path,
) -> None:
    passfile = write(tmp_path, "pgpass", f"*:*:*:qw:{CANARY}\n".encode())
    (link := tmp_path / "link").symlink_to(passfile)
    for path, reason in ((link, "symlink"), (tmp_path / "absent", "cannot stat")):
        exc = refused(fs.ConfigError, fs.passfile_conninfo,
                      {"QW_DB": f"host=/run/pg passfile={path}"}, "QW_DB")  # fmt: skip
        assert exc.variable == "QW_DB passfile" and reason in exc.reason
    for mode in (0o770, 0o707, 0o1777):
        (shared := tmp_path / f"shared{mode:o}").mkdir()
        shared.chmod(mode)
        inside = write(shared, "pgpass", f"*:*:*:qw:{CANARY}\n".encode())
        env = {"QW_DB": f"host=/run/pg passfile={inside}"}
        exc = refused(fs.ConfigError, fs.passfile_conninfo, env, "QW_DB")
        assert "directory is group or world writable" in exc.reason
    (private_dir := tmp_path / "private").mkdir(mode=0o755)
    ok = write(private_dir, "pgpass", b"*:*:*:qw:x\n")
    assert fs.passfile_conninfo({"QW_DB": f"host=/run/pg passfile={ok}"}, "QW_DB")
    # A *_FILE key is read once, now, so a symlink to a private file is accepted.
    assert fs.check_secret_file("QW_X_FILE", str(link))


def test_an_unreachable_database_is_reported_by_class_only(tmp_path: Path) -> None:
    passfile = write(tmp_path, "pgpass", f"*:*:*:qw:{CANARY}\n".encode())
    url = f"host={tmp_path / 'no-socket'} dbname=qw user=qw passfile={passfile}"
    with pytest.raises(fs.DatabaseUnavailable) as info:
        fs.check_runtime_login(url, "qw_app")
    assert str(info.value) == "database unavailable (OperationalError)"
    assert info.value.__cause__ is None and info.value.__suppress_context__
