from pathlib import Path
from types import SimpleNamespace

from observe.filters import FilterPolicy


def _frame(filename: str):
    return SimpleNamespace(f_code=compile("pass", filename, "exec"))


def _symbol(module: str = "example", qualname: str = "call"):
    return {"module": module, "qualname": qualname}


def test_excluded_source_path_is_not_traced_even_when_inside_project_root(tmp_path):
    virtualenv = tmp_path / ".venv"
    source = virtualenv / "lib/python3.13/site-packages/example/__init__.py"
    policy = FilterPolicy(
        project_root=tmp_path,
        include=("example.*",),
        exclude_paths=(virtualenv,),
    )

    assert not policy.includes(_frame(str(source)), _symbol())


def test_synthetic_frozen_filename_is_not_resolved_into_project_root():
    policy = FilterPolicy(project_root=Path.cwd())

    assert not policy.includes(
        _frame("<frozen importlib._bootstrap>"),
        _symbol("importlib._bootstrap", "_find_and_load"),
    )


def test_path_exclusion_keeps_project_source_included(tmp_path):
    virtualenv = tmp_path / ".venv"
    source = tmp_path / "scripts/evaluation.py"
    policy = FilterPolicy(project_root=tmp_path, exclude_paths=(virtualenv,))

    assert policy.includes(_frame(str(source)), _symbol("scripts.evaluation"))
