import fnmatch
from pathlib import Path


class FilterPolicy:
    def __init__(self, *, include=None, exclude=(), exclude_paths=(), project_root=None):
        self.include = tuple(include or ())
        self.exclude = tuple(exclude or ())
        self.exclude_paths = tuple(
            Path(path).expanduser().resolve() for path in (exclude_paths or ())
        )
        self.project_root = Path(project_root or Path.cwd()).expanduser().resolve()
        self.package_root = Path(__file__).resolve().parent
        self.decisions = {}

    def includes(self, frame, symbol):
        module = symbol["module"]
        key = (frame.f_code, module)
        if key not in self.decisions:
            self.decisions[key] = self._decide(frame, symbol)
        return self.decisions[key]

    def _decide(self, frame, symbol):
        source_name = frame.f_code.co_filename
        if source_name.startswith("<") and source_name.endswith(">"):
            return False
        source_file = Path(source_name).expanduser().resolve()
        try:
            source_file.relative_to(self.package_root)
        except ValueError:
            pass
        else:
            return False

        if any(
            source_file == excluded_path or excluded_path in source_file.parents
            for excluded_path in self.exclude_paths
        ):
            return False

        canonical = f"{symbol['module']}.{symbol['qualname']}"
        if any(fnmatch.fnmatchcase(canonical, pattern) for pattern in self.exclude):
            return False
        if any(fnmatch.fnmatchcase(canonical, pattern) for pattern in self.include):
            return True
        try:
            source_file.relative_to(self.project_root)
        except ValueError:
            return False
        return True
