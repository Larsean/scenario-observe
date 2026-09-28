import json
import types
from pathlib import Path
from uuid import uuid4

import pytest

from observe import trace


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _calls(records, suffix):
    return [
        record
        for record in records
        if record["event"] == "call" and record["symbol"]["qualname"].endswith(suffix)
    ]


def test_sync_trace_writes_schema_arguments_and_return(tmp_path):
    output = tmp_path / "nested" / "trace.jsonl"

    @trace(output=output, project_root=PROJECT_ROOT, include=[f"{__name__}.*"])
    def scenario(value, /, label, *, enabled, **extra):
        return {"value": value, "label": label, "enabled": enabled, "extra": extra}

    result = scenario(7, "繁體中文", enabled=True, marker="keep")
    records = _read_records(output)
    start, call, returned, end = records

    assert result == {"value": 7, "label": "繁體中文", "enabled": True, "extra": {"marker": "keep"}}
    assert [record["event"] for record in records] == ["session_start", "call", "return", "session_end"]
    assert call["schema_version"] == 1
    assert call["trace_id"] == start["trace_id"] == returned["trace_id"] == end["trace_id"]
    assert call["input"] == {
        "value": 7,
        "label": "繁體中文",
        "enabled": True,
        "extra": {"marker": "keep"},
    }
    assert call["symbol"]["module"] == __name__
    assert call["symbol"]["qualname"].endswith(".scenario")
    assert call["source"]["file"] == "tests/unit/test_sync_trace.py"
    assert call["parent_call_id"] is None
    assert returned["call_id"] == call["call_id"]
    assert returned["output"] == result
    assert returned["duration_ns"] > 0
    for record in records:
        assert isinstance(record["timestamp_ns"], int)
        assert isinstance(record["process_id"], int)
        assert isinstance(record["thread_id"], int)
        assert record["task_id"] is None


def test_nested_and_recursive_calls_keep_call_tree(tmp_path):
    output = tmp_path / "trace.jsonl"

    def leaf(value):
        return value + 1

    def visit(depth):
        if depth == 0:
            return leaf(depth)
        return visit(depth - 1)

    @trace(output=output, project_root=PROJECT_ROOT, include=[f"{__name__}.*"])
    def scenario():
        return visit(2)

    assert scenario() == 1
    records = _read_records(output)
    scenario_call = _calls(records, ".scenario")[0]
    visits = _calls(records, ".visit")
    leaf_call = _calls(records, ".leaf")[0]

    assert len(visits) == 3
    assert len({record["call_id"] for record in visits}) == 3
    assert visits[0]["parent_call_id"] == scenario_call["call_id"]
    assert visits[1]["parent_call_id"] == visits[0]["call_id"]
    assert visits[2]["parent_call_id"] == visits[1]["call_id"]
    assert leaf_call["parent_call_id"] == visits[2]["call_id"]


def test_excluded_parent_keeps_included_child_and_hop_count(tmp_path):
    output = tmp_path / "trace.jsonl"

    def child():
        return "observed"

    def excluded_parent():
        return child()

    @trace(
        output=output,
        project_root=PROJECT_ROOT,
        exclude=[f"{__name__}.*.excluded_parent"],
    )
    def scenario():
        return excluded_parent()

    assert scenario() == "observed"
    records = _read_records(output)
    scenario_call = _calls(records, ".scenario")[0]
    child_call = _calls(records, ".child")[0]

    assert _calls(records, ".excluded_parent") == []
    assert child_call["parent_call_id"] == scenario_call["call_id"]
    assert child_call["filtered_hops"] == 1


def test_default_project_scope_excludes_external_until_explicitly_included(tmp_path):
    external_module = types.ModuleType(f"external_fixture_{uuid4().hex}")
    source = "def external_call(value):\n    return value + 1\n"
    code = compile(source, str(tmp_path / "external_fixture.py"), "exec")
    exec(code, external_module.__dict__)

    default_output = tmp_path / "default.jsonl"

    @trace(output=default_output, project_root=PROJECT_ROOT)
    def default_scenario():
        return external_module.external_call(1)

    assert default_scenario() == 2
    assert _calls(_read_records(default_output), ".external_call") == []

    included_output = tmp_path / "included.jsonl"
    external_pattern = f"{external_module.__name__}.*"

    @trace(output=included_output, project_root=PROJECT_ROOT, include=[external_pattern])
    def included_scenario():
        return external_module.external_call(1)

    assert included_scenario() == 2
    included_call = _calls(_read_records(included_output), "external_call")[0]
    assert included_call["symbol"]["module"] == external_module.__name__
    assert included_call["parent_call_id"] is not None


def test_unwind_preserves_original_application_exception(tmp_path):
    output = tmp_path / "trace.jsonl"
    application_error = ValueError("original application error")

    @trace(output=output, project_root=PROJECT_ROOT)
    def scenario():
        raise application_error

    with pytest.raises(ValueError, match="original application error") as caught:
        scenario()

    assert caught.value is application_error
    records = _read_records(output)
    unwind = next(record for record in records if record["event"] == "unwind")
    call = next(record for record in records if record["event"] == "call")
    assert unwind["call_id"] == call["call_id"]
    assert unwind["exception"] == {
        "type": "ValueError",
        "message": "original application error",
    }
    assert records[-1]["event"] == "session_end"
    assert records[-1]["status"] == "error"


def test_unwind_does_not_call_custom_exception_stringifier(tmp_path):
    output = tmp_path / "unsafe-exception.jsonl"
    side_effects = []

    class DangerousError(Exception):
        def __str__(self):
            side_effects.append("stringified")
            raise AssertionError("exception stringifier must not run")

    application_error = DangerousError("original")

    @trace(output=output, project_root=PROJECT_ROOT)
    def scenario():
        raise application_error

    with pytest.raises(DangerousError) as caught:
        scenario()

    assert caught.value is application_error
    assert side_effects == []
    unwind = next(record for record in _read_records(output) if record["event"] == "unwind")
    assert unwind["exception"]["message"] == "original"


def test_output_preflight_fails_before_scenario_body(tmp_path):
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied", encoding="utf-8")
    output = parent_file / "trace.jsonl"
    body_calls = []

    @trace(output=output, project_root=PROJECT_ROOT)
    def scenario():
        body_calls.append(True)

    with pytest.raises(OSError):
        scenario()

    assert body_calls == []


def test_explicit_output_replaces_prior_trace(tmp_path):
    output = tmp_path / "trace.jsonl"
    output.write_text("stale trace\n", encoding="utf-8")

    @trace(output=output, project_root=PROJECT_ROOT)
    def scenario():
        return "fresh"

    assert scenario() == "fresh"
    assert scenario() == "fresh"
    records = _read_records(output)

    assert "stale trace" not in output.read_text(encoding="utf-8")
    assert [record["event"] for record in records].count("session_start") == 1
    assert len(_calls(records, ".scenario")) == 1


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "expected_reason"),
    [("max_depth", 0, "max_depth"), ("max_calls", 1, "max_calls")],
)
def test_call_budget_truncates_once_without_extra_detail(
    tmp_path, limit_name, limit_value, expected_reason
):
    output = tmp_path / f"{expected_reason}.jsonl"

    def child():
        return "child result"

    options = {limit_name: limit_value}

    @trace(output=output, project_root=PROJECT_ROOT, **options)
    def scenario():
        return child()

    assert scenario() == "child result"
    records = _read_records(output)

    assert len(_calls(records, ".scenario")) == 1
    assert _calls(records, ".child") == []
    truncations = [record for record in records if record["event"] == "trace_truncated"]
    assert len(truncations) == 1
    assert truncations[0]["reason"] == expected_reason
    assert records[-1]["event"] == "session_end"


def test_late_writer_failure_does_not_replace_application_exception(tmp_path, monkeypatch):
    from observe.writer import JsonlWriter

    output = tmp_path / "trace.jsonl"
    application_error = ValueError("application failure")
    original_write = JsonlWriter.write

    def fail_call_record(writer, record):
        if record.get("event") == "call":
            raise OSError("disk full")
        return original_write(writer, record)

    monkeypatch.setattr(JsonlWriter, "write", fail_call_record)

    @trace(output=output, project_root=PROJECT_ROOT)
    def scenario():
        raise application_error

    with pytest.raises(ValueError, match="application failure") as caught:
        scenario()

    assert caught.value is application_error
    records = _read_records(output)
    assert any(record["event"] == "trace_error" for record in records)
    assert records[-1]["event"] == "session_end"
