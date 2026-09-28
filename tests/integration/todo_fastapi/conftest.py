import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from observe import render


PROJECT_ROOT = Path(__file__).resolve().parents[3]
RUNNER = Path(__file__).with_name("scenario_runner.py")


@pytest.fixture
def run_scenario(tmp_path):
    def run(scenario):
        database_path = tmp_path / f"{scenario}.sqlite3"
        trace_path = tmp_path / f"{scenario}.jsonl"
        html_path = tmp_path / f"{scenario}.html"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(PROJECT_ROOT), environment.get("PYTHONPATH", "")]
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                scenario,
                str(database_path),
                str(trace_path),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        summary = json.loads(completed.stdout)
        records = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
        ]
        assert render(trace_path, html_path) == html_path
        assert html_path.is_file()
        return summary, records, trace_path

    return run
