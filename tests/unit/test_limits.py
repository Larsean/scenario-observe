import json
from pathlib import Path

import pytest

from observe import trace


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_trace_byte_budget_reserves_truncation_and_session_end(tmp_path):
    output = tmp_path / "limited.jsonl"

    @trace(output=output, project_root=PROJECT_ROOT, max_trace_bytes=1400)
    def scenario():
        return "x" * 5000

    assert scenario() == "x" * 5000
    records = _records(output)

    assert len([record for record in records if record["event"] == "trace_truncated"]) == 1
    assert records[-2]["event"] == "trace_truncated"
    assert records[-1]["event"] == "session_end"
    assert len(output.read_bytes()) <= 1400
    call = next(record for record in records if record["event"] == "call")
    assert not any(record.get("call_id") == call["call_id"] and record["event"] == "return" for record in records)
    truncation = records[-2]
    assert truncation["incomplete_call_count"] == 1
    assert truncation["incomplete_call_ids"] == [call["call_id"]]
    assert truncation["incomplete_call_ids_truncated"] is False


def test_trace_budget_too_small_fails_before_body(tmp_path):
    output = tmp_path / "too-small.jsonl"
    body_calls = []

    @trace(output=output, project_root=PROJECT_ROOT, max_trace_bytes=1)
    def scenario():
        body_calls.append(True)

    with pytest.raises(ValueError, match="max_trace_bytes"):
        scenario()

    assert body_calls == []


def test_object_budget_validation_fails_before_body(tmp_path):
    output = tmp_path / "object-limit.jsonl"
    body_calls = []

    @trace(output=output, project_root=PROJECT_ROOT, max_object_bytes=1)
    def scenario():
        body_calls.append(True)

    with pytest.raises(ValueError, match="max_object_bytes"):
        scenario()

    assert body_calls == []


def test_late_writer_error_preserves_application_result(tmp_path, monkeypatch):
    from observe.writer import JsonlWriter

    original_write = JsonlWriter.write

    def fail_return(writer, record):
        if record.get("event") == "return":
            raise OSError("disk full")
        return original_write(writer, record)

    monkeypatch.setattr(JsonlWriter, "write", fail_return)
    output = tmp_path / "writer-error.jsonl"
    result = object()

    @trace(output=output, project_root=PROJECT_ROOT)
    def scenario():
        return result

    assert scenario() is result
    assert any(record["event"] == "trace_error" for record in _records(output))


@pytest.mark.parametrize("limit_name", ["max_depth", "max_calls"])
def test_call_budget_still_returns_application_value(tmp_path, limit_name):
    output = tmp_path / f"{limit_name}.jsonl"

    def child():
        return "nested"

    options = {limit_name: 0 if limit_name == "max_depth" else 1}

    @trace(output=output, project_root=PROJECT_ROOT, **options)
    def scenario():
        return child()

    assert scenario() == "nested"
    assert any(record["event"] == "trace_truncated" for record in _records(output))


def test_truncation_bounds_incomplete_call_id_preview(tmp_path):
    output = tmp_path / "many-open-calls.jsonl"

    def recurse(depth):
        if depth == 0:
            return "done"
        return recurse(depth - 1)

    @trace(output=output, project_root=PROJECT_ROOT, max_calls=9, max_depth=20)
    def scenario():
        return recurse(20)

    assert scenario() == "done"
    records = _records(output)
    truncation = next(record for record in records if record["event"] == "trace_truncated")

    assert truncation["incomplete_call_count"] == 9
    assert len(truncation["incomplete_call_ids"]) == 8
    assert truncation["incomplete_call_ids_truncated"] is True
