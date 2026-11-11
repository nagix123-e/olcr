"""Request-owned cancellation, including a provider waiting for its first token."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
import http.client
import re
import socket
import threading
import time
import uuid
from urllib import request


class GenerationCancelled(RuntimeError):
    pass


class Generation:
    def __init__(self):
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks = set()

    def cancel(self):
        with self._lock:
            if self.event.is_set():
                return
            self.event.set()
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # A failed close must not leave the other provider/process
                # handles alive. Cancellation is already authoritative.
                continue

    set = cancel

    def is_set(self):
        return self.event.is_set()

    @contextmanager
    def interrupt(self, callback):
        with self._lock:
            self._callbacks.add(callback)
            cancelled = self.event.is_set()
        try:
            if cancelled:
                callback()
            check_cancelled(self)
            yield
            check_cancelled(self)
        finally:
            with self._lock:
                self._callbacks.discard(callback)


_active = ContextVar('olcr_active_generation', default=None)
_lock = threading.Lock()
_running: dict[str, Generation] = {}
_pending: dict[str, float] = {}


def current_generation() -> Generation | None:
    return _active.get()


def check_cancelled(generation=None):
    generation = generation or current_generation()
    if generation is not None and generation.event.is_set():
        raise GenerationCancelled('Generation stopped by user')


def open_provider_response(req, *, timeout, handlers=()):
    """Interrupt HTTP even before response headers/first-token metadata arrive."""
    generation = current_generation()
    if generation is None:
        return (request.build_opener(*handlers).open(req, timeout=timeout) if handlers
                else request.urlopen(req, timeout=timeout))
    check_cancelled()
    with ExitStack() as pending:
        def connected(connection):
            sock = connection.sock
            def shutdown():
                try: sock.shutdown(socket.SHUT_RDWR)
                except (OSError, ValueError): pass
                sock.close()
            pending.enter_context(generation.interrupt(shutdown))
        class HTTPConnection(http.client.HTTPConnection):
            def connect(self):
                super().connect()
                connected(self)
        class HTTPSConnection(http.client.HTTPSConnection):
            def connect(self):
                super().connect()
                connected(self)
        class HTTPHandler(request.HTTPHandler):
            def http_open(self, value):
                return self.do_open(HTTPConnection, value)
        class HTTPSHandler(request.HTTPSHandler):
            def https_open(self, value):
                return self.do_open(HTTPSConnection, value, context=self._context)
        try:
            response = request.build_opener(*handlers, HTTPHandler(), HTTPSHandler()).open(req, timeout=timeout)
            if generation.is_set():
                response.close()
            check_cancelled()
            return response
        except Exception:
            check_cancelled()
            raise


def cancel_generation(identifier: str):
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', identifier):
        raise ValueError('INVALID_GENERATION_ID')
    with _lock:
        now = time.monotonic()
        for key, deadline in list(_pending.items()):
            if deadline < now:
                _pending.pop(key, None)
        generation = _running.get(identifier)
        # The Stop POST can overtake the original HTTP request. Keep a short,
        # bounded tombstone so that request cannot start generation afterward.
        if generation is None:
            if len(_pending) >= 1024:
                _pending.pop(next(iter(_pending)))
            _pending[identifier] = now + 120
    if generation is not None:
        generation.cancel()


@contextmanager
def generation_scope(generation):
    token = _active.set(generation)
    try:
        check_cancelled()
        yield
    finally:
        _active.reset(token)


@contextmanager
def interrupt_response(response):
    """Shutdown the socket before close: close alone may wait on a blocked read."""
    generation = current_generation()
    if generation is None:
        yield response
        return
    def close():
        try:
            sock = response.fp.raw._sock
            sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError, ValueError):
            pass
        # Closing a buffered file can acquire its reader's lock. Do that off
        # the cancellation endpoint so Stop never waits on a provider read.
        def finish():
            try:
                response.close()
            except (OSError, ValueError):
                pass
        threading.Thread(target=finish, daemon=True).start()
    with generation.interrupt(close):
        try:
            yield response
        except Exception:
            check_cancelled(generation)
            raise


class GenerationControlMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get('headers', []))
        raw = headers.get(b'x-olcr-generation', b'').decode('ascii', 'ignore') or uuid.uuid4().hex
        if (scope['type'] != 'http' or scope.get('method') != 'POST'
                or scope.get('path') not in {'/api/chat', '/api/chat/stream', '/api/commands'}
                or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', raw)):
            return await self.app(scope, receive, send)
        generation = Generation()
        with _lock:
            deadline = _pending.pop(raw, 0)
            duplicate = raw in _running
            if not duplicate:
                _running[raw] = generation
        if duplicate:
            from starlette.responses import JSONResponse
            return await JSONResponse({'detail': 'GENERATION_ALREADY_RUNNING'}, status_code=409)(scope, receive, send)
        if deadline > time.monotonic():
            generation.cancel()
        started = False
        async def output(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
            await send(message)
        async def input_message():
            message = await receive()
            if message['type'] == 'http.disconnect':
                generation.cancel()
            return message
        try:
            with generation_scope(generation):
                await self.app(scope, input_message, output)
        except GenerationCancelled:
            if not started:
                from starlette.responses import JSONResponse
                await JSONResponse({'cancelled': True, 'response': ''})(scope, receive, send)
        finally:
            generation.cancel()
            with _lock:
                if _running.get(raw) is generation:
                    _running.pop(raw, None)
