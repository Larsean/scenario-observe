import asyncio
import os
import threading
import time


SCHEMA_VERSION = 1


def task_identifier():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return f"task-{id(task):x}" if task is not None else None


def make_record(trace_id, event, **fields):
    record = {
        "schema_version": SCHEMA_VERSION,
        "trace_id": trace_id,
        "event": event,
        "timestamp_ns": time.time_ns(),
        "process_id": os.getpid(),
        "thread_id": threading.get_ident(),
        "task_id": task_identifier(),
    }
    record.update(fields)
    return record
