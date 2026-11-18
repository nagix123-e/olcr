"""Shared fail-closed local build and preflight helpers for web deployers."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from typing import Callable

from ..coding_tasks import ReleaseIntent, ReleasePreflight, build_release_preflight
from ..release_workflow import ReleaseRequest


MAX_FILES = 20_000
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_COMMON_DIRECTORY_FLAGS = getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class WebAsset:
    path: Path
    relative_path: Path
    size: int
    sha256: str


def base_preflight(request: ReleaseRequest, provider_id: str, *, credentials_configured: bool,
                   local_build_validated: bool, now: float | None = None) -> tuple[list[dict], list[str]]:
    authorization = getattr(request, "authorization", None)
    metadata = request.metadata if isinstance(getattr(request, "metadata", None), Mapping) else {}
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
        blockers=[] if target_valid else ["The web deployment provider, project, version, and environment must be explicit."],
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
    return [dict(row) for row in common.checks], list(common.blockers)


def add_check(checks: list[dict], blockers: list[str], name: str, message: str, *, passed: bool) -> None:
    checks.append({"name": name, "status": "PASS" if passed else "BLOCKED", "message": message})
    if not passed:
        blockers.append(message)


def preflight_result(checks: list[dict], blockers: list[str]) -> ReleasePreflight:
    unique = list(dict.fromkeys(blockers))
    return ReleasePreflight("BLOCKED" if unique else "READY", checks, unique)


def _open_directory(root: Path, relative: Path) -> int:
    if (not relative.parts or any(part in {"", ".", ".."} for part in relative.parts)
            or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd
            or os.listdir not in os.supports_fd or not getattr(os, "O_DIRECTORY", 0)
            or not getattr(os, "O_NOFOLLOW", 0)):
        raise OSError("SAFE_DIRECTORY_ACCESS_UNAVAILABLE")
    current = os.open(root, os.O_RDONLY | _COMMON_DIRECTORY_FLAGS | getattr(os, "O_CLOEXEC", 0))
    try:
        for component in relative.parts:
            next_fd = os.open(
                component,
                os.O_RDONLY | _COMMON_DIRECTORY_FLAGS | getattr(os, "O_CLOEXEC", 0),
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        result = current
        current = -1
        return result
    finally:
        if current >= 0:
            os.close(current)


def _scan(fd: int, prefix: Path, *, assets: list[WebAsset], limits: tuple[int, int, int],
          total: list[int]) -> str | None:
    max_files, max_file_bytes, max_total_bytes = limits
    try:
        names = sorted(os.listdir(fd))
    except OSError:
        return "The verified deployment directory could not be listed safely."
    for name in names:
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or "/" in name or "\\" in name):
            return "The deployment tree contains an unsafe file name."
        relative = prefix / name
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError:
            return "A deployment asset changed while it was being inspected."
        if stat.S_ISLNK(info.st_mode):
            return "The deployment tree cannot contain symbolic links."
        if stat.S_ISDIR(info.st_mode):
            try:
                child_fd = os.open(
                    name,
                    os.O_RDONLY | _COMMON_DIRECTORY_FLAGS | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=fd,
                )
            except OSError:
                return "A deployment subdirectory could not be opened safely."
            try:
                error = _scan(child_fd, relative, assets=assets, limits=limits, total=total)
            finally:
                os.close(child_fd)
            if error:
                return error
            continue
        if not stat.S_ISREG(info.st_mode):
            return "The deployment tree may contain only regular files and directories."
        if info.st_size < 0 or info.st_size > max_file_bytes:
            return "A deployment file exceeds the provider's per-file size limit."
        if len(assets) >= max_files or total[0] + info.st_size > max_total_bytes:
            return "The deployment tree exceeds its file count or total byte limit."
        descriptor = -1
        try:
            descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_CLOEXEC", 0), dir_fd=fd)
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_dev != info.st_dev
                    or opened.st_ino != info.st_ino or opened.st_size != info.st_size):
                return "A deployment asset changed while it was being inspected."
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > max_file_bytes or total[0] + size > max_total_bytes:
                        return "A deployment asset grew beyond the provider's size limit."
                    digest.update(chunk)
            if size != opened.st_size:
                return "A deployment asset changed while it was being hashed."
            total[0] += size
            assets.append(WebAsset(Path(""), relative, size, digest.hexdigest()))
        except (OSError, RuntimeError):
            return "A deployment asset could not be read safely."
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    return None


def deployment_tree(request: ReleaseRequest, directory_value: object, *, max_files: int = MAX_FILES,
                    max_file_bytes: int = MAX_FILE_BYTES,
                    max_total_bytes: int = MAX_TOTAL_BYTES) -> tuple[tuple[WebAsset, ...], Path | None, str | None]:
    try:
        root = Path(request.project_root).expanduser().resolve(strict=True)
        if not root.is_dir() or not isinstance(directory_value, (str, Path)):
            raise ValueError("INVALID_ROOT_OR_DIRECTORY")
        candidate = Path(directory_value)
        relative_directory = candidate.relative_to(root) if candidate.is_absolute() else candidate
        if (not relative_directory.parts or any(part in {"", ".", ".."} for part in relative_directory.parts)
                or not isinstance(request.artifact_paths, (tuple, list)) or not request.artifact_paths):
            raise ValueError("INVALID_DIRECTORY_OR_ARTIFACTS")
        directory_fd = _open_directory(root, relative_directory)
    except (OSError, RuntimeError, TypeError, ValueError):
        return (), None, "The deployment directory must be an existing workspace-confined directory."
    assets: list[WebAsset] = []
    try:
        error = _scan(directory_fd, relative_directory, assets=assets,
                      limits=(max_files, max_file_bytes, max_total_bytes), total=[0])
    finally:
        os.close(directory_fd)
    if error:
        return (), None, error
    if not assets:
        return (), None, "The verified deployment directory contains no regular files."
    requested: list[Path] = []
    try:
        for raw in request.artifact_paths:
            candidate = Path(raw)
            relative = candidate.relative_to(root) if candidate.is_absolute() else candidate
            if (not relative.parts or any(part in {"", ".", ".."} for part in relative.parts)
                    or relative.parts[:len(relative_directory.parts)] != relative_directory.parts):
                return (), None, "Every artifact path must be inside the deployment directory."
            requested.append(relative)
    except (OSError, RuntimeError, TypeError, ValueError):
        return (), None, "A deployment artifact path is invalid."
    expected_paths = [asset.relative_path for asset in assets]
    if len(set(requested)) != len(requested) or sorted(requested) != sorted(expected_paths):
        return (), None, "The declared artifacts must exactly match every file in the deployment directory."
    by_path = {asset.relative_path: asset for asset in assets}
    rooted = tuple(WebAsset(root / item.relative_path, item.relative_path, item.size, item.sha256)
                   for item in (by_path[path] for path in sorted(requested)))
    return rooted, root / relative_directory, None


def same_tree(before: tuple[WebAsset, ...], after: tuple[WebAsset, ...]) -> bool:
    return len(before) == len(after) and all(
        (left.relative_path, left.size, left.sha256) == (right.relative_path, right.size, right.sha256)
        for left, right in zip(before, after)
    )


def workspace_path(request: ReleaseRequest) -> Path | None:
    try:
        root = Path(request.project_root).expanduser().resolve(strict=True)
        return root if root.is_dir() else None
    except (OSError, RuntimeError, TypeError):
        return None
