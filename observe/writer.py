import json
import threading
from pathlib import Path


class JsonlWriter:
    def __init__(self, output):
        self.path = Path(output).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("w", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()
        self.closed = False
        self.details_closed = False
        self.bytes_written = 0
        self.max_bytes = None
        self.control_reserve = 0

    def configure_budget(self, max_bytes, control_records):
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_trace_bytes must be a positive integer")
        sizes = [self._line_size(record) for record in control_records]
        minimum_bytes = sum(sizes)
        if max_bytes < minimum_bytes:
            raise ValueError(
                "max_trace_bytes is too small for session metadata and control records"
            )
        with self._lock:
            if self.closed:
                raise ValueError("JSONL writer is closed")
            self.max_bytes = max_bytes
            self.control_reserve = sum(sizes[1:])

    @staticmethod
    def _encoded_line(record):
        return json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"

    @classmethod
    def _line_size(cls, record):
        return len(cls._encoded_line(record).encode("utf-8"))

    def write(self, record):
        line = self._encoded_line(record)
        line_size = len(line.encode("utf-8"))
        with self._lock:
            if self.closed:
                raise ValueError("JSONL writer is closed")
            event = record.get("event")
            is_control = event in {"session_start", "trace_truncated", "session_end"}
            if self.details_closed and not is_control:
                return False
            if self.max_bytes is not None:
                reserve = 0 if is_control else self.control_reserve
                if self.bytes_written + line_size + reserve > self.max_bytes:
                    return False
            self._stream.write(line)
            self._stream.flush()
            self.bytes_written += line_size
            return True

    def stop_details(self):
        with self._lock:
            self.details_closed = True

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False
