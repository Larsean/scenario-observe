# todo-fastapi G0 fixture

This directory is reserved for Scenario integration probes against the nested `todo-fastapi` repository. Keep the fixture read-only: do not change its routes or tests to make tracing easier.

## Python 3.13 environment

Create the root environment with `uv venv --python 3.13 .venv` and install the fixture requirements with `uv pip install --python .venv -r todo-fastapi/requirements.txt`.

The fixture uses SQLite. If the pinned `psycopg2-binary` package cannot install on the selected CPython, create a temporary requirements file that omits only that PostgreSQL driver, then install the remaining pins:

```bash
rg -v '^psycopg2-binary==' todo-fastapi/requirements.txt > /tmp/scenario-cli-g0-requirements.txt
uv pip install --python .venv -r /tmp/scenario-cli-g0-requirements.txt
```

Run the existing test suite with its working directory set to a fresh temporary directory. `tests/conftest.py` uses a relative `test_todo.db` path and removes that file at session teardown.

```bash
/absolute/path/to/Scenario-cli/.venv/bin/python -m pytest \
  /absolute/path/to/Scenario-cli/todo-fastapi/tests -q
```

The G0 command, working directory, runtime version, package exception and baseline result belong in `plans/evidence/monitoring-spikes.md`.
