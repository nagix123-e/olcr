"""Deterministic Coding benchmark fixtures and result comparison."""
from __future__ import annotations
import hashlib, json, shutil, subprocess, tempfile, time, sys
from urllib import request as urlrequest
from pathlib import Path
from typing import Any
from .coding_telemetry import CodingTelemetry

BENCHMARK_VERSION = "coding-v2"
BENCHMARK_CASE = "small"
VERIFY_TIMEOUT_SECONDS = 20
MAX_RETAINED_INCOMPLETE_RUNS = 8
SUCCESSFUL_TASK_STATUSES = {"COMPLETED", "DONE"}
CASES: dict[str, dict[str, Any]] = {
    "small": {
        "files": {
            "src/math.py": "def add(a, b):\n    return a - b\n",
            "tests/test_math.py": "import unittest\nfrom src.math import add\n\nclass AddTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        },
        "target_files": ["src/math.py"],
        "verify_command": [sys.executable, "-m", "unittest", "tests.test_math"],
        "request": "Fix the existing add function so tests/test_math.py passes. Change only src/math.py, then run the focused test command.",
    },
    "medium": {"files": {"src/main.ts": "export const value = 1;\n", "src/util.ts": "export const add=(a:number,b:number)=>a+b;\n", "tests/util.test.ts": "// local focused test fixture\n"}, "target_files": ["src/main.ts", "src/util.ts"], "verify_command": ["true"], "request": "Update the existing local utility behavior and run the focused verification."},
    "large": {"files": {"package.json": "{}\n", "src/App.tsx": "export default function App(){return null}\n", "src/main.tsx": "import App from './App';\n", "src/styles.css": "/* fixture */\n", "tests/smoke.test.ts": "// local verification fixture\n"}, "target_files": ["src/App.tsx", "src/main.tsx"], "verify_command": ["true"], "request": "Update the existing local application and run the focused verification."},
}

def _hash_files(root: Path, files: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(files):
        digest.update(relative.encode()); digest.update(b"\\0"); digest.update((root / relative).read_bytes()); digest.update(b"\\0")
    return digest.hexdigest()

def _request_hash(request: str) -> str:
    return hashlib.sha256(request.encode("utf-8")).hexdigest()

def benchmark_contract(case: str = BENCHMARK_CASE) -> dict[str, Any]:
    if case not in CASES:
        raise ValueError(f"unknown coding benchmark case: {case}")
    spec = CASES[case]
    return {"benchmark_version": BENCHMARK_VERSION, "benchmark_case": case,
            "files": sorted(spec["files"]), "target_files": list(spec["target_files"]),
            "verify_command": list(spec["verify_command"]), "verify_timeout_seconds": VERIFY_TIMEOUT_SECONDS,
            "request": spec["request"], "request_hash": _request_hash(spec["request"]),
            "network": "FORBIDDEN", "mcp": "FORBIDDEN", "browser": "FORBIDDEN",
            "dependency_install": "FORBIDDEN", "allowed_mutation": list(spec["target_files"])}

def verify_fixture(case: str, root: str | Path, *, timeout_seconds: float = VERIFY_TIMEOUT_SECONDS) -> dict[str, str]:
    try:
        completed = subprocess.run(CASES[case]["verify_command"], cwd=Path(root), capture_output=True, text=True, timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "FAIL", "output": str(exc)}
    return {"status": "PASS" if completed.returncode == 0 else "FAIL", "output": (completed.stdout + completed.stderr)[-4000:]}

def prepare_fixture(case: str, base_dir: str | Path | None = None) -> dict[str, Any]:
    if case not in CASES: raise ValueError(f"unknown coding benchmark case: {case}")
    root = Path(tempfile.mkdtemp(prefix=f"olcr-bench-{case}-", dir=base_dir)); spec = CASES[case]
    for relative, content in spec["files"].items():
        path = root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content, encoding="utf-8")
    initial = verify_fixture(case, root)
    return {"case": case, "root": root, "benchmark_version": BENCHMARK_VERSION, "files": sorted(spec["files"]), "target_files": list(spec["target_files"]),
            "fixture_hash": _hash_files(root, list(spec["files"])), "request": spec["request"],
            "request_hash": _request_hash(spec["request"]), "verify_command": list(spec["verify_command"]),
            "verify_timeout_seconds": VERIFY_TIMEOUT_SECONDS,
            "forbidden_operations": ["network", "mcp", "browser", "dependency_install"],
            "initial_verify_status": initial["status"], "initial_verify_output": initial["output"]}

def _prune_retained_runs(storage_root: Path, *, keep: int = MAX_RETAINED_INCOMPLETE_RUNS) -> None:
    """Bound diagnostic retention without touching active or terminal runs."""
    retained: list[tuple[float, Path]] = []
    for run_dir in storage_root.iterdir() if storage_root.exists() else ():
        if not run_dir.is_dir():
            continue
        metadata_paths = list(run_dir.glob("*/.benchmark_metadata.json"))
        if len(metadata_paths) != 1:
            continue
        try:
            metadata = json.loads(metadata_paths[0].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if metadata.get("lifecycle_state") == "RETAINED_INCOMPLETE":
            retained.append((float(metadata.get("created_at", run_dir.stat().st_mtime)), run_dir))
    for _, run_dir in sorted(retained, key=lambda item: item[0], reverse=True)[keep:]:
        shutil.rmtree(run_dir, ignore_errors=True)

def write_incomplete_artifact(output_dir: str | Path, *, contract: dict[str, Any], task_id: str, telemetry: dict[str, Any] | None = None, reason: str = "TIMEOUT", diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    destination = Path(output_dir); destination.mkdir(parents=True, exist_ok=True)
    result = {"benchmark_version": BENCHMARK_VERSION, "case": contract["case"], "task_id": task_id, "benchmark_execution": "LIVE",
              "benchmark_result": "INCOMPLETE", "partial_metrics_available": bool(telemetry),
              "fixture_hash": contract["fixture_hash"], "request_hash": contract["request_hash"],
              "initial_verify_status": contract["initial_verify_status"], "failure_reason": reason,
              "baseline_eligible": False, "baseline_ineligible_reasons": [reason], "diagnostics": diagnostics or {},
              "telemetry": telemetry or {"task_id": task_id, "status": "NOT_AVAILABLE"}}
    path = destination / f"{int(time.time()*1000)}-{contract['case']}-incomplete.json"; path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"); result["result_path"] = str(path); return result

def _canonical_verify_status(value: dict[str, Any] | None) -> str | None:
    """Read the canonical telemetry field, with deterministic legacy support."""
    if not isinstance(value, dict):
        return None
    status = value.get("verify_status")
    if status is None:
        status = value.get("verification_status")
    return status


def baseline_eligibility(*, benchmark_execution_mode: str, task_status: str | None,
                         verify_status: str | None, telemetry: dict[str, Any] | None,
                         changed_files: list[str] | None = None,
                         allowed_files: set[str] | None = None) -> tuple[bool, list[str]]:
    """Evaluate the complete live-baseline contract once, with reasons."""
    record = telemetry if isinstance(telemetry, dict) else {}
    reasons: list[str] = []
    if benchmark_execution_mode != "LIVE_ORCHESTRATOR":
        reasons.append("NOT_LIVE_ORCHESTRATOR")
    if task_status not in SUCCESSFUL_TASK_STATUSES:
        reasons.append("TASK_NOT_SUCCESSFUL")
    if verify_status is None:
        reasons.append("VERIFY_STATUS_MISSING")
    elif verify_status != "PASS":
        reasons.append("VERIFY_NOT_PASS")
    if not record.get("task_finished_at"):
        reasons.append("TELEMETRY_NOT_FINISHED")
    model_calls = record.get("model_call_count")
    if not isinstance(model_calls, (int, float)) or model_calls <= 0:
        reasons.append("NO_REAL_MODEL_CALL")
    if allowed_files is not None:
        out_of_scope = sorted(set(changed_files or []) - allowed_files)
        if out_of_scope:
            reasons.append("OUT_OF_SCOPE_MUTATION")
    return not reasons, reasons


def finalize_live_artifact(output_dir: str | Path, *, contract: dict[str, Any], task_id: str,
                           telemetry: dict[str, Any], task_status: str, verify_status: str | None = None,
                           verification_status: str | None = None,
                           changed_files: list[str] | None = None, phase_summary: list[dict[str, Any]] | None = None,
                           runtime_model_observation: dict[str, Any] | None = None,
                           diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist a truthful live result; only DONE+PASS with model calls qualifies."""
    changed = sorted(set(changed_files or [])); allowed = set(contract.get("target_files") or [])
    canonical_status = verify_status if verify_status is not None else verification_status
    eligible, ineligible_reasons = baseline_eligibility(
        benchmark_execution_mode="LIVE_ORCHESTRATOR", task_status=task_status,
        verify_status=canonical_status, telemetry=telemetry,
        changed_files=changed, allowed_files=allowed,
    )
    result = {"benchmark_version": BENCHMARK_VERSION, "case": contract["case"], "task_id": task_id,
              "benchmark_execution_mode": "LIVE_ORCHESTRATOR", "benchmark_execution": "LIVE", "benchmark_result": "PASS" if eligible else "INCOMPLETE",
              "baseline_eligible": eligible, "partial_metrics_available": True,
              "fixture_hash": contract["fixture_hash"], "request_hash": contract["request_hash"],
              "initial_verify_status": contract["initial_verify_status"], "task_status": task_status,
              "verify_status": canonical_status, "baseline_ineligible_reasons": ineligible_reasons, "changed_files": changed,
              "allowed_mutation": sorted(allowed),
              "phase_summary": phase_summary or [], "runtime_model_observation": runtime_model_observation or {},
              "diagnostics": diagnostics or {}, "telemetry": telemetry}
    destination = Path(output_dir); destination.mkdir(parents=True, exist_ok=True)
    suffix = "pass" if eligible else "incomplete"
    path = destination / f"{int(time.time()*1000)}-{contract['case']}-{suffix}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    result["result_path"] = str(path); return result


def refinalize_live_artifact(source_artifact: str | Path | dict[str, Any], output_dir: str | Path | None = None) -> dict[str, Any]:
    """Re-finalize persisted evidence without rerunning the task or model."""
    source = json.loads(Path(source_artifact).read_text(encoding="utf-8")) if isinstance(source_artifact, (str, Path)) else dict(source_artifact)
    case = str(source.get("case") or BENCHMARK_CASE)
    contract = benchmark_contract(case)
    contract["case"] = case
    contract.update({key: source[key] for key in ("fixture_hash", "request_hash", "initial_verify_status") if key in source})
    telemetry = source.get("telemetry") if isinstance(source.get("telemetry"), dict) else {}
    status = source.get("task_status") or source.get("task_final_status")
    verify_status = _canonical_verify_status(source) or _canonical_verify_status(telemetry)
    destination = Path(output_dir) if output_dir is not None else Path(source_artifact).parent if isinstance(source_artifact, (str, Path)) else Path("benchmark_results")
    return finalize_live_artifact(
        destination, contract=contract, task_id=str(source.get("task_id") or "UNKNOWN"),
        telemetry=telemetry, task_status=str(status or "UNKNOWN"), verify_status=verify_status,
        changed_files=list(source.get("changed_files") or []), phase_summary=list(source.get("phase_summary") or []),
        runtime_model_observation=dict(source.get("runtime_model_observation") or {}),
        diagnostics={**(source.get("diagnostics") or {}), "refinalized_from": str(source.get("result_path") or "persisted_artifact")},
    )

def classify_live_failure(*, task_status: str, verification_status: str | None = None, verify_status: str | None = None,
                          done_satisfied: bool | None = None, verify_satisfied: bool | None = None,
                          replan_reason: str | None = None, failure_stage: str | None = None) -> dict[str, str]:
    """Map observed orchestrator evidence to stable BENCH-SMALL diagnostics."""
    canonical_status = verify_status if verify_status is not None else verification_status
    if failure_stage:
        stage = failure_stage
    elif done_satisfied is False:
        stage = "DONE"
    elif verify_satisfied is False or canonical_status != "PASS":
        stage = "VERIFY"
    elif task_status in {"TIMEOUT", "RESUMABLE"}:
        stage = "TIMEOUT_OR_RESUMABLE"
    elif task_status not in {"DONE", "COMPLETED"}:
        stage = "FINALIZATION"
    else:
        stage = "NONE"
    if done_satisfied is False:
        category = "DONE_UNSATISFIED"
    elif verify_satisfied is False or canonical_status != "PASS":
        category = "VERIFY_UNSATISFIED"
    elif replan_reason:
        category = "REPLAN"
    elif stage != "NONE":
        category = "INCOMPLETE_ARTIFACT"
    else:
        category = "NONE"
    return {"failure_stage": stage, "root_cause_category": category,
            "replan_reason": str(replan_reason or "NONE")}

def is_valid_baseline(result: dict[str, Any]) -> bool:
    telemetry = result.get("telemetry") if isinstance(result.get("telemetry"), dict) else {}
    eligible, _ = baseline_eligibility(
        benchmark_execution_mode=str(result.get("benchmark_execution_mode") or ("LIVE_ORCHESTRATOR" if result.get("benchmark_execution") == "LIVE" else "FIXTURE_ONLY")),
        task_status=str(result.get("task_status") or ("COMPLETED" if result.get("benchmark_result") == "PASS" else "UNKNOWN")),
        verify_status=_canonical_verify_status(result) or _canonical_verify_status(telemetry),
        telemetry=telemetry,
        changed_files=result.get("changed_files") or [],
        allowed_files=set(result["allowed_mutation"]) if "allowed_mutation" in result else None,
    )
    return bool(eligible and result.get("benchmark_result") == "PASS" and result.get("baseline_eligible") is True)

def run_case(case: str, output_dir: str | Path = "benchmark_results") -> dict[str, Any]:
    contract = prepare_fixture(case); started = time.perf_counter()
    try:
        telemetry = CodingTelemetry(f"bench-{case}-{int(time.time()*1000)}", model_name="UNKNOWN"); record = telemetry.finish("PASS")
        record.update({"benchmark_execution": "FIXTURE_ONLY", "fixture_root_not_persisted": True, "fixture_file_count": len(contract["files"]), "total_task_ms": (time.perf_counter()-started)*1000})
        result = {"benchmark_version": BENCHMARK_VERSION, "case": case, "task_id": record["task_id"],
                  "runtime_model_observation": {"model": "UNKNOWN", "runtime": "ollama", "engine": "UNKNOWN", "quantization": "UNKNOWN"},
                  "fixture_hash": contract["fixture_hash"], "request_hash": contract["request_hash"], "initial_verify_status": contract["initial_verify_status"],
                  "telemetry": record, "phase_summary": [], "success": True, "verify_status": "PASS", "live_model": False,
                  "benchmark_result": "FIXTURE_ONLY", "baseline_eligible": False}
    finally:
        shutil.rmtree(contract["root"], ignore_errors=True)
    destination = Path(output_dir); destination.mkdir(parents=True, exist_ok=True); path = destination / f"{int(time.time()*1000)}-{case}.json"; path.write_text(json.dumps(result, ensure_ascii=False, indent=2)); result["result_path"] = str(path); return result

def compare_results(baseline: str | Path | dict[str, Any], candidate: str | Path | dict[str, Any]) -> dict[str, Any]:
    def load(value): return json.loads(Path(value).read_text()) if isinstance(value, (str, Path)) else value
    left, right = load(baseline), load(candidate)
    compatible = left.get("benchmark_version") == right.get("benchmark_version") == BENCHMARK_VERSION and left.get("fixture_hash") == right.get("fixture_hash") and left.get("request_hash") == right.get("request_hash")
    fields = ("total_task_ms", "model_active_ms", "model_call_count", "input_tokens_total", "output_tokens_total", "load_ms", "prefill_tokens_per_sec", "decode_tokens_per_sec", "model_load_count", "peak_memory_bytes", "swap_delta_bytes",
              "planner_model_ms", "implementer_model_ms", "final_report_model_ms", "semantic_verification_model_ms",
              "deterministic_verify_ms", "final_report_model_call_count", "semantic_verification_model_call_count",
              "final_report_input_tokens", "final_report_output_tokens", "final_report_load_ms",
              "final_report_prompt_eval_ms", "final_report_decode_ms", "final_report_total_model_ms",
              "final_report_format_ms")
    a, b = left.get("telemetry", {}), right.get("telemetry", {})
    deltas = {field: b[field]-a[field] if isinstance(a.get(field), (int,float)) and isinstance(b.get(field), (int,float)) else "NOT_AVAILABLE" for field in fields}
    def historical_ambiguous(record: dict[str, Any]) -> bool:
        calls = record.get("calls") if isinstance(record, dict) else None
        return any(isinstance(call, dict) and call.get("role") == "VERIFICATION" for call in (calls or [])) and not (
            "final_report_model_call_count" in record or "verification_mode" in record
        )
    return {"benchmark_version": BENCHMARK_VERSION, "comparable": compatible, "baseline": left.get("result_path") or left.get("case"), "candidate": right.get("result_path") or right.get("case"),
            "baseline_verify_status": _canonical_verify_status(left) or _canonical_verify_status(a),
            "candidate_verify_status": _canonical_verify_status(right) or _canonical_verify_status(b),
            "baseline_verification_mode": a.get("verification_mode", "UNKNOWN"),
            "candidate_verification_mode": b.get("verification_mode", "UNKNOWN"),
            "baseline_verify_evidence_source": a.get("verify_evidence_source", "UNKNOWN"),
            "candidate_verify_evidence_source": b.get("verify_evidence_source", "UNKNOWN"),
            "baseline_historical_role_ambiguous": historical_ambiguous(a),
            "candidate_historical_role_ambiguous": historical_ambiguous(b),
            "historical_role_ambiguous": "YES" if historical_ambiguous(a) or historical_ambiguous(b) else "NO",
            "deltas": deltas}


def run_live_case(case: str, *, api_base: str, workspace: str | Path, output_dir: str | Path = "benchmark_results", timeout_seconds: int = 900) -> dict[str, Any]:
    """Submit one benchmark through the production POST /api/chat path.

    This function only creates the project/conversation, submits the request,
    and polls the persisted Coding Task. Planning, execution, retry, and
    verification remain exclusively in the backend scheduler.
    """
    run_id = f"{case}-{int(time.time()*1000)}"
    storage = Path(workspace).resolve() / ".olcr-benchmark" / run_id
    _prune_retained_runs(storage.parent)
    storage.mkdir(parents=True, exist_ok=True)
    contract = prepare_fixture(case, storage)
    root = contract["root"]
    metadata = {"benchmark_run_id": run_id, "task_id": None, "benchmark_version": BENCHMARK_VERSION,
                "initial_fixture_hash": contract["fixture_hash"], "request_hash": contract["request_hash"],
                "workspace_path": str(root), "created_at": time.time(), "lifecycle_state": "PREPARED",
                # This is a typed, read-only verification contract.  It is
                # consumed by the generic managed-task verifier; it does not
                # expand the implementation mutation scope.
                "verify_command": list(contract["verify_command"]),
                "verification_targets": [path for path in contract["files"] if path not in set(contract["target_files"])],
                "allowed_mutation": list(contract["target_files"])}
    (root / ".benchmark_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    def call(method: str, path: str, payload: dict[str, Any] | None = None):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urlrequest.Request(api_base.rstrip("/") + path, data=body, method=method, headers={"Content-Type": "application/json"})
        with urlrequest.urlopen(req, timeout=30) as response: return json.load(response)
    started = time.monotonic(); task_id = None; last = {}; artifact = None
    try:
        project = call("POST", "/projects", {"name": f"benchmark-{case}", "workspace_path": str(root)})
        conversation = call("POST", f"/projects/{project['id']}/conversations")
        submitted = call("POST", "/chat", {"project_id": project["id"], "conversation_id": conversation["id"], "message": contract["request"], "message_id": f"bench-{case}-{int(time.time()*1000)}", "execution_intent": "coding_mutation"})
        task_id = submitted.get("coding_task_id")
        if not task_id: raise RuntimeError("production Coding entrypoint did not return coding_task_id")
        metadata.update({"task_id": task_id, "lifecycle_state": "TASK_ACTIVE"})
        (root / ".benchmark_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        while time.monotonic() - started < timeout_seconds:
            last = call("GET", f"/coding-tasks/{task_id}")
            if last.get("status") in {"COMPLETED", "FAILED", "BLOCKED", "RESUMABLE"}: break
            time.sleep(1)
        else:
            result = write_incomplete_artifact(output_dir, contract=contract, task_id=task_id, reason="BENCHMARK_TASK_TIMEOUT")
            metadata["lifecycle_state"] = "RETAINED_INCOMPLETE"; (root / ".benchmark_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            result.update({"benchmark_execution_mode": "LIVE_ORCHESTRATOR", "last_task_status": last.get("status"), "last_task_activity": last.get("activity"), "workspace_path": str(root), "benchmark_run_id": run_id})
            Path(result["result_path"]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result
        telemetry = call("GET", f"/coding-tasks/{task_id}/telemetry")
        phase_payload = call("GET", f"/coding-tasks/{task_id}/phase-reports")
        phase_summary = phase_payload.get("reports") if isinstance(phase_payload, dict) else []
        phase_summary = phase_summary if isinstance(phase_summary, list) else []
        changed_files = []
        for report in phase_summary:
            if not isinstance(report, dict):
                continue
            for path in ((report.get("structured_report") or {}).get("changed_files") or []):
                if not isinstance(path, str) or not path.strip():
                    continue
                candidate = Path(path).expanduser()
                try:
                    relative = candidate.resolve().relative_to(root.resolve())
                    changed_files.append(relative.as_posix())
                except ValueError:
                    changed_files.append(path.strip())
        changed_files = sorted(set(changed_files))
        verify_status = _canonical_verify_status(telemetry)
        eligible, ineligible_reasons = baseline_eligibility(
            benchmark_execution_mode="LIVE_ORCHESTRATOR", task_status=last.get("status"),
            verify_status=verify_status, telemetry=telemetry,
            changed_files=changed_files, allowed_files=set(contract["target_files"]),
        )
        final_report = last.get("final_report") if isinstance(last.get("final_report"), dict) else {}
        final_report_present = isinstance(final_report.get("text"), str) and bool(final_report.get("text", "").strip())
        if not final_report_present:
            ineligible_reasons.append("FINAL_REPORT_MISSING")
        if telemetry.get("final_report_generation_mode") != "DETERMINISTIC":
            ineligible_reasons.append("FINAL_REPORT_NOT_DETERMINISTIC")
        eligible = not ineligible_reasons
        result = {"benchmark_version": BENCHMARK_VERSION, "case": case, "task_id": task_id, "benchmark_execution_mode": "LIVE_ORCHESTRATOR",
                  "benchmark_execution": "LIVE", "benchmark_result": "PASS" if eligible else "INCOMPLETE",
                  "fixture_hash": contract["fixture_hash"], "request_hash": contract["request_hash"], "initial_verify_status": contract["initial_verify_status"],
                  "task_final_status": last.get("status"), "verify_status": verify_status,
                  "baseline_ineligible_reasons": ineligible_reasons, "telemetry": telemetry, "success": eligible,
                  "baseline_eligible": eligible, "live_model": True, "final_report_present": final_report_present,
                  "final_report_generation_mode": telemetry.get("final_report_generation_mode", "UNKNOWN"),
                  "changed_files": changed_files, "phase_summary": phase_summary}
    except Exception as exc:
        result = write_incomplete_artifact(output_dir, contract=contract, task_id=task_id or "UNKNOWN", reason=f"{type(exc).__name__}:{exc}")
        result.update({"benchmark_execution_mode": "LIVE_ORCHESTRATOR", "workspace_path": str(root), "benchmark_run_id": run_id})
        metadata["lifecycle_state"] = "RETAINED_INCOMPLETE"
        (root / ".benchmark_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    finally:
        # Keep live workspaces on timeout, exception, or non-terminal state so
        # the persisted task can be inspected or recovered after the runner
        # exits. Cleanup is performed only after artifact finalization below.
        pass
    destination = Path(output_dir); destination.mkdir(parents=True, exist_ok=True); path = destination / f"{int(time.time()*1000)}-{case}-live.json"; path.write_text(json.dumps(result, ensure_ascii=False, indent=2)); result["result_path"] = str(path)
    result["workspace_path"] = str(root); result["benchmark_run_id"] = run_id
    terminal = last.get("status") in {"COMPLETED", "FAILED", "BLOCKED", "RESUMABLE"}
    metadata["lifecycle_state"] = "ARTIFACT_FINALIZED" if terminal else "RETAINED_INCOMPLETE"
    (root / ".benchmark_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    if terminal:
        metadata["lifecycle_state"] = "CLEANUP_ELIGIBLE"; (root / ".benchmark_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        shutil.rmtree(root, ignore_errors=True)
    return result
