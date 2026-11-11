#!/usr/bin/env python3
"""Exercise analysis through the production SSE chat route and record degradation.

Requires an already running, isolated backend with an explicitly matching build.
HTTP completion is recorded separately from missing evidence and model failures.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from urllib import request, error
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "backend/tests/fixtures/multi_api_analysis_cases.json"


def safe_text(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(api[_-]?key|access[_-]?token|authorization|password|secret)(\s*[=:]\s*)[^\s&,;]+",
                  r"\1\2[REDACTED]", text)
    return re.sub(r"(https?://[^\s?\)]+)\?[^\s\)]*", r"\1?[REDACTED]", text)


def case_outcome(task: dict, terminal: str | None, http_status: int | None, response: str) -> dict:
    contexts = task.get("selected_context") or []
    # Recovery can add intermediate analysis contexts. The final request-level
    # context owns coverage and credential status, not the first helper result.
    analysis = next((row for row in reversed(contexts) if "analysis_status" in row and row.get("analysis_request_id")),
                    next((row for row in reversed(contexts) if "analysis_status" in row), {}))
    models = [{key: row.get(key) for key in ("role", "model", "status", "error", "latency_ms")}
              for row in task.get("model_calls", [])]
    failures = [row for row in models if row.get("status") in {"generation_failed", "error", "runtime_failure_fallback"}]
    tools = [{"tool_id": (row.get("input") or {}).get("tool_id"),
              "status": row.get("status"), "error": row.get("error")}
             for row in task.get("tool_executions", []) if row.get("tool") == "external_api"]
    completed = bool(http_status == 200 and terminal == "done" and task.get("state") == "completed" and response.strip())
    return {"pipeline_completed": completed, "analysis_status": analysis.get("analysis_status", "unknown"),
            "synthesis_status": analysis.get("synthesis_status", "unknown"),
            "model_failure_count": len(failures), "model_calls": models, "api_executions": tools,
            "missing_coverage": analysis.get("missing_coverage", []),
            "credentials_missing": analysis.get("credentials_missing", []),
            "semantic_review": analysis.get("semantic_review", {}),
            "document_fetches": analysis.get("document_fetches", []),
            "semantic_pass": None}


def run(args) -> dict:
    parsed = urlsplit(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
        raise ValueError("Use the isolated backend's loopback HTTP URL")
    if args.output.exists() or args.output.with_suffix(".summary.json").exists():
        raise ValueError("Choose fresh output paths")
    base = args.base_url.rstrip("/")

    def open_request(path, payload=None, timeout=10):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        return request.urlopen(request.Request(base + path, data=body,
            headers={"Content-Type": "application/json"}), timeout=timeout)

    with open_request("/api/health") as response:
        health = json.load(response)
    if health.get("build_id") != args.expected_build_id:
        raise ValueError("Backend build ID mismatch; no prompts submitted")
    cases = json.loads(FIXTURE.read_text(encoding="utf8"))["cases"]
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [row for row in cases if row["id"] in wanted]
        if {row["id"] for row in cases} != wanted:
            raise ValueError("Unknown case ID")
    with open_request("/api/conversations", {}) as response:
        conversation = json.load(response)["id"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    print(json.dumps({"event": "start", "cases": len(cases), "conversation_id": conversation,
                      "output": str(args.output)}, ensure_ascii=False), flush=True)
    records = []
    with args.output.open("x", encoding="utf8") as stream:
        for index, case in enumerate(cases, 1):
            started = time.perf_counter()
            chunks, task, terminal, status, transport_error = [], {}, None, None, None
            try:
                with open_request("/api/chat/stream", {"message": case["prompt"],
                        "conversation_id": conversation, "message_id": f"{run_id}-{index}"}, args.timeout) as response:
                    status = response.status
                    for line in response:
                        if not line.startswith(b"data: "):
                            continue
                        event = json.loads(line[6:])
                        if event.get("type") == "chunk":
                            chunks.append(str(event.get("text") or ""))
                        if event.get("type") in {"done", "error", "cancelled"}:
                            terminal = event["type"]
                            task = event.get("task") or {}
                            break
            except error.HTTPError as exc:
                status, transport_error = exc.code, f"HTTP_{exc.code}"
            except (OSError, ValueError) as exc:
                transport_error = type(exc).__name__
            text = "".join(chunks)
            outcome = case_outcome(task, terminal, status, text)
            record = {"case_id": case["id"], "prompt": case["prompt"], "task_id": task.get("id"),
                      "http_status": status, "transport_error": transport_error,
                      "elapsed_seconds": round(time.perf_counter() - started, 2),
                      **outcome, "response": safe_text(text)}
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            records.append(record)
            print(json.dumps({"index": index, "total": len(cases), **{key: record[key] for key in
                ("case_id", "pipeline_completed", "analysis_status", "synthesis_status", "model_failure_count", "elapsed_seconds")}},
                ensure_ascii=False), flush=True)
    summary = {"run_id": run_id, "build_id": health["build_id"], "conversation_id": conversation,
               "cases": len(records), "pipeline_completed": sum(row["pipeline_completed"] for row in records),
               "analysis_statuses": dict(Counter(row["analysis_status"] for row in records)),
               "synthesis_statuses": dict(Counter(row["synthesis_status"] for row in records)),
               "model_failures": sum(row["model_failure_count"] for row in records),
               "api_statuses": dict(Counter(call["status"] for row in records for call in row["api_executions"])),
               "semantic_passes": None, "semantic_pass_note": "Requires evidence-based review; completion is not semantic PASS.",
               "finished_at": datetime.now(timezone.utc).isoformat()}
    args.output.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-build-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", help="Optional comma-separated fixture case IDs")
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    result = run(args)
    raise SystemExit(0 if result["pipeline_completed"] == result["cases"] and not result["model_failures"] else 1)
