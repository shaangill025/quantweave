"""`qw-release-admin` (run as `python -m qw_api.release_admin`): the installation
operator's release setup (owner decision 45, 2026-10-22; decision 5 of 2026-10-20:
the release reads go live only once real storage backs them). Not an HTTP route.
It records this installation's deployment and its trusted maintainer keys, and
revokes a key (append-only, at the database clock). The database URL comes from
QW_RELEASE_DATABASE_URL in the operator's environment and must be exactly a
qw_release login (as #140 declared the worker's and bootstrap's roles); any other
login is refused before anything is written. Public key material only:
`--public-key-file` takes the operator's public key (a private key is refused),
for an algorithm T009 has reviewed (`PRODUCTION_ALGORITHMS`, empty today, so
every non-test key is refused); `--test-only` records a test key with no key
material at all. Nothing here signs, deploys or contacts a network.
"""

import argparse
import os
import sys
import uuid
from pathlib import Path

from qw_adapters import release_store, tenancy
from qw_domain import release as rl
from qw_domain.instants import parse_instant

TEST_NOTE = (
    " (test-only: no key material is stored; it verifies only through an "
    "in-process test hook)"
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qw-release-admin", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("record-deployment", help="record this deployment once")
    d.add_argument("--deployment-id", required=True)
    d.add_argument("--edition", required=True, choices=[e.value for e in rl.Edition])
    d.add_argument("--operator", action="append", required=True)
    d.add_argument("--tenant", action="append", default=[])
    d.add_argument("--initial-schema", type=int, required=True)
    d.add_argument("--allow-test-keys", action="store_true")
    d.add_argument("--solo-maintainer", action="store_true")
    k = sub.add_parser("add-key", help="trust a maintainer's public key")
    k.add_argument("--key-id", required=True)
    k.add_argument("--principal", required=True)
    k.add_argument("--algorithm", required=True)
    k.add_argument("--valid-from", required=True)
    k.add_argument("--valid-until")
    material = k.add_mutually_exclusive_group(required=True)
    material.add_argument("--public-key-file", type=Path)
    material.add_argument("--test-only", action="store_true")
    r = sub.add_parser("revoke-key", help="revoke a trusted key now")
    r.add_argument("--key-id", required=True)
    r.add_argument("--reason", required=True)
    return p


def _deployment(a: argparse.Namespace) -> rl.Deployment:
    ed, ops = rl.Edition(a.edition), frozenset(a.operator)
    tenants = frozenset(str(uuid.UUID(t)) for t in a.tenant)
    solo = rl.SoloMaintainer(ed, ops) if a.solo_maintainer else None
    return rl.Deployment(a.deployment_id, ed, ops, tenants, a.initial_schema,
                         a.allow_test_keys, solo)  # fmt: skip


def _key(a: argparse.Namespace) -> release_store.KeyRecord:
    until = None if a.valid_until is None else parse_instant(a.valid_until)
    material = None if a.test_only else a.public_key_file.read_text().strip()
    return release_store.KeyRecord(a.key_id, a.principal, a.algorithm,
                                   parse_instant(a.valid_from), until, material,
                                   a.test_only)  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    conninfo = os.environ.get("QW_RELEASE_DATABASE_URL")
    if not conninfo:
        print("need QW_RELEASE_DATABASE_URL (a qw_release login)", file=sys.stderr)
        return 2
    try:
        built = _deployment(a) if a.command == "record-deployment" else (
            _key(a) if a.command == "add-key" else None)  # fmt: skip
    except (ValueError, OSError) as exc:  # bad ids, times, tenants or file
        print(f"invalid: {exc}", file=sys.stderr)
        return 2
    try:
        with tenancy.runtime_connection(conninfo, role="qw_release") as conn:
            if isinstance(built, rl.Deployment):
                new = release_store.record_deployment(conn, built)
                print(f"{'recorded' if new else 'unchanged'}: deployment "
                      f"{built.deployment_id}")  # fmt: skip
            elif isinstance(built, release_store.KeyRecord):
                new = release_store.record_key(conn, built)
                note = TEST_NOTE if built.test_only else ""
                print(f"{'recorded' if new else 'unchanged'}: key {built.key_id}{note}")
            else:
                at = release_store.revoke_key(conn, a.key_id, a.reason)
                print(f"revoked: key {a.key_id} at {at.isoformat()}")
    except (tenancy.TenancyError, release_store.ReleaseStoreError,
            rl.ReleaseError) as exc:  # fmt: skip
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # `python -m qw_api.release_admin`
    sys.exit(main())
