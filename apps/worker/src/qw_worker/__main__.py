"""`qw-worker` / `python -m qw_worker`: run the worker loop in the foreground (no
daemonisation; a process supervisor owns restarts). The runtime database URL comes
from QW_DATABASE_URL and must be exactly a qw_worker member (owner decision 17:
`runtime_connection(..., role="qw_worker")` refuses a qw_app, qw_assessor or mixed
login at startup); it refuses to start at all when QW_ASSESSOR_DATABASE_URL is set
(owner decision 40: only `qw-assessor` holds the assessor login); settings from
`settings_from_env`. Handlers: `optimizer_cycle.HANDLERS` (kind `optimizer.cycle`,
claimed only by an experiment-pool worker, QW_WORKER_POOL=experiment). No publish
target is passed,
so the outbox relay is off and events stay pending: a real transport needs explicit
owner authorisation.
"""

import logging
import os
import sys

from qw_adapters import tenancy

from qw_worker import optimizer_cycle
from qw_worker.config import settings_from_env
from qw_worker.loop import Worker, install_signal_handlers


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if os.environ.get("QW_ASSESSOR_DATABASE_URL"):  # owner decision 40
        print("refused: QW_ASSESSOR_DATABASE_URL is set; the assessor login is for "
              "qw-assessor only", file=sys.stderr)  # fmt: skip
        return 1
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
        with tenancy.runtime_connection(conninfo, role="qw_worker") as conn:
            worker = Worker(conn, settings, handlers=optimizer_cycle.HANDLERS,
                            publish=None)  # relay off (above)  # fmt: skip
            install_signal_handlers(worker)
            worker.run()
    except tenancy.TenancyError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
