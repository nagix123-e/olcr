"""Low-overhead, observational telemetry for Coding Task model calls."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import resource
import subprocess
import time
from typing import Any


def _rss() -> int | None:
    try:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if platform.system() == "Darwin" else value * 1024
    except Exception:
        return None


def _swap() -> int | None:
    if platform.system() != "Darwin":
        return None
    try:
        text = subprocess.check_output(["/usr/sbin/sysctl", "-n", "vm.swapusage"], text=True, timeout=0.25, stderr=subprocess.DEVNULL)
        import re
        match = re.search(r"used\s*=\s*([0-9.]+)([MG])", text)
        if not match:
            return None
        return int(float(match.group(1)) * (1024**2 if match.group(2) == "M" else 1024**3))
    except Exception:
        return None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


def prompt_sections(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    names = ["system_context", "tool_schema", "coding_protocol", "canonical_requirements", "blueprint", "file_context", "knowledge_context", "handoff", "dynamic_context"]
    values = {name: "" for name in names}
    for index, message in enumerate(messages):
        content = str(message.get("content") or "")
        role = str(message.get("role") or "")
        if index == len(messages) - 1:
            name = "dynamic_context"
        elif role == "system":
            lower = content.lower()
            name = "tool_schema" if "schema" in lower or "protocol" in lower else "system_context"
        else:
            name = "coding_protocol"
        values[name] += content
    return {name: {"bytes": len(value.encode("utf-8")), "fingerprint": _hash(value) if value else ""} for name, value in values.items()}


class CodingTelemetry:
    def __init__(self, task_id: str, model_name: str = "", runtime: str = "ollama", engine: str = "UNKNOWN", quantization: str = "UNKNOWN"):
        now=time.time()
        self.record: dict[str, Any] = {
            "task_id": task_id, "task_started_at": now, "task_finished_at": None, "total_task_ms": None,
            "model_runtime": runtime or "UNKNOWN", "model_engine": engine or "UNKNOWN", "model_name": model_name or "UNKNOWN", "model_quantization": quantization or "UNKNOWN",
            "model_call_count": 0, "planner_call_count": 0, "implementer_call_count": 0, "replan_call_count": 0,
            # ``verification_model_call_count`` is retained for compatibility
            # with persisted records; new records count only semantic model
            # verification there and expose the unambiguous role explicitly.
            "verification_model_call_count": 0, "semantic_verification_model_call_count": 0,
            "final_report_model_call_count": 0,
            "input_tokens_total": 0, "output_tokens_total": 0, "input_tokens_known": True, "output_tokens_known": True,
            "model_active_ms": 0.0, "tool_active_ms": 0.0, "verification_ms": 0.0,
            "planner_model_ms": 0.0, "implementer_model_ms": 0.0,
            "replan_model_ms": 0.0, "plan_repair_model_ms": 0.0,
            "semantic_verification_model_ms": 0.0, "final_report_model_ms": 0.0,
            "final_report_total_model_ms": 0.0,
            "final_report_input_tokens": 0, "final_report_output_tokens": 0,
            "final_report_load_ms": 0.0, "final_report_prompt_eval_ms": 0.0,
            "final_report_decode_ms": 0.0,
            "final_report_generation_mode": "NOT_RUN", "final_report_format_ms": 0.0,
            "verification_mode": "UNKNOWN", "verify_evidence_source": "UNKNOWN",
            "deterministic_verify_ms": None,
            "model_load_count": 0, "model_reuse_count": 0,
            "load_ms": 0.0,
            "peak_memory_bytes": _rss(), "process_rss_start": _rss(), "process_rss_peak": _rss(), "process_rss_end": None,
            "swap_used_start": _swap(), "swap_used_end": None, "swap_delta_bytes": None, "calls": [], "phases": [],
            "prefix_cache_eligible": False, "prefix_cache_observed": "UNKNOWN", "stable_prefix_fingerprint": "", "stable_prefix_reuse_bytes": 0,
            "model_provenance": {}, "request_options": {}, "request_options_observed": "UNKNOWN",
            "coding_planner_model_effective": "UNKNOWN", "coding_planner_model_source": "UNKNOWN",
            "coding_implementer_model_effective": "UNKNOWN", "coding_implementer_model_source": "UNKNOWN",
            "application_main_model": "UNKNOWN", "application_main_model_used_for_coding": "UNKNOWN",
        }
        self._last_prefix = ""
        self._last_sections: dict[str, dict[str, Any]] = {}
        self._load_metric_seen = False

    def add_call(self, *, phase_id: str | None, role: str, model: str, messages: list[dict[str, Any]], result: dict[str, Any] | None,
                 started: float, finished: float, structured: bool, thinking: bool, success: bool, failure_class: str = "NONE",
                 model_provenance: dict[str, Any] | None = None) -> dict[str, Any]:
        result = result if isinstance(result, dict) else {}
        for key in ("model_runtime", "model_engine", "model_quantization"):
            if result.get(key): self.record[{"model_runtime":"model_runtime","model_engine":"model_engine","model_quantization":"model_quantization"}[key]] = result[key]
        sections=prompt_sections(messages)
        stable="".join(sections[name]["fingerprint"] for name in ("system_context", "tool_schema", "coding_protocol", "canonical_requirements", "blueprint", "file_context", "knowledge_context", "handoff"))
        stable_match=bool(self._last_prefix and stable == self._last_prefix)
        stable_bytes=sum(sections[name]["bytes"] for name in sections if name != "dynamic_context")
        input_tokens=result.get("prompt_tokens")
        output_tokens=result.get("completion_tokens")
        elapsed=(finished-started)*1000
        call={"call_id": _hash(f"{self.record['task_id']}:{len(self.record['calls'])}:{started}"), "task_id": self.record["task_id"], "phase_id": phase_id or "UNKNOWN", "role": role,
              "model_name": model or self.record["model_name"], "runtime": self.record["model_runtime"], "started_at": started, "finished_at": finished, "elapsed_ms": elapsed,
              "input_tokens": input_tokens if isinstance(input_tokens, int) else "UNKNOWN", "output_tokens": output_tokens if isinstance(output_tokens, int) else "UNKNOWN",
              "prompt_fingerprint": _hash(json.dumps(messages, ensure_ascii=False, sort_keys=True)), "stable_prefix_fingerprint": _hash(stable),
              "stable_prefix_sections": [name for name in sections if name != "dynamic_context" and sections[name]["bytes"]], "stable_prefix_bytes": stable_bytes,
              "dynamic_suffix_bytes": sections["dynamic_context"]["bytes"], "structured_output_requested": structured, "thinking_requested": thinking,
              "success": bool(success), "failure_class": failure_class, "provider_metadata": {key: result.get(key) for key in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration") if key in result},
              "request_options": result.get("request_options", {"status": "NOT_RECORDED"}),
              "model_provenance": model_provenance or result.get("model_provenance", {}),
              "prefix_fingerprint_match": stable_match, "prompt_sections": sections}
        if call["request_options"] != {"status": "NOT_RECORDED"}:
            self.record["request_options_observed"] = "YES"
            self.record.setdefault("request_options", {})[role] = call["request_options"]
        if call["model_provenance"]:
            role_provenance = dict(call["model_provenance"])
            role_provenance.setdefault("role", role)
            self.record.setdefault("model_provenance", {})[role] = role_provenance
            if role == "PLANNER":
                self.record["coding_planner_model_effective"] = role_provenance.get("effective_model", "UNKNOWN")
                self.record["coding_planner_model_source"] = role_provenance.get("source", "UNKNOWN")
            if role == "IMPLEMENTER":
                self.record["coding_implementer_model_effective"] = role_provenance.get("effective_model", "UNKNOWN")
                self.record["coding_implementer_model_source"] = role_provenance.get("source", "UNKNOWN")
            if role in {"PLANNER", "IMPLEMENTER"}:
                self.record["application_main_model"] = role_provenance.get("application_main_model", "UNKNOWN")
                self.record["application_main_model_used_for_coding"] = role_provenance.get("application_main_model_used_for_coding", "UNKNOWN")
        prompt_duration=result.get("prompt_eval_duration"); eval_duration=result.get("eval_duration")
        call["ttft_ms"]="NOT_AVAILABLE"
        call["prefill_tokens_per_sec"]=(input_tokens / (prompt_duration / 1_000_000_000)) if isinstance(input_tokens, (int,float)) and isinstance(prompt_duration, (int,float)) and prompt_duration > 0 else "NOT_AVAILABLE"
        call["decode_tokens_per_sec"]=(output_tokens / (eval_duration / 1_000_000_000)) if isinstance(output_tokens, (int,float)) and isinstance(eval_duration, (int,float)) and eval_duration > 0 else "NOT_AVAILABLE"
        self.record["calls"].append(call)
        self.record["model_call_count"] += 1
        self.record["model_active_ms"] += elapsed
        role_key={"PLANNER":"planner_call_count", "IMPLEMENTER":"implementer_call_count", "REPLANNER":"replan_call_count", "PLAN_REPAIR":"plan_repair_calls", "SEMANTIC_VERIFICATION":"semantic_verification_model_call_count"}.get(role)
        if role_key: self.record[role_key]=self.record.get(role_key, 0)+1
        if role == "SEMANTIC_VERIFICATION":
            self.record["verification_model_call_count"] += 1
        role_ms_key={"PLANNER":"planner_model_ms", "IMPLEMENTER":"implementer_model_ms",
                     "REPLANNER":"replan_model_ms", "PLAN_REPAIR":"plan_repair_model_ms",
                     "SEMANTIC_VERIFICATION":"semantic_verification_model_ms",
                     "FINAL_REPORT":"final_report_model_ms"}.get(role)
        if role_ms_key:
            self.record[role_ms_key] += elapsed
        if role == "FINAL_REPORT":
            self.record["final_report_model_call_count"] += 1
            self.record["final_report_total_model_ms"] += elapsed
            self._add_final_report_metric("final_report_input_tokens", input_tokens)
            self._add_final_report_metric("final_report_output_tokens", output_tokens)
            load_ms = result.get("load_duration_ms")
            if load_ms is None:
                load_ms = self._ns_to_ms(result.get("load_duration"))
            prompt_ms = result.get("prompt_eval_duration")
            decode_ms = result.get("eval_duration")
            self._add_final_report_metric("final_report_load_ms", load_ms)
            self._add_final_report_metric("final_report_prompt_eval_ms", self._ns_to_ms(prompt_ms))
            self._add_final_report_metric("final_report_decode_ms", self._ns_to_ms(decode_ms))
        if isinstance(input_tokens, int): self.record["input_tokens_total"] += input_tokens
        else: self.record["input_tokens_known"] = False
        if isinstance(output_tokens, int): self.record["output_tokens_total"] += output_tokens
        else: self.record["output_tokens_known"] = False
        load_ms=result.get("load_duration_ms")
        if isinstance(load_ms, (int,float)):
            self._load_metric_seen = True
            self.record["load_ms"] += float(load_ms)
            self.record["model_load_count"] += 1 if load_ms > 0 else 0
            self.record["model_reuse_count"] += 1 if load_ms == 0 else 0
        self.record["prefix_cache_eligible"] = len(self.record["calls"]) > 1
        if stable_match:
            self.record["stable_prefix_reuse_bytes"] += stable_bytes
        self.record["stable_prefix_fingerprint"] = _hash(stable)
        rss=_rss()
        if rss is not None: self.record["process_rss_peak"] = max(self.record.get("process_rss_peak") or 0, rss); self.record["peak_memory_bytes"] = self.record["process_rss_peak"]
        self._last_prefix=stable
        self._last_sections=sections
        return call

    @staticmethod
    def _ns_to_ms(value: Any) -> float | None:
        return float(value) / 1_000_000 if isinstance(value, (int, float)) else None

    def _add_final_report_metric(self, key: str, value: Any) -> None:
        if isinstance(value, (int, float)):
            current = self.record.get(key, 0.0)
            if key.endswith("_tokens"):
                self.record[key] = int(current) + int(value) if isinstance(current, (int, float)) else int(value)
            else:
                self.record[key] = float(current) + float(value) if isinstance(current, (int, float)) else float(value)
        elif self.record.get(key) in (0, 0.0):
            self.record[key] = "UNKNOWN"

    def record_verification(self, *, mode: str, evidence_source: str, elapsed_ms: float | None = None) -> None:
        """Record deterministic/semantic verification evidence without a model call."""
        self.record["verification_mode"] = mode or "UNKNOWN"
        self.record["verify_evidence_source"] = evidence_source or "UNKNOWN"
        if isinstance(elapsed_ms, (int, float)):
            current = self.record.get("deterministic_verify_ms")
            self.record["deterministic_verify_ms"] = float(elapsed_ms) + (float(current) if isinstance(current, (int, float)) else 0.0)

    def record_final_report_format(self, elapsed_ms: float, *, generation_mode: str = "DETERMINISTIC") -> None:
        """Record deterministic final-report formatting separately from model time."""
        self.record["final_report_generation_mode"] = generation_mode or "UNKNOWN"
        self.record["final_report_format_ms"] = float(elapsed_ms) if isinstance(elapsed_ms, (int, float)) else "UNKNOWN"

    def finish(self, verify_status: str = "UNKNOWN") -> dict[str, Any]:
        now=time.time(); self.record["task_finished_at"]=now; self.record["total_task_ms"]=(now-self.record["task_started_at"])*1000
        self.record["process_rss_end"]=_rss(); self.record["swap_used_end"]=_swap()
        if isinstance(self.record["swap_used_start"], int) and isinstance(self.record["swap_used_end"], int): self.record["swap_delta_bytes"]=self.record["swap_used_end"]-self.record["swap_used_start"]
        self.record["verify_status"]=verify_status
        rates=[call["prefill_tokens_per_sec"] for call in self.record["calls"] if isinstance(call.get("prefill_tokens_per_sec"), (int,float))]
        decode_rates=[call["decode_tokens_per_sec"] for call in self.record["calls"] if isinstance(call.get("decode_tokens_per_sec"), (int,float))]
        self.record["prefill_tokens_per_sec"]=sum(rates)/len(rates) if rates else "NOT_AVAILABLE"
        self.record["decode_tokens_per_sec"]=sum(decode_rates)/len(decode_rates) if decode_rates else "NOT_AVAILABLE"
        if not self._load_metric_seen:
            self.record["model_load_count"] = "UNKNOWN"; self.record["model_reuse_count"] = "UNKNOWN"
        phase_rows={}
        for call in self.record["calls"]:
            phase=call.get("phase_id") or "UNKNOWN"; row=phase_rows.setdefault(phase, {"phase_id":phase,"phase_model_calls":0,"phase_input_tokens":0,"phase_output_tokens":0,"phase_model_ms":0.0,"phase_tool_ms":0.0,"phase_total_ms":0.0,"phase_retry_count":0})
            row["phase_model_calls"]+=1; row["phase_model_ms"]+=float(call.get("elapsed_ms") or 0); row["phase_total_ms"]+=float(call.get("elapsed_ms") or 0)
            if isinstance(call.get("input_tokens"), int): row["phase_input_tokens"]+=call["input_tokens"]
            else: row["phase_input_tokens"]="UNKNOWN"
            if isinstance(call.get("output_tokens"), int): row["phase_output_tokens"]+=call["output_tokens"]
            else: row["phase_output_tokens"]="UNKNOWN"
        self.record["phases"]=list(phase_rows.values())
        self.record["input_tokens_total"] = self.record["input_tokens_total"] if self.record["input_tokens_known"] else "UNKNOWN"
        self.record["output_tokens_total"] = self.record["output_tokens_total"] if self.record["output_tokens_known"] else "UNKNOWN"
        return self.record
