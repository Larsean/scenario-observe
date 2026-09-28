import json
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _write_trace(path, *, schema_version=1):
    record = {
        "schema_version": schema_version,
        "trace_id": "cli-trace",
        "event": "session_start",
        "timestamp_ns": 1,
        "process_id": 1,
        "thread_id": 1,
        "task_id": None,
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def _run_cli(*arguments):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "observe", "render", *map(str, arguments)],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_render_cli_writes_default_output(tmp_path):
    trace_path = tmp_path / "scenario.jsonl"
    _write_trace(trace_path)

    completed = _run_cli(trace_path)

    expected = trace_path.with_suffix(".html")
    assert completed.returncode == 0
    assert str(expected) in completed.stdout
    assert expected.is_file()
    assert completed.stderr == ""


def test_render_cli_supports_custom_output_and_reports_invalid_schema(tmp_path):
    trace_path = tmp_path / "scenario.jsonl"
    output_path = tmp_path / "reports" / "custom.html"
    _write_trace(trace_path)

    completed = _run_cli(trace_path, "--output", output_path)

    assert completed.returncode == 0
    assert output_path.is_file()

    invalid_path = tmp_path / "unsupported.jsonl"
    _write_trace(invalid_path, schema_version=9)
    failed = _run_cli(invalid_path)

    assert failed.returncode == 2
    assert "unsupported schema_version 9" in failed.stderr
    assert str(invalid_path) in failed.stderr
    assert "Traceback" not in failed.stderr
