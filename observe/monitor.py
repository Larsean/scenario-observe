import sys
import threading


_scope_lock = threading.Lock()


def find_frame(code):
    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_code is code:
            return frame
        frame = frame.f_back
    return None


class MonitoringScope:
    def __init__(self, session):
        self.session = session
        self.monitoring = sys.monitoring
        self.tool_id = None
        self.registered_events = []
        self.lock_acquired = False

    def __enter__(self):
        if not _scope_lock.acquire(blocking=False):
            raise RuntimeError("a trace monitoring scope is already active")
        self.lock_acquired = True
        try:
            self.tool_id = self._acquire_tool_id()
            self.session.monitoring_scope = self
            callbacks = (
                (self.monitoring.events.PY_START, self.session.on_start),
                (self.monitoring.events.PY_RESUME, self.session.on_resume),
                (self.monitoring.events.PY_THROW, self.session.on_throw),
                (self.monitoring.events.PY_YIELD, self.session.on_yield),
                (self.monitoring.events.PY_RETURN, self.session.on_return),
                (self.monitoring.events.PY_UNWIND, self.session.on_unwind),
            )
            event_mask = self.monitoring.events.NO_EVENTS
            for event, callback in callbacks:
                self.monitoring.register_callback(self.tool_id, event, self._guard(callback))
                self.registered_events.append(event)
                event_mask |= event
            self.monitoring.set_events(self.tool_id, event_mask)
            return self
        except BaseException:
            self._cleanup()
            raise

    def _acquire_tool_id(self):
        for tool_id in (3, 4):
            if self.monitoring.get_tool(tool_id) is not None:
                continue
            try:
                self.monitoring.use_tool_id(tool_id, "scenario-observe")
            except ValueError:
                continue
            return tool_id
        raise RuntimeError("sys.monitoring tool IDs 3 and 4 are occupied")

    def _guard(self, callback):
        def guarded(*args):
            if not self.session.is_active_context():
                return None
            try:
                callback(*args)
            except Exception as error:
                self.session.callback_failed(error)
            return None

        return guarded

    def disable(self):
        if self.tool_id is None:
            return
        try:
            self.monitoring.set_events(self.tool_id, self.monitoring.events.NO_EVENTS)
        except Exception as error:
            self.session.callback_failed(error)

    def _cleanup(self):
        if self.tool_id is not None:
            try:
                self.monitoring.set_events(self.tool_id, self.monitoring.events.NO_EVENTS)
            except Exception:
                pass
            for event in self.registered_events:
                try:
                    self.monitoring.register_callback(self.tool_id, event, None)
                except Exception:
                    pass
            try:
                self.monitoring.free_tool_id(self.tool_id)
            except Exception:
                pass
            self.tool_id = None
        if self.lock_acquired:
            self.lock_acquired = False
            _scope_lock.release()

    def __exit__(self, exc_type, exc_value, traceback):
        self._cleanup()
        return False
