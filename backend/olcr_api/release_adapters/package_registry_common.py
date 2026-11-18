"""Shared confined artifact and release gate helpers for package adapters."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..coding_tasks import ReleaseIntent, ReleasePreflight, build_release_preflight
from ..release_workflow import ReleaseRequest


MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARTIFACTS = 100


@dataclass(frozen=True)
class PackageArtifact:
    path: Path
    relative_path: Path
    filename: str
    size: int
    sha256: str
    sha512: str


def workspace_path(request: ReleaseRequest) -> Path | None:
    try:
        root = Path(request.project_root).expanduser().resolve(strict=True)
        return root if root.is_dir() else None
    except (OSError, RuntimeError, TypeError):
        return None


def open_confined_file(root: Path, relative: Path) -> tuple[int, os.stat_result]:
    """Open a workspace-relative regular file without following symlinks."""
    directory_flag = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if directory_flag is None or nofollow is None or os.open not in os.supports_dir_fd:
        raise OSError("SAFE_FILE_OPEN_UNAVAILABLE")
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError("ARTIFACT_PATH_INVALID")
    parent = os.open(root, os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0))
    current = parent
    descriptor = -1
    try:
        for part in relative.parts[:-1]:
            following = os.open(part, os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
                                dir_fd=current)
            if current != parent:
                os.close(current)
            current = following
        descriptor = os.open(relative.parts[-1], os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
                             dir_fd=current)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("ARTIFACT_NOT_REGULAR")
        result = descriptor
        descriptor = -1
        return result, info
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if current != parent:
            os.close(current)
        os.close(parent)


def local_artifacts(request: ReleaseRequest, *, suffixes: tuple[str, ...], exact_count: int | None = None):
    root = workspace_path(request)
    paths = request.artifact_paths if isinstance(request.artifact_paths, (tuple, list)) else ()
    if root is None:
        return (), "The package workspace is unavailable."
    if not paths or len(paths) > MAX_ARTIFACTS or (exact_count is not None and len(paths) != exact_count):
        return (), "The release requires a bounded set of local package artifacts."
    results = []
    filenames = set()
    total_bytes = 0
    for raw in paths:
        try:
            candidate = Path(raw)
            relative = candidate.relative_to(root) if candidate.is_absolute() else candidate
            if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                return (), "Package artifacts must remain inside the project workspace."
            filename = relative.name
            if (not filename or len(filename) > 240 or filename in filenames
                    or Path(filename).suffix.casefold() not in suffixes):
                return (), "Package artifact names or file types are invalid."
            descriptor, info = open_confined_file(root, relative)
            if info.st_size <= 0 or info.st_size > MAX_ARTIFACT_BYTES:
                os.close(descriptor)
                return (), "A package artifact is empty or exceeds the size limit."
            digest = hashlib.sha256()
            integrity_digest = hashlib.sha512()
            read_size = 0
            with os.fdopen(descriptor, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    read_size += len(chunk)
                    total_bytes += len(chunk)
                    if read_size > MAX_ARTIFACT_BYTES or total_bytes > MAX_ARTIFACT_BYTES:
                        return (), "Package artifacts exceed the combined size limit."
                    digest.update(chunk)
                    integrity_digest.update(chunk)
            if read_size != info.st_size:
                return (), "A package artifact changed while it was being hashed."
            results.append(PackageArtifact(root / relative, relative, filename, read_size,
                                           digest.hexdigest(), integrity_digest.hexdigest()))
            filenames.add(filename)
        except (OSError, RuntimeError, TypeError, ValueError):
            return (), "A package artifact could not be opened safely."
    return tuple(results), None


def same_artifacts(expected: tuple[PackageArtifact, ...], current: tuple[PackageArtifact, ...]) -> bool:
    if len(expected) != len(current):
        return False
    return all(
        (before.relative_path, before.filename, before.size, before.sha256, before.sha512)
        == (after.relative_path, after.filename, after.size, after.sha256, after.sha512)
        for before, after in zip(expected, current)
    )


def common_preflight(request: ReleaseRequest, provider_id: str, *, credentials_configured: bool,
                     local_build_validated: bool, now: float | None = None):
    metadata = request.metadata if isinstance(request.metadata, Mapping) else {}
    authorization = request.authorization
    target_valid = bool(
        getattr(request, "provider_id", None) == provider_id
        and getattr(authorization, "target_resolved", False) is True
        and isinstance(getattr(request, "app_or_package", None), str)
        and bool(request.app_or_package.strip())
        and isinstance(getattr(request, "version", None), str)
        and bool(request.version.strip())
        and isinstance(getattr(request, "channel", None), str)
        and bool(request.channel.strip())
    )
    intent = ReleaseIntent(
        requested=getattr(authorization, "explicit_intent", False) is True,
        provider_id=provider_id if target_valid else None,
        app_or_package=request.app_or_package if target_valid else None,
        version=request.version if target_valid else None,
        channel=request.channel if target_valid else None,
        blockers=[] if target_valid else ["Package release target must be explicitly resolved."],
    )
    references = getattr(authorization, "current_references", ())
    if not isinstance(references, (tuple, list)):
        references = ()
    required = metadata.get("required_attestations", ())
    completed = metadata.get("completed_attestations", ())
    common = build_release_preflight(
        intent,
        current_references=references,
        external_access_enabled=getattr(authorization, "external_access_enabled", False) is True,
        agreement_accepted_by_user=metadata.get("agreement_accepted_by_user") is True,
        credentials_configured=(metadata.get("credentials_configured") is True and credentials_configured),
        local_build_validated=(metadata.get("local_build_validated") is True and local_build_validated),
        paid_plan_choice_required=metadata.get("paid_plan_choice_required") is True,
        paid_plan_selected_by_user=metadata.get("paid_plan_selected_by_user") is True,
        required_attestations=tuple(value for value in required if isinstance(value, str))
        if isinstance(required, (tuple, list)) else (),
        completed_attestations=tuple(value for value in completed if isinstance(value, str))
        if isinstance(completed, (tuple, list)) else (),
        now=now,
    )
    return [dict(check) for check in common.checks], list(common.blockers)


def preflight_result(checks: list[dict], blockers: list[str]) -> ReleasePreflight:
    unique = list(dict.fromkeys(blockers))
    return ReleasePreflight("BLOCKED" if unique else "READY", checks, unique)

