import json
from html.parser import HTMLParser
from pathlib import Path
import re

import pytest

import observe


def _record(event, **fields):
    record = {
        "schema_version": 1,
        "trace_id": "trace-1",
        "event": event,
        "timestamp_ns": 123,
        "process_id": 42,
        "thread_id": 101,
        "task_id": "task-1",
    }
    record.update(fields)
    return record


def _call(call_id, *, parent=None, name="scenario", thread=101, task="task-1", **fields):
    return _record(
        "call",
        call_id=call_id,
        parent_call_id=parent,
        symbol={"module": "app", "qualname": name},
        source={"file": "app.py", "line": 12},
        input={},
        depth=0,
        thread_id=thread,
        task_id=task,
        **fields,
    )


def _write_trace(path, records):
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


class _ReportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.trace_json = []
        self.script_sources = []
        self.link_targets = []
        self._in_trace = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script":
            self._in_trace = attributes.get("id") == "trace-data"
            if attributes.get("src"):
                self.script_sources.append(attributes["src"])
        elif tag == "link" and attributes.get("href"):
            self.link_targets.append(attributes["href"])

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_trace = False

    def handle_data(self, data):
        if self._in_trace:
            self.trace_json.append(data)


def _embedded_document(html):
    parser = _ReportParser()
    parser.feed(html)
    return json.loads("".join(parser.trace_json)), parser


def _flatten(nodes):
    for node in nodes:
        yield node
        yield from _flatten(node["children"])


def test_render_reconstructs_lifecycle_roots_and_incomplete_calls(tmp_path):
    trace_path = tmp_path / "scenario.jsonl"
    output_path = tmp_path / "scenario.html"
    _write_trace(
        trace_path,
        [
            _record("session_start"),
            _call("c1", name="root"),
            _call("c2", parent="c1", name="child", filtered_hops=2),
            _record("return", call_id="c2", output={"ok": True}, duration_ns=45),
            _record(
                "unwind",
                call_id="c1",
                exception={"type": "ValueError", "message": "bad"},
            ),
            _call("c3", name="other_root", thread=202, task=None),
            _record("session_end", status="error"),
        ],
    )

    result = observe.render(trace_path, output_path)

    assert result == output_path
    document, _ = _embedded_document(output_path.read_text(encoding="utf-8"))
    groups = {(group["thread_id"], group["task_id"]): group for group in document["root_groups"]}
    nodes = {node["call_id"]: node for node in _flatten(document["roots"])}

    assert groups[(101, "task-1")]["root_call_ids"] == ["c1"]
    assert nodes["c1"]["status"] == "unwind"
    assert nodes["c1"]["exception"]["type"] == "ValueError"
    assert nodes["c1"]["docstring"] is None
    assert nodes["c2"]["status"] == "return"
    assert nodes["c2"]["output"] == {"ok": True}
    assert nodes["c2"]["filtered_hops"] == 2
    assert groups[(202, None)]["root_call_ids"] == ["c3"]
    assert nodes["c3"]["status"] == "incomplete"


def test_render_preserves_docstring_text_in_call_details(tmp_path):
    trace_path = tmp_path / "docstring.jsonl"
    output_path = tmp_path / "docstring.html"
    docstring = (
        "Create a todo.\n\n"
        "    Args:\n"
        "        title: <script>not executable</script> & keep as text.\n\n"
        "    Returns:\n"
        "        The created todo.\n"
    )
    _write_trace(
        trace_path,
        [
            _call("c1", name="create_todo", docstring=docstring),
            _record("return", call_id="c1", output={"id": 1}, duration_ns=1),
        ],
    )

    observe.render(trace_path, output_path)

    html = output_path.read_text(encoding="utf-8")
    document, _ = _embedded_document(html)
    node = document["roots"][0]
    assert node["docstring"] == docstring
    assert r"\u003cscript\u003enot executable\u003c/script\u003e \u0026" in html
    assert 'appendTextDetailSection(details, "Docstring", node.docstring)' in html
    assert "content.textContent = value;" in html


def test_render_prioritizes_node_details_and_shallowly_formats_values(tmp_path):
    trace_path = tmp_path / "readable-details.jsonl"
    output_path = tmp_path / "readable-details.html"
    input_value = {
        "todo": {"title": "Learn FastAPI", "metadata": {"private": True}},
        "limit": 1,
    }
    output_value = {
        "items": [{"id": 1, "title": "Learn FastAPI"}],
        "total": 1,
    }
    call = _call("c1", name="create_todo")
    call["input"] = input_value
    _write_trace(
        trace_path,
        [
            call,
            _record("return", call_id="c1", output=output_value, duration_ns=1),
        ],
    )

    observe.render(trace_path, output_path)

    html = output_path.read_text(encoding="utf-8")
    document, _ = _embedded_document(html)
    node = document["roots"][0]

    assert node["input"] == input_value
    assert node["output"] == output_value
    assert html.index('<section id="details-panel"') < html.index('<aside id="execution-sidebar"')
    assert '<details id="tree-sidebar" open>' in html
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]
    node_label_rule = re.search(r"\.node-label\s*\{([^}]*)\}", style).group(1)
    structured_value_rule = re.search(r"\.structured-value\s*\{([^}]*)\}", style).group(1)
    value_row_rule = re.search(r"\.value-row\s*\{([^}]*)\}", style).group(1)
    assert "min-width: 0;" in node_label_rule
    assert "white-space: normal;" in node_label_rule
    assert "overflow-wrap: anywhere;" in node_label_rule
    assert "border: 1px solid #394a59;" in structured_value_rule
    assert "border-bottom: 1px solid #394a59;" in value_row_rule
    assert "function appendStructuredValue(parent, value, depth = 0)" in html
    assert "const MAX_VALUE_DEPTH = 1;" in html
    assert "depth <= MAX_VALUE_DEPTH" in html
    assert 'appendStructuredDetailSection(details, "Input", node.input);' in html
    assert 'appendStructuredDetailSection(details, "Output", node.output);' in html
    assert "showDetails(firstSelection.group, firstSelection.wrapper);" in html


def test_render_preserves_large_integers_losslessly(tmp_path):
    trace_path = tmp_path / "large-integer.jsonl"
    output_path = tmp_path / "large-integer.html"
    value = 9_007_199_254_740_993
    call = _call("c1", name="large_integer")
    call["input"] = {"value": value}
    _write_trace(
        trace_path,
        [
            call,
            _record("return", call_id="c1", output={"value": value}, duration_ns=1),
        ],
    )

    observe.render(trace_path, output_path)

    document, _ = _embedded_document(output_path.read_text(encoding="utf-8"))
    node = document["roots"][0]
    assert node["input"]["value"] == str(value)
    assert node["output"]["value"] == str(value)
    source_records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert source_records[0]["input"]["value"] == value


def test_render_marks_child_calls_from_another_task(tmp_path):
    trace_path = tmp_path / "cross-task.jsonl"
    output_path = tmp_path / "cross-task.html"
    _write_trace(
        trace_path,
        [
            _call("c1", name="root", task="task-1"),
            _call("c2", parent="c1", name="child_task", task="task-2"),
        ],
    )

    observe.render(trace_path, output_path)

    document, _ = _embedded_document(output_path.read_text(encoding="utf-8"))
    nodes = {node["call_id"]: node for node in _flatten(document["roots"])}
    assert nodes["c2"]["task_boundary"] is True
    html = output_path.read_text(encoding="utf-8")
    assert "first.task_boundary" in html
    assert "Task boundary:" in html


def test_render_reports_corrupt_tail_and_unknown_schema(tmp_path):
    trace_path = tmp_path / "damaged.jsonl"
    trace_path.write_bytes(
        (json.dumps(_record("session_start")) + "\n").encode("utf-8")
        + b'{"schema_version":1,"event":"call"'
    )

    with pytest.raises(ValueError, match=r"damaged\.jsonl:2.*truncated JSONL record"):
        observe.render(trace_path)

    trace_path.write_text(
        json.dumps(_record("session_start", schema_version=9)) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"damaged\.jsonl:1.*unsupported schema_version 9"):
        observe.render(trace_path)


def test_render_reports_non_string_event_and_trace_ids(tmp_path):
    trace_path = tmp_path / "invalid-fields.jsonl"
    _write_trace(trace_path, [_record(["session_start"])])

    with pytest.raises(ValueError, match=r"invalid-fields\.jsonl:1.*unsupported event"):
        observe.render(trace_path)

    _write_trace(trace_path, [_record("session_start", trace_id={"bad": "id"})])

    with pytest.raises(ValueError, match=r"invalid-fields\.jsonl:1.*trace_id must be a non-empty string"):
        observe.render(trace_path)


def test_html_escapes_untrusted_data_and_uses_offline_dom_text(tmp_path):
    trace_path = tmp_path / "unsafe.jsonl"
    output_path = tmp_path / "unsafe.html"
    malicious = "</script><script>alert('xss')</script>"
    windows_path = r"C:\Users\Example\app.py"
    call = _call("c1", name="Root")
    call["source"]["file"] = windows_path
    call["input"] = {"payload": malicious, "title": "繁體中文"}
    _write_trace(trace_path, [_record("session_start"), call])

    observe.render(trace_path, output_path)

    html = output_path.read_text(encoding="utf-8")
    document, parser = _embedded_document(html)
    root = document["roots"][0]

    assert r"\u003c/script\u003e" in html
    assert malicious not in html
    assert root["input"]["payload"] == malicious
    assert root["source"]["file"] == windows_path
    assert root["input"]["title"] == "繁體中文"
    assert "innerHTML" not in html
    assert "textContent" in html
    assert "fetch(" not in html
    assert parser.script_sources == []
    assert parser.link_targets == []


def test_render_handles_large_traces_and_exposes_interactive_controls(tmp_path):
    trace_path = tmp_path / "large.jsonl"
    output_path = tmp_path / "large.html"
    records = [_record("session_start")]
    records.extend(_call(f"c{index}", name="repeat" if index < 4 else f"call_{index}") for index in range(1, 1201))
    records.append(_record("session_end", status="ok"))
    _write_trace(trace_path, records)

    observe.render(trace_path, output_path)

    html = output_path.read_text(encoding="utf-8")
    document, _ = _embedded_document(html)

    assert len(document["roots"]) == 1200
    assert output_path.stat().st_size > 100_000
    assert 'id="search"' in html
    assert 'id="expand-all"' in html
    assert 'id="collapse-all"' in html
    assert "foldSiblings" in html
    assert "filtered calls" in html
