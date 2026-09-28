# scenario-observe

`scenario-observe` records bounded Python call inputs, returns, and exceptions as local JSONL evidence, then renders that evidence into a self-contained HTML report. It targets CPython 3.12 and newer and has no runtime dependencies.

## Install

Requires CPython 3.12 or newer. The distribution name is `scenario-observe`; import it as `observe`.

Clone the repository and install it into the active environment:

```bash
git clone https://github.com/<owner>/<repo>.git
cd <repo>
python -m pip install .
```

Or install directly from GitHub:

```bash
python -m pip install "git+https://github.com/<owner>/<repo>.git"
```

Then import the public API:

```python
from observe import render, trace
```

For local development, install the package in editable mode with its test extra:

```bash
python -m pip install -e ".[test]"
# or, in a uv-managed environment
uv pip install -e ".[test]"
```

The test extra contains pytest only. Setuptools is a build-time requirement, not a runtime dependency. Package metadata and build configuration live in `pyproject.toml`; a separate `setup.py` is not required.

Build distributable files locally with:

```bash
python -m pip install build
python -m build
```

This creates a wheel and source archive under `dist/`. Publishing a GitHub Release with a `v`-prefixed tag matching the version in `pyproject.toml` automatically builds and attaches both files to that release.

## Record and render

Decorate a synchronous or asynchronous scenario. By default, `trace` writes `.observe/trace.jsonl` and observes Python code under the current project directory. `include` can add symbols outside that directory; `exclude` takes precedence over both explicit includes and the default project scope.

Each recorded call includes its function's raw `docstring` (or `null` when absent). The HTML report shows it verbatim in the selected call's details; Google-style sections are not parsed or reformatted.

```python
from observe import render, trace


@trace(
    output=".observe/checkout.jsonl",
    include=["my_app.*"],
    exclude=["my_app.credentials.*"],
)
def checkout_scenario():
    return {"status": "ready"}


checkout_scenario()
render(".observe/checkout.jsonl", ".observe/checkout.html")
```

The equivalent renderer command is:

```bash
python -m observe render .observe/checkout.jsonl --output .observe/checkout.html
```

The HTML is self-contained and works offline. Keep JSONL traces and HTML reports private: both may contain application inputs or outputs. The recorder bounds snapshots and supports key-based redaction, but it cannot reliably identify every secret. Unknown objects are summarized by type instead of being introspected.

## Tests and compatibility

Run the core suite with:

```bash
python -m pytest tests/unit -q
```

The Todo FastAPI acceptance fixture additionally needs its pinned test dependencies; see [`tests/integration/todo_fastapi/README.md`](tests/integration/todo_fastapi/README.md). GitHub Actions runs the core suite on CPython 3.12, 3.13, and 3.14. The current local test environment is CPython 3.13.

## Current limits

- Tracing depends on CPython's `sys.monitoring`; PyPy and older CPython releases are not supported.
- New threads do not inherit a causal parent. Calls observed in another thread form separate roots unless a trace scope is opened there explicitly.
- Subprocess tracing and database mutation tracking are not provided.
- The interpreter-wide `sys.monitoring` scope is exclusive: nested or overlapping trace scopes (including unrelated concurrent sessions) fail before their scenario body. Async tasks inside one scope are supported while their parent relationship is provable; a task that outlives the scope cannot write to its closed trace.
- Redaction is based on configured field names, not secret-content detection.
- In HTML, integers outside JavaScript's exact-number range are displayed as decimal strings; the source JSONL keeps them numeric.

Generated `.jsonl`, database, build, and distribution artifacts are excluded by `.gitignore`; review traces and reports before sharing them.
