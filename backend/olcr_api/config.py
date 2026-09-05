from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_MAIN_MODEL = "qwen3:14b"
MODEL_REQUEST_TIMEOUT_SECONDS = 750
MODEL_NAME_FIELDS = frozenset({"main_model", "router_model", "embedding_model", "semantic_judge_model"})


def default_db_path() -> str:
    """The one default database used by the packaged CLI, backend, and setup state."""
    return str(Path(os.environ.get("OLCR_APP_SUPPORT", Path.home() / "Library" / "Application Support" / "OLCR")) / "olcr.db")

@dataclass
class Settings:
    ollama_endpoint: str = "http://127.0.0.1:11434"
    main_model: str = DEFAULT_MAIN_MODEL
    vision_model: str = "qwen2.5vl:3b"
    vision_num_ctx: int = 4096
    vision_keep_alive: str = "10m"
    router_model: str = ""
    embedding_model: str = ""
    semantic_judge_model: str = ""
    reranker_enabled: bool = False
    reranker_model: str = "Qwen/Qwen3-Reranker-0.6B"
    reranker_threshold: float = 0.01
    allowed_roots: tuple[str, ...] = ()
    vector_enabled: bool = False
    context_budget: int = 8000
    result_limit: int = 20
    confirmation_policy: str = "explicit"
    web_mode: str = "off"
    web_provider: str = "none"
    db_path: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        roots = tuple(filter(None, os.environ.get("OLCR_ALLOWED_ROOTS", os.getcwd()).split(os.pathsep)))
        return cls(
            ollama_endpoint=os.environ.get("OLLAMA_ENDPOINT", cls.ollama_endpoint),
            main_model=os.environ.get("OLLAMA_MODEL") or cls.main_model,
            vision_model=os.environ.get("OLCR_VISION_MODEL", "qwen2.5vl:3b"),
            vision_num_ctx=int(os.environ.get("OLCR_VISION_NUM_CTX", "4096")),
            vision_keep_alive=os.environ.get("OLCR_VISION_KEEP_ALIVE", "10m"),
            router_model=os.environ.get("OLLAMA_ROUTER_MODEL", ""),
            embedding_model=os.environ.get("OLLAMA_EMBEDDING_MODEL", ""),
            semantic_judge_model=os.environ.get("OLLAMA_SEMANTIC_JUDGE_MODEL", ""),
            reranker_enabled=os.environ.get("OLCR_RERANKER_ENABLED", "false").lower() == "true",
            reranker_model=os.environ.get("OLCR_RERANKER_MODEL", "Qwen/Qwen3-Reranker-0.6B"),
            reranker_threshold=float(os.environ.get("OLCR_RERANKER_THRESHOLD", "0.01")),
            allowed_roots=roots,
            vector_enabled=os.environ.get("OLCR_VECTOR_ENABLED", "false").lower() == "true",
            context_budget=int(os.environ.get("OLCR_CONTEXT_BUDGET", "8000")),
            result_limit=int(os.environ.get("OLCR_RESULT_LIMIT", "20")),
            db_path=os.environ.get("OLCR_DB_PATH") or default_db_path(),
        ).validated()

    def validated(self) -> "Settings":
        if not self.ollama_endpoint.startswith(("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost")):
            raise ValueError("Ollama endpoint must be local")
        if not 256 <= self.context_budget <= 200_000:
            raise ValueError("context_budget out of range")
        if not 1 <= self.result_limit <= 200:
            raise ValueError("result_limit out of range")
        if self.reranker_threshold < 0:
            raise ValueError("reranker_threshold must be non-negative")
        if self.web_mode not in {"off", "manual", "auto"}: raise ValueError("web_mode must be off, manual, or auto")
        if self.web_provider not in {"none", "brave", "tavily", "duckduckgo"}: raise ValueError("unsupported web provider")
        self.allowed_roots = tuple(str(Path(p).expanduser().resolve()) for p in self.allowed_roots)
        return self

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["allowed_roots"] = list(self.allowed_roots)
        return value

    def with_overrides(self, values: dict[str, Any]) -> "Settings":
        allowed=set(self.__dataclass_fields__); unknown=set(values)-allowed
        if unknown: raise ValueError(f"unknown settings: {sorted(unknown)}")
        merged=self.public_dict()
        # A legacy empty model setting means "unset". It must never erase an
        # explicit environment value or the packaged default.
        merged.update({key: value for key, value in values.items() if key not in MODEL_NAME_FIELDS or bool(str(value).strip())})
        merged["allowed_roots"]=tuple(merged["allowed_roots"])
        return Settings(**merged).validated()
