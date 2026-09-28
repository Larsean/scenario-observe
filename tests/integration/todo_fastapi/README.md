# todo-fastapi G0 fixture

This directory is reserved for Scenario integration probes against the nested `todo-fastapi` repository. Keep the fixture read-only: do not change its routes or tests to make tracing easier.

The fixture is not vendored or tracked by the Scenario-cli repository. On a fresh checkout, obtain the exact revision used by the acceptance tests:

```bash
git clone https://github.com/datwquant/todo-fastapi.git todo-fastapi
git -C todo-fastapi checkout 34bf3890ae1b757d24d8576640f93e947653728f
```

The pinned fixture revision is recorded in `plans/evidence/monitoring-spikes.md`. Do not update it implicitly when reproducing the tests.

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

Run Scenario's six isolated JSONL/HTML acceptance scenarios from the Scenario-cli repository root:

```bash
python -m pytest tests/integration/todo_fastapi -q
```

Each scenario creates its own temporary SQLite database, JSONL trace, and HTML report. The tests start a separate Python process for the fixture so its top-level imports do not affect the root test process.

The G0 command, working directory, runtime version, package exception and baseline result belong in `plans/evidence/monitoring-spikes.md`.

## Warning and artifact policy

The root `pytest.ini` filters only the known Pydantic configuration and `datetime.utcnow()` deprecation warnings emitted by this legacy fixture and SQLAlchemy. Other warnings remain visible. Root `.gitignore` excludes generated JSONL traces, local databases, logs, caches and virtual environments; do not force-add runtime traces or temporary test data.
