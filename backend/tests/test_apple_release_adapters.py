from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from olcr_api.coding_tasks import ReleasePreflight
from olcr_api.provider_research import ProviderEvidence
from olcr_api.release_adapters.base import (
    CommandResult,
    ProviderApiTransport,
    ProviderRequest,
    ProviderResponse,
)
from olcr_api.release_adapters.apple_macos import BuildStageResult, MacOSNotarizationStage
from olcr_api.release_adapters.app_store_connect import AppStoreConnectAdapter
from olcr_api.release_workflow import ReleaseAuthorization, ReleaseRequest, ReleaseResult


NOW = 1_800_000_001.0
SUBMISSION_ID = "notary-submission-123"
APP_ID = "1234567890"
BUILD_UPLOAD_ID = "build-upload-123"
BUILD_ID = "build-123"
VERSION_ID = "version-123"
REVIEW_ID = "review-submission-123"
_TEST_PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgldmph3I+01dBkaVG
/Wo/OtfKwbcSWsr55o9CdFF8YlahRANCAAQxdCGUVwJNf734uqD0de0qmu6QeyLV
wxYfzwscRFeiYlPYWhpMSixnIooDdVOB9gc7txYLLjcm/hNpdMNcM70F
-----END PRIVATE KEY-----"""

def app_store_credentials(private_key=None):
    if private_key is None:
        private_key = _TEST_PRIVATE_KEY
    return {"issuer_id": "issuer-123", "key_id": "KEY1234567", "private_key": private_key}


class FakeRunner:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def run(self, spec):
        self.calls.append(spec)
        if spec.executable == "ditto":
            Path(spec.argv[-1]).write_bytes(b"test notarization archive")
            return CommandResult(0, "", "", False, None)
        key = spec.argv[:2]
        if spec.executable == "codesign" and "--verify" in spec.argv:
            return CommandResult(0, "", "", False, None)
        if spec.executable == "codesign" and "--display" in spec.argv:
            return CommandResult(
                0, "",
                "Authority=Developer ID Application: OLCR Test (TEAM123)\n"
                "Timestamp=2026-11-18T00:00:00Z\n"
                "CodeDirectory flags=0x10000(runtime)\n",
                False, None,
            )
        return self.responses.get(key, CommandResult(0, "", "", False, None))


class FakeApi:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, spec):
        self.calls.append(spec)
        return self.handler(spec)


class FakeUploadTransport:
    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def upload(self, url, headers, path, offset, length):
        self.calls.append((url, dict(headers), Path(path), offset, length))
        return self.status


def evidence(provider_id: str, category: str) -> ProviderEvidence:
    return ProviderEvidence(
        task_id="task-apple",
        provider_id=provider_id,
        category=category,
        title=f"Apple {category}",
        url=f"https://developer.apple.com/{category}",
        host="developer.apple.com",
        officiality="VERIFIED",
        accessed_at=NOW - 1,
        applicability="Current Apple release requirements",
        summary="Verified current Apple documentation.",
    )


def request_for(tmp_path, *, provider_id="apple_macos", channel="stable", metadata=None,
                artifact_name=None):
    if artifact_name is None:
        artifact_name = "Widget.pkg" if provider_id == "app_store_connect" else "Widget.app"
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    artifact = root / artifact_name
    if artifact_name.endswith(".app"):
        artifact.mkdir(exist_ok=True)
        (artifact / "Contents").mkdir(exist_ok=True)
        (artifact / "Contents" / "Info.plist").write_bytes(b"plist")
    else:
        artifact.write_bytes(b"signed Apple build bytes")
    initial = {
        "agreement_accepted_by_user": True,
        "credentials_configured": True,
        "local_build_validated": True,
        "signed_build": True,
        "hardened_runtime": True,
        "secure_timestamp": True,
        "build_number": "42",
        "apple_platform": "MAC_OS",
        "app_store_metadata_confirmed_by_user": True,
    }
    if metadata:
        initial.update(metadata)
    return ReleaseRequest(
        task_id="task-apple",
        provider_id=provider_id,
        project_root=root,
        app_or_package="com.example.widget",
        version="1.2.3",
        channel=channel,
        artifact_paths=(artifact,),
        metadata=initial,
        authorization=ReleaseAuthorization(
            True, True, True,
            (evidence(provider_id, "publishing"), evidence(provider_id, "terms"),
             *((evidence(provider_id, "signing"),) if provider_id == "apple_macos" else ())),
        ),
    )


def test_macos_release_requires_signed_build_and_current_notarization_requirements(tmp_path):
    request = request_for(tmp_path, metadata={"signed_build": False})
    runner = FakeRunner()
    stage = MacOSNotarizationStage(
        runner=runner,
        credential_provider=lambda profile: {"APPLE_NOTARY_KEYCHAIN_PROFILE": "olcr-notary"},
        clock=lambda: NOW,
    )

    result = stage.run(request)

    assert result.status == "BLOCKED"
    assert runner.calls == []
    assert any(check["name"] == "signed_build" for check in result.checks)


def test_notarization_timeout_inspects_submission_before_retry(tmp_path):
    submit = CommandResult(None, json.dumps({"id": SUBMISSION_ID}), "", True, "COMMAND_TIMEOUT")
    info = CommandResult(0, json.dumps({"id": SUBMISSION_ID, "status": "Accepted"}), "", False, None)
    runner = FakeRunner({("notarytool", "submit"): submit, ("notarytool", "info"): info})
    request = request_for(tmp_path)
    stage = MacOSNotarizationStage(
        runner=runner,
        credential_provider=lambda profile: {"APPLE_NOTARY_KEYCHAIN_PROFILE": "olcr-notary"},
        clock=lambda: NOW,
    )

    result = stage.run(request)

    notary_calls = [call for call in runner.calls if call.executable == "xcrun"]
    assert [call.argv[0] for call in notary_calls] == ["notarytool", "notarytool", "stapler", "stapler"]
    assert notary_calls[1].argv[1:3] == ("info", SUBMISSION_ID)
    assert result.status == "NOTARIZED"
    assert result.submission_id == SUBMISSION_ID
    assert all(call.argv[0:2] != ("notarytool", "submit") for call in notary_calls[1:])


def test_notarization_stage_does_not_claim_release_without_upload_readback(tmp_path):
    runner = FakeRunner({
        ("notarytool", "submit"): CommandResult(
            0, json.dumps({"id": SUBMISSION_ID, "status": "Accepted"}), "", False, None,
        ),
    })
    request = request_for(tmp_path)
    result = MacOSNotarizationStage(
        runner=runner,
        credential_provider=lambda profile: {"APPLE_NOTARY_KEYCHAIN_PROFILE": "olcr-notary"},
        clock=lambda: NOW,
    ).run(request)

    assert isinstance(result, BuildStageResult)
    assert result.status == "NOTARIZED"
    assert "PUBLISHED" not in {result.status, result.provider_state}


def test_app_store_connect_stops_for_unaccepted_agreement_or_required_attestation(tmp_path):
    calls = []
    private_key = app_store_credentials()["private_key"]
    credentials = lambda profile: app_store_credentials(private_key)
    api = FakeApi(lambda spec: calls.append(spec) or ProviderResponse(200, '{"data":[]}', "application/json"))
    adapter = AppStoreConnectAdapter(
        api_transport=api,
        credential_provider=credentials,
        clock=lambda: NOW,
    )
    request = request_for(tmp_path, provider_id="app_store_connect", metadata={
        "agreement_accepted_by_user": False,
        "required_attestations": ["export_compliance_confirmed"],
        "completed_attestations": [],
    })

    result = adapter.preflight(request)

    assert result.status == "BLOCKED"
    assert api.calls == []
    assert private_key not in repr(result)
    assert any(check["name"] == "agreement" and check["status"] == "BLOCKED" for check in result.checks)
    assert any(check["name"] == "attestations" and check["status"] == "BLOCKED" for check in result.checks)


def test_app_review_waiting_is_submitted_not_published(tmp_path):
    request = request_for(tmp_path, provider_id="app_store_connect", channel="stable")
    digest = hashlib.sha256(request.artifact_paths[0].read_bytes()).hexdigest()

    def handle(spec):
        body = json.loads(spec.body) if isinstance(spec.body, str) else spec.body
        if spec.method == "GET" and spec.path.startswith("/v1/apps?"):
            return ProviderResponse(200, json.dumps({"data": [{"id": APP_ID, "type": "apps",
                                                                "attributes": {"bundleId": "com.example.widget"}}]}),
                                    "application/json")
        if spec.method == "GET" and spec.path.startswith(f"/v1/apps/{APP_ID}/buildUploads?"):
            return ProviderResponse(200, json.dumps({"data": [{
                "id": BUILD_UPLOAD_ID,
                "type": "buildUploads",
                "attributes": {"cfBundleShortVersionString": "1.2.3", "cfBundleVersion": "42",
                               "platform": "MAC_OS", "state": "COMPLETE"},
            }]}), "application/json")
        if spec.method == "GET" and spec.path == f"/v1/buildUploads/{BUILD_UPLOAD_ID}/buildUploadFiles?limit=20":
            return ProviderResponse(200, json.dumps({"data": [{
                "id": "build-file-123", "type": "buildUploadFiles",
                "attributes": {"fileName": request.artifact_paths[0].name,
                               "fileSize": request.artifact_paths[0].stat().st_size,
                               "sourceFileChecksums": {"file": {"hash": digest, "algorithm": "SHA_256"}},
                               "assetDeliveryState": {"state": "COMPLETE"}},
            }]}), "application/json")
        if spec.method == "GET" and spec.path == f"/v1/buildUploads/{BUILD_UPLOAD_ID}?include=build":
            return ProviderResponse(200, json.dumps({
                "data": {"id": BUILD_UPLOAD_ID, "type": "buildUploads",
                         "attributes": {"cfBundleShortVersionString": "1.2.3", "cfBundleVersion": "42",
                                        "platform": "MAC_OS", "state": "COMPLETE"},
                         "relationships": {"build": {"data": {"id": BUILD_ID, "type": "builds"}}}},
                "included": [{"id": BUILD_ID, "type": "builds", "attributes": {"processingState": "VALID"}}],
            }), "application/json")
        if spec.method == "GET" and spec.path.startswith(f"/v1/apps/{APP_ID}/appStoreVersions?"):
            return ProviderResponse(200, json.dumps({"data": [{
                "id": VERSION_ID, "type": "appStoreVersions",
                "attributes": {"versionString": "1.2.3", "platform": "MAC_OS", "appStoreState": "READY_FOR_REVIEW"},
            }]}), "application/json")
        if spec.method == "PATCH" and spec.path == f"/v1/appStoreVersions/{VERSION_ID}/relationships/build":
            assert body["data"] == {"type": "builds", "id": BUILD_ID}
            return ProviderResponse(204, "", None)
        if spec.method == "GET" and spec.path == f"/v1/appStoreVersions/{VERSION_ID}?include=build":
            return ProviderResponse(200, json.dumps({
                "data": {"id": VERSION_ID, "type": "appStoreVersions",
                         "attributes": {"versionString": "1.2.3", "platform": "MAC_OS",
                                        "appStoreState": "READY_FOR_REVIEW"},
                         "relationships": {"build": {"data": {"id": BUILD_ID, "type": "builds"}}}},
            }), "application/json")
        if spec.method == "POST" and spec.path == "/v1/reviewSubmissions":
            assert body["data"]["relationships"]["app"]["data"]["id"] == APP_ID
            return ProviderResponse(201, json.dumps({"data": {"id": REVIEW_ID, "type": "reviewSubmissions",
                                                               "attributes": {"state": "READY_FOR_REVIEW"}}}),
                                    "application/json")
        if spec.method == "GET" and spec.path == f"/v1/apps/{APP_ID}/reviewSubmissions?filter%5Bplatform%5D=MAC_OS&limit=50":
            return ProviderResponse(200, '{"data":[]}', "application/json")
        if spec.method == "POST" and spec.path == "/v1/reviewSubmissionItems":
            assert body["data"]["relationships"]["appStoreVersion"]["data"]["id"] == VERSION_ID
            return ProviderResponse(201, json.dumps({"data": {"id": "item-123", "type": "reviewSubmissionItems"}}),
                                    "application/json")
        if spec.method == "PATCH" and spec.path == f"/v1/reviewSubmissions/{REVIEW_ID}":
            assert body["data"]["attributes"]["submitted"] is True
            return ProviderResponse(200, json.dumps({"data": {"id": REVIEW_ID, "type": "reviewSubmissions",
                                                               "attributes": {"state": "WAITING_FOR_REVIEW"}}}),
                                    "application/json")
        if spec.method == "GET" and spec.path == f"/v1/reviewSubmissions/{REVIEW_ID}":
            return ProviderResponse(200, json.dumps({"data": {"id": REVIEW_ID, "type": "reviewSubmissions",
                                                               "attributes": {"state": "WAITING_FOR_REVIEW"}}}),
                                    "application/json")
        raise AssertionError(f"Unexpected App Store Connect operation {spec.method} {spec.path}")

    api = FakeApi(handle)
    adapter = AppStoreConnectAdapter(
        api_transport=api,
        credential_provider=lambda profile: app_store_credentials(),
        upload_transport=FakeUploadTransport(),
        clock=lambda: NOW,
    )

    result = adapter.publish(request, "operation-123")

    assert isinstance(result, ReleaseResult)
    assert result.status == "SUBMITTED"
    assert result.provider_state == "WAITING_FOR_REVIEW"
    assert result.remote_id == REVIEW_ID
    assert result.retry_requires_inspection is False
    assert any(call.path == f"/v1/reviewSubmissions/{REVIEW_ID}" for call in api.calls)


def test_app_store_build_upload_uses_apple_ranges_and_reads_back_before_testflight(tmp_path):
    request = request_for(tmp_path, provider_id="app_store_connect", channel="testflight")
    artifact = request.artifact_paths[0]
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    def handle(spec):
        body = json.loads(spec.body) if isinstance(spec.body, str) else spec.body
        if spec.method == "GET" and spec.path.startswith("/v1/apps?"):
            return ProviderResponse(200, json.dumps({"data": [{"id": APP_ID, "type": "apps",
                                                                "attributes": {"bundleId": "com.example.widget"}}]}),
                                    "application/json")
        if spec.method == "GET" and spec.path.startswith(f"/v1/apps/{APP_ID}/buildUploads?"):
            return ProviderResponse(200, '{"data":[]}', "application/json")
        if spec.method == "POST" and spec.path == "/v1/buildUploads":
            assert body["data"]["attributes"] == {
                "cfBundleShortVersionString": "1.2.3", "cfBundleVersion": "42", "platform": "MAC_OS",
            }
            return ProviderResponse(201, json.dumps({"data": {"id": BUILD_UPLOAD_ID,
                                                                "type": "buildUploads"}}), "application/json")
        if spec.method == "POST" and spec.path == "/v1/buildUploadFiles":
            assert body["data"]["attributes"]["uti"] == "com.apple.pkg"
            return ProviderResponse(201, json.dumps({"data": {
                "id": "build-file-123", "type": "buildUploadFiles",
                "attributes": {"uploadOperations": [{
                    "method": "PUT",
                    "url": "https://store-030.blobstore.apple.com/upload?signature=private",
                    "offset": 0, "length": artifact.stat().st_size,
                    "requestHeaders": [{"name": "Content-Type", "value": "application/octet-stream"}],
                }]},
            }}), "application/json")
        if spec.method == "PATCH" and spec.path == "/v1/buildUploadFiles/build-file-123":
            assert body["data"]["attributes"]["uploaded"] is True
            assert body["data"]["attributes"]["sourceFileChecksums"]["file"] == {
                "hash": digest, "algorithm": "SHA_256",
            }
            return ProviderResponse(204, "", None)
        if spec.method == "GET" and spec.path == f"/v1/buildUploads/{BUILD_UPLOAD_ID}/buildUploadFiles?limit=20":
            return ProviderResponse(200, json.dumps({"data": [{
                "id": "build-file-123", "type": "buildUploadFiles",
                "attributes": {"fileName": artifact.name, "fileSize": artifact.stat().st_size,
                               "sourceFileChecksums": {"file": {"hash": digest, "algorithm": "SHA_256"}},
                               "assetDeliveryState": {"state": "COMPLETE"}},
            }]}), "application/json")
        if spec.method == "GET" and spec.path == f"/v1/buildUploads/{BUILD_UPLOAD_ID}?include=build":
            return ProviderResponse(200, json.dumps({
                "data": {"id": BUILD_UPLOAD_ID, "type": "buildUploads",
                         "attributes": {"cfBundleShortVersionString": "1.2.3", "cfBundleVersion": "42",
                                        "platform": "MAC_OS", "state": "COMPLETE"},
                         "relationships": {"build": {"data": {"id": BUILD_ID, "type": "builds"}}}},
            }), "application/json")
        raise AssertionError(f"Unexpected App Store Connect operation {spec.method} {spec.path}")

    api = FakeApi(handle)
    uploader = FakeUploadTransport()
    adapter = AppStoreConnectAdapter(
        api_transport=api,
        credential_provider=lambda profile: app_store_credentials(),
        upload_transport=uploader,
        clock=lambda: NOW,
    )

    result = adapter.publish(request, "operation-upload")

    assert result.status == "SUBMITTED"
    assert result.provider_state == "TESTFLIGHT_BUILD_READY"
    assert result.remote_id == BUILD_ID
    assert len(uploader.calls) == 1
    assert "Authorization" not in uploader.calls[0][1]


def test_app_store_auth_preflight_does_not_expose_key_material(tmp_path):
    private_key = app_store_credentials()["private_key"]
    request = request_for(tmp_path, provider_id="app_store_connect")
    adapter = AppStoreConnectAdapter(
        api_transport=FakeApi(lambda spec: ProviderResponse(200, '{"data":[]}', "application/json")),
        credential_provider=lambda profile: app_store_credentials(private_key),
        clock=lambda: NOW,
    )

    result = adapter.preflight(request)

    assert result.status == "READY"
    assert private_key not in repr(result)
    assert "private_key" not in repr(result).lower()


def test_app_store_auth_preflight_rejects_unusable_private_key(tmp_path):
    private_key = "not-an-elliptic-curve-private-key"
    request = request_for(tmp_path, provider_id="app_store_connect")
    adapter = AppStoreConnectAdapter(
        api_transport=FakeApi(lambda spec: ProviderResponse(200, '{"data":[]}', "application/json")),
        credential_provider=lambda profile: app_store_credentials(private_key),
        clock=lambda: NOW,
    )

    result = adapter.preflight(request)

    assert result.status == "BLOCKED"
    assert private_key not in repr(result)
    assert any(check["name"] == "credentials" and check["status"] == "BLOCKED" for check in result.checks)


def test_external_testflight_preflight_stops_before_api_calls(tmp_path):
    api = FakeApi(lambda spec: pytest.fail(f"unexpected API call: {spec.path}"))
    request = request_for(tmp_path, provider_id="app_store_connect", channel="external")
    adapter = AppStoreConnectAdapter(
        api_transport=api,
        credential_provider=lambda profile: app_store_credentials(),
        clock=lambda: NOW,
    )

    result = adapter.preflight(request)

    assert result.status == "BLOCKED"
    assert api.calls == []
    assert any(check["name"] == "external_testflight_review" and check["status"] == "BLOCKED"
               for check in result.checks)


def test_testflight_external_group_is_blocked_before_build_upload(tmp_path):
    request = request_for(
        tmp_path, provider_id="app_store_connect", channel="testflight",
        metadata={"testflight_group_ids": ["external-group-1"]},
    )

    def handle(spec):
        if spec.method == "GET" and spec.path.startswith("/v1/apps?"):
            return ProviderResponse(200, json.dumps({"data": [{
                "id": APP_ID, "type": "apps", "attributes": {"bundleId": "com.example.widget"},
            }]}), "application/json")
        if spec.method == "GET" and spec.path == "/v1/betaGroups/external-group-1?include=app":
            return ProviderResponse(200, json.dumps({"data": {
                "id": "external-group-1", "type": "betaGroups",
                "attributes": {"isInternalGroup": False},
                "relationships": {"app": {"data": {"id": APP_ID, "type": "apps"}}},
            }}), "application/json")
        raise AssertionError(f"Unexpected App Store Connect operation {spec.method} {spec.path}")

    api = FakeApi(handle)
    adapter = AppStoreConnectAdapter(
        api_transport=api,
        credential_provider=lambda profile: app_store_credentials(),
        upload_transport=FakeUploadTransport(),
        clock=lambda: NOW,
    )

    result = adapter.publish(request, "operation-external-group")

    assert result.status == "BLOCKED"
    assert result.provider_state == "TESTFLIGHT_GROUP_NOT_INTERNAL"
    assert not any(call.path == "/v1/buildUploads" for call in api.calls)


def test_testflight_assigns_only_verified_internal_group_for_selected_app(tmp_path):
    request = request_for(
        tmp_path, provider_id="app_store_connect", channel="testflight",
        metadata={"testflight_group_ids": ["internal-group-1"]},
    )
    calls = []

    def handle(spec):
        calls.append(spec)
        if spec.method == "GET" and spec.path == "/v1/betaGroups/internal-group-1?include=app":
            return ProviderResponse(200, json.dumps({"data": {
                "id": "internal-group-1", "type": "betaGroups",
                "attributes": {"isInternalGroup": True},
                "relationships": {"app": {"data": {"id": APP_ID, "type": "apps"}}},
            }}), "application/json")
        if spec.method == "POST" and spec.path == f"/v1/builds/{BUILD_ID}/relationships/betaGroups":
            body = json.loads(spec.body) if isinstance(spec.body, str) else spec.body
            assert body["data"] == [{"type": "betaGroups", "id": "internal-group-1"}]
            return ProviderResponse(204, "", None)
        if spec.method == "GET" and spec.path == f"/v1/builds/{BUILD_ID}/betaGroups?limit=200":
            return ProviderResponse(200, '{"data":[{"id":"internal-group-1","type":"betaGroups"}]}',
                                    "application/json")
        raise AssertionError(f"Unexpected App Store Connect operation {spec.method} {spec.path}")

    adapter = AppStoreConnectAdapter(
        api_transport=FakeApi(handle),
        credential_provider=lambda profile: app_store_credentials(),
        clock=lambda: NOW,
    )

    result = adapter._submit_testflight(request, APP_ID, BUILD_ID)

    assert result.status == "SUBMITTED"
    assert result.remote_id == BUILD_ID
    assert "assigned to verified internal" in result.summary
    assert len([call for call in calls if call.path == "/v1/betaGroups/internal-group-1?include=app"]) == 2


def test_ready_for_sale_requires_the_requested_build_relationship(tmp_path):
    api = FakeApi(lambda spec: ProviderResponse(200, json.dumps({"data": {
        "id": VERSION_ID, "type": "appStoreVersions",
        "relationships": {"build": {"data": {"id": "another-build", "type": "builds"}}},
    }}), "application/json"))
    adapter = AppStoreConnectAdapter(
        api_transport=api,
        credential_provider=lambda profile: app_store_credentials(),
        clock=lambda: NOW,
    )

    result = adapter._version_state_result({
        "id": VERSION_ID, "type": "appStoreVersions", "attributes": {"appStoreState": "READY_FOR_SALE"},
    }, BUILD_ID)

    assert result is not None
    assert result.status == "UNCERTAIN"
    assert result.provider_state == "VERSION_BUILD_READBACK_MISMATCH"


def test_empty_review_submission_blocks_duplicate_creation_retry(tmp_path):
    def handle(spec):
        if spec.method == "GET" and spec.path.startswith(f"/v1/apps/{APP_ID}/reviewSubmissions?"):
            return ProviderResponse(200, json.dumps({"data": [{
                "id": REVIEW_ID, "type": "reviewSubmissions", "attributes": {"state": "READY_FOR_REVIEW"},
            }]}), "application/json")
        if spec.method == "GET" and spec.path == f"/v1/reviewSubmissions/{REVIEW_ID}/items?include=appStoreVersion&limit=50":
            return ProviderResponse(200, '{"data":[]}', "application/json")
        raise AssertionError(f"Unexpected App Store Connect operation {spec.method} {spec.path}")

    adapter = AppStoreConnectAdapter(
        api_transport=FakeApi(handle),
        credential_provider=lambda profile: app_store_credentials(),
        clock=lambda: NOW,
    )

    submission, ambiguous = adapter._existing_review_submission(
        request_for(tmp_path, provider_id="app_store_connect"), APP_ID, VERSION_ID,
    )

    assert submission is None
    assert ambiguous is True


def test_build_upload_transport_rejects_untrusted_upload_hosts(tmp_path):
    request = request_for(tmp_path, provider_id="app_store_connect", artifact_name="Widget.ipa")
    uploader = FakeUploadTransport()
    adapter = AppStoreConnectAdapter(
        api_transport=FakeApi(lambda spec: ProviderResponse(200, '{"data":[]}', "application/json")),
        credential_provider=lambda profile: app_store_credentials(),
        upload_transport=uploader,
        clock=lambda: NOW,
    )

    with pytest.raises(ValueError, match="UPLOAD_HOST_NOT_ALLOWED"):
        adapter._upload_operation(
            {"method": "PUT", "url": "https://attacker.example/upload", "offset": 0,
             "length": 4, "requestHeaders": []},
            request.artifact_paths[0],
        )
    assert uploader.calls == []


def test_app_store_api_transport_uses_apple_host_and_profile_headers():
    captured = {}

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self, size=-1):
            return b'{"data":[]}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            return Response()

    transport = ProviderApiTransport(
        credential_provider=lambda profile: (captured.update({"profile": profile}) or "jwt-example-token"),
        opener_factory=lambda host, allowed_hosts: (captured.update({"host": host,
                                                                      "allowed_hosts": allowed_hosts}) or Opener()),
    )

    response = transport.request(ProviderRequest(
        "app_store_connect_api", "GET", "/v1/apps?limit=1",
    ))

    assert response.status == 200
    assert captured["url"] == "https://api.appstoreconnect.apple.com/v1/apps?limit=1"
    assert captured["host"] == "api.appstoreconnect.apple.com"
    assert captured["allowed_hosts"] == ("api.appstoreconnect.apple.com",)
    assert captured["profile"] == "app_store_connect_api"
    assert captured["headers"]["Authorization"] == "Bearer jwt-example-token"
    assert captured["headers"]["Accept"] == "application/json"
    assert "X-github-api-version" not in {name.casefold() for name in captured["headers"]}


def test_app_store_upload_operation_forwards_no_api_auth(tmp_path):
    request = request_for(tmp_path, provider_id="app_store_connect", artifact_name="Widget.pkg")
    uploader = FakeUploadTransport()
    adapter = AppStoreConnectAdapter(
        api_transport=FakeApi(lambda spec: ProviderResponse(200, '{"data":[]}', "application/json")),
        credential_provider=lambda profile: app_store_credentials(),
        upload_transport=uploader,
        clock=lambda: NOW,
    )
    operation = {
        "method": "PUT",
        "url": "https://store-030.blobstore.apple.com/upload?signature=temporary",
        "offset": 0,
        "length": request.artifact_paths[0].stat().st_size,
        "requestHeaders": [{"name": "Content-Type", "value": "application/octet-stream"},
                           {"name": "X-Apple-Signature", "value": "short-lived-signature"}],
    }

    assert adapter._upload_operation(operation, request.artifact_paths[0]) is True
    assert uploader.calls[0][1] == {
        "Content-Type": "application/octet-stream", "X-Apple-Signature": "short-lived-signature",
    }
    assert "Authorization" not in uploader.calls[0][1]
