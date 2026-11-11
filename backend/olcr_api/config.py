from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
from typing import Any

DEFAULT_MAIN_MODEL = "ornith-1.5:9b"
DEFAULT_ROUTER_MODEL = "laya-choice"
DEFAULT_LAYA_MODEL_REVISION = "052592a15d198d9ad47da779604259b10b47b7aa"
DEFAULT_LAYA_ENGLISH_MODEL_REVISION = "1c5edc17a7acd8701df6fc341c0d179f1c62c982"
MODEL_REQUEST_TIMEOUT_SECONDS = 750
MODEL_NAME_FIELDS = frozenset({"main_model", "vision_model", "router_model", "embedding_model", "semantic_judge_model",
                               "planner_model", "short_coding_model", "long_implementation_model",
                               "gui_vision_model", "general_image_model"})


def default_db_path() -> str:
    """The one default database used by the packaged CLI, backend, and setup state."""
    return str(Path(os.environ.get("OLCR_APP_SUPPORT", Path.home() / "Library" / "Application Support" / "OLCR")) / "olcr.db")

@dataclass
class Settings:
    ollama_endpoint: str = "http://127.0.0.1:11434"
    main_model: str = DEFAULT_MAIN_MODEL
    # ``vision_model`` is kept as a public compatibility field.  It is a role
    # selector, never an Ollama model name; Runtime resolves it through the
    # manifest before the perception call.
    vision_model: str = "role:gui_grounding"
    vision_num_ctx: int = 4096
    vision_keep_alive: str = "10m"
    router_model: str = DEFAULT_ROUTER_MODEL
    embedding_model: str = "embeddinggemma:latest"
    semantic_judge_model: str = ""
    model_manifest_path: str = "models/model_manifest.json"
    planner_model: str = "role:planner_replanner"
    short_coding_model: str = "role:short_coding_fix_debug"
    long_implementation_model: str = "role:long_implementation"
    gui_vision_model: str = "role:gui_grounding"
    general_image_model: str = "role:general_image"
    reranker_enabled: bool = False
    reranker_model: str = ""
    reranker_threshold: float = 0.01
    laya_multilingual_model: str = "convaiinnovations/laya-multilingual"
    laya_english_model: str = "convaiinnovations/laya"
    laya_model_revision: str = DEFAULT_LAYA_MODEL_REVISION
    laya_english_model_revision: str = DEFAULT_LAYA_ENGLISH_MODEL_REVISION
    laya_device: str = "auto"
    allowed_roots: tuple[str, ...] = ()
    vector_enabled: bool = True
    graphify_enabled: bool = True
    conversation_memory_enabled: bool = True
    conversation_memory_cross_project_enabled: bool = False
    conversation_memory_min_relevance: float = 0.55
    yarn_enabled: bool = False
    yarn_target_tokens: int = 524288
    context_auto_compress: bool = False
    local_context_tokens: int = 32768
    context_budget: int = 8000
    rag_min_relevance: float = 0.55
    model_context_budgets: dict[str, int] = field(default_factory=dict)
    result_limit: int = 20
    confirmation_policy: str = "explicit"
    permission_level: str = "automatic"
    web_mode: str = "auto"
    web_provider: str = "brave"
    web_scraping_enabled: bool = True
    local_e2e_enabled: bool = True
    computer_use_enabled: bool = True
    convert_enabled: bool = False
    computer_use_allowed_bundle_ids: tuple[str, ...] = ()
    external_access_enabled: bool = True
    coding_stackoverflow_enabled: bool = True
    # Managed coding orchestration is the default chat route. An explicit
    # environment or persisted preference can still turn it off.
    task_manager_enabled: bool = True
    db_path: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        roots = tuple(filter(None, os.environ.get("OLCR_ALLOWED_ROOTS", os.getcwd()).split(os.pathsep)))
        return cls(
            ollama_endpoint=os.environ.get("OLLAMA_ENDPOINT", cls.ollama_endpoint),
            main_model=os.environ.get("OLLAMA_MODEL") or cls.main_model,
            vision_model=os.environ.get("OLCR_VISION_MODEL", "role:gui_grounding"),
            vision_num_ctx=int(os.environ.get("OLCR_VISION_NUM_CTX", "4096")),
            vision_keep_alive=os.environ.get("OLCR_VISION_KEEP_ALIVE", "10m"),
            router_model=os.environ.get("OLLAMA_ROUTER_MODEL", DEFAULT_ROUTER_MODEL),
            embedding_model=os.environ.get("OLLAMA_EMBEDDING_MODEL", cls.embedding_model),
            semantic_judge_model=os.environ.get("OLLAMA_SEMANTIC_JUDGE_MODEL", ""),
            model_manifest_path=os.environ.get("OLCR_MODEL_MANIFEST", "models/model_manifest.json"),
            planner_model=os.environ.get("OLCR_PLANNER_MODEL", "role:planner_replanner"),
            short_coding_model=os.environ.get("OLCR_SHORT_CODING_MODEL", "role:short_coding_fix_debug"),
            long_implementation_model=os.environ.get("OLCR_LONG_IMPLEMENTATION_MODEL", "role:long_implementation"),
            gui_vision_model=os.environ.get("OLCR_GUI_VISION_MODEL", "role:gui_grounding"),
            general_image_model=os.environ.get("OLCR_GENERAL_IMAGE_MODEL", "role:general_image"),
            reranker_enabled=os.environ.get("OLCR_RERANKER_ENABLED", "false").lower() == "true",
            reranker_model=os.environ.get("OLCR_RERANKER_MODEL", ""),
            reranker_threshold=float(os.environ.get("OLCR_RERANKER_THRESHOLD", "0.01")),
            laya_multilingual_model=os.environ.get("OLCR_LAYA_MULTILINGUAL_MODEL", cls.laya_multilingual_model),
            laya_english_model=os.environ.get("OLCR_LAYA_ENGLISH_MODEL", cls.laya_english_model),
            laya_model_revision=os.environ.get("OLCR_LAYA_MODEL_REVISION", cls.laya_model_revision),
            laya_english_model_revision=os.environ.get("OLCR_LAYA_ENGLISH_REVISION", cls.laya_english_model_revision),
            laya_device=os.environ.get("OLCR_LAYA_DEVICE", cls.laya_device),
            allowed_roots=roots,
            vector_enabled=os.environ.get("OLCR_VECTOR_ENABLED", "true").lower() == "true",
            graphify_enabled=os.environ.get("OLCR_GRAPHIFY_ENABLED", "true").lower() == "true",
            conversation_memory_enabled=os.environ.get("OLCR_CONVERSATION_MEMORY_ENABLED", "true").lower() == "true",
            conversation_memory_cross_project_enabled=os.environ.get("OLCR_CONVERSATION_MEMORY_CROSS_PROJECT_ENABLED", "false").lower() == "true",
            conversation_memory_min_relevance=float(os.environ.get("OLCR_CONVERSATION_MEMORY_MIN_RELEVANCE", "0.55")),
            context_budget=int(os.environ.get("OLCR_CONTEXT_BUDGET", "8000")),
            rag_min_relevance=float(os.environ.get("OLCR_RAG_MIN_RELEVANCE", "0.55")),
            result_limit=int(os.environ.get("OLCR_RESULT_LIMIT", "20")),
            db_path=os.environ.get("OLCR_DB_PATH") or default_db_path(),
            task_manager_enabled=os.environ.get("OLCR_TASK_MANAGER_ENABLED", "true").lower() == "true",
            coding_stackoverflow_enabled=os.environ.get("OLCR_CODING_STACKOVERFLOW_ENABLED", "true").lower() == "true",
        ).validated()

    def validated(self) -> "Settings":
        if self.permission_level not in {"confirm", "automatic", "full"}:
            raise ValueError("permission_level must be confirm, automatic, or full")
        if not self.ollama_endpoint.startswith(("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost")):
            raise ValueError("Ollama endpoint must be local")
        for name in ('yarn_enabled', 'context_auto_compress', 'convert_enabled',
                     'conversation_memory_enabled', 'conversation_memory_cross_project_enabled'):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        for name in ('yarn_target_tokens', 'local_context_tokens'):
            value = getattr(self, name)
            if type(value) is not int or not 2048 <= value <= 2097152:
                raise ValueError(f"{name} must be 2048–2097152 tokens")
        if self.yarn_enabled and self.yarn_target_tokens < self.local_context_tokens:
            raise ValueError("yarn_target_tokens must be at least local_context_tokens")
        if not 256 <= self.context_budget <= 200_000:
            raise ValueError("context_budget out of range")
        if (type(self.conversation_memory_min_relevance) not in (int, float)
                or not math.isfinite(self.conversation_memory_min_relevance)
                or not 0 <= self.conversation_memory_min_relevance <= 1):
            raise ValueError("conversation_memory_min_relevance must be finite and between 0 and 1")
        if not 0 < self.rag_min_relevance <= 1:
            raise ValueError("rag_min_relevance must be greater than 0 and at most 1")
        if (not isinstance(self.model_context_budgets, dict) or len(self.model_context_budgets) > 100
                or any(not isinstance(key, str) or not key.strip() or len(key) > 300
                       or type(value) is not int or not 256 <= value <= 200_000
                       for key, value in self.model_context_budgets.items())):
            raise ValueError("model_context_budgets must map model IDs to 256–200000 characters")
        if not 1 <= self.result_limit <= 200:
            raise ValueError("result_limit out of range")
        if self.reranker_threshold < 0:
            raise ValueError("reranker_threshold must be non-negative")
        if self.reranker_enabled:
            # Migrate persisted pre-Laya settings without ever constructing or
            # invoking the retired Qwen reranker in production.
            self.reranker_enabled = False
            self.reranker_model = ""
        # Old persisted settings must not reactivate retired production paths.
        # Preserve the setting record's shape while binding it to the current
        # role architecture.
        for field in ("main_model", "vision_model", "planner_model", "short_coding_model",
                      "long_implementation_model", "gui_vision_model", "general_image_model"):
            value = str(getattr(self, field) or "")
            lower = value.lower()
            if any(marker in lower for marker in ("qwen3.5", "qwen3:14b", "lfm", "qwen2.5vl")):
                setattr(self, field, DEFAULT_MAIN_MODEL if field == "main_model" else
                        ("role:gui_grounding" if field in {"vision_model", "gui_vision_model"} else
                         "role:planner_replanner" if field == "planner_model" else
                         "role:short_coding_fix_debug" if field == "short_coding_model" else
                         "role:long_implementation"))
        if self.laya_device not in {"auto", "cpu", "mps"}:
            raise ValueError("laya_device must be auto, cpu, or mps")
        if not self.laya_model_revision.strip() or self.laya_model_revision == "UNPINNED":
            # Startup remains usable for local development, but the preflight
            # gate reports this as not release-ready instead of pretending a
            # floating checkpoint is reproducible.
            pass
        if self.web_mode not in {"off", "manual", "auto"}: raise ValueError("web_mode must be off, manual, or auto")
        if self.web_provider not in {"none", "brave", "tavily", "duckduckgo"}: raise ValueError("unsupported web provider")
        allowed_apps = []
        for bundle_id in self.computer_use_allowed_bundle_ids:
            normalized = str(bundle_id).strip()
            if not normalized or len(normalized) > 256 or any(ord(char) < 32 for char in normalized):
                raise ValueError("computer_use_allowed_bundle_ids contains an invalid bundle ID")
            if normalized not in allowed_apps:
                allowed_apps.append(normalized)
        if len(allowed_apps) > 50:
            raise ValueError("computer_use_allowed_bundle_ids supports at most 50 apps")
        self.computer_use_allowed_bundle_ids = tuple(allowed_apps)
        if self.router_model.strip() == "embeddinggemma:latest": raise ValueError("router_model is not generative-compatible")
        self.allowed_roots = tuple(str(Path(p).expanduser().resolve()) for p in self.allowed_roots)
        return self

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["allowed_roots"] = list(self.allowed_roots)
        value["computer_use_allowed_bundle_ids"] = list(self.computer_use_allowed_bundle_ids)
        return value

    def with_overrides(self, values: dict[str, Any]) -> "Settings":
        # ``coding_model`` was briefly persisted by an intermediate build that
        # split the primary Qwen role. Keep accepting that legacy key so old
        # databases boot cleanly, but make the single primary model authoritative.
        values = dict(values)
        values.pop("coding_model", None)
        values.pop("vision_fallback_model", None)  # Retired role in saved profiles.
        if values.get("main_model") in {"qwen3:14b", "qwen3.5:9b"}:
            values["main_model"] = DEFAULT_MAIN_MODEL
        if str(values.get("vision_model", "")).lower().startswith(("qwen2.5vl", "qwen3.5", "lfm")):
            values["vision_model"] = "role:gui_grounding"
        allowed=set(self.__dataclass_fields__); unknown=set(values)-allowed
        # Project-scoped core context is persisted in the same local settings
        # table, but is not a process Settings field. Keep it out of startup
        # validation so a saved project cannot prevent the backend from booting.
        unknown = {key for key in unknown if not key.startswith(("project_core_context:", "project_core_source:", "conversation_project_context:"))}
        if unknown: raise ValueError(f"unknown settings: {sorted(unknown)}")
        merged=self.public_dict()
        # A legacy empty model setting means "unset". It must never erase an
        # explicit environment value or the packaged default.
        merged.update({key: value for key, value in values.items() if key in allowed and (key not in MODEL_NAME_FIELDS or bool(str(value).strip()))})
        merged["allowed_roots"]=tuple(merged["allowed_roots"])
        merged["computer_use_allowed_bundle_ids"] = tuple(merged.get("computer_use_allowed_bundle_ids") or ())
        return Settings(**merged).validated()
