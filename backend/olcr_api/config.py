from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from typing import Any


@dataclass
class Settings:
    ollama_endpoint: str = "http://127.0.0.1:11434"
    main_model: str = ""
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
    db_path: str = str(Path.home() / ".olcr" / "olcr.db")

    @classmethod
    def from_env(cls) -> "Settings":
        roots = tuple(filter(None, os.environ.get("OLCR_ALLOWED_ROOTS", os.getcwd()).split(os.pathsep)))
        return cls(
            ollama_endpoint=os.environ.get("OLLAMA_ENDPOINT", cls.ollama_endpoint),
            main_model=os.environ.get("OLLAMA_MODEL", ""),
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
            db_path=os.environ.get("OLCR_DB_PATH", cls.db_path),
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
        self.allowed_roots = tuple(str(Path(p).expanduser().resolve()) for p in self.allowed_roots)
        return self

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["allowed_roots"] = list(self.allowed_roots)
        return value

    def with_overrides(self, values: dict[str, Any]) -> "Settings":
        allowed=set(self.__dataclass_fields__); unknown=set(values)-allowed
        if unknown: raise ValueError(f"unknown settings: {sorted(unknown)}")
        merged=self.public_dict(); merged.update(values); merged["allowed_roots"]=tuple(merged["allowed_roots"])
        return Settings(**merged).validated()
