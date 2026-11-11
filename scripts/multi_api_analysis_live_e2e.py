#!/usr/bin/env python3
"""Run every provider in the curated analysis matrix once per task.

Unlike the deterministic regression test, this invokes the real registered
provider adapters. Missing credentials and provider failures are recorded as
non-passing outcomes; no mock or skip is counted as success.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from olcr_api import external_tools
from olcr_api.ollama import ModelFailure


FIXTURE = ROOT / "backend/tests/fixtures/multi_api_analysis_cases.json"
SECRET_KEY = re.compile(r"(?:api[_-]?key|access[_-]?token|token|secret|password|authorization|credential)", re.I)
SECRET_VALUE = re.compile(r"(?i)(api[_-]?key|access[_-]?token|token|secret|password|authorization|credential)(\s*[=:]\s*)[^\s&,;]+")


def safe(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if SECRET_KEY.search(str(key)):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = safe(item)
        return result
    if isinstance(value, list):
        return [safe(item) for item in value[:100]]
    if isinstance(value, str):
        text = SECRET_VALUE.sub(r"\1\2[REDACTED]", value[:20_000])
        if text.startswith(("http://", "https://")):
            parsed = urlsplit(text)
            text = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        return text
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2_000]


def run(output: Path, delay: float) -> dict:
    output = output.expanduser().resolve()
    private_tmp = Path("/private/tmp").resolve()
    if not output.is_relative_to(private_tmp):
        raise ValueError("Live API results must be written under /private/tmp")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cases = payload["cases"]
    results = []
    total_calls = sum(len(case["tool_ids"]) for case in cases)
    started = datetime.now(timezone.utc).isoformat()
    ordinal = 0
    for case_index, case in enumerate(cases, 1):
        for tool_id in case["tool_ids"]:
            ordinal += 1
            record = {"case_id": case["id"], "case_prompt": case["prompt"], "tool_id": tool_id,
                      "provider": external_tools.RUNTIME_REGISTRY[tool_id].provider,
                      "ordinal": ordinal, "planned_calls": total_calls}
            credential = external_tools.credential_setup(tool_id)
            if credential:
                record.update(status="AUTH_BLOCKED", error="CREDENTIALS_NOT_CONFIGURED")
                results.append(record)
                print(f"[{ordinal}/{total_calls}] {case['id']} {tool_id}: AUTH_BLOCKED", flush=True)
                continue
            try:
                arguments = external_tools.compile_provider_arguments(tool_id, case["prompt"],
                                                                        {"query": case["prompt"]})
                record["arguments"] = safe(arguments)
            except external_tools.ExternalToolError as error:
                record.update(status="ARGUMENT_ERROR", error=str(error).split(":", 1)[0])
                results.append(record)
                print(f"[{ordinal}/{total_calls}] {case['id']} {tool_id}: ARGUMENT_ERROR", flush=True)
                continue
            try:
                before = time.perf_counter()
                # Exactly one adapter entry call for this case/API pair.
                response = external_tools.execute(tool_id, arguments)
                record["elapsed_ms"] = round((time.perf_counter() - before) * 1000, 1)
                if not isinstance(response, dict):
                    record.update(status="INVALID_ENVELOPE", error="NON_OBJECT_RESPONSE")
                else:
                    data = response.get("data") if isinstance(response.get("data"), dict) else {}
                    sources = response.get("sources") if isinstance(response.get("sources"), list) else []
                    record["response"] = safe(response)
                    if (not data or data.get("semantic_status") in {"EMPTY", "NO_RELEVANT_RESULT"}
                            or data.get("has_payload") is False):
                        record.update(status="EMPTY_DATA", error="EMPTY_DATA_ENVELOPE")
                    elif not sources:
                        record.update(status="PROVENANCE_ERROR", error="PROVENANCE_UNAVAILABLE")
                    else:
                        record["status"] = "PASS"
                results.append(record)
                print(f"[{ordinal}/{total_calls}] {case['id']} {tool_id}: {record['status']}", flush=True)
            except external_tools.ExternalToolError as error:
                category = str(error).split(":", 1)[0]
                record.update(status="API_ERROR", error=category)
                results.append(record)
                print(f"[{ordinal}/{total_calls}] {case['id']} {tool_id}: API_ERROR {category}", flush=True)
            except (TimeoutError, OSError, ValueError, ModelFailure) as error:
                record.update(status="TRANSPORT_ERROR", error=type(error).__name__)
                results.append(record)
                print(f"[{ordinal}/{total_calls}] {case['id']} {tool_id}: TRANSPORT_ERROR {type(error).__name__}", flush=True)
            except Exception as error:  # keep the batch moving; detail is sanitized
                record.update(status="ADAPTER_ERROR", error=type(error).__name__)
                results.append(record)
                print(f"[{ordinal}/{total_calls}] {case['id']} {tool_id}: ADAPTER_ERROR {type(error).__name__}", flush=True)
            if delay > 0:
                time.sleep(delay)
        print(f"TASK_COMPLETE {case_index}/{len(cases)} {case['id']}", flush=True)

    counts = Counter(item["status"] for item in results)
    per_api: dict[str, dict[str, int]] = {}
    for item in results:
        stats = per_api.setdefault(item["tool_id"], {"calls": 0, "PASS": 0, "AUTH_BLOCKED": 0,
            "ARGUMENT_ERROR": 0, "API_ERROR": 0, "EMPTY_DATA": 0, "PROVENANCE_ERROR": 0,
            "TRANSPORT_ERROR": 0, "ADAPTER_ERROR": 0, "INVALID_ENVELOPE": 0})
        stats["calls"] += 1
        stats[item["status"]] = stats.get(item["status"], 0) + 1
    summary = {
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "task_count": len(cases),
        "distinct_api_count": len(per_api),
        "scheduled_adapter_calls": total_calls,
        "actual_adapter_calls": sum(counts[key] for key in counts if key != "AUTH_BLOCKED" and key != "ARGUMENT_ERROR"),
        "status_counts": dict(counts),
        "per_api": per_api,
        "live_external_requests": True,
        "response_secrets_redacted": True,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=Path("/private/tmp/olcr-multi-api-live-e2e"))
    parser.add_argument("--delay", type=float, default=0.25)
    args = parser.parse_args()
    summary = run(args.output, args.delay)
    return 0 if summary["status_counts"].get("PASS", 0) == summary["scheduled_adapter_calls"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
