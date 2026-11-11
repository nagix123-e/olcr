"""Stable provider contracts used by the OLCR orchestration plane."""
from __future__ import annotations

import math
import time
from typing import Any, Protocol, Iterable

from .mcp_runtime import MCPRuntime
from .model_telemetry import model_telemetry
from .vision import ScreenState


def _finite_number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


class DecisionProvider(Protocol):
    def choice(self, *, question: str, candidates: list[dict[str, Any]], language: str = "multilingual") -> dict[str, Any]: ...
    def noul(self, *, question: str, candidates: list[dict[str, Any]], language: str = "multilingual") -> dict[str, Any]: ...
    def score(self, *, question: str, candidates: list[dict[str, Any]], language: str = "multilingual") -> dict[str, Any]: ...


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str], model: str) -> list[list[float]]: ...


class SkillProvider(Protocol):
    def registry(self) -> list[dict[str, Any]]: ...
    def lock(self) -> dict[str, Any]: ...


class PluginProvider(Protocol):
    def start(self) -> str: ...
    def close(self) -> None: ...
    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]: ...


class ObservationProvider(Protocol):
    def observe(self) -> dict[str, Any]: ...


class ActionExecutor(Protocol):
    def execute(self, action: dict[str, Any]) -> dict[str, Any]: ...


class DeterministicActionExecutor:
    """Allow-listed semantic action executor; coordinates are never generated here."""
    def __init__(self, handlers: dict[str, Any] | None = None):
        self.handlers = dict(handlers or {})

    def execute(self, action: dict[str, Any]) -> dict[str, Any]:
        kind = str(action.get("action") or action.get("type") or "")
        handler = self.handlers.get(kind)
        if handler is None:
            return {"status": "BLOCKED", "reason": "ACTION_NOT_ALLOWLISTED", "action": kind}
        return {"status": "PASS", "action": kind, "result": handler(action)}


class ActionRouter:
    """Select the cheapest available action mechanism that is safe and verifiable."""

    MECHANISM_ORDER = (
        "native_structured", "playwright", "accessibility", "verified_routine",
        "keyboard_shortcut", "deterministic_input", "vision_pointer",
    )
    _COST_ORDER = {
        "native_structured": 0,
        "playwright": 1,
        "accessibility": 2,
        "verified_routine": 3,
        "keyboard_shortcut": 4,
        "deterministic_input": 5,
        "vision_pointer": 6,
    }

    def __init__(self, *, min_reliability: float = 0.75, max_state_age_seconds: float = 30.0):
        self.min_reliability = min_reliability
        self.max_state_age_seconds = max_state_age_seconds

    def route(self, *, task_semantics: str, candidates: list[dict[str, Any]],
              now: float | None = None) -> dict[str, Any]:
        """Reject unavailable, mismatched, stale, unverifiable, or unsafe mechanisms."""
        if not task_semantics.strip():
            decision = {"status": "BLOCKED", "reason": "ACTION_SEMANTICS_REQUIRED", "considered": []}
            model_telemetry.record_action_router_decision(decision)
            return decision
        current = time.time() if now is None else now
        considered: list[dict[str, Any]] = []
        viable: list[tuple[float, int, float, dict[str, Any], dict[str, Any]]] = []
        for candidate in candidates[:16]:
            if not isinstance(candidate, dict):
                continue
            mechanism = str(candidate.get("mechanism") or "")
            rejected: list[str] = []
            if mechanism not in self._COST_ORDER:
                rejected.append("MECHANISM_NOT_SUPPORTED")
            if candidate.get("available") is not True:
                rejected.append("MECHANISM_UNAVAILABLE")
            if candidate.get("semantics_compatible") is not True:
                rejected.append("TASK_SEMANTICS_MISMATCH")
            observed_at = _finite_number(candidate.get("observed_at"))
            if (observed_at is None or observed_at <= 0
                    or current - observed_at > self.max_state_age_seconds
                    or observed_at > current + 1.0):
                rejected.append("CURRENT_STATE_STALE_OR_UNBOUND")
            if not isinstance(candidate.get("observation_id"), str) or not candidate["observation_id"]:
                rejected.append("OBSERVATION_ID_REQUIRED")
            if not isinstance(candidate.get("session_id"), str) or not candidate["session_id"]:
                rejected.append("OBSERVATION_SESSION_REQUIRED")
            if candidate.get("verification_available") is not True:
                rejected.append("POSTCONDITION_VERIFICATION_UNAVAILABLE")
            if candidate.get("security_allowed") is not True:
                rejected.append("SECURITY_POLICY_REJECTED")
            if mechanism == "vision_pointer":
                # A qualified visual bbox is still not an input coordinate.
                # Keep this route unavailable until the caller has joined the
                # captured frame to a verified display transform and current
                # target identity, and has an independent post-action check.
                if candidate.get("grounding_provenance_bound") is not True:
                    rejected.append("VISION_GROUNDING_PROVENANCE_UNBOUND")
                if candidate.get("coordinate_transform_verified") is not True:
                    rejected.append("VISION_COORDINATE_TRANSFORM_UNVERIFIED")
                if candidate.get("target_identity_bound") is not True:
                    rejected.append("VISION_TARGET_IDENTITY_UNBOUND")
                if candidate.get("independent_postcondition") is not True:
                    rejected.append("VISION_POSTCONDITION_UNAVAILABLE")
            reliability = _finite_number(candidate.get("reliability"))
            if reliability is None or not 0 <= reliability <= 1:
                rejected.append("RELIABILITY_INVALID")
            elif reliability < self.min_reliability:
                rejected.append("RELIABILITY_BELOW_THRESHOLD")
            expected_cost = _finite_number(candidate.get("expected_cost", self._COST_ORDER.get(mechanism, math.inf)))
            if expected_cost is None or expected_cost < 0:
                rejected.append("EXPECTED_COST_INVALID")
            item = {"mechanism": mechanism or "UNKNOWN", "rejected_reasons": rejected}
            considered.append(item)
            if not rejected and mechanism in self._COST_ORDER:
                viable.append((expected_cost, self._COST_ORDER[mechanism], -reliability, candidate, item))
        if any(candidate.get("mechanism") != "vision_pointer"
               for _cost, _order, _reliability, candidate, _item in viable):
            # Vision is the terminal fallback even if a caller estimates its
            # cost below an available structured, AX, routine, shortcut, or
            # deterministic route. Cost ranking applies after this policy gate.
            for _cost, _order, _reliability, candidate, item in viable:
                if candidate.get("mechanism") == "vision_pointer":
                    item["rejected_reasons"].append("PREFERRED_ACTION_ROUTE_AVAILABLE")
            viable = [entry for entry in viable if entry[3].get("mechanism") != "vision_pointer"]
        if not viable:
            reason = "NO_ACTION_MECHANISMS_AVAILABLE" if not considered else "NO_RELIABLE_SAFE_VERIFIABLE_MECHANISM"
            decision = {"status": "BLOCKED", "reason": reason, "task_semantics": task_semantics,
                        "considered": considered}
            model_telemetry.record_action_router_decision(decision)
            return decision
        _, _, _, chosen, _ = min(viable, key=lambda item: (item[0], item[1], item[2]))
        decision = {"status": "PASS", "mechanism": chosen["mechanism"],
                    "reliability": float(chosen["reliability"]), "task_semantics": task_semantics,
                    "verification_required": True, "observation_id": chosen["observation_id"],
                    "session_id": chosen["session_id"], "observed_at": float(chosen["observed_at"]),
                    "considered": considered}
        model_telemetry.record_action_router_decision(decision)
        return decision


class KeyboardShortcutRegistry:
    """Build ephemeral, provenance-bound shortcut affordances from observed app menus."""

    _FIND_LABELS = {"find", "find…", "find...", "search", "search…", "search...", "検索", "検索…", "検索..."}

    @classmethod
    def find_actions(cls, *, elements: Iterable[dict[str, Any]], task_semantics: str) -> list[dict[str, Any]]:
        if task_semantics != "open_find":
            return []
        actions: list[dict[str, Any]] = []
        for element in elements:
            if not isinstance(element, dict) or element.get("role") != "AXMenuItem":
                continue
            label = str(element.get("label") or "").strip()
            element_id = str(element.get("id") or "").strip()
            if not element_id or label.casefold() not in cls._FIND_LABELS:
                continue
            shortcut = element.get("keyboard_shortcut", element.get("keyboardShortcut"))
            if not isinstance(shortcut, dict):
                continue
            provenance = shortcut.get("provenance")
            key_code = shortcut.get("key_code", shortcut.get("keyCode"))
            modifiers = shortcut.get("modifiers")
            key = shortcut.get("key")
            if (provenance != "application_menu"
                    or not isinstance(key_code, int) or isinstance(key_code, bool) or not 0 <= key_code <= 127
                    or not isinstance(modifiers, int) or isinstance(modifiers, bool) or not 0 <= modifiers <= 15
                    or (modifiers & 8 != 0 and modifiers & 7 != 0)
                    or not isinstance(key, str) or not key or len(key) > 32):
                continue
            actions.append({"tool_id": f"shortcut:{element_id}", "action": task_semantics,
                            "element_id": element_id, "label": label,
                            "shortcut": {"key_code": key_code, "modifiers": modifiers, "key": key,
                                         "provenance": provenance},
                            "criteria": f"application menu action {label} with observed shortcut {key}"})
            if len(actions) >= 8:
                break
        return actions


class MCPPluginProvider:
    """Owned lazy MCP lifecycle adapter; no orphan process escapes its owner."""
    def __init__(self, runtime: MCPRuntime):
        self.runtime = runtime
        self.owner = f"mcp:{id(runtime)}"

    def start(self) -> str:
        self.runtime.start()
        return self.runtime.state

    def close(self) -> None:
        self.runtime.close()

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.runtime.call(tool, arguments)


class SemanticActionRuntime:
    """Turns grounded element ids into semantic actions before execution."""
    def __init__(self, decision_provider: DecisionProvider, executor: ActionExecutor):
        self.decision_provider = decision_provider
        self.executor = executor

    @staticmethod
    def candidates(state: ScreenState, subgoal: str) -> list[dict[str, Any]]:
        candidates = []
        for element in state.elements:
            element_id = str(element.get("id") or element.get("element_id") or "")
            if not element_id:
                continue
            role = str(element.get("role") or "element")
            if role not in {"button", "link", "disclosure", "menuitem", "tab"} or element.get("enabled") is False:
                continue
            label = str(element.get("label") or element.get("name") or element_id)
            candidates.append({"tool_id": f"click:{element_id}", "criteria": f"{role} {label} for {subgoal}",
                              "action": "click", "element_id": element_id})
        return candidates[:8]

    def select_candidates(self, candidates: list[dict[str, Any]], subgoal: str) -> dict[str, Any]:
        if not candidates:
            return {"status": "BLOCKED", "reason": "NO_GROUNDED_SEMANTIC_ACTION"}
        decision = self.decision_provider.choice(question=subgoal, candidates=candidates)
        selected = next((row for row in candidates if row["tool_id"] == decision.get("tool_id")), None)
        if not selected:
            return {"status": "BLOCKED", "reason": "INVALID_SEMANTIC_DECISION"}
        return {"status": "PASS", "semantic_action": selected, "decision": decision}

    def choose_and_execute(self, state: ScreenState, subgoal: str) -> dict[str, Any]:
        candidates = self.candidates(state, subgoal)
        selection = self.select_candidates(candidates, subgoal)
        if selection.get("status") != "PASS":
            return selection
        result = self.executor.execute(selection["semantic_action"])
        return {"status": result.get("status", "FAILED"), **selection, "executor_result": result}

    @staticmethod
    def verify_post_action(before: ScreenState, after: ScreenState, element_id: str, *,
                           expected_elements_present: tuple[str, ...] = (),
                           expected_elements_absent: tuple[str, ...] = (),
                           expected_text_contains: tuple[str, ...] = (),
                           expected_text_absent: tuple[str, ...] = (),
                           expected_element_states: tuple[tuple[str, str, Any], ...] = ()) -> dict[str, Any]:
        def inconclusive(reason: str) -> dict[str, Any]:
            return {"verified": False, "result": "INCONCLUSIVE", "stale_state_rejected": True,
                    "expected_postcondition_satisfied": False, "before_source": before.source,
                    "after_source": after.source, "reason": reason}

        before_ids = {str(item.get("id") or item.get("element_id")) for item in before.elements}
        after_ids = {str(item.get("id") or item.get("element_id")) for item in after.elements}
        requirements = (expected_elements_present, expected_elements_absent,
                        expected_text_contains, expected_text_absent, expected_element_states)
        has_postcondition = any(requirements)

        def matches(state: ScreenState) -> bool:
            ids = {str(item.get("id") or item.get("element_id")) for item in state.elements}
            text = "\n".join(state.text)
            return (all(item in ids for item in expected_elements_present)
                    and all(item not in ids for item in expected_elements_absent)
                    and all(item in text for item in expected_text_contains)
                    and all(item not in text for item in expected_text_absent)
                    and all(any(str(element.get("id") or element.get("element_id")) == target
                                and element.get(attribute) == expected
                                for element in state.elements)
                            for target, attribute, expected in expected_element_states))

        grounded_before = element_id in before_ids
        if not before.session_id or not after.session_id:
            return inconclusive("OBSERVATION_SESSION_REQUIRED")
        if before.session_id != after.session_id:
            return inconclusive("OBSERVATION_SESSION_MISMATCH")
        if (before.coordinate_space and after.coordinate_space
                and before.coordinate_space != after.coordinate_space):
            return inconclusive("OBSERVATION_COORDINATE_SPACE_MISMATCH")
        if not before.source or before.source != after.source:
            return inconclusive("OBSERVATION_SOURCE_MISMATCH")
        if before.source == "AX":
            valid_pid = lambda value: (isinstance(value, int)
                                       and not isinstance(value, bool) and value > 0)
            valid_bundle = lambda value: isinstance(value, str) and bool(value.strip())
            if (before.permission_granted is not True or after.permission_granted is not True
                    or not valid_pid(before.application_pid) or not valid_pid(after.application_pid)
                    or after.application_pid != before.application_pid
                    or not valid_bundle(before.application_bundle_id)
                    or after.application_bundle_id != before.application_bundle_id):
                return inconclusive("AX_APPLICATION_IDENTITY_MISMATCH")
        if (before.source in {"AX", "IMAGE"} and (not before.coordinate_space or not after.coordinate_space)):
            return inconclusive("OBSERVATION_COORDINATE_SPACE_REQUIRED")
        if (not before.observation_id or not after.observation_id
                or not before.observed_at or not after.observed_at):
            return inconclusive("OBSERVATION_PROVENANCE_REQUIRED")
        if not math.isfinite(before.observed_at) or not math.isfinite(after.observed_at):
            return inconclusive("OBSERVATION_TIMESTAMP_INVALID")
        if after.observed_at <= before.observed_at:
            return inconclusive("POST_ACTION_OBSERVATION_NOT_NEWER")
        if before.observation_id == after.observation_id:
            return inconclusive("POST_ACTION_OBSERVATION_REPLAYED")
        now = time.time()
        if before.observed_at > now + 2.0 or after.observed_at > now + 2.0:
            return inconclusive("OBSERVATION_TIMESTAMP_IN_FUTURE")
        if now - before.observed_at > 30.0 or now - after.observed_at > 30.0:
            return inconclusive("OBSERVATION_STALE")
        if (before.confidence < 0.65 or after.confidence < 0.65
                or before.uncertainty or after.uncertainty):
            return inconclusive("OBSERVATION_COVERAGE_INSUFFICIENT")
        before_matches = matches(before) if has_postcondition else False
        after_matches = matches(after) if has_postcondition else False
        verified = grounded_before and has_postcondition and not before_matches and after_matches
        if not grounded_before:
            reason = "TARGET_NOT_GROUNDED_IN_PRE_ACTION_STATE"
        elif not has_postcondition:
            reason = "EXPECTED_POSTCONDITION_REQUIRED"
        elif before_matches:
            reason = "POSTCONDITION_NOT_A_NEW_STATE_TRANSITION"
        elif not after_matches:
            reason = "EXPECTED_POSTCONDITION_NOT_OBSERVED"
        else:
            reason = "EXPECTED_STATE_TRANSITION_VERIFIED"
        return {"verified": verified,
                "result": "CONFIRMED" if verified else "NOT_CONFIRMED",
                "stale_state_rejected": element_id not in after_ids,
                "expected_postcondition_satisfied": after_matches,
                "before_source": before.source, "after_source": after.source,
                "reason": reason}


class ObservationAdapter:
    def __init__(self, observer: Any):
        self.observer = observer

    def observe(self) -> dict[str, Any]:
        value = self.observer() if callable(self.observer) else self.observer
        return value if isinstance(value, dict) else {"status": "INVALID_OBSERVATION"}
