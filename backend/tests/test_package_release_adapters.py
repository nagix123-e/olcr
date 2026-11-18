from __future__ import annotations

import hashlib
import json
import io
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from olcr_api.provider_research import ProviderEvidence
from olcr_api.release_adapters import npm as npm_module
from olcr_api.release_adapters import pypi as pypi_module
from olcr_api.release_adapters.base import (
    CommandResult,
    CommandSpec,
    ProviderApiTransport,
    ProviderRequest,
    ProviderResponse,
    ReleaseCommandRunner,
)
from olcr_api.release_adapters.npm import NpmPublishAdapter
from olcr_api.release_adapters.pypi import PyPIPublishAdapter
from olcr_api.release_workflow import ReleaseAuthorization, ReleaseRequest


NOW = 1_800_000_001.0
NPM_TOKEN = "npm-secret-token-123456789"
PYPI_TOKEN = "pypi-secret-token-123456789"


class FakeApi:
    def __init__(self):
        self.calls = []
        self.npm = None
        self.pypi = None
        self.testpypi = None
        self.on_request = None

    def request(self, spec: ProviderRequest) -> ProviderResponse:
        self.calls.append(spec)
        if self.on_request:
            callback, self.on_request = self.on_request, None
            callback()
        if spec.host_profile == "npm_registry_api":
            return self._response(self.npm)
        if spec.host_profile == "pypi_api":
            return self._response(self.pypi)
        if spec.host_profile == "testpypi_api":
            return self._response(self.testpypi)
        raise AssertionError(f"unexpected registry profile {spec.host_profile}")

    @staticmethod
    def _response(value):
        if value is None:
            return ProviderResponse(404, '{"error":"not found"}', "application/json")
        return ProviderResponse(200, json.dumps(value), "application/json")


class FakeRunner:
    def __init__(self, *, results=None, on_publish=None):
        self.results = results or {}
        self.on_publish = on_publish
        self.calls = []

    def run(self, spec):
        self.calls.append(spec)
        if spec.executable == "npm" and spec.argv[:1] == ("publish",) and self.on_publish:
            self.on_publish()
        if spec.executable == "twine" and spec.argv[:1] == ("upload",) and self.on_publish:
            self.on_publish()
        key = (spec.executable, spec.argv[0] if spec.argv else "")
        if key in self.results:
            return self.results[key]
        if spec.executable == "npm" and spec.argv[:3] == ("access", "get", "status"):
            return CommandResult(0, "public", "", False, None)
        return CommandResult(0, "", "", False, None)


def evidence(provider_id: str, category: str) -> ProviderEvidence:
    host = "docs.npmjs.com" if provider_id == "npm" else "docs.pypi.org"
    return ProviderEvidence(
        task_id="task-packages",
        provider_id=provider_id,
        category=category,
        title=f"{provider_id} {category}",
        url=f"https://{host}/{category}",
        host=host,
        officiality="VERIFIED",
        accessed_at=NOW - 1,
        applicability=f"Current {provider_id} publishing requirements",
        summary="Official package registry documentation.",
    )


def npm_artifact(root: Path, *, name="widget", version="1.2.3") -> Path:
    path = root / "widget-1.2.3.tgz"
    payload = json.dumps({"name": name, "version": version}, separators=(",", ":")).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(payload)
        archive.addfile(info, BytesIO(payload))
    return path


def npm_artifact_with_extra_member(root: Path, *, extra_size: int) -> Path:
    path = root / "widget-1.2.3.tgz"
    payload = json.dumps({"name": "widget", "version": "1.2.3"}, separators=(",", ":")).encode()
    with tarfile.open(path, "w:gz") as archive:
        manifest = tarfile.TarInfo("package/package.json")
        manifest.size = len(payload)
        archive.addfile(manifest, BytesIO(payload))
        extra = tarfile.TarInfo("package/extra.bin")
        extra.size = extra_size
        archive.addfile(extra, BytesIO(b"x" * extra_size))
    return path


def npm_doc(artifact: Path, *, name="widget", version="1.2.3", tag="latest"):
    integrity = "sha512-" + __import__("base64").b64encode(
        hashlib.sha512(artifact.read_bytes()).digest(),
    ).decode("ascii")
    return {"name": name, "version": version, "dist": {"integrity": integrity},
            "dist-tags": {tag: version}}


def pypi_artifact(root: Path, *, filename="widget-1.2.3-py3-none-any.whl", version="1.2.3") -> Path:
    path = root / filename
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"widget-{version}.dist-info/METADATA", f"Metadata-Version: 2.1\nName: widget\nVersion: {version}\n\n")
        archive.writestr(f"widget-{version}.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
    return path


def pypi_sdist_with_extra_member(root: Path, *, extra_size: int) -> Path:
    path = root / "widget-1.2.3.tar.gz"
    metadata = b"Metadata-Version: 2.1\nName: widget\nVersion: 1.2.3\n\n"
    with tarfile.open(path, "w:gz") as archive:
        pkg_info = tarfile.TarInfo("widget-1.2.3/PKG-INFO")
        pkg_info.size = len(metadata)
        archive.addfile(pkg_info, BytesIO(metadata))
        extra = tarfile.TarInfo("widget-1.2.3/extra.bin")
        extra.size = extra_size
        archive.addfile(extra, BytesIO(b"x" * extra_size))
    return path


def pypi_doc(artifacts: tuple[Path, ...], *, filenames: tuple[str, ...] | None = None):
    files = []
    for index, artifact in enumerate(artifacts):
        files.append({
            "filename": filenames[index] if filenames else artifact.name,
            "digests": {"sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()},
        })
    return {"info": {"name": "widget", "version": "1.2.3"}, "urls": files}


def npm_request(root: Path, *, name="widget", version="1.2.3", registry="https://registry.npmjs.org/",
                visibility="public", tag="latest", metadata=None, target_resolved=True, artifact_path=None):
    artifact = artifact_path or npm_artifact(root, name=name, version=version)
    details = {
        "registry": registry,
        "visibility": visibility,
        "agreement_accepted_by_user": True,
        "credentials_configured": True,
        "local_build_validated": True,
    }
    details.update(metadata or {})
    return ReleaseRequest(
        task_id="task-packages", provider_id="npm", project_root=root,
        app_or_package=name, version=version, channel=tag,
        artifact_paths=(artifact,), metadata=details,
        authorization=ReleaseAuthorization(
            True, target_resolved, True, (evidence("npm", "publishing"), evidence("npm", "terms")),
        ),
    )


def pypi_request(root: Path, *, name="widget", version="1.2.3", target="pypi",
                 artifacts=None, metadata=None, target_resolved=True):
    paths = tuple(artifacts) if artifacts is not None else (
        pypi_artifact(root, filename=f"widget-{version}-py3-none-any.whl", version=version),
    )
    details = {
        "pypi_target": target,
        "agreement_accepted_by_user": True,
        "credentials_configured": True,
        "local_build_validated": True,
    }
    details.update(metadata or {})
    provider_id = "pypi"
    return ReleaseRequest(
        task_id="task-packages", provider_id=provider_id, project_root=root,
        app_or_package=name, version=version, channel=target,
        artifact_paths=paths, metadata=details,
        authorization=ReleaseAuthorization(
            True, target_resolved, True, (evidence(provider_id, "publishing"), evidence(provider_id, "terms")),
        ),
    )


def npm_adapter(api, runner, *, credential_provider=None):
    return NpmPublishAdapter(
        api_transport=api, runner=runner,
        credential_provider=credential_provider or (lambda profile: {"NODE_AUTH_TOKEN": NPM_TOKEN}),
        clock=lambda: NOW,
    )


def pypi_adapter(api, runner, *, credential_provider=None):
    return PyPIPublishAdapter(
        api_transport=api, runner=runner,
        credential_provider=credential_provider or (lambda profile: {
            "TWINE_USERNAME": "__token__", "TWINE_PASSWORD": PYPI_TOKEN,
        }),
        clock=lambda: NOW,
    )


def test_npm_requires_name_version_registry_and_visibility(tmp_path):
    request = npm_request(tmp_path, name="Bad Name", registry="https://evil.example/", visibility=None)
    api, runner = FakeApi(), FakeRunner()

    result = npm_adapter(api, runner).preflight(request)

    assert result.status == "BLOCKED"
    assert api.calls == []
    assert runner.calls == []
    assert any("package" in blocker.lower() or "registry" in blocker.lower()
               or "visibility" in blocker.lower() for blocker in result.blockers)


@pytest.mark.parametrize("name", [".foo", "_foo"])
def test_npm_rejects_unscoped_names_starting_with_dot_or_underscore(tmp_path, name):
    request = npm_request(tmp_path, name=name)
    runner = FakeRunner()

    result = npm_adapter(FakeApi(), runner).preflight(request)

    assert result.status == "BLOCKED"
    assert not any(spec.argv[:1] == ("whoami",) for spec in runner.calls)


def test_npm_allows_registry_valid_trailing_dot_name(tmp_path):
    request = npm_request(tmp_path, name="foo.")

    result = npm_adapter(FakeApi(), FakeRunner()).preflight(request)

    assert result.status == "READY"


def test_npm_allows_scoped_package_segment_starting_with_underscore(tmp_path):
    request = npm_request(tmp_path, name="@acme/_foo")

    result = npm_adapter(FakeApi(), FakeRunner()).preflight(request)

    assert result.status == "READY"


@pytest.mark.parametrize("tag", ["1", "1.x", "v1.4"])
def test_npm_rejects_semver_range_distribution_tags(tmp_path, tag):
    request = npm_request(tmp_path, tag=tag)
    runner = FakeRunner()

    result = npm_adapter(FakeApi(), runner).preflight(request)

    assert result.status == "BLOCKED"
    assert not any(spec.argv[:1] == ("whoami",) for spec in runner.calls)


def test_pypi_requires_valid_distribution_and_configured_auth(tmp_path):
    artifact = pypi_artifact(tmp_path, filename="wrong-1.2.3-py3-none-any.whl")
    request = pypi_request(tmp_path, name="widget", artifacts=(artifact,))
    api, runner = FakeApi(), FakeRunner()
    adapter = pypi_adapter(api, runner, credential_provider=lambda profile: None)

    result = adapter.preflight(request)

    assert result.status == "BLOCKED"
    assert api.calls == []
    assert runner.calls == []
    assert any("credential" in blocker.lower() or "distribution" in blocker.lower()
               or "artifact" in blocker.lower() for blocker in result.blockers)


def test_pypi_accepts_pep440_preview_version(tmp_path):
    request = pypi_request(tmp_path, version="1.2.3preview1")
    result = pypi_adapter(FakeApi(), FakeRunner()).preflight(request)

    assert result.status == "READY"


def test_npm_tarball_uncompressed_size_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(npm_module, "MAX_ARTIFACT_BYTES", 60, raising=False)
    artifact = npm_artifact_with_extra_member(tmp_path, extra_size=40)
    request = npm_request(tmp_path, artifact_path=artifact)

    result = npm_adapter(FakeApi(), FakeRunner()).preflight(request)

    assert result.status == "BLOCKED"


def test_pypi_sdist_uncompressed_size_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(pypi_module, "MAX_ARTIFACT_BYTES", 60, raising=False)
    artifact = pypi_sdist_with_extra_member(tmp_path, extra_size=20)
    request = pypi_request(tmp_path, artifacts=(artifact,))

    result = pypi_adapter(FakeApi(), FakeRunner()).preflight(request)

    assert result.status == "BLOCKED"


@pytest.mark.parametrize("provider", ["npm", "pypi"])
def test_preexisting_version_checks_artifact_digest_before_publish(tmp_path, provider):
    api, runner = FakeApi(), FakeRunner()
    if provider == "npm":
        request = npm_request(tmp_path)
        api.npm = npm_doc(request.artifact_paths[0])
        adapter = npm_adapter(api, runner)
    else:
        request = pypi_request(tmp_path)
        api.pypi = pypi_doc(request.artifact_paths)
        adapter = pypi_adapter(api, runner)

    result = adapter.publish(request, "operation-preexisting")

    assert result.status == "PUBLISHED"
    assert not any(spec.argv[:1] in {("publish",), ("upload",)} for spec in runner.calls)
    assert result.remote_url and result.remote_url.startswith("https://")


def test_npm_timeout_inspects_registry_before_retry(tmp_path):
    request = npm_request(tmp_path)
    api = FakeApi()
    runner = FakeRunner(
        results={("npm", "publish"): CommandResult(None, "", "", True, "COMMAND_TIMEOUT")},
        on_publish=lambda: setattr(api, "npm", npm_doc(request.artifact_paths[0])),
    )

    result = npm_adapter(api, runner).publish(request, "operation-npm-timeout")

    assert result.status == "PUBLISHED"
    assert sum(spec.executable == "npm" and spec.argv[:1] == ("publish",) for spec in runner.calls) == 1
    assert api.calls


def test_pypi_publish_secret_is_redacted_from_result(tmp_path):
    request = pypi_request(tmp_path)
    api = FakeApi()
    runner = FakeRunner(results={
        ("twine", "upload"): CommandResult(1, "", f"failed with {PYPI_TOKEN}", False, "UPLOAD_FAILED"),
    })

    result = pypi_adapter(api, runner).publish(request, "operation-pypi-secret")

    assert result.status in {"FAILED", "UNCERTAIN"}
    assert PYPI_TOKEN not in repr(result)
    assert runner.calls


@pytest.mark.parametrize("provider", ["npm", "pypi"])
def test_existing_different_artifact_version_is_blocked(tmp_path, provider):
    api, runner = FakeApi(), FakeRunner()
    if provider == "npm":
        request = npm_request(tmp_path)
        api.npm = {"name": "widget", "version": "1.2.3", "dist": {"integrity": "sha512-different"}}
        adapter = npm_adapter(api, runner)
    else:
        request = pypi_request(tmp_path)
        api.pypi = {
            "info": {"name": "widget", "version": "1.2.3"},
            "urls": [{"filename": request.artifact_paths[0].name,
                      "digests": {"sha256": "0" * 64}}],
        }
        adapter = pypi_adapter(api, runner)

    result = adapter.publish(request, "operation-different-artifact")

    assert result.status == "BLOCKED"
    assert not any(spec.argv[:1] in {("publish",), ("upload",)} for spec in runner.calls)


def test_npm_visibility_and_distribution_tag_are_explicit(tmp_path):
    request = npm_request(tmp_path, name="@acme/widget", visibility="restricted", tag="beta")
    runner = FakeRunner()
    result = npm_adapter(FakeApi(), runner).preflight(request)

    assert result.status == "READY"
    publish_specs = [spec for spec in runner.calls if spec.argv[:1] == ("whoami",)]
    assert publish_specs
    assert publish_specs[0].environment_profile == "npm"


def test_pypi_target_is_pinned_to_selected_official_registry(tmp_path):
    request = pypi_request(tmp_path, target="testpypi")
    api = FakeApi()
    runner = FakeRunner(on_publish=lambda: setattr(api, "testpypi", pypi_doc(request.artifact_paths)))

    result = pypi_adapter(api, runner).publish(request, "operation-testpypi-target")

    assert result.status == "PUBLISHED"
    assert any(spec.executable == "twine" and spec.argv[:1] == ("check",) for spec in runner.calls)
    upload = next(spec for spec in runner.calls if spec.argv[:1] == ("upload",))
    assert "https://test.pypi.org/legacy/" in upload.argv
    assert api.calls
    assert all(spec.host_profile == "testpypi_api" for spec in api.calls)


def test_npm_publish_uses_explicit_registry_access_and_distribution_tag(tmp_path):
    request = npm_request(tmp_path, name="@acme/widget", visibility="restricted", tag="beta")
    api, runner = FakeApi(), FakeRunner(on_publish=lambda: setattr(api, "npm", npm_doc(
        request.artifact_paths[0], name=request.app_or_package, tag=request.channel,
    )), results={("npm", "access"): CommandResult(0, "private", "", False, None)})

    result = npm_adapter(api, runner).publish(request, "operation-npm-exact-flags")

    assert result.status == "PUBLISHED"
    spec = next(call for call in runner.calls if call.argv[:1] == ("publish",))
    assert spec.environment_profile == "npm"
    assert "--registry" in spec.argv and "https://registry.npmjs.org/" in spec.argv
    assert "--access" in spec.argv and "restricted" in spec.argv
    assert "--tag" in spec.argv and "beta" in spec.argv
    assert "--ignore-scripts" in spec.argv


@pytest.mark.parametrize("mismatch", ["tag", "visibility"])
def test_npm_readback_requires_requested_distribution_tag_and_visibility(tmp_path, mismatch):
    requested_tag = "beta"
    request = npm_request(tmp_path, tag=requested_tag)
    api = FakeApi()
    api.npm = npm_doc(request.artifact_paths[0], tag="latest" if mismatch == "tag" else requested_tag)
    runner = FakeRunner(results={
        ("npm", "access"): CommandResult(0, "private", "", False, None),
    } if mismatch == "visibility" else None)

    result = npm_adapter(api, runner).publish(request, "operation-npm-readback-mismatch")

    assert result.status == "BLOCKED"
    assert not any(spec.argv[:1] == ("publish",) for spec in runner.calls)


@pytest.mark.parametrize("provider", ["npm", "pypi"])
def test_package_artifact_is_revalidated_after_remote_precheck(tmp_path, provider):
    api, runner = FakeApi(), FakeRunner()
    if provider == "npm":
        request = npm_request(tmp_path)
        adapter = npm_adapter(api, runner)
    else:
        request = pypi_request(tmp_path)
        adapter = pypi_adapter(api, runner)
    artifact = request.artifact_paths[0]
    api.on_request = lambda: artifact.write_bytes(b"changed while registry precheck was running")

    result = adapter.publish(request, "operation-artifact-mutated")

    assert result.status == "BLOCKED"
    assert result.provider_state == "ARTIFACT_CHANGED_BEFORE_UPLOAD"
    assert not any(spec.argv[:1] in {("publish",), ("upload",)} for spec in runner.calls)


def test_npm_restricted_visibility_requires_scoped_name(tmp_path):
    request = npm_request(tmp_path, name="widget", visibility="restricted")
    runner = FakeRunner()

    result = npm_adapter(FakeApi(), runner).preflight(request)

    assert result.status == "BLOCKED"
    assert not any(call.argv[:1] == ("whoami",) for call in runner.calls)


def test_npm_provenance_requires_supported_configured_ci_runtime(tmp_path):
    request = npm_request(tmp_path, metadata={"provenance": True})
    runner = FakeRunner()

    result = npm_adapter(FakeApi(), runner).preflight(request)

    assert result.status == "BLOCKED"
    assert not runner.calls


def test_provider_api_profiles_pin_registry_hosts():
    captured = []

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self, size=-1):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def __init__(self, host):
            self.host = host

        def open(self, request, timeout):
            captured.append((self.host, request.full_url, request.get_header("Authorization")))
            return Response()

    transport = ProviderApiTransport(
        opener_factory=lambda host, allowed_hosts: Opener(host),
    )
    paths = {
        "npm_registry_api": ("registry.npmjs.org", "/widget/1.2.3"),
        "pypi_api": ("pypi.org", "/pypi/widget/1.2.3/json"),
        "testpypi_api": ("test.pypi.org", "/pypi/widget/1.2.3/json"),
    }

    for profile, (host, path) in paths.items():
        assert transport.request(ProviderRequest(profile, "GET", path)).status == 200
        assert captured[-1][0] == host
        assert captured[-1][1] == f"https://{host}{path}"
        assert captured[-1][2] is None


def test_command_runner_profiles_redact_npm_and_pypi_credentials(tmp_path):
    captured = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, output, **kwargs):
            self.stdout = io.BytesIO(output.encode())
            self.stderr = io.BytesIO(b"")

        def wait(self, timeout=None):
            return self.returncode

    def process_runner(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        secrets = [kwargs["env"].get("NODE_AUTH_TOKEN"), kwargs["env"].get("TWINE_PASSWORD")]
        output = " ".join(f"credential={value}" for value in secrets if value)
        return FakeProcess(output, **kwargs)

    def credentials(profile):
        if profile == "npm":
            return {"NODE_AUTH_TOKEN": NPM_TOKEN, "UNSAFE_ENV": "not-allowed"}
        if profile == "pypi":
            return {"TWINE_USERNAME": "__token__", "TWINE_PASSWORD": PYPI_TOKEN}
        return None

    runner = ReleaseCommandRunner(
        credential_provider=credentials,
        executable_resolver=lambda name: f"/usr/bin/{name}",
        process_runner=process_runner,
    )
    result = runner.run(CommandSpec("npm", ("whoami",), tmp_path, "npm"))

    assert result.returncode == 0
    assert NPM_TOKEN not in result.stdout
    assert PYPI_TOKEN not in result.stdout
    assert captured["env"]["NODE_AUTH_TOKEN"] == NPM_TOKEN
    assert "UNSAFE_ENV" not in captured["env"]

    pypi_result = runner.run(CommandSpec("twine", ("check",), tmp_path, "pypi"))
    assert pypi_result.returncode == 0
    assert PYPI_TOKEN not in pypi_result.stdout
    assert captured["env"]["TWINE_USERNAME"] == "__token__"
    assert captured["env"]["TWINE_PASSWORD"] == PYPI_TOKEN


@pytest.mark.parametrize("provider", ["npm", "pypi"])
def test_package_artifact_symlink_is_blocked(tmp_path, provider):
    if provider == "npm":
        request = npm_request(tmp_path)
        external = tmp_path.parent / "external-package.tgz"
        external.write_bytes(request.artifact_paths[0].read_bytes())
        link = tmp_path / "linked.tgz"
        link.symlink_to(external)
        request = ReleaseRequest(**{**request.__dict__, "artifact_paths": (link,)})
        adapter = npm_adapter(FakeApi(), FakeRunner())
    else:
        request = pypi_request(tmp_path)
        external = tmp_path.parent / "external-package.whl"
        external.write_bytes(request.artifact_paths[0].read_bytes())
        link = tmp_path / "linked.whl"
        link.symlink_to(external)
        request = ReleaseRequest(**{**request.__dict__, "artifact_paths": (link,)})
        adapter = pypi_adapter(FakeApi(), FakeRunner())

    result = adapter.preflight(request)

    assert result.status == "BLOCKED"
