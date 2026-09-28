from email import message_from_bytes
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _package_tool():
    if importlib.util.find_spec("pip") is not None:
        return "pip"
    uv = shutil.which("uv")
    if uv is not None:
        return uv
    pytest.fail("wheel acceptance requires pip or uv")


def _run(command, *, cwd, environment):
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _build_wheel(tool, wheel_directory, *, cwd, environment):
    if tool == "pip":
        command = [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(wheel_directory),
            str(PROJECT_ROOT),
        ]
    else:
        command = [tool, "build", "--wheel", "--out-dir", str(wheel_directory), str(PROJECT_ROOT)]
    _run(command, cwd=cwd, environment=environment)
    wheels = list(wheel_directory.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _install_wheel(tool, wheel, target, *, cwd, environment):
    if tool == "pip":
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(target),
            str(wheel),
        ]
    else:
        command = [
            tool,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--target",
            str(target),
            str(wheel),
        ]
    _run(command, cwd=cwd, environment=environment)


def test_wheel_includes_template_and_renders_without_runtime_dependencies(tmp_path):
    tool = _package_tool()
    wheel_directory = tmp_path / "wheels"
    wheel_directory.mkdir()
    install_target = tmp_path / "installed"
    environment = os.environ.copy()
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment["PIP_CACHE_DIR"] = str(tmp_path / "pip-cache")
    environment["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    wheel = _build_wheel(
        tool,
        wheel_directory,
        cwd=tmp_path,
        environment=environment,
    )

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "observe/templates/report.html" in names
        metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = message_from_bytes(archive.read(metadata_path))

    assert metadata["Requires-Python"] == ">=3.12"
    requirements = metadata.get_all("Requires-Dist", [])
    assert metadata.get_all("Provides-Extra", []) == ["test"]
    assert len(requirements) == 1
    assert requirements[0].lower().startswith("pytest>=8;")
    assert all(
        ";" in requirement and "extra" in requirement.lower() and "test" in requirement.lower()
        for requirement in requirements
    )

    _install_wheel(
        tool,
        wheel,
        install_target,
        cwd=tmp_path,
        environment=environment,
    )
    trace_path = tmp_path / "wheel-smoke.jsonl"
    trace_path.write_text(
        "".join(
            json.dumps(
                {
                    "schema_version": 1,
                    "trace_id": "wheel-smoke",
                    "event": event,
                    "timestamp_ns": 1,
                    "process_id": 1,
                    "thread_id": 1,
                    "task_id": None,
                    **fields,
                }
            )
            + "\n"
            for event, fields in (
                ("session_start", {}),
                ("session_end", {"status": "ok"}),
            )
        ),
        encoding="utf-8",
    )
    environment["PYTHONPATH"] = str(install_target)
    result = _run(
        [sys.executable, "-m", "observe", "render", str(trace_path)],
        cwd=tmp_path,
        environment=environment,
    )

    html_path = trace_path.with_suffix(".html")
    assert result.stdout.strip() == str(html_path)
    assert "trace-data" in html_path.read_text(encoding="utf-8")
