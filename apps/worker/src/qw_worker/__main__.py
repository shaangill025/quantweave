"""`qw-worker` / `python -m qw_worker`: run the worker loop in the foreground (no
daemonisation; a process supervisor owns restarts). The runtime database URL comes
from QW_DATABASE_URL, a libpq string whose password is in a checked passfile
(`qw_adapters.file_secrets.passfile_conninfo`, as qw-api and qw-migrate; T060,
owner decision 107: an inline password or PGPASSWORD is refused, exit 2), and must
be exactly a qw_worker member (owner decision 17:
`runtime_connection(..., role="qw_worker")` refuses a qw_app, qw_assessor or mixed
login at startup); it refuses to start at all when QW_ASSESSOR_DATABASE_URL is set
(owner decision 40: only `qw-assessor` holds the assessor login); settings from
`settings_from_env`. Handlers: `optimizer_cycle.HANDLERS` (kind `optimizer.cycle`,
claimed only by an experiment-pool worker, QW_WORKER_POOL=experiment) and
`budget_sweep.HANDLERS` (kind `budget.expire_due`, standard pool). A
standard-pool worker also runs the budget-sweep timer (owner decision 100) every
`budget_sweep_every` (QW_WORKER_BUDGET_SWEEP_EVERY_MINUTES, default 5); an
experiment-pool worker runs no timer. The record key comes from QW_RECORD_KEY_FILE
(`config.record_key_from_env`, decision 107; inline QW_RECORD_KEY or a short key
refuses the start, exit 2, also under --check-config). A standard-pool worker with
the key handles `retention.delete` (`deletion_job`) once a store `Target` exists;
none does yet, so no deletion handler is registered and those jobs stay queued
rather than failing. No publish
target is passed, so the outbox relay is off and events stay pending: a real
transport needs explicit owner authorisation.

`qw-worker --check-config` checks the same configuration as the user it runs as
and never connects: exit 0 and `config ok: qw-worker`, or exit 2 and the reason.
"""

import logging
import os
import sys
from collections.abc import Sequence

from qw_adapters import tenancy
from qw_adapters.file_secrets import ConfigError, passfile_conninfo
from qw_domain.jobs import Pool

from qw_worker import budget_sweep, deletion_job, optimizer_cycle
from qw_worker.config import record_key_from_env, settings_from_env
from qw_worker.loop import Worker, install_signal_handlers


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check = "--check-config" in args
    if check and args != ["--check-config"]:
        print("usage: qw-worker [--check-config]", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if os.environ.get("QW_ASSESSOR_DATABASE_URL"):  # owner decision 40
        print("refused: QW_ASSESSOR_DATABASE_URL is set; the assessor login is for "
              "qw-assessor only", file=sys.stderr)  # fmt: skip
        return 1
    try:
        conninfo = passfile_conninfo(os.environ, "QW_DATABASE_URL")
        record_key = record_key_from_env()
    except ConfigError as exc:  # names the variable and reason, never a value
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    try:
        settings = settings_from_env()
    except ValueError as exc:
        print(f"invalid worker settings: {exc}", file=sys.stderr)
        return 2
    if check:
        print("config ok: qw-worker")
        return 0
    try:
        with tenancy.runtime_connection(conninfo, role="qw_worker") as conn:
            handlers = {**optimizer_cycle.HANDLERS, **budget_sweep.HANDLERS}
            targets = deletion_job.TARGETS  # empty until a store handler exists
            if record_key is not None and settings.pool is Pool.STANDARD and targets:
                handlers |= deletion_job.handlers(
                    record_key, settings.retention_policy, targets)  # fmt: skip
            timers = ((budget_sweep.timer(settings.budget_sweep_every),)
                      if settings.pool is Pool.STANDARD else ())  # fmt: skip
            worker = Worker(conn, settings, handlers=handlers, timers=timers,
                            publish=None)  # relay off (above)  # fmt: skip
            install_signal_handlers(worker)
            worker.run()
    except tenancy.TenancyError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
