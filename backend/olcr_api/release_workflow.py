"""Provider-independent release preflight, operation persistence, and retry safety."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .coding_tasks import ReleaseIntent, ReleasePreflight, build_release_preflight
from .db import Database, ReleaseOperationConflict, is_valid_release_target
from .provider_research import ProviderEvidence


@dataclass(frozen=True)
class ReleaseAuthorization:
    explicit_intent: bool
    target_resolved: bool
    external_access_enabled: bool
    current_references: tuple[ProviderEvidence, ...]


@dataclass(frozen=True)
class ReleaseRequest:
    task_id: str
    provider_id: str
    project_root: Path
    app_or_package: str
    version: str
    channel: str
    artifact_paths: tuple[Path, ...]
    metadata: dict[str, object]
    authorization: ReleaseAuthorization


@dataclass(frozen=True)
class ReleaseResult:
    status: Literal["PUBLISHED", "SUBMITTED", "UNCERTAIN", "FAILED", "BLOCKED"]
    provider_state: str
    remote_url: str | None
    remote_id: str | None
    summary: str
    retry_requires_inspection: bool


class _ReleaseAdapter(Protocol):
    provider_id: str

    def preflight(self, request: ReleaseRequest) -> ReleasePreflight: ...

    def publish(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult: ...

    def inspect(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult: ...


_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
_MAX_SUMMARY_CHARS = 500
_SAFE_ERROR = re.compile(r"^[A-Z0-9_]{1,80}$")
_SAFE_PROVIDER_STATE = re.compile(r"^[A-Za-z0-9_. -]{1,120}$")
_SECRET_KEY = re.compile(r"(?:token|secret|password|credential|api.?key|authorization|private.?key)", re.I)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b[A-Za-z0-9_-]*(?:token|secret|password|credential|api[_ -]?key|authorization)[A-Za-z0-9_-]*\b\s*[:=]\s*)[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _release_identity(request: ReleaseRequest) -> tuple[str, str]:
    parts = (
        request.task_id,
        request.provider_id.casefold(),
        request.app_or_package.casefold(),
        request.version.casefold(),
    )
    key = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    operation_id = "relop_" + hashlib.sha256(f"{request.task_id}\0{request.provider_id}\0{key}".encode("utf-8")).hexdigest()
    return key, operation_id


def _artifact_hashes(request: ReleaseRequest, *, max_bytes: int) -> tuple[list[str], str | None]:
    try:
        root = request.project_root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return [], "Project workspace is missing or unavailable."
    if not root.is_dir():
        return [], "Project workspace is not a directory."
    if not request.artifact_paths:
        return [], "A local release artifact is required before publishing."
    digests: list[str] = []
    for raw_path in request.artifact_paths:
        candidate = raw_path if raw_path.is_absolute() else root / raw_path
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root):
                return [], "Release artifacts must remain inside the project workspace."
            if not resolved.is_file():
                return [], "Every release artifact must be an existing regular file."
            size = resolved.stat().st_size
            if size <= 0 or size > max_bytes:
                return [], "Release artifact is empty or exceeds the configured size limit."
            digest = hashlib.sha256()
            with resolved.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            digests.append(digest.hexdigest())
        except (OSError, RuntimeError):
            return [], "A release artifact could not be read safely."
    return sorted(digests), None


def _stored_reference_ids(database: Database, request: ReleaseRequest) -> list[str]:
    wanted = {
        (reference.provider_id, reference.category, reference.url, float(reference.accessed_at))
        for reference in request.authorization.current_references
        if reference.task_id == request.task_id
        and reference.provider_id == request.provider_id
        and reference.officiality == "VERIFIED"
        and reference.category in {"publishing", "terms"}
    }
    found: set[str] = set()
    for row in database.coding_task_references(request.task_id):
        try:
            accessed_at = float(row.get("accessed_at"))
        except (TypeError, ValueError):
            continue
        if (row.get("officiality") == "VERIFIED"
                and (row.get("provider_id"), row.get("category"), row.get("url"), accessed_at) in wanted):
            value = row.get("id")
            if isinstance(value, str):
                found.add(value)
    return sorted(found)


def _safe_summary(summary: object, metadata: Mapping[str, object]) -> str:
    text = str(summary or "")
    for key, value in metadata.items():
        if _SECRET_KEY.search(str(key)) and isinstance(value, str) and len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
    text = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _CONTROL.sub(" ", text)
    return " ".join(text.split())[:_MAX_SUMMARY_CHARS]


def _safe_state(value: object) -> str:
    text = str(value or "")
    return text[:120] if _SAFE_PROVIDER_STATE.fullmatch(text) and not _SECRET_KEY.search(text) else "UNKNOWN"


def _safe_remote_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            return None
    except ValueError:
        return None
    return value


def _safe_remote_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        return None
    if (_CONTROL.search(value) or _SECRET_KEY.search(value)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value)):
        return None
    return value.strip()


def _blocked(summary: str, *, provider_state: str = "BLOCKED") -> ReleaseResult:
    return ReleaseResult("BLOCKED", provider_state, None, None, summary[:_MAX_SUMMARY_CHARS], False)


class ReleaseCoordinator:
    """Applies common release gates and persists before and after provider operations."""

    def __init__(
        self,
        database: Database,
        adapter: _ReleaseAdapter,
        *,
        clock: Callable[[], float] = time.time,
        max_artifact_bytes: int = _MAX_ARTIFACT_BYTES,
        request_resolver: Callable[[dict[str, Any]], ReleaseRequest | None] | None = None,
    ) -> None:
        self.database = database
        self.adapter = adapter
        self.clock = clock
        self.max_artifact_bytes = max_artifact_bytes
        self.request_resolver = request_resolver
        self._requests: dict[str, ReleaseRequest] = {}

    def _intent(self, request: ReleaseRequest) -> ReleaseIntent:
        resolved = bool(
            request.authorization.target_resolved
            and request.provider_id.strip()
            and is_valid_release_target(request.app_or_package)
            and request.version.strip()
            and request.channel.strip()
        )
        blockers = [] if resolved else ["Provider, target, version, and channel are not fully resolved."]
        return ReleaseIntent(
            requested=bool(request.authorization.explicit_intent),
            provider_id=request.provider_id if resolved else None,
            app_or_package=request.app_or_package if resolved else None,
            version=request.version if resolved else None,
            channel=request.channel if resolved else None,
            blockers=blockers,
        )

    def _common_preflight(self, request: ReleaseRequest, artifact_error: str | None = None) -> ReleasePreflight:
        metadata = request.metadata if isinstance(request.metadata, Mapping) else {}
        base = build_release_preflight(
            self._intent(request),
            current_references=request.authorization.current_references,
            external_access_enabled=request.authorization.external_access_enabled,
            agreement_accepted_by_user=metadata.get("agreement_accepted_by_user") is True,
            credentials_configured=metadata.get("credentials_configured") is True,
            local_build_validated=metadata.get("local_build_validated") is True,
            paid_plan_choice_required=metadata.get("paid_plan_choice_required") is True,
            paid_plan_selected_by_user=metadata.get("paid_plan_selected_by_user") is True,
            required_attestations=(
                [value for value in metadata.get("required_attestations", []) if isinstance(value, str)]
                if isinstance(metadata.get("required_attestations", []), (list, tuple)) else []
            ),
            completed_attestations=(
                [value for value in metadata.get("completed_attestations", []) if isinstance(value, str)]
                if isinstance(metadata.get("completed_attestations", []), (list, tuple)) else []
            ),
            now=self.clock(),
        )
        checks = list(base.checks)
        blockers = list(base.blockers)
        if artifact_error:
            checks.append({"name": "artifacts", "status": "BLOCKED", "message": artifact_error})
            blockers.append(artifact_error)
        else:
            checks.append({"name": "artifacts", "status": "PASS", "message": "Workspace artifacts are present and hashed."})
        if self.adapter.provider_id != request.provider_id:
            message = "Configured release adapter does not match the requested provider."
            checks.append({"name": "provider_adapter", "status": "BLOCKED", "message": message})
            blockers.append(message)

        reference_ids = _stored_reference_ids(self.database, request)
        if not reference_ids:
            message = "Current verified provider evidence must be saved before creating a release operation."
            checks.append({"name": "persisted_evidence", "status": "BLOCKED", "message": message})
            blockers.append(message)
        else:
            checks.append({"name": "persisted_evidence", "status": "PASS", "message": "Current evidence is linked by reference id."})

        if blockers:
            return ReleasePreflight("BLOCKED", checks, list(dict.fromkeys(blockers)))
        try:
            adapter_result = self.adapter.preflight(request)
        except Exception:
            message = "Provider adapter preflight failed safely."
            checks.append({"name": "provider_adapter", "status": "BLOCKED", "message": message})
            blockers.append(message)
            return ReleasePreflight("BLOCKED", checks, blockers)
        if not isinstance(adapter_result, ReleasePreflight):
            message = "Provider adapter returned an invalid preflight result."
            checks.append({"name": "provider_adapter", "status": "BLOCKED", "message": message})
            return ReleasePreflight("BLOCKED", checks, [message])
        checks.extend(adapter_result.checks)
        blockers.extend(adapter_result.blockers)
        if adapter_result.status != "READY" and not adapter_result.blockers:
            blockers.append("Provider adapter is not ready to publish.")
        return ReleasePreflight(
            "BLOCKED" if blockers else "READY",
            checks,
            list(dict.fromkeys(blockers)),
        )

    def preflight(self, request: ReleaseRequest) -> ReleasePreflight:
        _, artifact_error = _artifact_hashes(request, max_bytes=self.max_artifact_bytes)
        return self._common_preflight(request, artifact_error)

    @staticmethod
    def _result_from_operation(operation: Mapping[str, Any]) -> ReleaseResult:
        state = str(operation.get("state") or "BLOCKED")
        status = state if state in {"PUBLISHED", "SUBMITTED", "UNCERTAIN", "FAILED", "BLOCKED"} else "BLOCKED"
        return ReleaseResult(
            status,  # type: ignore[arg-type]
            str(operation.get("provider_state") or state),
            operation.get("remote_url") if isinstance(operation.get("remote_url"), str) else None,
            operation.get("remote_id") if isinstance(operation.get("remote_id"), str) else None,
            str(operation.get("summary") or ""),
            state == "UNCERTAIN",
        )

    @staticmethod
    def _publish_in_progress() -> ReleaseResult:
        return ReleaseResult(
            "UNCERTAIN",
            "PUBLISH_IN_PROGRESS",
            None,
            None,
            "A provider release operation is already in progress; inspect remote state before retrying.",
            True,
        )

    def _operation(self, request: ReleaseRequest, hashes: list[str], state: str) -> dict[str, Any]:
        key, operation_id = _release_identity(request)
        reference_ids = _stored_reference_ids(self.database, request)
        operation, _created = self.database.create_release_operation(
            operation_id=operation_id,
            task_id=request.task_id,
            provider_id=request.provider_id,
            idempotency_key=key,
            target=request.app_or_package,
            version=request.version,
            channel=request.channel,
            artifact_hashes=hashes,
            reference_ids=reference_ids,
            state=state,
            now=self.clock(),
        )
        self._requests[str(operation["operation_id"])] = request
        return operation

    def _persist_result(
        self,
        operation_id: str,
        result: ReleaseResult,
        request: ReleaseRequest,
        *,
        inspected: bool,
        previous_state: str,
    ) -> ReleaseResult:
        allowed = {"PUBLISHED", "SUBMITTED", "UNCERTAIN", "FAILED", "BLOCKED"}
        status = result.status if result.status in allowed else "FAILED"
        if status == "PUBLISHED" and not inspected:
            status = "UNCERTAIN"
        provider_state = _safe_state(result.provider_state)
        remote_url = _safe_remote_url(result.remote_url)
        remote_id = _safe_remote_id(result.remote_id)
        if status == "PUBLISHED" and not (remote_url or remote_id):
            status = "UNCERTAIN"
            provider_state = "READBACK_INCOMPLETE"
        summary = _safe_summary(result.summary, request.metadata)
        safe_error = None
        if status in {"FAILED", "BLOCKED"}:
            safe_error = result.provider_state if _SAFE_ERROR.fullmatch(result.provider_state or "") else "PROVIDER_OPERATION_FAILED"

        persist_state = status
        inspection_absent = False
        if inspected and provider_state == "NOT_FOUND" and previous_state in {"UNCERTAIN", "PUBLISHING"}:
            persist_state = "READY"
            inspection_absent = True
            summary = summary or "Provider inspection confirmed that no release exists; a later publish attempt can be evaluated."
        self.database.update_release_operation(
            operation_id,
            state=persist_state,
            now=self.clock(),
            provider_state=provider_state,
            remote_url=remote_url,
            remote_id=remote_id,
            safe_error_code=safe_error,
            summary=summary,
            inspection_confirmed=(status == "PUBLISHED" and inspected),
            inspection_confirmed_absent=inspection_absent,
        )
        return ReleaseResult(
            status,  # type: ignore[arg-type]
            provider_state,
            remote_url,
            remote_id,
            summary,
            status == "UNCERTAIN" or bool(result.retry_requires_inspection),
        )

    def _load_request(self, operation: dict[str, Any]) -> ReleaseRequest | None:
        operation_id = str(operation.get("operation_id") or "")
        request = self._requests.get(operation_id)
        if request is not None:
            return request
        if self.request_resolver is None:
            return None
        try:
            request = self.request_resolver(operation)
        except Exception:
            return None
        if request is not None:
            self._requests[operation_id] = request
        return request

    def _inspect(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        operation = self.database.release_operation(operation_id)
        if operation is None:
            return _blocked("Release operation was not found.", provider_state="OPERATION_MISSING")
        self._requests[operation_id] = request
        previous_state = str(operation.get("state") or "BLOCKED")
        if not request.authorization.external_access_enabled:
            return _blocked("External Access is disabled; remote state inspection was skipped.", provider_state="EXTERNAL_ACCESS_OFF")
        if self.adapter.provider_id != request.provider_id:
            return _blocked("Configured release adapter does not match the stored operation.", provider_state="ADAPTER_MISMATCH")
        try:
            result = self.adapter.inspect(request, operation_id)
        except Exception:
            result = ReleaseResult("UNCERTAIN", "INSPECT_FAILED", None, None,
                                   "Provider state could not be confirmed safely.", True)
        return self._persist_result(
            operation_id, result, request, inspected=True, previous_state=previous_state
        )

    def inspect(self, operation_id: str) -> ReleaseResult:
        operation = self.database.release_operation(operation_id)
        if operation is None:
            return _blocked("Release operation was not found.", provider_state="OPERATION_MISSING")
        request = self._load_request(operation)
        if request is None:
            return _blocked(
                "A safe request resolver is required to inspect this persisted operation.",
                provider_state="REQUEST_RESTORE_REQUIRED",
            )
        return self._inspect(request, operation_id)

    def publish(self, request: ReleaseRequest) -> ReleaseResult:
        if not request.task_id.strip() or not request.provider_id.strip():
            return _blocked("Release task and provider are required.", provider_state="INVALID_TARGET")
        if not is_valid_release_target(request.app_or_package):
            return _blocked("Release target identifier is invalid.", provider_state="INVALID_TARGET")
        hashes, artifact_error = _artifact_hashes(request, max_bytes=self.max_artifact_bytes)
        if artifact_error:
            return _blocked(artifact_error, provider_state="ARTIFACT_INVALID")

        _, expected_operation_id = _release_identity(request)
        existing = self.database.release_operation(expected_operation_id)
        if existing is not None:
            if (existing.get("target") != request.app_or_package[:180]
                    or existing.get("version") != request.version[:80]
                    or existing.get("channel") != request.channel[:80]
                    or existing.get("artifact_hashes") != hashes):
                return _blocked(
                    "This task, provider, target, and version already have an operation for different artifact bytes.",
                    provider_state="IDEMPOTENCY_CONFLICT",
                )
            self._requests[expected_operation_id] = request
            existing_state = str(existing.get("state") or "BLOCKED")
            if existing_state == "PUBLISHED":
                return self._result_from_operation(existing)
            if existing_state == "PUBLISHING":
                return self._publish_in_progress()
            if existing_state in {"UNCERTAIN", "SUBMITTED"}:
                inspected = self._inspect(request, expected_operation_id)
                current = self.database.release_operation(expected_operation_id)
                if current is None or current.get("state") != "READY":
                    return inspected

        preflight = self._common_preflight(request)
        initial_state = "READY" if preflight.status == "READY" else "BLOCKED"
        try:
            operation = self._operation(request, hashes, initial_state)
        except ReleaseOperationConflict:
            return _blocked(
                "This task, provider, target, and version already have an operation for different artifact bytes.",
                provider_state="IDEMPOTENCY_CONFLICT",
            )
        operation_id = str(operation["operation_id"])
        state = str(operation.get("state") or "BLOCKED")

        if state == "PUBLISHED":
            return self._result_from_operation(operation)
        if state == "PUBLISHING":
            return self._publish_in_progress()

        if preflight.status != "READY":
            if state != "BLOCKED":
                self.database.update_release_operation(
                    operation_id, state="BLOCKED", now=self.clock(),
                    safe_error_code="PREFLIGHT_BLOCKED", summary="; ".join(preflight.blockers),
                )
            return _blocked("; ".join(preflight.blockers), provider_state="PREFLIGHT_BLOCKED")

        if state in {"UNCERTAIN", "SUBMITTED"}:
            inspected = self._inspect(request, operation_id)
            current = self.database.release_operation(operation_id)
            if current is None:
                return _blocked("Release operation was not found.", provider_state="OPERATION_MISSING")
            state = str(current.get("state") or "BLOCKED")
            if state != "READY":
                return inspected

        if state in {"BLOCKED", "FAILED"}:
            self.database.update_release_operation(
                operation_id, state="READY", now=self.clock(), summary="Release preflight is ready."
            )
        current = self.database.release_operation(operation_id)
        if current is None:
            return _blocked("Release operation was not found.", provider_state="OPERATION_MISSING")
        if current["state"] != "READY":
            return self._result_from_operation(current)
        claimed = self.database.claim_release_operation(operation_id, now=self.clock())
        if claimed is None:
            current = self.database.release_operation(operation_id)
            if current is None:
                return _blocked("Release operation was not found.", provider_state="OPERATION_MISSING")
            if current.get("state") == "PUBLISHING":
                return self._publish_in_progress()
            if current.get("state") == "PUBLISHED":
                return self._result_from_operation(current)
            return self._result_from_operation(current)
        try:
            result = self.adapter.publish(request, operation_id)
        except Exception:
            result = ReleaseResult(
                "UNCERTAIN", "PROVIDER_EXCEPTION", None, None,
                "Provider response was interrupted; inspect remote state before retrying.", True,
            )
        if result.status == "UNCERTAIN":
            return self._persist_result(
                operation_id, result, request, inspected=False, previous_state="PUBLISHING"
            )
        if result.status in {"PUBLISHED", "SUBMITTED"}:
            # A provider response is not proof of remote state; confirm by read-back.
            try:
                read_back = self.adapter.inspect(request, operation_id)
            except Exception:
                read_back = ReleaseResult(
                    "UNCERTAIN", "READBACK_FAILED", result.remote_url, result.remote_id,
                    "Provider state could not be confirmed after the publish response.", True,
                )
            return self._persist_result(
                operation_id, read_back, request, inspected=True, previous_state="PUBLISHING"
            )
        return self._persist_result(
            operation_id, result, request, inspected=False, previous_state="PUBLISHING"
        )
