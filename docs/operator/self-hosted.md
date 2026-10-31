# Self-hosted edition: local compose profile (T060)

Status: **not qualified.** These files have been checked statically only (see
`docs/receipts/T060.md`); no image has been built or started from them yet.
Backups, TLS, resource quotas and upgrade steps are still to come.

Files: `deploy/Dockerfile` (one image, linux/amd64), `deploy/compose.yaml`,
`.dockerignore`. Services:

| Service | Runs | uid:gid | Secrets it mounts |
|---|---|---|---|
| `db` | PostgreSQL 16 (official image) | 999:999 | `db/superuser_password` |
| `db-init` | `qw-db-init`, once, on request (profile `init`) | 10003:10003 | `dbinit/superuser.pgpass`, `dbinit/qw_{migrate,app,worker}.pgpass` |
| `migrate` | `qw-migrate` on every start | 10004:10004 | `migrate/qw_migrate.pgpass` |
| `api` | `qw-api` on 127.0.0.1:8000, and the web app (below) | 10001:10001 | `api/qw_app.pgpass`, `api/secret_key` |
| `worker` | `qw-worker` (standard pool) | 10002:10002 | `worker/qw_worker.pgpass` |

## 1. Pin the images (owner)

- `QW_POSTGRES_IMAGE`: a `postgres:16` reference pinned by digest, for example the
  CI mirror `ghcr.io/<owner>/<repo>/postgres@sha256:...` (decision 15).
- `QW_NODE_IMAGE`: a `node:22.22.0` reference pinned by its linux/amd64 digest
  (decision 109). Mirror it into GHCR as CI mirrors Python, then copy the digest
  from `docker buildx imagetools inspect <mirror> --raw`. Nothing is pinned for it
  in the repository yet; the build fails until it is set.
- The Python base is already pinned in `deploy/Dockerfile` (decision 13).

## 2. Create the secret files (owner, on the host)

No secret lives in the repository or in an image. Keep `QW_SECRETS_DIR` outside
the repository. The values are unencrypted on the host disk until T037/T062 add
encryption at rest (decision 107). Run as root:

```sh
export QW_SECRETS_DIR=/srv/quantweave/secrets
umask 077
install -d -m 0755 "$QW_SECRETS_DIR"
for d in db migrate api worker; do install -d -m 0700 "$QW_SECRETS_DIR/$d"; done
gen() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
cd "$QW_SECRETS_DIR"
pg=$(gen); mig=$(gen); app=$(gen); wrk=$(gen)
printf '%s\n' "$pg" > db/superuser_password
printf '*:*:*:qw_migrate_login:%s\n' "$mig" > migrate/qw_migrate.pgpass
printf '*:*:*:qw_app_login:%s\n' "$app" > api/qw_app.pgpass
printf '*:*:*:qw_worker_login:%s\n' "$wrk" > worker/qw_worker.pgpass
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > api/secret_key
unset pg mig app wrk
chown -R 999:999 db; chown -R 10004:10004 migrate
chown -R 10001:10001 api; chown -R 10002:10002 worker
chmod 0400 db/* migrate/* api/* worker/*
```

The `dbinit/` files are copies that only `db-init` reads, and they exist only while
it runs. This one command creates them from the files above, so they can never be
older than those files (run as root in `$QW_SECRETS_DIR`):

```sh
make_dbinit() {
  install -d -m 0700 dbinit
  printf '*:*:*:postgres:%s\n' "$(cat db/superuser_password)" > dbinit/superuser.pgpass
  cp migrate/qw_migrate.pgpass api/qw_app.pgpass worker/qw_worker.pgpass dbinit/
  chown -R 10003:10003 dbinit; chmod 0400 dbinit/*
}
```

Each file belongs to the uid of the one service that mounts it, mode 0400. Compose
bind-mounts file secrets with the host owner and mode (its secret `uid`/`gid`/`mode`
keys apply only in Swarm mode), so these host settings are what protect them;
`qw-api`, `qw-migrate`, `qw-db-init` and `qw-worker` refuse a file that is not
owned by their uid or that anyone else can read, and a database URL that is not
passfile-only.
Delete `dbinit/` after each `db-init` run (step 4). If a copy were kept and a
service passfile later rotated, a later `db-init` run would set the old password
back from the stale copy.

## 3. Check the configuration

Each command checks its own configuration with `--check-config`, as its own
uid and with its own mounts, so the owner and mode checks are the real ones. It
reads the variables and secret files and never connects to anything: exit 0 and
`config ok: <command>`, or exit 2 and `refused: <variable>: <reason>` (no value
is printed). `--no-deps` keeps the database and `migrate` stopped:

```sh
docker compose -f deploy/compose.yaml run --rm --no-deps migrate qw-migrate --check-config
docker compose -f deploy/compose.yaml run --rm --no-deps api qw-api --check-config
docker compose -f deploy/compose.yaml run --rm --no-deps worker qw-worker --check-config
(cd "$QW_SECRETS_DIR" && make_dbinit)
docker compose -f deploy/compose.yaml --profile init run --rm --no-deps db-init qw-db-init --check-config
rm -rf "$QW_SECRETS_DIR/dbinit"
```

`qw-api --check-config` also checks `QW_WEB_DIR` (an absolute directory whose
`index.html` is a file inside it). These commands have not been run in a
container yet (see the status line).

## 4. First start

```sh
export QW_POSTGRES_IMAGE=... QW_NODE_IMAGE=... QW_SECRETS_DIR=/srv/quantweave/secrets
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml up -d db
(cd "$QW_SECRETS_DIR" && make_dbinit)
docker compose -f deploy/compose.yaml --profile init run --rm db-init
rm -rf "$QW_SECRETS_DIR/dbinit"
docker compose -f deploy/compose.yaml up -d
```

To rotate a login's password: write the new service passfile as in step 2 (owner
and 0400 unchanged), then `make_dbinit`, run `db-init`, delete `dbinit/` and
restart that service.

`db-init` applies migration 0001 and the ledger guard as the superuser and creates
`qw_migrate_login`, `qw_app_login` and `qw_worker_login` (only their SCRAM
verifiers reach the server). Rerunning it is safe: it applies nothing more and sets
the verifiers from the current files (password rotation). `migrate` then applies
the later migrations as `qw_migrate_login` and exits; `api` and `worker` start only
after it succeeded. Before `db-init` has run, `migrate` refuses with a message that
names it. No running service holds the superuser (decision 108).

Networks: `backend` is `internal` (no route out), so `db`, `db-init`, `migrate` and
`worker` can reach only each other; `api` also joins `frontend`, through which its
published port 127.0.0.1:8000 is reached. `db` runs as 999:999 with every
capability dropped. These settings, like the rest of this file, have not yet been
started (see the status line).

The web app: `qw-api` serves the built files in `/opt/qw/web` (`QW_WEB_DIR`) on
its own origin, so open `http://127.0.0.1:8000/`; `QW_ALLOWED_ORIGINS` must hold
the origin the browser uses. API routes are under `/api/v1` and take precedence;
other GET and HEAD paths are files from that directory, with no directory listing
and nothing outside it (symlinks included). Static responses carry the same
`no-store` and correlation id as the API, plus `X-Frame-Options: DENY`,
`Content-Security-Policy: frame-ancestors 'none'` and `nosniff`.

Notes: uvicorn exits with signal 15 after a graceful SIGTERM stop, which compose
reports as exit 143; that is a clean stop. The outbox relay stays off (no publish
target is configured without owner authorisation).
