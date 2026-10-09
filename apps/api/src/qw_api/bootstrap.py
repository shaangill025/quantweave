"""`qw-bootstrap`: one-time, operator-run setup of a self-hosted installation's first
tenant, owner and local credential (T010). It is not an HTTP route. The runtime
database URL comes from QW_DATABASE_URL in the operator's environment and the
password from stdin, so neither appears in argv; a second run is refused by the
database (installation_bootstrap marker).
"""

import argparse
import os
import sys

from qw_adapters import tenancy

from qw_api.routes import Login
from qw_api.security import LocalPasswordAuthenticator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qw-bootstrap", description=__doc__)
    parser.add_argument("--login", required=True)
    parser.add_argument("--display-name", required=True)
    args = parser.parse_args(argv)
    conninfo = os.environ.get("QW_DATABASE_URL")
    password = sys.stdin.readline().rstrip("\n")
    try:
        Login.model_validate({"login": args.login, "password": password})
    except ValueError:
        conninfo = None
    if not conninfo or len(password) < 12:
        print("need QW_DATABASE_URL, a valid --login and a password of 12+ chars "
              "on stdin", file=sys.stderr)  # fmt: skip
        return 2
    hasher = LocalPasswordAuthenticator().hasher
    try:
        with tenancy.runtime_connection(conninfo) as conn:
            tenancy.bootstrap_installation(
                conn, args.display_name, args.login, hasher.hash(password)
            )
    except tenancy.TenancyError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print("bootstrapped: first owner created")
    return 0
