from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from olcr_api.coding_tasks import ReleasePreflight
from olcr_api.provider_research import ProviderEvidence
from olcr_api.release_adapters.base import CommandResult, ProviderRequest, ProviderResponse, ReleaseCommandRunner
from olcr_api.release_adapters.github import GitHubReleaseAdapter
from olcr_api.release_workflow import ReleaseAuthorization, ReleaseRequest, ReleaseResult


BUILD_COMMIT = "a" * 40
ANNOTATED_TAG_SHA = "b" * 40


class FakeGit:
    def __init__(self, urls=None, *, head=BUILD_COMMIT):
        self.urls = urls if urls is not None else {"origin": ["https://github.com/acme/widget.git"]}
        self.head = head
        self.calls = []

    def _run(self, root, args, *, ok=(0,), timeout=30):
        self.calls.append(tuple(args))
        if args == ["remote"]:
            return "\n".join(self.urls) + ("\n" if self.urls else "")
        if args == ["rev-parse", "--verify", "HEAD"]:
            return self.head
        if len(args) >= 5 and args[:3] == ["remote", "get-url", "--push"]:
            name = args[-1]
            return "\n".join(self.urls.get(name, []))
        return ""


class FakeRunner:
    def __init__(self, *, authenticated=True, on_create=None, create_result=None):
        self.authenticated = authenticated
        self.on_create = on_create
        self.create_result = create_result or CommandResult(0, "", "", False, None)
        self.calls = []

    def run(self, spec):
        self.calls.append(spec)
        if spec.argv[:2] == ("auth", "status"):
            return CommandResult(0 if self.authenticated else 1, "", "", False,
                                 None if self.authenticated else "AUTH_REQUIRED")
        if spec.argv[:2] == ("release", "create") and self.on_create:
            self.on_create()
        if spec.argv[:2] == ("release", "create"):
            return self.create_result
        return CommandResult(0, "", "", False, None)


class FakeApi:
    def __init__(self, release_provider=None, *, tag_exists=True, tag_commit=BUILD_COMMIT,
                 annotated_tag=False, on_release_read=None):
        self.release_provider = release_provider or (lambda: None)
        self.tag_exists = tag_exists
        self.tag_commit = tag_commit
        self.annotated_tag = annotated_tag
        self.on_release_read = on_release_read
        self.calls = []

    def request(self, spec):
        self.calls.append(spec)
        if spec.path.endswith("/git/ref/tags/v1.2.3"):
            if self.tag_exists:
                object_type = "tag" if self.annotated_tag else "commit"
                object_sha = ANNOTATED_TAG_SHA if self.annotated_tag else self.tag_commit
                payload = {"ref": "refs/tags/v1.2.3", "object": {"type": object_type, "sha": object_sha}}
                return ProviderResponse(200, json.dumps(payload), "application/json")
            return ProviderResponse(404, '{"message":"Not Found"}', "application/json")
        if spec.path.endswith(f"/git/tags/{ANNOTATED_TAG_SHA}") and self.annotated_tag:
            return ProviderResponse(200, json.dumps({"object": {"type": "commit", "sha": self.tag_commit}}), "application/json")
        if spec.path.endswith("/releases/tags/v1.2.3"):
            if self.on_release_read:
                self.on_release_read()
            release = self.release_provider()
            if release is None:
                return ProviderResponse(404, '{"message":"Not Found"}', "application/json")
            return ProviderResponse(200, json.dumps(release), "application/json")
        raise AssertionError(f"Unexpected provider API path: {spec.path}")


def evidence(task_id="task-1", category="publishing"):
    return ProviderEvidence(
        task_id=task_id,
        provider_id="github",
        category=category,
        title=f"GitHub {category}",
        url=f"https://docs.github.com/{category}",
        host="docs.github.com",
        officiality="VERIFIED",
        accessed_at=1_800_000_000.0,
        applicability="Current GitHub release",
        summary="Current official source.",
    )


def request_for(tmp_path, *, target="acme/widget", version="1.2.3", channel="stable", artifacts=None, external=True):
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    artifact = root / "dist.zip"
    artifact.write_bytes(b"release bytes")
    return ReleaseRequest(
        task_id="task-1",
        provider_id="github",
        project_root=root,
        app_or_package=target,
        version=version,
        channel=channel,
        artifact_paths=tuple(artifacts) if artifacts is not None else (artifact,),
        metadata={
            "remote_name": "origin",
            "build_commit": BUILD_COMMIT,
            "agreement_accepted_by_user": True,
            "credentials_configured": True,
            "local_build_validated": True,
        },
        authorization=ReleaseAuthorization(True, True, external, (evidence(), evidence(category="terms"))),
    )


def release_for(request, *, draft=False, prerelease=False, tag="v1.2.3", url="https://github.com/acme/widget/releases/tag/v1.2.3", remote_id=42):
    digest = hashlib.sha256(request.artifact_paths[0].read_bytes()).hexdigest()
    return {
        "tag_name": tag,
        "html_url": url,
        "id": remote_id,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [{"name": "dist.zip", "digest": f"sha256:{digest}"}],
    }


def adapter_for(request, *, git=None, runner=None, api=None):
    return GitHubReleaseAdapter(
        git_workspace=git or FakeGit(),
        runner=runner or FakeRunner(),
        api_transport=api or FakeApi(),
        clock=lambda: 1_800_000_001.0,
    )


def test_github_preflight_requires_single_github_remote_valid_tag_and_workspace_artifacts(tmp_path):
    request = request_for(tmp_path)
    adapter = adapter_for(request, api=FakeApi(tag_exists=True))
    result = adapter.preflight(request)
    assert result.status == "READY"
    assert any(check["name"] == "repository" and check["status"] == "PASS" for check in result.checks)
    assert any(check["name"] == "tag" and check["status"] == "PASS" for check in result.checks)
    assert any(check["name"] == "artifact" and check["status"] == "PASS" for check in result.checks)


def test_github_preflight_resolves_annotated_tag_to_build_commit(tmp_path):
    request = request_for(tmp_path)
    api = FakeApi(annotated_tag=True)
    result = adapter_for(request, api=api).preflight(request)

    assert result.status == "READY"
    assert any(spec.path.endswith(f"/git/tags/{ANNOTATED_TAG_SHA}") for spec in api.calls)


def test_github_preflight_blocks_tag_that_points_to_another_commit(tmp_path):
    request = request_for(tmp_path)
    api = FakeApi(tag_commit="c" * 40)
    result = adapter_for(request, api=api).preflight(request)

    assert result.status == "BLOCKED"
    assert any("commit" in blocker.lower() for blocker in result.blockers)


def test_github_preflight_blocks_build_commit_outside_current_workspace_head(tmp_path):
    request = request_for(tmp_path)
    api = FakeApi()
    result = adapter_for(request, git=FakeGit(head="c" * 40), api=api).preflight(request)

    assert result.status == "BLOCKED"
    assert any("current Git commit" in blocker for blocker in result.blockers)
    assert api.calls == []


def test_external_access_off_stops_before_auth_or_provider_calls(tmp_path):
    request = request_for(tmp_path, external=False)
    runner = FakeRunner()
    api = FakeApi()
    result = adapter_for(request, runner=runner, api=api).preflight(request)

    assert result.status == "BLOCKED"
    assert runner.calls == []
    assert api.calls == []


def test_api_transport_reuses_existing_github_credential_provider(tmp_path):
    secret = "ghp_private_repo_token_0123456789"
    captured = {}

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self, size=-1):
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def open(self, request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return Response()

    runner = ReleaseCommandRunner(credential_provider=lambda profile: {"GH_TOKEN": secret})
    request = request_for(tmp_path)
    adapter = GitHubReleaseAdapter(git_workspace=FakeGit(), runner=runner)
    adapter.api_transport.opener_factory = lambda host, allowed_hosts: Opener()

    response = adapter.api_transport.request(ProviderRequest(
        "github_api", "GET", "/repos/acme/widget/git/ref/tags/v1.2.3",
    ))

    assert captured["authorization"] == f"Bearer {secret}"
    assert captured["timeout"] <= 30
    assert secret not in response.body
    assert response == ProviderResponse(200, '{"ok":true}', "application/json")


@pytest.mark.parametrize("field", ["explicit_intent", "target_resolved", "external_access_enabled"])
def test_non_boolean_authorization_facts_do_not_open_release_gates(tmp_path, field):
    request = request_for(tmp_path)
    auth = request.authorization
    values = {
        "explicit_intent": auth.explicit_intent,
        "target_resolved": auth.target_resolved,
        "external_access_enabled": auth.external_access_enabled,
    }
    values[field] = "false"
    request = ReleaseRequest(
        task_id=request.task_id,
        provider_id=request.provider_id,
        project_root=request.project_root,
        app_or_package=request.app_or_package,
        version=request.version,
        channel=request.channel,
        artifact_paths=request.artifact_paths,
        metadata=request.metadata,
        authorization=ReleaseAuthorization(
            values["explicit_intent"], values["target_resolved"],
            values["external_access_enabled"], auth.current_references,
        ),
    )
    runner = FakeRunner()
    api = FakeApi()

    result = adapter_for(request, runner=runner, api=api).preflight(request)

    assert result.status == "BLOCKED"
    assert runner.calls == []
    assert api.calls == []


@pytest.mark.parametrize(
    "urls,target,artifact_outside,tag_exists",
    [
        ({"origin": ["https://github.com/acme/widget.git"], "backup": ["https://github.com/acme/other.git"]}, "acme/widget", False, True),
        ({"origin": ["https://evil.test/acme/widget.git"]}, "acme/widget", False, True),
        ({"origin": ["https://github.com/acme/widget.git"]}, "acme/other", False, True),
        ({"origin": ["https://github.com/acme/widget.git"]}, "acme/widget", False, False),
    ],
)
def test_github_preflight_blocks_ambiguous_remote_target_or_tag(tmp_path, urls, target, artifact_outside, tag_exists):
    request = request_for(tmp_path, target=target)
    adapter = adapter_for(
        request,
        git=FakeGit(urls),
        api=FakeApi(tag_exists=tag_exists),
    )
    result = adapter.preflight(request)
    assert result.status == "BLOCKED"
    assert result.blockers


def test_github_preflight_blocks_artifact_outside_workspace(tmp_path):
    request = request_for(tmp_path)
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"outside")
    request = request_for(tmp_path, artifacts=(outside,))
    result = adapter_for(request).preflight(request)
    assert result.status == "BLOCKED"
    assert any("workspace" in blocker.lower() or "artifact" in blocker.lower() for blocker in result.blockers)


def test_github_preflight_rejects_symlink_artifact_paths(tmp_path):
    request = request_for(tmp_path)
    inside = request.project_root / "inside.zip"
    inside.write_bytes(b"inside bytes")
    linked = request.project_root / "linked.zip"
    linked.symlink_to(inside)
    request = request_for(tmp_path, artifacts=(linked,))
    runner = FakeRunner()
    api = FakeApi()

    result = adapter_for(request, runner=runner, api=api).preflight(request)

    assert result.status == "BLOCKED"
    assert runner.calls == []
    assert api.calls == []


def test_existing_release_is_inspected_before_create(tmp_path):
    request = request_for(tmp_path)
    release = release_for(request)
    runner = FakeRunner()
    api = FakeApi(lambda: release)
    adapter = adapter_for(request, runner=runner, api=api)

    result = adapter.publish(request, "operation-1")

    assert result.status == "PUBLISHED"
    assert result.remote_id == "42"
    assert result.remote_url == release["html_url"]
    assert not any(spec.argv[:2] == ("release", "create") for spec in runner.calls)


def test_existing_release_with_different_artifact_digest_blocks_without_create(tmp_path):
    request = request_for(tmp_path)
    release = release_for(request)
    release["assets"][0]["digest"] = "sha256:" + "0" * 64
    runner = FakeRunner()
    adapter = adapter_for(request, runner=runner, api=FakeApi(lambda: release))

    result = adapter.publish(request, "operation-1")

    assert result.status == "BLOCKED"
    assert any(spec.argv[:2] == ("release", "create") for spec in runner.calls) is False


def test_existing_release_with_duplicate_asset_name_is_not_confirmed(tmp_path):
    request = request_for(tmp_path)
    release = release_for(request)
    release["assets"].append(dict(release["assets"][0]))
    runner = FakeRunner()
    adapter = adapter_for(request, runner=runner, api=FakeApi(lambda: release))

    result = adapter.publish(request, "operation-1")

    assert result.status == "BLOCKED"
    assert not any(spec.argv[:2] == ("release", "create") for spec in runner.calls)


def test_release_create_uses_verify_tag_and_remote_readback(tmp_path):
    request = request_for(tmp_path)
    state = {"created": False}
    release = release_for(request)
    api = FakeApi(lambda: release if state["created"] else None)
    runner = FakeRunner(on_create=lambda: state.__setitem__("created", True))
    adapter = adapter_for(request, runner=runner, api=api)

    result = adapter.publish(request, "operation-1")

    create = next(spec for spec in runner.calls if spec.argv[:2] == ("release", "create"))
    assert "--verify-tag" in create.argv
    assert [Path(arg).name for arg in create.argv if Path(arg).is_absolute()] == ["dist.zip"]
    assert result.status == "PUBLISHED"
    assert result.remote_url == release["html_url"]
    assert any(spec.path.endswith("/releases/tags/v1.2.3") for spec in api.calls)


def test_release_readback_blocks_if_tag_moves_after_create(tmp_path):
    request = request_for(tmp_path)
    state = {"created": False}
    release = release_for(request)
    api = FakeApi(lambda: release if state["created"] else None)

    def move_tag():
        state["created"] = True
        api.tag_commit = "c" * 40

    runner = FakeRunner(on_create=move_tag)
    result = adapter_for(request, runner=runner, api=api).publish(request, "operation-1")

    assert result.status == "BLOCKED"
    assert result.provider_state == "TAG_CONFLICT"


def test_release_creation_stops_if_tag_moves_after_absence_check(tmp_path):
    request = request_for(tmp_path)
    api = FakeApi()
    api.on_release_read = lambda: setattr(api, "tag_commit", "c" * 40)
    runner = FakeRunner()

    result = adapter_for(request, runner=runner, api=api).publish(request, "operation-1")

    assert result.status == "BLOCKED"
    assert result.provider_state == "TAG_CONFLICT"
    assert not any(spec.argv[:2] == ("release", "create") for spec in runner.calls)


def test_release_readback_rechecks_tag_after_fetching_release_assets(tmp_path):
    request = request_for(tmp_path)
    release = release_for(request)
    api = FakeApi(lambda: release)
    api.on_release_read = lambda: setattr(api, "tag_commit", "c" * 40)
    adapter = adapter_for(request, api=api)

    result = adapter.inspect(request, "operation-1")

    assert result.status == "BLOCKED"
    assert result.provider_state == "TAG_CONFLICT"


def test_missing_gh_auth_blocks_without_reading_token(tmp_path):
    request = request_for(tmp_path)
    runner = FakeRunner(authenticated=False)
    api = FakeApi()
    adapter = adapter_for(request, runner=runner, api=api)

    result = adapter.preflight(request)

    assert result.status == "BLOCKED"
    assert result.blockers
    assert api.calls == []
    assert runner.calls[0].argv[:2] == ("auth", "status")
