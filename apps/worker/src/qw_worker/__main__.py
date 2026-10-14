"""`qw-worker` / `python -m qw_worker`: run the worker loop in the foreground (no
daemonisation; a process supervisor owns restarts). The runtime database URL comes
from QW_DATABASE_URL (a qw_worker member, checked by `runtime_connection`); settings
from `settings_from_env`. No publish target is passed, so the outbox relay is off
and events stay pending: a real transport needs explicit owner authorisation.
"""

import logging
import os
import sys

from qw_adapters import tenancy

from qw_worker.config import settings_from_env
from qw_worker.loop import Worker, install_signal_handlers


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    conninfo = os.environ.get("QW_DATABASE_URL")
    if not conninfo:
        print("need QW_DATABASE_URL", file=sys.stderr)
        return 2
    try:
        settings = settings_from_env()
    except ValueError as exc:
        print(f"invalid worker settings: {exc}", file=sys.stderr)
        return 2
    try:
        with tenancy.runtime_connection(conninfo) as conn:
            worker = Worker(conn, settings, publish=None)  # relay off (above)
            install_signal_handlers(worker)
            worker.run()
    except tenancy.TenancyError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
