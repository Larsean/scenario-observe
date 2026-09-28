import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from observe.writer import JsonlWriter


def test_writer_emits_utf8_jsonl_records(tmp_path):
    output = tmp_path / "nested" / "trace.jsonl"

    with JsonlWriter(output) as writer:
        writer.write({"event": "call", "input": {"title": "繁體中文"}})
        writer.write({"event": "return", "output": [1, 2]})

    content = output.read_bytes()
    lines = content.decode("utf-8").splitlines()

    assert len(lines) == 2
    assert "繁體中文" in lines[0]
    assert json.loads(lines[0]) == {"event": "call", "input": {"title": "繁體中文"}}
    assert json.loads(lines[1]) == {"event": "return", "output": [1, 2]}
    assert content.endswith(b"\n")


def test_writer_replaces_existing_trace_on_open(tmp_path):
    output = tmp_path / "trace.jsonl"
    output.write_text("stale trace\n", encoding="utf-8")

    with JsonlWriter(output) as writer:
        writer.write({"event": "session_start"})

    assert output.read_text(encoding="utf-8") == '{"event":"session_start"}\n'


def test_writer_budget_counts_utf8_bytes_and_reserves_control_records(tmp_path):
    output = tmp_path / "bounded.jsonl"
    start = {"event": "session_start"}
    detail = {"event": "call", "input": {"title": "繁體中文" * 8}}
    truncated = {"event": "trace_truncated", "reason": "max_trace_bytes"}
    end = {"event": "session_end", "status": "ok"}
    records = (start, detail, truncated, end)
    max_bytes = sum(
        len((json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        for record in records
    )

    with JsonlWriter(output) as writer:
        writer.configure_budget(max_bytes, (start, truncated, end))
        assert writer.write(start)
        assert writer.write(detail)
        assert writer.write({"event": "return", "output": "extra"}) is False
        assert writer.write(truncated)
        assert writer.write(end)

    assert len(output.read_bytes()) == max_bytes
    assert "繁體中文" in output.read_text(encoding="utf-8")


def test_concurrent_writes_never_exceed_utf8_byte_budget(tmp_path):
    output = tmp_path / "concurrent.jsonl"
    start = {"event": "session_start"}
    detail = {"event": "call", "input": {"label": "並行"}}
    truncated = {"event": "trace_truncated", "reason": "max_trace_bytes"}
    end = {"event": "session_end", "status": "ok"}
    line_size = JsonlWriter._line_size
    max_bytes = line_size(start) + line_size(detail) + line_size(truncated) + line_size(end)

    class SlowStream:
        def __init__(self, stream):
            self.stream = stream

        def write(self, value):
            time.sleep(0.01)
            return self.stream.write(value)

        def flush(self):
            return self.stream.flush()

        def close(self):
            return self.stream.close()

    with JsonlWriter(output) as writer:
        writer.configure_budget(max_bytes, (start, truncated, end))
        writer.write(start)
        writer._stream = SlowStream(writer._stream)
        barrier = threading.Barrier(8)

        def write_detail():
            barrier.wait()
            return writer.write(detail)

        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(lambda _: write_detail(), range(8)))

        assert sum(accepted) == 1
        writer.write(truncated)
        writer.write(end)

    assert len(output.read_bytes()) <= max_bytes
