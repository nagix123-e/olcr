"""Process-local model invocation telemetry.

This is intentionally small and dependency-free.  It is the final accounting
boundary used by the health endpoint and the architecture evidence writer.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict, dataclass
import threading
import time
import math
from typing import Any


LEGACY_MODEL_MARKERS = ("qwen3.5", "qwen3:14b", "lfm", "reranker")
RECENT_CALL_LIMIT = 500
ACTION_ROUTER_MECHANISMS = frozenset({
    "native_structured", "playwright", "accessibility", "verified_routine",
    "keyboard_shortcut", "deterministic_input", "vision_pointer",
})
NATIVE_EXECUTION_MECHANISMS = frozenset(ACTION_ROUTER_MECHANISMS - {"playwright"})
PLAYWRIGHT_ACTIONS = frozenset({
    "navigate", "navigate_back", "load_more", "scroll", "wait_for_text", "assert_text",
})
VISION_REASONS = frozenset({
    "GUI_GROUNDING", "GUI_GROUNDING_WARMUP", "ATTACHMENT_ANALYSIS",
    "VISION_FORMAT_RETRY", "OTHER",
})


def _bounded_duration_ms(value: Any) -> float:
    duration = value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
    try:
        duration = float(duration)
    except (OverflowError, ValueError):
        duration = 0.0
    return max(0.0, min(duration, 86_400_000.0)) if math.isfinite(duration) else 0.0


@dataclass(frozen=True)
class Invocation:
    timestamp: float
    task_id: str
    phase_id: str
    stage: str
    role: str
    model: str
    runtime: str
    revision: str
    status: str


class ModelTelemetry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Keep aggregate counters for the process lifetime, but bound diagnostic
        # detail so a long-running local backend cannot grow memory per call.
        self._calls: deque[Invocation] = deque(maxlen=RECENT_CALL_LIMIT)
        self._counters: Counter[str] = Counter()

    def record(self, *, task_id: str = "", phase_id: str = "", stage: str = "",
               role: str = "", model: str = "", runtime: str = "",
               revision: str = "", status: str = "success") -> None:
        call = Invocation(time.time(), task_id, phase_id, stage, role, model,
                          runtime, revision, status)
        with self._lock:
            self._calls.append(call)
            self._counters["model_invocations_total"] += 1
            if model.lower().startswith("qwen3.5") or model.lower() == "qwen3:14b":
                self._counters["qwen3_5"] += 1
            if "lfm" in model.lower():
                self._counters["lfm"] += 1
            if "reranker" in model.lower() or "rerank" in role.lower():
                self._counters["qwen3_reranker"] += 1
            if stage == "LEGACY_API_ROUTER":
                self._counters["legacy_api_router"] += 1
            if role == "unexpected" or not role:
                self._counters["unexpected_model_calls"] += 1

    def record_action_router_decision(self, decision: dict[str, Any]) -> None:
        """Aggregate router decisions without retaining per-decision events."""
        with self._lock:
            self._counters["action_router_decisions"] += 1
            if decision.get("status") != "PASS":
                self._counters["action_router_blocked_decisions"] += 1
                return
            mechanism = decision.get("mechanism")
            if isinstance(mechanism, str) and mechanism in ACTION_ROUTER_MECHANISMS:
                self._counters[f"action_router_{mechanism}_decisions"] += 1

    def record_action_verification(self, result: Any) -> None:
        """Aggregate only explicit postcondition outcomes; unknown stays inconclusive."""
        if isinstance(result, dict):
            outcome = result.get("result")
        else:
            outcome = result
        if outcome not in ("CONFIRMED", "NOT_CONFIRMED", "INCONCLUSIVE"):
            outcome = "INCONCLUSIVE"
        with self._lock:
            self._counters["action_verification_attempts"] += 1
            key = {
                "CONFIRMED": "actions_verified",
                "NOT_CONFIRMED": "actions_not_confirmed",
                "INCONCLUSIVE": "verification_inconclusive",
            }[outcome]
            self._counters[key] += 1

    def record_action_execution(self, mechanism: str, *, routine: bool = False) -> None:
        """Count a receipt-backed dispatch by a closed mechanism allowlist."""
        if mechanism not in NATIVE_EXECUTION_MECHANISMS:
            return
        with self._lock:
            for key in ("native_actions", f"action_{mechanism}_executions"):
                self._counters[key] = min(self._counters[key] + 1, 2**63 - 1)
            if mechanism == "accessibility":
                self._counters["accessibility_actions"] = min(self._counters["accessibility_actions"] + 1, 2**63 - 1)
            if mechanism == "keyboard_shortcut":
                self._counters["shortcut_actions"] = min(self._counters["shortcut_actions"] + 1, 2**63 - 1)
            if mechanism in {"deterministic_input", "vision_pointer"}:
                self._counters["pointer_actions"] = min(self._counters["pointer_actions"] + 1, 2**63 - 1)
            if mechanism == "vision_pointer":
                self._counters["vision_grounded_actions"] = min(self._counters["vision_grounded_actions"] + 1, 2**63 - 1)
            if routine:
                self._counters["routine_actions"] = min(self._counters["routine_actions"] + 1, 2**63 - 1)

    def record_playwright_action(self, action: str, *, successful: bool) -> None:
        """Count managed-browser actions and evidence checks without retaining content."""
        if action not in PLAYWRIGHT_ACTIONS:
            return
        with self._lock:
            self._counters["playwright_action_attempts"] = min(
                self._counters["playwright_action_attempts"] + 1, 2**63 - 1)
            self._counters[f"playwright_{action}_attempts"] = min(
                self._counters[f"playwright_{action}_attempts"] + 1, 2**63 - 1)
            key = "playwright_actions" if successful else "playwright_action_failures"
            self._counters[key] = min(self._counters[key] + 1, 2**63 - 1)
            outcome_key = f"playwright_{action}_{'successes' if successful else 'failures'}"
            self._counters[outcome_key] = min(self._counters[outcome_key] + 1, 2**63 - 1)

    def record_routine_compilation(self) -> None:
        with self._lock:
            for key in ("routine_candidates", "routines_compiled"):
                self._counters[key] = min(self._counters[key] + 1, 2**63 - 1)

    def record_routine_match(self, match_count: Any) -> None:
        with self._lock:
            self._counters["routine_match_attempts"] = min(
                self._counters["routine_match_attempts"] + 1, 2**63 - 1)
            key = "routine_matches" if match_count == 1 else "routine_fallbacks"
            self._counters[key] = min(self._counters[key] + 1, 2**63 - 1)

    def record_routine_outcome(self, *, success: bool) -> None:
        with self._lock:
            self._counters["routine_runs"] = min(self._counters["routine_runs"] + 1, 2**63 - 1)
            key = "routine_successes" if success else "routine_failures"
            self._counters[key] = min(self._counters[key] + 1, 2**63 - 1)

    def record_vision_call(self, *, reason: str, scope: str, pixel_count: Any,
                           duration_ms: Any, status: str = "returned") -> None:
        """Aggregate bounded vision cost signals without retaining image or call data."""
        reason_key = reason if reason in VISION_REASONS else "OTHER"
        scope_key = scope if scope in {"full_screen", "roi"} else "unknown"
        pixels = pixel_count if isinstance(pixel_count, int) and not isinstance(pixel_count, bool) else 0
        pixels = max(0, min(pixels, 50_000_000))
        duration = _bounded_duration_ms(duration_ms)
        with self._lock:
            self._counters["vision_calls"] = min(self._counters["vision_calls"] + 1, 2**63 - 1)
            self._counters[f"vision_calls_reason_{reason_key.lower()}"] = min(
                self._counters[f"vision_calls_reason_{reason_key.lower()}" ] + 1, 2**63 - 1)
            if scope_key == "full_screen":
                self._counters["vision_fullscreen_calls"] = min(self._counters["vision_fullscreen_calls"] + 1, 2**63 - 1)
            elif scope_key == "roi":
                self._counters["vision_roi_calls"] = min(self._counters["vision_roi_calls"] + 1, 2**63 - 1)
            self._counters["vision_pixels_total"] = min(self._counters["vision_pixels_total"] + pixels, 2**63 - 1)
            self._counters["vision_inference_ms"] = min(self._counters["vision_inference_ms"] + duration, 2**63 - 1)
            if status != "returned":
                self._counters["vision_call_errors"] = min(self._counters["vision_call_errors"] + 1, 2**63 - 1)

    def record_ocr_call(self, duration_ms: Any, *, status: str = "returned") -> None:
        duration = _bounded_duration_ms(duration_ms)
        with self._lock:
            self._counters["ocr_calls"] = min(self._counters["ocr_calls"] + 1, 2**63 - 1)
            self._counters["ocr_ms"] = min(self._counters["ocr_ms"] + duration, 2**63 - 1)
            if status != "returned":
                self._counters["ocr_call_errors"] = min(self._counters["ocr_call_errors"] + 1, 2**63 - 1)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            counters.update({
                "qwen3_5_runtime_calls": counters.get("qwen3_5", 0),
                "lfm_runtime_calls": counters.get("lfm", 0),
                "qwen3_reranker_runtime_calls": counters.get("qwen3_reranker", 0),
                "legacy_api_router_calls": counters.get("legacy_api_router", 0),
            })
            vision_by_reason = {
                reason: counters.get(f"vision_calls_reason_{reason.lower()}", 0)
                for reason in sorted(VISION_REASONS)
            }
            return {"counters": counters,
                    "vision_calls_by_reason": vision_by_reason,
                    "legacy_zero": {name: counters.get(name, 0) == 0 for name in
                                    ("qwen3_5", "lfm", "qwen3_reranker", "legacy_api_router")},
                    "calls": [asdict(row) for row in self._calls]}

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()
            self._counters.clear()


model_telemetry = ModelTelemetry()
