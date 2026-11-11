"""Preflight and diagnostics for the pinned in-process Laya Choice runtime."""
from __future__ import annotations

import importlib.util
from importlib import metadata
from pathlib import Path
import os
import json
import threading
from typing import Any
from .model_telemetry import model_telemetry


LayaStatus = dict[str, Any]
LAYA_PACKAGE_VERSION = "0.3.5"
# The English root checkpoint and the standalone multilingual checkpoint are
# published at different repository revisions. Keep their pins independent.
LAYA_DEFAULT_MODEL_REVISION = "052592a15d198d9ad47da779604259b10b47b7aa"
LAYA_DEFAULT_ENGLISH_MODEL_REVISION = "1c5edc17a7acd8701df6fc341c0d179f1c62c982"
LOCK_PATH = Path(__file__).parents[1] / "laya-runtime.lock"


def _lock_metadata() -> dict[str, Any]:
    try:
        value = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _checkpoint_dir(cache_root: str, name: str) -> Path | None:
    """Resolve a staged release checkpoint without consulting the network."""
    if not cache_root:
        return None
    root = Path(cache_root)
    candidates = (root / name, root / "models" / name)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _checkpoint_ready(path: Path | None) -> bool:
    if path is None:
        return False
    required = ("rl_agent_config.json", "model.safetensors", "tokenizer/tokenizer.json")
    return all((path / item).is_file() for item in required)


class LayaRuntimeError(RuntimeError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def _serialized_laya_call(method):
    def invoke(self, *args, **kwargs):
        with self._runtime_lock:
            return method(self, *args, **kwargs)
    return invoke


class LayaDecisionProvider:
    """Small provider-neutral boundary for Laya Choice/Noul/Score.

    Loading is explicit and local-only.  It never downloads a package or
    checkpoint from a request path; callers must complete startup preflight.
    """
    def __init__(self, *, multilingual_model: str, english_model: str, revision: str,
                 english_revision: str = LAYA_DEFAULT_ENGLISH_MODEL_REVISION, device: str = "auto"):
        self.multilingual_model = multilingual_model
        self.english_model = english_model
        self.revision = revision
        self.english_revision = english_revision
        self.device = device
        self.loaded = False
        self.last_trace: dict[str, Any] = {}
        self._router: Any = None
        self.english_loaded = False
        self._runtime_lock = threading.RLock()

    @_serialized_laya_call
    def load(self) -> dict[str, Any]:
        status = preflight(multilingual_model=self.multilingual_model, english_model=self.english_model,
                           revision=self.revision, english_revision=self.english_revision, device=self.device)
        self.last_trace = {"laya_checkpoint": None, "laya_model_load_count": 0, "laya_cold_load_ms": 0.0,
                           "laya_device_backend": self.device, "status": status["health"]}
        if not status["ready"]:
            raise LayaRuntimeError(status["reason"], "Laya startup preflight is incomplete")
        try:
            import laya
            from laya import Router
            if status.get("cache_root"):
                os.environ.setdefault("HF_HOME", status["cache_root"])
            # ``preload`` is intentionally called here, during startup, never
            # from a request.  auto_task_detection stays disabled so
            # typed-decisions cannot silently become the API router.
            cache_root = str(status.get("cache_root") or "")
            multilingual_path = _checkpoint_dir(cache_root, "multilingual")
            self.english_loaded = False
            # All languages use one local checkpoint. Force the multilingual
            # route even for English so Router cannot load a second copy.
            self._router = Router(
                models={"multilingual": str(multilingual_path or self.multilingual_model)},
                max_loaded=1, default="multilingual", auto_task_detection=False,
                preload=False, device=None if self.device == "auto" else self.device,
            )
            # Router merges its upstream defaults into models. Remove those
            # unused Hub entries so no alternate checkpoint can be acquired.
            self._router.models = {"multilingual": str(multilingual_path or self.multilingual_model)}
            self._router.preload(["multilingual"])
        except (ImportError, AttributeError, OSError, RuntimeError) as exc:
            raise LayaRuntimeError("LAYA_MODEL_LOAD_FAILURE", "Pinned Laya checkpoints could not be preloaded") from exc
        self.loaded = True
        self.last_trace.update({"laya_model_load_count": 1, "status": "READY"})
        return self.last_trace

    @_serialized_laya_call
    def unload(self) -> None:
        self._router = None
        self.loaded = False
        self.english_loaded = False
        self.last_trace = {"status": "NOT_LOADED"}

    @_serialized_laya_call
    def choice(self, *, question: str, candidates: list[dict[str, Any]], language: str = "multilingual", instructions: str = "Which API source best answers this request?") -> dict[str, Any]:
        if not self.loaded:
            raise LayaRuntimeError("LAYA_NOT_LOADED", "Laya must be loaded during startup preflight")
        if not candidates or len(candidates) > 8:
            raise LayaRuntimeError("LAYA_INVALID_REQUEST", "choice candidates must contain 1..8 items")
        checkpoint = self.multilingual_model
        revision = self.revision
        self.last_trace.update({"laya_checkpoint": checkpoint, "laya_question_type": "choice",
                                "laya_candidate_count": len(candidates), "laya_routing_reason": language})
        model_telemetry.record(role="typed_local_decisions", model=checkpoint, runtime="laya", revision=revision, stage="CHOICE")
        if self._router is None:
            raise LayaRuntimeError("LAYA_RUNTIME_UNAVAILABLE", "Pinned Laya router is not loaded")
        criteria = {str(item["tool_id"]): str(item.get("criteria") or item.get("description") or item["tool_id"])
                    for item in candidates if isinstance(item, dict) and item.get("tool_id")}
        if not criteria:
            raise LayaRuntimeError("LAYA_INVALID_REQUEST", "choice candidates must contain tool_id values")
        from .model_traffic_logs import begin_model_traffic
        traffic_call = begin_model_traffic(
            role='typed_local_decisions', model=checkpoint, runtime='laya',
            request_body={"request": str(question)[:4000], "language": language,
                          "type": "choice", "instructions": instructions[:1000], "criteria": criteria})
        try:
            result = self._router.predict(
                {"request": str(question)[:4000]},
                {"api": {"type": "choice", "instructions": instructions[:1000], "criteria": criteria}},
                lang="ja",
            )
        except Exception as exc:
            traffic_call.finish(status='error', error_code=type(exc).__name__)
            raise LayaRuntimeError("LAYA_RUNTIME_FAILURE", "Laya Choice inference failed") from exc
        traffic_call.finish(status='success', response_body=result)
        answer = ((result.get("answers") or {}).get("api") if isinstance(result, dict) else None)
        selected = answer.get("choice") if isinstance(answer, dict) else None
        if selected not in criteria:
            raise LayaRuntimeError("LAYA_INVALID_RESPONSE", "Laya returned an unknown API candidate")
        self.last_trace.update({"laya_decision_outcome": selected,
                                "laya_confidence": answer.get("confidence") if isinstance(answer, dict) else None,
                                "laya_raw_distribution": answer.get("probabilities") if isinstance(answer, dict) else None})
        return {"tool_id": selected, "confidence": answer.get("confidence"), "raw": result}

    @_serialized_laya_call
    def _typed_predict(self, *, question: str, candidates: list[dict[str, Any]], kind: str,
                       language: str) -> dict[str, Any]:
        if not self.loaded:
            raise LayaRuntimeError("LAYA_NOT_LOADED", "Laya must be loaded during startup preflight")
        if not candidates or len(candidates) > 8:
            raise LayaRuntimeError("LAYA_INVALID_REQUEST", "typed decision candidates must contain 1..8 items")
        criteria = {str(item["tool_id"]): str(item.get("criteria") or item.get("description") or item["tool_id"])
                    for item in candidates if isinstance(item, dict) and item.get("tool_id")}
        if not criteria:
            raise LayaRuntimeError("LAYA_INVALID_REQUEST", "typed decision candidates must contain tool_id values")
        model_telemetry.record(role="typed_local_decisions", model=self.multilingual_model,
                               runtime="laya", revision=self.revision,
                               stage=kind.upper())
        from .model_traffic_logs import begin_model_traffic
        traffic_call = begin_model_traffic(
            role='typed_local_decisions', model=self.multilingual_model, runtime='laya',
            request_body={"request": str(question)[:4000], "language": language,
                          "type": kind, "criteria": criteria})
        try:
            result = self._router.predict(
                {"request": str(question)[:4000]},
                {"api": {"type": kind, "instructions": "Return a typed decision only.", "criteria": criteria}},
                lang="ja",
            )
        except Exception as exc:
            traffic_call.finish(status='error', error_code=type(exc).__name__)
            raise LayaRuntimeError("LAYA_RUNTIME_FAILURE", f"Laya {kind} inference failed") from exc
        traffic_call.finish(status='success', response_body=result)
        answer = ((result.get("answers") or {}).get("api") if isinstance(result, dict) else None)
        if not isinstance(answer, dict):
            raise LayaRuntimeError("LAYA_INVALID_RESPONSE", f"Laya {kind} returned no typed answer")
        selected = answer.get("choice") or answer.get("label") or answer.get("tool_id")
        if kind == "score":
            distribution = answer.get("probabilities") or answer.get("scores")
            if not isinstance(distribution, dict):
                raise LayaRuntimeError("LAYA_INVALID_RESPONSE", "Laya Score returned no score distribution")
            return {"scores": distribution, "confidence": answer.get("confidence"), "raw": result}
        if selected not in criteria:
            raise LayaRuntimeError("LAYA_INVALID_RESPONSE", f"Laya {kind} returned an unknown candidate")
        return {"tool_id": selected, "confidence": answer.get("confidence"), "raw": result}

    @_serialized_laya_call
    def noul(self, *, question: str, candidates: list[dict[str, Any]], language: str = "multilingual") -> dict[str, Any]:
        self.last_trace.update({"laya_question_type": "noul", "laya_routing_reason": language})
        return self._typed_predict(question=question, candidates=candidates, kind="noul", language=language)

    @_serialized_laya_call
    def score(self, *, question: str, candidates: list[dict[str, Any]], language: str = "multilingual") -> dict[str, Any]:
        self.last_trace.update({"laya_question_type": "score", "laya_routing_reason": language})
        return self._typed_predict(question=question, candidates=candidates, kind="score", language=language)


def preflight(*, multilingual_model: str = "convaiinnovations/laya-multilingual", english_model: str = "convaiinnovations/laya",
              revision: str = LAYA_DEFAULT_MODEL_REVISION,
              english_revision: str = LAYA_DEFAULT_ENGLISH_MODEL_REVISION, device: str = "auto") -> LayaStatus:
    """Report release readiness without downloading or building on a request path."""
    package_available = importlib.util.find_spec("laya") is not None
    torch_available = importlib.util.find_spec("torch") is not None
    pinned = bool(revision and revision != "UNPINNED")
    english_pinned = bool(english_revision and english_revision != "UNPINNED")
    try:
        installed_version = metadata.version("laya")
    except metadata.PackageNotFoundError:
        installed_version = None
    cache_root = os.environ.get("OLCR_LAYA_CACHE", "")
    if not cache_root:
        from .optional_runtimes import runtime as components
        if components.installed('laya'): cache_root = str(components.path('laya'))
    lock = _lock_metadata()
    cache_marker = Path(cache_root) / "olcr-laya-cache.json" if cache_root else None

    multilingual_path = _checkpoint_dir(cache_root, "multilingual")
    # The multilingual checkpoint is the required production path.  English
    # remains an optional model and must not block Japanese/general routing.
    paths_ready = _checkpoint_ready(multilingual_path)
    cache_verified = os.environ.get("OLCR_LAYA_CHECKPOINTS_READY", "").lower() == "true"
    if cache_marker and cache_marker.is_file():
        try:
            marker = __import__("json").loads(cache_marker.read_text(encoding="utf-8"))
            cache_verified = (marker.get("model_revision") == revision
                              and marker.get("multilingual") is True
                              and paths_ready)
        except (OSError, ValueError, TypeError):
            cache_verified = False
    elif cache_verified:
        # Environment flags are accepted for packaged layouts only when the
        # expected checkpoint files are actually present.
        cache_verified = paths_ready
    package_pinned = installed_version == LAYA_PACKAGE_VERSION
    pinned_revisions = lock.get("model_revisions") or {"multilingual": lock.get("model_revision")}
    lock_valid = (lock.get("package") == "laya" and lock.get("package_version") == LAYA_PACKAGE_VERSION
                  and pinned_revisions == {"multilingual": revision})
    english_path = None
    english_ready = False
    ready = bool(package_available and torch_available and pinned and package_pinned and lock_valid and cache_verified)
    return {
        "required": True,
        "provider": "LAYA",
        "in_process": True,
        "cloud_dependency": False,
        "package": "laya",
        "package_version_required": LAYA_PACKAGE_VERSION,
        "package_version_installed": installed_version,
        "package_version_pinned": package_pinned,
        "lockfile": str(LOCK_PATH),
        "lockfile_valid": lock_valid,
        "package_available": package_available,
        "torch_available": torch_available,
        "multilingual_model": multilingual_model,
        "english_model": english_model,
        "model_revision": revision,
        "model_revisions": {"multilingual": revision},
        "english_model_revision_pinned": english_pinned,
        "model_revision_pinned": pinned,
        "device": device,
        "cache_root_configured": bool(cache_root),
        "cache_root": cache_root or None,
        "checkpoints_cached": cache_verified,
        "english_checkpoint_path": str(english_path) if english_path else None,
        "english_checkpoint_ready": english_ready,
        "multilingual_checkpoint_path": str(multilingual_path) if multilingual_path else None,
        "request_path_download_forbidden": True,
        "ready": ready,
        "health": "READY" if ready else "DEGRADED",
        "reason": "READY" if ready else "LAYA_PREFLIGHT_INCOMPLETE",
    }
