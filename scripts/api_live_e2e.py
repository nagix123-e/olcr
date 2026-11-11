#!/usr/bin/env python3
"""Run the canonical 100-case API gate through the real OLCR chat endpoint.

This harness intentionally has no adapter imports, mocks, or PASS-on-skip path.
The caller must provide a running backend and project/conversation identifiers.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import os
import sys
import time
from urllib import error, request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from olcr_api.api_acceptance import ACCEPTANCE_CASES, acceptance_fixture_hash


def _post(url: str, payload: dict, token: str | None, timeout: float) -> tuple[int, dict | str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-OLCR-Session"] = token
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read(1_000_000).decode("utf-8", "replace")
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw
    except error.HTTPError as exc:
        raw = exc.read(100_000).decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except (error.URLError, TimeoutError) as exc:
        return 0, {"error_class": "EXTERNAL_BLOCKED", "error": str(exc)}


def run(base_url: str, project_id: str, conversation_id: str, output: Path, token: str | None, timeout: float) -> dict:
    run_id = uuid.uuid4().hex
    started = datetime.now(timezone.utc).isoformat()
    cases = []
    for index, case in enumerate(ACCEPTANCE_CASES, 1):
        case_started = time.perf_counter()
        status, response = _post(base_url.rstrip("/") + "/api/chat", {
            "message": case["user_prompt"], "project_id": project_id,
            "conversation_id": conversation_id, "message_id": f"api-e2e-{run_id}-{index}",
        }, token, timeout)
        elapsed = (time.perf_counter() - case_started) * 1000
        body = response if isinstance(response, dict) else {"body": str(response)[:4000]}
        blocked = status == 0 or status in {401, 403, 429, 502, 503, 504}
        tool_payload = body.get("tool") if isinstance(body.get("tool"), dict) else body.get("data")
        data = tool_payload if isinstance(tool_payload, dict) else {}
        observed_tool = body.get("tool_id") or data.get("tool_id")
        observed_kind = data.get("response_kind") or body.get("response_kind")
        user_message = body.get("response") or body.get("message")
        passed = bool(status == 200 and observed_tool == case["tool_id"] and
                      observed_kind == case["expected_response_kind"] and
                      isinstance(user_message, str) and user_message.strip() and
                      "Traceback" not in user_message)
        cases.append({"case_id": f"api-{index:03d}", **case,
                      "http_status": status, "latency_ms": elapsed,
                      "status": "EXTERNAL_BLOCKED" if blocked else "PASS" if passed else "FAIL",
                      "response": body})
    summary = {
        "run_id": run_id, "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
        "fixture_hash": acceptance_fixture_hash(), "api_total": len(cases),
        "api_pass": sum(item["status"] == "PASS" for item in cases),
        "api_fail": sum(item["status"] == "FAIL" for item in cases),
        "api_skip": 0, "external_blocked": sum(item["status"] == "EXTERNAL_BLOCKED" for item in cases),
        "auth_blocked": sum(item["http_status"] in {401, 403} for item in cases),
        "live_entrypoint": "/api/chat", "mock_used": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("OLCR_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--project-id", default=os.environ.get("OLCR_PROJECT_ID", ""))
    parser.add_argument("--conversation-id", default=os.environ.get("OLCR_CONVERSATION_ID", ""))
    parser.add_argument("--session-token", default=os.environ.get("OLCR_GUI_SESSION_TOKEN"))
    parser.add_argument("--output", type=Path, default=Path("benchmark_results/api_e2e"))
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if not args.project_id or not args.conversation_id:
        parser.error("--project-id and --conversation-id are required; the harness never creates a test-only backend path")
    summary = run(args.base_url, args.project_id, args.conversation_id, args.output, args.session_token, args.timeout)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["api_pass"] == 100 else 1


if __name__ == "__main__":
    raise SystemExit(main())
