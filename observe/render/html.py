import json
from importlib.resources import files
from pathlib import Path


SCHEMA_VERSION = 1
KNOWN_EVENTS = {
    "session_start",
    "call",
    "return",
    "unwind",
    "trace_truncated",
    "trace_error",
    "session_end",
}


class TraceFormatError(ValueError):
    pass


def _reject_constant(value):
    raise ValueError(f"non-standard JSON value {value}")


def _read_records(path):
    records = []
    with path.open("rb") as stream:
        line_number = 0
        while raw_line := stream.readline():
            line_number += 1
            try:
                line = raw_line.decode("utf-8")
                record = json.loads(line, parse_constant=_reject_constant)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                tail_is_partial = not raw_line.endswith(b"\n")
                diagnosis = "truncated JSONL record" if tail_is_partial else "invalid JSONL record"
                raise TraceFormatError(f"{path}:{line_number}: {diagnosis}: {error}") from error
            if type(record) is not dict:
                raise TraceFormatError(f"{path}:{line_number}: JSONL record must be an object")
            version = record.get("schema_version")
            if type(version) is not int or version != SCHEMA_VERSION:
                raise TraceFormatError(
                    f"{path}:{line_number}: unsupported schema_version {version!r}; "
                    f"expected {SCHEMA_VERSION}"
                )
            event = record.get("event")
            if not isinstance(event, str) or event not in KNOWN_EVENTS:
                raise TraceFormatError(f"{path}:{line_number}: unsupported event {event!r}")
            records.append((line_number, record))
    if not records:
        raise TraceFormatError(f"{path}: no JSONL records found")
    return records


def _call_node(path, line_number, record):
    call_id = record.get("call_id")
    parent_call_id = record.get("parent_call_id")
    if not isinstance(call_id, str) or not call_id:
        raise TraceFormatError(f"{path}:{line_number}: call record has no valid call_id")
    if parent_call_id is not None and not isinstance(parent_call_id, str):
        raise TraceFormatError(f"{path}:{line_number}: parent_call_id must be a string or null")
    symbol = record.get("symbol")
    if type(symbol) is not dict or not isinstance(symbol.get("module"), str) or not isinstance(
        symbol.get("qualname"), str
    ):
        raise TraceFormatError(f"{path}:{line_number}: call record has no valid symbol")
    source = record.get("source")
    if type(source) is not dict:
        source = {}
    thread_id = record.get("thread_id")
    task_id = record.get("task_id")
    if thread_id is not None and type(thread_id) is not int:
        raise TraceFormatError(f"{path}:{line_number}: thread_id must be an integer or null")
    if task_id is not None and not isinstance(task_id, str):
        raise TraceFormatError(f"{path}:{line_number}: task_id must be a string or null")
    filtered_hops = record.get("filtered_hops", 0)
    if type(filtered_hops) is not int or filtered_hops < 0:
        raise TraceFormatError(f"{path}:{line_number}: filtered_hops must be non-negative")
    return {
        "call_id": call_id,
        "parent_call_id": parent_call_id,
        "symbol": symbol,
        "source": source,
        "input": record.get("input"),
        "output": None,
        "duration_ns": None,
        "exception": None,
        "thread_id": thread_id,
        "task_id": task_id,
        "depth": record.get("depth"),
        "filtered_hops": filtered_hops,
        "status": "incomplete",
        "children": [],
    }


def _validate_parent_graph(path, nodes):
    state = {}
    for call_id in nodes:
        current = call_id
        trail = []
        while current in nodes and state.get(current, 0) == 0:
            state[current] = 1
            trail.append(current)
            current = nodes[current]["parent_call_id"]
        if current in nodes and state.get(current) == 1:
            raise TraceFormatError(f"{path}: cyclic parent_call_id chain includes {current!r}")
        for visited in trail:
            state[visited] = 2


def _build_document(path, records):
    nodes = {}
    terminal_ids = set()
    trace_ids = set()
    truncations = []
    trace_errors = []
    session_status = "incomplete"

    for line_number, record in records:
        trace_id = record.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id:
            raise TraceFormatError(f"{path}:{line_number}: trace_id must be a non-empty string")
        trace_ids.add(trace_id)
        if len(trace_ids) > 1:
            raise TraceFormatError(f"{path}:{line_number}: mixed trace_id values")
        event = record["event"]
        if event == "call":
            node = _call_node(path, line_number, record)
            if node["call_id"] in nodes:
                raise TraceFormatError(
                    f"{path}:{line_number}: duplicate call_id {node['call_id']!r}"
                )
            nodes[node["call_id"]] = node
        elif event in {"return", "unwind"}:
            call_id = record.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise TraceFormatError(f"{path}:{line_number}: {event} has no valid call_id")
            if call_id not in nodes:
                raise TraceFormatError(
                    f"{path}:{line_number}: {event} references unknown call_id {call_id!r}"
                )
            if call_id in terminal_ids:
                raise TraceFormatError(
                    f"{path}:{line_number}: duplicate terminal event for call_id {call_id!r}"
                )
            terminal_ids.add(call_id)
            node = nodes[call_id]
            node["status"] = event
            if event == "return":
                node["output"] = record.get("output")
                node["duration_ns"] = record.get("duration_ns")
            else:
                node["exception"] = record.get("exception")
        elif event == "trace_truncated":
            truncations.append(
                {
                    "reason": record.get("reason"),
                    "incomplete_call_count": record.get("incomplete_call_count"),
                    "incomplete_call_ids": record.get("incomplete_call_ids", []),
                }
            )
        elif event == "trace_error":
            trace_errors.append(record.get("error_type", "unknown error"))
        elif event == "session_end":
            session_status = record.get("status", "unknown")

    _validate_parent_graph(path, nodes)
    roots = []
    for node in nodes.values():
        parent_id = node["parent_call_id"]
        if parent_id is not None and parent_id in nodes:
            nodes[parent_id]["children"].append(node)
        else:
            if parent_id is not None:
                node["orphaned_parent_call_id"] = parent_id
            roots.append(node)

    root_groups = []
    group_index = {}
    for node in roots:
        group_key = (node["thread_id"], node["task_id"])
        group_position = group_index.get(group_key)
        if group_position is None:
            group_position = len(root_groups)
            group_index[group_key] = group_position
            root_groups.append(
                {
                    "thread_id": node["thread_id"],
                    "task_id": node["task_id"],
                    "root_call_ids": [],
                }
            )
        root_groups[group_position]["root_call_ids"].append(node["call_id"])

    return {
        "schema_version": SCHEMA_VERSION,
        "trace_id": next(iter(trace_ids), None),
        "status": session_status,
        "truncated": bool(truncations),
        "truncations": truncations,
        "trace_errors": trace_errors,
        "root_groups": root_groups,
        "roots": roots,
    }


def _serialize_document(document):
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render(input_path, output=None):
    source = Path(input_path).expanduser()
    records = _read_records(source)
    document = _build_document(source, records)
    destination = Path(output).expanduser() if output is not None else source.with_suffix(".html")
    if source.resolve() == destination.resolve():
        raise ValueError("HTML output must not overwrite the JSONL input")
    template = files("observe").joinpath("templates", "report.html").read_text(
        encoding="utf-8"
    )
    html = template.replace("{{TRACE_DATA}}", _serialize_document(document))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8", newline="\n")
    return destination
