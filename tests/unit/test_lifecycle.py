import asyncio
import json
import sys
import threading
from contextvars import copy_context
from types import SimpleNamespace
from pathlib import Path

import pytest

from observe import trace
from observe.monitor import MonitoringScope


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_generator_throw_and_close_preserve_one_call(tmp_path):
    output = tmp_path / "generator.jsonl"

    @trace(output=output, project_root=PROJECT_ROOT)
    def stream():
        try:
            yield "first"
        except ValueError:
            yield "second"

    iterator = stream()
    assert next(iterator) == "first"
    assert iterator.throw(ValueError("injected")) == "second"
    iterator.close()

    records = _records(output)
    calls = [record for record in records if record["event"] == "call"]
    assert len(calls) == 1
    generator_call = calls[0]
    assert not any(
        record["event"] == "return" and record["call_id"] == generator_call["call_id"]
        for record in records
    )
    unwind = next(
        record
        for record in records
        if record["event"] == "unwind" and record["call_id"] == generator_call["call_id"]
    )
    assert unwind["exception"]["type"] == "GeneratorExit"
    assert records[-1]["event"] == "session_end"


def test_unstarted_generator_does_not_replace_existing_trace(tmp_path):
    output = tmp_path / "generator.jsonl"
    original = b"previous evidence\n"
    output.write_bytes(original)

    @trace(output=output, project_root=PROJECT_ROOT)
    def stream():
        yield "unused"

    iterator = stream()
    iterator.close()

    assert output.read_bytes() == original


def test_unstarted_async_generator_does_not_create_output(tmp_path):
    output = tmp_path / "async-generator.jsonl"

    @trace(output=output, project_root=PROJECT_ROOT)
    async def stream():
        yield "unused"

    async def run_case():
        iterator = stream()
        await iterator.aclose()

    asyncio.run(run_case())

    assert not output.exists()


def test_async_generator_suspend_and_close_preserve_one_call(tmp_path):
    output = tmp_path / "async-generator.jsonl"

    @trace(output=output, project_root=PROJECT_ROOT)
    async def stream():
        yield "first"
        await asyncio.sleep(0)
        yield "second"

    async def run_case():
        iterator = stream()
        assert await anext(iterator) == "first"
        await iterator.aclose()

    asyncio.run(run_case())
    records = _records(output)
    calls = [record for record in records if record["event"] == "call"]
    assert len(calls) == 1
    unwind = next(record for record in records if record["event"] == "unwind")
    assert unwind["call_id"] == calls[0]["call_id"]
    assert unwind["exception"]["type"] == "GeneratorExit"
    assert records[-1]["event"] == "session_end"


def test_nested_trace_scope_fails_before_inner_body(tmp_path):
    output = tmp_path / "outer.jsonl"
    inner_output = tmp_path / "inner.jsonl"
    previous_trace = b"preserve previous trace\n"
    inner_output.write_bytes(previous_trace)
    inner_calls = []

    def child():
        return "child"

    @trace(output=inner_output, project_root=PROJECT_ROOT)
    def inner():
        inner_calls.append(True)

    @trace(output=output, project_root=PROJECT_ROOT)
    def outer():
        with pytest.raises(RuntimeError, match="already active"):
            inner()
        return child()

    assert outer() == "child"
    assert inner_calls == []
    assert inner_output.read_bytes() == previous_trace
    records = _records(output)
    outer_call = next(
        record
        for record in records
        if record["event"] == "call" and record["symbol"]["qualname"].endswith("outer")
    )
    child_call = next(
        record
        for record in records
        if record["event"] == "call" and record["symbol"]["qualname"].endswith("child")
    )
    assert child_call["parent_call_id"] == outer_call["call_id"]
    assert records[-1]["event"] == "session_end"


def test_partial_monitor_registration_rolls_back_and_releases_scope_lock():
    class FakeMonitoring:
        class Events:
            NO_EVENTS = 0
            PY_START = 1
            PY_RESUME = 2
            PY_THROW = 4
            PY_YIELD = 8
            PY_RETURN = 16
            PY_UNWIND = 32

        events = Events()

        def __init__(self, fail_registration=False):
            self.tools = {}
            self.callbacks = {}
            self.registered = 0
            self.fail_registration = fail_registration

        def get_tool(self, tool_id):
            return self.tools.get(tool_id)

        def use_tool_id(self, tool_id, name):
            self.tools[tool_id] = name

        def register_callback(self, tool_id, event, callback):
            if callback is not None:
                self.registered += 1
                if self.fail_registration and self.registered == 2:
                    raise RuntimeError("registration failed")
                self.callbacks[(tool_id, event)] = callback
            else:
                self.callbacks.pop((tool_id, event), None)

        def set_events(self, tool_id, events):
            return None

        def free_tool_id(self, tool_id):
            self.tools.pop(tool_id)

    session = SimpleNamespace(
        is_active_context=lambda: True,
        on_start=lambda *args: None,
        on_resume=lambda *args: None,
        on_throw=lambda *args: None,
        on_yield=lambda *args: None,
        on_return=lambda *args: None,
        on_unwind=lambda *args: None,
        monitoring_scope=None,
    )
    failed_monitoring = FakeMonitoring(fail_registration=True)
    failed_scope = MonitoringScope(session)
    failed_scope.monitoring = failed_monitoring

    with pytest.raises(RuntimeError, match="registration failed"):
        failed_scope.__enter__()

    assert failed_monitoring.tools == {}
    assert failed_monitoring.callbacks == {}
    assert failed_scope.lock_acquired is False

    recovered_monitoring = FakeMonitoring()
    recovered_scope = MonitoringScope(session)
    recovered_scope.monitoring = recovered_monitoring
    with recovered_scope:
        assert recovered_monitoring.get_tool(3) == "scenario-observe"
    assert recovered_monitoring.tools == {}


def test_both_monitor_ids_busy_fails_before_scenario_body(tmp_path):
    monitoring = sys.monitoring
    body_calls = []
    acquired = []
    try:
        for tool_id in (3, 4):
            monitoring.use_tool_id(tool_id, f"fixture-{tool_id}")
            acquired.append(tool_id)

        @trace(output=tmp_path / "busy.jsonl", project_root=PROJECT_ROOT)
        def scenario():
            body_calls.append(True)

        with pytest.raises(RuntimeError, match="tool IDs 3 and 4 are occupied"):
            scenario()
    finally:
        for tool_id in reversed(acquired):
            monitoring.free_tool_id(tool_id)

    assert body_calls == []


def test_context_copied_to_other_thread_starts_independent_root(tmp_path):
    output = tmp_path / "thread-boundary.jsonl"

    def worker():
        return threading.get_ident()

    @trace(output=output, project_root=PROJECT_ROOT)
    def scenario():
        context = copy_context()
        results = []
        thread = threading.Thread(target=lambda: results.append(context.run(worker)))
        thread.start()
        thread.join()
        return results[0]

    scenario_thread = threading.get_ident()
    worker_thread = scenario()
    records = _records(output)
    worker_call = next(
        record
        for record in records
        if record["event"] == "call" and record["symbol"]["qualname"].endswith("worker")
    )
    scenario_call = next(
        record
        for record in records
        if record["event"] == "call" and record["symbol"]["qualname"].endswith("scenario")
    )

    assert worker_thread != scenario_thread
    assert worker_call["thread_id"] != scenario_call["thread_id"]
    assert worker_call["parent_call_id"] is None


def test_child_task_outliving_scope_cannot_write_after_session_end(tmp_path):
    output = tmp_path / "late-child.jsonl"
    holder = {}

    async def child(release):
        await release.wait()
        return "finished"

    @trace(output=output, project_root=PROJECT_ROOT)
    async def scenario():
        release = asyncio.Event()
        holder["release"] = release
        holder["task"] = asyncio.create_task(child(release))
        await asyncio.sleep(0)
        return "scope ended"

    async def run_case():
        result = await scenario()
        before = output.read_bytes()
        holder["release"].set()
        child_result = await holder["task"]
        return result, before, child_result

    result, before, child_result = asyncio.run(run_case())
    assert result == "scope ended"
    assert child_result == "finished"
    assert output.read_bytes() == before
    assert sys.monitoring.get_tool(3) is None
    assert sys.monitoring.get_tool(4) is None
