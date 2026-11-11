"""Stable identities for model artifacts and the runtimes that load them.

Runtime profile IDs are compatibility generations. A future package update must
add a new ID instead of changing the packages behind an existing ID.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any


SCHEMA_VERSION = 2
_RECIPES = Path(__file__).with_name("runtime_recipes") / "recommended-model-profiles.json"


@dataclass(frozen=True)
class RuntimeProfile:
    id: str
    loader_id: str
    artifact_format: str
    packages: tuple[str, ...] = ()
    managed: bool = True
    verify_imports: tuple[str, ...] = ()

    @property
    def package_lock_id(self) -> str:
        if not self.managed:
            return f"external:{self.id}"
        normalized = "\n".join(sorted(self.packages)) + "\n"
        return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AssetIdentity:
    asset_id: str
    repository: str
    revision: str | None
    artifact_format: str
    loader_id: str
    runtime_profile_id: str
    package_lock_id: str


@dataclass(frozen=True)
class AssetRecord:
    schema_version: int
    asset_id: str | None
    repository: str
    revision: str | None
    artifact_format: str | None = None
    loader_id: str | None = None
    runtime_profile_id: str | None = None
    package_lock_id: str | None = None
    verified_files: tuple[dict[str, Any], ...] = ()
    verified_at: float | None = None


_PROFILES: dict[str, RuntimeProfile] = {
    "mlx-text-v1": RuntimeProfile(
        "mlx-text-v1", "mlx-lm", "mlx-safetensors",
        ("llguidance==1.8.0", "mlx==0.32.0", "mlx-lm==0.32.0"),
        verify_imports=("mlx", "mlx_lm", "llguidance.hf")),
    "mlx-vlm-legacy-v1": RuntimeProfile(
        "mlx-vlm-legacy-v1", "bonsai-vision-artifact", "mlx-bonsai-custom-runtime",
        ("llguidance==1.8.0", "mlx==0.32.0", "mlx-lm==0.32.0", "mlx-vlm==0.6.3"),
        verify_imports=("mlx", "mlx_lm", "mlx_vlm", "llguidance.hf")),
    "mlx-vlm-v1": RuntimeProfile(
        "mlx-vlm-v1", "mlx-vlm", "mlx-safetensors",
        ("llguidance==1.8.0", "mlx==0.32.0", "mlx-lm==0.32.0", "mlx-vlm==0.7.8"),
        verify_imports=("mlx", "mlx_lm", "mlx_vlm", "llguidance.hf")),
    "mlx-audio-v1": RuntimeProfile(
        "mlx-audio-v1", "mlx-audio", "liquid-audio-snapshot",
        ("mlx==0.32.0", "mlx-audio==0.4.6"),
        verify_imports=("mlx", "mlx_audio.sts.models.lfm_audio")),
    "mlx-whisper-v1": RuntimeProfile(
        "mlx-whisper-v1", "mlx-whisper", "mlx-whisper-snapshot",
        ("mlx==0.32.0", "mlx-whisper==0.4.3"),
        verify_imports=("mlx", "mlx_whisper")),
    "mlx-plamo-v1": RuntimeProfile(
        "mlx-plamo-v1", "plamo-translate-cli", "mlx-safetensors",
        ("llguidance==1.8.0", "mlx==0.32.0", "mlx-lm==0.32.0", "plamo-translate==1.2.0"),
        verify_imports=("mlx", "mlx_lm", "llguidance.hf")),
    "ollama-external-v1": RuntimeProfile(
        "ollama-external-v1", "ollama", "ollama-model-store", managed=False),
    "laya-external-v1": RuntimeProfile(
        "laya-external-v1", "laya-runtime-component", "laya-model-payload", managed=False),
}
_RUNTIME_LOCK = Path(__file__).with_name("runtime_recipes") / "mlx-runtime-lock.json"


def runtime_profiles() -> tuple[RuntimeProfile, ...]:
    return tuple(_PROFILES.values())


def validate_runtime_lock() -> dict[str, Any]:
    """Reject a stale package-lock document before preparing an environment."""
    try:
        value = json.loads(_RUNTIME_LOCK.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("MODEL_RUNTIME_LOCK_UNAVAILABLE") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeError("MODEL_RUNTIME_LOCK_INVALID")
    rows = value.get("profiles")
    if not isinstance(rows, dict):
        raise RuntimeError("MODEL_RUNTIME_LOCK_INVALID")
    if set(rows) != set(_PROFILES):
        raise RuntimeError("MODEL_RUNTIME_LOCK_PROFILE_SET_MISMATCH")
    for profile_id, profile in _PROFILES.items():
        row = rows.get(profile_id)
        expected = {
            "loader_id": profile.loader_id,
            "artifact_format": profile.artifact_format,
            "packages": sorted(profile.packages),
            "package_lock_id": profile.package_lock_id,
            "managed": profile.managed,
            "verify_imports": list(profile.verify_imports),
        }
        if not isinstance(row, dict) or any(row.get(key) != item for key, item in expected.items()):
            raise RuntimeError("MODEL_RUNTIME_LOCK_PROFILE_MISMATCH")
    return value

_FALLBACK_ASSETS: dict[str, tuple[str, str, str | None]] = {
    # Compatibility for the role manifest entries not duplicated in the
    # landing preset catalog. Revisions are deployment revisions when known.
    "ternary_bonsai_2_27b": ("mlx-vlm-legacy-v1", "mlx-bonsai-custom-runtime", "fcba37d2117a7077eac6b613b2668d14d9779edd"),
    "ui_venus_mlx_6bit": ("mlx-vlm-legacy-v1", "mlx-safetensors", "95e3994ac9d74fe047cf44c072f6569db62856e7"),
    "fastvlm_mlx_bf16": ("mlx-vlm-legacy-v1", "mlx-safetensors", "81ffe929046666c43de53691147b1669ba0f3a4c"),
    "ornith_1_5_9b": ("mlx-text-v1", "mlx-safetensors", None),
    "laya_multilingual": ("laya-external-v1", "laya-model-payload", None),
    "embeddinggemma": ("ollama-external-v1", "ollama-model-store", None),
    "transcription_ja": ("mlx-audio-v1", "liquid-audio-snapshot", None),
    "transcription_en": ("mlx-audio-v1", "liquid-audio-snapshot", None),
    "lfm2_5_audio_1_5b": ("mlx-audio-v1", "liquid-audio-snapshot", None),
}


@lru_cache(maxsize=1)
def _catalog_models() -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(_RECIPES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    models = value.get("models") if isinstance(value, dict) else None
    return models if isinstance(models, dict) else {}


def runtime_profile_for_asset(asset_id: str, revision: str | None) -> RuntimeProfile:
    """Resolve a curated asset to its immutable loader/runtime generation.

    Unknown assets fail closed. The caller can still report their files as
    downloaded, but must not silently claim runtime compatibility.
    """
    asset_id = str(asset_id or "").strip()
    if not asset_id:
        raise ValueError("MODEL_RUNTIME_ASSET_ID_REQUIRED")
    row = _catalog_models().get(asset_id)
    if row:
        profile_id = str(row.get("runtime_profile_id") or "")
        if not profile_id:
            raise ValueError("MODEL_RUNTIME_PROFILE_UNDECLARED")
        expected_revision = row.get("revision")
        if revision and expected_revision and str(revision) != str(expected_revision):
            raise ValueError("MODEL_RUNTIME_REVISION_UNSUPPORTED")
        profile = _PROFILES.get(profile_id)
        if profile is None:
            raise ValueError("MODEL_RUNTIME_PROFILE_UNKNOWN")
        return RuntimeProfile(
            id=profile.id,
            loader_id=str(row.get("loader_id") or profile.loader_id),
            artifact_format=str(row.get("artifact_format") or profile.artifact_format),
            packages=profile.packages,
            managed=profile.managed,
            verify_imports=profile.verify_imports,
        )

    fallback = _FALLBACK_ASSETS.get(asset_id)
    if fallback:
        profile_id, artifact_format, expected_revision = fallback
        if revision and expected_revision and str(revision) != expected_revision:
            raise ValueError("MODEL_RUNTIME_REVISION_UNSUPPORTED")
        profile = _PROFILES[profile_id]
        return RuntimeProfile(profile.id, profile.loader_id, artifact_format,
                              profile.packages, profile.managed, profile.verify_imports)

    if asset_id.startswith("whisper_"):
        profile = _PROFILES["mlx-whisper-v1"]
        return RuntimeProfile(profile.id, profile.loader_id, profile.artifact_format,
                              profile.packages, profile.managed, profile.verify_imports)
    raise ValueError("MODEL_RUNTIME_PROFILE_UNDECLARED")


def asset_identity_for_entry(entry: dict[str, Any]) -> AssetIdentity:
    asset_id = str(entry.get("id") or "")
    revision_value = entry.get("revision") or entry.get("canonical_revision") or entry.get("deployment_revision")
    revision = str(revision_value) if revision_value else None
    profile = runtime_profile_for_asset(asset_id, revision)
    repository = str(entry.get("repo") or entry.get("canonical_repo") or "")
    if not repository:
        raise ValueError("MODEL_ASSET_REPOSITORY_REQUIRED")
    return AssetIdentity(
        asset_id=asset_id,
        repository=repository,
        revision=revision,
        artifact_format=str(entry.get("artifact_format") or profile.artifact_format),
        loader_id=str(entry.get("loader_id") or profile.loader_id),
        runtime_profile_id=profile.id,
        package_lock_id=profile.package_lock_id,
    )


def verified_file_inventory(path: Path) -> tuple[dict[str, Any], ...]:
    """Capture a stable name/size inventory after the caller validates payload structure."""
    root = Path(path)
    if not root.is_dir():
        raise ValueError("MODEL_ASSET_DIRECTORY_REQUIRED")
    rows: list[dict[str, Any]] = []
    for candidate in sorted(root.rglob("*")):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root)
        if ".cache" in relative.parts or candidate.name.startswith(".olcr-complete.json"):
            continue
        rows.append({"path": relative.as_posix(), "size_bytes": candidate.stat().st_size})
    if not rows:
        raise ValueError("MODEL_ASSET_INVENTORY_EMPTY")
    return tuple(rows)


def _marker_path(path: Path) -> Path:
    path = Path(path)
    return path if path.name == ".olcr-complete.json" else path / ".olcr-complete.json"


def read_asset_record(path: Path, expected: AssetIdentity) -> AssetRecord | None:
    """Read legacy or current completion metadata without changing the asset."""
    marker = _marker_path(path)
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    repository = str(value.get("repository") or value.get("repo") or "")
    revision_value = value.get("revision")
    revision = str(revision_value) if revision_value else None
    if repository != expected.repository or revision != expected.revision:
        return None
    try:
        version = int(value.get("schema_version") or 1)
    except (TypeError, ValueError):
        return None
    if version == 1:
        # Old markers identify only the repository/revision. The caller must
        # re-check payload structure before writing a schema-2 record.
        return AssetRecord(1, None, repository, revision)
    if version != SCHEMA_VERSION:
        return None
    identity = (
        value.get("asset_id"), value.get("artifact_format"), value.get("loader_id"),
        value.get("runtime_profile_id"),
    )
    wanted = (
        expected.asset_id, expected.artifact_format, expected.loader_id,
        expected.runtime_profile_id, expected.package_lock_id,
    )
    identity = identity + (value.get("package_lock_id"),)
    if identity != wanted:
        return None
    files = value.get("verified_files")
    if not isinstance(files, list) or not files:
        return None
    root = marker.parent
    normalized: list[dict[str, Any]] = []
    for row in files:
        if not isinstance(row, dict):
            return None
        relative = row.get("path")
        size = row.get("size_bytes")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            return None
        if not isinstance(size, int) or size < 0:
            return None
        candidate = root / relative
        try:
            if not candidate.is_file() or candidate.stat().st_size != size:
                return None
        except OSError:
            return None
        normalized.append({"path": relative, "size_bytes": size})
    return AssetRecord(
        schema_version=version,
        asset_id=expected.asset_id,
        repository=repository,
        revision=revision,
        artifact_format=expected.artifact_format,
        loader_id=expected.loader_id,
        runtime_profile_id=expected.runtime_profile_id,
        package_lock_id=expected.package_lock_id,
        verified_files=tuple(normalized),
        verified_at=value.get("verified_at"),
    )


def make_asset_record(path: Path, identity: AssetIdentity) -> AssetRecord:
    return AssetRecord(
        schema_version=SCHEMA_VERSION,
        asset_id=identity.asset_id,
        repository=identity.repository,
        revision=identity.revision,
        artifact_format=identity.artifact_format,
        loader_id=identity.loader_id,
        runtime_profile_id=identity.runtime_profile_id,
        package_lock_id=identity.package_lock_id,
        verified_files=verified_file_inventory(path),
        verified_at=time.time(),
    )


def write_asset_record(path: Path, record: AssetRecord) -> None:
    """Atomically replace only the metadata marker, never model payload files."""
    if record.schema_version != SCHEMA_VERSION:
        raise ValueError("MODEL_ASSET_RECORD_SCHEMA_INVALID")
    if (not record.asset_id or not record.repository or not record.artifact_format
            or not record.loader_id or not record.runtime_profile_id or not record.package_lock_id
            or not record.verified_files):
        raise ValueError("MODEL_ASSET_RECORD_INCOMPLETE")
    marker = _marker_path(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    value = asdict(record)
    value["repository"] = record.repository
    # Retain the legacy fields for older OLCR versions that only read these.
    value["repo"] = record.repository
    value["revision"] = record.revision
    value["verified_files"] = list(record.verified_files)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=marker.parent,
                                         prefix=marker.name + ".", suffix=".tmp", delete=False) as handle:
            temporary_name = handle.name
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, marker)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
