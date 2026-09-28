import asyncio
import json
import sys
import weakref
from pathlib import Path

from observe import trace


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _calls(records, suffix):
    return [
        record
        for record in records
        if record["event"] == "call"
        and record["symbol"]["qualname"].endswith(suffix)
    ]


def test_async_resume_keeps_call_id_and_parent(tmp_path):
    output = tmp_path / "async.jsonl"

    async def child(value):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return value + 1

    @trace(output=output, project_root=PROJECT_ROOT)
    async def scenario():
        return await child(4)

    assert asyncio.run(scenario()) == 5
    records = _records(output)
    root = _calls(records, ".scenario")[0]
    child_call = _calls(records, ".child")[0]
    child_returns = [
        record
        for record in records
        if record["event"] == "return" and record["call_id"] == child_call["call_id"]
    ]

    assert child_call["parent_call_id"] == root["call_id"]
    assert len(child_returns) == 1
    assert child_returns[0]["output"] == 5


def test_interleaved_tasks_inherit_parent_without_cross_linking(tmp_path):
    output = tmp_path / "tasks.jsonl"

    async def leaf(label):
        await asyncio.sleep(0)
        return label

    async def branch(label, ready, release):
        ready[label].set()
        await release.wait()
        return await leaf(label)

    @trace(output=output, project_root=PROJECT_ROOT)
    async def scenario():
        ready = {label: asyncio.Event() for label in ("A", "B")}
        release = asyncio.Event()
        tasks = [asyncio.create_task(branch(label, ready, release)) for label in ("A", "B")]
        await asyncio.gather(*(event.wait() for event in ready.values()))
        release.set()
        return await asyncio.gather(*tasks)

    assert asyncio.run(scenario()) == ["A", "B"]
    records = _records(output)
    root = _calls(records, ".scenario")[0]
    branches = _calls(records, ".branch")
    leaves = _calls(records, ".leaf")

    assert len(branches) == len(leaves) == 2
    assert len({branch["task_id"] for branch in branches}) == 2
    assert {branch["parent_call_id"] for branch in branches} == {root["call_id"]}
    branches_by_task = {branch["task_id"]: branch for branch in branches}
    assert all(
        leaf["parent_call_id"] == branches_by_task[leaf["task_id"]]["call_id"]
        for leaf in leaves
    )


def test_task_identifiers_are_monotonic_and_not_object_addresses(tmp_path, monkeypatch):
    from observe import session as session_module
    from observe.session import TraceSession
    from observe.writer import JsonlWriter

    class TaskStub:
        pass

    task = TaskStub()
    current = [task]
    monkeypatch.setattr(session_module.asyncio, "current_task", lambda: current[0])
    writer = JsonlWriter(tmp_path / "task-ids.jsonl")
    observer = TraceSession(lambda: None, writer, project_root=PROJECT_ROOT)

    try:
        first = observer._task_identifier()
        assert observer._task_identifier() == first
        current[0] = TaskStub()
        second = observer._task_identifier()
        assert first == "task-1"
        assert second == "task-2"
        assert weakref.ref(task)() is task
    finally:
        writer.close()


def test_task_created_before_scope_is_not_attributed(tmp_path):
    output = tmp_path / "unrelated.jsonl"

    async def unrelated(started, release):
        started.set()
        await release.wait()
        return "outside"

    async def run_case():
        started = asyncio.Event()
        release = asyncio.Event()
        task = asyncio.create_task(unrelated(started, release))
        await started.wait()

        @trace(output=output, project_root=PROJECT_ROOT)
        async def scenario():
            release.set()
            await task
            return "inside"

        return await scenario()

    assert asyncio.run(run_case()) == "inside"
    assert _calls(_records(output), ".unrelated") == []


def test_cancellation_records_unwind_without_changing_application_result(tmp_path):
    output = tmp_path / "cancel.jsonl"

    async def blocked(started):
        started.set()
        await asyncio.Event().wait()

    @trace(output=output, project_root=PROJECT_ROOT)
    async def scenario():
        started = asyncio.Event()
        task = asyncio.create_task(blocked(started))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "handled"

    assert asyncio.run(scenario()) == "handled"
    records = _records(output)
    blocked_call = _calls(records, ".blocked")[0]
    unwinds = [
        record
        for record in records
        if record["event"] == "unwind" and record["call_id"] == blocked_call["call_id"]
    ]
    assert len(unwinds) == 1
    assert unwinds[0]["exception"]["type"] == "CancelledError"


def test_cancelling_root_scope_records_unwind_and_releases_monitor(tmp_path):
    output = tmp_path / "root-cancel.jsonl"

    @trace(output=output, project_root=PROJECT_ROOT)
    async def scenario(started):
        started.set()
        await asyncio.Event().wait()

    async def run_case():
        started = asyncio.Event()
        task = asyncio.create_task(scenario(started))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"

    assert asyncio.run(run_case()) == "cancelled"
    records = _records(output)
    scenario_call = _calls(records, ".scenario")[0]
    assert any(
        record["event"] == "unwind" and record["call_id"] == scenario_call["call_id"]
        for record in records
    )
    assert records[-1]["event"] == "session_end"
    assert records[-1]["status"] == "error"
    assert sys.monitoring.get_tool(3) is None
    assert sys.monitoring.get_tool(4) is None
