# quantweave

Implementation of the Portfolio Intelligence specification (`docs/spec/`). Agent and
contributor rules are in [CLAUDE.md](CLAUDE.md). Licence: Apache-2.0.

## Python workspace

One [uv](https://docs.astral.sh/uv/) workspace (`pyproject.toml`, `uv.lock`) with a
member per core package under `packages/`. Today that is `packages/domain`
(`qw_domain`: decimal value types, money, UTC instants; standard library only).
Requires CPython 3.13 and uv.

These are the commands run for T009 (from the repository root):

```sh
uv sync --locked                      # create .venv from the lockfile
uv run ruff check .                   # lint
uv run ruff format --check .          # formatting
uv run mypy --strict                  # types (paths come from pyproject.toml)
uv run pytest                         # unit and property tests
uv run python tools/check_no_float.py # no binary floats in money modules
uv run python tools/check_spec_artifacts.py && uv run python tools/check_spec_artifacts.py --self-test
uv run python tools/check_contracts.py && uv run python tools/check_contracts.py --self-test
```

The spec validator rewrites its reports, so run it on a copy:

```sh
tmp=$(mktemp -d) && cp -r docs/spec "$tmp/spec" && uv run python "$tmp/spec/tools/validate_spec.py"
```

To change a dependency, edit `pyproject.toml` and run `uv lock`; never edit `uv.lock` by
hand. CI (`.github/workflows/ci.yml`) runs the same commands.
