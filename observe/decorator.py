import inspect
from functools import wraps

from observe.monitor import MonitoringScope
from observe.session import ACTIVE_SESSION, LOGICAL_STACK, TraceSession
from observe.writer import JsonlWriter


class _TracedGenerator:
    def __init__(self, iterator, target, output, options):
        self.iterator = iterator
        self.target = target
        self.output = output
        self.options = options
        self.session = None
        self.writer = None
        self.finished = False

    def __iter__(self):
        return self

    def __next__(self):
        return self._advance(self.iterator.__next__)

    def send(self, value):
        return self._advance(self.iterator.send, value)

    def throw(self, *args):
        return self._advance(self.iterator.throw, *args)

    def close(self):
        if self.session is None:
            try:
                return self.iterator.close()
            finally:
                self.finished = True
        result = self._advance(self.iterator.close)
        self._finish("ok")
        return result

    def _ensure_trace(self):
        active_session = ACTIVE_SESSION.get()
        if active_session is not None and active_session.active:
            raise RuntimeError("a trace scope is already active")
        writer = JsonlWriter(self.output)
        try:
            session = TraceSession(self.target, writer, **self.options)
        except BaseException:
            try:
                writer.close()
            except Exception:
                pass
            raise
        self.writer = writer
        self.session = session

    def _advance(self, operation, *args):
        if self.finished:
            return operation(*args)
        if self.session is None:
            self._ensure_trace()
        try:
            with MonitoringScope(self.session):
                active_token = ACTIVE_SESSION.set(self.session)
                stack_token = LOGICAL_STACK.set(())
                try:
                    self.session.start()
                    result = operation(*args)
                finally:
                    LOGICAL_STACK.reset(stack_token)
                    ACTIVE_SESSION.reset(active_token)
        except StopIteration:
            self._finish("ok")
            raise
        except BaseException:
            self._finish("error")
            raise
        return result

    def _finish(self, status):
        if self.finished:
            return
        self.finished = True
        if self.session is None:
            return
        self.session.finish(status)
        try:
            self.writer.close()
        except Exception:
            pass

    def __del__(self):
        try:
            if self.session is None:
                self.finished = True
            else:
                self.close()
        except BaseException:
            self._finish("error")


class _TracedAsyncGenerator:
    def __init__(self, iterator, target, output, options):
        self.iterator = iterator
        self.target = target
        self.output = output
        self.options = options
        self.session = None
        self.writer = None
        self.finished = False

    def __aiter__(self):
        return self

    def __anext__(self):
        return self._advance(self.iterator.__anext__)

    def asend(self, value):
        return self._advance(self.iterator.asend, value)

    def athrow(self, *args):
        return self._advance(self.iterator.athrow, *args)

    def aclose(self):
        if self.session is None:
            async def close_unstarted():
                try:
                    return await self.iterator.aclose()
                finally:
                    self.finished = True

            return close_unstarted()
        async def close_iterator():
            result = await self._advance(self.iterator.aclose)
            self._finish("ok")
            return result

        return close_iterator()

    def _ensure_trace(self):
        active_session = ACTIVE_SESSION.get()
        if active_session is not None and active_session.active:
            raise RuntimeError("a trace scope is already active")
        writer = JsonlWriter(self.output)
        try:
            session = TraceSession(self.target, writer, **self.options)
        except BaseException:
            try:
                writer.close()
            except Exception:
                pass
            raise
        self.writer = writer
        self.session = session

    async def _advance(self, operation, *args):
        if self.finished:
            return await operation(*args)
        if self.session is None:
            self._ensure_trace()
        try:
            with MonitoringScope(self.session):
                active_token = ACTIVE_SESSION.set(self.session)
                stack_token = LOGICAL_STACK.set(())
                try:
                    self.session.start()
                    result = await operation(*args)
                finally:
                    LOGICAL_STACK.reset(stack_token)
                    ACTIVE_SESSION.reset(active_token)
        except StopAsyncIteration:
            self._finish("ok")
            raise
        except BaseException:
            self._finish("error")
            raise
        return result

    def _finish(self, status):
        if self.finished:
            return
        self.finished = True
        if self.session is None:
            return
        self.session.finish(status)
        try:
            self.writer.close()
        except Exception:
            pass

    def __del__(self):
        if self.session is None:
            self.finished = True
        else:
            self._finish("error")


def trace(
    function=None,
    *,
    output=".observe/trace.jsonl",
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
    def decorate(target):
        options = {
            "include": include,
            "exclude": exclude,
            "project_root": project_root,
            "max_depth": max_depth,
            "max_calls": max_calls,
            "max_trace_bytes": max_trace_bytes,
            "max_object_bytes": max_object_bytes,
            "serializers": serializers,
            "redact": redact,
            "serializer_max_depth": serializer_max_depth,
            "max_items": max_items,
            "max_string_length": max_string_length,
            "max_nodes": max_nodes,
        }

        def create_trace():
            active_session = ACTIVE_SESSION.get()
            if active_session is not None and active_session.active:
                raise RuntimeError("a trace scope is already active")
            writer = JsonlWriter(output)
            try:
                session = TraceSession(target, writer, **options)
            except BaseException:
                try:
                    writer.close()
                except Exception:
                    pass
                raise
            return session, writer

        def run_sync(*args, **kwargs):
            session, writer = create_trace()
            try:
                with MonitoringScope(session):
                    active_token = ACTIVE_SESSION.set(session)
                    stack_token = LOGICAL_STACK.set(())
                    try:
                        session.start()
                        result = target(*args, **kwargs)
                    finally:
                        LOGICAL_STACK.reset(stack_token)
                        ACTIVE_SESSION.reset(active_token)
            except BaseException:
                try:
                    session.finish("error")
                except BaseException:
                    pass
                raise
            else:
                session.finish("ok")
                return result
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        async def run_async(*args, **kwargs):
            session, writer = create_trace()
            try:
                with MonitoringScope(session):
                    active_token = ACTIVE_SESSION.set(session)
                    stack_token = LOGICAL_STACK.set(())
                    try:
                        session.start()
                        result = await target(*args, **kwargs)
                    finally:
                        LOGICAL_STACK.reset(stack_token)
                        ACTIVE_SESSION.reset(active_token)
            except BaseException:
                try:
                    session.finish("error")
                except BaseException:
                    pass
                raise
            else:
                session.finish("ok")
                return result
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        if inspect.isasyncgenfunction(target):
            @wraps(target)
            def wrapped_async_generator(*args, **kwargs):
                iterator = target(*args, **kwargs)
                return _TracedAsyncGenerator(iterator, target, output, options)

            return wrapped_async_generator

        if inspect.iscoroutinefunction(target):
            @wraps(target)
            async def wrapped_async(*args, **kwargs):
                return await run_async(*args, **kwargs)

            return wrapped_async

        if inspect.isgeneratorfunction(target):
            @wraps(target)
            def wrapped_generator(*args, **kwargs):
                iterator = target(*args, **kwargs)
                return _TracedGenerator(iterator, target, output, options)

            return wrapped_generator

        @wraps(target)
        def wrapped(*args, **kwargs):
            return run_sync(*args, **kwargs)

        return wrapped

    if function is None:
        return decorate
    return decorate(function)
