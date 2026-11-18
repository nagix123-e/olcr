from __future__ import annotations

import hashlib
import io
import json
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from olcr_api.provider_research import ProviderEvidence
from olcr_api.release_adapters.base import CommandResult, CommandSpec, ProviderRequest, ProviderResponse, ReleaseCommandRunner
from olcr_api.release_adapters.cloudflare_pages import CloudflarePagesReleaseAdapter
from olcr_api.release_adapters.vercel import VercelReleaseAdapter
from olcr_api.release_workflow import ReleaseAuthorization, ReleaseRequest


NOW = 1_800_000_100.0
VERCEL_TOKEN = "vcp-vercel-token-123456789"
CLOUDFLARE_TOKEN = "cf-pages-token-123456789"
CF_UPLOAD_JWT = "eyJhbGciOiJIUzI1NiJ9.pages-upload-jwt"
ACCOUNT_ID = "a" * 32
BUILD_COMMIT = "1" * 40
OPERATION_ID = "relop_" + "2" * 64


class FakeApi:
    def __init__(self):
        self.calls: list[ProviderRequest] = []
        self.vercel_project = {"id": "prj_demo", "name": "olcr-demo", "accountId": "team_demo"}
        self.vercel_domains = [{"name": "www.example.test", "projectId": "prj_demo"}]
        self.vercel_alias = None
        self.vercel_deployments: list[dict] = []
        self.cloudflare_project = {"name": "olcr-demo", "production_branch": "main",
                                   "domains": ["www.example.test"], "subdomain": "olcr-demo.pages.dev"}
        self.cloudflare_deployments: list[dict] = []
        self.cloudflare_project_status = 200
        self.cloudflare_create_status = 200
        self.cloudflare_asset_upload_status = 200
        self.cloudflare_missing_hashes = None
        self.cloudflare_created_deployment = None
        self.cloudflare_uploaded_assets: list[dict] = []
        self.cloudflare_upserted_hashes: list[str] = []
        self.cloudflare_deployment_manifests: list[dict] = []
        self.on_cloudflare_asset_upsert = None
        self.on_request = None

    def request(self, spec: ProviderRequest) -> ProviderResponse:
        self.calls.append(spec)
        if self.on_request:
            callback, self.on_request = self.on_request, None
            callback(spec)
        parsed = urlsplit(spec.path)
        if spec.host_profile == "vercel_api":
            if parsed.path == "/v9/projects/prj_demo":
                return self._json(200, self.vercel_project)
            if parsed.path == "/v9/projects/prj_demo/domains":
                return self._json(200, {"domains": self.vercel_domains,
                                        "pagination": {"count": len(self.vercel_domains), "next": None, "prev": None}})
            if parsed.path == "/v7/deployments":
                return self._json(200, {"deployments": self.vercel_deployments,
                                        "pagination": {"count": len(self.vercel_deployments), "next": None, "prev": None}})
            if parsed.path.startswith("/v4/aliases/"):
                if self.vercel_alias is not None:
                    return self._json(200, self.vercel_alias)
                return self._json(404, {"error": {"code": "not_found"}})
            if parsed.path.startswith("/v13/deployments/"):
                wanted = unquote(parsed.path.rsplit("/", 1)[-1])
                for deployment in self.vercel_deployments:
                    deployment_id = deployment.get("uid", deployment.get("id"))
                    if wanted in {deployment_id, deployment.get("url")}:
                        detail = dict(deployment)
                        detail["id"] = deployment_id
                        if "readyState" not in detail and isinstance(detail.get("state"), str):
                            detail["readyState"] = detail["state"]
                        return self._json(200, detail)
                return self._json(404, {"error": {"code": "not_found"}})
        elif spec.host_profile == "cloudflare_pages_api":
            prefix = f"/client/v4/accounts/{ACCOUNT_ID}/pages/projects/olcr-demo"
            if parsed.path == prefix and spec.method == "GET":
                return self._json(self.cloudflare_project_status,
                                  {"success": self.cloudflare_project_status == 200,
                                   "result": self.cloudflare_project})
            if parsed.path == prefix + "/upload-token" and spec.method == "GET":
                return self._json(200, {"success": True, "result": {"jwt": CF_UPLOAD_JWT}})
            if parsed.path == prefix + "/deployments" and spec.method == "POST":
                if self.cloudflare_create_status != 200:
                    return self._json(self.cloudflare_create_status, {"success": False, "errors": []})
                fields = self._multipart_fields(spec)
                manifest = json.loads(fields["manifest"])
                self.cloudflare_deployment_manifests.append(manifest)
                marker = fields["commit_message"]
                operation_id = marker.removeprefix("OLCR release operation ")
                branch = fields["branch"]
                row = self.cloudflare_created_deployment or make_cloudflare_deployment(
                    branch=branch,
                    environment="production" if branch == self.cloudflare_project["production_branch"] else "preview",
                    commit=fields["commit_hash"],
                    operation_id=operation_id,
                )
                self.cloudflare_deployments.append(row)
                return self._json(200, {"success": True, "result": {"id": row["id"]}})
            if parsed.path == prefix + "/deployments" and spec.method == "GET":
                env = parse_qs(parsed.query).get("env", [None])[0]
                rows = [row for row in self.cloudflare_deployments if row.get("environment") == env]
                return self._json(200, {"success": True, "result": rows,
                                        "result_info": {"page": 1, "per_page": 100,
                                                        "total_pages": 1, "total_count": len(rows)}})
        elif spec.host_profile == "cloudflare_pages_assets":
            payload = self._json_body(spec)
            if parsed.path == "/client/v4/pages/assets/check-missing" and spec.method == "POST":
                hashes = payload["hashes"]
                missing = self.cloudflare_missing_hashes
                return self._json(200, {"success": True,
                                        "result": hashes if missing is None else missing})
            if parsed.path == "/client/v4/pages/assets/upload" and spec.method == "POST":
                if self.cloudflare_asset_upload_status != 200:
                    return self._json(self.cloudflare_asset_upload_status, {"success": False})
                assert isinstance(payload, list)
                self.cloudflare_uploaded_assets.extend(payload)
                return self._json(200, {"success": True, "errors": [], "messages": []})
            if parsed.path == "/client/v4/pages/assets/upsert-hashes" and spec.method == "POST":
                self.cloudflare_upserted_hashes.extend(payload["hashes"])
                if self.on_cloudflare_asset_upsert:
                    callback, self.on_cloudflare_asset_upsert = self.on_cloudflare_asset_upsert, None
                    callback()
                return self._json(200, {"success": True, "errors": [], "messages": []})
        raise AssertionError(f"unexpected API request {spec.host_profile} {spec.method} {spec.path}")

    @staticmethod
    def _json(status: int, value: object) -> ProviderResponse:
        return ProviderResponse(status, json.dumps(value), "application/json")

    @staticmethod
    def _json_body(spec: ProviderRequest):
        if isinstance(spec.body, dict):
            return spec.body
        if isinstance(spec.body, (str, bytes)):
            return json.loads(spec.body)
        raise AssertionError("Cloudflare JSON body missing")

    @staticmethod
    def _multipart_fields(spec: ProviderRequest) -> dict[str, str]:
        assert isinstance(spec.body, bytes)
        content_type = spec.content_type
        assert isinstance(content_type, str) and content_type.startswith("multipart/form-data; boundary=")
        envelope = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + spec.body
        message = BytesParser(policy=email_policy).parsebytes(envelope)
        assert message.is_multipart()
        result = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if isinstance(name, str):
                result[name] = part.get_payload(decode=True).decode("utf-8")
        return result


class FakeRunner:
    def __init__(self, *, results=None, on_deploy=None, on_promote=None, promote_status="No active promotions"):
        self.results = results or {}
        self.on_deploy = on_deploy
        self.on_promote = on_promote
        self.promote_status = promote_status
        self.calls: list[CommandSpec] = []

    def run(self, spec: CommandSpec) -> CommandResult:
        self.calls.append(spec)
        if spec.executable == "wrangler" and spec.argv[:2] == ("pages", "deploy") and self.on_deploy:
            self.on_deploy(spec)
        if spec.executable == "vercel" and spec.argv[:1] == ("deploy",) and self.on_deploy:
            self.on_deploy(spec)
        if (spec.executable == "vercel" and spec.argv[:1] == ("promote",)
                and spec.argv[:2] != ("promote", "status") and self.on_promote):
            self.on_promote(spec)
        if spec.executable == "vercel" and spec.argv[:2] == ("promote", "status"):
            status = self.promote_status
            if isinstance(status, list):
                status = status.pop(0) if status else ""
            return CommandResult(0, status, "", False, None)
        key = (spec.executable, spec.argv[:2])
        return self.results.get(key, CommandResult(0, "https://dpl-demo.olcr-demo.vercel.app", "", False, None))


def evidence(provider_id: str, category: str) -> ProviderEvidence:
    host = "vercel.com" if provider_id == "vercel" else "developers.cloudflare.com"
    return ProviderEvidence(
        task_id="task-web-release", provider_id=provider_id, category=category,
        title=f"{provider_id} {category}", url=f"https://{host}/{category}", host=host,
        officiality="VERIFIED", accessed_at=NOW - 1,
        applicability=f"Current {provider_id} deployment requirements",
        summary="Official provider deployment documentation.",
    )


def _workspace(tmp_path: Path, *, name: str = "dist") -> tuple[Path, tuple[Path, ...]]:
    directory = tmp_path / name
    directory.mkdir(parents=True)
    index = directory / "index.html"
    index.write_text("<html>verified build</html>")
    return directory, (index,)


def cf_request(root: Path, artifacts: tuple[Path, ...], *, channel="preview", metadata=None,
               branch="feature/task7") -> ReleaseRequest:
    values = {
        "account_id": ACCOUNT_ID,
        "project_name": "olcr-demo",
        "environment": channel,
        "branch": branch,
        "build_commit": BUILD_COMMIT,
        "deployment_directory": "dist",
        "upload_mode": "direct_upload",
        "release_action": "deploy",
        "production_authorized": channel == "production",
        "target_domain": "www.example.test" if channel == "production" else None,
        "authorized_production_domains": ["www.example.test", "olcr-demo.pages.dev"],
        "local_build_validated": True,
        "credentials_configured": True,
        "agreement_accepted_by_user": True,
    }
    values.update(metadata or {})
    return ReleaseRequest(
        task_id="task-web-release", provider_id="cloudflare_pages", project_root=root,
        app_or_package="olcr-demo", version="1.0.0", channel=channel,
        artifact_paths=artifacts, metadata=values,
        authorization=ReleaseAuthorization(
            True, True, True,
            (evidence("cloudflare_pages", "publishing"), evidence("cloudflare_pages", "terms")),
        ),
    )


def vercel_request(root: Path, artifacts: tuple[Path, ...], *, channel="preview", metadata=None) -> ReleaseRequest:
    values = {
        "project_id": "prj_demo",
        "project_name": "olcr-demo",
        "team_id": "team_demo",
        "environment": "preview" if channel == "preview" else "production",
        "build_commit": BUILD_COMMIT,
        "deployment_directory": ".vercel/output",
        "release_action": "deploy",
        "production_authorized": channel != "preview",
        "target_domain": "www.example.test" if channel != "preview" else None,
        "local_build_validated": True,
        "credentials_configured": True,
        "agreement_accepted_by_user": True,
    }
    values.update(metadata or {})
    return ReleaseRequest(
        task_id="task-web-release", provider_id="vercel", project_root=root,
        app_or_package="olcr-demo", version="1.0.0", channel=channel,
        artifact_paths=artifacts, metadata=values,
        authorization=ReleaseAuthorization(
            True, True, True,
            (evidence("vercel", "publishing"), evidence("vercel", "terms")),
        ),
    )


def make_cloudflare_deployment(*, branch="feature/task7", environment="preview", commit=BUILD_COMMIT,
                               operation_id=OPERATION_ID, status="success"):
    return {
        "id": "cf-deployment-123",
        "project_id": "cf-project-123",
        "project_name": "olcr-demo",
        "environment": environment,
        "latest_stage": {"name": "deploy", "status": status},
        "url": "https://dpl123.olcr-demo.pages.dev",
        "aliases": ["https://feature-task7.olcr-demo.pages.dev"],
        "deployment_trigger": {"metadata": {
            "branch": branch,
            "commit_hash": commit,
            "commit_message": f"OLCR release operation {operation_id}",
        }},
    }


def make_vercel_deployment(*, environment="preview", commit=BUILD_COMMIT, operation_id=OPERATION_ID,
                            output_digest=None, alias_assigned=False, aliases=None, ready_state="READY",
                            deployment_id="dpl_demo"):
    return {
        "uid": deployment_id,
        "url": "dpl-demo.olcr-demo.vercel.app",
        "projectId": "prj_demo",
        "teamId": "team_demo",
        "target": None if environment == "preview" else environment,
        "state": ready_state,
        "aliasAssigned": alias_assigned,
        "aliases": aliases or [],
        "meta": {"olcrReleaseOperation": operation_id, "olcrBuildCommit": commit,
                 "olcrBuildOutputSha256": output_digest},
    }


def _vercel_output_digest(output: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in output.rglob("*") if item.is_file()):
        relative = path.relative_to(output).as_posix()
        content = path.read_bytes()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(str(len(content)).encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).hexdigest().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def vercel_deployment_from_spec(spec: CommandSpec, **overrides):
    metadata = {}
    for index, value in enumerate(spec.argv[:-1]):
        if value == "--meta":
            key, _, content = spec.argv[index + 1].partition("=")
            metadata[key] = content
    return make_vercel_deployment(
        environment="production" if "--prod" in spec.argv else "preview",
        commit=metadata.get("olcrBuildCommit"),
        operation_id=metadata.get("olcrReleaseOperation"),
        output_digest=metadata.get("olcrBuildOutputSha256"),
        **overrides,
    )


def cf_adapter(api: FakeApi, runner: FakeRunner) -> CloudflarePagesReleaseAdapter:
    return CloudflarePagesReleaseAdapter(
        api_transport=api, runner=runner,
        credential_provider=lambda profile: {
            "CLOUDFLARE_API_TOKEN": CLOUDFLARE_TOKEN,
            "CLOUDFLARE_ACCOUNT_ID": ACCOUNT_ID,
        },
        clock=lambda: NOW,
    )


def use_fake_cloudflare_hash(adapter: CloudflarePagesReleaseAdapter, monkeypatch) -> None:
    monkeypatch.setattr(
        adapter, "_pages_asset_hash",
        lambda content, path: hashlib.sha256(content + path.suffix.encode()).hexdigest()[:32],
    )


def vercel_adapter(api: FakeApi, runner: FakeRunner) -> VercelReleaseAdapter:
    return VercelReleaseAdapter(
        api_transport=api, runner=runner,
        credential_provider=lambda profile: {"VERCEL_TOKEN": VERCEL_TOKEN},
        clock=lambda: NOW,
    )


def test_cloudflare_requires_account_project_and_environment(tmp_path):
    directory, artifacts = _workspace(tmp_path)
    api, runner = FakeApi(), FakeRunner()
    request = cf_request(directory.parent, artifacts, metadata={"account_id": "", "project_name": ""})
    request = ReleaseRequest(**{**request.__dict__, "channel": "", "metadata": request.metadata})

    result = cf_adapter(api, runner).preflight(request)

    assert result.status == "BLOCKED"
    assert api.calls == []
    assert runner.calls == []


def test_cloudflare_production_branch_must_match_project_config(tmp_path):
    directory, artifacts = _workspace(tmp_path)
    api, runner = FakeApi(), FakeRunner()
    request = cf_request(directory.parent, artifacts, channel="production", branch="feature/task7")

    result = cf_adapter(api, runner).preflight(request)

    assert result.status == "BLOCKED"
    assert not any(spec.argv[:2] == ("pages", "deploy") for spec in runner.calls)


def test_cloudflare_production_requires_authorization_for_every_project_domain(tmp_path):
    directory, artifacts = _workspace(tmp_path)
    api, runner = FakeApi(), FakeRunner()
    request = cf_request(
        directory.parent, artifacts, channel="production", branch="main",
        metadata={"authorized_production_domains": ["www.example.test"]},
    )

    result = cf_adapter(api, runner).preflight(request)

    assert result.status == "BLOCKED"
    assert any("domain" in blocker.casefold() for blocker in result.blockers)
    assert not any(spec.path.endswith("/upload-token") for spec in api.calls)
    assert runner.calls == []


def test_cloudflare_production_requires_resolved_pages_dev_subdomain(tmp_path):
    directory, artifacts = _workspace(tmp_path)
    api, runner = FakeApi(), FakeRunner()
    api.cloudflare_project.pop("subdomain")
    request = cf_request(
        directory.parent, artifacts, channel="production", branch="main",
        metadata={"authorized_production_domains": ["www.example.test"]},
    )

    result = cf_adapter(api, runner).preflight(request)

    assert result.status == "BLOCKED"
    assert any("subdomain" in blocker.casefold() for blocker in result.blockers)
    assert not any(spec.path.endswith("/upload-token") for spec in api.calls)


def test_cloudflare_domain_set_change_after_asset_upload_blocks_production_deployment(tmp_path, monkeypatch):
    directory, artifacts = _workspace(tmp_path)
    api, runner = FakeApi(), FakeRunner()
    api.on_cloudflare_asset_upsert = lambda: api.cloudflare_project["domains"].append("new.example.test")
    adapter = cf_adapter(api, runner)
    use_fake_cloudflare_hash(adapter, monkeypatch)

    result = adapter.publish(
        cf_request(directory.parent, artifacts, channel="production", branch="main"), OPERATION_ID,
    )

    assert result.status == "BLOCKED"
    assert result.provider_state == "TARGET_CHANGED_BEFORE_DEPLOYMENT"
    assert not any(spec.method == "POST" and spec.path.endswith("/deployments") for spec in api.calls)


def test_cloudflare_production_domain_must_be_attached_before_deploy(tmp_path):
    directory, artifacts = _workspace(tmp_path)
    api, runner = FakeApi(), FakeRunner()
    api.cloudflare_project["domains"] = ["different.example.test"]
    request = cf_request(directory.parent, artifacts, channel="production", branch="main")

    result = cf_adapter(api, runner).preflight(request)

    assert result.status == "BLOCKED"
    assert not any(spec.argv[:2] == ("pages", "deploy") for spec in runner.calls)


def test_cloudflare_requires_explicit_account_profile_match(tmp_path):
    directory, artifacts = _workspace(tmp_path)
    api, runner = FakeApi(), FakeRunner()
    adapter = CloudflarePagesReleaseAdapter(
        api_transport=api, runner=runner,
        credential_provider=lambda profile: {
            "CLOUDFLARE_API_TOKEN": CLOUDFLARE_TOKEN,
            "CLOUDFLARE_ACCOUNT_ID": "b" * 32,
        },
        clock=lambda: NOW,
    )

    result = adapter.preflight(cf_request(directory.parent, artifacts))

    assert result.status == "BLOCKED"
    assert api.calls == []


def test_preview_does_not_promote_to_production(tmp_path, monkeypatch):
    directory, artifacts = _workspace(tmp_path)
    api = FakeApi()
    runner = FakeRunner()

    adapter = cf_adapter(api, runner)
    use_fake_cloudflare_hash(adapter, monkeypatch)
    result = adapter.publish(cf_request(directory.parent, artifacts), OPERATION_ID)

    assert result.status == "SUBMITTED"
    assert runner.calls == []
    assert api.cloudflare_deployment_manifests == [{"/index.html": api.cloudflare_uploaded_assets[0]["key"]}]
    assert api.cloudflare_uploaded_assets[0]["base64"] is True
    assert api.cloudflare_uploaded_assets[0]["metadata"]["contentType"] == "text/html"
    assert api.cloudflare_uploaded_assets[0]["value"]
    import base64
    assert base64.b64decode(api.cloudflare_uploaded_assets[0]["value"]) == artifacts[0].read_bytes()
    assert api.cloudflare_upserted_hashes == list(api.cloudflare_deployment_manifests[0].values())
    assert any(spec.path.endswith("/pages/projects/olcr-demo/upload-token") for spec in api.calls)
    asset_calls = [spec for spec in api.calls if spec.host_profile == "cloudflare_pages_assets"]
    assert len(asset_calls) == 3
    assert all(spec.auth_token == CF_UPLOAD_JWT for spec in asset_calls)
    assert all(spec.path in {
        "/client/v4/pages/assets/check-missing",
        "/client/v4/pages/assets/upload",
        "/client/v4/pages/assets/upsert-hashes",
    } for spec in asset_calls)
    deployment_call = next(spec for spec in api.calls if spec.method == "POST" and spec.path.endswith("/deployments"))
    fields = FakeApi._multipart_fields(deployment_call)
    assert fields["branch"] == "feature/task7"
    assert fields["commit_hash"] == BUILD_COMMIT
    assert fields["commit_dirty"] == "false"
    assert fields["commit_message"] == f"OLCR release operation {OPERATION_ID}"
    assert json.loads(fields["manifest"]) == api.cloudflare_deployment_manifests[0]
    assert deployment_call.path == f"/client/v4/accounts/{ACCOUNT_ID}/pages/projects/olcr-demo/deployments"
    assert deployment_call.content_type.startswith("multipart/form-data; boundary=")
    assert not any(spec.executable == "vercel" and spec.argv[:1] == ("promote",)
                   for spec in runner.calls)


def test_cloudflare_never_uses_wranger_and_only_calls_existing_project_paths(tmp_path, monkeypatch):
    directory, artifacts = _workspace(tmp_path)
    api = FakeApi()
    runner = FakeRunner()

    adapter = cf_adapter(api, runner)
    use_fake_cloudflare_hash(adapter, monkeypatch)
    result = adapter.publish(cf_request(directory.parent, artifacts), OPERATION_ID)

    assert result.status == "SUBMITTED"
    assert runner.calls == []
    assert all(spec.host_profile in {"cloudflare_pages_api", "cloudflare_pages_assets"} for spec in api.calls)
    assert not any(spec.path.rstrip("/").endswith("/pages/projects") for spec in api.calls)
    assert not any("/pages/projects/olcr-demo/settings" in spec.path for spec in api.calls)


def test_cloudflare_missing_project_blocks_without_creation_endpoint(tmp_path):
    directory, artifacts = _workspace(tmp_path)
    api, runner = FakeApi(), FakeRunner()
    api.cloudflare_project_status = 404

    result = cf_adapter(api, runner).publish(cf_request(directory.parent, artifacts), OPERATION_ID)

    assert result.status == "BLOCKED"
    assert runner.calls == []
    assert [spec.path for spec in api.calls] == [f"/client/v4/accounts/{ACCOUNT_ID}/pages/projects/olcr-demo"]


def test_cloudflare_project_deleted_after_preflight_stays_uncertain_without_fallback_create(tmp_path, monkeypatch):
    directory, artifacts = _workspace(tmp_path)
    api, runner = FakeApi(), FakeRunner()
    api.cloudflare_create_status = 404

    adapter = cf_adapter(api, runner)
    use_fake_cloudflare_hash(adapter, monkeypatch)
    result = adapter.publish(cf_request(directory.parent, artifacts), OPERATION_ID)

    assert result.status == "UNCERTAIN"
    assert runner.calls == []
    assert any(spec.method == "POST" and spec.path ==
               f"/client/v4/accounts/{ACCOUNT_ID}/pages/projects/olcr-demo/deployments" for spec in api.calls)
    assert not any(spec.method == "POST" and spec.path.rstrip("/").endswith("/pages/projects") for spec in api.calls)


def test_cloudflare_asset_api_failure_stops_before_deployment_create(tmp_path, monkeypatch):
    directory, artifacts = _workspace(tmp_path)
    api, runner = FakeApi(), FakeRunner()
    api.cloudflare_asset_upload_status = 500

    adapter = cf_adapter(api, runner)
    use_fake_cloudflare_hash(adapter, monkeypatch)
    result = adapter.publish(cf_request(directory.parent, artifacts), OPERATION_ID)

    assert result.status == "UNCERTAIN"
    assert runner.calls == []
    assert not any(spec.method == "POST" and spec.path.endswith("/deployments") for spec in api.calls)


def test_deployment_ready_state_requires_exact_provider_readback(tmp_path, monkeypatch):
    directory, artifacts = _workspace(tmp_path)
    api = FakeApi()
    api.cloudflare_created_deployment = make_cloudflare_deployment(commit="f" * 40)
    runner = FakeRunner()

    adapter = cf_adapter(api, runner)
    use_fake_cloudflare_hash(adapter, monkeypatch)
    result = adapter.publish(cf_request(directory.parent, artifacts), OPERATION_ID)

    assert result.status in {"BLOCKED", "UNCERTAIN"}
    assert result.remote_url is None


def test_cloudflare_production_requires_exact_domain_alias_readback(tmp_path, monkeypatch):
    directory, artifacts = _workspace(tmp_path)
    api = FakeApi()
    row = make_cloudflare_deployment(branch="main", environment="production")
    row["aliases"] = ["https://www.example.test"]
    api.cloudflare_created_deployment = row
    runner = FakeRunner()

    adapter = cf_adapter(api, runner)
    use_fake_cloudflare_hash(adapter, monkeypatch)
    result = adapter.publish(
        cf_request(directory.parent, artifacts, channel="production", branch="main"), OPERATION_ID,
    )

    assert result.status == "PUBLISHED", result
    assert result.remote_url == "https://dpl123.olcr-demo.pages.dev"
    assert runner.calls == []


def test_cloudflare_pages_hash_uses_wrangler_base64_plus_extension_contract():
    import blake3
    import base64

    content = b"<html>verified build</html>"
    expected = blake3.blake3((base64.b64encode(content).decode("ascii") + "html").encode("utf-8")).hexdigest()[:32]
    actual = CloudflarePagesReleaseAdapter._pages_asset_hash(content, Path("index.html"))

    assert blake3.blake3(b"abc").hexdigest() == "6437b3ac38465133ffb63b75273a8db548c558465d79db03fd359c6cd5bd9d85"
    assert actual == expected


def test_cloudflare_blocks_if_declared_blake3_runtime_dependency_is_missing(tmp_path):
    import importlib.util
    if importlib.util.find_spec("blake3") is not None:
        pytest.skip("BLAKE3 runtime dependency is installed")
    directory, artifacts = _workspace(tmp_path)
    api, runner = FakeApi(), FakeRunner()

    result = cf_adapter(api, runner).publish(cf_request(directory.parent, artifacts), OPERATION_ID)

    assert result.status == "BLOCKED"
    assert result.provider_state == "ARTIFACT_INVALID"
    assert not any(spec.path.endswith("/upload-token") for spec in api.calls)
    assert not any(spec.host_profile == "cloudflare_pages_assets" for spec in api.calls)
    assert runner.calls == []


def test_cloudflare_file_count_and_file_size_are_bounded(tmp_path, monkeypatch):
    import olcr_api.release_adapters.cloudflare_pages as module

    directory, artifacts = _workspace(tmp_path)
    request = cf_request(directory.parent, artifacts)
    monkeypatch.setattr(module, "_MAX_FILES", 1)
    extra = directory / "extra.txt"
    extra.write_text("extra")
    request = ReleaseRequest(**{**request.__dict__, "artifact_paths": (artifacts[0], extra)})
    assert cf_adapter(FakeApi(), FakeRunner()).preflight(request).status == "BLOCKED"

    monkeypatch.setattr(module, "_MAX_FILES", 20_000)
    monkeypatch.setattr(module, "_MAX_FILE_BYTES", 4)
    big = directory / "big.txt"
    big.write_text("12345")
    request = ReleaseRequest(**{**request.__dict__, "artifact_paths": (big,)})
    assert cf_adapter(FakeApi(), FakeRunner()).preflight(request).status == "BLOCKED"


def test_cloudflare_rejects_files_over_official_25_mib_limit(tmp_path):
    directory, artifacts = _workspace(tmp_path)
    large = directory / "large.js"
    with large.open("wb") as stream:
        stream.truncate(25 * 1024 * 1024 + 1)
    api, runner = FakeApi(), FakeRunner()
    request = cf_request(directory.parent, (*artifacts, large))

    result = cf_adapter(api, runner).preflight(request)

    assert result.status == "BLOCKED"
    assert api.calls == []
    assert runner.calls == []


@pytest.mark.parametrize(
    "special_path",
    [
        "_headers",
        "_redirects",
        "_routes.json",
        "_worker.js",
        "_worker.bundle",
        "functions/index.js",
        "functions-filepath-routing-config.json",
        ".DS_Store",
        "node_modules/package.json",
        "nested/.git/config",
        ".wrangler/state.json",
    ],
)
def test_cloudflare_blocks_control_and_functions_files_not_supported_by_direct_upload(tmp_path, special_path):
    directory, artifacts = _workspace(tmp_path)
    special = directory / special_path
    special.parent.mkdir(parents=True, exist_ok=True)
    special.write_text("provider control content")
    api, runner = FakeApi(), FakeRunner()
    request = cf_request(directory.parent, (*artifacts, special))

    result = cf_adapter(api, runner).preflight(request)

    assert result.status == "BLOCKED"
    assert any("unsupported" in blocker.casefold() for blocker in result.blockers)
    assert api.calls == []
    assert runner.calls == []


def test_vercel_requires_resolved_project_and_environment(tmp_path):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    api, runner = FakeApi(), FakeRunner()
    request = vercel_request(tmp_path, (index,), metadata={"project_id": "", "team_id": ""})
    request = ReleaseRequest(**{**request.__dict__, "channel": "unknown"})

    result = vercel_adapter(api, runner).preflight(request)

    assert result.status == "BLOCKED"
    assert api.calls == []
    assert runner.calls == []


def test_vercel_preflight_requires_exact_existing_project_team_and_production_domain(tmp_path):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    api, runner = FakeApi(), FakeRunner()
    api.vercel_project["accountId"] = "team_other"
    request = vercel_request(tmp_path, (index,), channel="production-stage",
                             metadata={"release_action": "stage"})

    result = vercel_adapter(api, runner).preflight(request)

    assert result.status == "BLOCKED"
    assert not any(spec.executable == "vercel" and spec.argv[:1] == ("deploy",) for spec in runner.calls)

    api.vercel_project["accountId"] = "team_demo"
    api.vercel_domains = [{"name": "other.example.test", "projectId": "prj_demo"}]
    result = vercel_adapter(api, runner).preflight(request)

    assert result.status == "BLOCKED"
    assert not any(spec.executable == "vercel" and spec.argv[:1] == ("deploy",) for spec in runner.calls)


def test_vercel_project_name_allows_official_punctuation_but_rejects_triple_hyphen(tmp_path):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    api, runner = FakeApi(), FakeRunner()
    adapter = vercel_adapter(api, runner)
    accepted_name = "olcr_demo.v1"
    api.vercel_project["name"] = accepted_name
    accepted = vercel_request(tmp_path, (index,), metadata={"project_name": accepted_name})
    accepted = ReleaseRequest(**{**accepted.__dict__, "app_or_package": accepted_name})

    assert adapter.preflight(accepted).status == "READY"

    api.calls.clear()
    rejected_name = "olcr---demo"
    rejected = vercel_request(tmp_path, (index,), metadata={"project_name": rejected_name})
    rejected = ReleaseRequest(**{**rejected.__dict__, "app_or_package": rejected_name})
    assert adapter.preflight(rejected).status == "BLOCKED"
    assert api.calls == []


def test_vercel_prebuilt_is_bound_to_exact_output_tree_and_operation_metadata(tmp_path):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    api = FakeApi()
    runner = FakeRunner(on_deploy=lambda spec: api.vercel_deployments.append(vercel_deployment_from_spec(spec)))

    result = vercel_adapter(api, runner).publish(vercel_request(tmp_path, (index,)), OPERATION_ID)

    deploy = next(spec for spec in runner.calls if spec.argv[:1] == ("deploy",))
    assert result.status == "SUBMITTED"
    assert "--project" in deploy.argv and deploy.argv[deploy.argv.index("--project") + 1] == "prj_demo"
    assert "--scope" in deploy.argv and deploy.argv[deploy.argv.index("--scope") + 1] == "team_demo"
    assert "--meta" in deploy.argv and "olcrReleaseOperation=" + OPERATION_ID in deploy.argv
    assert "olcrBuildCommit=" + BUILD_COMMIT in deploy.argv
    assert "olcrBuildOutputSha256=" + _vercel_output_digest(output) in deploy.argv


@pytest.mark.parametrize(
    ("environment", "channel", "expected_state"),
    [("preview", "preview", "PREVIEW_READY"),
     ("production", "production-stage", "PRODUCTION_STAGED")],
)
def test_vercel_reads_official_deployment_list_shape(tmp_path, environment, channel, expected_state):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    digest = _vercel_output_digest(output)
    api = FakeApi()
    api.vercel_deployments.append({
        "uid": "dpl_official_shape",
        "url": "dpl-official-shape.olcr-demo.vercel.app",
        "projectId": "prj_demo",
        "target": None if environment == "preview" else "production",
        "state": "READY",
        "meta": {"olcrReleaseOperation": OPERATION_ID, "olcrBuildCommit": BUILD_COMMIT,
                 "olcrBuildOutputSha256": digest},
    })
    request_channel = channel
    request = vercel_request(
        tmp_path, (index,), channel=request_channel,
        metadata={"release_action": "stage"} if channel == "production-stage" else None,
    )

    result = vercel_adapter(api, FakeRunner()).inspect(request, OPERATION_ID)

    assert result.status == "SUBMITTED"
    assert result.provider_state == expected_state
    assert result.remote_id == "dpl_official_shape"


def test_vercel_preview_readback_accepts_omitted_optional_target_after_preview_filter(tmp_path):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    api = FakeApi()
    api.vercel_deployments.append({
        "uid": "dpl_preview_without_optional_target",
        "url": "dpl-preview-without-target.olcr-demo.vercel.app",
        "projectId": "prj_demo",
        "state": "READY",
        "meta": {"olcrReleaseOperation": OPERATION_ID, "olcrBuildCommit": BUILD_COMMIT,
                 "olcrBuildOutputSha256": _vercel_output_digest(output)},
    })

    result = vercel_adapter(api, FakeRunner()).inspect(vercel_request(tmp_path, (index,)), OPERATION_ID)

    assert result.status == "SUBMITTED"
    deployment_list = next(spec for spec in api.calls if spec.path.startswith("/v7/deployments?"))
    assert "target=preview" in deployment_list.path


def test_vercel_prebuilt_tree_rejects_symlinks_and_oversized_files(tmp_path, monkeypatch):
    import olcr_api.release_adapters.vercel as module

    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    outside = tmp_path / "outside.html"
    outside.write_text("outside")
    link = output / "index.html"
    link.symlink_to(outside)
    api, runner = FakeApi(), FakeRunner()
    request = vercel_request(tmp_path, (link,))

    result = vercel_adapter(api, runner).preflight(request)

    assert result.status == "BLOCKED"
    assert api.calls == []
    assert runner.calls == []

    link.unlink()
    link.write_text("too large")
    monkeypatch.setattr(module, "_MAX_FILE_BYTES", 4)

    result = vercel_adapter(api, runner).preflight(request)

    assert result.status == "BLOCKED"
    assert api.calls == []
    assert runner.calls == []


@pytest.mark.parametrize("change", ["project", "team", "environment", "commit", "output"])
def test_vercel_readback_rejects_deployment_metadata_mismatch(tmp_path, change):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    api = FakeApi()

    def add_mismatched(spec):
        row = vercel_deployment_from_spec(spec)
        if change == "project":
            row["projectId"] = "prj_other"
        elif change == "team":
            row["teamId"] = "team_other"
        elif change == "environment":
            row["target"] = "production"
        elif change == "commit":
            row["meta"]["olcrBuildCommit"] = "f" * 40
        else:
            row["meta"]["olcrBuildOutputSha256"] = "f" * 64
        api.vercel_deployments.append(row)

    runner = FakeRunner(on_deploy=add_mismatched)
    result = vercel_adapter(api, runner).publish(vercel_request(tmp_path, (index,)), OPERATION_ID)

    assert result.status == "BLOCKED"
    assert result.remote_url is None


def test_vercel_timeout_with_exact_remote_readback_does_not_repeat_deploy(tmp_path):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    api = FakeApi()
    runner = FakeRunner(
        results={("vercel", ("deploy", "--prebuilt")): CommandResult(None, "", "", True, "COMMAND_TIMEOUT")},
        on_deploy=lambda spec: api.vercel_deployments.append(vercel_deployment_from_spec(spec)),
    )

    result = vercel_adapter(api, runner).publish(vercel_request(tmp_path, (index,)), OPERATION_ID)

    assert result.status == "SUBMITTED"
    assert result.provider_state == "PREVIEW_READY"
    assert sum(spec.argv[:1] == ("deploy",) for spec in runner.calls) == 1


def test_vercel_timeout_without_exact_deployment_remains_uncertain_and_is_inspected(tmp_path):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    api = FakeApi()
    runner = FakeRunner(results={
        ("vercel", ("deploy", "--prebuilt")): CommandResult(None, "", "", True, "COMMAND_TIMEOUT"),
    })
    adapter = vercel_adapter(api, runner)
    request = vercel_request(tmp_path, (index,))

    published = adapter.publish(request, OPERATION_ID)
    inspected = adapter.inspect(request, OPERATION_ID)

    assert published.status == "UNCERTAIN"
    assert inspected.status == "UNCERTAIN"
    assert sum(spec.argv[:1] == ("deploy",) for spec in runner.calls) == 1
    assert any(urlsplit(spec.path).path == "/v7/deployments" for spec in api.calls)


def test_vercel_production_alias_must_point_to_exact_deployment(tmp_path):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    api = FakeApi()
    staged = make_vercel_deployment(environment="production", output_digest=_vercel_output_digest(output),
                                    alias_assigned=False, aliases=[])
    api.vercel_deployments.append(staged)
    runner = FakeRunner(on_promote=lambda spec: setattr(api, "vercel_alias", {
        "alias": "www.example.test", "deploymentId": "dpl_other", "projectId": "prj_demo", "deletedAt": None,
    }))
    request = vercel_request(
        tmp_path, (index,), channel="production-promote",
        metadata={"release_action": "promote", "deployment_id": "dpl_demo"},
    )

    result = vercel_adapter(api, runner).publish(request, "relop_" + "3" * 64)

    assert result.status != "PUBLISHED"
    assert result.remote_url == "https://dpl-demo.olcr-demo.vercel.app"


def test_vercel_empty_promotion_status_does_not_start_promotion(tmp_path):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    api = FakeApi()
    api.vercel_deployments.append(make_vercel_deployment(
        environment="production", output_digest=_vercel_output_digest(output), alias_assigned=False, aliases=[],
    ))
    runner = FakeRunner(promote_status="")
    request = vercel_request(
        tmp_path, (index,), channel="production-promote",
        metadata={"release_action": "promote", "deployment_id": "dpl_demo"},
    )

    result = vercel_adapter(api, runner).publish(request, OPERATION_ID)

    assert result.status == "UNCERTAIN"
    assert not any(spec.argv[:1] == ("promote",) and spec.argv[:2] != ("promote", "status")
                   for spec in runner.calls)


def test_vercel_multi_domain_promotion_requires_complete_domain_authorization(tmp_path):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    api, runner = FakeApi(), FakeRunner()
    api.vercel_domains = [
        {"name": "www.example.test", "projectId": "prj_demo"},
        {"name": "example.test", "projectId": "prj_demo"},
    ]
    adapter = vercel_adapter(api, runner)
    request = vercel_request(
        tmp_path, (index,), channel="production-promote",
        metadata={"release_action": "promote", "deployment_id": "dpl_demo"},
    )

    blocked = adapter.preflight(request)

    assert blocked.status == "BLOCKED"
    assert not any(spec.argv[:1] == ("promote",) and spec.argv[:2] != ("promote", "status")
                   for spec in runner.calls)

    authorized = ReleaseRequest(**{
        **request.__dict__,
        "metadata": {**request.metadata,
                     "authorized_production_domains": ["www.example.test", "example.test"]},
    })
    assert adapter.preflight(authorized).status == "READY"


def test_vercel_preview_deploy_never_promotes(tmp_path):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    api = FakeApi()
    runner = FakeRunner(on_deploy=lambda spec: api.vercel_deployments.append(vercel_deployment_from_spec(spec)))

    result = vercel_adapter(api, runner).publish(vercel_request(tmp_path, (index,)), OPERATION_ID)

    deploy = next(spec for spec in runner.calls if spec.argv[:1] == ("deploy",))
    assert result.status == "SUBMITTED"
    assert "--prebuilt" in deploy.argv
    assert "--environment" in deploy.argv and "preview" in deploy.argv
    assert "--prod" not in deploy.argv
    assert not any(spec.argv[:1] == ("promote",) for spec in runner.calls)


def test_vercel_stage_and_promotion_are_separate_exact_id_operations(tmp_path):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    api = FakeApi()
    staged = make_vercel_deployment(environment="production", output_digest=_vercel_output_digest(output),
                                    alias_assigned=False, aliases=[])
    runner = FakeRunner(on_deploy=lambda spec: api.vercel_deployments.append(vercel_deployment_from_spec(spec)))
    adapter = vercel_adapter(api, runner)

    stage = vercel_request(tmp_path, (index,), channel="production-stage",
                           metadata={"release_action": "stage"})
    stage_result = adapter.publish(stage, OPERATION_ID)
    stage_spec = next(spec for spec in runner.calls if spec.argv[:1] == ("deploy",))
    assert stage_result.status == "SUBMITTED"
    assert "--prod" in stage_spec.argv and "--skip-domain" in stage_spec.argv

    def promote(spec):
        staged["aliasAssigned"] = True
        staged["aliases"] = ["www.example.test"]
        api.vercel_alias = {"alias": "www.example.test", "deploymentId": "dpl_demo",
                            "projectId": "prj_demo", "deletedAt": None}

    runner.on_deploy = None
    runner.on_promote = promote
    promote_request = vercel_request(
        tmp_path, (index,), channel="production-promote",
        metadata={"release_action": "promote", "deployment_id": "dpl_demo"},
    )
    promoted = adapter.publish(promote_request, "relop_" + "3" * 64)
    promote_spec = next(spec for spec in runner.calls
                        if spec.argv[:1] == ("promote",) and spec.argv[:2] != ("promote", "status"))

    assert promoted.status == "PUBLISHED"
    assert promoted.remote_url == "https://www.example.test"
    assert promote_spec.argv[1] == "dpl_demo"
    assert "--yes" in promote_spec.argv
    assert "--scope" in promote_spec.argv and promote_spec.argv[promote_spec.argv.index("--scope") + 1] == "team_demo"
    status_spec = next(spec for spec in runner.calls if spec.argv[:2] == ("promote", "status"))
    assert status_spec.argv[-2:] == ("--scope", "team_demo")


def test_vercel_pending_promotion_timeout_does_not_repeat(tmp_path):
    output = tmp_path / ".vercel" / "output"
    output.mkdir(parents=True)
    index = output / "index.html"
    index.write_text("verified")
    api = FakeApi()
    staged = make_vercel_deployment(environment="production", output_digest=_vercel_output_digest(output),
                                    alias_assigned=False, aliases=[])
    api.vercel_deployments.append(staged)
    runner = FakeRunner(
        results={("vercel", ("promote", "dpl_demo")): CommandResult(None, "", "", True, "COMMAND_TIMEOUT")},
        on_promote=lambda spec: setattr(api, "vercel_alias", {
            "alias": "www.example.test", "deploymentId": "dpl_demo", "projectId": "prj_demo", "deletedAt": None,
        }),
        promote_status=["No active promotions", "Promotion in progress", "Promotion in progress"],
    )
    adapter = vercel_adapter(api, runner)
    request = vercel_request(
        tmp_path, (index,), channel="production-promote",
        metadata={"release_action": "promote", "deployment_id": "dpl_demo"},
    )

    first = adapter.publish(request, OPERATION_ID)
    second = adapter.inspect(request, OPERATION_ID)

    assert first.status == "UNCERTAIN"
    assert second.status == "UNCERTAIN"
    assert sum(spec.argv[:1] == ("promote",) and spec.argv[:2] != ("promote", "status")
               and spec.argv[1:2] == ("dpl_demo",)
               for spec in runner.calls) == 1


def test_deployment_secrets_are_redacted_by_shared_runner(tmp_path):
    captured = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, **kwargs):
            self.stdout = io.BytesIO(VERCEL_TOKEN.encode())
            self.stderr = io.BytesIO(VERCEL_TOKEN.encode())

        def wait(self, timeout=None):
            return 0

    def process_runner(argv, **kwargs):
        captured["env"] = kwargs["env"]
        return FakeProcess(**kwargs)

    runner = ReleaseCommandRunner(
        credential_provider=lambda profile: ({"VERCEL_TOKEN": VERCEL_TOKEN} if profile == "vercel" else {
            "CLOUDFLARE_API_TOKEN": CLOUDFLARE_TOKEN, "CLOUDFLARE_ACCOUNT_ID": ACCOUNT_ID,
        }),
        executable_resolver=lambda name: f"/usr/bin/{name}", process_runner=process_runner,
    )

    result = runner.run(CommandSpec("vercel", ("--version",), tmp_path, "vercel"))

    assert VERCEL_TOKEN not in result.stdout + result.stderr
    assert CLOUDFLARE_TOKEN not in captured["env"]
    assert captured["env"]["VERCEL_TOKEN"] == VERCEL_TOKEN
