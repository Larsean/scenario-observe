---
name: scenario-observe
description: Capture and inspect bounded call traces for a Python scenario with scenario-observe. Use when debugging a multi-function execution flow or collecting local acceptance evidence; do not use it as a profiler or production telemetry system.
---

# scenario-observe

Use `scenario-observe` when a concrete scenario needs a local record of Python call inputs, return values, or exceptions, and an HTML view of that record would help inspect the flow. It supports synchronous and asynchronous scenario functions on CPython 3.12 or newer.

## When to use it

- Use it to inspect how a request, task, or acceptance scenario moves through application functions.
- Use it when debugging unexpected values or exceptions across several calls, or when a reviewer needs local execution evidence.
- Do not use it for timing or performance profiling, subprocess tracing, database mutation tracking, or production telemetry.
- Do not rely on it to connect work in new threads to the scenario's call tree. Avoid nested or overlapping trace scopes; `sys.monitoring` is interpreter-wide and exclusive.

## Record and inspect a scenario

Import the public API from `observe`. Decorate the scenario entry point, write traces under the project's ignored `.observe/` directory, and narrow capture with module patterns when useful. Exclusions take precedence over includes and the default project scope.

```python
from observe import render, trace


@trace(
    output=".observe/checkout.jsonl",
    include=["my_app.*"],
    exclude=["my_app.credentials.*"],
    redact=("password", "token"),
)
def checkout_scenario():
    return perform_checkout()


checkout_scenario()
render(".observe/checkout.jsonl", ".observe/checkout.html")
```

The same scenario decorator supports `async def` entry points. To render an existing trace, use `render(input_path, output_path)` or run `python -m observe render <trace.jsonl> --output <report.html>`.

## Protect captured data

Trace inputs, outputs, and HTML reports can contain application data. Prefer synthetic or otherwise safe test data, exclude sensitive modules, and inspect artifacts before sharing them. `redact` matches configured field names; it does not detect secrets by content, so it is not a guarantee that every secret is removed. Snapshots and trace size are bounded, but the resulting record can still be sensitive.

## Environment and behavior

- The package requires CPython 3.12 or newer because tracing uses `sys.monitoring`; PyPy is unsupported.
- If `observe` is not installed in the active environment, install this checkout with `python -m pip install -e .` from the repository root.
- Async tasks can be linked while their parent relationship is provable. A task that outlives the trace scope cannot write to its closed trace.
- Calls in another thread form separate roots unless a trace scope is opened there explicitly.
