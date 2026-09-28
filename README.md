# Scenario Observe

**See what your Python scenario actually does.**

Capture function inputs, returns, and exceptions, then explore the call tree in a searchable, offline HTML report. Scenario Observe helps you understand how data moves through a Python workflow and keep execution evidence alongside your tests.

The package is distributed as `scenario-observe` and imported as `observe`. It targets CPython 3.12 and newer and has **zero runtime dependencies**.

## Why Scenario Observe?

When a scenario crosses several functions, its final result only tells part of the story. Scenario Observe records the calls within a selected execution scope so you can inspect the inputs, intermediate returns, and exceptions that explain that result.

- **Understand a workflow:** follow the call tree from a scenario entry point into the functions it invokes.
- **Investigate failures:** inspect recorded exceptions and the call inputs around a failing path.
- **Keep test evidence:** save local JSONL traces and render them into reports you can revisit after the run.
- **Explore unfamiliar code:** read a function's docstring beside its observed inputs and outputs.

## Highlights

| Capability | What you get |
| --- | --- |
| Synchronous and asynchronous scenarios | Trace a regular function or coroutine with the same `@trace` API. |
| Interactive call tree | Expand calls, select their details, and highlight search matches in the HTML report. |
| Inputs, returns, and exceptions | Inspect recorded values at each observed Python call. |
| Offline reports | Open a self-contained HTML file without a report server or internet connection. |
| Focused tracing | Include or exclude symbols to control which calls appear. |
| Bounded capture | Configure call depth, call count, trace size, and value snapshot limits. |
| Field-name redaction | Mask configured keys in captured data before writing the trace. |

## Quick start

After [installing the package](#install), save this example as `checkout_demo.py` in your project directory:

```python
from observe import render, trace


def subtotal(items):
    """Calculate the total price before discounts."""
    return sum(item["price"] * item["quantity"] for item in items)


def apply_discount(amount, discount_rate):
    """Apply a fractional discount to the subtotal."""
    return round(amount * (1 - discount_rate), 2)


@trace(output=".observe/checkout.jsonl", redact=["email"])
def checkout_scenario():
    items = [{"price": 25, "quantity": 2}, {"price": 10, "quantity": 1}]
    amount = subtotal(items)
    total = apply_discount(amount, discount_rate=0.1)
    return {"total": total, "email": "customer@example.com"}


result = checkout_scenario()
report = render(".observe/checkout.jsonl", ".observe/checkout.html")
print(f"Total: {result['total']}")
print(f"Report: {report}")
```

Run it with:

```bash
python checkout_demo.py
```

Open `.observe/checkout.html` in your browser. Select `subtotal` to inspect its input items and return value of `60`, then select `apply_discount` to see how the total becomes `54.0`. The scenario's recorded return masks `email`; its actual Python return value remains unchanged.

The report contains the observed Python calls, including this flow:

```text
checkout_scenario
├── subtotal → 60
└── apply_discount → 54.0
```

Use the search box to highlight matching calls and the call details to inspect captured values and docstrings. Keep the JSONL file when you want to regenerate the report later.

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

The Todo FastAPI acceptance fixture additionally needs its pinned test dependencies; see [`tests/integration/todo_fastapi/README.md`](tests/integration/todo_fastapi/README.md). GitHub Actions runs the core suite on CPython 3.13 and 3.14. The current local test environment is CPython 3.13.

## Current limits

- Tracing depends on CPython's `sys.monitoring`; PyPy and older CPython releases are not supported.
- New threads do not inherit a causal parent. Calls observed in another thread form separate roots unless a trace scope is opened there explicitly.
- Subprocess tracing and database mutation tracking are not provided.
- The interpreter-wide `sys.monitoring` scope is exclusive: nested or overlapping trace scopes (including unrelated concurrent sessions) fail before their scenario body. Async tasks inside one scope are supported while their parent relationship is provable; a task that outlives the scope cannot write to its closed trace.
- Redaction is based on configured field names, not secret-content detection.
- In HTML, integers outside JavaScript's exact-number range are displayed as decimal strings; the source JSONL keeps them numeric.

Generated `.jsonl`, database, build, and distribution artifacts are excluded by `.gitignore`; review traces and reports before sharing them.
