#!/usr/bin/env python3
"""Measure bounded DOM Recipe extraction and opt-in network metadata overhead on a local fixture."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from olcr_api.browser_network import BrowserSessionManager
from olcr_api.node_mcp_runtime import managed_browser_command


ROWS = [{"name": f"Organization {index:03d}", "price": index * 7} for index in range(1, 26)]
HTML = b'''<!doctype html><html><body><main><h1>Local Recipe Benchmark</h1><div id="records"></div>
<script>fetch('/api/records?session=fixture').then(r=>r.json()).then(data=>{
document.querySelector('#records').innerHTML=data.map(row=>`<article class="record"><span class="name">${row.name}</span><span class="price">${row.price}</span></article>`).join('');
});</script></main></body></html>'''


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/records"):
            body = json.dumps(ROWS).encode()
            content_type = "application/json"
        else:
            body = HTML
            content_type = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, int((len(ordered) * fraction + 0.999999)) - 1)], 3)


def run(iterations: int) -> dict:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = server.server_address[1]
    manager = BrowserSessionManager(managed_browser_command, maximum_sessions=1)
    startup_ms: list[float] = []
    navigation_ms: list[float] = []
    snapshot_ms: list[float] = []
    extraction_ms: list[float] = []
    observer_ms: list[float] = []
    correct = 0
    observed_json_requests = 0
    address = ("127.0.0.1", port)
    try:
        with patch("olcr_api.browser_network._resolve_public_addresses", return_value=(address,)):
            for _ in range(iterations):
                start = time.perf_counter()
                opened = manager.open()
                startup_ms.append((time.perf_counter() - start) * 1000)
                if opened.get("status") != "READY":
                    raise RuntimeError(opened.get("error") or "BROWSER_START_FAILED")
                session_id = opened["session_id"]
                try:
                    start = time.perf_counter()
                    page = manager.navigate(session_id, f"http://fixture.example:{port}/")
                    navigation_ms.append((time.perf_counter() - start) * 1000)
                    if page.get("status") != "AVAILABLE":
                        raise RuntimeError(page.get("error") or "FIXTURE_NAVIGATION_FAILED")

                    start = time.perf_counter()
                    snapshot = manager.snapshot(session_id)
                    snapshot_ms.append((time.perf_counter() - start) * 1000)
                    if snapshot.get("status") != "AVAILABLE":
                        raise RuntimeError(snapshot.get("error") or "SNAPSHOT_FAILED")

                    start = time.perf_counter()
                    extracted = manager.extract_recipe(session_id, "article.record", {
                        "name": {"selector": ".name", "extract": "text"},
                        "price": {"selector": ".price", "extract": "number"},
                    }, limit=100)
                    extraction_ms.append((time.perf_counter() - start) * 1000)
                    expected = [{"name": row["name"], "price": row["price"]} for row in ROWS]
                    if extracted.get("status") == "AVAILABLE" and extracted.get("rows") == expected:
                        correct += 1

                    start = time.perf_counter()
                    network = manager.network_requests(session_id)
                    observer_ms.append((time.perf_counter() - start) * 1000)
                    if network.get("status") == "AVAILABLE":
                        observed_json_requests += sum(
                            request.get("url", "").endswith("/api/records") and request.get("status_code") == 200
                            for request in network.get("requests", [])
                        )
                finally:
                    manager.close(session_id)
    finally:
        manager.close_all()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    def summary(values):
        return {"p50_ms": round(statistics.median(values), 3), "p95_ms": _percentile(values, .95),
                "min_ms": round(min(values), 3), "max_ms": round(max(values), 3)}

    return {
        "benchmark": "scrape_observer_local_fixture_v1",
        "scope": "loopback fixture through bundled Chromium and pinned proxy; excludes live sites and macOS GUI",
        "iterations": iterations,
        "fixture_records": len(ROWS),
        "quality": {"exact_rows": correct, "total": iterations, "accuracy": correct / iterations,
                    "json_fetch_observed": observed_json_requests, "total_fetches_expected": iterations},
        "latency": {"browser_startup": summary(startup_ms), "navigation_and_snapshot": summary(navigation_ms),
                    "snapshot_only": summary(snapshot_ms), "dom_extract_25_records": summary(extraction_ms),
                    "opt_in_network_metadata_added": summary(observer_ms)},
        "model_calls": 0,
        "persisted_network_bodies_or_headers": False,
        "timestamp_unix": time.time(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=10)
    args = parser.parse_args()
    if not 3 <= args.iterations <= 50:
        parser.error("--iterations must be in [3, 50]")
    print(json.dumps(run(args.iterations), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
