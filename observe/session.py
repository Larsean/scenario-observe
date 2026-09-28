import asyncio
import itertools
import threading
import time
import uuid
import weakref
from contextvars import ContextVar
from dataclasses import dataclass

from observe.filters import FilterPolicy
from observe.monitor import find_frame
from observe.schema import make_record
from observe.serializer import (
    argument_snapshot,
    snapshot_value,
    validate_serialization_options,
)
from observe.symbols import identify_frame


ACTIVE_SESSION = ContextVar("observe_active_session", default=None)
LOGICAL_STACK = ContextVar("observe_logical_stack", default=())


def _exception_details(exception, *, max_string_length, max_object_bytes):
    exception_type = type(exception)
    try:
        name = type.__getattribute__(exception_type, "__name__")
    except (AttributeError, TypeError):
        name = "<unknown>"
    try:
        arguments = BaseException.__dict__["args"].__get__(exception, exception_type)
    except (AttributeError, TypeError):
        return name, "<message unavailable>"
    if type(arguments) is not tuple:
        return name, "<message unavailable>"
    if not arguments:
        return name, ""
    if len(arguments) == 1 and type(arguments[0]) is str:
        message = arguments[0]
    elif all(type(argument) is str for argument in arguments):
        message = ", ".join(arguments)
    else:
        return name, "<message omitted>"
    byte_limit = max(0, max_object_bytes - len("…".encode("utf-8")))
    candidate = message[:max_string_length]
    while candidate and len(candidate.encode("utf-8")) > byte_limit:
        candidate = candidate[:-1]
    if len(candidate) < len(message):
        candidate += "…"
    return name, candidate


@dataclass(eq=False)
class FrameState:
    frame: object
    included: bool
    call_id: str | None
    depth: int
    started_ns: int
    thread_id: int


class TraceSession:
    def __init__(
        self,
        function,
        writer,
        *,
        include=None,
        exclude=(),
        project_root=None,
        max_depth=12,
        max_calls=10_000,
        max_trace_bytes=20_000_000,
        max_object_bytes=64_000,
        serializers=None,
        redact=(),
        serializer_max_depth=6,
        max_items=100,
        max_string_length=4096,
        max_nodes=1000,
    ):
        self.function = function
        self.writer = writer
        self.trace_id = uuid.uuid4().hex
        self.task_ids = weakref.WeakKeyDictionary()
        self.task_id_counter = itertools.count(1)
        self._state_lock = threading.RLock()
        self.filter_policy = FilterPolicy(
            include=include,
            exclude=exclude,
            project_root=project_root,
        )
        self.max_depth = max_depth
        self.max_calls = max_calls
        if type(max_depth) is not int or max_depth < 0:
            raise ValueError("max_depth must be a non-negative integer")
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("max_calls must be a positive integer")
        self.serialization_options = {
            "serializers": serializers,
            "redact": redact,
            "max_depth": serializer_max_depth,
            "max_items": max_items,
            "max_string_length": max_string_length,
            "max_nodes": max_nodes,
            "max_object_bytes": max_object_bytes,
        }
        validate_serialization_options(self.serialization_options)
        start_record = self._budget_record("session_start")
        truncation_record = self._truncation_budget_record()
        end_record = self._budget_record("session_end", status="error")
        self.writer.configure_budget(
            max_trace_bytes,
            (start_record, truncation_record, end_record),
        )
        self.call_ids = itertools.count(1)
        self.frame_states = {}
        self.open_calls = {}
        self.active = True
        self.started = False
        self.truncated = False
        self.failed = False
        self.call_count = 0
        self.monitoring_scope = None

    def is_active_context(self):
        return self.active and ACTIVE_SESSION.get() is self

    def _record(self, event, **fields):
        record = make_record(self.trace_id, event, **fields)
        record["task_id"] = self._task_identifier()
        return record

    def _task_identifier(self):
        with self._state_lock:
            try:
                task = asyncio.current_task()
            except RuntimeError:
                task = None
            if task is None:
                return None
            task_id = self.task_ids.get(task)
            if task_id is None:
                task_id = f"task-{next(self.task_id_counter)}"
                self.task_ids[task] = task_id
            return task_id

    def _budget_record(self, event, **fields):
        record = self._record(event, **fields)
        record.update(
            timestamp_ns=9_999_999_999_999_999_999,
            process_id=9_999_999_999,
            thread_id=9_999_999_999_999_999_999,
            task_id="task-ffffffffffffffff",
        )
        return record

    def _truncation_budget_record(self):
        digit_count = len(str(self.max_calls))
        preview_count = min(self.max_calls, 8)
        largest_call_id = f"c{'9' * digit_count}"
        return self._budget_record(
            "trace_truncated",
            reason="max_trace_bytes",
            incomplete_call_count=self.max_calls,
            incomplete_call_ids=[largest_call_id] * preview_count,
            incomplete_call_ids_truncated=self.max_calls > preview_count,
        )

    def start(self):
        if self.started:
            return
        try:
            self.writer.write(self._record("session_start"))
            self.started = True
        except Exception as error:
            self.callback_failed(error)

    def finish(self, status):
        with self._state_lock:
            if not self.started:
                self.active = False
                self.frame_states.clear()
                return
            try:
                self.writer.write(self._record("session_end", status=status))
            except Exception:
                pass
            self.active = False
            self.frame_states.clear()

    def callback_failed(self, error):
        with self._state_lock:
            if self.failed or not self.active:
                return
            self.failed = True
            self.active = False
            self.frame_states.clear()
            LOGICAL_STACK.set(())
            try:
                self.writer.write(
                    self._record("trace_error", error_type=type(error).__name__)
                )
            except Exception:
                pass
            if self.monitoring_scope is not None:
                try:
                    self.monitoring_scope.disable()
                except Exception:
                    pass

    def _scoped_frame(self, code):
        if not self.is_active_context():
            return None
        return find_frame(code)

    def _parent_state(self):
        filtered_hops = 0
        thread_id = threading.get_ident()
        for state in reversed(LOGICAL_STACK.get()):
            if state.thread_id != thread_id:
                continue
            if state.included and state.call_id is not None:
                return state.call_id, state.depth + 1, filtered_hops
            if not state.included:
                filtered_hops += 1
        return None, 0, filtered_hops

    def _push_state(self, state):
        stack = LOGICAL_STACK.get()
        if not stack or stack[-1] is not state:
            LOGICAL_STACK.set(stack + (state,))

    def _remove_state(self, state):
        stack = LOGICAL_STACK.get()
        for index in range(len(stack) - 1, -1, -1):
            if stack[index] is state:
                LOGICAL_STACK.set(stack[:index] + stack[index + 1 :])
                return

    def on_start(self, code, instruction_offset):
        with self._state_lock:
            self._on_start(code, instruction_offset)

    def _on_start(self, code, instruction_offset):
        frame = self._scoped_frame(code)
        if frame is None:
            return
        current_state = self.frame_states.get(id(frame))
        if current_state is not None and current_state.frame is frame:
            self._push_state(current_state)
            return
        symbol = identify_frame(frame, self.filter_policy.project_root)
        included = self.filter_policy.includes(frame, symbol)
        parent_call_id, depth, filtered_hops = self._parent_state()
        started_ns = time.monotonic_ns()
        call_id = None
        if included:
            if depth > self.max_depth:
                self._truncate("max_depth")
                return
            if self.call_count >= self.max_calls:
                self._truncate("max_calls")
                return
            call_id = f"c{next(self.call_ids)}"
            docstring = frame.f_code.co_consts[0] if frame.f_code.co_consts else None
            if not isinstance(docstring, str):
                docstring = None
            record = self._record(
                "call",
                call_id=call_id,
                parent_call_id=parent_call_id,
                symbol={"module": symbol["module"], "qualname": symbol["qualname"]},
                source={"file": symbol["file"], "line": symbol["line"]},
                docstring=docstring,
                input=argument_snapshot(frame, **self.serialization_options),
                depth=depth,
                **({"filtered_hops": filtered_hops} if filtered_hops else {}),
            )
            if not self.writer.write(record):
                self._truncate("max_trace_bytes")
                return
            self.call_count += 1
            self.open_calls[call_id] = True
        state = FrameState(
            frame,
            included,
            call_id,
            depth,
            started_ns,
            threading.get_ident(),
        )
        self.frame_states[id(frame)] = state
        self._push_state(state)

    def on_resume(self, code, instruction_offset):
        with self._state_lock:
            self._on_resume(code, instruction_offset)

    def _on_resume(self, code, instruction_offset):
        frame = self._scoped_frame(code)
        if frame is None:
            return
        state = self.frame_states.get(id(frame))
        if state is not None and state.frame is frame:
            self._push_state(state)

    def on_throw(self, code, instruction_offset, exception):
        self.on_resume(code, instruction_offset)

    def on_yield(self, code, instruction_offset, value):
        with self._state_lock:
            self._on_yield(code, instruction_offset, value)

    def _on_yield(self, code, instruction_offset, value):
        frame = self._scoped_frame(code)
        if frame is None:
            return
        state = self.frame_states.get(id(frame))
        if state is not None and state.frame is frame:
            self._remove_state(state)

    def _complete(self, code, event, value):
        with self._state_lock:
            self._complete_locked(code, event, value)

    def _complete_locked(self, code, event, value):
        frame = self._scoped_frame(code)
        if frame is None:
            return
        state = self.frame_states.pop(id(frame), None)
        if state is None or state.frame is not frame:
            return
        self._remove_state(state)
        if not state.included or state.call_id is None:
            return
        if event == "return":
            elapsed_ns = time.monotonic_ns() - state.started_ns
            written = self.writer.write(
                self._record(
                    "return",
                    call_id=state.call_id,
                    output=snapshot_value(value, **self.serialization_options),
                    duration_ns=max(0, elapsed_ns),
                )
            )
            if not written:
                self._truncate("max_trace_bytes")
            else:
                self.open_calls.pop(state.call_id, None)
        else:
            error_type, message = _exception_details(
                value,
                max_string_length=self.serialization_options["max_string_length"],
                max_object_bytes=self.serialization_options["max_object_bytes"],
            )
            written = self.writer.write(
                self._record(
                    "unwind",
                    call_id=state.call_id,
                    exception={"type": error_type, "message": message},
                )
            )
            if not written:
                self._truncate("max_trace_bytes")
            else:
                self.open_calls.pop(state.call_id, None)

    def on_return(self, code, instruction_offset, value):
        self._complete(code, "return", value)

    def on_unwind(self, code, instruction_offset, exception):
        self._complete(code, "unwind", exception)

    def _truncate(self, reason):
        with self._state_lock:
            if self.truncated:
                return
            self.truncated = True
            self.active = False
            self.frame_states.clear()
            LOGICAL_STACK.set(())
            incomplete_call_ids = list(self.open_calls)[:8]
            try:
                self.writer.stop_details()
                self.writer.write(
                    self._record(
                        "trace_truncated",
                        reason=reason,
                        incomplete_call_count=len(self.open_calls),
                        incomplete_call_ids=incomplete_call_ids,
                        incomplete_call_ids_truncated=(len(self.open_calls) > 8),
                    )
                )
            except Exception as error:
                self.failed = True
                try:
                    self.writer.write(self._record("trace_error", error_type=type(error).__name__))
                except Exception:
                    pass
            if self.monitoring_scope is not None:
                self.monitoring_scope.disable()
