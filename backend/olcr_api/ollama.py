from __future__ import annotations

from abc import ABC, abstractmethod
import json
import base64
import os
import time
from typing import Any, Iterator
from urllib import request, error
import re
from .config import MODEL_REQUEST_TIMEOUT_SECONDS


class ModelFailure(RuntimeError):
    def __init__(self, category: str, message: str): super().__init__(message); self.category = category


class ModelProvider(ABC):
    @abstractmethod
    def generate(self, messages: list[dict[str, str]], model: str, stream: bool = False, think: bool | None = None, format: Any | None = None) -> Any: ...
    def vision(self, image_bytes: bytes, mime_type: str, prompt: str, model: str = "qwen2.5vl:7b") -> Any:
        raise ModelFailure("unsupported", "vision model is unavailable")


class OllamaProvider(ModelProvider):
    def __init__(self, endpoint: str, timeout: float = MODEL_REQUEST_TIMEOUT_SECONDS): self.endpoint, self.timeout = endpoint.rstrip("/"), timeout

    @staticmethod
    def _request_options(*, think: bool | None, format: Any | None) -> dict[str, Any]:
        """Describe effective request options without adding any to the request.

        Coding calls intentionally rely on Ollama/provider defaults.  Keeping
        omitted values explicit in telemetry makes that fact auditable while
        preserving the exact inference payload.
        """
        return {
            "num_ctx": "OMITTED", "num_batch": "OMITTED", "num_thread": "OMITTED",
            "num_gpu": "OMITTED", "keep_alive": "OMITTED", "temperature": "OMITTED",
            "top_p": "OMITTED", "top_k": "OMITTED", "seed": "OMITTED",
            "num_predict": "OMITTED", "think": bool(think) if think is not None else "OMITTED",
            "format": "EXPLICIT_JSON_SCHEMA" if isinstance(format, dict) else ("EXPLICIT" if format is not None else "OMITTED"),
        }

    def generate(self, messages: list[dict[str, str]], model: str, stream: bool = False, think: bool | None = None, format: Any | None = None) -> Any:
        if not model: raise ModelFailure("configuration", "No Ollama model configured")
        body={"model": model, "messages": messages, "stream": stream}
        if think is not None: body["think"] = think
        if format is not None:
            body["format"] = format
            print("MODEL_STRUCTURED_FORMAT_SENT=YES PROVIDER_FORMAT_FIELD=format OLLAMA_STRUCTURED_OUTPUT_PATH=/api/chat", file=__import__("sys").stderr, flush=True)
        payload = json.dumps(body).encode()
        req = request.Request(self.endpoint + "/api/chat", data=payload, headers={"Content-Type": "application/json"})
        started = time.perf_counter()
        try:
            response = request.urlopen(req, timeout=self.timeout)
            if stream: return self._stream(response, started, self._request_options(think=think, format=format))
            data = json.load(response)
            thinking=data.get("message", {}).get("thinking")
            return {"text": data.get("message", {}).get("content", ""), "thinking_present": isinstance(thinking,str) and bool(thinking), "thinking_chars": len(thinking) if isinstance(thinking,str) else 0, "prompt_tokens": data.get("prompt_eval_count"),
                    "completion_tokens": data.get("eval_count"), "latency_ms": (time.perf_counter()-started)*1000,
                    "total_duration": data.get("total_duration"), "load_duration": data.get("load_duration"),
                    "prompt_eval_duration": data.get("prompt_eval_duration"), "eval_duration": data.get("eval_duration"),
                    "load_duration_ms": data.get("load_duration", 0) / 1_000_000 if isinstance(data.get("load_duration"), (int,float)) else None,
                    "model_runtime": "ollama", "model_engine": "UNKNOWN", "model_quantization": "UNKNOWN",
                    "request_options": self._request_options(think=think, format=format)}

        except error.URLError as exc: raise ModelFailure("unavailable", str(exc.reason)) from exc
        except TimeoutError as exc: raise ModelFailure("timeout", "Ollama request timed out") from exc
        except (ValueError, KeyError) as exc: raise ModelFailure("invalid_response", str(exc)) from exc
    def vision(self, image_bytes: bytes, mime_type: str, prompt: str, model: str = "qwen2.5vl:7b") -> Any:
        """Perception-only Ollama call using its native message images field."""
        message = {"role": "user", "content": prompt, "images": [base64.b64encode(image_bytes).decode("ascii")]}
        payload = json.dumps({"model": model, "messages": [message], "stream": False,
                              "keep_alive": os.environ.get("OLCR_VISION_KEEP_ALIVE", "10m"),
                              "options": {"num_ctx": int(os.environ.get("OLCR_VISION_NUM_CTX", "8192"))}}).encode()
        req=request.Request(self.endpoint + "/api/chat", data=payload, headers={"Content-Type":"application/json"})
        started=time.perf_counter()
        try:
            with request.urlopen(req, timeout=self.timeout) as response: data=json.load(response)
            return {"text":data.get("message",{}).get("content",""),"latency_ms":(time.perf_counter()-started)*1000}
        except error.HTTPError as exc:
            try: detail=exc.read().decode("utf-8", "replace")[:500]
            except Exception: detail=""
            raise ModelFailure("vision_rejected", f"VISION_MODEL_REJECTED model={model}: {detail or exc.reason}") from exc
        except error.URLError as exc: raise ModelFailure("unavailable", str(exc.reason)) from exc
        except TimeoutError as exc: raise ModelFailure("timeout", "Ollama vision request timed out") from exc
    def _stream(self, response: Any, started: float, request_options: dict[str, Any]) -> Iterator[dict[str, Any]]:
        try:
            for raw in response:
                data = json.loads(raw); yield {"text": data.get("message", {}).get("content", ""), "done": data.get("done", False), "latency_ms": (time.perf_counter()-started)*1000,
                    "prompt_tokens": data.get("prompt_eval_count"), "completion_tokens": data.get("eval_count"), "request_options": request_options}
        finally: response.close()

    @staticmethod
    def _parameter_defaults(parameters: Any) -> dict[str, Any]:
        defaults: dict[str, Any] = {}
        for line in str(parameters or "").splitlines():
            match = re.match(r"\s*(temperature|top_p|top_k|num_ctx|num_batch|num_thread|num_gpu|seed|num_predict)\s+(.+?)\s*$", line)
            if not match:
                continue
            value: Any = match.group(2)
            try:
                value = float(value) if "." in value else int(value)
            except (TypeError, ValueError):
                pass
            defaults[match.group(1)] = value
        return defaults

    @classmethod
    def summarize_runtime_observation(cls, *, version: Any = None, ps: Any = None, show: Any = None, target_model: str = "") -> dict[str, Any]:
        """Return an allow-listed, read-only snapshot of Ollama runtime state."""
        loaded: list[dict[str, Any]] = []
        for item in (ps or {}).get("models", []) if isinstance(ps, dict) else []:
            if not isinstance(item, dict):
                continue
            details = item.get("details") if isinstance(item.get("details"), dict) else {}
            loaded.append({key: item.get(key, "UNKNOWN") for key in ("name", "context_length", "size", "size_vram", "expires_at")}
                          | {"details": {key: details.get(key, "UNKNOWN") for key in ("format", "quantization_level", "parameter_size")}})
        show_details = (show or {}).get("details") if isinstance(show, dict) and isinstance((show or {}).get("details"), dict) else {}
        show_info = (show or {}).get("model_info") if isinstance(show, dict) and isinstance((show or {}).get("model_info"), dict) else {}
        context_capacity = show_info.get("qwen35.context_length") or show_info.get("general.context_length") or "UNKNOWN"
        metadata = {
            "format": show_details.get("format", "UNKNOWN"),
            "quantization": show_details.get("quantization_level", "UNKNOWN"),
            "parameter_size": show_details.get("parameter_size", "UNKNOWN"),
            "context_capacity": context_capacity,
            "parameter_defaults": cls._parameter_defaults((show or {}).get("parameters") if isinstance(show, dict) else None),
        }
        target = next((row for row in loaded if row.get("name") == target_model), None)
        return {
            "status": "OK" if version is not None or ps is not None or show is not None else "UNKNOWN",
            "ollama_version": (version or {}).get("version", "UNKNOWN") if isinstance(version, dict) else "UNKNOWN",
            "target_model": target_model or "UNKNOWN", "target_loaded": bool(target),
            "model_metadata": metadata,
            "loaded_models": loaded,
            "model_context_active": target.get("context_length", "UNKNOWN") if target else "UNKNOWN",
            "model_memory_footprint": {"size": target.get("size", "UNKNOWN") if target else "UNKNOWN", "size_vram": target.get("size_vram", "UNKNOWN") if target else "UNKNOWN"},
            "processor_placement": "UNKNOWN", "gpu_offload": "UNKNOWN",
            "num_batch_effective": "UNKNOWN", "num_thread_effective": "UNKNOWN", "num_gpu_effective": "UNKNOWN",
            "daemon_keep_alive": "UNKNOWN", "sources": {"version": "/api/version", "ps": "/api/ps", "show": "/api/show"},
        }

    def runtime_observation(self, model: str) -> dict[str, Any]:
        """Read supported Ollama endpoints; never mutates runtime state."""
        version = ps = show = None
        try:
            with request.urlopen(self.endpoint + "/api/version", timeout=min(self.timeout, 5)) as response:
                version = json.load(response)
        except Exception:
            pass
        try:
            with request.urlopen(self.endpoint + "/api/ps", timeout=min(self.timeout, 5)) as response:
                ps = json.load(response)
        except Exception:
            pass
        try:
            payload = json.dumps({"name": model}).encode()
            req = request.Request(self.endpoint + "/api/show", data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with request.urlopen(req, timeout=min(self.timeout, 5)) as response:
                show = json.load(response)
        except Exception:
            pass
        return self.summarize_runtime_observation(version=version, ps=ps, show=show, target_model=model)
