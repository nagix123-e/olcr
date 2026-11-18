"""Fail-closed npm publish adapter with exact registry integrity read-back."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tarfile
import time
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import quote

from ..coding_tasks import ReleasePreflight
from ..release_workflow import ReleaseRequest, ReleaseResult
from .base import (
    CommandResult,
    CommandSpec,
    ProviderApiTransport,
    ProviderRequest,
    ProviderResponse,
    ReleaseCommandRunner,
)
from .package_registry_common import (
    MAX_ARTIFACT_BYTES,
    PackageArtifact,
    common_preflight,
    local_artifacts,
    open_confined_file,
    preflight_result,
    same_artifacts,
    workspace_path,
)


_NPM_REGISTRY = "https://registry.npmjs.org/"
_UNSCOPED_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,213}$")
_SCOPED_PACKAGE_NAME = re.compile(r"^@[a-z0-9._-]+/[a-z0-9_-][a-z0-9._-]*$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_DIST_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_JSON_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024


def _semver(value: object) -> bool:
    if not isinstance(value, str):
        return False
    match = _SEMVER.fullmatch(value)
    if not match:
        return False
    prerelease = match.group(4)
    if prerelease:
        for part in prerelease.split("."):
            if part.isdigit() and len(part) > 1 and part.startswith("0"):
                return False
    return True


def _package_name(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 214 or not value.isascii() or value != value.casefold():
        return False
    valid_shape = (_SCOPED_PACKAGE_NAME.fullmatch(value) if value.startswith("@")
                   else _UNSCOPED_PACKAGE_NAME.fullmatch(value))
    return bool(valid_shape) and value not in {"node_modules", "favicon.ico"}


def _valid_dist_tag(value: object) -> bool:
    if not isinstance(value, str) or not _DIST_TAG.fullmatch(value) or _semver(value):
        return False
    # npm rejects any tag parsed as a semver range; numeric and v-prefixed tags
    # are deliberately rejected because they are the ambiguous range forms.
    return not value[0].isdigit() and value[0].casefold() != "v"


class NpmPublishAdapter:
    """Publish one existing npm tarball to the official registry and verify SRI."""

    provider_id = "npm"

    def __init__(self, *, runner: ReleaseCommandRunner | None = None,
                 api_transport: ProviderApiTransport | None = None,
                 credential_provider: Callable[[str], Mapping[str, str] | None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.credential_provider = credential_provider
        self.runner = runner or ReleaseCommandRunner(credential_provider=credential_provider)
        self._credential_source = credential_provider or getattr(self.runner, "credential_provider", None)
        self.api_transport = api_transport or ProviderApiTransport(
            credential_provider=lambda profile: self._api_token(profile),
        )
        self.clock = clock

    def _credentials(self) -> Mapping[str, str] | None:
        if self._credential_source is None:
            return None
        try:
            value = self._credential_source("npm")
            return value if isinstance(value, Mapping) else None
        except Exception:
            return None

    def _api_token(self, profile: str) -> str | None:
        if profile != "npm_registry_api":
            return None
        credentials = self._credentials()
        token = credentials.get("NODE_AUTH_TOKEN") if credentials else None
        return token if isinstance(token, str) and token.strip() else None

    def _token(self) -> str | None:
        credentials = self._credentials()
        value = credentials.get("NODE_AUTH_TOKEN") if credentials else None
        if (not isinstance(value, str) or not value.strip() or len(value) > 16_384
                or any(ord(char) < 32 for char in value)):
            return None
        return value.strip()

    @staticmethod
    def _metadata(request: ReleaseRequest) -> Mapping[str, object]:
        return request.metadata if isinstance(request.metadata, Mapping) else {}

    def _target_error(self, request: ReleaseRequest) -> str | None:
        metadata = self._metadata(request)
        name = request.app_or_package
        version = request.version
        tag = request.channel
        visibility = metadata.get("visibility")
        registry = metadata.get("registry")
        if not _package_name(name):
            return "npm package name is invalid or must use lowercase registry syntax."
        if not _semver(version):
            return "npm release version must be an exact semantic version."
        if not _valid_dist_tag(tag):
            return "npm distribution tag must be explicit and must not be a semantic version or range."
        if registry != _NPM_REGISTRY:
            return "Only the explicitly selected official npm registry is supported."
        if visibility not in {"public", "restricted"}:
            return "npm package visibility must be explicitly public or restricted."
        if visibility == "restricted" and not name.startswith("@"):
            return "npm restricted visibility is supported only for scoped packages."
        if metadata.get("provenance") is True:
            return "npm provenance requires a supported configured CI/OIDC publishing runtime."
        return None

    @staticmethod
    def _validate_tarball(request: ReleaseRequest, artifact: PackageArtifact) -> str | None:
        root = workspace_path(request)
        if root is None:
            return "npm workspace is unavailable."
        try:
            descriptor, _ = open_confined_file(root, artifact.relative_path)
            with os.fdopen(descriptor, "rb") as stream:
                with tarfile.open(fileobj=stream, mode="r|gz") as archive:
                    manifest_payload = None
                    uncompressed_bytes = 0
                    for index, member in enumerate(archive):
                        if index >= 100_000:
                            return "npm tarball has too many files to inspect safely."
                        if member.size < 0:
                            return "npm tarball contains an invalid member size."
                        uncompressed_bytes += member.size
                        if uncompressed_bytes > MAX_ARTIFACT_BYTES:
                            return "npm tarball exceeds the uncompressed size limit."
                        if member.name == "package/package.json":
                            if manifest_payload is not None or not member.isfile():
                                return "npm tarball manifest is duplicated or not a regular file."
                            if member.size <= 0 or member.size > _MAX_MANIFEST_BYTES:
                                return "npm package manifest is empty or exceeds the metadata size limit."
                            source = archive.extractfile(member)
                            if source is None:
                                return "npm package manifest could not be read."
                            manifest_payload = source.read(_MAX_MANIFEST_BYTES + 1)
                    if manifest_payload is None:
                        return "npm tarball must contain package/package.json."
                    payload = json.loads(manifest_payload)
                    if not isinstance(payload, Mapping):
                        return "npm package manifest must be a JSON object."
                    if payload.get("name") != request.app_or_package or payload.get("version") != request.version:
                        return "npm tarball package name and version must match the requested release."
        except (OSError, EOFError, tarfile.TarError, json.JSONDecodeError, TypeError, ValueError):
            return "npm tarball could not be inspected safely."
        return None

    def _artifacts(self, request: ReleaseRequest):
        artifacts, error = local_artifacts(request, suffixes=(".tgz",), exact_count=1)
        if error:
            return (), error
        error = self._validate_tarball(request, artifacts[0])
        return (artifacts, None) if error is None else ((), error)

    def preflight(self, request: ReleaseRequest) -> ReleasePreflight:
        target_error = self._target_error(request)
        artifacts, artifact_error = self._artifacts(request)
        metadata = self._metadata(request)
        token = self._token()
        checks, blockers = common_preflight(
            request, self.provider_id,
            credentials_configured=token is not None,
            local_build_validated=metadata.get("local_build_validated") is True and artifact_error is None,
            now=self.clock(),
        )
        if target_error:
            checks.append({"name": "npm_target", "status": "BLOCKED", "message": target_error})
            blockers.append(target_error)
        else:
            checks.append({"name": "npm_target", "status": "PASS", "message": "npm name, version, registry, visibility, and tag are explicit."})
        if artifact_error:
            checks.append({"name": "artifact", "status": "BLOCKED", "message": artifact_error})
            blockers.append(artifact_error)
        else:
            checks.append({"name": "artifact", "status": "PASS", "message": "The npm tarball is workspace-confined, hashed, and matches the requested package."})
        if blockers:
            return preflight_result(checks, blockers)

        root = workspace_path(request)
        try:
            auth = self.runner.run(CommandSpec(
                "npm", ("whoami", "--registry", _NPM_REGISTRY), root,
                environment_profile="npm", timeout_seconds=30,
            ))
        except Exception:
            auth = CommandResult(None, "", "", False, "AUTH_CHECK_FAILED")
        authenticated = auth.returncode == 0 and not auth.timed_out and auth.error_code is None
        checks.append({"name": "registry_auth", "status": "PASS" if authenticated else "BLOCKED",
                       "message": "Configured npm authentication is available." if authenticated else
                       "Configured npm authentication could not be verified."})
        if not authenticated:
            blockers.append("Configured npm authentication could not be verified.")
        return preflight_result(checks, blockers)

    def _version_path(self, request: ReleaseRequest) -> str:
        package = quote(request.app_or_package, safe="@")
        return f"/{package}/{quote(request.version, safe='')}"

    def _read_version(self, request: ReleaseRequest):
        try:
            response = self.api_transport.request(ProviderRequest(
                "npm_registry_api", "GET", self._version_path(request), timeout_seconds=20,
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

    def _requested_tag_matches(self, request: ReleaseRequest) -> bool:
        package_state, package = self._read_package_metadata(request)
        if package_state != "found":
            return False
        dist_tags = package.get("dist-tags")
        return isinstance(dist_tags, Mapping) and dist_tags.get(request.channel) == request.version

    def _read_package_metadata(self, request: ReleaseRequest):
        try:
            response = self.api_transport.request(ProviderRequest(
                "npm_registry_api", "GET", f"/{quote(request.app_or_package, safe='@')}",
                timeout_seconds=20,
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

    def _requested_visibility_matches(self, request: ReleaseRequest) -> bool:
        root = workspace_path(request)
        if root is None:
            return False
        try:
            result = self.runner.run(CommandSpec(
                "npm",
                ("access", "get", "status", request.app_or_package, "--registry", _NPM_REGISTRY),
                root,
                environment_profile="npm",
                timeout_seconds=30,
            ))
        except Exception:
            return False
        if (not isinstance(result, CommandResult) or result.returncode != 0
                or result.timed_out or result.error_code is not None):
            return False
        actual = result.stdout.strip().casefold()
        expected = self._metadata(request).get("visibility")
        if actual.startswith('"') and actual.endswith('"') and len(actual) > 1:
            actual = actual[1:-1]
        if actual.startswith("{"):
            try:
                payload = json.loads(actual)
            except json.JSONDecodeError:
                return False
            if not isinstance(payload, Mapping):
                return False
            actual = payload.get("access", payload.get("status", ""))
            actual = actual.casefold() if isinstance(actual, str) else ""
        if expected == "public":
            return actual == "public"
        if expected == "restricted":
            return actual in {"private", "restricted"}
        return False

    @staticmethod
    def _integrity(artifact: PackageArtifact) -> str:
        return "sha512-" + base64.b64encode(bytes.fromhex(artifact.sha512)).decode("ascii")

    def _remote_state(self, request: ReleaseRequest, artifact: PackageArtifact):
        state, payload = self._read_version(request)
        if state != "found":
            return state
        dist = payload.get("dist") if isinstance(payload, Mapping) else None
        integrity = dist.get("integrity") if isinstance(dist, Mapping) else None
        if (payload.get("name") != request.app_or_package or payload.get("version") != request.version
                or not isinstance(integrity, str)):
            return "conflict"
        expected = self._integrity(artifact)
        integrities = set(integrity.split())
        if expected not in integrities:
            return "conflict"
        if not self._requested_tag_matches(request):
            return "conflict"
        if not self._requested_visibility_matches(request):
            return "conflict"
        return "exact"

    def _remote_url(self, request: ReleaseRequest) -> str:
        return ("https://www.npmjs.com/package/" + quote(request.app_or_package, safe="@/")
                + "/v/" + quote(request.version, safe=""))

    @staticmethod
    def _remote_id(request: ReleaseRequest) -> str:
        key = hashlib.sha256(f"{request.app_or_package}\0{request.version}".encode()).hexdigest()
        return "npm:" + key

    def _state_result(self, request: ReleaseRequest, state: str, *, after_publish: bool = False) -> ReleaseResult:
        if state == "exact":
            return ReleaseResult("PUBLISHED", "REGISTRY_INTEGRITY_MATCH", self._remote_url(request),
                                 self._remote_id(request),
                                 "npm registry reads back the exact tarball integrity, requested distribution tag, and visibility.",
                                 False)
        if state == "conflict":
            return ReleaseResult("BLOCKED", "VERSION_ARTIFACT_CONFLICT", None, self._remote_id(request),
                                 "npm read-back found a different artifact, distribution tag, or visibility, or could not verify one of them.",
                                 False)
        if state == "missing" and not after_publish:
            return ReleaseResult("FAILED", "VERSION_NOT_FOUND", None, self._remote_id(request),
                                 "The requested npm package version is not present in the official registry.", False)
        if state == "missing":
            return ReleaseResult("UNCERTAIN", "PUBLISH_NOT_READ_BACK", None, self._remote_id(request),
                                 "npm publish returned without an exact registry read-back; inspect before retrying.", True)
        return ReleaseResult("UNCERTAIN", "REGISTRY_INSPECTION_REQUIRED", None, self._remote_id(request),
                             "The npm registry state could not be confirmed; inspect before retrying.", True)

    def publish(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        preflight = self.preflight(request)
        if preflight.status != "READY":
            return ReleaseResult("BLOCKED", "PREFLIGHT_BLOCKED", None, None,
                                 preflight.blockers[0] if preflight.blockers else "npm release preflight is blocked.", False)
        artifacts, error = self._artifacts(request)
        if error or len(artifacts) != 1:
            return ReleaseResult("BLOCKED", "ARTIFACT_INVALID", None, None,
                                 error or "The npm release tarball is unavailable.", False)
        artifact = artifacts[0]
        state = self._remote_state(request, artifact)
        if state != "missing":
            return self._state_result(request, state)
        current_artifacts, current_error = self._artifacts(request)
        if current_error or not same_artifacts(artifacts, current_artifacts):
            return ReleaseResult("BLOCKED", "ARTIFACT_CHANGED_BEFORE_UPLOAD", None,
                                 self._remote_id(request),
                                 "The npm tarball changed after validation and before upload.", False)
        metadata = self._metadata(request)
        root = workspace_path(request)
        try:
            result = self.runner.run(CommandSpec(
                "npm",
                ("publish", str(artifact.path), "--registry", _NPM_REGISTRY,
                 "--access", str(metadata["visibility"]), "--tag", request.channel,
                 "--ignore-scripts"),
                root, environment_profile="npm", timeout_seconds=300,
            ))
        except Exception:
            result = CommandResult(None, "", "", False, "PUBLISH_COMMAND_FAILED")
        # A CLI response is never proof of publication; inspect remote state before retrying or reporting.
        state = self._remote_state(request, artifact)
        if state == "exact":
            return self._state_result(request, state, after_publish=True)
        if state == "conflict":
            return self._state_result(request, state, after_publish=True)
        if result.timed_out or result.returncode is None or result.error_code:
            return self._state_result(request, "error", after_publish=True)
        if result.returncode != 0 and state == "missing":
            return ReleaseResult("FAILED", "NPM_PUBLISH_FAILED", None, self._remote_id(request),
                                 "npm rejected the package publication; no matching version is present.", False)
        return self._state_result(request, state, after_publish=True)

    def inspect(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        preflight = self.preflight(request)
        if preflight.status != "READY":
            return ReleaseResult("BLOCKED", "PREFLIGHT_BLOCKED", None, None,
                                 preflight.blockers[0] if preflight.blockers else "npm inspection preflight is blocked.", False)
        artifacts, error = self._artifacts(request)
        if error or len(artifacts) != 1:
            return ReleaseResult("BLOCKED", "ARTIFACT_INVALID", None, None,
                                 error or "The npm release tarball is unavailable.", False)
        return self._state_result(request, self._remote_state(request, artifacts[0]))


__all__ = ["NpmPublishAdapter"]
