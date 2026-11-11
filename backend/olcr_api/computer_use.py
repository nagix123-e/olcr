"""Structured, read-only computer-use observation helpers."""
from __future__ import annotations

import math
import re
import time
from typing import Any

from .providers import (
    ActionRouter,
    DeterministicActionExecutor,
    KeyboardShortcutRegistry,
    SemanticActionRuntime,
    _finite_number,
)
from .vision import DOMAXObservationProvider


def resolve_domax_question(question: str, observation: dict[str, Any], decision_provider: Any) -> dict[str, Any]:
    """Select a visible semantic item from a fresh DOM/AX observation.

    This path is intentionally observation-only: it never accepts coordinates,
    invokes a Vision model, or performs a UI action.
    """
    if not isinstance(observation, dict):
        return {"status": "BLOCKED", "reason": "DOMAX_OBSERVATION_REQUIRED"}
    provider = DOMAXObservationProvider()
    state = provider.observe(dom=observation.get("dom"), ax=observation.get("ax"))
    if state.source not in {"DOM", "AX"}:
        return {"status": "BLOCKED", "reason": "DOMAX_STATE_REQUIRED", "observation_source": state.source}
    if not question.strip():
        return {"status": "BLOCKED", "reason": "QUESTION_REQUIRED", "observation_source": state.source}

    candidates: list[dict[str, Any]] = []
    by_tool_id: dict[str, dict[str, Any]] = {}
    for element in state.elements:
        element_id = str(element.get("id") or "").strip()
        label = str(element.get("label") or element.get("text") or "").strip()
        role = str(element.get("role") or "element").strip()
        if not element_id or not label or element.get("visible") is False:
            continue
        tool_id = f"read:{element_id}"
        level = element.get("level")
        level_text = f" level {level}" if isinstance(level, int) and not isinstance(level, bool) else ""
        row = {"tool_id": tool_id, "criteria": f"{role}{level_text}: {label}"}
        candidates.append(row)
        by_tool_id[tool_id] = {"id": element_id, "role": role, "label": label, "level": level}
        if len(candidates) == 8:
            break
    if not candidates:
        return {"status": "BLOCKED", "reason": "NO_GROUNDED_DOMAX_ELEMENTS", "observation_source": state.source}
    # Prefer the semantic role explicitly named by a read question. This keeps a
    # generic status badge (for example READY) from outranking an actual page
    # heading when the user asks for the heading. The chosen item is still
    # grounded in the same fresh DOM/AX snapshot; model choice remains available
    # when several candidates share the requested role.
    normalized_question = question.casefold()
    preferred_roles: tuple[str, ...] = ()
    if any(term in normalized_question for term in ("heading", "title")):
        preferred_roles = ("heading",)
    elif "status" in normalized_question:
        preferred_roles = ("status",)
    if preferred_roles:
        preferred_ids = {
            f"read:{element['id']}"
            for element in by_tool_id.values()
            if element["role"].casefold() in preferred_roles
        }
        preferred_candidates = [item for item in candidates if item["tool_id"] in preferred_ids]
        if preferred_candidates and "project" in normalized_question and preferred_roles == ("heading",):
            level_one_ids = {
                f"read:{element['id']}"
                for element in by_tool_id.values()
                if element["role"].casefold() == "heading" and element.get("level") == 1
            }
            level_one_candidates = [item for item in preferred_candidates if item["tool_id"] in level_one_ids]
            if level_one_candidates:
                preferred_candidates = level_one_candidates
        if preferred_candidates:
            candidates = preferred_candidates
    if len(candidates) == 1:
        decision = {"tool_id": candidates[0]["tool_id"], "selection_source": "SINGLE_GROUNDED_CANDIDATE"}
    else:
        decision = decision_provider.choice(question=question[:1000], candidates=candidates, language="multilingual")
    selected = by_tool_id.get(str(decision.get("tool_id") or ""))
    if selected is None:
        return {"status": "BLOCKED", "reason": "INVALID_SEMANTIC_DECISION", "observation_source": state.source}
    return {
        "status": "PASS",
        "answer": selected["label"],
        "selected_element": selected,
        "observation_source": state.source,
        "vision_model_calls": 0,
        "action_executed": False,
        "decision": decision,
    }


def _vision_pointer_action_evidence(observation: dict[str, Any], element: dict[str, Any],
                                    *, now: float | None = None) -> bool:
    """Validate UI-Venus and tracked-frame evidence for one exact AX target.

    This evidence only makes a route planable. The native executor must recompute
    the frame-pair transform and repeat identity/geometry checks immediately before
    posting input; client-supplied metadata is never execution authority.
    """
    evidence = observation.get("vision_pointer")
    if not isinstance(evidence, dict):
        return False
    candidate = evidence.get("candidate")
    provenance = evidence.get("provenance")
    alignment = evidence.get("alignment")
    ax = observation.get("ax")
    if not all(isinstance(value, dict) for value in (candidate, provenance, alignment, ax)):
        return False

    label = str(element.get("label") or element.get("text") or "").strip()
    candidate_label = candidate.get("label")
    bbox = candidate.get("bbox")
    confidence = _finite_number(candidate.get("confidence"))
    if (not label or not isinstance(candidate_label, str)
            or candidate_label.strip().casefold() != label.casefold()
            or not isinstance(bbox, (list, tuple)) or len(bbox) != 4
            or any(not isinstance(value, (int, float)) or isinstance(value, bool)
                   or not 0 <= value <= 1 or not math.isfinite(float(value)) for value in bbox)
            or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]
            or confidence is None or not 0.65 <= confidence <= 1):
        return False
    # UI-Venus currently binds candidates by accessible label, not by the
    # native AX element id. Do not let a same-labelled AX element make a
    # visually tracked box appear to identify one specific action target.
    ax_elements = ax.get("elements")
    if not isinstance(ax_elements, list):
        return False
    matching_elements = [item for item in ax_elements
        if isinstance(item, dict)
        and str(item.get("label") or item.get("text") or "").strip().casefold() == label.casefold()]
    if (len(matching_elements) != 1
            or str(matching_elements[0].get("id") or "").strip() != str(element.get("id") or "").strip()):
        return False
    pid = ax.get("applicationPid")
    bundle_id = ax.get("applicationBundleId")
    target = provenance.get("target_application")
    geometry = provenance.get("display_geometry")
    aligned_target = alignment.get("targetApplication")
    capture_session_id = provenance.get("capture_session_id")
    frame_sequence = provenance.get("frame_sequence")
    tracked_sequence = provenance.get("tracked_frame_sequence")
    observed_at = provenance.get("observed_at")
    verified_at = alignment.get("verifiedAt")
    display_id = geometry.get("displayId") if isinstance(geometry, dict) else None
    global_point = alignment.get("mappedGlobalPoint")
    image_width = provenance.get("width")
    image_height = provenance.get("height")
    current = _finite_number(time.time() if now is None else now)
    if current is None:
        return False
    if (ax.get("permissionGranted") is not True
            or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not isinstance(bundle_id, str) or not bundle_id.strip()
            or not isinstance(target, dict)
            or target.get("applicationPid") != pid
            or target.get("applicationBundleId") != bundle_id
            or alignment.get("status") != "READY"
            or not isinstance(aligned_target, dict)
            or aligned_target.get("applicationPid") != pid
            or aligned_target.get("applicationBundleId") != bundle_id
            or not isinstance(capture_session_id, str) or not capture_session_id
            or capture_session_id != alignment.get("captureSessionId")
            or not isinstance(frame_sequence, int) or isinstance(frame_sequence, bool) or frame_sequence <= 0
            or not isinstance(tracked_sequence, int) or isinstance(tracked_sequence, bool)
            or alignment.get("seedSequence") != frame_sequence
            or alignment.get("verifiedSequence") != tracked_sequence
            or tracked_sequence <= frame_sequence
            or not isinstance(display_id, int) or isinstance(display_id, bool) or display_id <= 0
            or alignment.get("displayId") != display_id
            or alignment.get("targetWindowContainsPoint") is not True
            # Match the native capture/tracker limits. Retina and large desktop
            # captures routinely exceed 1920x1080 even when their point geometry is smaller.
            or not isinstance(image_width, int) or isinstance(image_width, bool) or not 1 <= image_width <= 8192
            or not isinstance(image_height, int) or isinstance(image_height, bool) or not 1 <= image_height <= 8192
            or image_width * image_height > 50_000_000
            or not isinstance(global_point, (list, tuple)) or len(global_point) != 2
            or alignment.get("trackingSource") not in {"APPLE_VISION_OBJECT_TRACKING", "OPENCV_LUCAS_KANADE"}):
        return False
    point_x, point_y = _finite_number(global_point[0]), _finite_number(global_point[1])
    display_values = [_finite_number(geometry.get(key)) for key in
                      ("originX", "originY", "widthPoints", "heightPoints")]
    if (point_x is None or point_y is None or any(value is None for value in display_values)
            or abs(point_x) > 100_000 or abs(point_y) > 100_000
            or display_values[2] <= 0 or display_values[3] <= 0
            or display_values[2] > 32_768 or display_values[3] > 32_768
            or not display_values[0] <= point_x < display_values[0] + display_values[2]
            or not display_values[1] <= point_y < display_values[1] + display_values[3]):
        return False
    tracking_confidence = _finite_number(alignment.get("trackingConfidence"))
    observed_at = _finite_number(observed_at)
    verified_at = _finite_number(verified_at)
    if (tracking_confidence is None or not 0.75 <= tracking_confidence <= 1
            or observed_at is None or observed_at <= 0
            or verified_at is None
            # UI-Venus/OCR grounds the pinned seed before tracking runs, so the
            # seed may be older than the final tracked frame by the time this
            # local plan request arrives. Match the native executor's bounded
            # ten-second seed window; the tracked frame remains limited to
            # 2.5 s and native execution recomputes the transform before input.
            or observed_at > current + 1.0 or current - observed_at > 10.0
            # The native executor re-tracks and revalidates the point immediately
            # before input. Keep the plan evidence within the same bounded frame
            # window as its seed; a 1 s limit was intermittently expiring between
            # the native two-frame tracker and this local planning request.
            or verified_at > current + 1.0 or current - verified_at > 2.5):
        return False
    image_hash = provenance.get("image_sha256")
    if (not isinstance(image_hash, str) or len(image_hash) != 64
            or any(character not in "0123456789abcdef" for character in image_hash)):
        return False
    return True


def _has_usable_ax_frame(element: dict[str, Any]) -> bool:
    frame = element.get("frame")
    if not isinstance(frame, dict):
        return False
    values = [frame.get(key) for key in ("x", "y", "width", "height")]
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in values):
        return False
    try:
        numbers = [float(value) for value in values]
    except (OverflowError, ValueError):
        return False
    return all(math.isfinite(value) for value in numbers) and numbers[2] > 0 and numbers[3] > 0


def plan_disclosure_open(question: str, observation: dict[str, Any], decision_provider: Any) -> dict[str, Any]:
    """Choose only a visible, currently closed disclosure for a reversible open action."""
    if not isinstance(observation, dict):
        return {"status": "BLOCKED", "reason": "DOMAX_OBSERVATION_REQUIRED"}
    state = DOMAXObservationProvider().observe(dom=observation.get("dom"), ax=observation.get("ax"))
    if state.source not in {"DOM", "AX"}:
        return {"status": "BLOCKED", "reason": "DOMAX_STATE_REQUIRED", "observation_source": state.source}
    pointer_access = observation.get("pointer_event_permission_granted") is True
    ax_payload = observation.get("ax") if isinstance(observation.get("ax"), dict) else {}
    ax_identity_valid = (ax_payload.get("permissionGranted") is True
        and isinstance(ax_payload.get("applicationPid"), int)
        and not isinstance(ax_payload.get("applicationPid"), bool)
        and ax_payload.get("applicationPid", 0) > 0
        and isinstance(ax_payload.get("applicationBundleId"), str)
        and bool(ax_payload.get("applicationBundleId", "").strip()))

    def valid_ax_frame(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        dimensions = (value.get("x"), value.get("y"), value.get("width"), value.get("height"))
        if any(not isinstance(number, (int, float)) or isinstance(number, bool) for number in dimensions):
            return False
        return all(math.isfinite(float(number)) for number in dimensions) and value["width"] > 0 and value["height"] > 0
    candidates=[]
    selected_by_tool={}
    for element in state.elements:
        element_id=str(element.get("id") or "").strip()
        label=str(element.get("label") or element.get("text") or "").strip()
        role = str(element.get("role") or "")
        axpress = "AXPress" in element.get("supported_actions", element.get("supportedActions", []))
        vision_pointer_allowed = (state.source == "AX" and ax_identity_valid
            and role == "AXDisclosureTriangle" and pointer_access
            and element.get("frame") is None and not axpress
            and _vision_pointer_action_evidence(observation, element))
        role_allowed = (state.source == "DOM" and role == "disclosure") or (
            state.source == "AX" and ax_identity_valid and role == "AXDisclosureTriangle"
            and ((valid_ax_frame(element.get("frame")) and (axpress or pointer_access))
                 or vision_pointer_allowed))
        if (not element_id or not label or not role_allowed or element.get("visible") is False
                or element.get("enabled") is not True or element.get("expanded") is not False):
            continue
        tool_id=f"open_disclosure:{element_id}"
        candidates.append({"tool_id":tool_id,"criteria":f"closed disclosure to open: {label}",
                           "action":"open_disclosure","element_id":element_id})
        selected_by_tool[tool_id]=element
        if len(candidates)==8:break
    if not candidates:
        return {"status":"BLOCKED","reason":"NO_CLOSED_GROUNDED_DISCLOSURE","observation_source":state.source}
    selector=SemanticActionRuntime(decision_provider,DeterministicActionExecutor())
    selection=selector.select_candidates(candidates,question[:1000])
    if selection.get("status")!="PASS":
        return {"status":"BLOCKED","reason":"INVALID_SEMANTIC_DECISION","observation_source":state.source}
    action=selection["semantic_action"]
    selected=selected_by_tool.get(str(action["tool_id"]))
    if selected is None:
        return {"status":"BLOCKED","reason":"INVALID_SEMANTIC_DECISION","observation_source":state.source}
    selected_vision_pointer_evidence = _vision_pointer_action_evidence(observation, selected)
    selected_has_frame = valid_ax_frame(selected.get("frame"))
    target_allowed=bool(selected.get("id") and selected.get("label")
                        and selected.get("visible") is not False
                        and selected.get("enabled") is True
                        and ((state.source == "DOM" and selected.get("role")=="disclosure")
                             or (state.source == "AX" and selected.get("role")=="AXDisclosureTriangle"
                                 and ax_identity_valid
                                 and (selected_has_frame or selected_vision_pointer_evidence)))
                        and selected.get("expanded") is False)
    selected_has_axpress = "AXPress" in selected.get("supported_actions", selected.get("supportedActions", []))
    route_candidates=[]
    for rank,mechanism in enumerate(ActionRouter.MECHANISM_ORDER):
        executable=(mechanism=="native_structured" and state.source=="DOM" and target_allowed) or (
            mechanism=="accessibility" and state.source=="AX" and target_allowed and selected_has_axpress) or (
            mechanism=="deterministic_input" and state.source=="AX" and target_allowed
            and selected_has_frame and not selected_has_axpress and pointer_access) or (
            mechanism=="vision_pointer" and state.source=="AX" and target_allowed
            and selected.get("frame") is None and not selected_has_axpress and pointer_access
            and selected_vision_pointer_evidence)
        route_candidates.append({
            "mechanism":mechanism,
            "available":executable,
            "semantics_compatible":(mechanism=="native_structured" and state.source=="DOM"
                and selected.get("role")=="disclosure" and selected.get("expanded") is False) or (
                mechanism=="accessibility" and state.source=="AX" and selected.get("role")=="AXDisclosureTriangle"
                and ax_identity_valid
                and (selected_has_frame or selected_vision_pointer_evidence)
                and selected_has_axpress
                and selected.get("expanded") is False) or (
                mechanism=="deterministic_input" and state.source=="AX"
                and selected.get("role")=="AXDisclosureTriangle"
                and ax_identity_valid
                and selected_has_frame
                and not selected_has_axpress
                and pointer_access
                and selected.get("expanded") is False) or (
                mechanism=="vision_pointer" and state.source=="AX"
                and selected.get("role")=="AXDisclosureTriangle"
                and ax_identity_valid and selected.get("frame") is None and not selected_has_axpress
                and pointer_access and selected_vision_pointer_evidence
                and selected.get("expanded") is False),
            "observed_at":state.observed_at,
            "observation_id":state.observation_id,
            "session_id":state.session_id,
            "reliability":0.98 if executable else 0.0,
            "expected_cost":0.1+rank,
            "verification_available":executable,
            "security_allowed":executable and target_allowed,
            "grounding_provenance_bound":mechanism == "vision_pointer" and selected_vision_pointer_evidence,
            "coordinate_transform_verified":mechanism == "vision_pointer" and selected_vision_pointer_evidence,
            "target_identity_bound":mechanism == "vision_pointer" and selected_vision_pointer_evidence,
            "independent_postcondition":mechanism == "vision_pointer" and selected_vision_pointer_evidence,
        })
    route=ActionRouter().route(task_semantics="open_disclosure", candidates=route_candidates)
    if route.get("status")!="PASS":
        return {"status":"BLOCKED","reason":route.get("reason","ACTION_ROUTE_BLOCKED"),
                "observation_source":state.source,"action_route":route}
    return {"status":"PASS","observation_source":state.source,"semantic_action":action,
        "selected_element":{"id":str(selected.get("id")),"label":str(selected.get("label") or selected.get("text") or "")},
        "expected_element_state":{"id":str(selected.get("id")),"attribute":"expanded","value":True},
        "action_route":route,
        "before_state":state,"decision":selection["decision"],"vision_model_calls":0}


def plan_approved_routine_action(question: str, observation: dict[str, Any],
                                 procedure: Any, match: dict[str, Any]) -> dict[str, Any] | None:
    """Plan a previously approved action through the normal verified action contract.

    This reuses the existing native DOM/AX executor path. A routine supplies only
    the already-reviewed target/mechanism; current state, permission, route and
    postcondition requirements are checked again before the frontend executes it.
    """
    if (not isinstance(procedure, dict) or procedure.get("status") != "CANDIDATE"
            or procedure.get("validated") is not False
            or not isinstance(procedure.get("review"), dict)
            or procedure["review"].get("decision") != "APPROVED"
            or not isinstance(match, dict)):
        return None
    if not isinstance(observation, dict):
        return None
    state = DOMAXObservationProvider().observe(dom=observation.get("dom"), ax=observation.get("ax"))
    if (state.source not in {"DOM", "AX"} or not state.observation_id or not state.session_id
            or state.observed_at is None or not question.strip()):
        return None
    captured = procedure.get("captured_trace")
    if not isinstance(captured, dict):
        return None
    action = captured.get("action")
    actions = captured.get("actions")
    preconditions = procedure.get("preconditions")
    effects = captured.get("expected_effects")
    if (not isinstance(action, dict) or action.get("kind") not in {"open_disclosure", "open_find"}
            or not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], dict)
            or actions[0].get("kind") != action.get("kind")
            or not isinstance(preconditions, list) or len(preconditions) != 1
            or not isinstance(effects, list) or len(effects) != 1 or not isinstance(effects[0], dict)):
        return None
    expected_effect = ({"role": "disclosure", "expanded": True} if action["kind"] == "open_disclosure"
                       else {"role": "AXSearchField|AXTextField", "focused": True})
    if effects[0] != expected_effect:
        return None
    route_evidence = actions[0]
    mechanism = route_evidence.get("mechanism")
    expected_mechanism = ("native_structured" if state.source == "DOM" else "accessibility")
    kind = action["kind"]
    if kind == "open_disclosure" and mechanism != expected_mechanism:
        return None
    if kind == "open_find":
        ax = observation.get("ax")
        shortcut = route_evidence.get("shortcut")
        if (state.source != "AX" or mechanism != "keyboard_shortcut"
                or not isinstance(ax, dict) or ax.get("permissionGranted") is not True
                or not isinstance(ax.get("applicationPid"), int)
                or isinstance(ax.get("applicationPid"), bool) or ax.get("applicationPid", 0) <= 0
                or not isinstance(shortcut, dict) or shortcut.get("provenance") != "application_menu"
                or not isinstance(shortcut.get("key_code"), int)
                or isinstance(shortcut.get("key_code"), bool)
                or not 0 <= shortcut["key_code"] <= 127
                or not isinstance(shortcut.get("modifiers"), int)
                or isinstance(shortcut.get("modifiers"), bool)
                or not 0 <= shortcut["modifiers"] <= 15
                or not isinstance(shortcut.get("key"), str) or not shortcut["key"]):
            return None
        registered = KeyboardShortcutRegistry.find_actions(elements=state.elements, task_semantics="open_find")
        registered_action = next((item for item in registered if item["element_id"] == match.get("element_id")), None)
        if not registered_action or registered_action.get("shortcut") != shortcut:
            return None
    if state.source == "AX":
        ax = observation.get("ax")
        if (not isinstance(ax, dict) or ax.get("permissionGranted") is not True
                or not isinstance(ax.get("applicationPid"), int)
                or isinstance(ax.get("applicationPid"), bool) or ax.get("applicationPid", 0) <= 0):
            return None
    element_id = match.get("element_id")
    element = next((item for item in state.elements
                    if str(item.get("id") or item.get("element_id") or "") == element_id), None)
    expected = preconditions[0]
    safe_precondition_keys = {"role", "subrole", "label", "enabled", "expanded", "focused", "selected"}
    if (not isinstance(element_id, str) or not element
            or not expected or any(key not in safe_precondition_keys for key in expected)
            or any(element.get(key) != value for key, value in expected.items())):
        return None
    expected_role = ("disclosure" if state.source == "DOM" else "AXDisclosureTriangle")
    if (kind == "open_disclosure" and
            (element.get("role") != expected_role or element.get("visible") is False
             or element.get("expanded") is not False or element.get("enabled") is not True)):
        return None
    if kind == "open_disclosure" and state.source == "AX":
        frame = element.get("frame")
        dimensions = (frame.get("x"), frame.get("y"), frame.get("width"), frame.get("height")) if isinstance(frame, dict) else ()
        if (element.get("role") != "AXDisclosureTriangle"
                or "AXPress" not in element.get("supported_actions", element.get("supportedActions", []))
                or len(dimensions) != 4
                or any(not isinstance(number, (int, float)) or isinstance(number, bool)
                       or not math.isfinite(float(number)) for number in dimensions)
                or dimensions[2] <= 0 or dimensions[3] <= 0):
            return None
    if kind == "open_find" and (element.get("role") != "AXMenuItem" or element.get("enabled") is not True
                                 or element.get("visible") is False):
        return None
    if match.get("observation_id") != state.observation_id or match.get("session_id") != state.session_id:
        return None
    route_candidate = {"mechanism": mechanism, "available": True, "semantics_compatible": True,
        "observed_at": state.observed_at, "observation_id": state.observation_id,
        "session_id": state.session_id, "reliability": 0.98 if kind == "open_disclosure" else 0.9,
        "expected_cost": 0.0, "verification_available": True, "security_allowed": True}
    route = ActionRouter().route(task_semantics=kind, candidates=[route_candidate])
    if route.get("status") != "PASS":
        return None
    route["routine_candidate_id"] = procedure.get("id")
    selected = {"id": element_id, "label": str(element.get("label") or element.get("text") or "")}
    if kind == "open_disclosure":
        return {"status": "PASS", "observation_source": state.source,
            "semantic_action": {"action": kind, "element_id": element_id},
            "selected_element": selected,
            "expected_element_state": {"id": element_id, "attribute": "expanded", "value": True},
            "action_route": route, "before_state": state,
            "decision": {"selection_source": "APPROVED_VERIFIED_ROUTINE",
                         "procedure_id": procedure.get("id")},
            "routine_candidate_id": procedure.get("id"), "vision_model_calls": 0}
    return {"status": "PASS", "observation_source": state.source,
        "semantic_action": {"action": kind, "element_id": element_id},
        "selected_element": selected, "keyboard_shortcut": route_evidence["shortcut"],
        "expected_effect": effects[0], "action_route": route, "before_state": state,
        "decision": {"selection_source": "APPROVED_VERIFIED_ROUTINE",
                     "procedure_id": procedure.get("id")},
        "routine_candidate_id": procedure.get("id"), "vision_model_calls": 0}


def verify_disclosure_open(before: Any, after_observation: dict[str, Any], element_id: str) -> dict[str, Any]:
    """Verify a new expanded-state transition from a fresh DOM/AX snapshot."""
    if not isinstance(after_observation, dict):
        return {"verified":False,"reason":"FRESH_DOMAX_OBSERVATION_REQUIRED"}
    after=DOMAXObservationProvider().observe(dom=after_observation.get("dom"),ax=after_observation.get("ax"))
    if after.source not in {"DOM","AX"}:
        return {"verified":False,"reason":"FRESH_DOMAX_STATE_REQUIRED","after_source":after.source}
    return SemanticActionRuntime.verify_post_action(
        before,after,element_id,expected_element_states=((element_id,"expanded",True),))


def plan_open_find_shortcut(question: str, observation: dict[str, Any], decision_provider: Any) -> dict[str, Any]:
    """Plan only a menu-discovered, reversible Find/Search shortcut from fresh AX metadata."""
    if not isinstance(observation, dict):
        return {"status": "BLOCKED", "reason": "AX_OBSERVATION_REQUIRED"}
    ax = observation.get("ax")
    if not isinstance(ax, dict):
        return {"status": "BLOCKED", "reason": "AX_OBSERVATION_REQUIRED"}
    state = DOMAXObservationProvider().observe(ax=ax)
    if state.source != "AX":
        return {"status": "BLOCKED", "reason": "AX_STATE_REQUIRED"}
    pid = ax.get("applicationPid")
    if (ax.get("permissionGranted") is not True or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
        return {"status": "BLOCKED", "reason": "AX_APPLICATION_IDENTITY_REQUIRED"}
    if not any(token in question.casefold() for token in ("find", "search", "検索")):
        return {"status": "BLOCKED", "reason": "FIND_SHORTCUT_SEMANTICS_REQUIRED"}
    if any(element.get("role") in {"AXTextField", "AXSearchField"} and element.get("focused") is True
           and (element.get("subrole") == "AXSearchField"
                or str(element.get("label") or "").casefold() in {"find", "search", "検索"})
           for element in state.elements):
        return {"status": "BLOCKED", "reason": "SEARCH_FIELD_ALREADY_FOCUSED"}
    source_candidates = KeyboardShortcutRegistry.find_actions(elements=state.elements, task_semantics="open_find")
    selected_by_tool: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    for item in source_candidates:
        element = next((row for row in state.elements if str(row.get("id") or "") == item["element_id"]), None)
        if element is None or element.get("enabled") is not True:
            continue
        candidates.append(item)
        selected_by_tool[item["tool_id"]] = element
    if not candidates:
        return {"status": "BLOCKED", "reason": "NO_MENU_DISCOVERED_FIND_SHORTCUT", "observation_source": state.source}
    selector = SemanticActionRuntime(decision_provider, DeterministicActionExecutor())
    selection = selector.select_candidates(candidates, question[:1000])
    if selection.get("status") != "PASS":
        return {"status": "BLOCKED", "reason": "INVALID_SEMANTIC_DECISION", "observation_source": state.source}
    action = selection["semantic_action"]
    selected = selected_by_tool.get(str(action["tool_id"]))
    if selected is None:
        return {"status": "BLOCKED", "reason": "INVALID_SEMANTIC_DECISION", "observation_source": state.source}
    route_candidates = []
    for rank, mechanism in enumerate(ActionRouter.MECHANISM_ORDER):
        executable = mechanism == "keyboard_shortcut"
        route_candidates.append({"mechanism": mechanism, "available": executable,
            "semantics_compatible": executable, "observed_at": state.observed_at,
            "observation_id": state.observation_id, "session_id": state.session_id,
            "reliability": 0.9 if executable else 0.0, "expected_cost": 0.1 + rank,
            "verification_available": executable, "security_allowed": executable})
    route = ActionRouter().route(task_semantics="open_find", candidates=route_candidates)
    if route.get("status") != "PASS":
        return {"status": "BLOCKED", "reason": route.get("reason", "ACTION_ROUTE_BLOCKED"),
                "observation_source": state.source, "action_route": route}
    shortcut = action["shortcut"]
    return {"status": "PASS", "observation_source": state.source,
        "semantic_action": {"action": "open_find", "element_id": action["element_id"]},
        "selected_element": {"id": str(selected.get("id")), "label": str(selected.get("label") or "")},
        "keyboard_shortcut": shortcut,
        "expected_effect": {"role": "AXSearchField|AXTextField", "focused": True},
        "action_route": route, "before_state": state, "decision": selection["decision"],
        "vision_model_calls": 0}


def verify_find_shortcut(before: Any, after_observation: dict[str, Any]) -> dict[str, Any]:
    def inconclusive(reason: str) -> dict[str, Any]:
        return {"verified": False, "result": "INCONCLUSIVE", "reason": reason}

    if not isinstance(after_observation, dict) or not isinstance(after_observation.get("ax"), dict):
        return inconclusive("FRESH_AX_OBSERVATION_REQUIRED")
    payload = after_observation["ax"]
    pid = payload.get("applicationPid")
    if (payload.get("permissionGranted") is not True or not isinstance(pid, int)
            or isinstance(pid, bool) or pid <= 0):
        return inconclusive("AX_APPLICATION_OR_PERMISSION_MISMATCH")
    after = DOMAXObservationProvider().observe(ax=payload)
    if not isinstance(before, type(after)) or before.source != "AX" or after.source != "AX":
        return inconclusive("AX_STATE_REQUIRED")
    if not before.session_id or before.session_id != after.session_id:
        return inconclusive("OBSERVATION_SESSION_MISMATCH")
    if (not before.coordinate_space or before.coordinate_space != after.coordinate_space):
        return inconclusive("OBSERVATION_COORDINATE_SPACE_MISMATCH")
    if (not before.observation_id or not after.observation_id
            or before.observation_id == after.observation_id):
        return inconclusive("POST_ACTION_OBSERVATION_REPLAYED")
    if (before.observed_at is None or after.observed_at is None
            or not math.isfinite(before.observed_at) or not math.isfinite(after.observed_at)
            or after.observed_at <= before.observed_at):
        return inconclusive("POST_ACTION_OBSERVATION_NOT_NEWER")
    now = time.time()
    if before.observed_at > now + 2.0 or after.observed_at > now + 2.0:
        return inconclusive("OBSERVATION_TIMESTAMP_IN_FUTURE")
    if now - before.observed_at > 30.0 or now - after.observed_at > 30.0:
        return inconclusive("OBSERVATION_STALE")
    if (before.confidence < 0.65 or after.confidence < 0.65
            or before.uncertainty or after.uncertainty):
        return inconclusive("OBSERVATION_COVERAGE_INSUFFICIENT")
    if any(element.get("role") in {"AXTextField", "AXSearchField"} and element.get("focused") is True
           and (element.get("subrole") == "AXSearchField"
                or str(element.get("label") or "").casefold() in {"find", "search", "検索"})
           for element in after.elements):
        # The focus control can be newly inserted and need not share a stable AX ordinal.
        return {"verified": True, "result": "CONFIRMED", "reason": "FRESH_SEARCH_FIELD_FOCUSED",
                "after_source": after.source}
    return {"verified": False, "result": "NOT_CONFIRMED",
            "reason": "SEARCH_FIELD_FOCUS_NOT_OBSERVED", "after_source": after.source}


def plan_button_press(question: str, target_label: str | None, expected_text: str, observation: dict[str, Any],
                      decision_provider: Any) -> dict[str, Any]:
    """Plan a grounded AX button press with a user-specified visible effect.

    When the user states the intended outcome but not the exact button label,
    SemanticActionRuntime may choose among a small, unambiguous set of current
    AX buttons. The postcondition remains explicit and must be absent before
    dispatch so execution can never be reported as success without fresh proof.
    """
    if not isinstance(observation, dict) or not isinstance(observation.get("ax"), dict):
        return {"status": "BLOCKED", "reason": "AX_OBSERVATION_REQUIRED"}
    expected_text = str(expected_text or "").strip()
    target_label = str(target_label or "").strip()
    if len(target_label) > 120:
        return {"status": "BLOCKED", "reason": "BUTTON_LABEL_INVALID"}
    if not expected_text or len(expected_text) > 240:
        return {"status": "BLOCKED", "reason": "EXPECTED_VISIBLE_TEXT_REQUIRED"}
    ax = observation["ax"]
    pointer_access = observation.get("pointer_event_permission_granted") is True
    state = DOMAXObservationProvider().observe(ax=ax)
    if (state.source != "AX" or ax.get("permissionGranted") is not True
            or not isinstance(ax.get("applicationPid"), int)
            or isinstance(ax.get("applicationPid"), bool) or ax["applicationPid"] <= 0
            or not isinstance(ax.get("applicationBundleId"), str)
            or not ax["applicationBundleId"].strip()):
        return {"status": "BLOCKED", "reason": "AX_APPLICATION_IDENTITY_REQUIRED"}
    if expected_text in "\n".join(state.text):
        return {"status": "BLOCKED", "reason": "EXPECTED_TEXT_ALREADY_VISIBLE"}
    by_tool_id: dict[str, dict[str, Any]] = {}
    pressable_candidates: list[dict[str, Any]] = []
    vision_pointer_candidates: list[dict[str, Any]] = []
    for element in state.elements:
        element_id = str(element.get("id") or "").strip()
        label = str(element.get("label") or "").strip()
        actions = element.get("supported_actions", element.get("supportedActions", []))
        ax_press = isinstance(actions, list) and "AXPress" in actions
        # A zero-sized AX frame is not a usable coordinate. Keep such a button
        # as a provisional candidate when visibility is unknown; the route
        # still cannot execute until fresh, identity-bound visual tracking
        # proves the button's location and containment in its target window.
        usable_frame = _has_usable_ax_frame(element)
        vision_pointer_candidate = pointer_access and not ax_press and not usable_frame
        if (not element_id or (target_label and label.casefold() != target_label.casefold())
                or element.get("role") != "AXButton"
                or element.get("visible") is False
                or (element.get("visible") is not True and not vision_pointer_candidate)
                or element.get("enabled") is not True
                or not (ax_press or vision_pointer_candidate)):
            continue
        tool_id = f"press_button:{element_id}"
        candidate = {"tool_id": tool_id,
                     "criteria": f'visible enabled button "{label}"; verify visible text "{expected_text}"',
                     "action": "press_button", "element_id": element_id}
        (pressable_candidates if ax_press else vision_pointer_candidates).append(candidate)
        by_tool_id[tool_id] = element
    # Keep perception as a fallback: if any current grounded target exposes
    # AXPress, choose among those before paying for visual grounding and pointer input.
    candidates = pressable_candidates or vision_pointer_candidates
    if len(candidates) > 8:
        return {"status": "BLOCKED", "reason": "TOO_MANY_GROUNDED_AX_BUTTONS"}
    if not candidates:
        return {"status": "BLOCKED", "reason": "NO_PRESSABLE_GROUNDED_AX_BUTTON"}
    labels = [str(by_tool_id[item["tool_id"]].get("label") or "").strip().casefold()
              for item in candidates]
    if len(labels) != len(set(labels)):
        return {"status": "BLOCKED", "reason": "AMBIGUOUS_GROUNDED_AX_BUTTON_LABELS"}
    if target_label and len(candidates) == 1:
        # The chat supplied an exact label and current AX exposed one matching
        # pressable candidate. Avoid a model round-trip that can age the native
        # AX snapshot past the pointer executor's short freshness window.
        selection = {
            "status": "PASS",
            "semantic_action": {"tool_id": candidates[0]["tool_id"]},
            "decision": {"source": "unique_exact_chat_label"},
        }
    else:
        selection = SemanticActionRuntime(decision_provider, DeterministicActionExecutor()).select_candidates(
            candidates, question[:1000])
    if selection.get("status") != "PASS":
        return {"status": "BLOCKED", "reason": "INVALID_SEMANTIC_DECISION"}
    action = selection["semantic_action"]
    selected = by_tool_id.get(str(action.get("tool_id")))
    if selected is None:
        return {"status": "BLOCKED", "reason": "INVALID_SEMANTIC_DECISION"}
    actions = selected.get("supported_actions", selected.get("supportedActions", []))
    has_axpress = isinstance(actions, list) and "AXPress" in actions
    vision_pointer_evidence = (not has_axpress and pointer_access
                               and not _has_usable_ax_frame(selected)
                               and _vision_pointer_action_evidence(observation, selected))
    if not has_axpress and not vision_pointer_evidence:
        return {"status": "VISION_GROUNDING_REQUIRED", "observation_source": "AX",
                "reason": "BUTTON_REQUIRES_FRESH_VISION_GROUNDING",
                "selected_element": {"id": str(selected["id"]), "label": str(selected["label"])},
                "expected_text_contains": expected_text}
    mechanism = "accessibility" if has_axpress else "vision_pointer"
    route = ActionRouter().route(task_semantics="press_button", candidates=[{
        "mechanism": mechanism, "available": True, "semantics_compatible": True,
        "observed_at": state.observed_at, "observation_id": state.observation_id,
        "session_id": state.session_id, "reliability": 0.98,
        "expected_cost": 0.0 if has_axpress else 1.0,
        "verification_available": True, "security_allowed": True,
        "grounding_provenance_bound": vision_pointer_evidence,
        "coordinate_transform_verified": vision_pointer_evidence,
        "target_identity_bound": vision_pointer_evidence,
        "independent_postcondition": vision_pointer_evidence,
    }])
    if route.get("status") != "PASS":
        return {"status": "BLOCKED", "reason": route.get("reason", "ACTION_ROUTE_BLOCKED"),
                "action_route": route}
    return {"status": "PASS", "observation_source": "AX", "semantic_action": action,
            "selected_element": {"id": str(selected["id"]), "label": str(selected["label"])},
            "expected_text_contains": expected_text, "action_route": route,
            "before_state": state, "decision": selection["decision"], "vision_model_calls": 0}


def verify_button_press(before: Any, after_observation: dict[str, Any], element_id: str,
                        expected_text: str) -> dict[str, Any]:
    """Confirm only when fresh same-target AX evidence shows the requested text."""
    if not isinstance(after_observation, dict) or not isinstance(after_observation.get("ax"), dict):
        return {"verified": False, "result": "INCONCLUSIVE", "reason": "FRESH_AX_OBSERVATION_REQUIRED"}
    after = DOMAXObservationProvider().observe(ax=after_observation.get("ax"))
    if after.source != "AX":
        return {"verified": False, "result": "INCONCLUSIVE", "reason": "FRESH_AX_STATE_REQUIRED"}
    return SemanticActionRuntime.verify_post_action(
        before, after, element_id, expected_text_contains=(expected_text,))


def plan_text_entry(question: str, target_label: str, input_text: str,
                    observation: dict[str, Any]) -> dict[str, Any]:
    """Plan exact text replacement in one focused, non-secure AX text control."""
    if not isinstance(observation, dict) or not isinstance(observation.get("ax"), dict):
        return {"status": "BLOCKED", "reason": "AX_OBSERVATION_REQUIRED"}
    target_label = str(target_label or "").strip()
    input_text = str(input_text or "")
    if not target_label or len(target_label) > 120:
        return {"status": "BLOCKED", "reason": "TEXT_TARGET_LABEL_INVALID"}
    if not input_text or len(input_text) > 2_048 or "\x00" in input_text:
        return {"status": "BLOCKED", "reason": "TEXT_INPUT_INVALID"}
    ax = observation["ax"]
    state = DOMAXObservationProvider().observe(ax=ax)
    if (state.source != "AX" or state.permission_granted is not True
            or not isinstance(state.application_pid, int) or isinstance(state.application_pid, bool)
            or state.application_pid <= 0 or not state.application_bundle_id.strip()
            or not state.observation_id or not state.session_id):
        return {"status": "BLOCKED", "reason": "AX_APPLICATION_IDENTITY_REQUIRED"}
    if state.observed_at is None or not math.isfinite(state.observed_at):
        return {"status": "BLOCKED", "reason": "AX_OBSERVATION_TIMESTAMP_REQUIRED"}
    matches = [element for element in state.elements
               if str(element.get("label") or "").strip().casefold() == target_label.casefold()]
    if any(element.get("role") == "AXSecureTextField" for element in matches):
        return {"status": "BLOCKED", "reason": "SECURE_TEXT_ENTRY_UNAVAILABLE"}
    candidates = [element for element in matches
                  if element.get("role") in {"AXTextField", "AXTextArea"}]
    if len(candidates) != 1:
        return {"status": "BLOCKED", "reason": "TEXT_TARGET_NOT_UNIQUE"}
    selected = candidates[0]
    if selected.get("visible") is not True or selected.get("enabled") is not True:
        return {"status": "BLOCKED", "reason": "TEXT_TARGET_NOT_VISIBLE_AND_ENABLED"}
    element_id = selected.get("id")
    if not isinstance(element_id, str) or not element_id.strip() or len(element_id) > 256:
        return {"status": "BLOCKED", "reason": "TEXT_TARGET_ID_INVALID"}
    if "\n" in input_text and selected.get("role") != "AXTextArea":
        return {"status": "BLOCKED", "reason": "MULTILINE_TEXT_REQUIRES_TEXT_AREA"}
    if any(ord(char) < 32 and char != "\n" for char in input_text):
        return {"status": "BLOCKED", "reason": "TEXT_INPUT_CONTROL_CHARACTER"}
    route = ActionRouter().route(task_semantics="replace_focused_text_value", candidates=[{
        "mechanism": "accessibility", "available": True, "semantics_compatible": True,
        "observed_at": state.observed_at, "observation_id": state.observation_id,
        "session_id": state.session_id, "reliability": 0.99, "expected_cost": 0.0,
        "verification_available": True, "security_allowed": True,
    }])
    if route.get("status") != "PASS":
        return {"status": "BLOCKED", "reason": route.get("reason", "ACTION_ROUTE_BLOCKED"),
                "action_route": route}
    return {"status": "PASS", "observation_source": "AX",
            "selected_element": {"id": element_id, "label": target_label,
                                 "role": selected["role"]},
            "action_route": route, "before_state": state}


def verify_text_entry(before: Any, after_observation: dict[str, Any], element_id: str,
                      execution_receipt: Any) -> dict[str, Any]:
    """Verify native AXSetValue readback without returning or logging field content."""
    if (not isinstance(execution_receipt, dict)
            or execution_receipt.get("value_matched") is not True
            or execution_receipt.get("execution_attempted") is not True
            or execution_receipt.get("verified") is not True
            or execution_receipt.get("status") != "PASS"
            or execution_receipt.get("element_id") != element_id):
        return {"verified": False, "result": "NOT_CONFIRMED", "reason": "TEXT_VALUE_RECEIPT_REQUIRED"}
    if not isinstance(after_observation, dict) or not isinstance(after_observation.get("ax"), dict):
        return {"verified": False, "result": "INCONCLUSIVE", "reason": "FRESH_AX_OBSERVATION_REQUIRED"}
    after = DOMAXObservationProvider().observe(ax=after_observation["ax"])
    now = time.time()
    target = [element for element in after.elements if str(element.get("id")) == element_id]
    if (after.source != "AX" or len(target) != 1
            or after.observation_id == before.observation_id
            or after.observed_at is None or before.observed_at is None
            or after.observed_at <= before.observed_at
            or not math.isfinite(after.observed_at) or not math.isfinite(before.observed_at)
            or after.observed_at > now + 2.0 or before.observed_at > now + 2.0
            or now - after.observed_at > 30.0 or now - before.observed_at > 30.0
            or after.session_id != before.session_id
            or after.application_pid != before.application_pid
            or after.application_bundle_id != before.application_bundle_id
            or target[0].get("role") not in {"AXTextField", "AXTextArea"}
            or target[0].get("enabled") is not True or target[0].get("visible") is not True
            or target[0].get("focused") is not True):
        return {"verified": False, "result": "NOT_CONFIRMED", "reason": "TEXT_TARGET_FOCUS_NOT_RETAINED"}
    # Focus is expected to remain stable across value replacement, so the
    # generic semantic verifier's required state transition is inappropriate.
    # The native executor privately read AXValue back; require a distinct,
    # newer same-session AX snapshot as the independent target/focus check.
    return {"verified": True, "result": "CONFIRMED", "value_matched": True,
            "before_source": before.source, "after_source": after.source,
            "observation_id": after.observation_id}


def plan_tab_focus(question: str, target_label: str,
                   observation: dict[str, Any]) -> dict[str, Any]:
    """Plan one explicit Tab transition between visible AX focus targets."""
    ax = observation.get("ax") if isinstance(observation, dict) else None
    if not isinstance(ax, dict):
        return {"status": "BLOCKED", "reason": "AX_OBSERVATION_REQUIRED"}
    state = DOMAXObservationProvider().observe(ax=ax)
    bundle_id = state.application_bundle_id
    if (state.source != "AX" or ax.get("permissionGranted") is not True
            or not isinstance(state.application_pid, int) or isinstance(state.application_pid, bool)
            or state.application_pid <= 0 or not isinstance(bundle_id, str) or not bundle_id.strip()
            or not state.observation_id or not state.session_id
            or state.observed_at is None or not question.strip()):
        return {"status": "BLOCKED", "reason": "AX_APPLICATION_IDENTITY_REQUIRED"}
    now = time.time()
    if (not math.isfinite(state.observed_at) or state.observed_at > now + 1.0
            or now - state.observed_at > 5.0):
        return {"status": "BLOCKED", "reason": "AX_OBSERVATION_STALE"}
    if not isinstance(target_label, str) or not target_label.strip() or len(target_label) > 120:
        return {"status": "BLOCKED", "reason": "TAB_FOCUS_TARGET_REQUIRED"}
    focusable_roles = {"AXButton", "AXTextField", "AXTextArea", "AXCheckBox",
                       "AXRadioButton", "AXPopUpButton", "AXLink", "AXComboBox",
                       "AXSlider", "AXIncrementor", "AXTab"}
    current = [element for element in state.elements
               if element.get("focused") is True and element.get("visible") is True]
    targets = [element for element in state.elements
               if str(element.get("label") or element.get("text") or "").strip().casefold()
               == target_label.strip().casefold()]
    if len(current) != 1:
        return {"status": "BLOCKED", "reason": "CURRENT_AX_FOCUS_NOT_UNIQUE"}
    if len(targets) != 1:
        return {"status": "BLOCKED", "reason": "TAB_FOCUS_TARGET_NOT_UNIQUE"}
    target = targets[0]
    if (target.get("role") not in focusable_roles or target.get("visible") is not True
            or target.get("enabled") is not True or target.get("focused") is True
            or not str(target.get("id") or "").strip()):
        return {"status": "BLOCKED", "reason": "TAB_FOCUS_TARGET_NOT_ACTIONABLE"}
    current_id = str(current[0].get("id") or "").strip()
    if not current_id:
        return {"status": "BLOCKED", "reason": "CURRENT_AX_FOCUS_ID_UNAVAILABLE"}
    candidates = []
    for rank, mechanism in enumerate(ActionRouter.MECHANISM_ORDER):
        executable = mechanism == "keyboard_shortcut"
        candidates.append({"mechanism": mechanism, "available": executable,
            "semantics_compatible": executable, "observed_at": state.observed_at,
            "observation_id": state.observation_id, "session_id": state.session_id,
            "reliability": 0.9 if executable else 0.0, "expected_cost": 0.1 + rank,
            "verification_available": executable, "security_allowed": executable,
            "target_identity_bound": executable})
    route = ActionRouter().route(task_semantics="keyboard_tab_focus", candidates=candidates)
    if route.get("status") != "PASS":
        return {"status": "BLOCKED", "reason": route.get("reason", "ACTION_ROUTE_BLOCKED"),
                "action_route": route}
    return {"status": "PASS", "observation_source": "AX",
        "semantic_action": {"action": "press_tab", "target_element_id": str(target["id"])},
        "selected_element": {"id": str(target["id"]),
            "label": str(target.get("label") or target.get("text") or "")},
        "current_focus_element": {"id": current_id,
            "label": str(current[0].get("label") or current[0].get("text") or "")},
        "action_route": route, "before_state": state,
        "expected_effect": {"id": str(target["id"]), "focused": True},
        "vision_model_calls": 0}


def verify_tab_focus(before: Any, after_observation: dict[str, Any],
                     current_focus_id: str, target_id: str, target_label: str) -> dict[str, Any]:
    """Require fresh same-session AX evidence of focus moving to the requested control."""
    if not isinstance(after_observation, dict) or not isinstance(after_observation.get("ax"), dict):
        return {"verified": False, "result": "INCONCLUSIVE", "reason": "FRESH_AX_OBSERVATION_REQUIRED"}
    ax = after_observation["ax"]
    if (getattr(before, "source", None) != "AX"
            or ax.get("permissionGranted") is not True
            or ax.get("applicationPid") != getattr(before, "application_pid", None)
            or ax.get("applicationBundleId") != getattr(before, "application_bundle_id", None)
            or ax.get("sessionId") != getattr(before, "session_id", None)):
        return {"verified": False, "result": "INCONCLUSIVE", "reason": "AX_APPLICATION_IDENTITY_MISMATCH"}
    observed_at = ax.get("observedAt")
    if (not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool)
            or not math.isfinite(float(observed_at))
            or observed_at <= getattr(before, "observed_at", 0)):
        return {"verified": False, "result": "INCONCLUSIVE", "reason": "FRESH_AX_OBSERVATION_REQUIRED"}
    elements = ax.get("elements")
    if not isinstance(elements, list):
        return {"verified": False, "result": "INCONCLUSIVE", "reason": "AX_ELEMENTS_REQUIRED"}
    targets = [element for element in elements if isinstance(element, dict)
               and element.get("id") == target_id and element.get("label") == target_label]
    current = [element for element in elements if isinstance(element, dict)
               and element.get("id") == current_focus_id]
    verified = (len(targets) == 1 and targets[0].get("focused") is True
                and targets[0].get("visible") is True and targets[0].get("enabled") is True
                and len(current) == 1 and current[0].get("focused") is not True)
    return {"verified": verified,
        "result": "CONFIRMED" if verified else "NOT_CONFIRMED",
        "reason": None if verified else "REQUESTED_CONTROL_DID_NOT_RECEIVE_FOCUS",
        "target_element_id": target_id, "current_focus_element_id": current_focus_id}


_CHAT_KEY_CODES = {"Enter": 36, "Escape": 53, "Space": 49,
                   "ArrowLeft": 123, "ArrowRight": 124, "ArrowDown": 125, "ArrowUp": 126}


def plan_keypress(question: str, key: str, expected_text: str,
                  observation: dict[str, Any]) -> dict[str, Any]:
    """Plan one allowlisted key for the uniquely focused visible control."""
    ax = observation.get("ax") if isinstance(observation, dict) else None
    if not isinstance(ax, dict):
        return {"status": "BLOCKED", "reason": "AX_OBSERVATION_REQUIRED"}
    state = DOMAXObservationProvider().observe(ax=ax)
    if (state.source != "AX" or ax.get("permissionGranted") is not True
            or not isinstance(state.application_pid, int) or isinstance(state.application_pid, bool)
            or state.application_pid <= 0 or not state.application_bundle_id.strip()
            or not state.observation_id or not state.session_id
            or state.observed_at is None or not question.strip()):
        return {"status": "BLOCKED", "reason": "AX_APPLICATION_IDENTITY_REQUIRED"}
    now = time.time()
    if (not math.isfinite(state.observed_at) or state.observed_at > now + 1.0
            or now - state.observed_at > 5.0):
        return {"status": "BLOCKED", "reason": "AX_OBSERVATION_STALE"}
    if key not in _CHAT_KEY_CODES:
        return {"status": "BLOCKED", "reason": "KEY_NOT_ALLOWLISTED"}
    expected_text = str(expected_text or "").strip()
    if not expected_text or len(expected_text) > 240 or any(ord(ch) < 32 for ch in expected_text):
        return {"status": "BLOCKED", "reason": "KEY_POSTCONDITION_REQUIRED"}
    focus_containers = {"AXGroup", "AXWindow", "AXWebArea", "AXCanvas"}
    visible_focused = [element for element in state.elements
                       if element.get("focused") is True and element.get("visible") is True]
    if any(element.get("role") == "AXSecureTextField"
           or element.get("subrole") == "AXSecureTextField" for element in visible_focused):
        return {"status": "BLOCKED", "reason": "SECURE_KEY_TARGET_UNAVAILABLE"}
    focused = [element for element in state.elements
               if element.get("focused") is True and element.get("visible") is True
               and (element.get("enabled") is True
                    or (element.get("role") in focus_containers and element.get("enabled") is not False))
               and element.get("role") != "AXSecureTextField"
               and element.get("subrole") != "AXSecureTextField"]
    if len(focused) != 1:
        return {"status": "BLOCKED", "reason": "CURRENT_AX_FOCUS_NOT_UNIQUE"}
    selected = focused[0]
    element_id = str(selected.get("id") or "").strip()
    label = str(selected.get("label") or selected.get("text") or "").strip()
    if not element_id or not label:
        return {"status": "BLOCKED", "reason": "CURRENT_AX_FOCUS_IDENTITY_REQUIRED"}
    before_text = " ".join([str(getattr(state, "text", "") or ""),
                             *(str(element.get("label") or element.get("text") or "")
                               for element in state.elements)]).casefold()
    if expected_text.casefold() in before_text:
        return {"status": "BLOCKED", "reason": "KEY_POSTCONDITION_ALREADY_VISIBLE"}
    candidates = []
    for rank, mechanism in enumerate(ActionRouter.MECHANISM_ORDER):
        executable = mechanism == "deterministic_input"
        candidates.append({"mechanism": mechanism, "available": executable,
            "semantics_compatible": executable, "observed_at": state.observed_at,
            "observation_id": state.observation_id, "session_id": state.session_id,
            "reliability": 0.85 if executable else 0.0, "expected_cost": 0.1 + rank,
            "verification_available": executable, "security_allowed": executable,
            "target_identity_bound": executable})
    route = ActionRouter().route(task_semantics="bounded_keypress_with_visible_postcondition",
                                 candidates=candidates)
    if route.get("status") != "PASS":
        return {"status": "BLOCKED", "reason": route.get("reason", "ACTION_ROUTE_BLOCKED"),
                "action_route": route}
    return {"status": "PASS", "observation_source": "AX", "key": key,
        "selected_element": {"id": element_id, "label": label, "role": selected.get("role")},
        "expected_text": expected_text, "action_route": route, "before_state": state,
        "vision_model_calls": 0}


def verify_keypress(before: Any, after_observation: dict[str, Any], element_id: str,
                    expected_text: str) -> dict[str, Any]:
    """Reuse the existing semantic AX verifier for the key's explicit outcome."""
    return verify_button_press(before, after_observation, element_id, expected_text)


def _bounded_scroll_frame(element: dict[str, Any]) -> tuple[float, float, float, float] | None:
    frame = element.get("frame")
    if not isinstance(frame, dict):
        return None
    values = tuple(frame.get(key) for key in ("x", "y", "width", "height"))
    if any(not isinstance(value, (int, float)) or isinstance(value, bool)
           or not math.isfinite(float(value)) for value in values):
        return None
    x, y, width, height = map(float, values)
    if (abs(x) > 100_000 or abs(y) > 100_000 or not 0 < width <= 32_768
            or not 0 < height <= 32_768):
        return None
    return x, y, width, height


def _scrollbar_for_area(elements: list[dict[str, Any]], area: dict[str, Any]) -> dict[str, Any] | None:
    depth = area.get("depth")
    area_frame = _bounded_scroll_frame(area)
    if (not isinstance(depth, int) or isinstance(depth, bool) or area_frame is None
            or not any(element.get("id") == area.get("id") for element in elements)):
        return None
    descendants = []
    for element in elements:
        child_depth = element.get("depth")
        if (not isinstance(child_depth, int) or isinstance(child_depth, bool)
                or child_depth <= depth):
            continue
        frame = _bounded_scroll_frame(element)
        value = element.get("scrollValue", element.get("scroll_value"))
        if (element.get("role") == "AXScrollBar" and element.get("visible") is True
                and frame and frame[3] >= frame[2] * 2.0
                and _scrollbar_frame_belongs_to_area(area_frame, frame)
                and isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0):
            descendants.append((element, float(value)))
    return descendants[0][0] | {"_scroll_value": descendants[0][1]} if len(descendants) == 1 else None


def _scrollbar_frame_belongs_to_area(area: tuple[float, float, float, float],
                                     scrollbar: tuple[float, float, float, float]) -> bool:
    """Associate AX's separately-listed scrollbar with an area by nearby geometry."""
    ax, ay, aw, ah = area
    sx, sy, sw, sh = scrollbar
    overlap_y = max(0.0, min(ay + ah, sy + sh) - max(ay, sy))
    tolerance = max(16.0, sw)
    scrollbar_center_x = sx + sw / 2.0
    return (overlap_y >= sh * 0.5
            and ax - tolerance <= scrollbar_center_x <= ax + aw + tolerance)


def plan_wheel_scroll(question: str, target_label: str, direction: str,
                      observation: dict[str, Any]) -> dict[str, Any]:
    """Plan a small wheel movement in one AX scroll area with a measurable bar."""
    ax = observation.get("ax") if isinstance(observation, dict) else None
    if not isinstance(ax, dict):
        return {"status": "BLOCKED", "reason": "AX_OBSERVATION_REQUIRED"}
    state = DOMAXObservationProvider().observe(ax=ax)
    if (state.source != "AX" or ax.get("permissionGranted") is not True
            or not isinstance(state.application_pid, int) or isinstance(state.application_pid, bool)
            or state.application_pid <= 0 or not state.application_bundle_id.strip()
            or not state.observation_id or not state.session_id
            or not isinstance(observation.get("pointer_event_permission_granted"), bool)
            or observation.get("pointer_event_permission_granted") is not True):
        return {"status": "BLOCKED", "reason": "AX_IDENTITY_OR_POINTER_PERMISSION_REQUIRED"}
    now = time.time()
    if (state.observed_at is None or not math.isfinite(state.observed_at)
            or state.observed_at > now + 1.0 or now - state.observed_at > 5.0):
        return {"status": "BLOCKED", "reason": "AX_OBSERVATION_STALE"}
    if direction not in {"up", "down"}:
        return {"status": "BLOCKED", "reason": "SCROLL_DIRECTION_INVALID"}
    target_label = str(target_label or "").strip()
    if not target_label or len(target_label) > 120 or not question.strip():
        return {"status": "BLOCKED", "reason": "SCROLL_TARGET_LABEL_REQUIRED"}
    areas = [element for element in state.elements
             if element.get("role") in {"AXScrollArea", "AXWebArea"}
             and str(element.get("label") or "").strip().casefold() == target_label.casefold()]
    if len(areas) != 1:
        return {"status": "BLOCKED", "reason": "SCROLL_AREA_NOT_UNIQUE"}
    area = areas[0]
    frame = _bounded_scroll_frame(area)
    window = ax.get("windowFrame", ax.get("window_frame"))
    if not frame or not isinstance(window, dict):
        return {"status": "BLOCKED", "reason": "SCROLL_AREA_GEOMETRY_REQUIRED"}
    window_frame = tuple(window.get(key) for key in ("x", "y", "width", "height"))
    if (any(not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(float(value)) for value in window_frame)):
        return {"status": "BLOCKED", "reason": "SCROLL_WINDOW_GEOMETRY_INVALID"}
    wx, wy, ww, wh = map(float, window_frame)
    if (ww <= 0 or wh <= 0 or not (wx <= frame[0] + frame[2] / 2 <= wx + ww)
            or not (wy <= frame[1] + frame[3] / 2 <= wy + wh)
            or area.get("visible") is not True):
        return {"status": "BLOCKED", "reason": "SCROLL_AREA_OUTSIDE_VISIBLE_WINDOW"}
    elements = [element for element in state.elements if isinstance(element, dict)]
    scrollbar = _scrollbar_for_area(elements, area)
    if scrollbar is None:
        return {"status": "BLOCKED", "reason": "VERTICAL_SCROLL_VALUE_UNAVAILABLE_OR_AMBIGUOUS"}
    scroll_value = float(scrollbar.pop("_scroll_value"))
    candidates = [{"mechanism": mechanism, "available": mechanism == "deterministic_input",
        "semantics_compatible": mechanism == "deterministic_input",
        "observed_at": state.observed_at, "observation_id": state.observation_id,
        "session_id": state.session_id, "reliability": 0.9 if mechanism == "deterministic_input" else 0.0,
        "expected_cost": 0.1 + rank, "verification_available": mechanism == "deterministic_input",
        "security_allowed": mechanism == "deterministic_input", "target_identity_bound": True}
        for rank, mechanism in enumerate(ActionRouter.MECHANISM_ORDER)]
    route = ActionRouter().route(task_semantics="native_wheel_scroll", candidates=candidates)
    if route.get("status") != "PASS":
        return {"status": "BLOCKED", "reason": route.get("reason", "ACTION_ROUTE_BLOCKED"),
                "action_route": route}
    return {"status": "PASS", "observation_source": "AX",
        "selected_element": {"id": str(area["id"]), "label": target_label,
                             "role": str(area["role"]), "scrollbar_id": str(scrollbar["id"]),
                             "scroll_value": scroll_value},
        "direction": direction, "action_route": route, "before_state": state,
        "expected_effect": {"scroll_value_direction": "increase" if direction == "down" else "decrease"},
        "vision_model_calls": 0}


def verify_wheel_scroll(before: Any, after_observation: dict[str, Any], area_id: str,
                        scrollbar_id: str, target_label: str, direction: str,
                        before_scroll_value: float) -> dict[str, Any]:
    """Confirm only a fresh same-app vertical scroll-bar value transition."""
    if not isinstance(after_observation, dict) or not isinstance(after_observation.get("ax"), dict):
        return {"verified": False, "result": "INCONCLUSIVE", "reason": "FRESH_AX_OBSERVATION_REQUIRED"}
    ax = after_observation["ax"]
    after = DOMAXObservationProvider().observe(ax=ax)
    if (getattr(before, "source", None) != "AX" or ax.get("permissionGranted") is not True
            or ax.get("applicationPid") != getattr(before, "application_pid", None)
            or ax.get("applicationBundleId") != getattr(before, "application_bundle_id", None)
            or ax.get("sessionId") != getattr(before, "session_id", None)):
        return {"verified": False, "result": "INCONCLUSIVE", "reason": "AX_APPLICATION_IDENTITY_MISMATCH"}
    areas = [element for element in after.elements if element.get("id") == area_id
             and element.get("label") == target_label and element.get("role") in {"AXScrollArea", "AXWebArea"}
             and element.get("visible") is True]
    ax_elements = [element for element in after.elements if isinstance(element, dict)]
    bar = _scrollbar_for_area(ax_elements, areas[0]) if len(areas) == 1 else None
    value = (bar.get("_scroll_value")
             if bar is not None and bar.get("id") == scrollbar_id else None)
    changed = (isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0
        and (float(value) - before_scroll_value > 0.001 if direction == "down"
             else before_scroll_value - float(value) > 0.001))
    now = time.time()
    fresh = (after.observation_id != before.observation_id
        and after.observed_at is not None and before.observed_at is not None
        and math.isfinite(after.observed_at) and math.isfinite(before.observed_at)
        and after.observed_at > before.observed_at and after.observed_at <= now + 2.0
        and before.observed_at <= now + 2.0 and now - after.observed_at <= 30.0
        and after.session_id == before.session_id and after.application_pid == before.application_pid
        and after.application_bundle_id == before.application_bundle_id)
    verified = fresh and len(areas) == 1 and changed
    return {"verified": verified, "result": "CONFIRMED" if verified else "NOT_CONFIRMED",
        "reason": None if verified else "SCROLL_POSITION_DID_NOT_MOVE_IN_REQUESTED_DIRECTION",
        "area_id": area_id, "scrollbar_id": scrollbar_id,
        "scroll_value_changed": bool(changed), "observation_id": after.observation_id}


def validate_chat_native_button_plan(plan: Any, question: str,
                                     observation: dict[str, Any], *,
                                     allow_visual_candidates: bool = False, allow_sensitive: bool = False) -> dict[str, Any]:
    """Validate a local-model native action proposal against fresh AX evidence.

    The model may translate an outcome into one exact button label and one
    concise visible postcondition. It cannot supply selectors, coordinates or
    action code; both the chosen button and the expected effect are checked
    deterministically before the existing action planner can issue a plan.
    """
    ax = observation.get("ax") if isinstance(observation, dict) else None
    if not isinstance(ax, dict):
        raise ValueError("NATIVE_CHAT_AX_OBSERVATION_REQUIRED")
    if not isinstance(plan, dict) or plan.get("decision") not in {"execute", "complete", "clarify"}:
        raise ValueError("NATIVE_CHAT_ACTION_PLAN_INVALID")
    if plan["decision"] == "clarify":
        clarification = " ".join(str(plan.get("clarification") or "").split())[:400]
        return {"decision": "clarify", "clarification": clarification or
                "Which visible action should I take to reach that outcome?"}

    state = DOMAXObservationProvider().observe(ax=ax)
    if (state.source != "AX" or ax.get("permissionGranted") is not True
            or not isinstance(ax.get("applicationPid"), int)
            or isinstance(ax.get("applicationPid"), bool) or ax["applicationPid"] <= 0
            or not isinstance(ax.get("applicationBundleId"), str)
            or not ax["applicationBundleId"].strip()):
        raise ValueError("AX_APPLICATION_IDENTITY_REQUIRED")
    if plan["decision"] == "complete":
        completion_text = " ".join(str(plan.get("completion_text") or "").split())
        if not completion_text or len(completion_text) > 240:
            raise ValueError("NATIVE_CHAT_COMPLETION_EVIDENCE_REQUIRED")
        now = time.time()
        if (state.observed_at is None or not math.isfinite(state.observed_at)
                or state.observed_at > now + 2.0 or now - state.observed_at > 30.0):
            raise ValueError("NATIVE_CHAT_COMPLETION_OBSERVATION_STALE")
        if state.confidence < 0.65 or state.uncertainty:
            raise ValueError("NATIVE_CHAT_COMPLETION_OBSERVATION_UNCERTAIN")
        if completion_text.casefold() not in "\n".join(state.text).casefold():
            raise ValueError("NATIVE_CHAT_COMPLETION_EVIDENCE_NOT_VISIBLE")
        return {"decision": "complete", "completion_text": completion_text,
                "question": str(question)[:1000]}
    target_label = " ".join(str(plan.get("target_label") or "").split())
    expected_text = " ".join(str(plan.get("expected_text") or "").split())
    if not target_label or len(target_label) > 120:
        raise ValueError("NATIVE_CHAT_TARGET_LABEL_INVALID")
    if not allow_sensitive and re.search(r"\b(?:delete|remove|purchase|buy|pay|send|submit|confirm|authorize|transfer|publish|install|reset|erase)\b|"
                 r"削除|消去|購入|支払|決済|送金|送信|提出|確定|認可|公開|インストール|初期化|リセット|消去",
                 target_label, re.IGNORECASE):
        raise ValueError("NATIVE_CHAT_ACTION_CONFIRMATION_REQUIRED")
    if not expected_text or len(expected_text) > 240:
        raise ValueError("NATIVE_CHAT_POSTCONDITION_INVALID")
    if expected_text.casefold() in "\n".join(state.text).casefold():
        raise ValueError("NATIVE_CHAT_POSTCONDITION_ALREADY_VISIBLE")

    buttons = []
    for element in state.elements:
        actions = element.get("supported_actions", element.get("supportedActions", []))
        label = " ".join(str(element.get("label") or "").split())
        ax_press = isinstance(actions, list) and "AXPress" in actions
        # This is only a semantic proposal. The existing button planner still
        # requires pointer permission and fresh tracked visual evidence before
        # it can issue an executable plan for a control without AX geometry.
        visual_candidate = (allow_visual_candidates and not ax_press
                            and bool(element.get("id")) and not _has_usable_ax_frame(element))
        if (element.get("role") == "AXButton" and label
                and element.get("visible") is not False and element.get("enabled") is True
                and ((element.get("visible") is True and ax_press) or visual_candidate)):
            buttons.append((label, element))
    matches = [(label, element) for label, element in buttons
               if label.casefold() == target_label.casefold()]
    if len(matches) != 1:
        raise ValueError("NATIVE_CHAT_TARGET_NOT_UNIQUELY_GROUNDED")
    return {"decision": "execute", "target_label": matches[0][0],
            "expected_text": expected_text, "question": str(question)[:1000]}
