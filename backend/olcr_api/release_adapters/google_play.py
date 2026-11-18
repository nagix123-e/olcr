"""Fail-closed Google Play Android Publisher API release adapter."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import parse_qs, quote, urlsplit

from ..coding_tasks import ReleaseIntent, ReleasePreflight, build_release_preflight
from ..release_workflow import ReleaseRequest, ReleaseResult
from .base import ProviderApiTransport, ProviderRequest, ProviderResponse


_PACKAGE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9_]*\.)+[A-Za-z][A-Za-z0-9_]*$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}$")
_VERSION_CODE = re.compile(r"^[1-9][0-9]{0,17}$")
_EDIT_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACCESS_TOKEN = re.compile(r"^[A-Za-z0-9._~+/-]{16,16384}={0,2}$")
_UPLOAD_ID = re.compile(r"^[A-Za-z0-9._~+/-]{1,1024}$")
_UPLOAD_RANGE = re.compile(r"^(?:bytes=)?0-([0-9]+)$")
_TRACKS = frozenset({"internal", "alpha", "beta", "production"})
_ARTIFACT_MAX_BYTES = 2 * 1024 * 1024 * 1024
_API_SUCCESS = frozenset({200, 201, 204})
_TRANSIENT = frozenset({0, 408, 409, 429, 500, 502, 503, 504})
_RELEASE_FIELDS = frozenset({
    "name", "versionCodes", "releaseNotes", "status", "userFraction",
    "countryTargeting", "inAppUpdatePriority",
})


@dataclass(frozen=True)
class _Artifact:
    path: Path
    kind: str
    size: int
    sha256: str


class GooglePlayMediaUploadTransport:
    """Resume an APK/AAB upload through Google's fixed resumable upload endpoint."""

    _CHUNK_BYTES = 1024 * 1024
    _RESPONSE_MAX_BYTES = 1024 * 1024
    _RESUME_ATTEMPTS = 4

    @staticmethod
    def _open_absolute_nofollow(path: Path) -> int:
        parts = path.parts
        directory_flag = getattr(os, "O_DIRECTORY", None)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if (not path.is_absolute() or len(parts) < 2 or parts[0] != "/"
                or directory_flag is None or nofollow is None or os.open not in os.supports_dir_fd):
            raise OSError("SAFE_UPLOAD_PATH_UNAVAILABLE")
        parent = os.open("/", os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0))
        try:
            for component in parts[1:-1]:
                next_parent = os.open(
                    component, os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent,
                )
                os.close(parent)
                parent = next_parent
            return os.open(parts[-1], os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0), dir_fd=parent)
        finally:
            os.close(parent)

    @staticmethod
    def _validated_session_url(value: object, route: str) -> str | None:
        if not isinstance(value, str) or len(value) > 4096:
            return None
        try:
            parsed = urlsplit(value)
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
            upload_ids = query.get("upload_id", [])
            if (parsed.scheme != "https" or parsed.hostname not in {
                    "androidpublisher.googleapis.com", "www.googleapis.com",
                } or parsed.username is not None or parsed.password is not None
                    or parsed.port not in {None, 443} or parsed.fragment
                    or parsed.path != route or set(query) != {"uploadType", "upload_id"}
                    or query.get("uploadType") != ["resumable"] or len(upload_ids) != 1
                    or not _UPLOAD_ID.fullmatch(upload_ids[0])):
                return None
            return value
        except (ValueError, TypeError):
            return None

    @classmethod
    def _response_body(cls, response) -> tuple[str, str | None]:
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > cls._RESPONSE_MAX_BYTES:
                return "", response.headers.get("Content-Type")
            body.extend(chunk)
        return body.decode("utf-8", "replace"), response.headers.get("Content-Type")

    @staticmethod
    def _received_offset(range_header: object, size: int) -> int | None:
        if range_header is None:
            return 0
        if not isinstance(range_header, str):
            return None
        match = _UPLOAD_RANGE.fullmatch(range_header.strip())
        if not match:
            return None
        last_byte = int(match.group(1))
        return last_byte + 1 if 0 <= last_byte < size else None

    def upload(self, package_name: str, edit_id: str, artifact_type: str,
               path: Path, access_token: str) -> ProviderResponse:
        if (not _PACKAGE.fullmatch(package_name) or not _EDIT_ID.fullmatch(edit_id)
                or artifact_type not in {"apk", "bundle"} or not isinstance(access_token, str)
                or not _ACCESS_TOKEN.fullmatch(access_token)):
            return ProviderResponse(0, "", None)
        resource = "apks" if artifact_type == "apk" else "bundles"
        route = ("/upload/androidpublisher/v3/applications/" + quote(package_name, safe="")
                 + "/edits/" + quote(edit_id, safe="") + f"/{resource}")
        url = "https://androidpublisher.googleapis.com" + route + "?uploadType=resumable"
        mime_type = "application/vnd.android.package-archive" if artifact_type == "apk" else "application/octet-stream"
        try:
            descriptor = self._open_absolute_nofollow(Path(path))
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _ARTIFACT_MAX_BYTES:
                os.close(descriptor)
                return ProviderResponse(0, "", None)
            stream = os.fdopen(descriptor, "rb")
        except OSError:
            return ProviderResponse(0, "", None)

        base_headers = {"Authorization": f"Bearer {access_token}"}

        def content(offset: int):
            stream.seek(offset)
            remaining = info.st_size - offset
            while remaining:
                chunk = stream.read(min(self._CHUNK_BYTES, remaining))
                if not chunk:
                    raise OSError("UPLOAD_SOURCE_CHANGED")
                remaining -= len(chunk)
                yield chunk

        try:
            import httpx

            with httpx.Client(timeout=300, follow_redirects=False, verify=True) as client:
                start_headers = {
                    **base_headers,
                    "Content-Length": "0",
                    "X-Upload-Content-Type": mime_type,
                    "X-Upload-Content-Length": str(info.st_size),
                }
                try:
                    start_response = client.post(url, headers=start_headers, content=b"")
                except Exception:
                    return ProviderResponse(0, "", None)
                try:
                    start_body, start_content_type = self._response_body(start_response)
                except Exception:
                    return ProviderResponse(0, "", None)
                if start_response.status_code != 200:
                    return ProviderResponse(start_response.status_code, start_body, start_content_type)
                session_url = self._validated_session_url(start_response.headers.get("Location"), route)
                if session_url is None:
                    return ProviderResponse(0, "", None)

                offset = 0
                for attempt in range(self._RESUME_ATTEMPTS):
                    upload_headers = {
                        **base_headers,
                        "Content-Type": mime_type,
                        "Content-Length": str(info.st_size - offset),
                    }
                    if offset:
                        upload_headers["Content-Range"] = (
                            f"bytes {offset}-{info.st_size - 1}/{info.st_size}"
                        )
                    try:
                        with client.stream("PUT", session_url, headers=upload_headers,
                                           content=content(offset)) as response:
                            body, content_type = self._response_body(response)
                            status = response.status_code
                            response_headers = response.headers
                    except Exception:
                        status, body, content_type, response_headers = 0, "", None, {}
                    if status in {200, 201}:
                        return ProviderResponse(status, body, content_type)
                    if status not in {0, 308, 408, 409, 429} and status < 500:
                        return ProviderResponse(status, body, content_type)

                    status_headers = {
                        **base_headers,
                        "Content-Length": "0",
                        "Content-Range": f"bytes */{info.st_size}",
                    }
                    try:
                        with client.put(session_url, headers=status_headers, content=b"") as response:
                            status_body, status_content_type = self._response_body(response)
                            status_code = response.status_code
                            next_offset = self._received_offset(response.headers.get("Range"), info.st_size)
                    except Exception:
                        return ProviderResponse(0, "", None)
                    if status_code in {200, 201}:
                        return ProviderResponse(status_code, status_body, status_content_type)
                    if status_code != 308 or next_offset is None or next_offset >= info.st_size:
                        return ProviderResponse(0, "", None)
                    offset = next_offset
                return ProviderResponse(0, "", None)
        except Exception:
            # The bearer credential and request URL are never included in errors.
            return ProviderResponse(0, "", None)
        finally:
            stream.close()


class GooglePlayReleaseAdapter:
    """Upload one exact APK/AAB and commit an explicit Google Play track release."""

    provider_id = "google_play"
    _PUBLISH_SCOPE = "https://www.googleapis.com/auth/androidpublisher"

    def __init__(
        self,
        *,
        api_transport: ProviderApiTransport | None = None,
        credential_provider: Callable[[str], Mapping[str, str] | None] | None = None,
        upload_transport: GooglePlayMediaUploadTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.credential_provider = credential_provider
        self.clock = clock
        self.api_transport = api_transport or ProviderApiTransport(
            credential_provider=lambda profile: self._access_token()
            if profile == "google_play_api" else None,
        )
        self.upload_transport = upload_transport or GooglePlayMediaUploadTransport()

    def _access_token(self) -> str | None:
        if self.credential_provider is None:
            return None
        try:
            supplied = self.credential_provider("google_play")
        except Exception:
            return None
        if not isinstance(supplied, Mapping):
            return None
        token = supplied.get("access_token")
        scopes = supplied.get("scopes", supplied.get("scope"))
        if isinstance(scopes, str):
            scope_values = set(scopes.split())
        elif isinstance(scopes, (tuple, list, set)) and all(isinstance(item, str) for item in scopes):
            scope_values = set(scopes)
        else:
            return None
        if (not isinstance(token, str) or not _ACCESS_TOKEN.fullmatch(token.strip())
                or self._PUBLISH_SCOPE not in scope_values):
            return None
        return token.strip()

    @staticmethod
    def _metadata(request: ReleaseRequest) -> Mapping[str, object]:
        return request.metadata if isinstance(request.metadata, Mapping) else {}

    @staticmethod
    def _resource_id(value: object) -> str | None:
        return value if isinstance(value, str) and _EDIT_ID.fullmatch(value) else None

    @staticmethod
    def _artifact_digest(payload: Mapping[str, object], artifact_type: str) -> object:
        if artifact_type == "apk":
            binary = payload.get("binary")
            return binary.get("sha256") if isinstance(binary, Mapping) else None
        return payload.get("sha256")

    def _artifact(self, request: ReleaseRequest) -> tuple[_Artifact | None, str | None]:
        if not isinstance(request.artifact_paths, (tuple, list)) or len(request.artifact_paths) != 1:
            return None, "Exactly one APK or Android App Bundle artifact is required."
        try:
            root = Path(request.project_root).expanduser().resolve(strict=True)
            raw = Path(request.artifact_paths[0])
            relative = raw.relative_to(root) if raw.is_absolute() else raw
            if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                return None, "The Android release artifact must remain inside the workspace."
            if root != root.resolve(strict=True) or not root.is_dir():
                return None, "The Android project workspace is unavailable."
            directory_flag = getattr(os, "O_DIRECTORY", None)
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if directory_flag is None or nofollow is None or os.open not in os.supports_dir_fd:
                return None, "Safe workspace artifact access is unavailable."
            parent = os.open(root, os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0))
            parent_to_close = parent
            descriptor = -1
            try:
                for component in relative.parts[:-1]:
                    next_parent = os.open(
                        component, os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=parent_to_close,
                    )
                    if parent_to_close != parent:
                        os.close(parent_to_close)
                    parent_to_close = next_parent
                descriptor = os.open(
                    relative.parts[-1], os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_to_close,
                )
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _ARTIFACT_MAX_BYTES:
                    return None, "The Android artifact is empty, too large, or not a regular file."
                extension = Path(relative.name).suffix.casefold()
                if extension not in {".apk", ".aab"}:
                    return None, "Use one .apk or .aab Android release artifact."
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = -1
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        size += len(chunk)
                        if size > _ARTIFACT_MAX_BYTES:
                            return None, "The Android release artifact exceeds the configured size limit."
                        digest.update(chunk)
                if size != info.st_size:
                    return None, "The Android release artifact changed while it was being checked."
                return _Artifact(root / relative, "apk" if extension == ".apk" else "bundle",
                                 size, digest.hexdigest()), None
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if parent_to_close != parent:
                    os.close(parent_to_close)
                os.close(parent)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None, "The Android release artifact could not be opened safely."

    @staticmethod
    def _rollout(metadata: Mapping[str, object], track: str) -> tuple[str | None, float | None, str | None]:
        value = metadata.get("rollout_percentage")
        if track == "production":
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(float(value)) or not 1 <= float(value) <= 100):
                return None, None, "Production rollout percentage must be explicitly set from 1 through 100."
        elif value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)
                                    or not math.isfinite(float(value)) or float(value) != 100):
            return None, None, "Non-production tracks do not accept a staged production rollout percentage."
        if track != "production":
            return "completed", None, None
        percentage = float(value)
        if percentage == 100:
            return "completed", None, None
        return "inProgress", percentage / 100.0, None

    def preflight(self, request: ReleaseRequest) -> ReleasePreflight:
        metadata = self._metadata(request)
        authorization = request.authorization
        package = request.app_or_package if isinstance(request.app_or_package, str) else ""
        version = request.version if isinstance(request.version, str) else ""
        version_code = metadata.get("version_code")
        track = request.channel.casefold() if isinstance(request.channel, str) else ""
        target_valid = bool(
            request.provider_id == self.provider_id
            and getattr(authorization, "target_resolved", False) is True
            and _PACKAGE.fullmatch(package)
            and _VERSION.fullmatch(version) and isinstance(version_code, str)
            and _VERSION_CODE.fullmatch(version_code) and int(version_code) <= 2_100_000_000
            and track in _TRACKS
        )
        intent = ReleaseIntent(
            requested=getattr(authorization, "explicit_intent", False) is True,
            provider_id=self.provider_id if target_valid else None,
            app_or_package=package if target_valid else None,
            version=version if target_valid else None,
            channel=track if target_valid else None,
            blockers=[] if target_valid else [
                "Google Play target must be resolved, with a valid package, track, version, and version code."
            ],
        )
        references = getattr(authorization, "current_references", ())
        if not isinstance(references, (tuple, list)):
            references = ()
        required = metadata.get("required_attestations", ())
        completed = metadata.get("completed_attestations", ())
        rollout_status, rollout_fraction, rollout_error = self._rollout(metadata, track)
        common = build_release_preflight(
            intent,
            current_references=references,
            external_access_enabled=getattr(authorization, "external_access_enabled", False) is True,
            agreement_accepted_by_user=metadata.get("agreement_accepted_by_user") is True,
            credentials_configured=(metadata.get("credentials_configured") is True
                                    and self._access_token() is not None),
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
        artifact, artifact_error = self._artifact(request)
        if artifact_error:
            checks.append({"name": "artifact", "status": "BLOCKED", "message": artifact_error})
            blockers.append(artifact_error)
        else:
            checks.append({"name": "artifact", "status": "PASS", "message": "The Android artifact is confined and hashed."})
        if rollout_error:
            checks.append({"name": "rollout", "status": "BLOCKED", "message": rollout_error})
            blockers.append(rollout_error)
        else:
            rollout_message = ("The requested production rollout is explicitly staged."
                               if rollout_status == "inProgress" else "The full track rollout is explicitly selected.")
            checks.append({"name": "rollout", "status": "PASS", "message": rollout_message})
        if request.provider_id != self.provider_id:
            message = "Configured Google Play adapter does not match the requested provider."
            checks.append({"name": "provider", "status": "BLOCKED", "message": message})
            blockers.append(message)
        return ReleasePreflight("BLOCKED" if blockers else "READY", checks,
                                list(dict.fromkeys(blockers)))

    def _api(self, method: str, path: str, body: Mapping[str, object] | None = None) \
            -> tuple[int, Mapping[str, object] | None]:
        response = self.api_transport.request(ProviderRequest(
            "google_play_api", method, path, body=body, timeout_seconds=30,
        ))
        if not isinstance(response, ProviderResponse):
            return 0, None
        if not response.body:
            return response.status, {}
        if len(response.body) > 1024 * 1024:
            return response.status, None
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, TypeError):
            return response.status, None
        return response.status, payload if isinstance(payload, Mapping) else None

    def _insert_edit(self, package: str) -> tuple[str | None, ReleaseResult | None]:
        status, payload = self._api("POST", f"/androidpublisher/v3/applications/{quote(package, safe='')}/edits", {})
        edit = payload.get("id") if isinstance(payload, Mapping) else None
        edit_id = self._resource_id(edit)
        if status not in {200, 201} or edit_id is None:
            if status in _TRANSIENT:
                return None, ReleaseResult("UNCERTAIN", "EDIT_CREATE_UNCONFIRMED", None, None,
                                           "Google Play edit creation could not be confirmed; inspect before retrying.", True)
            return None, ReleaseResult("FAILED", "EDIT_CREATE_FAILED", None, None,
                                       "Google Play could not create an edit for this package.", False)
        return edit_id, None

    def _read_edit(self, package: str, edit_id: str) -> tuple[bool, ReleaseResult | None]:
        status, payload = self._api(
            "GET", f"/androidpublisher/v3/applications/{quote(package, safe='')}/edits/{quote(edit_id, safe='')}",
        )
        if status != 200 or not isinstance(payload, Mapping) or payload.get("id") != edit_id:
            if status in _TRANSIENT:
                return False, ReleaseResult("UNCERTAIN", "EDIT_INSPECTION_REQUIRED", None, edit_id,
                                            "The Google Play edit could not be read back; inspect before retrying.", True)
            return False, ReleaseResult("BLOCKED", "EDIT_NOT_FOUND", None, edit_id,
                                        "The Google Play edit is missing or expired.", False)
        expiry = payload.get("expiryTimeSeconds")
        try:
            expiry_value = float(expiry)
        except (TypeError, ValueError):
            return False, ReleaseResult("UNCERTAIN", "EDIT_EXPIRY_UNKNOWN", None, edit_id,
                                        "The Google Play edit expiry could not be confirmed.", True)
        if not math.isfinite(expiry_value) or expiry_value <= self.clock():
            return False, ReleaseResult("BLOCKED", "EDIT_EXPIRED", None, edit_id,
                                        "The Google Play edit has expired and must be inspected before retrying.", False)
        return True, None

    def _uploaded_artifact(self, package: str, edit_id: str, artifact: _Artifact,
                           version_code: str) -> tuple[bool, bool]:
        resource = "apks" if artifact.kind == "apk" else "bundles"
        status, payload = self._api(
            "GET", f"/androidpublisher/v3/applications/{quote(package, safe='')}/edits/"
            f"{quote(edit_id, safe='')}/{resource}",
        )
        collection_key = "apks" if artifact.kind == "apk" else "bundles"
        uploads = payload.get(collection_key) if isinstance(payload, Mapping) else None
        if status != 200 or not isinstance(uploads, list):
            return False, True
        if isinstance(payload.get("nextPageToken"), str) and payload.get("nextPageToken"):
            return False, True
        candidates = [item for item in uploads if isinstance(item, Mapping)
                      and str(item.get("versionCode")) == version_code]
        if not candidates:
            return False, False
        if len(candidates) != 1:
            return False, True
        digest = self._artifact_digest(candidates[0], artifact.kind)
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest.casefold()):
            return False, True
        if digest.casefold() != artifact.sha256:
            return False, True
        return True, False

    def _ensure_upload(self, request: ReleaseRequest, edit_id: str, artifact: _Artifact,
                       version_code: str) -> ReleaseResult | None:
        package = request.app_or_package
        found, uncertain = self._uploaded_artifact(package, edit_id, artifact, version_code)
        if found:
            return None
        if uncertain:
            return ReleaseResult("UNCERTAIN", "UPLOAD_INSPECTION_REQUIRED", None, edit_id,
                                 "The Google Play upload needs inspection before another upload can start.", True)
        token = self._access_token()
        if token is None:
            return ReleaseResult("BLOCKED", "CREDENTIALS_UNAVAILABLE", None, edit_id,
                                 "A configured Google Play OAuth access token is required.", False)
        response = self.upload_transport.upload(package, edit_id, artifact.kind, artifact.path, token)
        upload_payload = None
        if isinstance(response, ProviderResponse) and response.body and len(response.body) <= 1024 * 1024:
            try:
                decoded = json.loads(response.body)
                upload_payload = decoded if isinstance(decoded, Mapping) else None
            except (json.JSONDecodeError, TypeError):
                upload_payload = None
        if (isinstance(response, ProviderResponse) and response.status in _API_SUCCESS
                and isinstance(upload_payload, Mapping)
                and str(upload_payload.get("versionCode")) == version_code
                and isinstance(self._artifact_digest(upload_payload, artifact.kind), str)
                and self._artifact_digest(upload_payload, artifact.kind).casefold() == artifact.sha256):
            found, uncertain = self._uploaded_artifact(package, edit_id, artifact, version_code)
            if found:
                return None
            return ReleaseResult("UNCERTAIN", "UPLOAD_READBACK_UNAVAILABLE" if uncertain else "UPLOAD_READBACK_MISMATCH",
                                 None, edit_id,
                                 "Google Play did not read back the uploaded version and SHA-256 before track release.", True)
        status = response.status if isinstance(response, ProviderResponse) else 0
        if status in _TRANSIENT or status in _API_SUCCESS:
            found, uncertain = self._uploaded_artifact(package, edit_id, artifact, version_code)
            if found:
                return None
            return ReleaseResult("UNCERTAIN", "UPLOAD_RESULT_UNCONFIRMED", None, edit_id,
                                 "Google Play did not confirm the uploaded artifact; inspect before retrying.", True)
        return ReleaseResult("FAILED", "UPLOAD_REJECTED", None, edit_id,
                             "Google Play rejected the APK or Android App Bundle upload.", False)

    @staticmethod
    def _version_codes(release: Mapping[str, object]) -> set[str] | None:
        value = release.get("versionCodes")
        if not isinstance(value, list) or not value:
            return None
        result = {str(item) for item in value if isinstance(item, (int, str)) and not isinstance(item, bool)}
        return result if len(result) == len(value) else None

    def _desired_release(self, request: ReleaseRequest, rollout_status: str,
                         rollout_fraction: float | None) -> dict[str, object]:
        version_code = str(self._metadata(request)["version_code"])
        release: dict[str, object] = {
            "name": request.version,
            "versionCodes": [version_code],
            "status": rollout_status,
        }
        if rollout_fraction is not None:
            release["userFraction"] = rollout_fraction
        return release

    def _release_identity(self, request: ReleaseRequest) -> str:
        version_code = str(self._metadata(request)["version_code"])
        return f"{request.app_or_package}/{request.channel.casefold()}/{version_code}"

    def _read_track(self, package: str, edit_id: str, track: str) \
            -> tuple[Mapping[str, object] | None, ReleaseResult | None]:
        path = (f"/androidpublisher/v3/applications/{quote(package, safe='')}/edits/"
                f"{quote(edit_id, safe='')}/tracks/{quote(track, safe='')}")
        status, payload = self._api("GET", path)
        if status != 200 or not isinstance(payload, Mapping) or payload.get("track") != track:
            if status in _TRANSIENT:
                return None, ReleaseResult("UNCERTAIN", "TRACK_INSPECTION_REQUIRED", None, edit_id,
                                           "The Google Play track could not be read; inspect before retrying.", True)
            return None, ReleaseResult("BLOCKED", "TRACK_NOT_FOUND", None, edit_id,
                                       "The requested Google Play track does not exist for this app.", False)
        releases = payload.get("releases", [])
        if not isinstance(releases, list) or any(not isinstance(item, Mapping) for item in releases):
            return None, ReleaseResult("UNCERTAIN", "TRACK_RESPONSE_INVALID", None, edit_id,
                                       "Google Play returned an invalid track state.", True)
        return payload, None

    def _track_result(self, request: ReleaseRequest, edit_id: str, artifact: _Artifact,
                      track_payload: Mapping[str, object], expected: Mapping[str, object]) -> ReleaseResult:
        version_code = str(self._metadata(request)["version_code"])
        release_identity = self._release_identity(request)
        releases = track_payload.get("releases")
        matches = []
        for release in releases if isinstance(releases, list) else []:
            if not isinstance(release, Mapping):
                continue
            codes = self._version_codes(release)
            if codes is not None and version_code in codes:
                matches.append(release)
        if len(matches) != 1:
            return ReleaseResult("UNCERTAIN", "TRACK_VERSION_READBACK_MISMATCH", None, release_identity,
                                 "Google Play did not read back exactly one release with the requested version code.", True)
        remote = matches[0]
        remote_status = remote.get("status")
        if (remote_status not in {"draft", "inProgress", "halted", "completed"}
                or remote_status != expected.get("status")):
            return ReleaseResult("UNCERTAIN", "TRACK_STATUS_READBACK_MISMATCH", None, release_identity,
                                 "Google Play track status does not match the explicit release request.", True)
        if remote_status in {"inProgress", "halted"}:
            try:
                fraction = float(remote.get("userFraction"))
            except (TypeError, ValueError):
                fraction = -1
            if not math.isfinite(fraction) or fraction != expected.get("userFraction"):
                return ReleaseResult("UNCERTAIN", "ROLLOUT_READBACK_MISMATCH", None, release_identity,
                                     "Google Play did not read back the requested staged rollout fraction.", True)
        artifact_found, uncertain = self._uploaded_artifact(
            request.app_or_package, edit_id, artifact, version_code,
        )
        if not artifact_found:
            state = "UPLOAD_READBACK_UNAVAILABLE" if uncertain else "UPLOAD_READBACK_MISSING"
            return ReleaseResult("UNCERTAIN", state, None, release_identity,
                                 "The exact uploaded artifact could not be confirmed in the inspected edit.", True)
        lifecycle, lifecycle_error = self._release_lifecycle(request, version_code)
        if lifecycle_error is not None:
            return lifecycle_error
        if lifecycle == "RELEASE_LIFECYCLE_STATE_PUBLISHED":
            if remote_status == "inProgress":
                summary = "Google Play confirms the release is published and partially rolled out."
            elif remote_status == "halted":
                summary = "Google Play confirms a published release, but its rollout is halted."
            else:
                summary = "Google Play confirms the release is published on the requested track."
            return ReleaseResult("PUBLISHED", lifecycle, None, release_identity, summary, False)
        if lifecycle in {
            "RELEASE_LIFECYCLE_STATE_IN_REVIEW",
            "RELEASE_LIFECYCLE_STATE_APPROVED_NOT_PUBLISHED",
        }:
            return ReleaseResult("SUBMITTED", lifecycle, None, release_identity,
                                 "Google Play reads back the release, but review or manual publication is still pending.", False)
        if lifecycle == "RELEASE_LIFECYCLE_STATE_NOT_APPROVED":
            return ReleaseResult("BLOCKED", lifecycle, None, release_identity,
                                 "Google Play did not approve this release; user action is required before another attempt.", False)
        if lifecycle in {
            "RELEASE_LIFECYCLE_STATE_DRAFT",
            "RELEASE_LIFECYCLE_STATE_NOT_SENT_FOR_REVIEW",
        }:
            return ReleaseResult("BLOCKED", lifecycle, None, release_identity,
                                 "Google Play requires a manual release action before review can proceed.", False)
        return ReleaseResult("UNCERTAIN", "RELEASE_LIFECYCLE_UNKNOWN", None, release_identity,
                             "Google Play did not confirm a known lifecycle state for this release.", True)

    def _release_lifecycle(self, request: ReleaseRequest, version_code: str) \
            -> tuple[str | None, ReleaseResult | None]:
        package = request.app_or_package
        track = request.channel.casefold()
        path = (f"/androidpublisher/v3/applications/{quote(package, safe='')}/tracks/"
                f"{quote(track, safe='')}/releases")
        status, payload = self._api("GET", path)
        release_identity = self._release_identity(request)
        if status != 200 or not isinstance(payload, Mapping) or not isinstance(payload.get("releases"), list):
            return None, ReleaseResult("UNCERTAIN", "RELEASE_LIFECYCLE_INSPECTION_REQUIRED", None,
                                       release_identity,
                                       "Google Play release lifecycle could not be read; inspect before retrying.", True)
        matches = []
        for release in payload["releases"]:
            if not isinstance(release, Mapping) or release.get("track") != track:
                continue
            artifacts = release.get("activeArtifacts")
            if not isinstance(artifacts, list):
                continue
            codes = {
                str(item.get("versionCode")) for item in artifacts if isinstance(item, Mapping)
                and isinstance(item.get("versionCode"), (str, int))
                and not isinstance(item.get("versionCode"), bool)
            }
            if version_code in codes:
                matches.append(release)
        if len(matches) != 1:
            return None, ReleaseResult("UNCERTAIN", "RELEASE_LIFECYCLE_READBACK_MISMATCH", None,
                                       release_identity,
                                       "Google Play did not identify exactly one lifecycle record for the requested version.", True)
        lifecycle = matches[0].get("releaseLifecycleState")
        allowed = {
            "RELEASE_LIFECYCLE_STATE_DRAFT",
            "RELEASE_LIFECYCLE_STATE_NOT_SENT_FOR_REVIEW",
            "RELEASE_LIFECYCLE_STATE_IN_REVIEW",
            "RELEASE_LIFECYCLE_STATE_APPROVED_NOT_PUBLISHED",
            "RELEASE_LIFECYCLE_STATE_NOT_APPROVED",
            "RELEASE_LIFECYCLE_STATE_PUBLISHED",
        }
        if lifecycle not in allowed:
            return None, ReleaseResult("UNCERTAIN", "RELEASE_LIFECYCLE_UNKNOWN", None,
                                       release_identity,
                                       "Google Play returned an unknown release lifecycle state.", True)
        return str(lifecycle), None

    def _discard_edit(self, package: str, edit_id: str) -> None:
        try:
            self._api("DELETE", f"/androidpublisher/v3/applications/{quote(package, safe='')}/edits/"
                      f"{quote(edit_id, safe='')}")
        except Exception:
            # A temporary inspection edit expires without committing changes.
            pass

    def _inspect_live_state(self, request: ReleaseRequest, artifact: _Artifact,
                            expected: Mapping[str, object], edit_id: str | None = None) -> ReleaseResult:
        package = request.app_or_package
        temporary_edit = edit_id is None
        if edit_id is None:
            edit_id, error = self._insert_edit(package)
            if error is not None or edit_id is None:
                return error or ReleaseResult("UNCERTAIN", "INSPECTION_EDIT_UNAVAILABLE", None, None,
                                               "Google Play inspection edit could not be created.", True)
        try:
            stable_id = self._release_identity(request) if temporary_edit else edit_id
            ok, error = self._read_edit(package, edit_id)
            if not ok:
                if error is not None:
                    return ReleaseResult(error.status, error.provider_state, error.remote_url, stable_id,
                                         error.summary, error.retry_requires_inspection)
                return ReleaseResult("UNCERTAIN", "EDIT_INSPECTION_REQUIRED", None, stable_id,
                                     "The Google Play edit could not be inspected.", True)
            track, error = self._read_track(package, edit_id, request.channel.casefold())
            if error is not None or track is None:
                if error is not None:
                    return ReleaseResult(error.status, error.provider_state, error.remote_url, stable_id,
                                         error.summary, error.retry_requires_inspection)
                return ReleaseResult("UNCERTAIN", "TRACK_INSPECTION_REQUIRED", None, stable_id,
                                     "The Google Play track could not be inspected.", True)
            return self._track_result(request, edit_id, artifact, track, expected)
        finally:
            if temporary_edit:
                self._discard_edit(package, edit_id)

    def publish(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        preflight = self.preflight(request)
        if preflight.status != "READY":
            return ReleaseResult("BLOCKED", "PREFLIGHT_BLOCKED", None, None,
                                 preflight.blockers[0] if preflight.blockers else "Google Play preflight is blocked.", False)
        artifact, artifact_error = self._artifact(request)
        if artifact is None:
            return ReleaseResult("BLOCKED", "ARTIFACT_INVALID", None, None,
                                 artifact_error or "The Android release artifact is invalid.", False)
        metadata = self._metadata(request)
        version_code = str(metadata["version_code"])
        rollout_status, rollout_fraction, _ = self._rollout(metadata, request.channel.casefold())
        expected = self._desired_release(request, rollout_status or "draft", rollout_fraction)
        package = request.app_or_package
        edit_id: str | None = None
        try:
            supplied_edit_id = metadata.get("google_play_edit_id")
            if supplied_edit_id is not None:
                edit_id = self._resource_id(supplied_edit_id)
                if edit_id is None:
                    return ReleaseResult("BLOCKED", "EDIT_ID_INVALID", None, None,
                                         "The saved Google Play edit identifier is invalid.", False)
            else:
                edit_id, error = self._insert_edit(package)
                if error is not None or edit_id is None:
                    return error or ReleaseResult("UNCERTAIN", "EDIT_ID_UNAVAILABLE", None, None,
                                                   "Google Play edit id is unavailable.", True)
            ok, error = self._read_edit(package, edit_id)
            if not ok:
                return error or ReleaseResult("UNCERTAIN", "EDIT_INSPECTION_REQUIRED", None, edit_id,
                                               "The Google Play edit could not be inspected.", True)
            upload_error = self._ensure_upload(request, edit_id, artifact, version_code)
            if upload_error is not None:
                return upload_error
            track, error = self._read_track(package, edit_id, request.channel.casefold())
            if error is not None or track is None:
                return error or ReleaseResult("UNCERTAIN", "TRACK_INSPECTION_REQUIRED", None, edit_id,
                                               "The Google Play track could not be read.", True)
            releases = track.get("releases", [])
            if not isinstance(releases, list) or any(not isinstance(item, Mapping) for item in releases):
                return ReleaseResult("UNCERTAIN", "TRACK_RESPONSE_INVALID", None, edit_id,
                                     "Google Play returned an invalid release list.", True)
            updated_releases: list[Mapping[str, object]] = []
            replaced = False
            for old in releases:
                if any(key not in _RELEASE_FIELDS for key in old):
                    return ReleaseResult("BLOCKED", "TRACK_HAS_UNSUPPORTED_FIELDS", None, edit_id,
                                         "The track contains fields this adapter cannot preserve safely.", False)
                codes = self._version_codes(old)
                if codes is None:
                    return ReleaseResult("UNCERTAIN", "TRACK_VERSION_CODES_INVALID", None, edit_id,
                                         "An existing track release has invalid version codes.", True)
                if version_code in codes:
                    if len(codes) != 1:
                        return ReleaseResult("BLOCKED", "VERSION_CODE_SHARED_BY_RELEASE", None, edit_id,
                                             "The requested version code is grouped with another release version.", False)
                    if "countryTargeting" in old and expected.get("status") != "inProgress":
                        return ReleaseResult("BLOCKED", "COUNTRY_TARGETING_NEEDS_CONFIRMATION", None, edit_id,
                                             "The existing production country targeting must be reviewed before changing rollout state.", False)
                    replacement = dict(old)
                    replacement.update(expected)
                    if "userFraction" not in expected:
                        replacement.pop("userFraction", None)
                    updated_releases.append(replacement)
                    replaced = True
                else:
                    updated_releases.append(dict(old))
            if not replaced:
                if (request.channel.casefold() == "production"
                        and any("countryTargeting" in old for old in releases)):
                    return ReleaseResult("BLOCKED", "COUNTRY_TARGETING_NEEDS_EXPLICIT_TARGET", None, edit_id,
                                         "The production track has country targeting; the new release needs an explicit matching target.", False)
                updated_releases.append(expected)
            desired_track = {"track": request.channel.casefold(), "releases": updated_releases}
            update_status, _ = self._api(
                "PUT", f"/androidpublisher/v3/applications/{quote(package, safe='')}/edits/"
                f"{quote(edit_id, safe='')}/tracks/{quote(request.channel.casefold(), safe='')}",
                desired_track,
            )
            if update_status not in _API_SUCCESS:
                if update_status in _TRANSIENT:
                    inspected, inspect_error = self._read_track(package, edit_id, request.channel.casefold())
                    if inspect_error is None and inspected is not None:
                        if expected in inspected.get("releases", []):
                            pass
                        else:
                            return ReleaseResult("UNCERTAIN", "TRACK_UPDATE_UNCONFIRMED", None, edit_id,
                                                 "Track update could not be confirmed; inspect before retrying.", True)
                    else:
                        return inspect_error or ReleaseResult("UNCERTAIN", "TRACK_UPDATE_UNCONFIRMED", None,
                                                              edit_id, "Track update could not be confirmed.", True)
                else:
                    return ReleaseResult("FAILED", "TRACK_UPDATE_FAILED", None, edit_id,
                                         "Google Play rejected the requested track update.", False)
            validate_status, _ = self._api(
                "POST", f"/androidpublisher/v3/applications/{quote(package, safe='')}/edits/"
                f"{quote(edit_id, safe='')}:validate",
            )
            if validate_status not in _API_SUCCESS:
                return ReleaseResult("BLOCKED", "EDIT_VALIDATION_FAILED", None, edit_id,
                                     "Google Play did not validate the app edit; no release was committed.", False)
            commit_path = (f"/androidpublisher/v3/applications/{quote(package, safe='')}/edits/"
                           f"{quote(edit_id, safe='')}:commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW")
            commit_status, _ = self._api("POST", commit_path)
            if commit_status in _API_SUCCESS:
                return self._inspect_live_state(request, artifact, expected)
            if commit_status in _TRANSIENT:
                # A timed-out commit may already have reached Play. Read the live track first.
                result = self._inspect_live_state(request, artifact, expected)
                if result.status in {"PUBLISHED", "SUBMITTED"}:
                    return result
                return ReleaseResult("UNCERTAIN", "COMMIT_RESULT_UNCONFIRMED", None, edit_id,
                                     "Google Play commit could not be confirmed; the track was inspected before retry.", True)
            return ReleaseResult("BLOCKED", "COMMIT_BLOCKED", None, edit_id,
                                 "Google Play stopped this commit; existing review state was not canceled.", False)
        except Exception:
            return ReleaseResult("UNCERTAIN", "GOOGLE_PLAY_OPERATION_INTERRUPTED", None, edit_id,
                                 "Google Play operation was interrupted; inspect remote edit and track state before retrying.", True)

    def inspect(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        preflight = self.preflight(request)
        if preflight.status != "READY":
            return ReleaseResult("BLOCKED", "PREFLIGHT_BLOCKED", None, None,
                                 preflight.blockers[0] if preflight.blockers else "Google Play preflight is blocked.", False)
        artifact, artifact_error = self._artifact(request)
        if artifact is None:
            return ReleaseResult("BLOCKED", "ARTIFACT_INVALID", None, None,
                                 artifact_error or "The Android release artifact is invalid.", False)
        metadata = self._metadata(request)
        rollout_status, rollout_fraction, _ = self._rollout(metadata, request.channel.casefold())
        expected = self._desired_release(request, rollout_status or "draft", rollout_fraction)
        try:
            # Always use a fresh edit so an uncommitted draft cannot be reported
            # as live track state.
            return self._inspect_live_state(request, artifact, expected)
        except Exception:
            return ReleaseResult("UNCERTAIN", "GOOGLE_PLAY_INSPECTION_INTERRUPTED", None, None,
                                 "Google Play inspection was interrupted; inspect remote state before retrying.", True)


__all__ = ["GooglePlayMediaUploadTransport", "GooglePlayReleaseAdapter"]
