from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
import json
import base64
import os
import time
from typing import Any, Iterator
from urllib import request, error
import re
from .config import MODEL_REQUEST_TIMEOUT_SECONDS
from .generation_control import check_cancelled, interrupt_response, open_provider_response


class ModelFailure(RuntimeError):
    def __init__(self, category: str, message: str): super().__init__(message); self.category = category


def model_concurrency_failure(exc: error.HTTPError, model: str = "") -> ModelFailure | None:
    """Recognize an explicit capacity rejection without leaking provider bodies."""
    if exc.code not in {409, 429, 503}:
        return None
    try:
        detail = exc.read(8192).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    if not re.search(r"concurren(?:t|cy)|parallel.{0,40}(?:limit|maximum|exceed)|"
                     r"(?:limit|maximum|exceed).{0,40}parallel|model.{0,40}(?:busy|already running)|"
                     r"too many (?:active|running|in.flight) requests|already processing a request", detail, re.I):
        return None
    return ModelFailure("model_concurrency_limit",
                        "MODEL_CONCURRENCY_LIMIT: 指定モデル／プロバイダーの同時実行上限により、この会話の実行が拒否されました。"
                        "別の会話の完了後に再試行するか、別のモデルを選択してください。"
                        " / The selected model/provider rejected concurrent execution.")


_inference_deadline = ContextVar("olcr_inference_deadline", default=None)
_unbounded_inference = ContextVar('olcr_unbounded_inference', default=False)


@contextmanager
def unbounded_inference():
    """Allow callers to opt out of default timeouts unless they set a deadline."""
    token = _unbounded_inference.set(True)
    try:
        yield
    finally:
        _unbounded_inference.reset(token)


def inference_is_unbounded() -> bool:
    return _unbounded_inference.get()


@contextmanager
def inference_timeout(seconds: float):
    """Bound optional answer enrichment without changing normal model budgets."""
    token = _inference_deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _inference_deadline.reset(token)


def model_timeout_seconds(default: float) -> float | None:
    deadline = _inference_deadline.get()
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ModelFailure("timeout", "Model inference deadline exceeded")
        return min(default, remaining)
    return None if _unbounded_inference.get() else default


def provider_timeout_seconds(default: float) -> float:
    """Remote connection/read inactivity stays bounded during managed coding.

    This is a socket timeout, not a total generation or local inference budget.
    """
    deadline=_inference_deadline.get()
    if deadline is None: return default
    remaining=deadline-time.monotonic()
    if remaining<=0: raise ModelFailure('timeout','Provider answer composition timed out')
    return min(default,remaining)


class ModelProvider(ABC):
    @abstractmethod
    def generate(self, messages: list[dict[str, str]], model: str, stream: bool = False, think: bool | None = None, format: Any | None = None) -> Any: ...
    def vision(self, image_bytes: bytes, mime_type: str, prompt: str, model: str) -> Any:
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

    def generate(self, messages: list[dict[str, str]], model: str, stream: bool = False, think: bool | None = None, format: Any | None = None, options: dict[str, Any] | None = None) -> Any:
        check_cancelled()
        if not model: raise ModelFailure("configuration", "No Ollama model configured")
        from .model_progress import model_progress_active, current_model_invocation
        live_collect=not stream and (model_progress_active()
                                    or (inference_is_unbounded() and format is not None))
        body={"model": model, "messages": messages, "stream": stream or live_collect}
        if options: body["options"] = options
        request_options = self._request_options(think=think, format=format)
        request_options.update(options or {})
        if think is not None: body["think"] = think
        if format is not None:
            body["format"] = format
            print("MODEL_STRUCTURED_FORMAT_SENT=YES PROVIDER_FORMAT_FIELD=format OLLAMA_STRUCTURED_OUTPUT_PATH=/api/chat", file=__import__("sys").stderr, flush=True)
        payload = json.dumps(body).encode()
        req = request.Request(self.endpoint + "/api/chat", data=payload, headers={"Content-Type": "application/json"})
        started = time.perf_counter()
        try:
            response = open_provider_response(req, timeout=provider_timeout_seconds(self.timeout))
            if stream: return self._stream(response, started, request_options, current_model_invocation())
            if live_collect:
                text=[];reasoning=[];last={}
                for frame in self._stream(response,started,request_options,current_model_invocation()):
                    text.append(frame.get('text',''));last=frame
                    if frame.get('reasoning'):reasoning.append(frame['reasoning']['text'])
                from .thinking import received_reasoning
                return {**last,'text':''.join(text),'model':model,'reasoning':received_reasoning(''.join(reasoning)),
                        'model_runtime':'ollama','thinking_present':bool(reasoning),'thinking_chars':sum(map(len,reasoning))}
            with response, interrupt_response(response):
                data = json.load(response)
            thinking=data.get("message", {}).get("thinking")
            from .thinking import received_reasoning
            return {"text": data.get("message", {}).get("content", ""), "model": data.get("model") or model, "thinking_present": isinstance(thinking,str) and bool(thinking), "thinking_chars": len(thinking) if isinstance(thinking,str) else 0, "prompt_tokens": data.get("prompt_eval_count"),
                    'reasoning': received_reasoning(thinking), 'finish_reason': data.get('done_reason'),
                    "completion_tokens": data.get("eval_count"), "latency_ms": (time.perf_counter()-started)*1000,
                    "total_duration": data.get("total_duration"), "load_duration": data.get("load_duration"),
                    "prompt_eval_duration": data.get("prompt_eval_duration"), "eval_duration": data.get("eval_duration"),
                    "load_duration_ms": data.get("load_duration", 0) / 1_000_000 if isinstance(data.get("load_duration"), (int,float)) else None,
                    "model_runtime": "ollama", "model_engine": "UNKNOWN", "model_quantization": "UNKNOWN",
                    "request_options": request_options}

        except error.HTTPError as exc:
            failure = model_concurrency_failure(exc, model)
            if failure is not None:
                raise failure from None
            raise ModelFailure("provider_rejected", f"Ollama rejected the request (HTTP {exc.code})") from None
        except error.URLError as exc: raise ModelFailure("unavailable", str(exc.reason)) from exc
        except TimeoutError as exc: raise ModelFailure("timeout", "Ollama request timed out") from exc
        except (ValueError, KeyError) as exc: raise ModelFailure("invalid_response", str(exc)) from exc
    def vision(self, image_bytes: bytes, mime_type: str, prompt: str, model: str) -> Any:
        """Perception-only Ollama call using its native message images field."""
        message = {"role": "user", "content": prompt, "images": [base64.b64encode(image_bytes).decode("ascii")]}
        payload = json.dumps({"model": model, "messages": [message], "stream": False,
                              "keep_alive": os.environ.get("OLCR_VISION_KEEP_ALIVE", "10m"),
                              "options": {"num_ctx": int(os.environ.get("OLCR_VISION_NUM_CTX", "8192")),
                                          "num_predict": int(os.environ.get("OLCR_VISION_MAX_TOKENS", "1024")),
                                          "temperature": 0.0}}).encode()
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
    def _stream(self, response: Any, started: float, request_options: dict[str, Any], invocation_id=None) -> Iterator[dict[str, Any]]:
        from .thinking import ThinkingProgressGuard
        progress = ThinkingProgressGuard()
        reasoning_chars = output_chars = 0
        saw_answer = False
        last_progress = 0.0
        structured_format = request_options.get('format')
        structured_coding = inference_is_unbounded() and structured_format is not None and structured_format != 'OMITTED'
        try:
            with interrupt_response(response):
                for raw in response:
                    check_cancelled()
                    from .thinking import received_reasoning
                    data = json.loads(raw)
                    from .model_progress import model_progress
                    channel=data.get('message',{}).get('thinking')
                    reasoning_chars += len(channel or '')
                    content = data.get('message',{}).get('content') or ''
                    output_chars += len(content)
                    saw_answer = saw_answer or bool(content.strip())
                    now = time.monotonic()
                    if now - last_progress >= 1 or data.get('done'):
                        model_progress(invocation_id=invocation_id,state='worker_progress',
                                       event='ollama_generation_progress',reasoning_chars=reasoning_chars,
                                       output_chars=output_chars,generated_tokens=data.get('eval_count'),
                                       channel='reasoning' if channel else 'answer')
                        last_progress = now
                    if channel:model_progress(invocation_id=invocation_id,state='reasoning',text=channel,kind='trace',complete=bool(data.get('done')),model=data.get('model'))
                    if structured_coding and not saw_answer and reasoning_chars > 16384 and not data.get('done'):
                        model_progress(invocation_id=invocation_id,state='request_failed',failure='reasoning_budget',model=data.get('model'))
                        raise ModelFailure('reasoning_budget','THINKING_BUDGET: structured generation used its reasoning allowance without returning JSON')
                    if channel and not data.get('done') and progress.repeats(channel):
                        model_progress(invocation_id=invocation_id,state='request_failed',failure='reasoning_loop',model=data.get('model'))
                        raise ModelFailure('reasoning_loop','THINKING_REPETITION: repeated reasoning without an answer; generation stopped and saved work retained')
                    yield {"text": data.get("message", {}).get("content", ""), 'reasoning': received_reasoning(data.get('message', {}).get('thinking'), complete=bool(data.get('done'))), "done": data.get("done", False), "latency_ms": (time.perf_counter()-started)*1000,
                        "prompt_tokens": data.get("prompt_eval_count"), "completion_tokens": data.get("eval_count"), "finish_reason": data.get('done_reason'), "request_options": request_options}
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
