"""Normalized perception plane and deterministic GUI vision routing."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import math
import re
from typing import Any


@dataclass(frozen=True)
class ScreenState:
    source: str
    width: int | None
    height: int | None
    elements: tuple[dict[str, Any], ...]
    text: tuple[str, ...]
    confidence: float
    uncertainty: tuple[str, ...]
    raw_model: str = ""
    observation_id: str = ""
    observed_at: float | None = None
    session_id: str = ""
    coordinate_space: str = ""
    application_pid: int | None = None
    application_bundle_id: str = ""
    permission_granted: bool = False

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


class DOMAXObservationProvider:
    """DOM/AX is authoritative when present; screenshots are evidence only."""
    def observe(self, *, dom: dict[str, Any] | None = None, ax: dict[str, Any] | None = None,
                image: dict[str, Any] | None = None) -> ScreenState:
        if isinstance(ax, dict):
            source = "AX"
            payload = ax
        elif isinstance(dom, dict):
            source = "DOM"
            payload = dom
        else:
            source = "IMAGE"
            payload = image if isinstance(image, dict) else {}
        uncertainty = _string_tuple(payload.get("uncertainty"))
        raw_elements = payload.get("elements", [])
        if isinstance(raw_elements, (list, tuple)):
            elements = tuple(item for item in raw_elements if isinstance(item, dict))
        else:
            elements = ()
            uncertainty += ("INVALID_ELEMENTS",)
        raw_text = payload.get("text", [])
        if isinstance(raw_text, (list, tuple)):
            text = tuple(str(item) for item in raw_text if item not in (None, ""))
        else:
            text = ()
            uncertainty += ("INVALID_TEXT",)
        if "confidence" in payload:
            confidence, confidence_valid = _confidence(payload.get("confidence"))
        else:
            confidence, confidence_valid = (1.0 if source in {"DOM", "AX"} else 0.0), True
        if not confidence_valid:
            uncertainty += ("INVALID_CONFIDENCE",)
        return ScreenState(source, payload.get("width"), payload.get("height"), elements, text,
                           confidence, uncertainty,
                           str(payload.get("model") or ""),
                           str(payload.get("observation_id") or payload.get("observationId") or ""),
                           _timestamp(payload.get("observed_at", payload.get("observedAt"))),
                           str(payload.get("session_id") or payload.get("sessionId") or ""),
                           str(payload.get("coordinate_space") or payload.get("coordinateSpace") or ""),
                           payload.get("application_pid", payload.get("applicationPid")),
                           str(payload.get("application_bundle_id") or payload.get("applicationBundleId") or ""),
                           payload.get("permission_granted", payload.get("permissionGranted")) is True)


class VisionRouter:
    MINIMUM_CONFIDENCE = 0.65

    def select_role(self, request: str, *, screen_state: ScreenState | None = None) -> str:
        if screen_state and screen_state.source in {"DOM", "AX"}:
            return "role:gui_grounding"
        value = str(request or "").lower()
        gui_english = ("screen", "screenshot", "screenshots", "screen shot", "screen shots",
                       "window", "windows", "dialog", "dialogs", "desktop", "app", "apps",
                       "interface", "control", "controls", "button", "buttons", "checkbox",
                       "checkboxes", "radio button", "radio buttons", "click", "tap", "field",
                       "fields", "text field", "text fields", "input field", "input fields",
                       "search bar", "search bars", "menu", "menus", "toolbar", "toolbars",
                       "panel", "panels", "cursor", "pointer", "ui")
        gui_japanese = ("画面", "スクリーンショット", "ダイアログ", "デスクトップ", "アプリ",
                        "インターフェース", "ボタン", "チェックボックス", "クリック", "入力欄")
        english_pattern = r"\b(?:" + "|".join(re.escape(token) for token in gui_english) + r")\b"
        return ("role:gui_grounding" if re.search(english_pattern, value)
                or any(token in value for token in gui_japanese) else "role:general_image")

    def fallback_required(self, state: ScreenState | None) -> bool:
        """Require a stronger vision model whenever normalized evidence is uncertain."""
        if not isinstance(state, ScreenState):
            return True
        confidence = _finite_confidence(state.confidence)
        if confidence is None or confidence < self.MINIMUM_CONFIDENCE or state.uncertainty:
            return True
        return not state.elements and not state.text

    def escalation_reason(self, state: ScreenState | None, request: str = "") -> str | None:
        """Return a stable reason for escalating uncertain visual evidence."""
        if not isinstance(state, ScreenState):
            return "UNKNOWN_REGION"
        if not self.fallback_required(state):
            return None
        value = str(request or "").lower()
        text_read = bool(re.search(r"read|text|ocr|文字|文章|書いて|書かれ|読ん", value))
        target_action = bool(re.search(
            r"click|tap|press|select|find|locate|button|control|checkbox|field|"
            r"クリック|押し|選択|探し|見つけ|ボタン|入力欄|操作", value))
        if text_read and (not state.text or state.confidence < self.MINIMUM_CONFIDENCE):
            return "OCR_INSUFFICIENT"
        if target_action and (state.uncertainty or state.confidence < self.MINIMUM_CONFIDENCE):
            return "AMBIGUOUS_TARGET"
        if not state.elements and not state.text:
            return "UNKNOWN_REGION"
        if state.uncertainty and any(item in state.uncertainty for item in ("INVALID_TEXT", "INVALID_ELEMENTS")):
            return "OCR_INSUFFICIENT" if text_read else "UNKNOWN_REGION"
        return "AMBIGUOUS_TARGET" if target_action else "OCR_INSUFFICIENT"

    @staticmethod
    def screen_state_from_visual(visual: dict[str, Any]) -> ScreenState:
        """Normalize a validated visual response before confidence routing."""
        raw_elements = visual.get("elements", [])
        elements = (tuple(item for item in raw_elements if isinstance(item, dict))
                    if isinstance(raw_elements, (list, tuple)) else ())
        uncertainty = _string_tuple(visual.get("uncertainty"))
        if not isinstance(raw_elements, (list, tuple)):
            uncertainty += ("INVALID_ELEMENTS",)
        raw_text = visual.get("text", [])
        if isinstance(raw_text, (list, tuple)):
            text = tuple(str(item) for item in raw_text if item not in (None, ""))
        else:
            text = ()
            uncertainty += ("INVALID_TEXT",)
        if "confidence" in visual:
            confidence, confidence_valid = _confidence(visual.get("confidence"))
            if not confidence_valid:
                uncertainty += ("INVALID_CONFIDENCE",)
        else:
            items_with_confidence = [item for item in elements if "confidence" in item]
            values = [value for item in items_with_confidence
                      if (value := _finite_confidence(item.get("confidence"))) is not None]
            confidence = (sum(values) / len(values) if values else
                          (0.0 if items_with_confidence else 1.0))
            if len(values) != len(items_with_confidence):
                uncertainty += ("INVALID_CONFIDENCE",)
        return ScreenState(
            source="IMAGE",
            width=visual.get("width"),
            height=visual.get("height"),
            elements=elements,
            text=text,
            confidence=confidence,
            uncertainty=uncertainty,
            raw_model=str(visual.get("model") or ""),
            observation_id=str(visual.get("observation_id") or visual.get("observationId") or ""),
            observed_at=_timestamp(visual.get("observed_at", visual.get("observedAt"))),
            session_id=str(visual.get("session_id") or visual.get("sessionId") or ""),
            coordinate_space=str(visual.get("coordinate_space") or visual.get("coordinateSpace") or ""),
        )


def _timestamp(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _finite_confidence(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _confidence(value: Any) -> tuple[float, bool]:
    parsed = _finite_confidence(value)
    if parsed is None:
        return 0.0, False
    return max(0.0, min(1.0, parsed)), True


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if item not in (None, ""))
