"""Fail-closed App Store Connect build upload and submission adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import quote, urlencode, urlsplit

from ..coding_tasks import ReleaseIntent, ReleasePreflight, build_release_preflight
from ..release_workflow import ReleaseRequest, ReleaseResult
from .base import ProviderApiTransport, ProviderRequest, ProviderResponse


_BUNDLE_ID = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}$")
_BUILD_NUMBER = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.-]{0,17}$")
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_SAFE_UPLOAD_HEADER = re.compile(r"^(?:content-[a-z0-9-]+|x-apple-[a-z0-9-]+)$", re.I)
_ARTIFACT_MAX_BYTES = 2 * 1024 * 1024 * 1024
_UPLOAD_OPERATION_MAX = 256
_UPLOAD_HEADERS_MAX = 64
_UPLOAD_URL_MAX = 8192
_API_SUCCESS = frozenset({200, 201, 202, 204})
_PLATFORMS = frozenset({"IOS", "MAC_OS", "TV_OS", "VISION_OS"})
_APP_STORE_CHANNELS = frozenset({"stable", "latest", "prod", "production", "app_store"})
_TESTFLIGHT_CHANNELS = frozenset({"testflight", "beta", "internal", "external"})
_CHECKSUM_ALGORITHM = "SHA_256"


@dataclass(frozen=True)
class _Artifact:
    path: Path
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class _UploadInspection:
    state: str
    upload_id: str
    build_id: str | None
    exact_artifact: bool


class AppleBuildUploadTransport:
    """Stream Apple-issued upload ranges without API credentials or redirects."""

    _CHUNK_BYTES = 1024 * 1024

    @staticmethod
    def _open_absolute_nofollow(path: Path) -> int:
        """Open a file by walking every absolute path component without symlinks."""
        parts = path.parts
        if not path.is_absolute() or len(parts) < 2 or parts[0] != "/":
            raise OSError("UPLOAD_PATH_INVALID")
        directory_flag = getattr(os, "O_DIRECTORY", None)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if directory_flag is None or nofollow is None or os.open not in os.supports_dir_fd:
            raise OSError("SAFE_UPLOAD_PATH_UNAVAILABLE")
        parent = os.open("/", os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0))
        try:
            for component in parts[1:-1]:
                next_parent = os.open(
                    component,
                    os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent,
                )
                os.close(parent)
                parent = next_parent
            return os.open(
                parts[-1], os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0), dir_fd=parent,
            )
        finally:
            os.close(parent)

    def upload(self, url: str, headers: Mapping[str, str], path: Path,
               offset: int, length: int) -> int | None:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        if (parsed.scheme != "https" or not host
                or not (host == "blobstore.apple.com" or host.endswith(".blobstore.apple.com"))
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.fragment or len(url) > _UPLOAD_URL_MAX):
            return None
        try:
            descriptor = self._open_absolute_nofollow(path)
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode) or offset < 0 or length <= 0 \
                    or offset + length > file_stat.st_size:
                os.close(descriptor)
                return None
        except OSError:
            return None
        try:
            stream = os.fdopen(descriptor, "rb")
        except OSError:
            try:
                os.close(descriptor)
            except OSError:
                pass
            return None

        safe_headers = dict(headers)
        safe_headers["Content-Length"] = str(length)

        def content() -> Iterable[bytes]:
            stream.seek(offset)
            remaining = length
            while remaining:
                chunk = stream.read(min(self._CHUNK_BYTES, remaining))
                if not chunk:
                    raise OSError("UPLOAD_SOURCE_CHANGED")
                remaining -= len(chunk)
                yield chunk

        try:
            import httpx

            with httpx.Client(timeout=60, follow_redirects=False, verify=True) as client:
                response = client.put(url, headers=safe_headers, content=content())
                return response.status_code
        except Exception:
            # The signed URL and transport exception are intentionally never returned.
            return None
        finally:
            stream.close()


class AppStoreConnectAdapter:
    """Upload one exact build and submit it for TestFlight or App Review."""

    provider_id = "app_store_connect"

    def __init__(
        self,
        *,
        api_transport: ProviderApiTransport | None = None,
        credential_provider: Callable[[str], Mapping[str, str] | None] | None = None,
        upload_transport: AppleBuildUploadTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.credential_provider = credential_provider
        self.clock = clock
        if api_transport is not None:
            self.api_transport = api_transport
        else:
            self.api_transport = ProviderApiTransport(
                credential_provider=lambda profile: self._jwt_token()
                if profile == "app_store_connect_api" else None,
            )
        self.upload_transport = upload_transport or AppleBuildUploadTransport()

    def _credentials(self) -> Mapping[str, str] | None:
        if self.credential_provider is None:
            return None
        try:
            values = self.credential_provider("app_store_connect")
        except Exception:
            return None
        if not isinstance(values, Mapping):
            return None
        issuer = values.get("issuer_id") or values.get("issuerId")
        key_id = values.get("key_id") or values.get("keyId")
        private_key = values.get("private_key") or values.get("privateKey")
        if (not isinstance(issuer, str) or not 1 <= len(issuer) <= 200
                or not re.fullmatch(r"[A-Za-z0-9-]+", issuer)
                or not isinstance(key_id, str) or not 1 <= len(key_id) <= 100
                or not re.fullmatch(r"[A-Za-z0-9_-]+", key_id)
                or not isinstance(private_key, str) or not private_key.strip()
                or len(private_key) > 64 * 1024):
            return None
        return {"issuer_id": issuer, "key_id": key_id, "private_key": private_key}

    def _jwt_token(self) -> str | None:
        credentials = self._credentials()
        if credentials is None:
            return None
        try:
            import jwt

            issued_at = int(self.clock())
            return jwt.encode(
                {"iss": credentials["issuer_id"], "iat": issued_at,
                 "exp": issued_at + 15 * 60, "aud": "appstoreconnect-v1"},
                credentials["private_key"],
                algorithm="ES256",
                headers={"kid": credentials["key_id"], "typ": "JWT"},
            )
        except Exception:
            # Never include exception text: crypto parsers can include input material.
            return None

    @staticmethod
    def _metadata(request: ReleaseRequest) -> Mapping[str, object]:
        return request.metadata if isinstance(request.metadata, Mapping) else {}

    @staticmethod
    def _resource_id(value: object) -> str | None:
        return value if isinstance(value, str) and _RESOURCE_ID.fullmatch(value) else None

    def _artifact(self, request: ReleaseRequest) -> tuple[_Artifact | None, str | None]:
        if not isinstance(request.artifact_paths, (tuple, list)) or len(request.artifact_paths) != 1:
            return None, "Exactly one App Store build artifact is required."
        try:
            root = Path(request.project_root).expanduser().resolve(strict=True)
            raw = Path(request.artifact_paths[0])
            relative = raw.relative_to(root) if raw.is_absolute() else raw
            if (not relative.parts or any(part in {"", ".", ".."} for part in relative.parts)):
                return None, "The App Store build artifact must remain inside the workspace."
            path = root / relative
            directory_flag = getattr(os, "O_DIRECTORY", None)
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if directory_flag is None or nofollow is None or os.open not in os.supports_dir_fd:
                return None, "Safe workspace artifact access is unavailable."
            root_descriptor = os.open(
                root, os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
            )
            parent_descriptor = root_descriptor
            descriptor = -1
            try:
                for component in relative.parts[:-1]:
                    next_descriptor = os.open(
                        component,
                        os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=parent_descriptor,
                    )
                    if parent_descriptor != root_descriptor:
                        os.close(parent_descriptor)
                    parent_descriptor = next_descriptor
                descriptor = os.open(
                    relative.parts[-1], os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_descriptor,
                )
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > _ARTIFACT_MAX_BYTES:
                    return None, "The App Store build artifact is empty, too large, or not a regular file."
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = -1
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        size += len(chunk)
                        if size > _ARTIFACT_MAX_BYTES:
                            return None, "The App Store build artifact exceeds the configured size limit."
                        digest.update(chunk)
                if size != info.st_size:
                    return None, "The App Store build artifact changed while it was being checked."
                platform = self._metadata(request).get("apple_platform")
                extension = path.suffix.casefold()
                allowed_extension = (
                    extension == ".pkg" if platform == "MAC_OS" else extension == ".ipa"
                )
                if not allowed_extension:
                    return None, "Use a .pkg artifact for macOS or a .ipa artifact for other Apple platforms."
                return _Artifact(path, path.name, size, digest.hexdigest()), None
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if parent_descriptor != root_descriptor:
                    os.close(parent_descriptor)
                os.close(root_descriptor)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None, "The App Store build artifact could not be opened safely."

    def preflight(self, request: ReleaseRequest) -> ReleasePreflight:
        metadata = self._metadata(request)
        authorization = request.authorization
        bundle_id = request.app_or_package if isinstance(request.app_or_package, str) else ""
        version = request.version if isinstance(request.version, str) else ""
        build_number = metadata.get("build_number")
        platform = metadata.get("apple_platform")
        channel = request.channel.casefold() if isinstance(request.channel, str) else ""
        target_valid = bool(
            request.provider_id == self.provider_id
            and _BUNDLE_ID.fullmatch(bundle_id)
            and _VERSION.fullmatch(version)
            and isinstance(build_number, str) and _BUILD_NUMBER.fullmatch(build_number)
            and platform in _PLATFORMS
            and channel in _APP_STORE_CHANNELS | _TESTFLIGHT_CHANNELS
        )
        intent = ReleaseIntent(
            requested=getattr(authorization, "explicit_intent", False) is True,
            provider_id=self.provider_id if target_valid else None,
            app_or_package=bundle_id if target_valid else None,
            version=version if target_valid else None,
            channel=request.channel if target_valid else None,
            blockers=[] if target_valid else ["App Store Connect app, platform, version, build number, or channel is invalid."],
        )
        references = getattr(authorization, "current_references", ())
        if not isinstance(references, (tuple, list)):
            references = ()
        required = metadata.get("required_attestations", ())
        completed = metadata.get("completed_attestations", ())
        # A configured-looking PEM is not enough: make sure this exact key can
        # sign the short-lived token the transport will need, without returning it.
        jwt_ready = self._jwt_token() is not None
        common = build_release_preflight(
            intent,
            current_references=references,
            external_access_enabled=getattr(authorization, "external_access_enabled", False) is True,
            agreement_accepted_by_user=metadata.get("agreement_accepted_by_user") is True,
            credentials_configured=(metadata.get("credentials_configured") is True and jwt_ready),
            local_build_validated=metadata.get("local_build_validated") is True,
            paid_plan_choice_required=metadata.get("paid_plan_choice_required") is True,
            paid_plan_selected_by_user=metadata.get("paid_plan_selected_by_user") is True,
            required_attestations=tuple(value for value in required if isinstance(value, str))
            if isinstance(required, (tuple, list)) else (),
            completed_attestations=tuple(value for value in completed if isinstance(value, str))
            if isinstance(completed, (tuple, list)) else (),
            now=self.clock(),
        )
        checks = [dict(row) for row in common.checks]
        blockers = list(common.blockers)
        if request.provider_id != self.provider_id:
            message = "Configured App Store Connect adapter does not match the requested provider."
            checks.append({"name": "provider", "status": "BLOCKED", "message": message})
            blockers.append(message)
        artifact, artifact_error = self._artifact(request)
        if artifact_error:
            checks.append({"name": "artifact", "status": "BLOCKED", "message": artifact_error})
            blockers.append(artifact_error)
        else:
            checks.append({"name": "artifact", "status": "PASS", "message": "The App Store build artifact is confined and hashed."})
        if channel in _APP_STORE_CHANNELS and metadata.get("app_store_metadata_confirmed_by_user") is not True:
            message = "The user must confirm App Store metadata and review information before submission."
            checks.append({"name": "app_review_metadata", "status": "BLOCKED", "message": message})
            blockers.append(message)
        elif channel in _APP_STORE_CHANNELS:
            checks.append({"name": "app_review_metadata", "status": "PASS",
                           "message": "App Store metadata and review information were confirmed by the user."})
        if channel == "external":
            message = (
                "External TestFlight distribution requires Beta App Review and tester-facing metadata; "
                "this adapter only supports internal TestFlight groups."
            )
            checks.append({"name": "external_testflight_review", "status": "BLOCKED", "message": message})
            blockers.append(message)
        if channel in _TESTFLIGHT_CHANNELS:
            group_ids = metadata.get("testflight_group_ids", ())
            if group_ids and (
                not isinstance(group_ids, (tuple, list)) or not 1 <= len(group_ids) <= 50
                or any(self._resource_id(item) is None for item in group_ids)
                or len(set(group_ids)) != len(group_ids)
            ):
                message = "TestFlight group identifiers must be explicitly supplied, unique, and valid."
                checks.append({"name": "testflight_groups", "status": "BLOCKED", "message": message})
                blockers.append(message)
        return ReleasePreflight("BLOCKED" if blockers else "READY", checks,
                                list(dict.fromkeys(blockers)))

    def _api(self, method: str, path: str, body: Mapping[str, object] | None = None) \
            -> tuple[int, Mapping[str, object] | None]:
        response = self.api_transport.request(ProviderRequest(
            "app_store_connect_api", method, path, body=body, timeout_seconds=30,
        ))
        if not isinstance(response, ProviderResponse):
            return 0, None
        if response.status == 204 or not response.body:
            return response.status, {}
        if len(response.body) > 1024 * 1024:
            return response.status, None
        try:
            data = json.loads(response.body)
        except (json.JSONDecodeError, TypeError):
            return response.status, None
        return response.status, data if isinstance(data, Mapping) else None

    @staticmethod
    def _data_object(payload: Mapping[str, object] | None) -> Mapping[str, object] | None:
        if not isinstance(payload, Mapping):
            return None
        value = payload.get("data")
        return value if isinstance(value, Mapping) else None

    @staticmethod
    def _attributes(resource: Mapping[str, object] | None) -> Mapping[str, object]:
        attributes = resource.get("attributes") if isinstance(resource, Mapping) else None
        return attributes if isinstance(attributes, Mapping) else {}

    def _app_id(self, request: ReleaseRequest) -> str | None:
        path = "/v1/apps?" + urlencode({"filter[bundleId]": request.app_or_package, "limit": "10"})
        status, payload = self._api("GET", path)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if status != 200 or not isinstance(data, list):
            return None
        matches = [item for item in data if isinstance(item, Mapping)
                   and self._attributes(item).get("bundleId") == request.app_or_package]
        if len(matches) != 1:
            return None
        return self._resource_id(matches[0].get("id"))

    def _matching_upload(self, request: ReleaseRequest, app_id: str,
                         artifact: _Artifact) -> tuple[_UploadInspection | None, bool]:
        metadata = self._metadata(request)
        query = urlencode({
            "filter[cfBundleShortVersionString]": request.version,
            "filter[cfBundleVersion]": str(metadata["build_number"]),
            "filter[platform]": str(metadata["apple_platform"]),
            "limit": "20",
        })
        status, payload = self._api("GET", f"/v1/apps/{quote(app_id, safe='')}/buildUploads?{query}")
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if status != 200 or not isinstance(data, list):
            return None, True
        candidates = [item for item in data if isinstance(item, Mapping)
                      and self._attributes(item).get("cfBundleShortVersionString") == request.version
                      and str(self._attributes(item).get("cfBundleVersion")) == str(metadata["build_number"])
                      and self._attributes(item).get("platform") == metadata["apple_platform"]]
        if not candidates:
            return None, False
        if len(candidates) != 1:
            return None, True
        candidate = candidates[0]
        upload_id = self._resource_id(candidate.get("id"))
        if upload_id is None:
            return None, True
        state = self._attributes(candidate).get("state")
        if not isinstance(state, str):
            return None, True
        file_status, file_payload = self._api(
            "GET", f"/v1/buildUploads/{quote(upload_id, safe='')}/buildUploadFiles?limit=20",
        )
        files = file_payload.get("data") if isinstance(file_payload, Mapping) else None
        if file_status != 200 or not isinstance(files, list) or len(files) != 1 \
                or not isinstance(files[0], Mapping):
            return _UploadInspection(state, upload_id, None, False), True
        attributes = self._attributes(files[0])
        checksum = attributes.get("sourceFileChecksums")
        file_checksum = checksum.get("file") if isinstance(checksum, Mapping) else None
        exact = bool(
            attributes.get("fileName") == artifact.name
            and attributes.get("fileSize") == artifact.size
            and isinstance(file_checksum, Mapping)
            and file_checksum.get("algorithm") == _CHECKSUM_ALGORITHM
            and file_checksum.get("hash") == artifact.sha256
        )
        upload_inspection = self._read_upload(upload_id, expected=(request, artifact))
        if upload_inspection is None:
            return _UploadInspection(state, upload_id, None, exact), True
        return _UploadInspection(upload_inspection.state, upload_id, upload_inspection.build_id, exact), not exact

    def _read_upload(self, upload_id: str, expected: tuple[ReleaseRequest, _Artifact] | None = None) \
            -> _UploadInspection | None:
        status, payload = self._api("GET", f"/v1/buildUploads/{quote(upload_id, safe='')}?include=build")
        resource = self._data_object(payload)
        if status != 200 or resource is None or resource.get("id") != upload_id:
            return None
        attributes = self._attributes(resource)
        state = attributes.get("state")
        if not isinstance(state, str):
            return None
        if expected is not None:
            request, artifact = expected
            metadata = self._metadata(request)
            if (attributes.get("cfBundleShortVersionString") != request.version
                    or str(attributes.get("cfBundleVersion")) != str(metadata.get("build_number"))
                    or attributes.get("platform") != metadata.get("apple_platform")):
                return None
        relationships = resource.get("relationships")
        build_rel = relationships.get("build") if isinstance(relationships, Mapping) else None
        build_data = build_rel.get("data") if isinstance(build_rel, Mapping) else None
        build_id = self._resource_id(build_data.get("id")) if isinstance(build_data, Mapping) else None
        included = payload.get("included") if isinstance(payload, Mapping) else None
        if build_id is None and isinstance(included, list):
            build = next((item for item in included if isinstance(item, Mapping)
                          and item.get("type") == "builds"), None)
            build_id = self._resource_id(build.get("id")) if isinstance(build, Mapping) else None
        return _UploadInspection(state, upload_id, build_id, True)

    def _upload_operation(self, operation: Mapping[str, object], path: Path) -> bool:
        method = operation.get("method")
        url = operation.get("url")
        offset = operation.get("offset")
        length = operation.get("length")
        if method != "PUT" or not isinstance(url, str) or len(url) > _UPLOAD_URL_MAX:
            raise ValueError("UPLOAD_OPERATION_INVALID")
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").casefold()
            if (parsed.scheme != "https" or not host
                    or not (host == "blobstore.apple.com" or host.endswith(".blobstore.apple.com"))
                    or parsed.username or parsed.password or parsed.port not in (None, 443)
                    or parsed.fragment):
                raise ValueError("UPLOAD_HOST_NOT_ALLOWED")
        except ValueError as exc:
            if str(exc) == "UPLOAD_HOST_NOT_ALLOWED":
                raise
            raise ValueError("UPLOAD_URL_INVALID") from None
        if (not isinstance(offset, int) or isinstance(offset, bool)
                or not isinstance(length, int) or isinstance(length, bool)
                or offset < 0 or length <= 0):
            raise ValueError("UPLOAD_RANGE_INVALID")
        headers_value = operation.get("requestHeaders", [])
        if not isinstance(headers_value, list) or len(headers_value) > _UPLOAD_HEADERS_MAX:
            raise ValueError("UPLOAD_HEADERS_INVALID")
        headers: dict[str, str] = {}
        for item in headers_value:
            if not isinstance(item, Mapping):
                raise ValueError("UPLOAD_HEADERS_INVALID")
            name = item.get("name")
            value = item.get("value")
            if (not isinstance(name, str) or not _SAFE_UPLOAD_HEADER.fullmatch(name)
                    or name.casefold() in {key.casefold() for key in headers}
                    or not isinstance(value, str) or len(value) > 8192
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
                raise ValueError("UPLOAD_HEADERS_INVALID")
            headers[name] = value
        status = self.upload_transport.upload(url, headers, path, offset, length)
        return isinstance(status, int) and not isinstance(status, bool) and 200 <= status < 300

    @staticmethod
    def _validate_operations(operations: object, size: int) -> list[Mapping[str, object]] | None:
        if not isinstance(operations, list) or not operations or len(operations) > _UPLOAD_OPERATION_MAX:
            return None
        if any(not isinstance(operation, Mapping) for operation in operations):
            return None
        for operation in operations:
            offset = operation.get("offset")
            if not isinstance(offset, int) or isinstance(offset, bool):
                return None
        ordered = sorted(operations, key=lambda operation: operation.get("offset", -1))
        cursor = 0
        for operation in ordered:
            offset = operation.get("offset")
            length = operation.get("length")
            if (not isinstance(offset, int) or isinstance(offset, bool)
                    or not isinstance(length, int) or isinstance(length, bool)
                    or offset != cursor or length <= 0 or offset + length > size):
                return None
            cursor += length
        return ordered if cursor == size else None

    def _create_upload(self, request: ReleaseRequest, app_id: str,
                       artifact: _Artifact) -> tuple[ReleaseResult | None, str | None]:
        metadata = self._metadata(request)
        platform = str(metadata["apple_platform"])
        body = {"data": {
            "type": "buildUploads",
            "attributes": {"cfBundleShortVersionString": request.version,
                           "cfBundleVersion": str(metadata["build_number"]), "platform": platform},
            "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
        }}
        status, payload = self._api("POST", "/v1/buildUploads", body)
        resource = self._data_object(payload)
        upload_id = self._resource_id(resource.get("id")) if isinstance(resource, Mapping) else None
        if status not in {201, 200} or upload_id is None:
            if status in {0, 408, 429, 500, 502, 503, 504}:
                return ReleaseResult("UNCERTAIN", "BUILD_UPLOAD_CREATE_UNCONFIRMED", None, None,
                                     "App Store Connect build upload creation could not be confirmed; inspect before retrying.", True), None
            return ReleaseResult("FAILED", "BUILD_UPLOAD_CREATE_FAILED", None, None,
                                 "App Store Connect could not create a build upload.", False), None

        extension = artifact.path.suffix.casefold()
        uti = "com.apple.pkg" if extension == ".pkg" else "com.apple.ipa"
        file_body = {"data": {
            "type": "buildUploadFiles",
            "attributes": {"fileName": artifact.name, "fileSize": artifact.size,
                           "assetType": "ASSET", "uti": uti},
            "relationships": {"buildUpload": {"data": {"type": "buildUploads", "id": upload_id}}},
        }}
        file_status, file_payload = self._api("POST", "/v1/buildUploadFiles", file_body)
        file_resource = self._data_object(file_payload)
        file_id = self._resource_id(file_resource.get("id")) if isinstance(file_resource, Mapping) else None
        file_attributes = self._attributes(file_resource)
        if file_status not in {201, 200} or file_id is None:
            return ReleaseResult("UNCERTAIN", "BUILD_FILE_RESERVATION_UNCONFIRMED", None, upload_id,
                                 "App Store Connect build file reservation could not be confirmed; inspect before retrying.", True), None
        operations = self._validate_operations(file_attributes.get("uploadOperations"), artifact.size)
        if operations is None:
            return ReleaseResult("UNCERTAIN", "UPLOAD_OPERATIONS_INVALID", None, upload_id,
                                 "Apple returned upload instructions that could not be validated; inspect before retrying.", True), None
        for operation in operations:
            try:
                uploaded = self._upload_operation(operation, artifact.path)
            except ValueError:
                return ReleaseResult("BLOCKED", "UPLOAD_OPERATION_BLOCKED", None, upload_id,
                                     "Apple returned an unsupported or unsafe build upload operation.", False), None
            if not uploaded:
                return ReleaseResult("UNCERTAIN", "BUILD_FILE_UPLOAD_UNCONFIRMED", None, upload_id,
                                     "A build upload part could not be confirmed; inspect before retrying.", True), None
        commit_body = {"data": {
            "type": "buildUploadFiles", "id": file_id,
            "attributes": {"uploaded": True, "sourceFileChecksums": {
                "file": {"hash": artifact.sha256, "algorithm": _CHECKSUM_ALGORITHM},
            }},
        }}
        commit_status, _ = self._api("PATCH", f"/v1/buildUploadFiles/{quote(file_id, safe='')}", commit_body)
        if commit_status not in _API_SUCCESS:
            return ReleaseResult("UNCERTAIN", "BUILD_FILE_COMMIT_UNCONFIRMED", None, upload_id,
                                 "App Store Connect did not confirm the build file commit; inspect before retrying.", True), None
        return None, upload_id

    def _processed_build(self, request: ReleaseRequest, app_id: str,
                         artifact: _Artifact) -> tuple[ReleaseResult | None, str | None]:
        existing, uncertain = self._matching_upload(request, app_id, artifact)
        if uncertain:
            return ReleaseResult("UNCERTAIN", "BUILD_UPLOAD_INSPECTION_REQUIRED", None,
                                 existing.upload_id if existing else None,
                                 "An App Store Connect build upload needs inspection before another upload can start.", True), None
        if existing is None:
            result, upload_id = self._create_upload(request, app_id, artifact)
            if result is not None:
                return result, None
            if upload_id is None:
                return ReleaseResult("UNCERTAIN", "BUILD_UPLOAD_ID_UNAVAILABLE", None, None,
                                     "The App Store Connect build upload could not be identified; inspect before retrying.", True), None
            existing = self._read_upload(upload_id, expected=(request, artifact))
            if existing is None:
                return ReleaseResult("UNCERTAIN", "BUILD_UPLOAD_READBACK_UNAVAILABLE", None, upload_id,
                                     "The App Store Connect build upload could not be read back; inspect before retrying.", True), None
            # Read back the file checksum as well as the build upload state after commit.
            file_status, files_payload = self._api(
                "GET", f"/v1/buildUploads/{quote(upload_id, safe='')}/buildUploadFiles?limit=20",
            )
            files = files_payload.get("data") if isinstance(files_payload, Mapping) else None
            if file_status != 200 or not isinstance(files, list) or len(files) != 1 \
                    or not isinstance(files[0], Mapping):
                return ReleaseResult("UNCERTAIN", "BUILD_FILE_READBACK_UNAVAILABLE", None, upload_id,
                                     "The App Store Connect build file could not be read back; inspect before retrying.", True), None
            attributes = self._attributes(files[0])
            checksum = attributes.get("sourceFileChecksums")
            file_checksum = checksum.get("file") if isinstance(checksum, Mapping) else None
            if (attributes.get("fileName") != artifact.name or attributes.get("fileSize") != artifact.size
                    or not isinstance(file_checksum, Mapping)
                    or file_checksum.get("algorithm") != _CHECKSUM_ALGORITHM
                    or file_checksum.get("hash") != artifact.sha256):
                return ReleaseResult("UNCERTAIN", "BUILD_FILE_CHECKSUM_MISMATCH", None, upload_id,
                                     "The App Store Connect build file checksum did not match the local artifact.", True), None
            state = self._attributes_from_upload(upload_id, existing.state)
            existing = _UploadInspection(state, upload_id, existing.build_id, True)
        if existing.state.casefold() == "failed":
            return ReleaseResult("FAILED", "BUILD_PROCESSING_FAILED", None, existing.upload_id,
                                 "App Store Connect rejected the uploaded build during processing.", False), None
        if existing.state.casefold() != "complete" or existing.build_id is None:
            return ReleaseResult("SUBMITTED", existing.state.upper()[:80], None, existing.upload_id,
                                 "The build is uploaded and App Store Connect is processing it.", True), None
        return None, existing.build_id

    def _attributes_from_upload(self, upload_id: str, fallback: str) -> str:
        status, payload = self._api("GET", f"/v1/buildUploads/{quote(upload_id, safe='')}?include=build")
        resource = self._data_object(payload)
        state = self._attributes(resource).get("state") if status == 200 else None
        return state if isinstance(state, str) else fallback

    def _app_store_version(self, request: ReleaseRequest, app_id: str) \
            -> tuple[Mapping[str, object] | None, ReleaseResult | None]:
        metadata = self._metadata(request)
        query = urlencode({"filter[versionString]": request.version,
                           "filter[platform]": str(metadata["apple_platform"]), "limit": "20"})
        status, payload = self._api("GET", f"/v1/apps/{quote(app_id, safe='')}/appStoreVersions?{query}")
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if status != 200 or not isinstance(data, list):
            return None, ReleaseResult("UNCERTAIN", "APP_VERSION_INSPECTION_FAILED", None, None,
                                       "App Store Connect version state could not be inspected.", True)
        matches = [item for item in data if isinstance(item, Mapping)
                   and self._attributes(item).get("versionString") == request.version
                   and self._attributes(item).get("platform") == metadata["apple_platform"]]
        if len(matches) != 1:
            return None, ReleaseResult("BLOCKED", "APP_VERSION_NOT_CONFIGURED", None, None,
                                       "Create the matching App Store version and complete its metadata before release.", False)
        return matches[0], None

    def _version_state_result(self, version: Mapping[str, object], build_id: str | None) -> ReleaseResult | None:
        version_id = self._resource_id(version.get("id"))
        state = self._attributes(version).get("appStoreState")
        if not isinstance(state, str):
            return None
        if state in {
            "READY_FOR_SALE", "WAITING_FOR_REVIEW", "IN_REVIEW",
            "PENDING_DEVELOPER_RELEASE", "READY_FOR_DISTRIBUTION",
        }:
            if version_id is None or build_id is None:
                return ReleaseResult("UNCERTAIN", "VERSION_BUILD_READBACK_UNAVAILABLE", None, version_id,
                                     "The App Store version and requested build could not be matched remotely.", True)
            read_status, read_payload = self._api(
                "GET", f"/v1/appStoreVersions/{quote(version_id, safe='')}?include=build",
            )
            read_resource = self._data_object(read_payload)
            if read_status != 200 or read_resource is None or read_resource.get("id") != version_id:
                return ReleaseResult("UNCERTAIN", "VERSION_BUILD_READBACK_UNAVAILABLE", None, version_id,
                                     "The App Store version build relationship could not be read back.", True)
            relationships = read_resource.get("relationships")
            build_relation = relationships.get("build") if isinstance(relationships, Mapping) else None
            build_data = build_relation.get("data") if isinstance(build_relation, Mapping) else None
            linked_build_id = self._resource_id(build_data.get("id")) if isinstance(build_data, Mapping) else None
            if linked_build_id != build_id:
                return ReleaseResult("UNCERTAIN", "VERSION_BUILD_READBACK_MISMATCH", None, version_id,
                                     "The App Store version is linked to a different build than this release request.", True)
        if state == "READY_FOR_SALE":
            return ReleaseResult("PUBLISHED", state, None, version_id,
                                 "App Store Connect confirms the app version is available for sale.", False)
        if state in {"WAITING_FOR_REVIEW", "IN_REVIEW", "PENDING_DEVELOPER_RELEASE", "READY_FOR_DISTRIBUTION"}:
            return ReleaseResult("SUBMITTED", state, None, version_id,
                                 "App Store Connect has accepted the version into its release workflow.", False)
        return None

    def _existing_review_submission(self, request: ReleaseRequest, app_id: str,
                                    version_id: str) -> tuple[tuple[str, str] | None, bool]:
        platform = str(self._metadata(request)["apple_platform"])
        query = urlencode({"filter[platform]": platform, "limit": "50"})
        status, payload = self._api("GET", f"/v1/apps/{quote(app_id, safe='')}/reviewSubmissions?{query}")
        submissions = payload.get("data") if isinstance(payload, Mapping) else None
        if status != 200 or not isinstance(submissions, list):
            return None, True
        links = payload.get("links") if isinstance(payload, Mapping) else None
        if isinstance(links, Mapping) and links.get("next"):
            return None, True
        matches: list[tuple[str, str]] = []
        for submission in submissions:
            if not isinstance(submission, Mapping):
                return None, True
            submission_id = self._resource_id(submission.get("id"))
            submission_state = self._attributes(submission).get("state")
            if submission_id is None or not isinstance(submission_state, str):
                return None, True
            item_status, items_payload = self._api(
                "GET", f"/v1/reviewSubmissions/{quote(submission_id, safe='')}/items?include=appStoreVersion&limit=50",
            )
            items = items_payload.get("data") if isinstance(items_payload, Mapping) else None
            if item_status != 200 or not isinstance(items, list):
                return None, True
            if not items and submission_state == "READY_FOR_REVIEW":
                # This may be a submission created by an earlier request whose
                # response was lost before its version item was attached.
                return None, True
            for item in items:
                if not isinstance(item, Mapping):
                    return None, True
                relationships = item.get("relationships")
                version_rel = relationships.get("appStoreVersion") if isinstance(relationships, Mapping) else None
                version_data = version_rel.get("data") if isinstance(version_rel, Mapping) else None
                if not isinstance(version_data, Mapping) or self._resource_id(version_data.get("id")) is None:
                    return None, True
                if version_data.get("id") == version_id:
                    matches.append((submission_id, submission_state))
                    break
        if len(matches) > 1:
            return None, True
        return (matches[0] if matches else None), False

    def _submit_for_review(self, request: ReleaseRequest, app_id: str,
                           build_id: str) -> ReleaseResult:
        version, error = self._app_store_version(request, app_id)
        if error is not None:
            return error
        if version is None:
            return ReleaseResult("UNCERTAIN", "APP_VERSION_UNAVAILABLE", None, None,
                                 "The App Store version could not be confirmed.", True)
        already_submitted = self._version_state_result(version, build_id)
        if already_submitted is not None:
            return already_submitted
        version_id = self._resource_id(version.get("id"))
        state = self._attributes(version).get("appStoreState")
        if version_id is None or state != "READY_FOR_REVIEW":
            return ReleaseResult("BLOCKED", "APP_VERSION_NOT_READY", None, version_id,
                                 "App Store metadata must be complete and ready for review before submission.", False)

        patch_body = {"data": {"type": "builds", "id": build_id}}
        patch_status, _ = self._api("PATCH", f"/v1/appStoreVersions/{quote(version_id, safe='')}/relationships/build",
                                    patch_body)
        if patch_status not in _API_SUCCESS:
            return ReleaseResult("UNCERTAIN", "VERSION_BUILD_LINK_UNCONFIRMED", None, version_id,
                                 "App Store Connect build attachment could not be confirmed; inspect before retrying.", True)
        read_status, read_payload = self._api("GET", f"/v1/appStoreVersions/{quote(version_id, safe='')}?include=build")
        read_resource = self._data_object(read_payload)
        relationships = read_resource.get("relationships") if isinstance(read_resource, Mapping) else None
        build_relation = relationships.get("build") if isinstance(relationships, Mapping) else None
        build_data = build_relation.get("data") if isinstance(build_relation, Mapping) else None
        read_build_id = self._resource_id(build_data.get("id")) if isinstance(build_data, Mapping) else None
        if read_status != 200 or read_build_id != build_id:
            return ReleaseResult("UNCERTAIN", "VERSION_BUILD_READBACK_MISMATCH", None, version_id,
                                 "App Store Connect did not read back the expected build attachment.", True)

        existing_submission, ambiguous = self._existing_review_submission(request, app_id, version_id)
        if ambiguous:
            return ReleaseResult("UNCERTAIN", "REVIEW_SUBMISSION_INSPECTION_REQUIRED", None, version_id,
                                 "Existing App Review submissions could not be inspected safely; inspect before retrying.", True)
        submission_id: str | None = None
        if existing_submission is not None:
            submission_id, existing_state = existing_submission
            if existing_state in {"WAITING_FOR_REVIEW", "IN_REVIEW"}:
                return ReleaseResult("SUBMITTED", existing_state, None, submission_id,
                                     "App Store Connect confirms an existing submission is in review.", False)
            if existing_state != "READY_FOR_REVIEW":
                return ReleaseResult("UNCERTAIN", existing_state[:80], None, submission_id,
                                     "An existing App Review submission needs inspection before retrying.", True)
        else:
            create_body = {"data": {
                "type": "reviewSubmissions",
                "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
            }}
            create_status, create_payload = self._api("POST", "/v1/reviewSubmissions", create_body)
            submission = self._data_object(create_payload)
            submission_id = self._resource_id(submission.get("id")) if isinstance(submission, Mapping) else None
            if create_status not in {201, 200} or submission_id is None:
                if create_status in {0, 408, 429, 500, 502, 503, 504}:
                    return ReleaseResult("UNCERTAIN", "REVIEW_SUBMISSION_CREATE_UNCONFIRMED", None, version_id,
                                         "App Review submission creation could not be confirmed; inspect before retrying.", True)
                return ReleaseResult("FAILED", "REVIEW_SUBMISSION_CREATE_FAILED", None, version_id,
                                     "App Store Connect could not create an App Review submission.", False)
            item_body = {"data": {
                "type": "reviewSubmissionItems",
                "relationships": {
                    "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": submission_id}},
                    "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}},
                },
            }}
            item_status, _ = self._api("POST", "/v1/reviewSubmissionItems", item_body)
            if item_status not in {201, 200}:
                return ReleaseResult("UNCERTAIN", "REVIEW_SUBMISSION_ITEM_UNCONFIRMED", None, submission_id,
                                     "The App Review version item could not be confirmed; inspect before retrying.", True)
        if submission_id is None:
            return ReleaseResult("UNCERTAIN", "REVIEW_SUBMISSION_ID_UNAVAILABLE", None, version_id,
                                 "The App Review submission could not be identified; inspect before retrying.", True)
        submit_body = {"data": {"type": "reviewSubmissions", "id": submission_id,
                                 "attributes": {"submitted": True}}}
        submit_status, _ = self._api("PATCH", f"/v1/reviewSubmissions/{quote(submission_id, safe='')}", submit_body)
        if submit_status not in _API_SUCCESS:
            return ReleaseResult("UNCERTAIN", "REVIEW_SUBMISSION_UNCONFIRMED", None, submission_id,
                                 "App Review submission could not be confirmed; inspect before retrying.", True)
        verify_status, verify_payload = self._api("GET", f"/v1/reviewSubmissions/{quote(submission_id, safe='')}")
        verify_resource = self._data_object(verify_payload)
        verify_state = self._attributes(verify_resource).get("state")
        if verify_status != 200 or not isinstance(verify_state, str):
            return ReleaseResult("UNCERTAIN", "REVIEW_SUBMISSION_READBACK_UNAVAILABLE", None, submission_id,
                                 "App Review submission state could not be read back; inspect before retrying.", True)
        if verify_state in {"WAITING_FOR_REVIEW", "IN_REVIEW"}:
            return ReleaseResult("SUBMITTED", verify_state, None, submission_id,
                                 "App Store Connect confirms the version is waiting for App Review.", False)
        if verify_state in {"APPROVED", "ACCEPTED"}:
            return ReleaseResult("SUBMITTED", verify_state, None, submission_id,
                                 "App Review approved the submission; storefront availability still needs read-back.", True)
        return ReleaseResult("UNCERTAIN", verify_state[:80], None, submission_id,
                             "App Store Connect returned a non-final review state; inspect before retrying.", True)

    def _internal_testflight_group_error(self, app_id: str, group_ids: tuple[str, ...] | list[str],
                                         build_id: str | None = None) -> ReleaseResult | None:
        """Verify each group belongs to this app and is internal before assigning a build."""
        for group_id in group_ids:
            status, payload = self._api(
                "GET", f"/v1/betaGroups/{quote(group_id, safe='')}?include=app",
            )
            group = self._data_object(payload)
            if status != 200 or group is None or group.get("id") != group_id:
                return ReleaseResult(
                    "UNCERTAIN", "TESTFLIGHT_GROUP_INSPECTION_REQUIRED", None, build_id,
                    "A TestFlight group could not be inspected; no build was assigned to it.", True,
                )
            if self._attributes(group).get("isInternalGroup") is not True:
                return ReleaseResult(
                    "BLOCKED", "TESTFLIGHT_GROUP_NOT_INTERNAL", None, build_id,
                    "Only verified internal TestFlight groups can be assigned automatically.", False,
                )
            relationships = group.get("relationships")
            app_relation = relationships.get("app") if isinstance(relationships, Mapping) else None
            app_data = app_relation.get("data") if isinstance(app_relation, Mapping) else None
            linked_app_id = self._resource_id(app_data.get("id")) if isinstance(app_data, Mapping) else None
            if linked_app_id != app_id:
                return ReleaseResult(
                    "BLOCKED", "TESTFLIGHT_GROUP_APP_MISMATCH", None, build_id,
                    "A TestFlight group does not belong to the selected App Store Connect app.", False,
                )
        return None

    def _submit_testflight(self, request: ReleaseRequest, app_id: str,
                           build_id: str) -> ReleaseResult:
        if request.channel.casefold() == "external":
            return ReleaseResult(
                "BLOCKED", "EXTERNAL_TESTFLIGHT_REQUIRES_REVIEW", None, build_id,
                "External TestFlight distribution requires Beta App Review and tester-facing metadata.", False,
            )
        group_ids = self._metadata(request).get("testflight_group_ids", ())
        if group_ids:
            if not isinstance(group_ids, (tuple, list)) or not 1 <= len(group_ids) <= 50 \
                    or any(self._resource_id(item) is None for item in group_ids) \
                    or len(set(group_ids)) != len(group_ids):
                return ReleaseResult("BLOCKED", "TESTFLIGHT_GROUPS_INVALID", None, build_id,
                                     "TestFlight group identifiers must be explicitly supplied and valid.", False)
            group_error = self._internal_testflight_group_error(app_id, group_ids, build_id)
            if group_error is not None:
                return group_error
            body = {"data": [{"type": "betaGroups", "id": item} for item in group_ids]}
            status, _ = self._api("POST", f"/v1/builds/{quote(build_id, safe='')}/relationships/betaGroups", body)
            if status not in _API_SUCCESS:
                return ReleaseResult("UNCERTAIN", "TESTFLIGHT_GROUP_LINK_UNCONFIRMED", None, build_id,
                                     "TestFlight group assignment could not be confirmed; inspect before retrying.", True)
            read_status, read_payload = self._api(
                "GET", f"/v1/builds/{quote(build_id, safe='')}/betaGroups?limit=200",
            )
            data = read_payload.get("data") if isinstance(read_payload, Mapping) else None
            read_ids = {item.get("id") for item in data if isinstance(item, Mapping)} if isinstance(data, list) else set()
            if read_status != 200 or not set(group_ids).issubset(read_ids):
                return ReleaseResult("UNCERTAIN", "TESTFLIGHT_GROUP_READBACK_MISMATCH", None, build_id,
                                     "App Store Connect did not read back the requested TestFlight groups.", True)
            group_error = self._internal_testflight_group_error(app_id, group_ids, build_id)
            if group_error is not None:
                return group_error
        if group_ids:
            summary = "The build was assigned to verified internal TestFlight groups and the relationship was read back."
        else:
            summary = "The build was uploaded; select an internal TestFlight group before testers can access it."
        return ReleaseResult("SUBMITTED", "TESTFLIGHT_BUILD_READY", None, build_id, summary, False)

    def publish(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        preflight = self.preflight(request)
        if preflight.status != "READY":
            return ReleaseResult("BLOCKED", "PREFLIGHT_BLOCKED", None, None,
                                 preflight.blockers[0] if preflight.blockers else "App Store Connect preflight is blocked.", False)
        artifact, artifact_error = self._artifact(request)
        if artifact is None:
            return ReleaseResult("BLOCKED", "ARTIFACT_INVALID", None, None,
                                 artifact_error or "The App Store build artifact is invalid.", False)
        try:
            app_id = self._app_id(request)
            if app_id is None:
                return ReleaseResult("BLOCKED", "APP_NOT_FOUND_OR_AMBIGUOUS", None, None,
                                     "The App Store Connect app could not be resolved by bundle identifier.", False)
            channel = request.channel.casefold()
            group_ids = self._metadata(request).get("testflight_group_ids", ())
            if channel in _TESTFLIGHT_CHANNELS and group_ids:
                group_error = self._internal_testflight_group_error(app_id, group_ids)
                if group_error is not None:
                    return group_error
            build_result, build_id = self._processed_build(request, app_id, artifact)
            if build_result is not None:
                return build_result
            if build_id is None:
                return ReleaseResult("UNCERTAIN", "BUILD_ID_UNAVAILABLE", None, None,
                                     "The processed App Store build could not be identified; inspect before retrying.", True)
            if channel in _TESTFLIGHT_CHANNELS:
                return self._submit_testflight(request, app_id, build_id)
            return self._submit_for_review(request, app_id, build_id)
        except Exception:
            return ReleaseResult("UNCERTAIN", "APPLE_OPERATION_INTERRUPTED", None, None,
                                 "App Store Connect operation was interrupted; inspect remote state before retrying.", True)

    def inspect(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        preflight = self.preflight(request)
        if preflight.status != "READY":
            return ReleaseResult("BLOCKED", "PREFLIGHT_BLOCKED", None, None,
                                 preflight.blockers[0] if preflight.blockers else "App Store Connect preflight is blocked.", False)
        artifact, artifact_error = self._artifact(request)
        if artifact is None:
            return ReleaseResult("BLOCKED", "ARTIFACT_INVALID", None, None,
                                 artifact_error or "The App Store build artifact is invalid.", False)
        try:
            app_id = self._app_id(request)
            if app_id is None:
                return ReleaseResult("UNCERTAIN", "APP_INSPECTION_UNAVAILABLE", None, None,
                                     "The App Store Connect app could not be resolved during inspection.", True)
            upload, uncertain = self._matching_upload(request, app_id, artifact)
            if uncertain:
                return ReleaseResult("UNCERTAIN", "BUILD_UPLOAD_INSPECTION_REQUIRED", None,
                                     upload.upload_id if upload else None,
                                     "App Store Connect build state is ambiguous; inspect before retrying.", True)
            if upload is None:
                return ReleaseResult("FAILED", "BUILD_UPLOAD_NOT_FOUND", None, None,
                                     "No matching App Store Connect upload was found.", False)
            if upload.state.casefold() != "complete" or upload.build_id is None:
                return ReleaseResult("SUBMITTED", upload.state.upper()[:80], None, upload.upload_id,
                                     "App Store Connect is still processing the uploaded build.", True)
            if request.channel.casefold() in _TESTFLIGHT_CHANNELS:
                return self._submit_testflight(request, app_id, upload.build_id)
            version, error = self._app_store_version(request, app_id)
            if error is not None:
                return error
            if version is None:
                return ReleaseResult("UNCERTAIN", "APP_VERSION_UNAVAILABLE", None, None,
                                     "App Store version state could not be inspected.", True)
            result = self._version_state_result(version, upload.build_id)
            if result is not None:
                return result
            return ReleaseResult("SUBMITTED", str(self._attributes(version).get("appStoreState", "UNKNOWN"))[:80],
                                 None, self._resource_id(version.get("id")),
                                 "App Store Connect has not completed App Review or storefront publication.", True)
        except Exception:
            return ReleaseResult("UNCERTAIN", "APPLE_INSPECTION_INTERRUPTED", None, None,
                                 "App Store Connect inspection was interrupted; inspect remote state before retrying.", True)


__all__ = ["AppStoreConnectAdapter", "AppleBuildUploadTransport"]
