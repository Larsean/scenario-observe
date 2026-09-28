from pathlib import Path


def identify_frame(frame, project_root):
    module = frame.f_globals.get("__name__", "<unknown>")
    source_file = Path(frame.f_code.co_filename)
    try:
        source_file = source_file.resolve().relative_to(project_root)
    except ValueError:
        source_file = source_file.resolve()
    return {
        "module": module,
        "qualname": frame.f_code.co_qualname,
        "file": source_file.as_posix(),
        "line": frame.f_code.co_firstlineno,
    }


def canonical_name(symbol):
    return f"{symbol['module']}.{symbol['qualname']}"
