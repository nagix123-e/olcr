"""The single runtime authority for OLCR model roles.

The readiness manifest is deliberately treated as data, not as a suggestion.
Request code resolves a role to one manifest entry and records the resulting
deployment/runtime/revision tuple before invoking a provider.  This keeps the
model selected by the UI, the backend and the benchmark artifacts auditable.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = REPO_ROOT / "models" / "model_manifest.json"
PACKAGED_MANIFEST_PATH = Path(__file__).with_name('runtime_recipes') / 'model-catalog.json'
if not DEFAULT_MANIFEST_PATH.is_file(): DEFAULT_MANIFEST_PATH = PACKAGED_MANIFEST_PATH
DEFAULT_READINESS_PATH = REPO_ROOT / "benchmark_results" / "model_readiness" / "readiness_report.json"

ROLE_TO_MODEL = {
    "semantic_embedding": "embeddinggemma",
    "typed_local_decisions": "laya_multilingual",
    "planner_replanner": "ternary_bonsai_2_27b",
    "short_coding_fix_debug": "ternary_bonsai_2_27b",
    "long_implementation": "ornith_1_5_9b",
    "gui_grounding": "ui_venus_mlx_6bit",
    "general_image": "fastvlm_mlx_bf16",
    "translation": "plamo_3_translate_2b",
    "normal_chat": "ornith_1_5_9b",
}

# Persisted opt-in presets live separately so importing or validating the
# registry never rewrites the immutable 32GB compatibility baseline above.
_EFFECTIVE_ROLE_TO_MODEL: dict[str, str] = {}


def set_effective_role_map(value: Mapping[str, str] | None) -> None:
    global _EFFECTIVE_ROLE_TO_MODEL
    if value is None:
        _EFFECTIVE_ROLE_TO_MODEL = {}
        return
    if (not isinstance(value, Mapping) or set(value) - set(ROLE_TO_MODEL)
            or any(not isinstance(model, str) or not model for model in value.values())):
        raise ValueError('MODEL_ROLE_MAP_INVALID')
    _EFFECTIVE_ROLE_TO_MODEL = dict(value)


def effective_role_map() -> dict[str, str]:
    return {**ROLE_TO_MODEL, **_EFFECTIVE_ROLE_TO_MODEL}


def _profile_model_rows() -> dict[str, dict[str, Any]]:
    path = Path(__file__).with_name('runtime_recipes') / 'recommended-model-profiles.json'
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    models = value.get('models') if isinstance(value, dict) else None
    rows = {}
    if not isinstance(models, dict):
        return rows
    for model_id, row in models.items():
        if not isinstance(row, dict) or row.get('type') not in {'huggingface', 'ollama'}:
            continue
        if not isinstance(row.get('repo'), str) or not isinstance(row.get('revision'), str):
            continue
        relative = str(row.get('relative') or '')
        is_ollama = row['type'] == 'ollama'
        configured_root = os.environ.get('OLCR_MODEL_ASSETS_DIR')
        local_path = ('Ollama model store' if is_ollama else
                      str(Path(configured_root) / relative) if configured_root else
                      str(REPO_ROOT / 'models' / relative))
        repo = 'ollama/' + row['repo'] if is_ollama else row['repo']
        rows[model_id] = {
            'id': model_id, 'role': row.get('role') or 'profile',
            'canonical_repo': row['repo'], 'canonical_revision': row['revision'],
            'deployment_repo': repo, 'deployment_revision': row['revision'],
            'runtime': row.get('runtime') or ('Ollama' if is_ollama else 'MLX'),
            'quantization': row.get('quantization') or row.get('runtime') or 'managed profile model',
            'local_path': local_path,
            'license': row.get('license') or 'UNKNOWN',
            'artifact_size_bytes_upstream': row.get('size_bytes'),
            'ready': 'YES',
        }
    return rows


class ModelRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelBinding:
    role: str
    model_id: str
    deployment_model: str
    canonical_repo: str
    canonical_revision: str
    deployment_repo: str
    deployment_revision: str
    runtime: str
    local_path: str
    ready: bool
    manifest_path: str
    manifest_sha256: str

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def _deployment_model(entry: Mapping[str, Any]) -> str:
    """Return the provider-facing model name from a manifest entry."""
    repo = str(entry.get("deployment_repo") or "")
    # Ollama deployment_repo values are already valid model names.  Local MLX
    # entries use their manifest id and are handled by the dedicated runtime.
    if repo.startswith("ollama/"):
        return repo.removeprefix("ollama/")
    return str(entry.get("id") or repo)


class ModelRegistry:
    def __init__(self, manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
                 readiness_path: str | Path = DEFAULT_READINESS_PATH):
        manifest = Path(manifest_path).expanduser()
        readiness = Path(readiness_path).expanduser()
        if not manifest.is_absolute() and (REPO_ROOT / manifest).is_file():
            manifest = REPO_ROOT / manifest
        elif manifest == Path('models/model_manifest.json') and not manifest.is_file():
            manifest = PACKAGED_MANIFEST_PATH
        if not readiness.is_absolute() and (REPO_ROOT / readiness).is_file():
            readiness = REPO_ROOT / readiness
        self.manifest_path = manifest.resolve()
        self.readiness_path = readiness.resolve()
        self._manifest: dict[str, Any] | None = None
        self._sha256: str | None = None
        self._readiness: dict[str, Any] | None = None

    @property
    def manifest(self) -> dict[str, Any]:
        if self._manifest is None:
            try:
                raw = self.manifest_path.read_bytes()
                value = json.loads(raw)
            except (OSError, ValueError) as exc:
                raise ModelRegistryError(f"MODEL_MANIFEST_INVALID: {self.manifest_path}") from exc
            if not isinstance(value, dict) or not isinstance(value.get("models"), list):
                raise ModelRegistryError("MODEL_MANIFEST_SCHEMA_INVALID")
            self._manifest = value
            self._sha256 = hashlib.sha256(raw).hexdigest()
        return self._manifest

    @property
    def manifest_sha256(self) -> str:
        _ = self.manifest
        assert self._sha256 is not None
        return self._sha256

    @property
    def readiness(self) -> dict[str, Any]:
        if self._readiness is None:
            try:
                value = json.loads(self.readiness_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                value = {}
            self._readiness = value if isinstance(value, dict) else {}
        return self._readiness

    def entries(self) -> dict[str, dict[str, Any]]:
        entries = {str(row.get("id")): row for row in self.manifest["models"]
                   if isinstance(row, dict) and row.get("id")}
        entries.update({key: row for key, row in _profile_model_rows().items() if key not in entries})
        return entries

    def entry_for_role(self, role: str) -> dict[str, Any]:
        model_id = _EFFECTIVE_ROLE_TO_MODEL.get(role, ROLE_TO_MODEL.get(role, role))
        entry = self.entries().get(model_id)
        if entry is None:
            raise ModelRegistryError(f"MODEL_ROLE_UNBOUND: {role}")
        return entry

    def binding(self, role: str) -> ModelBinding:
        entry = self.entry_for_role(role)
        return ModelBinding(
            role=role,
            model_id=str(entry["id"]),
            deployment_model=_deployment_model(entry),
            canonical_repo=str(entry.get("canonical_repo") or "UNKNOWN"),
            canonical_revision=str(entry.get("canonical_revision") or "UNKNOWN"),
            deployment_repo=str(entry.get("deployment_repo") or "UNKNOWN"),
            deployment_revision=str(entry.get("deployment_revision") or "UNKNOWN"),
            runtime=str(entry.get("runtime") or "UNKNOWN"),
            local_path=str(entry.get("local_path") or "UNKNOWN"),
            ready=str(entry.get("ready", "NO")).upper() == "YES",
            manifest_path=str(self.manifest_path),
            manifest_sha256=self.manifest_sha256,
        )

    def validate(self, required_roles: tuple[str, ...] | None = None) -> dict[str, Any]:
        roles = required_roles or tuple(ROLE_TO_MODEL)
        bindings: list[dict[str, Any]] = []
        errors: list[str] = []
        for role in roles:
            try:
                row = self.binding(role)
                bindings.append(row.public_dict())
                if not row.ready:
                    errors.append(f"{role}:NOT_READY")
            except ModelRegistryError as exc:
                errors.append(str(exc))
        return {"status": "PASS" if not errors else "FAIL", "errors": errors,
                "manifest_sha256": self.manifest_sha256, "bindings": bindings}

    def validate_ollama_inventory(self, installed_models: list[dict[str, Any]]) -> dict[str, Any]:
        """Compare pinned Ollama deployment revisions with live tag digests."""
        installed = {
            str(row.get("name") or ""): str(row.get("digest") or "").strip().lower().removeprefix('sha256:')
            for row in installed_models if isinstance(row, dict) and row.get("name")
        }
        bindings: list[dict[str, str]] = []
        errors: list[str] = []
        for entry in self.manifest["models"]:
            if not isinstance(entry, dict) or not str(entry.get("deployment_repo") or "").startswith("ollama/"):
                continue
            if entry.get('id') not in ROLE_TO_MODEL.values():
                continue
            model = _deployment_model(entry)
            expected = str(entry.get("deployment_revision") or "").strip().lower().removeprefix('sha256:')
            actual = installed.get(model, "")
            if not actual:
                status = "NOT_INSTALLED"
            elif len(expected) < 12 or not actual.startswith(expected):
                status = "DIGEST_MISMATCH"
            else:
                status = "MATCH"
            if status != "MATCH":
                errors.append(f"{entry.get('id')}:{status}")
            bindings.append({"model_id": str(entry.get("id") or "UNKNOWN"),
                             "model": model, "expected_revision": expected or "UNKNOWN",
                             "actual_digest": actual or "NOT_INSTALLED", "status": status})
        return {"status": "PASS" if not errors else "FAIL", "errors": errors,
                "manifest_sha256": self.manifest_sha256, "bindings": bindings}

    def public_status(self) -> dict[str, Any]:
        return {"manifest_path": str(self.manifest_path),
                "manifest_sha256": self.manifest_sha256,
                "bindings": {role: self.binding(role).public_dict() for role in ROLE_TO_MODEL}}


default_registry = ModelRegistry()
