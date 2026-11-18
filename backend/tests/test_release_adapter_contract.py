from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from olcr_api.release_adapters.base import (
    CommandSpec,
    ProviderApiTransport,
    ProviderRequest,
    ProviderResponse,
    ReleaseAdapter,
    ReleaseCommandRunner,
    ReleaseResult,
)
from olcr_api.coding_tasks import ReleasePreflight


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, captured=None):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.captured = captured if captured is not None else {}

    def wait(self, timeout=None):
        self.captured["wait_timeout"] = timeout
        return self.returncode

    def kill(self):
        self.captured["killed"] = True


class CompleteAdapter:
    provider_id = "github"

    def preflight(self, request):
        return ReleasePreflight("READY", [], [])

    def publish(self, request, operation_id):
        return ReleaseResult("SUBMITTED", "submitted", None, operation_id, "queued", True)

    def inspect(self, request, operation_id):
        return ReleaseResult("PUBLISHED", "published", "https://github.com/acme/widget/releases/tag/v1.2.3", "8", "confirmed", False)


class MissingInspectAdapter:
    provider_id = "github"

    def preflight(self, request):
        return ReleasePreflight("READY", [], [])

    def publish(self, request, operation_id):
        return ReleaseResult("SUBMITTED", "submitted", None, operation_id, "queued", True)


def test_adapter_protocol_requires_preflight_publish_and_inspect():
    assert isinstance(CompleteAdapter(), ReleaseAdapter)
    assert not isinstance(MissingInspectAdapter(), ReleaseAdapter)


def test_runner_never_uses_shell_and_filters_environment(tmp_path, monkeypatch):
    tmp_path.mkdir(exist_ok=True)
    monkeypatch.setenv("BASH_ENV", "/tmp/unsafe.sh")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/unsafe.dylib")
    captured = {}

    def process_runner(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return FakeProcess(b"ok", b"", 0, captured)

    runner = ReleaseCommandRunner(
        credential_provider=lambda profile: {
            "GH_TOKEN": "ghp_example_secret_0123456789",
            "UNSAFE_ENV": "should-not-pass",
        },
        executable_resolver=lambda name: f"/usr/bin/{name}",
        process_runner=process_runner,
    )
    result = runner.run(CommandSpec(
        executable="gh",
        argv=("auth", "status", "--hostname", "github.com"),
        cwd=tmp_path,
        environment_profile="github",
        timeout_seconds=10,
    ))

    assert result.returncode == 0
    assert result.stdout == "ok"
    assert captured["shell"] is False
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE
    assert "GH_TOKEN" in captured["env"]
    assert "UNSAFE_ENV" not in captured["env"]
    assert "BASH_ENV" not in captured["env"]
    assert "LD_PRELOAD" not in captured["env"]
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["wait_timeout"] == 10


def test_runner_rejects_executables_outside_the_fixed_allowlist(tmp_path):
    runner = ReleaseCommandRunner(executable_resolver=lambda name: f"/usr/bin/{name}")
    spec = CommandSpec("sh", ("-c", "echo unsafe"), tmp_path, "github")
    with pytest.raises(ValueError, match="EXECUTABLE_NOT_ALLOWED"):
        runner.run(spec)


def test_runner_redacts_credentials_from_output_and_error(tmp_path):
    secret = "ghp_example_secret_0123456789"

    def process_runner(argv, **kwargs):
        return FakeProcess(f"token={secret}".encode(), f"Bearer {secret}".encode(), 1)

    runner = ReleaseCommandRunner(
        credential_provider=lambda profile: {"GH_TOKEN": secret},
        executable_resolver=lambda name: f"/usr/bin/{name}",
        process_runner=process_runner,
    )
    result = runner.run(CommandSpec("gh", ("auth", "status"), tmp_path, "github"))
    assert result.returncode == 1
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert "[REDACTED]" in result.stdout
    assert "[REDACTED]" in result.stderr


def test_runner_discards_captured_content_when_output_exceeds_limit(tmp_path):
    def process_runner(argv, **kwargs):
        return FakeProcess(b"x" * 4096, b"y" * 4096, 0)

    runner = ReleaseCommandRunner(
        executable_resolver=lambda name: f"/usr/bin/{name}",
        process_runner=process_runner,
        max_output_bytes=8,
    )
    result = runner.run(CommandSpec("gh", ("auth", "status"), tmp_path, "github"))

    assert result.stdout == "[OUTPUT TRUNCATED]"
    assert result.stderr == "[OUTPUT TRUNCATED]"


class FakeResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, body=b'{"ok":true}'):
        self._body = body

    def read(self, size=-1):
        if size < 0:
            return self._body
        return self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


def test_api_transport_restricts_method_and_official_host():
    opener = FakeOpener()
    profiles = []
    transport = ProviderApiTransport(
        credential_provider=lambda profile: profiles.append(profile) or "ghp_api_secret_0123456789",
        opener_factory=lambda host, allowed_hosts: (profiles.append((host, allowed_hosts)) or opener),
    )
    response = transport.request(ProviderRequest(
        host_profile="github_api",
        method="GET",
        path="/repos/acme/widget/releases/tags/v1.2.3",
    ))
    request, timeout = opener.requests[0]
    assert response == ProviderResponse(200, '{"ok":true}', "application/json")
    assert request.full_url == "https://api.github.com/repos/acme/widget/releases/tags/v1.2.3"
    assert request.get_method() == "GET"
    assert request.has_header("Authorization")
    assert timeout <= 30
    assert ("api.github.com", ("api.github.com",)) in profiles
    assert "api.github.com" in request.full_url

    for invalid in (
        ProviderRequest("github.com", "GET", "/repos/acme/widget"),
        ProviderRequest("github_api", "OPTIONS", "/repos/acme/widget"),
        ProviderRequest("github_api", "GET", "https://evil.test/repos/acme/widget"),
        ProviderRequest("github_api", "GET", "//evil.test/repos/acme/widget"),
    ):
        with pytest.raises(ValueError):
            transport.request(invalid)


def test_api_transport_redacts_provider_credentials_and_bounds_response():
    secret = "github_pat_secret_0123456789"
    opener = FakeOpener(FakeResponse(f'{{"message":"token={secret}"}}'.encode()))
    transport = ProviderApiTransport(
        credential_provider=lambda profile: secret,
        opener_factory=lambda host, allowed_hosts: opener,
    )
    result = transport.request(ProviderRequest("github_api", "GET", "/repos/acme/widget"))
    assert secret not in result.body
    assert "[REDACTED]" in result.body

    large = ProviderApiTransport(
        opener_factory=lambda host, allowed_hosts: FakeOpener(FakeResponse(b"x" * 32)),
        max_response_bytes=8,
    )
    with pytest.raises(ValueError, match="RESPONSE_TOO_LARGE"):
        large.request(ProviderRequest("github_api", "GET", "/repos/acme/widget"))


def test_cloudflare_asset_transport_uses_ephemeral_jwt_content_type_and_large_body():
    jwt = "eyJhbGciOiJIUzI1NiJ9.pages-upload-jwt"
    payload = {"hashes": ["a" * 32]}
    opener = FakeOpener()
    transport = ProviderApiTransport(
        credential_provider=lambda profile: pytest.fail("asset JWT must not be loaded from credentials"),
        opener_factory=lambda host, allowed_hosts: opener,
        max_request_bytes=40 * 1024 * 1024,
    )

    response = transport.request(ProviderRequest(
        host_profile="cloudflare_pages_assets",
        method="POST",
        path="/client/v4/pages/assets/check-missing",
        body=payload,
        auth_token=jwt,
    ))

    request, _timeout = opener.requests[0]
    assert response.status == 200
    assert request.get_header("Authorization") == f"Bearer {jwt}"
    assert request.get_header("Content-type") == "application/json"
    assert request.data == b'{"hashes":["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]}'
    assert jwt not in response.body


def test_api_transport_allows_typed_multipart_only_for_cloudflare_deployment():
    body = b"--olcr-boundary\r\nContent-Disposition: form-data; name=\"manifest\"\r\n\r\n{}\r\n--olcr-boundary--\r\n"
    opener = FakeOpener()
    transport = ProviderApiTransport(
        credential_provider=lambda profile: "cf-api-token-123456789",
        opener_factory=lambda host, allowed_hosts: opener,
    )

    transport.request(ProviderRequest(
        host_profile="cloudflare_pages_api",
        method="POST",
        path="/client/v4/accounts/" + "a" * 32 + "/pages/projects/site/deployments",
        body=body,
        content_type="multipart/form-data; boundary=olcr-boundary",
    ))

    request, _timeout = opener.requests[0]
    assert request.get_header("Content-type") == "multipart/form-data; boundary=olcr-boundary"
    with pytest.raises(ValueError):
        transport.request(ProviderRequest(
            host_profile="github_api",
            method="POST",
            path="/repos/acme/widget/releases",
            body=body,
            content_type="multipart/form-data; boundary=olcr-boundary",
        ))
    for method, path in (
        ("POST", "/client/v4/accounts/" + "a" * 32 + "/pages/projects"),
        ("GET", "/client/v4/accounts/" + "a" * 32 + "/pages/projects/site/deployments"),
    ):
        with pytest.raises(ValueError, match="REQUEST_CONTENT_TYPE_NOT_ALLOWED"):
            transport.request(ProviderRequest(
                host_profile="cloudflare_pages_api",
                method=method,
                path=path,
                body=body,
                content_type="multipart/form-data; boundary=olcr-boundary",
            ))
