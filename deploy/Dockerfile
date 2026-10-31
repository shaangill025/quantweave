# syntax=docker/dockerfile:1
# Self-hosted image (T060): one image for qw-api, qw-worker, qw-db-init and
# qw-migrate; compose picks the command and a separate uid per service (owner
# decisions 106-109). Build context: the repository root.
#   docker build -f deploy/Dockerfile --build-arg NODE_IMAGE=<node@sha256:...> .
# linux/amd64 only: the Python base and the uv wheel below are pinned for it.
# No secret is copied or baked in; they are mounted as files at run time.

# python:3.13-slim, the linux/amd64 manifest digest pinned in
# tools/candidate_rootfs.json (owner decision 13; the same bytes on Docker Hub and
# in the GHCR mirror).
ARG PYTHON_IMAGE=docker.io/library/python:3.13-slim@sha256:8fb4cfa1a2616d7b8e0c2175cc6ad68f5729c34ea8488c0b360d2934b7be9024
# node:22.22.0 (web.yml's version), digest-pinned by the owner (decision 109). No
# default: the build fails until it is given.
ARG NODE_IMAGE

FROM ${NODE_IMAGE} AS web
WORKDIR /src
ENV CI=1
# pnpm 10.28.0 (package.json's packageManager), its npm tarball checked against
# the registry's sha512 integrity before install; no lifecycle scripts run.
RUN npm pack --silent pnpm@10.28.0 \
 && echo '05df71d1421f21399e053fde567cea34d446fa02c76571441bfc1c7956e98e363088982d940465fd34480d4d90a0668bc12362f8aa88000a64e83d0b0e47be48  pnpm-10.28.0.tgz' | sha512sum -c - \
 && npm install -g --ignore-scripts ./pnpm-10.28.0.tgz \
 && rm pnpm-10.28.0.tgz
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY apps/web/ apps/web/
RUN pnpm build

FROM ${PYTHON_IMAGE} AS build
ENV UV_PYTHON=/usr/local/bin/python3 UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/qw/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# uv 0.11.32 (CI's version) and the uv_build backend the workspace members name
# (uv_build>=0.11,<0.13), each as its manylinux x86_64 wheel by SHA-256 (PyPI).
RUN printf '%s\n' 'uv==0.11.32 --hash=sha256:3da76cd4e2697de30928b8a8524bd39183ac1e08cb7e72833807c022b7cba6c4' > /tmp/uv.txt \
 && printf '%s\n' 'uv-build==0.11.32 --hash=sha256:a9c64f4139a05fa45a38f2a646e130b9b79fdd1110de5c54218b378100ae4d9d' > /tmp/uv-build.txt \
 && pip install --no-cache-dir --no-deps --require-hashes -r /tmp/uv.txt
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY apps/api/ apps/api/
COPY apps/worker/ apps/worker/
# The locked runtime set (no dev group); workspace packages not editable. The
# members are built with the pre-installed, hash-checked uv_build (no build
# isolation, so no unlocked download); --inexact keeps it during the sync.
RUN uv venv /opt/qw/venv \
 && uv pip install --python /opt/qw/venv/bin/python --no-deps --require-hashes \
      -r /tmp/uv-build.txt \
 && uv sync --locked --no-dev --no-editable --all-packages --no-build-isolation \
      --inexact

FROM ${PYTHON_IMAGE} AS runtime
# libpq for psycopg's pure-Python build. One uid per service (decision 107);
# none can log in. pip is removed: nothing installs at run time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/* \
 && for u in qw-api:10001 qw-worker:10002 qw-dbinit:10003 qw-migrate:10004; do \
      useradd --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin \
        --user-group --uid "${u#*:}" "${u%%:*}"; \
    done \
 && rm -rf /usr/local/bin/pip* /usr/local/lib/python3.13/site-packages/pip \
      /usr/local/lib/python3.13/site-packages/pip-*.dist-info
COPY --from=build /opt/qw/venv /opt/qw/venv
# The built web app. Not served yet: the API's static file serving (decision 109)
# is a later T060 increment.
COPY --from=web /src/apps/web/dist /opt/qw/web
ENV PATH=/opt/qw/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
CMD ["qw-api"]
