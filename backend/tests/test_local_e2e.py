import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from olcr_api.local_e2e import LocalE2EManager, load_project_e2e_spec, normalize_local_origin
from olcr_api.browser_network import _resolve_local_addresses, BrowserSessionManager
from olcr_api.node_mcp_runtime import managed_browser_command


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LocalE2ETests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="olcr-local-e2e-test-")
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        (self.root / ".olcr").mkdir()
        self.port = _free_port()
        self.origin = f"http://127.0.0.1:{self.port}"
        self.server = self.root / "test_server.py"
        self.server.write_text(
            "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            "import argparse\n"
            "p=argparse.ArgumentParser(); p.add_argument('--host'); p.add_argument('--port', type=int); a=p.parse_args()\n"
            "class H(BaseHTTPRequestHandler):\n"
            " def do_GET(self):\n"
            "  b=b'<button>Start game</button>'; self.send_response(200); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)\n"
            "HTTPServer((a.host,a.port),H).serve_forever()\n", encoding="utf-8")
        manifest = {"version": 1, "origin": self.origin,
                    "command": [sys.executable, str(self.server), "--host", "127.0.0.1",
                                "--port", str(self.port)]}
        (self.root / ".olcr" / "e2e.json").write_text(json.dumps(manifest), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_declared_loopback_project_command_starts_and_only_owned_process_stops(self):
        manager = LocalE2EManager(startup_timeout=5)
        run = manager.start(self.root, [self.temp.name], expected_origin=self.origin)
        try:
            self.assertEqual("READY", run["status"])
            self.assertTrue(run["owned_process"])
            self.assertIn(b"Start game", urlopen(self.origin, timeout=2).read())
            state = manager.get(run["id"])
            self.assertEqual("READY", state["status"])
        finally:
            stopped = manager.stop(run["id"])
        self.assertTrue(stopped["stopped"])
        self.assertEqual("STOPPED", manager.get(run["id"])["status"])

    def test_manifest_rejects_public_origins_and_non_loopback_commands(self):
        manifest_path = self.root / ".olcr" / "e2e.json"
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["origin"] = "https://example.test"
        manifest_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_ORIGIN_MUST_BE_LOOPBACK_HTTP"):
            load_project_e2e_spec(self.root, [self.temp.name])
        value["origin"] = self.origin
        value["command"][3] = "0.0.0.0"
        manifest_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_COMMAND_MUST_BIND_DECLARED_LOOPBACK_ORIGIN"):
            load_project_e2e_spec(self.root, [self.temp.name])

    def test_manifest_requires_workspace_inside_allowed_roots_and_matching_origin(self):
        elsewhere = Path(self.temp.name) / "elsewhere"
        elsewhere.mkdir()
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_WORKSPACE_OUT_OF_SCOPE"):
            load_project_e2e_spec(self.root, [str(elsewhere)])
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_ORIGIN_MISMATCH"):
            load_project_e2e_spec(self.root, [self.temp.name], expected_origin="http://localhost:4173")

    def test_vite_dev_script_is_resolved_without_a_manifest_and_bound_to_requested_origin(self):
        (self.root / ".olcr" / "e2e.json").unlink()
        (self.root / "package.json").write_text(json.dumps({
            "scripts": {"dev": "vite --open"},
            "devDependencies": {"vite": "*"},
        }), encoding="utf-8")
        with patch("olcr_api.local_e2e.shutil.which", return_value="/usr/bin/npm"):
            spec = load_project_e2e_spec(self.root, [self.temp.name],
                                         expected_origin="http://localhost:5173/game")
        self.assertEqual("package.json", spec["source"])
        self.assertEqual("dev", spec["script"])
        self.assertEqual("http://localhost:5173", spec["origin"])
        self.assertEqual("--host", spec["command"][-5])
        self.assertEqual("127.0.0.1", spec["command"][-4])
        self.assertEqual("--port", spec["command"][-3])
        self.assertEqual("5173", spec["command"][-2])
        self.assertEqual("--strictPort", spec["command"][-1])

    def test_explicit_chat_command_starts_without_a_manifest_and_is_bound_to_loopback(self):
        (self.root / ".olcr" / "e2e.json").unlink()
        command = (f'"{sys.executable}" "{self.server}" --host 127.0.0.1 --port {self.port}')
        spec = load_project_e2e_spec(self.root, [self.temp.name], requested_command=command)
        self.assertEqual("chat", spec["source"])
        self.assertEqual(self.origin, spec["origin"])
        self.assertEqual(sys.executable, spec["command"][0])

        manager = LocalE2EManager(startup_timeout=5)
        run = manager.start(self.root, [self.temp.name], requested_command=command)
        try:
            self.assertEqual("READY", run["status"])
            self.assertTrue(run["owned_process"])
            self.assertIn(b"Start game", urlopen(self.origin, timeout=2).read())
            self.assertNotIn("command", manager.get(run["id"]))
        finally:
            stopped = manager.stop(run["id"])
        self.assertTrue(stopped["stopped"])

    def test_explicit_chat_command_fails_closed_for_shells_bad_binds_and_origin_mismatch(self):
        shell_command = f"sh -c 'python3 server.py --host 127.0.0.1 --port {self.port}'"
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_COMMAND_SHELL_NOT_ALLOWED"):
            load_project_e2e_spec(self.root, [self.temp.name], requested_command=shell_command)
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_COMMAND_MUST_BE_A_SINGLE_COMMAND"):
            load_project_e2e_spec(self.root, [self.temp.name],
                                  requested_command=f"python3 server.py --host 127.0.0.1 --port {self.port}; echo ready")
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_COMMAND_MUST_DECLARE_LOOPBACK_HOST_AND_PORT"):
            load_project_e2e_spec(self.root, [self.temp.name],
                                  requested_command=f"python3 server.py --host 0.0.0.0 --port {self.port}")
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_ORIGIN_MUST_MATCH_COMMAND_PORT"):
            load_project_e2e_spec(self.root, [self.temp.name],
                                  expected_origin="http://127.0.0.1:4173",
                                  requested_command=f"python3 server.py --host 127.0.0.1 --port {self.port}")

    def test_package_script_resolution_requires_one_supported_server_candidate(self):
        (self.root / ".olcr" / "e2e.json").unlink()
        (self.root / "package.json").write_text(json.dumps({
            "scripts": {"dev": "vite", "start": "next dev"},
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_SCRIPT_AMBIGUOUS: dev, start"):
            load_project_e2e_spec(self.root, [self.temp.name])
        with patch("olcr_api.local_e2e.shutil.which", return_value="/usr/bin/npm"):
            selected = load_project_e2e_spec(self.root, [self.temp.name], requested_script="start")
        self.assertEqual("start", selected["script"])
        self.assertIn("--hostname", selected["command"])

    def test_unsupported_or_malformed_package_scripts_fail_closed(self):
        (self.root / ".olcr" / "e2e.json").unlink()
        (self.root / "package.json").write_text(json.dumps({
            "scripts": {"dev": "node ./scripts/dev.js"},
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_MANIFEST_NOT_FOUND"):
            load_project_e2e_spec(self.root, [self.temp.name])
        package = self.root / "package.json"
        package.write_text("{invalid", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_PACKAGE_CONFIG_INVALID"):
            load_project_e2e_spec(self.root, [self.temp.name])

    def test_package_script_explicit_wildcard_bind_is_rejected(self):
        (self.root / ".olcr" / "e2e.json").unlink()
        (self.root / "package.json").write_text(json.dumps({
            "scripts": {"dev": "vite --host 0.0.0.0"},
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_PACKAGE_SCRIPT_MUST_BIND_LOOPBACK"):
            load_project_e2e_spec(self.root, [self.temp.name])

    def test_package_script_shell_chains_are_rejected(self):
        (self.root / ".olcr" / "e2e.json").unlink()
        (self.root / "package.json").write_text(json.dumps({
            "scripts": {"dev": "vite && echo ready"},
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_PACKAGE_SCRIPT_MUST_BE_A_SINGLE_COMMAND"):
            load_project_e2e_spec(self.root, [self.temp.name])

    def test_start_never_adopts_or_stops_an_existing_user_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"user-owned")
            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        manager = LocalE2EManager(startup_timeout=2)
        try:
            with self.assertRaisesRegex(ValueError, "LOCAL_E2E_PORT_IN_USE"):
                manager.start(self.root, [self.temp.name], expected_origin=self.origin)
            attached = manager.attach(self.root, [self.temp.name], self.origin)
            self.assertFalse(attached["owned_process"])
            stopped = manager.stop(attached["id"])
            self.assertFalse(stopped["stopped"])
            self.assertEqual(b"user-owned", urlopen(self.origin, timeout=2).read())
        finally:
            manager.close_all()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_local_origin_normalization_and_destination_pinning(self):
        self.assertIsNone(normalize_local_origin("https://127.0.0.1:443"))
        self.assertIsNone(normalize_local_origin("http://192.168.1.5:3000"))
        self.assertIsNotNone(normalize_local_origin(self.origin))
        addresses = _resolve_local_addresses(self.origin + "/", self.origin)
        self.assertTrue(addresses)
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_ORIGIN_REJECTED"):
            _resolve_local_addresses("http://127.0.0.1:9/", self.origin)

    def test_real_managed_browser_opens_and_clicks_a_local_project_fixture(self):
        if not managed_browser_command():
            self.skipTest("OLCR bundled Playwright MCP runtime is unavailable")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = (b"<!doctype html><title>Local game</title><button onclick=\"this.textContent='Started'\">Start game</button>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        manager = BrowserSessionManager(managed_browser_command, maximum_sessions=1)
        session_id = None
        try:
            opened = manager.open_local(origin)
            self.assertEqual("READY", opened.get("status"), opened)
            session_id = opened["session_id"]
            page = manager.navigate(session_id, origin + "/")
            self.assertEqual("AVAILABLE", page.get("status"), page)
            self.assertIn("Start game", page.get("clean_markdown", ""))
            denied = manager.navigate(session_id, "http://127.0.0.1:9/")
            self.assertEqual("BLOCKED", denied.get("status"), denied)
            clicked = manager.click_local_control(session_id, "Start game")
            self.assertEqual("AVAILABLE", clicked.get("status"), clicked)
            snapshot = manager.snapshot(session_id)
            self.assertIn("Started", snapshot.get("clean_markdown", ""))
        finally:
            if session_id:
                manager.close(session_id)
            manager.close_all()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
