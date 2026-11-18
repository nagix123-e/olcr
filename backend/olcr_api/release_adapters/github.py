"""Fail-closed GitHub Releases adapter backed by GitHub CLI and API read-back."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import quote, unquote, urlsplit

from ..coding_tasks import ReleaseIntent, ReleasePreflight, build_release_preflight
from ..db import is_valid_release_target
from ..github_review import GitHubReview
from ..release_workflow import ReleaseRequest, ReleaseResult
from .base import (
    CommandResult,
    CommandSpec,
    ProviderApiTransport,
    ProviderRequest,
    ProviderResponse,
    ReleaseCommandRunner,
)


_VERSION = re.compile(r"^v?[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}$")
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_OWNER_OR_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.I)
_ASSET_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$", re.I)
_CHANNELS = {
    "stable": False,
    "latest": False,
    "prod": False,
    "production": False,
    "alpha": True,
    "beta": True,
    "canary": True,
    "internal": True,
    "next": True,
    "preview": True,
    "rc": True,
}
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARTIFACTS = 100
_REFERENCE_MAX_AGE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class _Artifact:
    path: Path
    name: str
    digest: str
    relative_path: Path | None = None


def _value(item: object, key: str, default=None):
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _tag_for(version: str) -> str | None:
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        return None
    return version if version.startswith("v") else f"v{version}"


def _target_parts(target: object) -> tuple[str, str] | None:
    if not is_valid_release_target(target):
        return None
    pieces = target.split("/")
    if len(pieces) != 2 or not all(_OWNER_OR_REPO.fullmatch(piece) for piece in pieces):
        return None
    return pieces[0], pieces[1]


def _open_confined_file(root: Path, relative: Path) -> tuple[int, os.stat_result]:
    """Open a regular file under root without following symlinks in any component."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory_flag is None or os.open not in os.supports_dir_fd:
        raise OSError("Safe descriptor-relative file access is unavailable")
    root_fd = os.open(root, os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0))
    parent_fd = root_fd
    descriptor = None
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = next_fd
        descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("Release artifact is not a regular file")
        result = descriptor
        descriptor = None
        return result, file_stat
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)


def _local_artifacts(request: ReleaseRequest) -> tuple[tuple[_Artifact, ...], str | None]:
    try:
        root = Path(request.project_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError):
        return (), "Project workspace is unavailable."
    if not root.is_dir():
        return (), "Project workspace is not a directory."
    if not request.artifact_paths or len(request.artifact_paths) > _MAX_ARTIFACTS:
        return (), "A bounded set of local release artifacts is required."

    results: list[_Artifact] = []
    seen_names: set[str] = set()
    total_bytes = 0
    for raw_path in request.artifact_paths:
        try:
            candidate = Path(raw_path)
            relative = candidate.relative_to(root) if candidate.is_absolute() else candidate
            if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                return (), "Release artifact paths must remain inside the project workspace."
            name = relative.name
            if (not name or len(name) > 240 or name in seen_names
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
                return (), "Release artifact names must be unique and safe."
            descriptor, file_stat = _open_confined_file(root, relative)
            if file_stat.st_size <= 0 or file_stat.st_size > _MAX_ARTIFACT_BYTES:
                os.close(descriptor)
                return (), "A release artifact is empty or exceeds the size limit."
            digest = hashlib.sha256()
            bytes_read = 0
            with os.fdopen(descriptor, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    bytes_read += len(chunk)
                    if (bytes_read > _MAX_ARTIFACT_BYTES
                            or total_bytes + bytes_read > _MAX_ARTIFACT_BYTES):
                        return (), "Release artifacts grew beyond the combined size limit while being read."
                    digest.update(chunk)
            if bytes_read != file_stat.st_size:
                return (), "A release artifact changed size while being read."
            total_bytes += bytes_read
            results.append(_Artifact(root / relative, name, digest.hexdigest(), relative))
            seen_names.add(name)
        except (OSError, RuntimeError, TypeError, ValueError):
            return (), "A release artifact could not be read safely."
    return tuple(results), None


def _snapshot_artifacts(
    root: Path,
    artifacts: tuple[_Artifact, ...],
    directory: Path,
) -> tuple[tuple[_Artifact, ...], str | None]:
    snapshots: list[_Artifact] = []
    total_bytes = 0
    for artifact in artifacts:
        # Preserve the public asset name; duplicate names are rejected before staging.
        target = directory / artifact.name
        digest = hashlib.sha256()
        size = 0
        descriptor = None
        try:
            if artifact.relative_path is None:
                return (), "Release artifact path is unavailable for safe staging."
            descriptor, file_stat = _open_confined_file(root, artifact.relative_path)
            with os.fdopen(descriptor, "rb") as source, target.open("xb") as output:
                descriptor = None
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(chunk)
                    total_bytes += len(chunk)
                    if size > _MAX_ARTIFACT_BYTES or total_bytes > _MAX_ARTIFACT_BYTES:
                        return (), "Release artifact bytes changed beyond the allowed size limit."
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
            if size != file_stat.st_size or not size or digest.hexdigest() != artifact.digest:
                return (), "Release artifact bytes changed after validation."
            snapshots.append(_Artifact(target, artifact.name, digest.hexdigest()))
        except (OSError, RuntimeError):
            return (), "A release artifact snapshot could not be created safely."
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return tuple(snapshots), None


class GitHubReleaseAdapter:
    """Create GitHub Releases only after local preflight and exact remote read-back."""

    provider_id = "github"

    def __init__(
        self,
        *,
        git_workspace,
        runner: ReleaseCommandRunner | None = None,
        api_transport: ProviderApiTransport | None = None,
        credential_provider: Callable[[str], Mapping[str, str] | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.git_workspace = git_workspace
        self.github_review = GitHubReview(git_workspace)
        self.runner = runner or ReleaseCommandRunner(credential_provider=credential_provider)
        credential_source = credential_provider or getattr(self.runner, "credential_provider", None)
        if api_transport is not None:
            self.api_transport = api_transport
        else:
            def api_credential(profile: str) -> str | None:
                if profile != "github_api" or credential_source is None:
                    return None
                supplied = credential_source("github")
                if supplied is None:
                    return None
                if not isinstance(supplied, Mapping):
                    raise ValueError("CREDENTIAL_PROVIDER_INVALID")
                for key in ("GH_TOKEN", "GITHUB_TOKEN"):
                    value = supplied.get(key)
                    if value:
                        if not isinstance(value, str):
                            raise ValueError("CREDENTIAL_PROVIDER_INVALID")
                        return value
                return None

            self.api_transport = ProviderApiTransport(credential_provider=api_credential)
        self.clock = clock

    def _base_checks(self, request: ReleaseRequest) -> tuple[list[dict[str, str]], list[str]]:
        authorization = getattr(request, "authorization", None)
        parts = _target_parts(getattr(request, "app_or_package", None))
        version = getattr(request, "version", None)
        channel = getattr(request, "channel", None)
        resolved = bool(_value(authorization, "target_resolved", False) is True
                        and getattr(request, "provider_id", None) == self.provider_id
                        and parts is not None and _tag_for(version) is not None)
        intent = ReleaseIntent(
            requested=_value(authorization, "explicit_intent", False) is True,
            provider_id=self.provider_id if resolved else None,
            app_or_package=request.app_or_package if resolved else None,
            version=version if resolved else None,
            channel=channel if resolved else None,
            blockers=[],
        )
        metadata = request.metadata if isinstance(request.metadata, Mapping) else {}
        external = _value(authorization, "external_access_enabled", False) is True
        references = _value(authorization, "current_references", ())
        if not isinstance(references, (tuple, list)):
            references = ()
        required_attestations = metadata.get("required_attestations", ())
        completed_attestations = metadata.get("completed_attestations", ())
        if not isinstance(required_attestations, (tuple, list)):
            required_attestations = ()
        if not isinstance(completed_attestations, (tuple, list)):
            completed_attestations = ()
        common = build_release_preflight(
            intent,
            current_references=references,
            external_access_enabled=external,
            agreement_accepted_by_user=metadata.get("agreement_accepted_by_user") is True,
            credentials_configured=metadata.get("credentials_configured") is True,
            local_build_validated=metadata.get("local_build_validated") is True,
            paid_plan_choice_required=metadata.get("paid_plan_choice_required") is True,
            paid_plan_selected_by_user=metadata.get("paid_plan_selected_by_user") is True,
            required_attestations=[value for value in required_attestations if isinstance(value, str)],
            completed_attestations=[value for value in completed_attestations if isinstance(value, str)],
            now=self.clock(),
        )
        checks = [dict(row) for row in common.checks]
        blockers = list(common.blockers)
        if getattr(request, "provider_id", None) != self.provider_id:
            message = "The GitHub release adapter does not match the requested provider."
            checks.append({"name": "provider", "status": "BLOCKED", "message": message})
            blockers.append(message)

        prerelease = _CHANNELS.get(str(channel).casefold()) if isinstance(channel, str) else None
        channel_passed = prerelease is not None
        channel_message = ("The requested GitHub release channel is supported." if channel_passed else
                           "The requested GitHub release channel is unsupported.")
        checks.append({"name": "channel", "status": "PASS" if channel_passed else "BLOCKED",
                       "message": channel_message})
        if not channel_passed:
            blockers.append(channel_message)

        build_commit = metadata.get("build_commit")
        build_commit_valid = isinstance(build_commit, str) and _COMMIT_SHA.fullmatch(build_commit) is not None
        build_commit_message = ("A validated full Git commit is available for this build." if build_commit_valid else
                                "A validated release build commit is required.")
        checks.append({"name": "build_commit", "status": "PASS" if build_commit_valid else "BLOCKED",
                       "message": build_commit_message})
        if not build_commit_valid:
            blockers.append(build_commit_message)

        artifacts, artifact_error = _local_artifacts(request)
        checks.append({"name": "artifact", "status": "PASS" if artifact_error is None else "BLOCKED",
                       "message": artifact_error or "Local release artifacts are inside the workspace and hashed."})
        if artifact_error:
            blockers.append(artifact_error)
        return checks, list(dict.fromkeys(blockers))

    def _root_and_remote(self, request: ReleaseRequest) -> tuple[Path | None, str | None]:
        try:
            root = Path(request.project_root).expanduser().resolve(strict=True)
            if not root.is_dir():
                return None, "Project workspace is not a directory."
            metadata = request.metadata if isinstance(request.metadata, Mapping) else {}
            build_commit = metadata.get("build_commit")
            head = self.git_workspace._run(root, ["rev-parse", "--verify", "HEAD"]).strip()
            if (not isinstance(build_commit, str) or not _COMMIT_SHA.fullmatch(build_commit)
                    or not _COMMIT_SHA.fullmatch(head) or head.casefold() != build_commit.casefold()):
                return None, "The validated release build must match the current Git commit."
            names = [name.strip() for name in self.git_workspace._run(root, ["remote"]).splitlines() if name.strip()]
            remote_name = request.metadata.get("remote_name", "origin")
            if (not isinstance(remote_name, str) or not _REMOTE_NAME.fullmatch(remote_name)
                    or names != [remote_name]):
                return None, "Exactly one configured Git remote matching the selected remote is required."
            repository = self.github_review.repo(root, remote_name)
            if repository.casefold() != request.app_or_package.casefold():
                return None, "The Git push destination must be the requested GitHub repository."
            return root, None
        except Exception:
            return None, "GitHub repository information could not be verified safely."

    @staticmethod
    def _tag_path(repository: str, tag: str) -> str:
        owner, repo = repository.split("/", 1)
        return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/ref/tags/{quote(tag, safe='')}"

    @staticmethod
    def _tag_object_path(repository: str, object_sha: str) -> str:
        owner, repo = repository.split("/", 1)
        return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/tags/{quote(object_sha, safe='')}"

    @staticmethod
    def _release_path(repository: str, tag: str) -> str:
        owner, repo = repository.split("/", 1)
        return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/releases/tags/{quote(tag, safe='')}"

    def _get(self, path: str) -> ProviderResponse:
        return self.api_transport.request(ProviderRequest(
            host_profile="github_api", method="GET", path=path, timeout_seconds=20,
        ))

    @staticmethod
    def _json_object(response: ProviderResponse) -> dict | None:
        try:
            value = json.loads(response.body)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _tag_commit(self, repository: str, tag_object: dict | None) -> str | None:
        """Resolve lightweight or annotated Git tags to a bounded full commit SHA."""
        current = tag_object.get("object") if isinstance(tag_object, dict) else None
        seen: set[str] = set()
        for _ in range(8):
            if not isinstance(current, dict):
                return None
            kind = current.get("type")
            object_sha = current.get("sha")
            if not isinstance(object_sha, str) or not _COMMIT_SHA.fullmatch(object_sha):
                return None
            if kind == "commit":
                return object_sha.lower()
            if kind != "tag" or object_sha in seen:
                return None
            seen.add(object_sha)
            response = self._get(self._tag_object_path(repository, object_sha))
            if response.status != 200:
                return None
            annotated = self._json_object(response)
            current = annotated.get("object") if annotated is not None else None
        return None

    def _tag_readback_result(self, request: ReleaseRequest, tag: str, build_commit: str) -> ReleaseResult | None:
        try:
            response = self._get(self._tag_path(request.app_or_package, tag))
        except Exception:
            return ReleaseResult("UNCERTAIN", "TAG_INSPECT_FAILED", None, None,
                                 "GitHub tag state could not be confirmed.", True)
        if response.status == 404:
            return ReleaseResult("BLOCKED", "TAG_CONFLICT", None, None,
                                 "The GitHub release tag no longer exists.", False)
        if response.status != 200:
            return ReleaseResult("UNCERTAIN", "TAG_INSPECT_FAILED", None, None,
                                 "GitHub tag state could not be confirmed.", True)
        tag_object = self._json_object(response)
        if tag_object is None:
            return ReleaseResult("UNCERTAIN", "TAG_INSPECT_INVALID_RESPONSE", None, None,
                                 "GitHub returned an unreadable tag record.", True)
        if tag_object.get("ref") != f"refs/tags/{tag}":
            return ReleaseResult("BLOCKED", "TAG_CONFLICT", None, None,
                                 "The GitHub tag response does not match the requested version.", False)
        try:
            tag_commit = self._tag_commit(request.app_or_package, tag_object)
        except Exception:
            return ReleaseResult("UNCERTAIN", "TAG_INSPECT_FAILED", None, None,
                                 "GitHub tag target could not be confirmed.", True)
        if tag_commit is None:
            return ReleaseResult("UNCERTAIN", "TAG_INSPECT_INVALID_RESPONSE", None, None,
                                 "GitHub tag target could not be resolved to a commit.", True)
        if tag_commit != build_commit.casefold():
            return ReleaseResult("BLOCKED", "TAG_CONFLICT", None, None,
                                 "The GitHub tag no longer resolves to the validated release build commit.", False)
        return None

    def preflight(self, request: ReleaseRequest) -> ReleasePreflight:
        checks, blockers = self._base_checks(request)
        if blockers:
            return ReleasePreflight("BLOCKED", checks, blockers)

        try:
            auth = self.runner.run(CommandSpec(
                executable="gh",
                argv=("auth", "status", "--hostname", "github.com"),
                cwd=Path(request.project_root),
                environment_profile="github",
                timeout_seconds=15,
            ))
        except Exception:
            auth = CommandResult(None, "", "", False, "AUTH_CHECK_FAILED")
        authenticated = bool(auth.returncode == 0 and not auth.timed_out and auth.error_code is None)
        checks.append({"name": "credentials", "status": "PASS" if authenticated else "BLOCKED",
                       "message": "GitHub CLI authentication is available." if authenticated else
                       "GitHub CLI authentication is required before a release can be created."})
        if not authenticated:
            return ReleasePreflight("BLOCKED", checks, ["GitHub CLI authentication is unavailable."])

        root, remote_error = self._root_and_remote(request)
        checks.append({"name": "repository", "status": "PASS" if remote_error is None else "BLOCKED",
                       "message": "The sole Git push destination matches the requested GitHub repository." if remote_error is None else remote_error})
        if remote_error:
            return ReleasePreflight("BLOCKED", checks, [remote_error])

        tag = _tag_for(request.version)
        try:
            response = self._get(self._tag_path(request.app_or_package, tag))
            tag_object = self._json_object(response)
            resolved_commit = (self._tag_commit(request.app_or_package, tag_object)
                               if response.status == 200 and tag_object is not None else None)
        except Exception:
            response, tag_object, resolved_commit = None, None, None
        expected_ref = f"refs/tags/{tag}"
        expected_commit = request.metadata.get("build_commit")
        tag_matches_build = bool(
            response is not None and response.status == 200
            and tag_object is not None and tag_object.get("ref") == expected_ref
            and isinstance(expected_commit, str) and resolved_commit == expected_commit.casefold()
        )
        tag_message = ("The requested GitHub tag resolves to the validated release build commit." if tag_matches_build else
                       "The GitHub tag is missing or does not resolve to the validated release build commit.")
        checks.append({"name": "tag", "status": "PASS" if tag_matches_build else "BLOCKED", "message": tag_message})
        return ReleasePreflight("READY" if tag_matches_build else "BLOCKED", checks,
                                [] if tag_matches_build else [tag_message])

    def _inspect_remote(self, request: ReleaseRequest, artifacts: tuple[_Artifact, ...]) -> ReleaseResult:
        if _value(request.authorization, "external_access_enabled", False) is not True:
            return ReleaseResult("BLOCKED", "EXTERNAL_ACCESS_OFF", None, None,
                                 "External Access is disabled; remote inspection was skipped.", False)
        if request.provider_id != self.provider_id or _target_parts(request.app_or_package) is None:
            return ReleaseResult("BLOCKED", "INVALID_TARGET", None, None,
                                 "The GitHub release target is invalid.", False)
        tag = _tag_for(request.version)
        if tag is None:
            return ReleaseResult("BLOCKED", "INVALID_VERSION", None, None,
                                 "The GitHub release version is invalid.", False)
        metadata = request.metadata if isinstance(request.metadata, Mapping) else {}
        build_commit = metadata.get("build_commit")
        if not isinstance(build_commit, str) or not _COMMIT_SHA.fullmatch(build_commit):
            return ReleaseResult("BLOCKED", "BUILD_COMMIT_REQUIRED", None, None,
                                 "A validated release build commit is required for GitHub inspection.", False)
        tag_result = self._tag_readback_result(request, tag, build_commit)
        if tag_result is not None:
            return tag_result
        try:
            response = self._get(self._release_path(request.app_or_package, tag))
        except Exception:
            return ReleaseResult("UNCERTAIN", "INSPECT_FAILED", None, None,
                                 "GitHub release state could not be confirmed.", True)
        if response.status == 404:
            tag_result = self._tag_readback_result(request, tag, build_commit)
            if tag_result is not None:
                return tag_result
            return ReleaseResult("FAILED", "NOT_FOUND", None, None,
                                 "No GitHub release currently exists for the requested tag.", False)
        if response.status != 200:
            return ReleaseResult("UNCERTAIN", "INSPECT_FAILED", None, None,
                                 "GitHub release state could not be confirmed.", True)
        release = self._json_object(response)
        if release is None:
            return ReleaseResult("UNCERTAIN", "INSPECT_INVALID_RESPONSE", None, None,
                                 "GitHub returned an unreadable release record.", True)

        release_tag = release.get("tag_name")
        remote_url = self._validated_release_url(release.get("html_url"), request.app_or_package, tag)
        remote_id = release.get("id")
        if isinstance(remote_id, int) and not isinstance(remote_id, bool) and remote_id > 0:
            remote_id = str(remote_id)
        if (release_tag != tag or remote_url is None or not isinstance(remote_id, str)
                or not remote_id.isdigit() or len(remote_id) > 30):
            return ReleaseResult("BLOCKED", "RELEASE_CONFLICT", None, None,
                                 "An existing GitHub release does not match the requested repository and tag.", False)

        expected = {artifact.name: artifact.digest for artifact in artifacts}
        assets = release.get("assets")
        if not isinstance(assets, list):
            return ReleaseResult("BLOCKED", "ASSET_CONFLICT", remote_url, remote_id,
                                 "The existing GitHub release has no verifiable asset list.", False)
        actual: dict[str, str | None] = {}
        duplicate_names: set[str] = set()
        for asset in assets:
            if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
                continue
            name = asset["name"]
            if name in actual:
                duplicate_names.add(name)
                continue
            digest_match = _ASSET_DIGEST.fullmatch(str(asset.get("digest") or ""))
            actual[name] = digest_match.group(1).lower() if digest_match is not None else None
        if any(name in duplicate_names or actual.get(name) != digest for name, digest in expected.items()):
            return ReleaseResult("BLOCKED", "ASSET_CONFLICT", remote_url, remote_id,
                                 "An existing GitHub release does not contain the exact requested artifact bytes.", False)

        # Recheck after the release and assets were read to narrow tag-movement races.
        tag_result = self._tag_readback_result(request, tag, build_commit)
        if tag_result is not None:
            return tag_result

        draft = release.get("draft")
        prerelease = release.get("prerelease")
        expected_prerelease = _CHANNELS.get(str(request.channel).casefold())
        if not isinstance(draft, bool) or not isinstance(prerelease, bool) or prerelease != expected_prerelease:
            return ReleaseResult("BLOCKED", "RELEASE_CONFLICT", remote_url, remote_id,
                                 "The existing GitHub release state does not match the requested channel.", False)
        if draft:
            return ReleaseResult("SUBMITTED", "DRAFT", remote_url, remote_id,
                                 "A matching GitHub release exists as a draft and needs a human to publish it.", True)
        return ReleaseResult("PUBLISHED", "PUBLISHED", remote_url, remote_id,
                             "GitHub confirms the release and matching artifact hashes.", False)

    @staticmethod
    def _validated_release_url(value: object, repository: str, tag: str) -> str | None:
        if not isinstance(value, str) or len(value) > 2048:
            return None
        try:
            parsed = urlsplit(value)
            expected_owner, expected_repo = repository.split("/", 1)
            actual_path = unquote(parsed.path).split("/")
            if (parsed.scheme != "https" or parsed.hostname != "github.com"
                    or parsed.username is not None or parsed.password is not None or parsed.port is not None
                    or parsed.query or parsed.fragment
                    or len(actual_path) != 6
                    or actual_path[1].casefold() != expected_owner.casefold()
                    or actual_path[2].casefold() != expected_repo.casefold()
                    or actual_path[3:] != ["releases", "tag", tag]):
                return None
        except ValueError:
            return None
        return value

    def inspect(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        del operation_id  # The provider's read endpoint is keyed by repository and tag.
        artifacts, error = _local_artifacts(request)
        if error:
            return ReleaseResult("BLOCKED", "ARTIFACT_INVALID", None, None, error, False)
        return self._inspect_remote(request, artifacts)

    def publish(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        preflight = self.preflight(request)
        if preflight.status != "READY":
            return ReleaseResult("BLOCKED", "PREFLIGHT_BLOCKED", None, None,
                                 "; ".join(preflight.blockers), False)
        artifacts, error = _local_artifacts(request)
        if error:
            return ReleaseResult("BLOCKED", "ARTIFACT_INVALID", None, None, error, False)

        existing = self._inspect_remote(request, artifacts)
        if existing.status == "PUBLISHED" or existing.status == "SUBMITTED":
            return existing
        if existing.provider_state != "NOT_FOUND":
            return existing

        root, remote_error = self._root_and_remote(request)
        if remote_error or root is None:
            return ReleaseResult("BLOCKED", "REPOSITORY_INVALID", None, None,
                                 remote_error or "GitHub repository could not be verified.", False)
        tag = _tag_for(request.version)
        if tag is None:
            return ReleaseResult("BLOCKED", "INVALID_VERSION", None, None,
                                 "The GitHub release version is invalid.", False)
        prerelease = _CHANNELS[str(request.channel).casefold()]

        try:
            with tempfile.TemporaryDirectory(prefix="olcr-github-release-") as temp_dir:
                snapshots, snapshot_error = _snapshot_artifacts(root, artifacts, Path(temp_dir))
                if snapshot_error:
                    return ReleaseResult("BLOCKED", "ARTIFACT_CHANGED", None, None, snapshot_error, False)
                argv = ["release", "create", tag]
                argv.extend(str(item.path) for item in snapshots)
                argv.extend(("--repo", request.app_or_package, "--title", tag, "--notes", "", "--verify-tag"))
                if prerelease:
                    argv.append("--prerelease")
                tag_check = self._tag_readback_result(request, tag, request.metadata["build_commit"])
                if tag_check is not None:
                    return tag_check
                result = self.runner.run(CommandSpec(
                    executable="gh",
                    argv=tuple(argv),
                    cwd=root,
                    environment_profile="github",
                    timeout_seconds=300,
                ))
                read_back = self._inspect_remote(request, snapshots)
                if read_back.status in {"PUBLISHED", "SUBMITTED", "BLOCKED"}:
                    return read_back
                if result.timed_out or result.error_code is not None or result.returncode == 0:
                    return ReleaseResult("UNCERTAIN", "CREATE_UNCONFIRMED", None, None,
                                         "GitHub release creation could not be confirmed; inspect before retrying.", True)
                return ReleaseResult("FAILED", "CREATE_FAILED", None, None,
                                     "GitHub rejected the release creation request.", False)
        except Exception:
            return ReleaseResult("UNCERTAIN", "CREATE_UNCERTAIN", None, None,
                                 "GitHub release creation was interrupted; inspect before retrying.", True)


__all__ = ["GitHubReleaseAdapter"]
