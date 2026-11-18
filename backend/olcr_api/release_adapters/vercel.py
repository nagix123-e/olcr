"""Fail-closed Vercel prebuilt deployment and production promotion adapter."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

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
from .web_common import (
    MAX_FILE_BYTES,
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


_PROJECT_ID = re.compile(r"^prj_[A-Za-z0-9_-]{1,160}$")
_TEAM_ID = re.compile(r"^team_[A-Za-z0-9_-]{1,160}$")
_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_DOMAIN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.I)
_OPERATION_ID = re.compile(r"^relop_[0-9a-f]{64}$", re.I)
_MAX_JSON_BYTES = 1024 * 1024
_MAX_LIST_PAGES = 20
_MAX_FILES = MAX_FILES
_MAX_FILE_BYTES = MAX_FILE_BYTES
_MAX_TOTAL_BYTES = MAX_TOTAL_BYTES
_OUTPUT_DIRECTORY = ".vercel/output"


class VercelReleaseAdapter:
    """Deploy an existing `.vercel/output` tree to one explicitly selected project."""

    provider_id = "vercel"

    def __init__(
        self,
        *,
        runner: ReleaseCommandRunner | None = None,
        api_transport: ProviderApiTransport | None = None,
        credential_provider: Callable[[str], Mapping[str, str] | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.credential_provider = credential_provider
        self.runner = runner or ReleaseCommandRunner(credential_provider=credential_provider)
        self._credential_source = credential_provider or getattr(self.runner, "credential_provider", None)
        self.api_transport = api_transport or ProviderApiTransport(
            credential_provider=lambda profile: self._api_token(profile),
        )
        self.clock = clock

    @staticmethod
    def _metadata(request: ReleaseRequest) -> Mapping[str, object]:
        return request.metadata if isinstance(getattr(request, "metadata", None), Mapping) else {}

    def _credentials(self) -> Mapping[str, str] | None:
        if self._credential_source is None:
            return None
        try:
            value = self._credential_source("vercel")
            return value if isinstance(value, Mapping) else None
        except Exception:
            return None

    def _api_token(self, profile: str) -> str | None:
        if profile != "vercel_api":
            return None
        credentials = self._credentials()
        token = credentials.get("VERCEL_TOKEN") if credentials else None
        return token if isinstance(token, str) and token.strip() else None

    def _has_credentials(self) -> bool:
        credentials = self._credentials()
        token = credentials.get("VERCEL_TOKEN") if credentials else None
        return bool(
            isinstance(token, str) and 16 <= len(token) <= 4096
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token)
        )

    def _target_error(self, request: ReleaseRequest) -> str | None:
        metadata = self._metadata(request)
        project_id = metadata.get("project_id")
        project_name = metadata.get("project_name")
        team_id = metadata.get("team_id")
        environment = metadata.get("environment")
        action = metadata.get("release_action")
        commit = metadata.get("build_commit")
        if (not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id)
                or not isinstance(project_name, str) or not _PROJECT_NAME.fullmatch(project_name)
                or "---" in project_name
                or request.app_or_package != project_name
                or not isinstance(team_id, str) or not _TEAM_ID.fullmatch(team_id)):
            return "The exact existing Vercel project ID, project name, and team scope must be resolved."
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            return "A full validated Git commit is required for the Vercel deployment."
        if metadata.get("deployment_directory") != _OUTPUT_DIRECTORY:
            return "Vercel can deploy only the exact workspace `.vercel/output` directory."

        if request.channel == "preview":
            if (environment != "preview" or action != "deploy"
                    or metadata.get("production_authorized") is not False
                    or metadata.get("target_domain") is not None):
                return "Preview must be an explicit preview deploy without production authorization or a production domain."
            return None
        if request.channel == "production-stage":
            expected_action = "stage"
        elif request.channel == "production-promote":
            expected_action = "promote"
        else:
            return "Vercel supports only separate preview, production-stage, and production-promote actions."
        target_domain = metadata.get("target_domain")
        if (environment != "production" or action != expected_action
                or metadata.get("production_authorized") is not True
                or not isinstance(target_domain, str) or not _DOMAIN.fullmatch(target_domain)):
            return "Production staging or promotion requires explicit authorization and an exact attached domain."
        if request.channel == "production-promote":
            deployment_id = metadata.get("deployment_id")
            if not isinstance(deployment_id, str) or not re.fullmatch(r"dpl_[A-Za-z0-9_-]{1,180}", deployment_id):
                return "Production promotion requires the exact already-staged deployment ID."
        return None

    def _asset_tree(self, request: ReleaseRequest) -> tuple[tuple[WebAsset, ...], Path | None, str | None]:
        metadata = self._metadata(request)
        return deployment_tree(
            request,
            metadata.get("deployment_directory"),
            max_files=_MAX_FILES,
            max_file_bytes=_MAX_FILE_BYTES,
            max_total_bytes=_MAX_TOTAL_BYTES,
        )

    @staticmethod
    def _fingerprint(assets: tuple[WebAsset, ...]) -> str:
        digest = hashlib.sha256()
        output_root = Path(_OUTPUT_DIRECTORY)
        for asset in sorted(assets, key=lambda item: item.relative_path.as_posix()):
            relative = asset.relative_path.relative_to(output_root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(asset.size).encode("ascii"))
            digest.update(b"\0")
            digest.update(asset.sha256.encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    @staticmethod
    def _project_path(project_id: str) -> str:
        return f"/v9/projects/{quote(project_id, safe='')}"

    def _api_json(self, spec: ProviderRequest) -> tuple[str, Mapping[str, object] | None]:
        try:
            response = self.api_transport.request(spec)
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
        return ("found", payload) if isinstance(payload, Mapping) else ("error", None)

    def _read_project(self, request: ReleaseRequest) -> tuple[str, Mapping[str, object] | None]:
        metadata = self._metadata(request)
        project_id, team_id = metadata.get("project_id"), metadata.get("team_id")
        if not isinstance(project_id, str) or not isinstance(team_id, str):
            return "invalid", None
        query = urlencode({"teamId": team_id})
        state, payload = self._api_json(ProviderRequest(
            "vercel_api", "GET", f"{self._project_path(project_id)}?{query}", timeout_seconds=20,
        ))
        if state != "found" or not isinstance(payload, Mapping):
            return state, None
        if (payload.get("id") != project_id or payload.get("name") != metadata.get("project_name")
                or payload.get("accountId") != team_id):
            return "conflict", payload
        return "found", payload

    def _read_domains(self, request: ReleaseRequest) -> tuple[str, tuple[Mapping[str, object], ...]]:
        metadata = self._metadata(request)
        project_id, team_id = metadata.get("project_id"), metadata.get("team_id")
        if not isinstance(project_id, str) or not isinstance(team_id, str):
            return "invalid", ()
        rows: list[Mapping[str, object]] = []
        cursor = None
        seen: set[str] = set()
        for _ in range(_MAX_LIST_PAGES):
            params: dict[str, object] = {"teamId": team_id, "target": "production", "limit": 100}
            if cursor:
                params["until"] = cursor
            state, payload = self._api_json(ProviderRequest(
                "vercel_api", "GET",
                f"{self._project_path(project_id)}/domains?{urlencode(params)}", timeout_seconds=20,
            ))
            if state != "found" or not isinstance(payload, Mapping):
                return "error", ()
            found_rows = payload.get("domains")
            pagination = payload.get("pagination")
            if (not isinstance(found_rows, list) or not all(isinstance(row, Mapping) for row in found_rows)
                    or not isinstance(pagination, Mapping)):
                return "error", ()
            rows.extend(found_rows)
            next_value = pagination.get("next")
            if next_value is None or next_value == "" or next_value == 0 or next_value == "0":
                return "found", tuple(rows)
            next_cursor = str(next_value)
            if next_cursor in seen or len(next_cursor) > 40:
                return "error", ()
            seen.add(next_cursor)
            cursor = next_cursor
        return "error", ()

    def _target_domain_attached(self, request: ReleaseRequest, rows: tuple[Mapping[str, object], ...]) -> bool:
        metadata = self._metadata(request)
        target_domain = metadata.get("target_domain")
        project_id = metadata.get("project_id")
        if not isinstance(target_domain, str) or not isinstance(project_id, str):
            return False
        return any(
            isinstance(row.get("name"), str)
            and row["name"].casefold() == target_domain.casefold()
            and row.get("projectId") == project_id
            for row in rows
        )

    def _production_domain_scope_authorized(
        self, request: ReleaseRequest, rows: tuple[Mapping[str, object], ...],
    ) -> bool:
        if request.channel != "production-promote":
            return True
        metadata = self._metadata(request)
        project_id = metadata.get("project_id")
        target_domain = metadata.get("target_domain")
        if not isinstance(project_id, str) or not isinstance(target_domain, str):
            return False
        configured: set[str] = set()
        for row in rows:
            name = row.get("name")
            if row.get("projectId") != project_id or not isinstance(name, str) or not _DOMAIN.fullmatch(name):
                return False
            configured.add(name.casefold())
        if target_domain.casefold() not in configured:
            return False
        authorized = metadata.get("authorized_production_domains")
        if len(configured) > 1 or authorized is not None:
            if (not isinstance(authorized, (tuple, list))
                    or not all(isinstance(name, str) and _DOMAIN.fullmatch(name) for name in authorized)):
                return False
            authorized_names = {name.casefold() for name in authorized}
            return len(authorized_names) == len(authorized) and authorized_names == configured
        return True

    def preflight(self, request: ReleaseRequest) -> ReleasePreflight:
        target_error = self._target_error(request)
        credentials = self._has_credentials()
        assets, directory, asset_error = self._asset_tree(request)
        checks, blockers = base_preflight(
            request, self.provider_id,
            credentials_configured=credentials,
            local_build_validated=self._metadata(request).get("local_build_validated") is True and asset_error is None,
            now=self.clock(),
        )
        add_check(checks, blockers, "vercel_target",
                  target_error or "The exact Vercel project, team, environment, and action are explicit.",
                  passed=target_error is None)
        add_check(checks, blockers, "deployment_assets",
                  asset_error or "Every declared file exactly matches the workspace-confined `.vercel/output` tree.",
                  passed=asset_error is None and directory is not None)
        add_check(checks, blockers, "credential_profile",
                  "A configured Vercel token is available through the selected provider profile." if credentials else
                  "A configured Vercel token is required through the selected provider profile.",
                  passed=credentials)
        if blockers:
            return preflight_result(checks, blockers)

        project_state, project = self._read_project(request)
        project_ok = project_state == "found" and project is not None
        add_check(checks, blockers, "existing_project",
                  "The exact existing Vercel project and team binding were confirmed." if project_ok else
                  "The exact Vercel project and team could not be confirmed; project creation is unsupported.",
                  passed=project_ok)
        if request.channel in {"production-stage", "production-promote"}:
            domain_state, domains = self._read_domains(request)
            attached = domain_state == "found" and self._target_domain_attached(request, domains)
            add_check(checks, blockers, "existing_production_domain",
                      "The requested production domain is attached to the exact existing Vercel project." if attached else
                      "The requested domain is not confirmed as an existing production domain on this project.",
                      passed=attached)
            if request.channel == "production-promote":
                scope_authorized = domain_state == "found" and self._production_domain_scope_authorized(request, domains)
                add_check(checks, blockers, "production_domain_scope",
                          "All production domains affected by promotion are explicitly authorized." if scope_authorized else
                          "Promotion can affect every production domain on this project; explicitly authorize the exact complete domain set.",
                          passed=scope_authorized)
        if blockers:
            return preflight_result(checks, blockers)

        root = workspace_path(request)
        try:
            cli = self.runner.run(CommandSpec(
                "vercel", ("--version",), root, environment_profile="vercel", timeout_seconds=15,
            ))
        except Exception:
            cli = CommandResult(None, "", "", False, "CLI_PROBE_FAILED")
        available = cli.returncode == 0 and not cli.timed_out and cli.error_code is None
        add_check(checks, blockers, "vercel_cli",
                  "The Vercel CLI is available for an explicit prebuilt deployment." if available else
                  "The Vercel CLI could not be verified in the configured environment.",
                  passed=available)
        return preflight_result(checks, blockers)

    def _read_deployments(self, request: ReleaseRequest) -> tuple[str, tuple[Mapping[str, object], ...]]:
        metadata = self._metadata(request)
        project_id, team_id = metadata.get("project_id"), metadata.get("team_id")
        if not isinstance(project_id, str) or not isinstance(team_id, str):
            return "invalid", ()
        rows: list[Mapping[str, object]] = []
        cursor = None
        seen: set[str] = set()
        for _ in range(_MAX_LIST_PAGES):
            params: dict[str, object] = {
                "projectId": project_id,
                "teamId": team_id,
                "target": "preview" if request.channel == "preview" else "production",
                "limit": 100,
            }
            if cursor:
                params["until"] = cursor
            state, payload = self._api_json(ProviderRequest(
                "vercel_api", "GET", f"/v7/deployments?{urlencode(params)}", timeout_seconds=20,
            ))
            if state != "found" or not isinstance(payload, Mapping):
                return "error", ()
            found_rows = payload.get("deployments")
            pagination = payload.get("pagination")
            if (not isinstance(found_rows, list) or not all(isinstance(row, Mapping) for row in found_rows)
                    or not isinstance(pagination, Mapping)):
                return "error", ()
            rows.extend(found_rows)
            next_value = pagination.get("next")
            if next_value is None or next_value == "" or next_value == 0 or next_value == "0":
                return "found", tuple(rows)
            next_cursor = str(next_value)
            if next_cursor in seen or len(next_cursor) > 40:
                return "error", ()
            seen.add(next_cursor)
            cursor = next_cursor
        return "error", ()

    def _matching_deployment(self, request: ReleaseRequest, operation_id: str, output_digest: str):
        if not _OPERATION_ID.fullmatch(operation_id):
            return "invalid", None
        state, rows = self._read_deployments(request)
        if state != "found":
            return state, None
        matched = []
        for row in rows:
            deployment_meta = row.get("meta")
            if (isinstance(deployment_meta, Mapping)
                    and deployment_meta.get("olcrReleaseOperation") == operation_id):
                matched.append(row)
        if not matched:
            return "missing", None
        if len(matched) != 1:
            return "conflict", None
        row = matched[0]
        metadata = self._metadata(request)
        deployment_id = row.get("uid")
        expected_target = "production" if request.channel != "preview" else None
        row_team_id = row.get("teamId")
        listed_state = row.get("readyState", row.get("state"))
        exact = bool(
            isinstance(deployment_id, str)
            and re.fullmatch(r"dpl_[A-Za-z0-9_-]{1,180}", deployment_id)
            and row.get("projectId") == metadata.get("project_id")
            and (row_team_id is None or row_team_id == metadata.get("team_id"))
            # The exact list request is filtered by target. Preview `target` is
            # optional in the deployment response schema, so an omitted value
            # is equivalent to null only on this already-filtered preview path.
            and row.get("target") == expected_target
            and isinstance(listed_state, str)
            and isinstance(row.get("meta"), Mapping)
            and row["meta"].get("olcrBuildCommit") == metadata.get("build_commit")
            and row["meta"].get("olcrBuildOutputSha256") == output_digest
        )
        normalized = dict(row)
        if isinstance(deployment_id, str):
            normalized["id"] = deployment_id
        if "readyState" not in normalized and isinstance(listed_state, str):
            normalized["readyState"] = listed_state
        return ("exact", normalized) if exact else ("conflict", normalized)

    @staticmethod
    def _safe_deployment_url(value: object) -> str | None:
        if not isinstance(value, str) or len(value) > 253:
            return None
        from urllib.parse import urlsplit

        candidate = value if "://" in value else f"https://{value}"
        try:
            parsed = urlsplit(candidate)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.port not in {None, 443}
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                    or not parsed.hostname.casefold().endswith(".vercel.app")):
                return None
            return f"https://{parsed.hostname.casefold()}"
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _deployment_result(state: str, row: Mapping[str, object] | None,
                           *, after_deploy: bool = False) -> ReleaseResult:
        deployment_id = row.get("id") if isinstance(row, Mapping) else None
        valid_id = isinstance(deployment_id, str) and bool(re.fullmatch(r"dpl_[A-Za-z0-9_-]{1,180}", deployment_id))
        remote_id = deployment_id if valid_id else None
        remote_url = VercelReleaseAdapter._safe_deployment_url(row.get("url")) if isinstance(row, Mapping) else None
        if state == "exact" and isinstance(row, Mapping):
            if not valid_id:
                return ReleaseResult("BLOCKED", "DEPLOYMENT_ID_INVALID", None, None,
                                     "Vercel returned a deployment ID outside the supported format.", False)
            ready_state = row.get("readyState")
            if ready_state == "READY":
                if remote_url is None:
                    return ReleaseResult("UNCERTAIN", "DEPLOYMENT_URL_INVALID", None, remote_id,
                                         "Vercel reports a ready deployment but its URL could not be validated.", True)
                if row.get("target") is None:
                    return ReleaseResult("SUBMITTED", "PREVIEW_READY", remote_url, remote_id,
                                         "Vercel confirmed the exact preview deployment; no production promotion was requested.", False)
                return ReleaseResult("SUBMITTED", "PRODUCTION_STAGED", remote_url, remote_id,
                                     "Vercel confirmed the exact staged production deployment without assigning its domain.", False)
            if ready_state in {"ERROR", "CANCELED"}:
                return ReleaseResult("FAILED", "DEPLOYMENT_FAILED", None, remote_id,
                                     "Vercel reported that the exact deployment failed or was canceled.", False)
            return ReleaseResult("UNCERTAIN", "DEPLOYMENT_PENDING", remote_url, remote_id,
                                 "Vercel has not reported a ready deployment; inspect before retrying.", True)
        if state == "conflict":
            return ReleaseResult("BLOCKED", "DEPLOYMENT_CONFLICT", None, remote_id,
                                 "Vercel read-back found a deployment with conflicting project, team, environment, commit, or output metadata.", False)
        if state == "missing" and not after_deploy:
            return ReleaseResult("UNCERTAIN", "DEPLOYMENT_NOT_FOUND", None, None,
                                 "No deployment with this operation marker was found; inspect before any retry.", True)
        return ReleaseResult("UNCERTAIN", "DEPLOYMENT_INSPECTION_REQUIRED", None, remote_id,
                             "Vercel deployment state could not be confirmed; inspect before any retry.", True)

    def _deployment_by_id(self, request: ReleaseRequest, deployment_id: str):
        metadata = self._metadata(request)
        team_id = metadata.get("team_id")
        if not isinstance(team_id, str):
            return "invalid", None
        query = urlencode({"teamId": team_id})
        state, row = self._api_json(ProviderRequest(
            "vercel_api", "GET",
            f"/v13/deployments/{quote(deployment_id, safe='')}?{query}", timeout_seconds=20,
        ))
        if state != "found" or not isinstance(row, Mapping):
            return state, None
        return "found", row

    def _staged_deployment(self, request: ReleaseRequest, output_digest: str):
        metadata = self._metadata(request)
        deployment_id = metadata.get("deployment_id")
        if not isinstance(deployment_id, str):
            return "invalid", None
        state, row = self._deployment_by_id(request, deployment_id)
        if state != "found" or not isinstance(row, Mapping):
            return state, None
        deployment_meta = row.get("meta")
        exact = bool(
            row.get("id") == deployment_id
            and row.get("projectId") == metadata.get("project_id")
            and row.get("teamId") == metadata.get("team_id")
            and row.get("target") == "production"
            and isinstance(deployment_meta, Mapping)
            and _OPERATION_ID.fullmatch(str(deployment_meta.get("olcrReleaseOperation", "")))
            and deployment_meta.get("olcrBuildCommit") == metadata.get("build_commit")
            and deployment_meta.get("olcrBuildOutputSha256") == output_digest
        )
        return ("exact", row) if exact else ("conflict", row)

    def _read_alias(self, request: ReleaseRequest):
        metadata = self._metadata(request)
        target_domain, team_id, project_id = (
            metadata.get("target_domain"), metadata.get("team_id"), metadata.get("project_id"),
        )
        if not all(isinstance(value, str) for value in (target_domain, team_id, project_id)):
            return "invalid", None
        query = urlencode({"teamId": team_id, "projectId": project_id})
        state, alias = self._api_json(ProviderRequest(
            "vercel_api", "GET",
            f"/v4/aliases/{quote(target_domain, safe='')}?{query}", timeout_seconds=20,
        ))
        if state != "found" or not isinstance(alias, Mapping):
            return state, None
        alias_name = alias.get("alias")
        if (alias.get("projectId") != project_id or not isinstance(alias_name, str)
                or alias_name.casefold() != target_domain.casefold()):
            return "conflict", alias
        deleted_at = alias.get("deletedAt")
        if deleted_at is not None and deleted_at != 0 and deleted_at != "0" and deleted_at != "":
            return "missing", alias
        return "found", alias

    def _promotion_status(self, request: ReleaseRequest) -> tuple[str, str]:
        metadata = self._metadata(request)
        project_name, team_id, root = metadata.get("project_name"), metadata.get("team_id"), workspace_path(request)
        if not isinstance(project_name, str) or not isinstance(team_id, str) or root is None:
            return "error", ""
        try:
            result = self.runner.run(CommandSpec(
                "vercel", ("promote", "status", project_name, "--scope", team_id),
                root, environment_profile="vercel", timeout_seconds=45,
            ))
        except Exception:
            return "error", ""
        if result.returncode != 0 or result.timed_out or result.error_code is not None:
            return "error", result.stdout
        text = (result.stdout + "\n" + result.stderr).strip().casefold()
        if any(phrase in text for phrase in ("no pending", "no active promotion", "no current promotion")):
            return "clear", text
        if any(phrase in text for phrase in ("in progress", "pending", "queued", "running", "promoting")):
            return "pending", text
        return "unknown", text

    @staticmethod
    def _promotion_result(*, deployment: Mapping[str, object], alias_state: str,
                          alias: Mapping[str, object] | None, target_domain: str,
                          status_state: str, command_result: CommandResult | None = None) -> ReleaseResult:
        deployment_id = deployment.get("id")
        remote_id = deployment_id if isinstance(deployment_id, str) else None
        staged_url = VercelReleaseAdapter._safe_deployment_url(deployment.get("url"))
        if status_state in {"pending", "unknown", "error"}:
            return ReleaseResult("UNCERTAIN", "PROMOTION_PENDING_OR_UNKNOWN", staged_url, remote_id,
                                 "Vercel promotion is pending or its status is unknown; inspect before any retry.", True)
        if (alias_state == "found" and isinstance(alias, Mapping)
                and alias.get("deploymentId") == deployment_id
                and alias.get("projectId") == deployment.get("projectId")):
            return ReleaseResult("PUBLISHED", "PRODUCTION_ALIAS_CURRENT", f"https://{target_domain.casefold()}", remote_id,
                                 "Vercel confirmed that the requested production domain currently aliases the exact staged deployment.", False)
        if alias_state == "conflict":
            return ReleaseResult("BLOCKED", "DOMAIN_ALIAS_CONFLICT", None, remote_id,
                                 "Vercel alias read-back did not bind the requested domain to the exact project.", False)
        if alias_state == "error":
            return ReleaseResult("UNCERTAIN", "ALIAS_INSPECTION_REQUIRED", staged_url, remote_id,
                                 "Vercel could not confirm the current production domain alias.", True)
        if command_result is not None:
            if command_result.timed_out or command_result.returncode is None or command_result.error_code is not None:
                return ReleaseResult("UNCERTAIN", "PROMOTION_TIMEOUT", staged_url, remote_id,
                                     "The Vercel promotion command timed out; status and alias were inspected, so inspect again before retrying.", True)
            if command_result.returncode != 0:
                return ReleaseResult("FAILED", "PROMOTION_FAILED", None, remote_id,
                                     "Vercel rejected the promotion and the requested domain is not current on this deployment.", False)
            return ReleaseResult("SUBMITTED", "PROMOTION_ACCEPTED_ALIAS_PENDING", staged_url, remote_id,
                                 "Vercel accepted the promotion, but the requested domain is not yet confirmed as current.", True)
        return ReleaseResult("SUBMITTED", "PRODUCTION_STAGED", staged_url,
                             remote_id, "The exact production deployment remains staged; the requested domain is not current on it.", False)

    def _inspect_promotion(self, request: ReleaseRequest, output_digest: str,
                           command_result: CommandResult | None = None) -> ReleaseResult:
        state, deployment = self._staged_deployment(request, output_digest)
        if state != "exact" or not isinstance(deployment, Mapping):
            return self._deployment_result(state, deployment)
        ready_state = deployment.get("readyState")
        if ready_state in {"ERROR", "CANCELED"}:
            return ReleaseResult("FAILED", "STAGED_DEPLOYMENT_FAILED", None,
                                 str(deployment.get("id", "")),
                                 "The exact staged production deployment failed or was canceled.", False)
        if ready_state != "READY":
            return ReleaseResult("UNCERTAIN", "STAGED_DEPLOYMENT_PENDING", None,
                                 str(deployment.get("id", "")),
                                 "The exact staged production deployment is not ready yet.", True)
        alias_state, alias = self._read_alias(request)
        status_state, _ = self._promotion_status(request)
        target_domain = self._metadata(request).get("target_domain")
        if not isinstance(target_domain, str):
            return ReleaseResult("BLOCKED", "TARGET_DOMAIN_INVALID", None, str(deployment.get("id", "")),
                                 "The exact production domain is missing from the request.", False)
        return self._promotion_result(
            deployment=deployment, alias_state=alias_state, alias=alias, target_domain=target_domain,
            status_state=status_state, command_result=command_result,
        )

    def _publish_promotion(self, request: ReleaseRequest, operation_id: str, output_digest: str) -> ReleaseResult:
        metadata = self._metadata(request)
        state, deployment = self._staged_deployment(request, output_digest)
        if state != "exact" or not isinstance(deployment, Mapping):
            return self._deployment_result(state, deployment)
        if deployment.get("readyState") != "READY":
            return self._inspect_promotion(request, output_digest)
        alias_state, alias = self._read_alias(request)
        target_domain = metadata.get("target_domain")
        if not isinstance(target_domain, str):
            return ReleaseResult("BLOCKED", "TARGET_DOMAIN_INVALID", None, str(deployment.get("id", "")),
                                 "The exact production domain is missing from the request.", False)
        status_state, _ = self._promotion_status(request)
        if (alias_state == "found" and isinstance(alias, Mapping)
                and alias.get("deploymentId") == deployment.get("id")
                and alias.get("projectId") == metadata.get("project_id")):
            return self._promotion_result(deployment=deployment, alias_state=alias_state, alias=alias,
                                          target_domain=target_domain, status_state=status_state)
        if alias_state in {"error", "conflict", "invalid"}:
            return self._promotion_result(deployment=deployment, alias_state=alias_state, alias=alias,
                                          target_domain=target_domain, status_state="unknown")
        if status_state != "clear":
            return self._promotion_result(deployment=deployment, alias_state=alias_state, alias=alias,
                                          target_domain=target_domain, status_state=status_state)
        domain_state, domains = self._read_domains(request)
        if (domain_state != "found" or not self._target_domain_attached(request, domains)
                or not self._production_domain_scope_authorized(request, domains)):
            return ReleaseResult("BLOCKED", "PRODUCTION_DOMAIN_SCOPE_CHANGED", None,
                                 str(deployment.get("id", "")),
                                 "The production domain set changed or could not be reconfirmed before promotion.", False)
        root = workspace_path(request)
        if root is None:
            return ReleaseResult("BLOCKED", "WORKSPACE_INVALID", None, str(deployment.get("id", "")),
                                 "The Vercel workspace could not be resolved safely.", False)
        try:
            command = self.runner.run(CommandSpec(
                "vercel", ("promote", str(deployment["id"]), "--yes", "--scope", str(metadata["team_id"])),
                root, environment_profile="vercel", timeout_seconds=210,
            ))
        except Exception:
            command = CommandResult(None, "", "", False, "PROMOTION_COMMAND_FAILED")
        return self._inspect_promotion(request, output_digest, command_result=command)

    def publish(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        preflight = self.preflight(request)
        if preflight.status != "READY":
            return ReleaseResult("BLOCKED", "PREFLIGHT_BLOCKED", None, None,
                                 preflight.blockers[0] if preflight.blockers else "Vercel preflight is blocked.", False)
        if not _OPERATION_ID.fullmatch(operation_id):
            return ReleaseResult("BLOCKED", "OPERATION_ID_INVALID", None, None,
                                 "A valid release operation ID is required for exact Vercel read-back.", False)
        assets, directory, error = self._asset_tree(request)
        if error or directory is None:
            return ReleaseResult("BLOCKED", "ARTIFACT_INVALID", None, None,
                                 error or "The Vercel output directory is unavailable.", False)
        output_digest = self._fingerprint(assets)
        if request.channel == "production-promote":
            return self._publish_promotion(request, operation_id, output_digest)

        state, row = self._matching_deployment(request, operation_id, output_digest)
        if state in {"exact", "conflict"}:
            return self._deployment_result(state, row)
        if state != "missing":
            return self._deployment_result("error", None)
        current, current_directory, current_error = self._asset_tree(request)
        if current_error or current_directory != directory or not same_tree(assets, current):
            return ReleaseResult("BLOCKED", "ARTIFACT_CHANGED_BEFORE_DEPLOY", None, None,
                                 "The Vercel prebuilt output changed after validation and before deployment.", False)
        root = workspace_path(request)
        if root is None:
            return ReleaseResult("BLOCKED", "WORKSPACE_INVALID", None, None,
                                 "The Vercel workspace could not be resolved safely.", False)
        metadata = self._metadata(request)
        argv: list[str] = ["deploy", "--prebuilt", "--yes", "--project", str(metadata["project_id"]),
                           "--scope", str(metadata["team_id"]),
                           "--meta", f"olcrReleaseOperation={operation_id}",
                           "--meta", f"olcrBuildCommit={metadata['build_commit']}",
                           "--meta", f"olcrBuildOutputSha256={output_digest}"]
        if request.channel == "preview":
            argv.extend(("--environment", "preview"))
        else:
            argv.extend(("--prod", "--skip-domain"))
        try:
            command = self.runner.run(CommandSpec(
                "vercel", tuple(argv), root, environment_profile="vercel", timeout_seconds=300,
            ))
        except Exception:
            command = CommandResult(None, "", "", False, "DEPLOY_COMMAND_FAILED")
        state, row = self._matching_deployment(request, operation_id, output_digest)
        if state in {"exact", "conflict"}:
            return self._deployment_result(state, row, after_deploy=True)
        if command.timed_out or command.returncode is None or command.error_code is not None:
            return self._deployment_result("error", None, after_deploy=True)
        if command.returncode != 0:
            return ReleaseResult("FAILED", "DEPLOY_COMMAND_FAILED", None, None,
                                 "Vercel rejected the deployment and no exact matching deployment was read back.", False)
        return self._deployment_result("error", None, after_deploy=True)

    def inspect(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        preflight = self.preflight(request)
        if preflight.status != "READY":
            return ReleaseResult("BLOCKED", "PREFLIGHT_BLOCKED", None, None,
                                 preflight.blockers[0] if preflight.blockers else "Vercel inspection is blocked.", False)
        if not _OPERATION_ID.fullmatch(operation_id):
            return ReleaseResult("BLOCKED", "OPERATION_ID_INVALID", None, None,
                                 "A valid release operation ID is required for exact Vercel read-back.", False)
        assets, _, error = self._asset_tree(request)
        if error:
            return ReleaseResult("BLOCKED", "ARTIFACT_INVALID", None, None, error, False)
        output_digest = self._fingerprint(assets)
        if request.channel == "production-promote":
            return self._inspect_promotion(request, output_digest)
        state, row = self._matching_deployment(request, operation_id, output_digest)
        return self._deployment_result(state, row)


__all__ = ["VercelReleaseAdapter"]
