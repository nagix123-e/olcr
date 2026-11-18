from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from olcr_api.provider_research import ProviderEvidence
from olcr_api.release_adapters.base import ProviderApiTransport, ProviderRequest, ProviderResponse
from olcr_api.release_adapters.google_play import GooglePlayMediaUploadTransport, GooglePlayReleaseAdapter
from olcr_api.release_workflow import ReleaseAuthorization, ReleaseRequest


NOW = 1_800_000_001.0
PACKAGE = "com.example.widget"
EDIT_ID = "edit-upload-123"
READBACK_EDIT_ID = "edit-readback-123"
VERSION_CODE = "42"


class FakeApi:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, spec):
        self.calls.append(spec)
        return self.handler(spec)


class FakeUpload:
    def __init__(self, api, *, status=200, version_code=VERSION_CODE):
        self.api = api
        self.status = status
        self.version_code = version_code
        self.calls = []

    def upload(self, package_name, edit_id, artifact_type, path, access_token):
        self.calls.append((package_name, edit_id, artifact_type, Path(path), access_token))
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        self.api.uploaded = {"versionCode": int(self.version_code)}
        if artifact_type == "apk":
            self.api.uploaded["binary"] = {"sha256": digest}
        else:
            self.api.uploaded["sha256"] = digest
        return ProviderResponse(self.status, json.dumps(self.api.uploaded), "application/json")


def evidence(category: str) -> ProviderEvidence:
    return ProviderEvidence(
        task_id="task-play",
        provider_id="google_play",
        category=category,
        title=f"Google Play {category}",
        url=f"https://developers.google.com/android-publisher/{category}",
        host="developers.google.com",
        officiality="VERIFIED",
        accessed_at=NOW - 1,
        applicability="Current Google Play publishing requirements",
        summary="Verified current Google Play documentation.",
    )


def request_for(tmp_path, *, package=PACKAGE, channel="production", version="1.2.3",
                version_code=VERSION_CODE, rollout_percentage=100, metadata=None,
                artifact_name="widget.aab", target_resolved=True):
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    artifact = root / artifact_name
    artifact.write_bytes(b"signed Android application bundle")
    values = {
        "version_code": version_code,
        "rollout_percentage": rollout_percentage,
        "agreement_accepted_by_user": True,
        "credentials_configured": True,
        "local_build_validated": True,
    }
    if metadata:
        values.update(metadata)
    return ReleaseRequest(
        task_id="task-play",
        provider_id="google_play",
        project_root=root,
        app_or_package=package,
        version=version,
        channel=channel,
        artifact_paths=(artifact,),
        metadata=values,
        authorization=ReleaseAuthorization(
            True, target_resolved, True, (evidence("publishing"), evidence("terms")),
        ),
    )


def adapter_for(api, uploader, *, credentials=None):
    return GooglePlayReleaseAdapter(
        api_transport=api,
        upload_transport=uploader,
        credential_provider=credentials or (lambda profile: {
            "access_token": "configured-play-token",
            "scopes": ["https://www.googleapis.com/auth/androidpublisher"],
        }),
        clock=lambda: NOW,
    )


class PlayPublishApi:
    def __init__(self, *, staged=False, commit_lost=False, lifecycle_state="RELEASE_LIFECYCLE_STATE_PUBLISHED"):
        self.calls = []
        self.uploaded = None
        self.release = None
        self.committed_release = None
        self.staged = staged
        self.commit_lost = commit_lost
        self.lifecycle_state = lifecycle_state
        self.edit_count = 0

    @staticmethod
    def _body(spec):
        return json.loads(spec.body) if isinstance(spec.body, str) else spec.body

    def request(self, spec):
        self.calls.append(spec)
        path = spec.path
        if spec.method == "POST" and path == f"/androidpublisher/v3/applications/{PACKAGE}/edits":
            self.edit_count += 1
            edit_id = EDIT_ID if self.edit_count == 1 else READBACK_EDIT_ID
            return ProviderResponse(200, json.dumps({"id": edit_id, "expiryTimeSeconds": "1800003600"}),
                                    "application/json")
        if spec.method == "GET" and path in {
            f"/androidpublisher/v3/applications/{PACKAGE}/edits/{EDIT_ID}",
            f"/androidpublisher/v3/applications/{PACKAGE}/edits/{READBACK_EDIT_ID}",
        }:
            edit_id = path.rsplit("/", 1)[-1]
            return ProviderResponse(200, json.dumps({"id": edit_id, "expiryTimeSeconds": "1800003600"}),
                                    "application/json")
        if spec.method == "GET" and path.endswith("/bundles"):
            bundles = [self.uploaded] if self.uploaded else []
            return ProviderResponse(200, json.dumps({"kind": "androidpublisher#bundlesListResponse",
                                                     "bundles": bundles}), "application/json")
        if spec.method == "GET" and path.endswith("/apks"):
            apks = [self.uploaded] if self.uploaded else []
            return ProviderResponse(200, json.dumps({"kind": "androidpublisher#apksListResponse",
                                                     "apks": apks}), "application/json")
        if spec.method == "GET" and path == f"/androidpublisher/v3/applications/{PACKAGE}/edits/{EDIT_ID}/tracks/production":
            return ProviderResponse(200, json.dumps(self.release or {"track": "production", "releases": []}),
                                    "application/json")
        if spec.method == "PUT" and path == f"/androidpublisher/v3/applications/{PACKAGE}/edits/{EDIT_ID}/tracks/production":
            self.release = self._body(spec)
            return ProviderResponse(200, json.dumps(self.release), "application/json")
        if spec.method == "POST" and path == f"/androidpublisher/v3/applications/{PACKAGE}/edits/{EDIT_ID}:validate":
            return ProviderResponse(200, json.dumps({"id": EDIT_ID}), "application/json")
        if spec.method == "POST" and path.startswith(f"/androidpublisher/v3/applications/{PACKAGE}/edits/{EDIT_ID}:commit"):
            self.committed_release = self.release
            if self.commit_lost:
                self.commit_lost = False
                return ProviderResponse(503, '{"error":"temporary"}', "application/json")
            return ProviderResponse(200, json.dumps({"id": EDIT_ID}), "application/json")
        if spec.method == "GET" and path == f"/androidpublisher/v3/applications/{PACKAGE}/edits/{READBACK_EDIT_ID}/tracks/production":
            if self.committed_release is None:
                return ProviderResponse(200, json.dumps({"track": "production", "releases": []}),
                                        "application/json")
            return ProviderResponse(200, json.dumps(self.committed_release), "application/json")
        if spec.method == "GET" and path == f"/androidpublisher/v3/applications/{PACKAGE}/tracks/production/releases":
            releases = []
            track_releases = self.committed_release.get("releases", []) if self.committed_release else []
            for release in track_releases:
                if str(VERSION_CODE) in {str(code) for code in release.get("versionCodes", [])}:
                    releases.append({
                        "releaseName": release.get("name"),
                        "track": "production",
                        "activeArtifacts": [{"versionCode": int(VERSION_CODE)}],
                        "releaseLifecycleState": self.lifecycle_state,
                    })
            return ProviderResponse(200, json.dumps({"releases": releases}), "application/json")
        if spec.method == "DELETE" and path == f"/androidpublisher/v3/applications/{PACKAGE}/edits/{READBACK_EDIT_ID}":
            return ProviderResponse(204, "", None)
        raise AssertionError(f"Unexpected Google Play operation {spec.method} {path}")


def test_google_play_requires_package_track_and_version(tmp_path):
    request = request_for(tmp_path, package="bad package", channel="unknown", version_code="0")
    adapter = adapter_for(FakeApi(lambda spec: pytest.fail(f"unexpected API call: {spec.path}")), None)

    result = adapter.preflight(request)

    assert result.status == "BLOCKED"
    assert any("package" in blocker.lower() or "track" in blocker.lower()
               or "version" in blocker.lower() for blocker in result.blockers)


def test_google_play_requires_user_resolved_target(tmp_path):
    api = FakeApi(lambda spec: pytest.fail(f"unexpected API call: {spec.path}"))
    request = request_for(tmp_path, target_resolved=False)
    adapter = adapter_for(api, None)

    result = adapter.preflight(request)

    assert result.status == "BLOCKED"
    assert api.calls == []
    assert any("resolved" in blocker.lower() for blocker in result.blockers)


def test_google_play_stops_for_agreement_or_required_policy_attestation(tmp_path):
    api = FakeApi(lambda spec: pytest.fail(f"unexpected API call: {spec.path}"))
    request = request_for(tmp_path, metadata={
        "agreement_accepted_by_user": False,
        "required_attestations": ["content_rating_confirmed"],
        "completed_attestations": [],
    })
    adapter = adapter_for(api, None)

    result = adapter.preflight(request)

    assert result.status == "BLOCKED"
    assert api.calls == []
    assert any(check["name"] == "agreement" and check["status"] == "BLOCKED" for check in result.checks)
    assert any(check["name"] == "attestations" and check["status"] == "BLOCKED" for check in result.checks)


def test_staged_rollout_percentage_is_never_invented(tmp_path):
    api = FakeApi(lambda spec: pytest.fail(f"unexpected API call: {spec.path}"))
    request = request_for(tmp_path, rollout_percentage=None)
    adapter = adapter_for(api, None)

    result = adapter.preflight(request)

    assert result.status == "BLOCKED"
    assert api.calls == []
    assert any("rollout" in blocker.lower() for blocker in result.blockers)


def test_uploaded_or_in_review_is_submitted_not_published(tmp_path):
    request = request_for(tmp_path, rollout_percentage=25)
    api = PlayPublishApi(staged=True, lifecycle_state="RELEASE_LIFECYCLE_STATE_IN_REVIEW")
    fake_api = FakeApi(api.request)
    uploader = FakeUpload(api)
    adapter = adapter_for(fake_api, uploader)

    result = adapter.publish(request, "operation-play-in-progress")

    assert result.status == "SUBMITTED", result
    assert result.provider_state == "RELEASE_LIFECYCLE_STATE_IN_REVIEW"
    assert result.remote_id == f"{PACKAGE}/production/{VERSION_CODE}"
    assert result.retry_requires_inspection is False
    assert any(call.method == "POST" and call.path.endswith(":commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW")
               for call in fake_api.calls)
    assert uploader.calls[0][1:3] == (EDIT_ID, "bundle")
    assert uploader.calls[0][4] == "configured-play-token"


def test_google_play_timeout_reads_edit_and_release_state_before_retry(tmp_path):
    request = request_for(tmp_path, rollout_percentage=100)
    api = PlayPublishApi(commit_lost=True)
    fake_api = FakeApi(api.request)
    uploader = FakeUpload(api)
    adapter = adapter_for(fake_api, uploader)

    result = adapter.publish(request, "operation-play-timeout")

    assert result.status == "PUBLISHED", result
    assert result.provider_state == "RELEASE_LIFECYCLE_STATE_PUBLISHED"
    assert result.remote_id == f"{PACKAGE}/production/{VERSION_CODE}"
    assert len(uploader.calls) == 1
    assert any(spec.method == "GET" and spec.path.endswith(f"/{READBACK_EDIT_ID}/tracks/production")
               for spec in fake_api.calls)
    assert sum(spec.method == "POST" and spec.path.endswith("/edits") for spec in fake_api.calls) == 2


def test_completed_track_is_not_published_until_lifecycle_is_published(tmp_path):
    request = request_for(tmp_path, rollout_percentage=100)
    api = PlayPublishApi(lifecycle_state="RELEASE_LIFECYCLE_STATE_APPROVED_NOT_PUBLISHED")
    fake_api = FakeApi(api.request)
    adapter = adapter_for(fake_api, FakeUpload(api))

    result = adapter.publish(request, "operation-play-approved")

    assert result.status == "SUBMITTED"
    assert result.provider_state == "RELEASE_LIFECYCLE_STATE_APPROVED_NOT_PUBLISHED"
    assert result.remote_id == f"{PACKAGE}/production/{VERSION_CODE}"


def test_apk_artifact_hash_is_read_from_binary_resource(tmp_path):
    request = request_for(tmp_path, artifact_name="widget.apk", rollout_percentage=100)
    api = PlayPublishApi()
    fake_api = FakeApi(api.request)
    uploader = FakeUpload(api)
    adapter = adapter_for(fake_api, uploader)

    result = adapter.publish(request, "operation-play-apk")

    assert result.status == "PUBLISHED", result
    assert uploader.calls[0][2] == "apk"
    assert api.uploaded["binary"]["sha256"] == hashlib.sha256(request.artifact_paths[0].read_bytes()).hexdigest()


def test_staged_rollout_is_published_only_when_lifecycle_says_available(tmp_path):
    request = request_for(tmp_path, rollout_percentage=25)
    api = PlayPublishApi(staged=True)
    fake_api = FakeApi(api.request)
    result = adapter_for(fake_api, FakeUpload(api)).publish(request, "operation-play-partial-live")

    assert result.status == "PUBLISHED"
    assert result.provider_state == "RELEASE_LIFECYCLE_STATE_PUBLISHED"
    assert "partially rolled out" in result.summary


def test_existing_same_version_release_fields_are_preserved(tmp_path):
    request = request_for(tmp_path, rollout_percentage=25)
    api = PlayPublishApi(staged=True)
    api.release = {
        "track": "production",
        "releases": [{
            "name": "old name",
            "versionCodes": [VERSION_CODE],
            "releaseNotes": [{"language": "en-US", "text": "Keep this text"}],
            "status": "inProgress",
            "userFraction": 0.25,
            "countryTargeting": {"countries": ["US"], "includeRestOfWorld": False},
            "inAppUpdatePriority": 2,
        }],
    }
    fake_api = FakeApi(api.request)
    adapter = adapter_for(fake_api, FakeUpload(api))

    result = adapter.publish(request, "operation-play-preserve")

    assert result.status == "PUBLISHED", result
    posted_track = next(
        json.loads(call.body) if isinstance(call.body, str) else call.body for call in fake_api.calls
        if call.method == "PUT" and call.path.endswith("/tracks/production")
    )
    release = posted_track["releases"][0]
    assert release["name"] == request.version
    assert release["releaseNotes"] == [{"language": "en-US", "text": "Keep this text"}]
    assert release["countryTargeting"] == {"countries": ["US"], "includeRestOfWorld": False}
    assert release["inAppUpdatePriority"] == 2


def test_new_production_release_blocks_when_existing_release_is_country_targeted(tmp_path):
    request = request_for(tmp_path, version="1.2.4", version_code="43", rollout_percentage=100)
    api = PlayPublishApi()
    api.release = {
        "track": "production",
        "releases": [{
            "name": "1.2.3",
            "versionCodes": [VERSION_CODE],
            "status": "completed",
            "countryTargeting": {"countries": ["US"], "includeRestOfWorld": False},
        }],
    }
    fake_api = FakeApi(api.request)
    result = adapter_for(fake_api, FakeUpload(api, version_code="43")).publish(
        request, "operation-play-country-target",
    )

    assert result.status == "BLOCKED"
    assert result.provider_state == "COUNTRY_TARGETING_NEEDS_EXPLICIT_TARGET"
    assert not any(call.method == "PUT" and call.path.endswith("/tracks/production") for call in fake_api.calls)


def test_auth_probe_never_serializes_service_account_json(tmp_path):
    credential_document = '{"private_key":"service-account-secret","client_email":"play@example.test"}'
    api = FakeApi(lambda spec: pytest.fail(f"unexpected API call: {spec.path}"))
    request = request_for(tmp_path, metadata={"credentials_configured": False})
    adapter = adapter_for(api, None, credentials=lambda profile: {
        "service_account_json": credential_document,
        "access_token": credential_document,
        "scopes": ["https://www.googleapis.com/auth/androidpublisher"],
    })

    result = adapter.preflight(request)

    assert result.status == "BLOCKED"
    assert api.calls == []
    assert credential_document not in repr(result)
    assert "service-account-secret" not in repr(result)


def test_google_play_auth_requires_android_publisher_scope(tmp_path):
    api = FakeApi(lambda spec: pytest.fail(f"unexpected API call: {spec.path}"))
    request = request_for(tmp_path)
    adapter = adapter_for(api, None, credentials=lambda profile: {
        "access_token": "token-without-publish-scope",
        "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
    })

    result = adapter.preflight(request)

    assert result.status == "BLOCKED"
    assert api.calls == []


def test_google_play_api_transport_uses_fixed_host_and_bearer_profile():
    captured = {}

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self, size=-1):
            return b'{"id":"edit-upload-123"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return Response()

    transport = ProviderApiTransport(
        credential_provider=lambda profile: captured.update({"profile": profile}) or "configured-play-token",
        opener_factory=lambda host, allowed_hosts: captured.update({"host": host, "allowed_hosts": allowed_hosts}) or Opener(),
    )

    response = transport.request(ProviderRequest(
        "google_play_api", "GET", f"/androidpublisher/v3/applications/{PACKAGE}/edits/{EDIT_ID}",
    ))

    assert response.status == 200
    assert captured["host"] == "androidpublisher.googleapis.com"
    assert captured["allowed_hosts"] == ("androidpublisher.googleapis.com",)
    assert captured["url"] == f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{PACKAGE}/edits/{EDIT_ID}"
    assert captured["profile"] == "google_play_api"
    assert captured["authorization"] == "Bearer configured-play-token"


def test_google_play_binary_upload_uses_fixed_resumable_session(tmp_path, monkeypatch):
    artifact = tmp_path / "widget.aab"
    artifact.write_bytes(b"android bundle bytes")
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {
            "Content-Type": "application/json",
            "Location": (f"https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications/{PACKAGE}"
                         f"/edits/{EDIT_ID}/bundles?uploadType=resumable&upload_id=session-token"),
        }

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            yield b'{"versionCode":42}'

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_options"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, *, headers, content):
            captured["start_url"] = url
            captured["start_headers"] = dict(headers)
            assert content == b""
            return FakeResponse()

        def stream(self, method, url, *, headers, content):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = dict(headers)
            captured["body"] = b"".join(content)
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "Client", FakeClient)
    result = GooglePlayMediaUploadTransport().upload(
        PACKAGE, EDIT_ID, "bundle", artifact, "configured-play-token",
    )

    assert result.status == 200
    assert captured["start_url"] == (
        f"https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications/{PACKAGE}"
        f"/edits/{EDIT_ID}/bundles?uploadType=resumable"
    )
    assert captured["method"] == "PUT"
    assert captured["url"].endswith("upload_id=session-token")
    assert captured["client_options"]["follow_redirects"] is False
    assert captured["headers"]["Authorization"] == "Bearer configured-play-token"
    assert captured["headers"]["Content-Length"] == str(artifact.stat().st_size)
    assert captured["start_headers"]["X-Upload-Content-Length"] == str(artifact.stat().st_size)
    assert captured["body"] == artifact.read_bytes()


def test_google_play_resumable_upload_inspects_offset_after_interruption(tmp_path, monkeypatch):
    artifact = tmp_path / "widget.aab"
    artifact.write_bytes(b"0123456789abcdef")
    session_url = (f"https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications/{PACKAGE}"
                   f"/edits/{EDIT_ID}/bundles?uploadType=resumable&upload_id=session-token")
    captured = {"upload_attempts": 0}

    class FakeResponse:
        def __init__(self, status, headers=None, body=b""):
            self.status_code = status
            self.headers = headers or {}
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            yield self.body

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, *, headers, content):
            return FakeResponse(200, {"Location": session_url})

        def stream(self, method, url, *, headers, content):
            captured["upload_attempts"] += 1
            body = b"".join(content)
            captured.setdefault("bodies", []).append(body)
            if captured["upload_attempts"] == 1:
                return FakeResponse(503)
            captured["resume_headers"] = dict(headers)
            return FakeResponse(201, {"Content-Type": "application/json"}, b'{"versionCode":42}')

        def put(self, url, *, headers, content):
            captured["status_url"] = url
            captured["status_headers"] = dict(headers)
            assert content == b""
            return FakeResponse(308, {"Range": "bytes=0-3"})

    import httpx

    monkeypatch.setattr(httpx, "Client", FakeClient)
    result = GooglePlayMediaUploadTransport().upload(
        PACKAGE, EDIT_ID, "bundle", artifact, "configured-play-token",
    )

    assert result.status == 201
    assert captured["status_url"] == session_url
    assert captured["status_headers"]["Content-Range"] == f"bytes */{artifact.stat().st_size}"
    assert captured["bodies"] == [artifact.read_bytes(), artifact.read_bytes()[4:]]
    assert captured["resume_headers"]["Content-Range"] == f"bytes 4-{artifact.stat().st_size - 1}/{artifact.stat().st_size}"


def test_google_play_resumable_upload_rejects_untrusted_session_host(tmp_path, monkeypatch):
    artifact = tmp_path / "widget.aab"
    artifact.write_bytes(b"android bundle bytes")
    captured = {"upload_calls": 0}

    class FakeResponse:
        status_code = 200
        headers = {"Location": f"https://attacker.example/upload/androidpublisher/v3/applications/{PACKAGE}"
                              f"/edits/{EDIT_ID}/bundles?uploadType=resumable&upload_id=session-token"}

        def iter_bytes(self):
            yield b""

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, *, headers, content):
            return FakeResponse()

        def stream(self, *args, **kwargs):
            captured["upload_calls"] += 1
            raise AssertionError("untrusted session URL must not be contacted")

    import httpx

    monkeypatch.setattr(httpx, "Client", FakeClient)
    result = GooglePlayMediaUploadTransport().upload(
        PACKAGE, EDIT_ID, "bundle", artifact, "configured-play-token",
    )

    assert result.status == 0
    assert captured["upload_calls"] == 0

