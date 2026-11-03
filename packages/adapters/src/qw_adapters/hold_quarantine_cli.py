"""`qw-hold-quarantine`: owner decision 115's audited repair of one corrupt
legal-hold row (T058; 0061; `legal_hold_store.quarantine`). Run by hand, like
`qw-db-init`, after the owner approved it; never by a running service.

QW_QUARANTINE_DATABASE_URL is a passfile-only libpq string for a PostgreSQL
superuser (`file_secrets.passfile_conninfo`); a service login is refused, here
and again by 0061's guard. Arguments: `--tenant` (uuid), `--seq` (the first event
that fails replay), `--approved-by` (the owner's principal id), `--approval-ref`
(the id of the owner's recorded approval), `--reason` (a code). The blocked
scope is stated by the approver, because the corrupt row's own scope cannot be
trusted: each `--class` (default: every class of the tenant) and each
`--record-id` (default: every record of those classes). The run appends a
replacement hold with exactly that scope, which blocks until a valid two-person
release of it is recorded through the hold route; the rest of the tenant resumes.

Exit 0 and one report line (the replacement hold id and its scope; no record
content), 2 for configuration or arguments, 1 for a refusal or a database
failure. `--check-config` checks the configuration only and never connects.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Mapping, Sequence

import psycopg
from qw_domain import retention as rt

from qw_adapters import legal_hold_store as hs
from qw_adapters import tenancy
from qw_adapters.file_secrets import (
    LIBPQ_INLINE,
    ConfigError,
    passfile_conninfo,
    refuse_inline,
)

URL_VARIABLE = "QW_QUARANTINE_DATABASE_URL"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qw-hold-quarantine", description=__doc__)
    p.add_argument("--check-config", action="store_true")
    p.add_argument("--tenant", type=uuid.UUID)
    p.add_argument("--seq", type=int)
    p.add_argument("--approved-by")
    p.add_argument("--approval-ref")
    p.add_argument("--reason")
    p.add_argument("--class", dest="classes", action="append", default=None,
                   type=rt.RetentionClass,
                   choices=list(rt.RetentionClass))  # fmt: skip
    p.add_argument("--record-id", dest="record_ids", action="append", default=[])
    return p


def main(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse printed the reason
        return 0 if exc.code == 0 else 2
    env = os.environ if environ is None else environ
    try:
        refuse_inline(env, LIBPQ_INLINE)
        conninfo = passfile_conninfo(env, URL_VARIABLE)
    except ConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.check_config:
        print("config ok: qw-hold-quarantine")
        return 0
    need = (args.tenant, args.seq, args.approved_by, args.approval_ref, args.reason)
    if any(v is None for v in need):
        print("refused: --tenant, --seq, --approved-by, --approval-ref and --reason "
              "are required", file=sys.stderr)  # fmt: skip
        return 2
    try:
        with psycopg.connect(conninfo, autocommit=True) as conn:
            su = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = "
                              "session_user").fetchone()  # fmt: skip
            if su is None or su[0] is not True:
                print("refused: qw-hold-quarantine needs a PostgreSQL superuser "
                      "login", file=sys.stderr)  # fmt: skip
                return 1
            with tenancy.tenant_transaction(conn, args.tenant) as tx:
                h = hs.quarantine(tx, args.seq, args.approved_by, args.approval_ref,
                                  args.reason, args.classes,
                                  args.record_ids)  # fmt: skip
    except hs.HoldStoreError as exc:  # a code and a short reason, no record
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:  # no server text: it could echo a statement
        print(f"failed: database error ({exc.sqlstate or type(exc).__name__})",
              file=sys.stderr)  # fmt: skip
        return 1
    print(f"quarantined event {args.seq} of tenant {args.tenant}: replacement hold "
          f"{h.hold_id} blocks {', '.join(sorted(c.value for c in h.classes))}; "
          f"{len(h.record_ids) or 'all'} record(s)")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
