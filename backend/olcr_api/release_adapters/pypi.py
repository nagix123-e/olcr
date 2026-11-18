"""Fail-closed PyPI/TestPyPI upload adapter with per-file SHA-256 read-back."""

from __future__ import annotations

import email
import hashlib
import json
import os
import re
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import quote

from packaging.version import InvalidVersion, Version

from ..coding_tasks import ReleasePreflight
from ..release_workflow import ReleaseRequest, ReleaseResult
from .base import CommandResult, CommandSpec, ProviderApiTransport, ProviderRequest, ProviderResponse, ReleaseCommandRunner
from .package_registry_common import (
    MAX_ARTIFACTS,
    MAX_ARTIFACT_BYTES,
    PackageArtifact,
    common_preflight,
    local_artifacts,
    open_confined_file,
    preflight_result,
    same_artifacts,
    workspace_path,
)


_TARGETS = {
    "pypi": ("https://upload.pypi.org/legacy/", "pypi_api", "https://pypi.org"),
    "testpypi": ("https://test.pypi.org/legacy/", "testpypi_api", "https://test.pypi.org"),
}
_PROJECT_NAME = re.compile(r"^[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*$")
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_JSON_BYTES = 2 * 1024 * 1024


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _valid_project_name(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 200 and bool(_PROJECT_NAME.fullmatch(value))


def _valid_version(value: object) -> bool:
    return _parsed_version(value) is not None


def _parsed_version(value: object) -> Version | None:
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    try:
        return Version(value)
    except InvalidVersion:
        return None


def _normalized_version(value: object) -> str | None:
    version = _parsed_version(value)
    return str(version) if version is not None else None


def _same_version(left: object, right: object) -> bool:
    left_version = _parsed_version(left)
    right_version = _parsed_version(right)
    return left_version is not None and left_version == right_version


def _metadata_matches(data: bytes, request: ReleaseRequest) -> bool:
    if len(data) > _MAX_METADATA_BYTES:
        return False
    try:
        message = email.message_from_bytes(data)
        name = message.get("Name")
        version = message.get("Version")
        return (
            isinstance(name, str) and _normalized_name(name) == _normalized_name(request.app_or_package)
            and _same_version(version, request.version)
        )
    except (TypeError, ValueError):
        return False


def _read_zip_metadata(stream, request: ReleaseRequest, *, wheel: bool) -> bool:
    with zipfile.ZipFile(stream) as archive:
        if len(archive.infolist()) > 100_000:
            return False
        candidates = []
        for info in archive.infolist():
            is_metadata = (info.filename.endswith(".dist-info/METADATA") if wheel else
                           info.filename == "PKG-INFO" or info.filename.endswith("/PKG-INFO"))
            if is_metadata:
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode and (mode & 0o170000) == 0o120000:
                    return False
                candidates.append(info)
        if len(candidates) != 1 or candidates[0].file_size <= 0 or candidates[0].file_size > _MAX_METADATA_BYTES:
            return False
        return _metadata_matches(archive.read(candidates[0]), request)


def _read_tar_metadata(stream, request: ReleaseRequest) -> bool:
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        candidate_bytes = None
        count = 0
        uncompressed_bytes = 0
        for index, member in enumerate(archive):
            if index >= 100_000:
                return False
            if member.size < 0:
                return False
            uncompressed_bytes += member.size
            if uncompressed_bytes > MAX_ARTIFACT_BYTES:
                return False
            if member.name.rsplit("/", 1)[-1] == "PKG-INFO":
                if candidate_bytes is not None or not member.isfile() or member.size <= 0 or member.size > _MAX_METADATA_BYTES:
                    return False
                source = archive.extractfile(member)
                if source is None:
                    return False
                candidate_bytes = source.read(_MAX_METADATA_BYTES + 1)
                count += 1
        return count == 1 and candidate_bytes is not None and _metadata_matches(candidate_bytes, request)


class PyPIPublishAdapter:
    """Upload exact wheel/sdist files to an explicit official PyPI target."""

    provider_id = "pypi"

    def __init__(self, *, runner: ReleaseCommandRunner | None = None,
                 api_transport: ProviderApiTransport | None = None,
                 credential_provider: Callable[[str], Mapping[str, str] | None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.credential_provider = credential_provider
        self.runner = runner or ReleaseCommandRunner(credential_provider=credential_provider)
        self._credential_source = credential_provider or getattr(self.runner, "credential_provider", None)
        self.api_transport = api_transport or ProviderApiTransport()
        self.clock = clock

    def _credentials(self) -> Mapping[str, str] | None:
        if self._credential_source is None:
            return None
        try:
            value = self._credential_source("pypi")
            return value if isinstance(value, Mapping) else None
        except Exception:
            return None

    def _has_credentials(self) -> bool:
        credentials = self._credentials()
        username = credentials.get("TWINE_USERNAME") if credentials else None
        password = credentials.get("TWINE_PASSWORD") if credentials else None
        return (
            isinstance(username, str) and username == "__token__"
            and isinstance(password, str) and password.startswith("pypi-")
            and len(password) <= 16_384 and not any(ord(char) < 32 for char in password)
        )

    @staticmethod
    def _metadata(request: ReleaseRequest) -> Mapping[str, object]:
        return request.metadata if isinstance(request.metadata, Mapping) else {}

    def _target_error(self, request: ReleaseRequest) -> str | None:
        metadata = self._metadata(request)
        target = metadata.get("pypi_target")
        if not _valid_project_name(request.app_or_package):
            return "PyPI project name is invalid."
        if not _valid_version(request.version):
            return "PyPI release version must be a valid explicit PEP 440 version."
        if not isinstance(target, str) or target not in _TARGETS or request.channel != target:
            return "PyPI or TestPyPI target must be explicitly selected and match the release channel."
        return None

    def _artifacts(self, request: ReleaseRequest):
        paths = request.artifact_paths if isinstance(request.artifact_paths, (tuple, list)) else ()
        if not paths or len(paths) > MAX_ARTIFACTS:
            return (), "A bounded set of wheel or source distribution artifacts is required."
        for raw in paths:
            name = Path(raw).name.casefold()
            if not name.endswith((".whl", ".tar.gz", ".zip")):
                return (), "Only wheel, .tar.gz, and .zip source distributions can be uploaded."
        artifacts, error = local_artifacts(request, suffixes=(".whl", ".gz", ".zip"))
        if error:
            return (), error
        for artifact in artifacts:
            if artifact.filename.endswith(".whl"):
                error = self._validate_wheel_name(artifact.filename, request)
            else:
                error = self._validate_sdist_name(artifact.filename, request)
            if error:
                return (), error
            error = self._validate_distribution_metadata(request, artifact)
            if error:
                return (), error
        return artifacts, None

    @staticmethod
    def _validate_wheel_name(filename: str, request: ReleaseRequest) -> str | None:
        stem = filename[:-4]
        pieces = stem.split("-")
        if len(pieces) < 5:
            return "Wheel filename does not follow the supported distribution format."
        prefix, py_tag, abi_tag, platform_tag = pieces[:-3], *pieces[-3:]
        if len(prefix) not in {2, 3}:
            return "Wheel filename does not identify one project and version."
        distribution, version = prefix[:2]
        if len(prefix) == 3 and not re.fullmatch(r"[0-9][A-Za-z0-9]*", prefix[2]):
            return "Wheel build tag is invalid."
        if (_normalized_name(distribution) != _normalized_name(request.app_or_package)
                or not _same_version(version, request.version)
                or not all(re.fullmatch(r"[A-Za-z0-9_.]+", tag) for tag in (py_tag, abi_tag, platform_tag))):
            return "Wheel project name or version must match the requested release."
        return None

    @staticmethod
    def _validate_sdist_name(filename: str, request: ReleaseRequest) -> str | None:
        if filename.endswith(".tar.gz"):
            stem = filename[:-7]
        elif filename.endswith(".zip"):
            stem = filename[:-4]
        else:
            return "Source distribution filename is invalid."
        if "-" not in stem:
            return "Source distribution filename must include project and version."
        distribution, version = stem.rsplit("-", 1)
        if (_normalized_name(distribution) != _normalized_name(request.app_or_package)
                or not _same_version(version, request.version)):
            return "Source distribution project name or version must match the requested release."
        return None

    @staticmethod
    def _validate_distribution_metadata(request: ReleaseRequest, artifact: PackageArtifact) -> str | None:
        root = workspace_path(request)
        if root is None:
            return "PyPI workspace is unavailable."
        try:
            descriptor, _ = open_confined_file(root, artifact.relative_path)
            with os.fdopen(descriptor, "rb") as stream:
                matches = (_read_zip_metadata(stream, request, wheel=artifact.filename.endswith(".whl"))
                           if artifact.filename.endswith((".whl", ".zip")) else
                           _read_tar_metadata(stream, request))
            if not matches:
                return "Distribution metadata name/version does not match the requested PyPI release."
        except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile, ValueError):
            return "Python distribution metadata could not be inspected safely."
        return None

    def preflight(self, request: ReleaseRequest) -> ReleasePreflight:
        target_error = self._target_error(request)
        artifacts, artifact_error = self._artifacts(request)
        metadata = self._metadata(request)
        checks, blockers = common_preflight(
            request, self.provider_id,
            credentials_configured=self._has_credentials(),
            local_build_validated=metadata.get("local_build_validated") is True and artifact_error is None,
            now=self.clock(),
        )
        if target_error:
            checks.append({"name": "pypi_target", "status": "BLOCKED", "message": target_error})
            blockers.append(target_error)
        else:
            checks.append({"name": "pypi_target", "status": "PASS", "message": "Project, version, and PyPI target are explicit."})
        if artifact_error:
            checks.append({"name": "artifacts", "status": "BLOCKED", "message": artifact_error})
            blockers.append(artifact_error)
        else:
            checks.append({"name": "artifacts", "status": "PASS", "message": "Wheel or sdist artifacts are workspace-confined and match project metadata."})
        if blockers:
            return preflight_result(checks, blockers)

        root = workspace_path(request)
        try:
            checked = self.runner.run(CommandSpec(
                "twine", ("check", *(str(item.path) for item in artifacts)), root,
                environment_profile="pypi", timeout_seconds=120,
            ))
        except Exception:
            checked = CommandResult(None, "", "", False, "TWINE_CHECK_FAILED")
        metadata_valid = checked.returncode == 0 and not checked.timed_out and checked.error_code is None
        checks.append({"name": "twine_check", "status": "PASS" if metadata_valid else "BLOCKED",
                       "message": "Twine metadata check passed." if metadata_valid else
                       "Twine did not validate the selected distributions."})
        if not metadata_valid:
            blockers.append("Twine did not validate the selected distributions.")
        return preflight_result(checks, blockers)

    def _target_info(self, request: ReleaseRequest):
        target = self._metadata(request).get("pypi_target")
        return _TARGETS.get(target) if isinstance(target, str) else None

    def _read_version(self, request: ReleaseRequest):
        target = self._target_info(request)
        if target is None:
            return "error", None
        _, profile, _ = target
        project = quote(_normalized_name(request.app_or_package), safe="-")
        normalized_version = _normalized_version(request.version)
        if normalized_version is None:
            return "error", None
        version = quote(normalized_version, safe="")
        try:
            response = self.api_transport.request(ProviderRequest(
                profile, "GET", f"/pypi/{project}/{version}/json", timeout_seconds=20,
            ))
        except Exception:
            return "error", None
        if not isinstance(response, ProviderResponse):
            return "error", None
        if response.status == 404:
            return "missing", None
        if response.status != 200 or not isinstance(response.body, str) or len(response.body) > _MAX_JSON_BYTES:
            return "error", None
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, TypeError):
            return "error", None
        if not isinstance(payload, Mapping):
            return "error", None
        return "found", payload

    def _remote_state(self, request: ReleaseRequest, artifacts: tuple[PackageArtifact, ...]):
        state, payload = self._read_version(request)
        if state != "found":
            return state
        info = payload.get("info")
        if (not isinstance(info, Mapping)
                or _normalized_name(str(info.get("name", ""))) != _normalized_name(request.app_or_package)
                or not _same_version(info.get("version"), request.version)):
            return "conflict"
        remote_files = payload.get("urls")
        if not isinstance(remote_files, list):
            return "conflict"
        by_name = {}
        for item in remote_files:
            if not isinstance(item, Mapping) or not isinstance(item.get("filename"), str):
                return "conflict"
            digests = item.get("digests")
            sha256 = digests.get("sha256") if isinstance(digests, Mapping) else None
            if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
                return "conflict"
            if item["filename"] in by_name:
                return "conflict"
            by_name[item["filename"]] = sha256.casefold()
        for artifact in artifacts:
            if by_name.get(artifact.filename) != artifact.sha256:
                return "conflict"
        return "exact"

    def _remote_id(self, request: ReleaseRequest) -> str:
        key = hashlib.sha256(f"{_normalized_name(request.app_or_package)}\0{request.version}".encode()).hexdigest()
        return "pypi:" + key

    def _remote_url(self, request: ReleaseRequest) -> str:
        _, _, api_root = self._target_info(request)
        version = _normalized_version(request.version) or request.version
        return f"{api_root}/project/{quote(_normalized_name(request.app_or_package), safe='-')}/{quote(version, safe='')}/"

    def _state_result(self, request: ReleaseRequest, state: str, *, after_publish: bool = False) -> ReleaseResult:
        if state == "exact":
            return ReleaseResult("PUBLISHED", "DISTRIBUTION_HASHES_MATCH", self._remote_url(request),
                                 self._remote_id(request),
                                 "The selected PyPI index reads back every requested distribution with matching SHA-256.",
                                 False)
        if state == "conflict":
            return ReleaseResult("BLOCKED", "VERSION_ARTIFACT_CONFLICT", None, self._remote_id(request),
                                 "This PyPI version contains a different or unverifiable distribution; the version will not be replaced.", False)
        if state == "missing" and not after_publish:
            return ReleaseResult("FAILED", "VERSION_NOT_FOUND", None, self._remote_id(request),
                                 "The requested PyPI release is not present on the selected index.", False)
        if state == "missing":
            return ReleaseResult("UNCERTAIN", "PUBLISH_NOT_READ_BACK", None, self._remote_id(request),
                                 "The PyPI upload returned without exact file read-back; inspect before retrying.", True)
        return ReleaseResult("UNCERTAIN", "REGISTRY_INSPECTION_REQUIRED", None, self._remote_id(request),
                             "The selected PyPI index state could not be confirmed; inspect before retrying.", True)

    def publish(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        preflight = self.preflight(request)
        if preflight.status != "READY":
            return ReleaseResult("BLOCKED", "PREFLIGHT_BLOCKED", None, None,
                                 preflight.blockers[0] if preflight.blockers else "PyPI release preflight is blocked.", False)
        artifacts, error = self._artifacts(request)
        if error or not artifacts:
            return ReleaseResult("BLOCKED", "ARTIFACT_INVALID", None, None,
                                 error or "PyPI distributions are unavailable.", False)
        state = self._remote_state(request, artifacts)
        if state != "missing":
            return self._state_result(request, state)
        current_artifacts, current_error = self._artifacts(request)
        if current_error or not same_artifacts(artifacts, current_artifacts):
            return ReleaseResult("BLOCKED", "ARTIFACT_CHANGED_BEFORE_UPLOAD", None,
                                 self._remote_id(request),
                                 "The selected PyPI distributions changed after validation and before upload.", False)
        target_info = self._target_info(request)
        if target_info is None:
            return ReleaseResult("BLOCKED", "PYPI_TARGET_INVALID", None, None,
                                 "The selected PyPI upload target is invalid.", False)
        repository_url = target_info[0]
        root = workspace_path(request)
        args = ("upload", "--non-interactive", "--disable-progress-bar", "--repository-url", repository_url,
                *(str(item.path) for item in artifacts))
        try:
            result = self.runner.run(CommandSpec("twine", args, root, environment_profile="pypi", timeout_seconds=300))
        except Exception:
            result = CommandResult(None, "", "", False, "PUBLISH_COMMAND_FAILED")
        state = self._remote_state(request, artifacts)
        if state in {"exact", "conflict"}:
            return self._state_result(request, state, after_publish=True)
        if result.timed_out or result.returncode is None or result.error_code:
            return self._state_result(request, "error", after_publish=True)
        if result.returncode != 0 and state == "missing":
            return ReleaseResult("FAILED", "PYPI_UPLOAD_FAILED", None, self._remote_id(request),
                                 "Twine rejected the selected distributions; the release is not present.", False)
        return self._state_result(request, state, after_publish=True)

    def inspect(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        preflight = self.preflight(request)
        if preflight.status != "READY":
            return ReleaseResult("BLOCKED", "PREFLIGHT_BLOCKED", None, None,
                                 preflight.blockers[0] if preflight.blockers else "PyPI inspection preflight is blocked.", False)
        artifacts, error = self._artifacts(request)
        if error or not artifacts:
            return ReleaseResult("BLOCKED", "ARTIFACT_INVALID", None, None,
                                 error or "PyPI distributions are unavailable.", False)
        return self._state_result(request, self._remote_state(request, artifacts))


__all__ = ["PyPIPublishAdapter"]
