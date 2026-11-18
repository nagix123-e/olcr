"""Fail-closed Cloudflare Pages Direct Upload adapter."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import stat
import secrets
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

from ..coding_tasks import ReleasePreflight
from ..release_workflow import ReleaseRequest, ReleaseResult
from .base import ProviderApiTransport, ProviderRequest, ProviderResponse, ReleaseCommandRunner
from .web_common import (
    MAX_FILES,
    MAX_TOTAL_BYTES,
    WebAsset,
    add_check,
    base_preflight,
    deployment_tree,
    preflight_result,
    same_tree,
    workspace_path,
)


_ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")
_PROJECT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.I)
_DOMAIN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)
_OPERATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_LIST_PAGES = 20
_MAX_JSON_BYTES = 1024 * 1024
_MAX_API_REQUEST_BYTES = 40 * 1024 * 1024
_MAX_ASSET_UPLOAD_BATCH_BYTES = 38 * 1024 * 1024
_MAX_ASSET_UPLOAD_BATCH_FILES = 2_000
_MAX_FILES = MAX_FILES
_MAX_FILE_BYTES = 25 * 1024 * 1024
_MAX_TOTAL_BYTES = MAX_TOTAL_BYTES


class CloudflarePagesReleaseAdapter:
    """Deploy an exact local build directory to one existing Pages project."""

    provider_id = "cloudflare_pages"

    def __init__(self, *, runner: ReleaseCommandRunner | None = None,
                 api_transport: ProviderApiTransport | None = None,
                 credential_provider: Callable[[str], Mapping[str, str] | None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.credential_provider = credential_provider
        self.runner = runner or ReleaseCommandRunner(credential_provider=credential_provider)
        self._credential_source = credential_provider or getattr(self.runner, "credential_provider", None)
        self.api_transport = api_transport or ProviderApiTransport(
            credential_provider=lambda profile: self._api_token(profile),
            max_request_bytes=_MAX_API_REQUEST_BYTES,
        )
        self.clock = clock

    def _credentials(self) -> Mapping[str, str] | None:
        if self._credential_source is None:
            return None
        try:
            value = self._credential_source("cloudflare_pages")
            return value if isinstance(value, Mapping) else None
        except Exception:
            return None

    def _api_token(self, profile: str) -> str | None:
        if profile != "cloudflare_pages_api":
            return None
        credentials = self._credentials()
        token = credentials.get("CLOUDFLARE_API_TOKEN") if credentials else None
        return token if isinstance(token, str) and token.strip() else None

    def _has_credentials(self, account_id: str | None) -> bool:
        credentials = self._credentials()
        token = credentials.get("CLOUDFLARE_API_TOKEN") if credentials else None
        profile_account = credentials.get("CLOUDFLARE_ACCOUNT_ID") if credentials else None
        return bool(
            isinstance(token, str) and 16 <= len(token) <= 4096
            and not any(ord(char) < 32 for char in token)
            and isinstance(profile_account, str) and _ACCOUNT_ID.fullmatch(profile_account)
            and isinstance(account_id, str) and profile_account == account_id
        )

    @staticmethod
    def _metadata(request: ReleaseRequest) -> Mapping[str, object]:
        return request.metadata if isinstance(request.metadata, Mapping) else {}

    def _target_error(self, request: ReleaseRequest) -> str | None:
        metadata = self._metadata(request)
        account_id = metadata.get("account_id")
        project_name = metadata.get("project_name")
        environment = metadata.get("environment")
        branch = metadata.get("branch")
        commit = metadata.get("build_commit")
        action = metadata.get("release_action")
        if (not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id)
                or not isinstance(project_name, str) or not _PROJECT_NAME.fullmatch(project_name)
                or request.app_or_package != project_name):
            return "Cloudflare account and exact existing Pages project must be explicitly resolved."
        if request.channel not in {"preview", "production"} or environment != request.channel:
            return "Cloudflare Pages environment must be explicitly preview or production and match the release channel."
        if (not isinstance(branch, str) or not _BRANCH.fullmatch(branch)
                or ".." in branch.split("/") or "//" in branch or branch.endswith("/")):
            return "Cloudflare Pages requires an exact safe branch name."
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            return "A full validated Git commit is required for the Pages deployment."
        if metadata.get("upload_mode") != "direct_upload" or action != "deploy":
            return "Only an explicit direct-upload deployment action is supported for Cloudflare Pages."
        if request.channel == "production":
            target_domain = metadata.get("target_domain")
            if (metadata.get("production_authorized") is not True
                    or not isinstance(target_domain, str) or not _DOMAIN.fullmatch(target_domain)):
                return "Production Pages deployment requires explicit authorization and an exact target domain."
        return None

    def _asset_tree(self, request: ReleaseRequest):
        metadata = self._metadata(request)
        return deployment_tree(
            request,
            metadata.get("deployment_directory"),
            max_files=_MAX_FILES,
            max_file_bytes=_MAX_FILE_BYTES,
            max_total_bytes=_MAX_TOTAL_BYTES,
        )

    @staticmethod
    def _unsupported_direct_upload_file(
        assets: tuple[WebAsset, ...], directory: Path | None,
    ) -> str | None:
        if directory is None:
            return None
        control_files = {
            "_headers", "_redirects", "_routes.json", "_worker.js", "_worker.bundle",
            "functions-filepath-routing-config.json",
        }
        for asset in assets:
            try:
                relative = asset.path.relative_to(directory)
            except (OSError, ValueError):
                return "A Cloudflare Pages deployment file is outside the verified output directory."
            parts = relative.parts
            if not parts:
                return "A Cloudflare Pages deployment file has an invalid relative path."
            if parts[0] in {"functions", ".wrangler"} or (
                len(parts) == 1 and parts[0] in control_files
            ):
                return (
                    "This Cloudflare Pages output contains control files or Functions inputs unsupported "
                    "by the static Direct Upload adapter."
                )
            if any(part in {".DS_Store", "node_modules", ".git"} for part in parts):
                return (
                    "This Cloudflare Pages output contains a path unsupported by the Direct Upload adapter "
                    "because Wrangler excludes it; remove it before deployment."
                )
        return None

    @staticmethod
    def _read_asset_bytes(asset: WebAsset, root: Path) -> bytes | None:
        parts = asset.relative_path.parts
        if (not parts or any(part in {"", ".", ".."} or "/" in part or "\\" in part for part in parts)
                or not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0)):
            return None
        directory_fd = file_fd = -1
        try:
            directory_fd = os.open(
                root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            )
            for component in parts[:-1]:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = child_fd
            file_fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory_fd,
            )
            opened = os.fstat(file_fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != asset.size:
                return None
            digest = hashlib.sha256()
            content = bytearray()
            with os.fdopen(file_fd, "rb") as stream:
                file_fd = -1
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    content.extend(chunk)
                    digest.update(chunk)
                    if len(content) > asset.size:
                        return None
                after = os.fstat(stream.fileno())
            if (len(content) != asset.size or digest.hexdigest() != asset.sha256
                    or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                return None
            return bytes(content)
        except (OSError, RuntimeError):
            return None
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            if directory_fd >= 0:
                os.close(directory_fd)

    @staticmethod
    def _pages_asset_hash(content: bytes, relative_path: Path) -> str:
        try:
            from blake3 import blake3
        except ImportError as exc:
            raise RuntimeError("BLAKE3_UNAVAILABLE") from exc
        extension = relative_path.suffix[1:]
        input_value = base64.b64encode(content).decode("ascii") + extension
        return blake3(input_value.encode("utf-8")).hexdigest()[:32]

    def _asset_manifest(self, assets: tuple[WebAsset, ...], workspace_root: Path, deployment_directory: Path):
        manifest: dict[str, str] = {}
        assets_by_hash: dict[str, WebAsset] = {}
        for asset in assets:
            content = self._read_asset_bytes(asset, workspace_root)
            if content is None:
                return None, None, "A Cloudflare deployment asset changed after local verification."
            try:
                digest = self._pages_asset_hash(content, asset.relative_path)
            except RuntimeError:
                return None, None, "The Cloudflare Pages BLAKE3 upload dependency is unavailable."
            try:
                relative = asset.path.relative_to(deployment_directory)
            except (OSError, ValueError):
                return None, None, "A Cloudflare deployment asset is outside the verified output directory."
            if not relative.parts or any(part in {".", ".."} for part in relative.parts):
                return None, None, "A Cloudflare deployment asset path is invalid."
            manifest_path = "/" + relative.as_posix()
            manifest[manifest_path] = digest
            assets_by_hash.setdefault(digest, asset)
        if not manifest or len(manifest) > _MAX_FILES:
            return None, None, "The Cloudflare deployment manifest is empty or exceeds the file limit."
        return manifest, assets_by_hash, None

    @staticmethod
    def _project_path(account_id: str, project_name: str) -> str:
        return (f"/client/v4/accounts/{quote(account_id, safe='')}/pages/projects/"
                f"{quote(project_name, safe='')}")

    def _api_json(self, spec: ProviderRequest):
        try:
            response = self.api_transport.request(spec)
        except Exception:
            return "error", None
        if (not isinstance(response, ProviderResponse) or response.status != 200
                or not isinstance(response.body, str) or len(response.body) > _MAX_JSON_BYTES):
            return "error", None
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, TypeError):
            return "error", None
        return ("found", payload) if isinstance(payload, Mapping) else ("error", None)

    def _upload_token(self, request: ReleaseRequest) -> str | None:
        metadata = self._metadata(request)
        account_id, project_name = metadata.get("account_id"), metadata.get("project_name")
        if not isinstance(account_id, str) or not isinstance(project_name, str):
            return None
        status, payload = self._api_json(ProviderRequest(
            "cloudflare_pages_api", "GET",
            self._project_path(account_id, project_name) + "/upload-token", timeout_seconds=20,
        ))
        result = payload.get("result") if isinstance(payload, Mapping) else None
        token = result.get("jwt") if isinstance(result, Mapping) else None
        if (status != "found" or payload.get("success") is not True
                or not isinstance(token, str) or not token.strip() or len(token) > 8192
                or any(ord(char) < 32 for char in token)):
            return None
        return token.strip()

    @staticmethod
    def _asset_json_request(path: str, body: bytes | Mapping[str, object], token: str) -> ProviderRequest:
        return ProviderRequest(
            "cloudflare_pages_assets", "POST", path, body=body, timeout_seconds=30, auth_token=token,
        )

    def _missing_asset_hashes(self, hashes: list[str], token: str) -> list[str] | None:
        status, payload = self._api_json(self._asset_json_request(
            "/client/v4/pages/assets/check-missing", {"hashes": hashes}, token,
        ))
        missing = payload.get("result") if isinstance(payload, Mapping) else None
        if (status != "found" or payload.get("success") is not True or not isinstance(missing, list)
                or not all(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{32}", item) for item in missing)
                or len(set(missing)) != len(missing) or not set(missing).issubset(hashes)):
            return None
        return missing

    def _upload_asset_batch(self, batch: list[dict[str, object]], token: str) -> bool:
        body = json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > _MAX_ASSET_UPLOAD_BATCH_BYTES:
            return False
        status, payload = self._api_json(self._asset_json_request(
            "/client/v4/pages/assets/upload", body, token,
        ))
        errors = payload.get("errors") if isinstance(payload, Mapping) else None
        return status == "found" and payload.get("success") is True and errors in (None, [])

    def _upload_missing_assets(self, missing: list[str], assets_by_hash: Mapping[str, WebAsset],
                               workspace_root: Path, token: str) -> bool:
        batch: list[dict[str, object]] = []

        def upload_pending() -> bool:
            nonlocal batch
            if not batch:
                return True
            okay = self._upload_asset_batch(batch, token)
            batch = []
            return okay

        for digest in missing:
            asset = assets_by_hash.get(digest)
            if asset is None:
                return False
            content = self._read_asset_bytes(asset, workspace_root)
            if content is None:
                return False
            content_type = mimetypes.guess_type(asset.path.name)[0] or "application/octet-stream"
            item = {
                "base64": True,
                "key": digest,
                "metadata": {"contentType": content_type},
                "value": base64.b64encode(content).decode("ascii"),
            }
            candidate = [*batch, item]
            candidate_bytes = len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if len(candidate) > _MAX_ASSET_UPLOAD_BATCH_FILES or candidate_bytes > _MAX_ASSET_UPLOAD_BATCH_BYTES:
                if not upload_pending():
                    return False
                candidate = [item]
                candidate_bytes = len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if candidate_bytes > _MAX_ASSET_UPLOAD_BATCH_BYTES:
                return False
            batch = candidate
        return upload_pending()

    def _upsert_asset_hashes(self, hashes: list[str], token: str) -> bool:
        status, payload = self._api_json(self._asset_json_request(
            "/client/v4/pages/assets/upsert-hashes", {"hashes": hashes}, token,
        ))
        errors = payload.get("errors") if isinstance(payload, Mapping) else None
        return status == "found" and payload.get("success") is True and errors in (None, [])

    @staticmethod
    def _multipart_form(fields: Mapping[str, str]) -> tuple[str, bytes]:
        boundary = "olcr-" + secrets.token_hex(16)
        body = bytearray()
        for name, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode("ascii"))
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("ascii"))
        return boundary, bytes(body)

    def _read_project(self, request: ReleaseRequest):
        metadata = self._metadata(request)
        account_id, project_name = metadata.get("account_id"), metadata.get("project_name")
        if not isinstance(account_id, str) or not isinstance(project_name, str):
            return "invalid", None
        status, payload = self._api_json(ProviderRequest(
            "cloudflare_pages_api", "GET", self._project_path(account_id, project_name), timeout_seconds=20,
        ))
        if status != "found" or payload.get("success") is not True:
            return status, None
        project = payload.get("result")
        if (not isinstance(project, Mapping) or project.get("name") != project_name
                or not isinstance(project.get("production_branch"), str)
                or not project.get("production_branch")):
            return "conflict", None
        source = project.get("source")
        if isinstance(source, Mapping) and source.get("type") not in {None, "direct_upload"}:
            return "conflict", None
        return "found", project

    def _project_target_error(self, request: ReleaseRequest, project: Mapping[str, object]) -> str | None:
        metadata = self._metadata(request)
        branch = metadata.get("branch")
        production_branch = project.get("production_branch")
        if not isinstance(branch, str) or not isinstance(production_branch, str):
            return "Cloudflare's existing production branch could not be confirmed."
        if request.channel == "production":
            if branch != production_branch:
                return "The requested Cloudflare production branch no longer matches the configured production branch."
            target_domain = metadata.get("target_domain")
            domains = project.get("domains")
            if not isinstance(target_domain, str) or not isinstance(domains, list):
                return "Cloudflare's production target domain list could not be confirmed."
            subdomain = project.get("subdomain")
            if (not isinstance(subdomain, str) or not _DOMAIN.fullmatch(subdomain)
                    or not subdomain.casefold().endswith(".pages.dev")):
                return "Cloudflare's generated pages.dev subdomain could not be confirmed for production scope."
            configured = set()
            for item in domains:
                candidate = item.get("name", item.get("domain")) if isinstance(item, Mapping) else item
                if not isinstance(candidate, str) or not _DOMAIN.fullmatch(candidate):
                    return "A Cloudflare custom domain could not be verified for production scope."
                configured.add(candidate.casefold())
            configured.add(subdomain.casefold())
            if target_domain.casefold() not in configured:
                return "The requested Cloudflare production domain is not attached to the existing project."
            authorized = metadata.get("authorized_production_domains")
            if len(configured) > 1 or authorized is not None:
                if (not isinstance(authorized, (tuple, list))
                        or not all(isinstance(name, str) and _DOMAIN.fullmatch(name) for name in authorized)):
                    return (
                        "Cloudflare production requires explicit authorization for every attached custom domain "
                        "and the generated pages.dev subdomain."
                    )
                authorized_names = {name.casefold() for name in authorized}
                if len(authorized_names) != len(authorized) or authorized_names != configured:
                    return (
                        "Cloudflare production authorization must exactly match every attached custom domain "
                        "and the generated pages.dev subdomain."
                    )
                return None
            return None
        if branch == production_branch:
            return "The requested Cloudflare preview branch matches the configured production branch."
        return None

    def _project_matches_target(self, request: ReleaseRequest, project: Mapping[str, object]) -> bool:
        return self._project_target_error(request, project) is None

    def preflight(self, request: ReleaseRequest) -> ReleasePreflight:
        target_error = self._target_error(request)
        metadata = self._metadata(request)
        account_id = metadata.get("account_id") if isinstance(metadata.get("account_id"), str) else None
        credentials = self._has_credentials(account_id)
        assets, directory, asset_error = self._asset_tree(request)
        checks, blockers = base_preflight(
            request, self.provider_id,
            credentials_configured=credentials,
            local_build_validated=metadata.get("local_build_validated") is True and asset_error is None,
            now=self.clock(),
        )
        add_check(checks, blockers, "cloudflare_target",
                  target_error or "The Cloudflare account, project, environment, branch, and direct-upload action are explicit.",
                  passed=target_error is None)
        add_check(checks, blockers, "deployment_assets",
                  asset_error or "Every declared deployment file matches the workspace-confined output directory.",
                  passed=asset_error is None)
        unsupported_file_error = self._unsupported_direct_upload_file(assets, directory)
        add_check(checks, blockers, "direct_upload_supported_files",
                  unsupported_file_error or
                  "The output contains only static assets supported by the Cloudflare Pages Direct Upload adapter.",
                  passed=unsupported_file_error is None)
        add_check(checks, blockers, "credential_profile",
                  "The configured Cloudflare token and account id match the requested account." if credentials else
                  "A configured Cloudflare Pages token for the exact selected account is required.",
                  passed=credentials)
        if blockers:
            return preflight_result(checks, blockers)

        project_state, project = self._read_project(request)
        project_target_error = (
            self._project_target_error(request, project)
            if project_state == "found" and isinstance(project, Mapping) else None
        )
        target_matches = project_target_error is None and project_state == "found" and project is not None
        add_check(checks, blockers, "existing_project",
                  "The exact existing Cloudflare Pages project is reachable." if project_state == "found" else
                  "The requested Cloudflare Pages project could not be confirmed; project creation is unsupported.",
                  passed=project_state == "found")
        add_check(checks, blockers, "production_branch",
                  "The requested branch and production domain scope match the existing Pages project configuration." if target_matches else
                  project_target_error or "The requested branch or production domain does not match the existing Pages project configuration.",
                  passed=target_matches)
        if blockers:
            return preflight_result(checks, blockers)

        add_check(checks, blockers, "pages_api_direct_upload",
                  "The existing project and project-scoped Direct Upload API are available.",
                  passed=True)
        return preflight_result(checks, blockers)

    def _read_deployments(self, request: ReleaseRequest, *, environment: str):
        metadata = self._metadata(request)
        account_id, project_name = metadata.get("account_id"), metadata.get("project_name")
        if not isinstance(account_id, str) or not isinstance(project_name, str):
            return "error", ()
        rows: list[Mapping[str, object]] = []
        expected_pages = None
        for page in range(1, _MAX_LIST_PAGES + 1):
            query = urlencode({"env": environment, "page": page, "per_page": 100})
            status, payload = self._api_json(ProviderRequest(
                "cloudflare_pages_api", "GET",
                self._project_path(account_id, project_name) + "/deployments?" + query,
                timeout_seconds=20,
            ))
            if status != "found" or payload.get("success") is not True:
                return "error", ()
            result = payload.get("result")
            info = payload.get("result_info")
            if not isinstance(result, list) or not all(isinstance(row, Mapping) for row in result):
                return "error", ()
            if not isinstance(info, Mapping):
                return "error", ()
            total_pages = info.get("total_pages")
            if (not isinstance(total_pages, int) or isinstance(total_pages, bool)
                    or not 1 <= total_pages <= _MAX_LIST_PAGES):
                return "error", ()
            if expected_pages is None:
                expected_pages = total_pages
            elif expected_pages != total_pages:
                return "error", ()
            rows.extend(result)
            if page >= expected_pages:
                return "found", tuple(rows)
        return "error", ()

    @staticmethod
    def _safe_url(value: object) -> str | None:
        if not isinstance(value, str) or len(value) > 2048:
            return None
        from urllib.parse import urlsplit

        try:
            parsed = urlsplit(value)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.port not in {None, 443}
                    or parsed.fragment or not parsed.hostname.casefold().endswith(".pages.dev")):
                return None
            return value
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _alias_host(value: object) -> str | None:
        if not isinstance(value, str) or len(value) > 2048:
            return None
        from urllib.parse import urlsplit

        candidate = value if "://" in value else f"https://{value}"
        try:
            parsed = urlsplit(candidate)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.port not in {None, 443}
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                    or not _DOMAIN.fullmatch(parsed.hostname)):
                return None
            return parsed.hostname.casefold()
        except (TypeError, ValueError):
            return None

    def _matching_deployment(self, request: ReleaseRequest, operation_id: str):
        if not _OPERATION_ID.fullmatch(operation_id):
            return "invalid", None
        state, rows = self._read_deployments(request, environment=request.channel)
        if state != "found":
            return state, None
        marker = f"OLCR release operation {operation_id}"
        matched = []
        for row in rows:
            trigger = row.get("deployment_trigger")
            trigger_metadata = trigger.get("metadata") if isinstance(trigger, Mapping) else None
            if isinstance(trigger_metadata, Mapping) and trigger_metadata.get("commit_message") == marker:
                matched.append(row)
        if not matched:
            return "missing", None
        if len(matched) != 1:
            return "conflict", None
        row = matched[0]
        metadata = self._metadata(request)
        project_state, project = self._read_project(request)
        trigger = row.get("deployment_trigger")
        trigger_metadata = trigger.get("metadata") if isinstance(trigger, Mapping) else None
        exact = bool(
            project_state == "found" and isinstance(project, Mapping)
            and row.get("project_name") == metadata.get("project_name")
            and (not project.get("id") or row.get("project_id") == project.get("id"))
            and self._project_matches_target(request, project)
            and row.get("environment") == request.channel
            and isinstance(trigger_metadata, Mapping)
            and trigger_metadata.get("branch") == metadata.get("branch")
            and str(trigger_metadata.get("commit_hash", "")).casefold()
            == str(metadata.get("build_commit", "")).casefold()
        )
        return ("exact", row) if exact else ("conflict", row)

    def _deployment_result(self, request: ReleaseRequest, state: str, row: Mapping[str, object] | None,
                           *, after_upload: bool = False) -> ReleaseResult:
        metadata = self._metadata(request)
        operation_id = row.get("id") if isinstance(row, Mapping) else None
        url = self._safe_url(row.get("url")) if isinstance(row, Mapping) else None
        if state == "exact" and isinstance(row, Mapping):
            stage = row.get("latest_stage")
            stage_status = stage.get("status") if isinstance(stage, Mapping) else None
            if stage_status == "success":
                if request.channel == "preview":
                    return ReleaseResult("SUBMITTED", "PREVIEW_READY", url, str(operation_id or ""),
                                         "Cloudflare confirmed the exact preview deployment; it was not promoted to production.", False)
                target = metadata.get("target_domain")
                aliases = row.get("aliases")
                alias_hosts = set()
                if isinstance(aliases, list):
                    for alias in aliases:
                        host = self._alias_host(alias)
                        if host:
                            alias_hosts.add(host)
                if isinstance(target, str) and target.casefold() in alias_hosts:
                    return ReleaseResult("PUBLISHED", "PRODUCTION_ALIAS_READY", url, str(operation_id or ""),
                                         "Cloudflare confirmed the exact production branch, commit, and requested domain alias.", False)
                return ReleaseResult("SUBMITTED", "PRODUCTION_DEPLOYMENT_READY", url, str(operation_id or ""),
                                     "Cloudflare confirmed the production deployment, but the requested domain alias is not assigned.", False)
            if stage_status in {"failure", "canceled"}:
                return ReleaseResult("FAILED", "DEPLOYMENT_FAILED", None, str(operation_id or ""),
                                     "Cloudflare reported that this deployment failed or was canceled.", False)
            return ReleaseResult("UNCERTAIN", "DEPLOYMENT_PENDING", url, str(operation_id or ""),
                                 "Cloudflare has not reported a completed deployment; inspect before retrying.", True)
        if state == "conflict":
            return ReleaseResult("BLOCKED", "DEPLOYMENT_CONFLICT", None,
                                 str(operation_id) if isinstance(operation_id, str) else None,
                                 "Cloudflare read-back found a deployment with a conflicting project, branch, environment, or commit.", False)
        if state == "missing" and not after_upload:
            return ReleaseResult("FAILED", "DEPLOYMENT_NOT_FOUND", None, None,
                                 "No Cloudflare deployment exists for this operation marker.", False)
        return ReleaseResult("UNCERTAIN", "DEPLOYMENT_INSPECTION_REQUIRED", None,
                             str(operation_id) if isinstance(operation_id, str) else None,
                             "Cloudflare deployment state could not be confirmed; inspect before any retry.", True)

    def publish(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            return ReleaseResult("BLOCKED", "OPERATION_ID_INVALID", None, None,
                                 "The Cloudflare release operation id is invalid.", False)
        preflight = self.preflight(request)
        if preflight.status != "READY":
            return ReleaseResult("BLOCKED", "PREFLIGHT_BLOCKED", None, None,
                                 preflight.blockers[0] if preflight.blockers else "Cloudflare Pages preflight is blocked.", False)
        assets, directory, error = self._asset_tree(request)
        if error or directory is None:
            return ReleaseResult("BLOCKED", "ARTIFACT_INVALID", None, None,
                                 error or "The Pages deployment directory is unavailable.", False)
        state, row = self._matching_deployment(request, operation_id)
        if state == "exact" or state == "conflict":
            return self._deployment_result(request, state, row)
        if state != "missing":
            return self._deployment_result(request, "error", None)
        current, current_directory, current_error = self._asset_tree(request)
        if current_error or current_directory != directory or not same_tree(assets, current):
            return ReleaseResult("BLOCKED", "ARTIFACT_CHANGED_BEFORE_UPLOAD", None, None,
                                 "The Cloudflare deployment assets changed after validation and before upload.", False)
        root = workspace_path(request)
        if root is None:
            return ReleaseResult("BLOCKED", "WORKSPACE_UNAVAILABLE", None, None,
                                 "The verified Cloudflare workspace is unavailable.", False)
        manifest, assets_by_hash, manifest_error = self._asset_manifest(assets, root, directory)
        if manifest_error or manifest is None or assets_by_hash is None:
            return ReleaseResult("BLOCKED", "ARTIFACT_INVALID", None, None,
                                 manifest_error or "The Cloudflare Pages upload manifest is invalid.", False)

        token = self._upload_token(request)
        if token is None:
            return ReleaseResult("BLOCKED", "UPLOAD_TOKEN_UNAVAILABLE", None, None,
                                 "Cloudflare did not issue an upload token for the existing Pages project.", False)
        hashes = sorted(set(manifest.values()))
        missing = self._missing_asset_hashes(hashes, token)
        if missing is None:
            return ReleaseResult("BLOCKED", "ASSET_CHECK_FAILED", None, None,
                                 "Cloudflare could not confirm which Pages assets are missing.", False)
        if not self._upload_missing_assets(missing, assets_by_hash, root, token):
            return ReleaseResult("UNCERTAIN", "ASSET_UPLOAD_UNCERTAIN", None, None,
                                 "Cloudflare asset upload did not complete cleanly; inspect before retrying.", True)
        if not self._upsert_asset_hashes(hashes, token):
            return ReleaseResult("UNCERTAIN", "ASSET_HASH_UPSERT_UNCERTAIN", None, None,
                                 "Cloudflare did not confirm the complete asset hash set; inspect before retrying.", True)

        metadata = self._metadata(request)
        marker = f"OLCR release operation {operation_id}"
        boundary, body = self._multipart_form({
            "branch": str(metadata["branch"]),
            "commit_dirty": "false",
            "commit_hash": str(metadata["build_commit"]),
            "commit_message": marker,
            "manifest": json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        })
        latest_project_state, latest_project = self._read_project(request)
        if (latest_project_state != "found" or not isinstance(latest_project, Mapping)
                or not self._project_matches_target(request, latest_project)):
            return ReleaseResult(
                "BLOCKED", "TARGET_CHANGED_BEFORE_DEPLOYMENT", None, None,
                "Cloudflare Pages branch or domain configuration changed after preflight; no deployment was created.",
                False,
            )
        account_id, project_name = metadata["account_id"], metadata["project_name"]
        create_state, create_payload = self._api_json(ProviderRequest(
            "cloudflare_pages_api", "POST",
            self._project_path(str(account_id), str(project_name)) + "/deployments",
            body=body,
            timeout_seconds=30,
            content_type=f"multipart/form-data; boundary={boundary}",
        ))
        create_accepted = bool(
            create_state == "found" and isinstance(create_payload, Mapping)
            and create_payload.get("success") is True
            and isinstance(create_payload.get("result"), Mapping)
            and isinstance(create_payload["result"].get("id"), str)
        )
        state, row = self._matching_deployment(request, operation_id)
        if state in {"exact", "conflict"}:
            return self._deployment_result(request, state, row, after_upload=True)
        if create_accepted:
            return self._deployment_result(request, "error", None, after_upload=True)
        return self._deployment_result(request, "error", None, after_upload=True)

    def inspect(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        preflight = self.preflight(request)
        if preflight.status != "READY":
            return ReleaseResult("BLOCKED", "PREFLIGHT_BLOCKED", None, None,
                                 preflight.blockers[0] if preflight.blockers else "Cloudflare inspection is blocked.", False)
        state, row = self._matching_deployment(request, operation_id)
        return self._deployment_result(request, state, row)


__all__ = ["CloudflarePagesReleaseAdapter"]
