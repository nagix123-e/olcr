from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from fastapi.testclient import TestClient

from olcr_api.generation_control import (Generation, GenerationCancelled, generation_scope,
    current_generation, cancel_generation, check_cancelled, GenerationControlMiddleware)
from olcr_api.ollama import OllamaProvider
from olcr_api.brain_providers import BrainProviders, call_json
from olcr_api.model_provider import RoleAwareModelProvider
from olcr_api.config import Settings
from olcr_api.coding_cancellation import coding_generation,cancel_coding_generation


class GenerationCancellationTests(unittest.TestCase):
    def test_failed_cleanup_cannot_prevent_other_cancellation_callbacks(self):
        from contextlib import ExitStack
        generation=Generation();closed=[]
        def broken():raise OSError('already closed')
        with self.assertRaises(GenerationCancelled), ExitStack() as stack:
            stack.enter_context(generation.interrupt(broken))
            stack.enter_context(generation.interrupt(lambda:closed.append(True)))
            generation.cancel()
            self.assertTrue(generation.is_set())
            self.assertEqual([True],closed)

    def test_stop_overtaking_request_prevents_execution(self):
        identifier = uuid.uuid4().hex
        cancel_generation(identifier)
        called = []
        async def action(request):
            called.append(True)
            return JSONResponse({'response': 'must not execute'})
        app = Starlette(routes=[Route('/api/chat', action, methods=['POST'])])
        app.add_middleware(GenerationControlMiddleware)
        response = TestClient(app).post('/api/chat', headers={'X-OLCR-Generation': identifier})
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()['cancelled'])
        self.assertEqual([], called)

    def test_context_and_cancellation_are_owned_by_one_request(self):
        first, other = Generation(), Generation()
        with generation_scope(other):
            first.cancel()
            check_cancelled()
            self.assertIs(other, current_generation())
        self.assertIsNone(current_generation())
        with self.assertRaises(GenerationCancelled), generation_scope(first):
            self.fail('cancelled scope entered')

    def test_http_cancellation_closes_connection_before_the_first_token(self):
        # The server sends HTTP headers, then deliberately sends no body. A
        # token-boundary-only implementation would wait here indefinitely.
        for provider, send_headers in ((kind, headers) for kind in ('ollama', 'chatgpt', 'json-api') for headers in (True, False)):
            with self.subTest(provider=provider, headers=send_headers):
                accepted, disconnected = threading.Event(), threading.Event()
                class StalledProvider(BaseHTTPRequestHandler):
                    def do_POST(inner):
                        inner.rfile.read(int(inner.headers.get('Content-Length', '0')))
                        if send_headers:
                            inner.send_response(200)
                            inner.send_header('Content-Type', 'text/event-stream')
                            inner.end_headers()
                            inner.wfile.flush()
                        accepted.set()
                        inner.connection.settimeout(3)
                        try:
                            if inner.connection.recv(1) == b'':
                                disconnected.set()
                        except OSError:
                            pass
                    def log_message(self, *args):
                        pass
                server = ThreadingHTTPServer(('127.0.0.1', 0), StalledProvider)
                server.daemon_threads = True
                serving = threading.Thread(target=server.serve_forever, daemon=True)
                serving.start()
                root = f'http://127.0.0.1:{server.server_port}'
                generation, failures = Generation(), []
                def consume():
                    try:
                        with generation_scope(generation),coding_generation('stalled-'+provider):
                            if provider == 'ollama':
                                list(OllamaProvider(root).generate([{'role': 'user', 'content': 'test'}], 'test', stream=True))
                            elif provider == 'chatgpt':
                                object.__new__(BrainProviders)._responses_stream(root, {}, 'fixture-token')
                            else:
                                call_json(root, {}, {})
                    except Exception as exc:
                        failures.append(exc)
                worker = threading.Thread(target=consume, daemon=True)
                try:
                    worker.start()
                    self.assertTrue(accepted.wait(2))
                    self.assertTrue(cancel_coding_generation('stalled-'+provider))
                    worker.join(1)
                    self.assertFalse(worker.is_alive(), 'Stop waited for another provider token')
                    self.assertEqual(1, len(failures), failures)
                    self.assertIsInstance(failures[0], GenerationCancelled)
                    self.assertTrue(disconnected.wait(1), 'provider connection remained open')
                finally:
                    generation.cancel()
                    server.shutdown()
                    server.server_close()
                    worker.join(3)

    def test_stop_terminates_only_the_request_owned_mlx_worker(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as directory:
            provider = RoleAwareModelProvider(Settings(db_path=str(Path(directory) / 'test.db')))
            generation, ready, failures, processes = Generation(), threading.Event(), [], []
            popen = subprocess.Popen
            def launch(*args, **kwargs):
                process = popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
                processes.append(process)
                ready.set()
                return process
            def execute():
                try:
                    with generation_scope(generation):
                        provider._run_mlx('ornith', {'messages': []})
                except Exception as exc:
                    failures.append(exc)
            with patch.object(provider, '_mlx_python', return_value=Path(sys.executable)), \
                 patch('olcr_api.model_provider.subprocess.Popen', side_effect=launch):
                worker = threading.Thread(target=execute, daemon=True)
                try:
                    worker.start()
                    self.assertTrue(ready.wait(2))
                    generation.cancel()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertIsInstance(failures[0], GenerationCancelled)
                    self.assertIsNotNone(processes[0].poll())
                finally:
                    generation.cancel()
                    worker.join(3)
