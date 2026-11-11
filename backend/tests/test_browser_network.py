import http.client
import base64
import json
import socket
import socketserver
import threading
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from olcr_api.browser_network import (ManagedBrowserSession, SafeBrowserEgressProxy,
                                     BrowserSessionManager, playwright_egress_command)
from olcr_api.model_telemetry import model_telemetry
from olcr_api.node_mcp_runtime import managed_browser_command


class BrowserEgressProxyTests(unittest.TestCase):
    def test_travel_browser_uses_fixed_scanner_and_fresh_typed_control_actions(self):
        from types import SimpleNamespace
        class Runtime:
            state="READY"
            def __init__(self):self.calls=[]
            def call(self,tool,arguments):
                self.calls.append((tool,arguments))
                if tool!="browser_evaluate":raise AssertionError(tool)
                if "const cfg={" in arguments["function"]:
                    value={"ok":True}
                else:
                    value={"url":"https://rail.example/search","origin":"https://rail.example",
                        "document_id":1234,"requires_user_action":False,
                        "controls":[{"selector":"input:nth-of-type(1)","label":"出発日","tag":"input","type":"date","options":[]}],
                        "terms":["¥12,000"],"receipt":None}
                return {"status":"AVAILABLE","response":{"result":{"content":[{"type":"text","text":json.dumps(value)}]}}}
            def close(self):pass
        session=ManagedBrowserSession(["/runtime/node","/runtime/node_modules/@playwright/mcp/cli.js"])
        session.runtime=Runtime()
        try:
            evidence=session.inspect_travel_page()
            self.assertEqual("AVAILABLE",evidence["status"])
            self.assertEqual("¥12,000",evidence["terms"][0])
            identity={"url":evidence["url"],"origin":evidence["origin"],"document_id":evidence["document_id"]}
            result=session.apply_travel_action(identity,evidence["controls"][0],"fill","2026-12-12")
            self.assertEqual("AVAILABLE",result["status"])
            self.assertIn("new Event('input'",session.runtime.calls[-1][1]["function"])
            self.assertNotIn("browser_evaluate",session.public_tools)
        finally:session.close()

    def test_managed_browser_screenshot_returns_only_bounded_png_or_jpeg_content(self):
        class Runtime:
            state = "READY"
            def __init__(self, image): self.image = image; self.arguments = None
            def call(self, tool, arguments):
                self.asserted_tool = tool
                self.arguments = arguments
                return {"status": "AVAILABLE", "response": {"result": {"content": [self.image]}}}
            def close(self): pass

        session = ManagedBrowserSession(["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js"])
        image = b"\xff\xd8\xffframe"
        runtime = Runtime({"type": "image", "mimeType": "image/jpeg",
                           "data": base64.b64encode(image).decode("ascii")})
        session.runtime = runtime
        try:
            result = session.screenshot()
            self.assertEqual("AVAILABLE", result["status"])
            self.assertEqual("image/jpeg", result["mime_type"])
            self.assertEqual(base64.b64encode(image).decode("ascii"), result["data"])
            self.assertEqual("browser_take_screenshot", runtime.asserted_tool)
            self.assertEqual({"type": "jpeg", "scale": "css",
                              "filename": "chat-browser-viewport.jpg"}, runtime.arguments)
            runtime.image = {"type": "image", "mimeType": "image/svg+xml", "data": "PHN2Zy8+"}
            self.assertEqual("BROWSER_SCREENSHOT_FORMAT_UNSUPPORTED", session.screenshot()["error"])
            runtime.image = {"type": "image", "mimeType": "image/png", "data": "not-base64"}
            self.assertEqual("BROWSER_SCREENSHOT_DATA_INVALID", session.screenshot()["error"])
            session._temporary_directory = tempfile.TemporaryDirectory(prefix="screenshot-fallback-")
            Path(session._temporary_directory.name, "chat-browser-viewport.jpg").write_bytes(
                b"\xff\xd8\xffsaved-to-output-dir")
            runtime.image = {"type": "text", "text": "Screenshot saved"}
            saved = session.screenshot()
            self.assertEqual("AVAILABLE", saved["status"])
            self.assertEqual("image/jpeg", saved["mime_type"])
        finally:
            session.close()

    def test_managed_playwright_command_forces_policy_proxy_and_blocks_service_workers(self):
        command=playwright_egress_command(["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js"],
                                          "http://127.0.0.1:12345", "/tmp/session/policy.js", "/tmp/session")
        self.assertIn("--isolated", command)
        self.assertIn("--headless", command)
        self.assertEqual(command[-9:], ["--proxy-server", "http://127.0.0.1:12345",
                                       "--proxy-bypass", "<-loopback>",
                                       "--block-service-workers", "--output-dir", "/tmp/session",
                                       "--init-script", "/tmp/session/policy.js"])
        persistent = playwright_egress_command(
            ["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js", "--isolated"],
            "http://127.0.0.1:12345", "/tmp/session/policy.js", "/tmp/session",
            user_data_dir="/tmp/profiles/demo")
        self.assertNotIn("--isolated", persistent)
        self.assertIn("--headless", persistent)
        self.assertIn("/tmp/profiles/demo", persistent)
        headed = playwright_egress_command(
            ["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js", "--isolated", "--headless"],
            "http://127.0.0.1:12345", "/tmp/session/policy.js", "/tmp/session",
            user_data_dir="/tmp/profiles/demo", interactive_login=True)
        self.assertNotIn("--isolated", headed)
        self.assertNotIn("--headless", headed)
        self.assertIn("/tmp/profiles/demo", headed)
        with self.assertRaisesRegex(ValueError, "BUNDLED_PLAYWRIGHT_MCP_REQUIRED"):
            playwright_egress_command(["npx", "playwright-mcp"], "http://127.0.0.1:12345",
                                      "/tmp/session/policy.js", "/tmp/session")

    def test_managed_session_fails_closed_when_command_is_not_bundled(self):
        session=ManagedBrowserSession(["npx", "playwright-mcp"])
        try:
            self.assertEqual("FAILED", session.start()["status"])
        finally:
            session.close()
        self.assertIsNone(session._init_script_path)
        self.assertIsNone(session._temporary_directory)

    def test_managed_session_rejects_mutating_browser_tools(self):
        with self.assertRaisesRegex(ValueError, "UNSAFE_BROWSER_TOOL_ALLOWLIST"):
            ManagedBrowserSession(["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js"],
                                  allowed_tools=("browser_evaluate",))

    def test_picker_parses_playwright_markdown_wrapped_json_result(self):
        class Runtime:
            state = "READY"
            def call(self, _tool, _arguments):
                return {"status":"AVAILABLE", "response":{"result":{"content":[{
                    "type":"text", "text":"### Result\n\"{\\\"candidates\\\":[{\\\"kind\\\":\\\"row\\\",\\\"text\\\":\\\"Example\\\"}],\\\"truncated\\\":false}\"\n### Ran Playwright code\n```js\n// fixed script\n```"
                }]}}}
            def close(self): pass
        session=ManagedBrowserSession(["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js"])
        session.runtime=Runtime()
        try:
            evidence=session.picker_evidence()
            self.assertEqual("AVAILABLE",evidence["status"])
            self.assertEqual("Example",evidence["candidates"][0]["text"])
        finally:
            session.close()

    def test_recipe_selector_recovery_is_bounded_read_only_and_returns_match_counts(self):
        class Runtime:
            state = "READY"
            def __init__(self): self.calls = []
            def call(self, tool, arguments):
                self.calls.append((tool, arguments))
                return {"status":"AVAILABLE", "response":{"result":{"content":[{
                    "type":"text", "text":"{\"results\":[{\"candidate_id\":\"r0\",\"selector\":\"table tbody tr\",\"record_count\":4,\"field_match_counts\":{\"name\":4}}]}"
                }]}}}
            def close(self): pass
        session = ManagedBrowserSession(["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js"])
        session.runtime = Runtime()
        try:
            result = session.probe_recipe_selectors(
                [{"candidate_id":"r0", "selector":"table tbody tr"}],
                {"name":{"selector":"td.name", "extract":"text"}})
            self.assertEqual("AVAILABLE", result["status"])
            self.assertEqual(4, result["results"][0]["record_count"])
            tool, arguments = session.runtime.calls[0]
            self.assertEqual("browser_evaluate", tool)
            self.assertIn("querySelectorAll", arguments["function"])
            self.assertNotIn(".click(", arguments["function"])
            self.assertNotIn("fetch(", arguments["function"])
            rejected = session.probe_recipe_selectors(
                [{"candidate_id":"r0", "selector":"tr};fetch('https://invalid.test')"}], {})
            self.assertEqual("BLOCKED", rejected["status"])
            self.assertEqual(1, len(session.runtime.calls))
        finally:
            session.close()

    def test_page_load_wait_uses_a_fixed_read_only_two_second_script(self):
        class Runtime:
            state = "READY"
            def __init__(self): self.calls = []
            def call(self, tool, arguments):
                self.calls.append((tool, arguments))
                return {"status":"AVAILABLE", "response":{"result":{"content":[{
                    "type":"text", "text":"{\"ready\":true}"
                }]}}}
            def close(self): pass
        session = ManagedBrowserSession(["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js"])
        session.runtime = Runtime()
        try:
            self.assertEqual({"status":"AVAILABLE", "ready":True}, session.wait_for_page_load())
            tool, arguments = session.runtime.calls[0]
            self.assertEqual("browser_evaluate", tool)
            self.assertIn("document.readyState", arguments["function"])
            self.assertIn("setTimeout(() => resolve(false), 2000)", arguments["function"])
            self.assertNotIn(".click(", arguments["function"])
            self.assertNotIn("fetch(", arguments["function"])
        finally:
            session.close()

    def test_recipe_next_link_uses_a_fixed_read_only_anchor_inspection(self):
        class Runtime:
            state = "READY"
            def __init__(self): self.arguments = None
            def call(self, tool, arguments):
                self.arguments = (tool, arguments)
                return {"status":"AVAILABLE", "response":{"result":{"content":[{
                    "type":"text", "text":"{\"url\":\"https://example.test/page/2\"}"
                }]}}}
            def close(self): pass
        session = ManagedBrowserSession(["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js"])
        session.runtime = Runtime()
        try:
            result = session.extract_next_link("a.next")
            self.assertEqual("AVAILABLE", result["status"])
            self.assertEqual("https://example.test/page/2", result["url"])
            tool, arguments = session.runtime.arguments
            self.assertEqual("browser_evaluate", tool)
            self.assertIn("document.querySelector(cfg.selector)", arguments["function"])
            self.assertIn("tagName.toLowerCase() !==", arguments["function"])
            self.assertNotIn(".click(", arguments["function"])
        finally:
            session.close()

    def test_network_observer_returns_only_bounded_metadata_and_removes_query_values(self):
        class Runtime:
            state = "READY"
            allowed = {"browser_navigate", "browser_snapshot", "browser_network_requests"}
            def __init__(self): self.calls=[]
            def call(self, tool, arguments):
                self.calls.append((tool, arguments))
                return {"status":"AVAILABLE", "response":{"result":{"content":[{
                    "type":"text", "text":"### Network\n2. [GET] https://example.test/api/items?token=secret => [200] OK\n3. [POST] https://example.test/api/search#frag => [204] No Content"
                }]}}}
            def close(self): pass
        runtime=Runtime()
        session=ManagedBrowserSession(["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js"])
        session.runtime=runtime
        try:
            self.assertNotIn("browser_network_requests", session.public_tools)
            evidence=session.network_requests()
            self.assertEqual("AVAILABLE", evidence["status"])
            self.assertEqual("https://example.test/api/items", evidence["requests"][0]["url"])
            self.assertNotIn("secret", str(evidence))
            self.assertEqual(("browser_network_requests", {"static":False}), runtime.calls[0])
        finally:
            session.close()

    def test_network_json_evidence_requires_explicit_same_origin_json_get_and_redacts_secrets(self):
        class Runtime:
            state = "READY"
            def __init__(self, *, method="GET", status=200, content_type="application/json", length="57", body='{"data":[{"name":"Ada","token":"secret"}]}'):
                self.calls=[]
                self.method=method; self.status=status; self.content_type=content_type; self.length=length; self.body=body
            def call(self, tool, arguments):
                self.calls.append((tool, arguments))
                if tool == "browser_network_requests":
                    line=f"2. [{self.method}] https://example.test/api/items?session=private => [{self.status}] OK"
                elif tool == "browser_network_request" and arguments.get("part") == "response-headers":
                    line=f"content-type: {self.content_type}\ncontent-length: {self.length}"
                elif tool == "browser_network_request" and arguments.get("part") == "response-body":
                    line=self.body
                else:
                    raise AssertionError((tool, arguments))
                return {"status":"AVAILABLE", "response":{"result":{"content":[{"type":"text","text":line}]}}}
            def close(self): pass

        runtime=Runtime()
        session=ManagedBrowserSession(["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js"])
        session.runtime=runtime
        try:
            evidence=session.network_json_evidence(2, "https://example.test/list")
            self.assertEqual("AVAILABLE", evidence["status"])
            self.assertEqual("https://example.test/api/items", evidence["url"])
            self.assertEqual("application/json", evidence["content_type"])
            self.assertEqual("Ada", evidence["preview"]["data"][0]["name"])
            self.assertEqual("[redacted]", evidence["preview"]["data"][0]["token"])
            self.assertFalse(evidence["persisted"])
            self.assertNotIn("private", str(evidence))
            self.assertEqual(["browser_network_requests", "browser_network_request", "browser_network_request"],
                             [tool for tool, _ in runtime.calls])
            self.assertEqual({"index":2,"part":"response-body"}, runtime.calls[-1][1])
        finally:
            session.close()

    def test_network_json_evidence_rejects_cross_origin_non_json_oversize_and_invalid_body(self):
        cases = [
            ({}, "https://other.test/list", "NETWORK_JSON_CROSS_ORIGIN"),
            ({"method":"POST"}, "https://example.test/list", "NETWORK_JSON_GET_REQUIRED"),
            ({"status":404}, "https://example.test/list", "NETWORK_JSON_GET_REQUIRED"),
            ({"content_type":"text/html"}, "https://example.test/list", "NETWORK_RESPONSE_NOT_JSON"),
            ({"length":""}, "https://example.test/list", "NETWORK_RESPONSE_LENGTH_UNKNOWN"),
            ({"length":"65537"}, "https://example.test/list", "NETWORK_JSON_TOO_LARGE"),
            ({"body":"not-json"}, "https://example.test/list", "NETWORK_RESPONSE_JSON_INVALID"),
            ({"body":"x" * (64 * 1024 + 1), "length":"10"}, "https://example.test/list", "NETWORK_JSON_TOO_LARGE"),
        ]
        for options, current_url, error in cases:
            with self.subTest(error=error):
                class Runtime:
                    state = "READY"
                    def __init__(self): self.calls=[]
                    def call(self, tool, arguments):
                        self.calls.append((tool, arguments))
                        if tool == "browser_network_requests":
                            values={"method":"GET","status":200,**options}
                            line=f"2. [{values['method']}] https://example.test/api/items => [{values['status']}] OK"
                        elif arguments.get("part") == "response-headers":
                            values={"content_type":"application/json","length":"10",**options}
                            line=f"content-type: {values['content_type']}\ncontent-length: {values['length']}"
                        else:
                            line=options.get("body", '{"ok":true}')
                        return {"status":"AVAILABLE","response":{"result":{"content":[{"type":"text","text":line}]}}}
                    def close(self): pass
                runtime=Runtime()
                session=ManagedBrowserSession(["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js"])
                session.runtime=runtime
                try:
                    result=session.network_json_evidence(2, current_url)
                    self.assertEqual("BLOCKED", result["status"])
                    self.assertEqual(error, result["error"])
                    if error in {"NETWORK_JSON_CROSS_ORIGIN", "NETWORK_JSON_GET_REQUIRED"}:
                        self.assertEqual(["browser_network_requests"], [tool for tool, _ in runtime.calls])
                    if error in {"NETWORK_RESPONSE_NOT_JSON", "NETWORK_RESPONSE_LENGTH_UNKNOWN", "NETWORK_JSON_TOO_LARGE"} and not options.get("body"):
                        self.assertNotIn(("browser_network_request", {"index":2,"part":"response-body"}), runtime.calls)
                finally:
                    session.close()

    def test_network_json_recipe_rows_reuse_observed_playwright_body_and_redact_sensitive_fields(self):
        body = '{"data":{"items":[{"id":1,"name":"Ada","api_token":"secret"},{"id":2,"name":"Lin"}]}}'
        class Runtime:
            state = "READY"
            def __init__(self): self.calls=[]
            def call(self, tool, arguments):
                self.calls.append((tool, arguments))
                if tool == "browser_network_requests":
                    text = "2. [GET] https://example.test/api/items?page=1 => [200] OK"
                elif arguments.get("part") == "response-headers":
                    text = f"content-type: application/json\ncontent-length: {len(body.encode())}"
                elif arguments.get("part") == "response-body":
                    text = body
                else:
                    raise AssertionError((tool, arguments))
                return {"status":"AVAILABLE", "response":{"result":{"content":[{"type":"text","text":text}]}}}
            def close(self): pass

        runtime = Runtime()
        session = ManagedBrowserSession(["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js"])
        session.runtime = runtime
        try:
            result = session.network_json_recipe_rows(
                "https://example.test/list", "/api/items", "/data/items",
                {"name":"/name", "token":"/api_token"}, limit=5,
            )
            self.assertEqual("AVAILABLE", result["status"])
            self.assertEqual(2, result["record_count"])
            self.assertEqual([{"name":"Ada", "token":"[redacted]"},
                              {"name":"Lin", "token":None}], result["rows"])
            self.assertEqual({"name":2, "token":1}, result["field_match_counts"])
            self.assertFalse(result["persisted"])
            self.assertNotIn("secret", str(result))
            self.assertEqual(2, sum(tool == "browser_network_requests" for tool, _ in runtime.calls))
            self.assertEqual({"index":2, "part":"response-body"}, runtime.calls[-1][1])
        finally:
            session.close()

    def test_managed_playwright_captures_local_json_response_body_for_recipe(self):
        """Exercise real bundled Chromium/MCP network-body retrieval without contacting a public site."""
        rows = [
            {"id": 1, "name": "Ada", "api_token": "fixture-secret-1"},
            {"id": 2, "name": "Lin", "api_token": "fixture-secret-2"},
        ]
        html = b'''<!doctype html><html><body><main><h1>JSON fixture</h1>
        <pre id="loaded">pending</pre></main><script>
        fetch('/api/items?page=1').then(response => response.json()).then(payload => {
          document.querySelector('#loaded').textContent = String(payload.items.length);
        });</script></body></html>'''

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/api/items"):
                    body = json.dumps({"items": rows}, separators=(",", ":")).encode()
                    content_type = "application/json; charset=utf-8"
                else:
                    body = html
                    content_type = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        address = ("127.0.0.1", server.server_address[1])
        manager = BrowserSessionManager(managed_browser_command, maximum_sessions=1)
        session_id = None
        try:
            if not managed_browser_command():
                self.skipTest("OLCR bundled Playwright MCP runtime is unavailable")

            def resolve_fixture_only(url):
                if urlsplit(url).hostname == "fixture.example":
                    return (address,)
                raise ValueError("FIXTURE_EGRESS_ONLY")

            with patch("olcr_api.browser_network._resolve_public_addresses", side_effect=resolve_fixture_only):
                opened = manager.open()
                self.assertEqual("READY", opened.get("status"), opened)
                session_id = opened["session_id"]
                current_url = f"http://fixture.example:{address[1]}/"
                navigation = manager.navigate(session_id, current_url)
                self.assertEqual("AVAILABLE", navigation.get("status"), navigation)

                observed = None
                for _ in range(30):
                    network = manager.network_requests(session_id)
                    observed = next((request for request in network.get("requests", [])
                                     if request.get("url", "").endswith("/api/items")
                                     and request.get("status_code") == 200), None)
                    if observed:
                        break
                    time.sleep(0.1)
                self.assertIsNotNone(observed, network)
                self.assertEqual("GET", observed["method"])
                self.assertEqual(f"http://fixture.example:{address[1]}/api/items", observed["url"])

                evidence = manager.network_json_evidence(session_id, observed["index"])
                self.assertEqual("AVAILABLE", evidence.get("status"), evidence)
                self.assertEqual(200, evidence["status_code"])
                self.assertEqual("application/json", evidence["content_type"])
                self.assertFalse(evidence["persisted"])
                self.assertEqual("[redacted]", evidence["preview"]["items"][0]["api_token"])
                self.assertNotIn("fixture-secret", json.dumps(evidence))

                recipe = manager.extract_network_json_recipe(session_id, {
                    "endpoint_path": "/api/items",
                    "records_pointer": "/items",
                    "fields": {
                        "id": {"pointer": "/id"},
                        "name": {"pointer": "/name"},
                        "token": {"pointer": "/api_token"},
                    },
                }, limit=5)
                self.assertEqual("AVAILABLE", recipe.get("status"), recipe)
                self.assertEqual([{"id": 1, "name": "Ada", "token": "[redacted]"},
                                  {"id": 2, "name": "Lin", "token": "[redacted]"}], recipe["rows"])
                self.assertEqual(2, recipe["record_count"])
                self.assertFalse(recipe["persisted"])
                self.assertNotIn("fixture-secret", json.dumps(recipe))
        finally:
            if session_id:
                manager.close(session_id)
            manager.close_all()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_managed_playwright_pins_redirect_and_subresource_hosts_through_the_proxy(self):
        """A redirect and its subresource must each resolve through the managed egress proxy."""
        requests = []
        pixel = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
                 b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.headers.get("Host", ""), self.path))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", f"http://landing.fixture.example:{server.server_port}/page")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/pixel.gif":
                    body, content_type = pixel, "image/gif"
                elif self.path == "/page":
                    body = (f'<!doctype html><title>Redirect fixture</title><img '
                            f'src="http://asset.fixture.example:{server.server_port}/pixel.gif">').encode()
                    content_type = "text/html; charset=utf-8"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        address = ("127.0.0.1", server.server_port)
        allowed_hosts = {"start.fixture.example", "landing.fixture.example", "asset.fixture.example"}
        resolved_hosts = []
        manager = BrowserSessionManager(managed_browser_command, maximum_sessions=1)
        session_id = None
        try:
            if not managed_browser_command():
                self.skipTest("OLCR bundled Playwright MCP runtime is unavailable")

            def resolve_fixture_only(url):
                host = urlsplit(url).hostname
                resolved_hosts.append(host)
                if host not in allowed_hosts:
                    raise ValueError("FIXTURE_EGRESS_ONLY")
                return (address,)

            with patch("olcr_api.browser_network._resolve_public_addresses", side_effect=resolve_fixture_only):
                opened = manager.open()
                self.assertEqual("READY", opened.get("status"), opened)
                session_id = opened["session_id"]
                start_url = f"http://start.fixture.example:{server.server_port}/redirect"
                navigation = manager.navigate(session_id, start_url)
                self.assertEqual("AVAILABLE", navigation.get("status"), navigation)
                self.assertEqual(f"http://landing.fixture.example:{server.server_port}/page", navigation["url"])

                for _ in range(30):
                    if any(host == f"asset.fixture.example:{server.server_port}" and path == "/pixel.gif"
                           for host, path in requests):
                        break
                    time.sleep(0.1)

                self.assertIn((f"start.fixture.example:{server.server_port}", "/redirect"), requests)
                self.assertIn((f"landing.fixture.example:{server.server_port}", "/page"), requests)
                self.assertIn((f"asset.fixture.example:{server.server_port}", "/pixel.gif"), requests)
                self.assertTrue(allowed_hosts.issubset(set(resolved_hosts)), resolved_hosts)
                self.assertTrue(all(host in allowed_hosts for host in resolved_hosts), resolved_hosts)
        finally:
            if session_id:
                manager.close(session_id)
            manager.close_all()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_recipe_load_more_uses_bounded_button_preflight_before_click(self):
        class Runtime:
            state = "READY"
            def __init__(self, evidence): self.evidence=evidence; self.calls=[]
            def call(self, tool, arguments):
                self.calls.append((tool, arguments))
                text = self.evidence if tool == "browser_evaluate" else "- Page URL: https://example.test/list\n- Page Title: Directory"
                return {"status":"AVAILABLE", "response":{"result":{"content":[{"type":"text","text":text}]}}}
            def close(self): pass
        runtime=Runtime('{"ok":true}')
        session=ManagedBrowserSession(["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js"])
        session.runtime=runtime
        try:
            result=session.click_recipe_load_more("button.more", "Load more")
            self.assertEqual("AVAILABLE",result["status"])
            self.assertEqual(["browser_evaluate","browser_click"],[call[0] for call in runtime.calls])
            self.assertEqual("button.more",runtime.calls[1][1]["target"])
            self.assertIn("SELECTOR_NOT_UNIQUE_OR_VISIBLE",runtime.calls[0][1]["function"])
            self.assertIn('"label":"Load more"',runtime.calls[0][1]["function"])
        finally:
            session.close()

    def test_recipe_load_more_refuses_non_button_before_click(self):
        class Runtime:
            state="READY"
            def __init__(self): self.calls=[]
            def call(self, tool, _arguments):
                self.calls.append(tool)
                return {"status":"AVAILABLE", "response":{"result":{"content":[{"type":"text","text":"{\"ok\":false,\"reason\":\"LOAD_MORE_TARGET_NOT_ENABLED_BUTTON\"}"}]}}}
            def close(self): pass
        runtime=Runtime()
        session=ManagedBrowserSession(["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js"])
        session.runtime=runtime
        try:
            result=session.click_recipe_load_more("button.more")
            self.assertEqual("FAILED",result["status"])
            self.assertEqual(["browser_evaluate"],runtime.calls)
        finally:
            session.close()

    def test_browser_navigation_rejects_non_public_web_schemes_before_mcp(self):
        class Runtime:
            state="READY"
            def call(self, *_args): raise AssertionError("blocked navigation reached MCP")
            def close(self): pass
        session=ManagedBrowserSession(["/runtime/node", "/runtime/node_modules/@playwright/mcp/cli.js"])
        session.runtime=Runtime()
        try:
            self.assertEqual("BLOCKED",session.call("browser_navigate",{"url":"file:///etc/passwd"})["status"])
            self.assertEqual("BLOCKED",session.call("browser_navigate",{"url":"http://127.0.0.1/"})["status"])
            self.assertEqual("BLOCKED",session.call("browser_evaluate",{"function":"() => fetch('https://attacker.test')"})["status"])
        finally:
            session.close()

    def test_http_requests_are_forwarded_through_the_pinned_address(self):
        class Origin(BaseHTTPRequestHandler):
            def do_GET(self):
                body=(self.path+"|"+self.headers.get("Host","")).encode()
                self.send_response(200); self.send_header("Content-Length",str(len(body))); self.end_headers()
                self.wfile.write(body)
            def log_message(self,*_args):pass
        origin=ThreadingHTTPServer(("127.0.0.1",0),Origin)
        origin_thread=threading.Thread(target=origin.serve_forever,daemon=True); origin_thread.start()
        origin_port=origin.server_address[1]
        proxy=SafeBrowserEgressProxy(); proxy_url=proxy.start()
        proxy_port=int(proxy_url.rsplit(":",1)[1])
        try:
            with patch("olcr_api.browser_network._resolve_public_addresses",
                       return_value=(("127.0.0.1",origin_port),)):
                connection=http.client.HTTPConnection("127.0.0.1",proxy_port,timeout=2)
                connection.request("GET",f"http://public.example:{origin_port}/items?q=1")
                response=connection.getresponse()
                self.assertEqual(200,response.status)
                self.assertEqual(f"/items?q=1|public.example:{origin_port}",response.read().decode())
                connection.close()
        finally:
            proxy.close(); origin.shutdown(); origin.server_close(); origin_thread.join(timeout=2)

    def test_https_connect_tunnels_through_the_pinned_address(self):
        received=[]
        class Echo(socketserver.BaseRequestHandler):
            def handle(self):
                received.append(self.request.recv(4))
                self.request.sendall(b"pong")
        origin=socketserver.ThreadingTCPServer(("127.0.0.1",0),Echo)
        origin_thread=threading.Thread(target=origin.serve_forever,daemon=True); origin_thread.start()
        origin_port=origin.server_address[1]
        proxy=SafeBrowserEgressProxy(); proxy_url=proxy.start()
        proxy_port=int(proxy_url.rsplit(":",1)[1])
        try:
            with patch("olcr_api.browser_network._resolve_public_addresses",
                       return_value=(("127.0.0.1",origin_port),)):
                client=socket.create_connection(("127.0.0.1",proxy_port),timeout=2)
                client.sendall(f"CONNECT public.example:{origin_port} HTTP/1.1\r\nHost: public.example:{origin_port}\r\n\r\n".encode())
                response=b""
                while b"\r\n\r\n" not in response:
                    response+=client.recv(4096)
                self.assertIn(b"200 Connection Established",response)
                client.sendall(b"ping")
                self.assertEqual(b"pong",client.recv(4))
                client.close()
            self.assertEqual([b"ping"],received)
        finally:
            proxy.close(); origin.shutdown(); origin.server_close(); origin_thread.join(timeout=2)

    def test_rejected_dns_answer_never_reaches_an_origin(self):
        class Origin(BaseHTTPRequestHandler):
            requests=0
            def do_GET(self):
                type(self).requests+=1
                self.send_response(200); self.end_headers()
            def log_message(self,*_args):pass
        origin=ThreadingHTTPServer(("127.0.0.1",0),Origin)
        origin_thread=threading.Thread(target=origin.serve_forever,daemon=True); origin_thread.start()
        proxy=SafeBrowserEgressProxy(); proxy_url=proxy.start()
        proxy_port=int(proxy_url.rsplit(":",1)[1])
        try:
            with patch("olcr_api.browser_network._resolve_public_addresses",side_effect=ValueError("PRIVATE_ADDRESS_REJECTED")):
                connection=http.client.HTTPConnection("127.0.0.1",proxy_port,timeout=2)
                connection.request("GET","http://metadata.example/latest")
                response=connection.getresponse()
                self.assertEqual(502,response.status)
                response.read(); connection.close()
            self.assertEqual(0,Origin.requests)
        finally:
            proxy.close(); origin.shutdown(); origin.server_close(); origin_thread.join(timeout=2)

    def test_ambiguous_request_body_framing_is_rejected_before_forwarding(self):
        class Origin(BaseHTTPRequestHandler):
            requests=0
            def do_POST(self):
                type(self).requests+=1
                self.send_response(200); self.end_headers()
            def log_message(self,*_args):pass
        origin=ThreadingHTTPServer(("127.0.0.1",0),Origin)
        origin_thread=threading.Thread(target=origin.serve_forever,daemon=True); origin_thread.start()
        proxy=SafeBrowserEgressProxy(); proxy_url=proxy.start()
        proxy_port=int(proxy_url.rsplit(":",1)[1]); target=f"http://public.example:{origin.server_address[1]}/"
        try:
            with patch("olcr_api.browser_network._resolve_public_addresses",
                       return_value=(("127.0.0.1",origin.server_address[1]),)):
                for framing, expected in ((b"Transfer-Encoding: chunked\r\n", b"501"),
                                          (b"Content-Length: 0\r\nContent-Length: 1\r\n", b"400")):
                    connection=socket.create_connection(("127.0.0.1",proxy_port),timeout=2)
                    request=(f"POST {target} HTTP/1.1\r\nHost: public.example\r\n".encode()
                             + framing + b"\r\n")
                    connection.sendall(request)
                    response=connection.recv(4096)
                    self.assertIn(expected,response)
                    connection.close()
            self.assertEqual(0,Origin.requests)
        finally:
            proxy.close(); origin.shutdown(); origin.server_close(); origin_thread.join(timeout=2)

    def test_browser_session_manager_exposes_bounded_navigation_and_closes_process(self):
        class Session:
            def __init__(self, command, user_data_dir=None, interactive_login=False):
                self.closed=False; self.user_data_dir=user_data_dir; self.interactive_login=interactive_login
            def start(self): return {"status":"READY","tools":["browser_navigate","browser_snapshot","browser_navigate_back"]}
            def call(self, tool, arguments):
                if tool == "browser_navigate":
                    self.current = arguments["url"]
                elif tool == "browser_navigate_back":
                    self.current = "https://example.test/page"
                text=(f"- Page URL: {self.current}\n- Page Title: Example" if tool in {"browser_navigate", "browser_navigate_back"}
                      else "- heading \"Example\"\n- paragraph: content")
                return {"status":"AVAILABLE","response":{"result":{"content":[{"type":"text","text":text}]}}}
            def click_recipe_load_more(self, _selector):
                return {"status":"AVAILABLE","page":"- Page URL: https://other.example.test/list"}
            def scroll_for_recipe_page(self):
                return {"status":"AVAILABLE","page":"- Page URL: https://example.test/page"}
            def picker_evidence(self): return {"status":"AVAILABLE","candidates":[{"kind":"row","text":"A row"}]}
            def screenshot(self):
                return {"status":"AVAILABLE", "mime_type":"image/png", "data":"cG5n", "timestamp":1.0}
            def close(self): self.closed=True
        manager=BrowserSessionManager(lambda:["unused"],maximum_sessions=1,session_factory=Session)
        counters_before=model_telemetry.snapshot()["counters"]
        try:
            opened=manager.open(); session_id=opened["session_id"]
            self.assertEqual("READY",opened["status"])
            self.assertEqual("LIMIT_REACHED",manager.open()["status"])
            page=manager.navigate(session_id,"https://example.test/page")
            self.assertEqual("AVAILABLE",page["status"])
            self.assertEqual("Example",page["title"])
            self.assertIn("paragraph",page["clean_markdown"])
            frame = manager.screenshot(session_id)
            self.assertEqual("AVAILABLE", frame["status"])
            self.assertEqual("https://example.test/page", frame["url"])
            self.assertEqual("image/png", frame["mime_type"])
            reloaded=manager.reload(session_id)
            self.assertEqual("AVAILABLE",reloaded["status"])
            self.assertEqual("https://example.test/page",reloaded["url"])
            self.assertEqual("https://example.test/page",manager.list()[0]["url"])
            self.assertEqual("BLOCKED",manager.click_recipe_load_more(session_id,"button.more")["status"])
            self.assertEqual("AVAILABLE",manager.scroll_for_recipe_page(session_id)["status"])
            back=manager.navigate_back(session_id)
            self.assertEqual("AVAILABLE",back["status"])
            self.assertEqual("https://example.test/page",back["url"])
            self.assertIn("paragraph",back["clean_markdown"])
            self.assertEqual("A row",manager.picker_evidence(session_id,"row")["candidates"][0]["text"])
            self.assertEqual("AVAILABLE",manager.snapshot(session_id)["status"])
            self.assertTrue(manager.close(session_id))
            self.assertFalse(manager.close(session_id))
            counters_after=model_telemetry.snapshot()["counters"]
            self.assertEqual(counters_before.get("playwright_action_attempts",0)+5,
                             counters_after["playwright_action_attempts"])
            self.assertEqual(counters_before.get("playwright_actions",0)+5,
                             counters_after["playwright_actions"])
            self.assertEqual(counters_before.get("playwright_navigate_attempts",0)+2,
                             counters_after["playwright_navigate_attempts"])
            self.assertEqual(counters_before.get("playwright_navigate_back_attempts",0)+1,
                             counters_after["playwright_navigate_back_attempts"])
            self.assertEqual(counters_before.get("playwright_load_more_attempts",0)+1,
                             counters_after["playwright_load_more_attempts"])
            self.assertEqual(counters_before.get("playwright_scroll_successes",0)+1,
                             counters_after["playwright_scroll_successes"])
        finally:
            manager.close_all()

    def test_chat_scrape_control_allows_same_origin_links_and_readonly_reveal_only(self):
        class Session:
            def __init__(self, command, user_data_dir=None, interactive_login=False):
                self.current = ""
                self.clicked = []
                self.clicked_links = []
            def start(self): return {"status": "READY", "tools": ["browser_navigate", "browser_snapshot"]}
            def call(self, tool, arguments):
                if tool == "browser_navigate": self.current = arguments["url"]
                text = (f"- Page URL: {self.current}\n- Page Title: Catalog" if tool == "browser_navigate"
                        else "- heading \"Catalog\"\n- paragraph: updated records")
                return {"status": "AVAILABLE", "response": {"result": {"content": [{"type": "text", "text": text}]}}}
            def picker_evidence(self):
                return {"status": "AVAILABLE", "candidates": [
                    {"kind": "navigation", "tag": "a", "text": "Next", "attributes": {"href": "/catalog", "href_query_keys": "page"},
                     "selector_candidates": ["a.next"]},
                    {"kind": "control", "tag": "button", "text": "Load more", "attributes": {},
                     "selector_candidates": ["button.more"]},
                    {"kind": "control", "tag": "button", "text": "Add to cart", "attributes": {},
                     "selector_candidates": ["button.cart"]},
                    {"kind": "navigation", "tag": "a", "text": "External", "attributes": {"href": "https://other.example/catalog", "href_query_keys": ""},
                     "selector_candidates": ["a.external"]},
                    {"kind": "navigation", "tag": "a", "text": "Delete account", "attributes": {"href": "/account/delete", "href_query_keys": ""},
                     "selector_candidates": ["a.delete"]},
                    {"kind": "navigation", "tag": "a", "text": "Query action", "attributes": {"href": "/catalog", "href_query_keys": "action"},
                     "selector_candidates": ["a.query-delete"]},
                ]}
            def click_recipe_load_more(self, selector, label=None):
                self.clicked.append((selector, label))
                return {"status": "AVAILABLE", "page": f"- Page URL: {self.current}\n- Page Title: Catalog"}
            def click_local_control(self, selector, label, origin):
                self.clicked_links.append((selector, label, origin))
                self.current = "https://example.test/catalog?page=2"
                return {"status": "AVAILABLE", "page": f"- Page URL: {self.current}\n- Page Title: Catalog page 2"}
            def close(self): pass

        manager = BrowserSessionManager(lambda: ["unused"], session_factory=Session)
        try:
            opened = manager.open()
            session_id = opened["session_id"]
            manager.navigate(session_id, "https://example.test/catalog")
            candidates = manager.scrape_readonly_control_candidates(session_id)
            self.assertEqual([("Next", "navigation"), ("Load more", "control")],
                             [(item["label"], item["kind"]) for item in candidates["candidates"]])
            next_page = manager.click_scrape_readonly_control(session_id, "Next")
            self.assertEqual("AVAILABLE", next_page["status"], next_page)
            self.assertEqual("https://example.test/catalog?page=2", next_page["url"])
            more = manager.click_scrape_readonly_control(session_id, "Load more")
            self.assertEqual("AVAILABLE", more["status"])
            session = manager._sessions[session_id]["session"]
            self.assertEqual([("a.next", "Next", "https://example.test")], session.clicked_links)
            self.assertEqual([("button.more", "Load more")], session.clicked)
            self.assertEqual("SCRAPE_CONTROL_NOT_READ_ONLY",
                             manager.click_scrape_readonly_control(session_id, "Add to cart")["error"])
            self.assertEqual("SCRAPE_NAVIGATION_LEFT_ORIGIN",
                             manager.click_scrape_readonly_control(session_id, "External")["error"])
            self.assertEqual("SCRAPE_CONTROL_NOT_READ_ONLY",
                             manager.click_scrape_readonly_control(session_id, "Delete account")["error"])
            self.assertEqual("SCRAPE_CONTROL_NOT_READ_ONLY",
                             manager.click_scrape_readonly_control(session_id, "Query action")["error"])
            self.assertEqual([("button.more", "Load more")], session.clicked)
            self.assertEqual([("a.next", "Next", "https://example.test")], session.clicked_links)
        finally:
            manager.close_all()

    def test_local_e2e_chat_navigation_and_fresh_accessibility_text_checks(self):
        class Session:
            def __init__(self, command, *, allowed_local_origin=None):
                self.origin = allowed_local_origin
                self.current = ""
            def start(self): return {"status": "READY"}
            def call(self, tool, arguments):
                if tool == "browser_navigate":
                    self.current = arguments["url"]
                    content = f"- Page URL: {self.current}\n- Page Title: Game"
                else:
                    content = '- heading "Game"\n- paragraph: Level 2 Ready'
                return {"status": "AVAILABLE", "response": {"result": {"content": [
                    {"type": "text", "text": content}]}}}
            def close(self): pass

        manager = BrowserSessionManager(lambda: ["bundled"], session_factory=Session)
        try:
            opened = manager.open_local("http://127.0.0.1:4173")
            session_id = opened["session_id"]
            self.assertEqual("AVAILABLE", manager.navigate(session_id, "http://127.0.0.1:4173/")["status"])
            navigated = manager.navigate_local_path(session_id, "/game/level-2?test=1")
            self.assertEqual("AVAILABLE", navigated["status"])
            self.assertEqual("http://127.0.0.1:4173/game/level-2?test=1", navigated["url"])
            self.assertEqual("BLOCKED", manager.navigate_local_path(session_id, "//evil.test/")["status"])
            self.assertTrue(manager.check_local_text(session_id, "Level 2", wait=True)["verified"])
            self.assertTrue(manager.check_local_text(session_id, "Ready")["verified"])
            missing = manager.check_local_text(session_id, "Not present")
            self.assertEqual("LOCAL_E2E_TEXT_NOT_FOUND", missing["error"])
        finally:
            manager.close_all()

    def test_persistent_profile_is_scoped_private_and_single_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            created = []
            class Session:
                def __init__(self, command, *, user_data_dir=None, interactive_login=False):
                    self.user_data_dir = user_data_dir
                    self.interactive_login = interactive_login
                    self.closed = False
                    created.append(self)
                def start(self): return {"status": "READY"}
                def close(self): self.closed = True
            manager = BrowserSessionManager(lambda: ["bundled"], maximum_sessions=2,
                session_factory=Session, profile_root=Path(tmp) / "profiles")
            try:
                opened = manager.open("account-one", interactive_login=True)
                self.assertEqual("READY", opened["status"])
                self.assertTrue(Path(created[0].user_data_dir).is_dir())
                self.assertEqual(0o700, Path(created[0].user_data_dir).stat().st_mode & 0o777)
                self.assertEqual("PROFILE_IN_USE", manager.open("account-one")["status"])
                self.assertEqual([{"profile_id": "account-one", "in_use": True}], manager.profiles())
                with self.assertRaisesRegex(ValueError, "BROWSER_PROFILE_IN_USE"):
                    manager.forget_profile("account-one")
                self.assertTrue(manager.close(opened["session_id"]))
                reopened = manager.open("account-one")
                self.assertEqual("READY", reopened["status"])
                self.assertFalse(created[1].interactive_login)
                manager.close(reopened["session_id"])
                self.assertTrue(manager.forget_profile("account-one"))
                self.assertFalse((Path(tmp) / "profiles" / "account-one").exists())
                self.assertEqual("BROWSER_PROFILE_ID_INVALID", manager.open("../escape")["error"])
            finally:
                manager.close_all()


if __name__ == "__main__":
    unittest.main()
