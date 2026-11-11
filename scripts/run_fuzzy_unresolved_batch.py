#!/usr/bin/env python3
"""Replay a reviewed unresolved fuzzy-routing batch into one existing OLCR chat.

This is a live GUI-backed harness: it requires the running OLCR desktop backend,
never creates projects/conversations, and preserves every SSE event in JSONL.
It deliberately records results without declaring semantic PASS; that requires
the subsequent evidence-based audit against the expected capability.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from urllib import error, request


def _post_stream(url: str, prompt: str, project_id: str, conversation_id: str,
                 session_token: str | None, timeout: float) -> tuple[int, list[dict], str | None]:
    chat_payload = {"message": prompt, "conversation_id": conversation_id}
    if project_id:
        chat_payload["project_id"] = project_id
    payload = json.dumps(chat_payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if session_token:
        headers["X-OLCR-Session"] = session_token
    req = request.Request(url, data=payload, headers=headers, method="POST")
    events: list[dict] = []
    try:
        with request.urlopen(req, timeout=timeout) as response:
            status = response.status
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    events.append({"type": "malformed_sse", "raw": line[:4000]})
                    continue
                events.append(event)
                if event.get("type") in {"done", "error"}:
                    break
            return status, events, None
    except error.HTTPError as exc:
        body = exc.read(100_000).decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"body": body}
        return exc.code, [{"type": "http_error", "body": parsed}], f"HTTP_{exc.code}"
    except (error.URLError, TimeoutError, OSError) as exc:
        return 0, events, f"{type(exc).__name__}: {exc}"


def run(source: Path, output: Path, base_url: str, project_id: str,
        conversation_id: str, session_token: str | None, timeout: float,
        start_index: int) -> dict:
    source_rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    output.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    finished = 0
    http_200 = 0
    transport_errors = 0
    with output.open("w", encoding="utf-8") as stream:
        for index, case in enumerate(source_rows, 1):
            if index < start_index:
                continue
            prompt = str(case.get("prompt") or case.get("user_prompt") or "").strip()
            if not prompt:
                result = {"batch_index": index, "case_id": case.get("case_id"),
                          "expected_tool": case.get("expected_tool"), "prompt": prompt,
                          "http_status": 0, "transport_error": "EMPTY_PROMPT", "events": []}
            else:
                tick = time.perf_counter()
                status, events, transport_error = _post_stream(
                    base_url.rstrip("/") + "/api/chat/stream", prompt,
                    project_id, conversation_id, session_token, timeout,
                )
                done = next((event for event in reversed(events) if event.get("type") == "done"), {})
                task = done.get("task") if isinstance(done.get("task"), dict) else {}
                chunks = "".join(str(event.get("text") or "") for event in events
                                 if event.get("type") == "chunk")
                result = {
                    "batch_index": index,
                    "case_id": case.get("case_id"),
                    "expected_tool": case.get("expected_tool"),
                    "prompt": prompt,
                    "conversation_id": conversation_id,
                    "http_status": status,
                    "elapsed_seconds": round(time.perf_counter() - tick, 3),
                    "transport_error": transport_error,
                    "task": task,
                    "response_text": chunks,
                    "events": events,
                }
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            stream.flush()
            finished += 1
            http_200 += result.get("http_status") == 200
            transport_errors += bool(result.get("transport_error"))
            print(json.dumps({
                "batch_index": index,
                "total": len(source_rows),
                "case_id": result.get("case_id"),
                "http_status": result.get("http_status"),
                "transport_error": result.get("transport_error"),
                "task_state": (result.get("task") or {}).get("state"),
            }, ensure_ascii=False), flush=True)
    summary = {
        "batch": output.stem,
        "source_batch": str(source.resolve()),
        "jsonl": str(output.resolve()),
        "total_source_rows": len(source_rows),
        "submitted": finished,
        "http_200": http_200,
        "transport_errors": transport_errors,
        "conversation_id": conversation_id,
        "base_url": base_url.rstrip("/"),
        "endpoint": "/api/chat/stream",
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "semantic_passes": None,
        "semantic_pass_note": "Requires separate evidence-based audit; HTTP 200 is not a semantic pass.",
    }
    summary_path = output.with_name(output.stem + "-summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Reviewed unresolved case JSONL")
    parser.add_argument("--output", required=True, type=Path, help="Output JSONL; must not already exist")
    parser.add_argument("--base-url", default=os.environ.get("OLCR_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--project-id", default=os.environ.get("OLCR_PROJECT_ID", ""))
    parser.add_argument("--conversation-id", default=os.environ.get("OLCR_CONVERSATION_ID", ""))
    parser.add_argument("--session-token", default=os.environ.get("OLCR_GUI_SESSION_TOKEN"))
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--start-index", type=int, default=1,
                        help="Resume after a partial log; rows before this 1-based index are skipped")
    args = parser.parse_args()
    if not args.source.is_file():
        parser.error("--source must identify an existing JSONL file")
    if args.output.exists():
        parser.error("--output already exists; choose a fresh run-specific path")
    if not args.conversation_id:
        parser.error("--conversation-id is required; projectless chats are supported and no new chat is created")
    if args.start_index < 1 or args.timeout < 1:
        parser.error("--start-index and --timeout must be positive")
    summary = run(args.source, args.output, args.base_url, args.project_id,
                  args.conversation_id, args.session_token, args.timeout, args.start_index)
    return 0 if summary["submitted"] == max(0, summary["total_source_rows"] - args.start_index + 1) else 1


if __name__ == "__main__":
    raise SystemExit(main())
