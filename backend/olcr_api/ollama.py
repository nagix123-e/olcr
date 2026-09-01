from __future__ import annotations

from abc import ABC, abstractmethod
import json
import time
from typing import Any, Iterator
from urllib import request, error


class ModelFailure(RuntimeError):
    def __init__(self, category: str, message: str): super().__init__(message); self.category = category


class ModelProvider(ABC):
    @abstractmethod
    def generate(self, messages: list[dict[str, str]], model: str, stream: bool = False) -> Any: ...


class OllamaProvider(ModelProvider):
    def __init__(self, endpoint: str, timeout: float = 60): self.endpoint, self.timeout = endpoint.rstrip("/"), timeout
    def generate(self, messages: list[dict[str, str]], model: str, stream: bool = False) -> Any:
        if not model: raise ModelFailure("configuration", "No Ollama model configured")
        payload = json.dumps({"model": model, "messages": messages, "stream": stream}).encode()
        req = request.Request(self.endpoint + "/api/chat", data=payload, headers={"Content-Type": "application/json"})
        started = time.perf_counter()
        try:
            response = request.urlopen(req, timeout=self.timeout)
            if stream: return self._stream(response, started)
            data = json.load(response)
            return {"text": data.get("message", {}).get("content", ""), "prompt_tokens": data.get("prompt_eval_count"),
                    "completion_tokens": data.get("eval_count"), "latency_ms": (time.perf_counter()-started)*1000}
        except error.URLError as exc: raise ModelFailure("unavailable", str(exc.reason)) from exc
        except TimeoutError as exc: raise ModelFailure("timeout", "Ollama request timed out") from exc
        except (ValueError, KeyError) as exc: raise ModelFailure("invalid_response", str(exc)) from exc
    def _stream(self, response: Any, started: float) -> Iterator[dict[str, Any]]:
        try:
            for raw in response:
                data = json.loads(raw); yield {"text": data.get("message", {}).get("content", ""), "done": data.get("done", False), "latency_ms": (time.perf_counter()-started)*1000,
                    "prompt_tokens": data.get("prompt_eval_count"), "completion_tokens": data.get("eval_count")}
        finally: response.close()
