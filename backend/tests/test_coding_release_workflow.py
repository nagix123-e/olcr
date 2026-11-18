from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from pathlib import Path
from threading import Barrier

import pytest

from olcr_api.coding_tasks import ReleasePreflight
from olcr_api.db import Database, SCHEMA_VERSION
from olcr_api.provider_research import ProviderEvidence
from olcr_api.release_workflow import (
    ReleaseAuthorization,
    ReleaseCoordinator,
    ReleaseRequest,
    ReleaseResult,
)


NOW = 1_800_000_000.0


def _database(tmp_path: Path, task_id: str = "task-1") -> Database:
    database = Database(str(tmp_path / "release.sqlite"))
    database.initialize()
    with database.connect() as db:
        db.execute(
            """INSERT INTO coding_tasks(
                   id,conversation_id,original_goal,status,activity,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (task_id, "conversation-1", "Release package", "RUNNING", "NONE", NOW, NOW),
        )
    return database


def _evidence(database: Database, task_id: str = "task-1") -> tuple[ProviderEvidence, ...]:
    references = (
        ProviderEvidence(
            task_id, "npm", "publishing", "Publishing requirements", "https://docs.npmjs.com/publish",
            "docs.npmjs.com", "VERIFIED", NOW, "npm package", "Untrusted reference claim: package visibility is explicit.",
        ),
        ProviderEvidence(
            task_id, "npm", "terms", "Terms of service", "https://docs.npmjs.com/policies/terms",
            "docs.npmjs.com", "VERIFIED", NOW, "npm package", "Untrusted reference claim: users must comply.",
        ),
    )
    for reference in references:
        database.save_coding_task_reference(reference)
    return references


def _request(
    database: Database,
    workspace: Path,
    *,
    task_id: str = "task-1",
    external_access: bool = True,
    artifact_name: str = "package.tgz",
    artifact_bytes: bytes = b"artifact-v1",
    metadata: dict[str, object] | None = None,
) -> ReleaseRequest:
    workspace.mkdir(parents=True, exist_ok=True)
    artifact = workspace / artifact_name
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(artifact_bytes)
    return ReleaseRequest(
        task_id=task_id,
        provider_id="npm",
        project_root=workspace,
        app_or_package="@example/widget",
        version="1.2.3",
        channel="latest",
        artifact_paths=(artifact,),
        metadata=metadata or {
            "agreement_accepted_by_user": True,
            "credentials_configured": True,
            "local_build_validated": True,
        },
        authorization=ReleaseAuthorization(
            explicit_intent=True,
            target_resolved=True,
            external_access_enabled=external_access,
            current_references=_evidence(database, task_id),
        ),
    )


class FakeAdapter:
    provider_id = "npm"

    def __init__(self, *, publish_results=None, inspect_results=None, preflight=None):
        self.publish_results = list(publish_results or [
            ReleaseResult("SUBMITTED", "PUBLISH_SUCCESS", "https://www.npmjs.com/package/example", "remote-1", "Submitted", False)
        ])
        self.inspect_results = list(inspect_results if inspect_results is not None else [
            ReleaseResult("SUBMITTED", "SUBMITTED", "https://www.npmjs.com/package/example", "remote-1", "Submitted", False)
        ] * 10)
        self.preflight_result = preflight or ReleasePreflight("READY", [], [])
        self.calls: list[str] = []
        self.operation_ids: list[str] = []

    def preflight(self, request: ReleaseRequest) -> ReleasePreflight:
        self.calls.append("preflight")
        return self.preflight_result

    def publish(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        self.calls.append("publish")
        self.operation_ids.append(operation_id)
        return self.publish_results.pop(0)

    def inspect(self, request: ReleaseRequest, operation_id: str) -> ReleaseResult:
        self.calls.append("inspect")
        self.operation_ids.append(operation_id)
        return self.inspect_results.pop(0)


def _operation(database: Database, task_id: str = "task-1") -> dict[str, object]:
    with database.connect() as db:
        row = db.execute(
            "SELECT operation_id FROM coding_release_operations WHERE task_id=?", (task_id,)
        ).fetchone()
    assert row is not None
    result = database.release_operation(str(row[0]))
    assert result is not None
    return result


def test_operation_transitions_record_blocked_ready_publishing_and_final_states(tmp_path):
    database = _database(tmp_path)
    workspace = tmp_path / "workspace"
    blocked = _request(database, workspace, external_access=False)
    adapter = FakeAdapter()
    coordinator = ReleaseCoordinator(database, adapter, clock=lambda: NOW)

    blocked_result = coordinator.publish(blocked)
    assert blocked_result.status == "BLOCKED"
    assert "publish" not in adapter.calls
    assert _operation(database)["state"] == "BLOCKED"

    ready = replace(
        blocked,
        authorization=replace(blocked.authorization, external_access_enabled=True),
    )
    assert coordinator.preflight(ready).status == "READY"
    submitted = coordinator.publish(ready)
    operation = _operation(database)
    assert submitted.status == "SUBMITTED"
    assert operation["state"] == "SUBMITTED"
    assert [event["state"] for event in operation["state_history"]] == [
        "BLOCKED", "READY", "PUBLISHING", "SUBMITTED"
    ]


def test_operation_persists_without_credentials_or_response_secrets(tmp_path):
    database = _database(tmp_path)
    workspace = tmp_path / "workspace"
    secret = "sensitive-provider-token-123"
    response_secret = "provider-response-secret-987654"
    request = _request(
        database,
        workspace,
        metadata={
            "agreement_accepted_by_user": True,
            "credentials_configured": True,
            "local_build_validated": True,
            "access_token": secret,
        },
    )
    adapter = FakeAdapter(publish_results=[
        ReleaseResult("FAILED", "access_token " + response_secret,
                      "https://www.npmjs.com/package/example?token=" + response_secret,
                      None, f"token={response_secret} access_token={secret}", False)
    ])
    result = ReleaseCoordinator(database, adapter, clock=lambda: NOW).publish(request)

    assert result.status == "FAILED"
    operation = _operation(database)
    serialized = repr(operation)
    assert secret not in serialized
    assert response_secret not in serialized
    assert operation["provider_state"] == "UNKNOWN"
    assert operation["remote_url"] is None
    assert "access_token" not in operation
    assert "artifact_path" not in operation
    assert len(operation["artifact_hashes"]) == 1
    assert len(operation["artifact_hashes"][0]) == 64
    with database.connect() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(coding_release_operations)")}
    assert not {"metadata_json", "request_json", "credentials", "token"} & columns


def test_same_task_provider_version_reuses_operation_key_and_does_not_publish_twice(tmp_path):
    database = _database(tmp_path)
    request = _request(database, tmp_path / "workspace")
    adapter = FakeAdapter()
    coordinator = ReleaseCoordinator(database, adapter, clock=lambda: NOW)

    first = coordinator.publish(request)
    operation_id = str(_operation(database)["operation_id"])
    second = coordinator.publish(request)

    assert first.status == second.status == "SUBMITTED"
    assert adapter.calls.count("publish") == 1
    assert set(adapter.operation_ids) == {operation_id}


def test_concurrent_publish_attempts_atomically_claim_one_ready_operation(tmp_path):
    database = _database(tmp_path)
    request = _request(database, tmp_path / "workspace")
    adapter = FakeAdapter()
    coordinator = ReleaseCoordinator(database, adapter, clock=lambda: NOW)
    ready_reads = Barrier(2)
    original_release_operation = database.release_operation

    def synchronized_release_operation(operation_id):
        operation = original_release_operation(operation_id)
        if operation is not None and operation["state"] == "READY":
            ready_reads.wait(timeout=3)
        return operation

    database.release_operation = synchronized_release_operation
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(coordinator.publish, request)
        second = pool.submit(coordinator.publish, request)
        results = [first.result(timeout=5), second.result(timeout=5)]

    assert all(result.status in {"SUBMITTED", "UNCERTAIN"} for result in results)
    assert adapter.calls.count("publish") == 1
    assert len(set(adapter.operation_ids)) == 1


@pytest.mark.parametrize(
    ("column", "value"),
    [("officiality", "CANDIDATE"), ("accessed_at", NOW - 2 * 24 * 60 * 60)],
)
def test_persisted_provider_references_must_still_be_verified_and_current(tmp_path, column, value):
    database = _database(tmp_path)
    request = _request(database, tmp_path / "workspace")
    with database.connect() as db:
        db.execute(f"UPDATE coding_task_references SET {column}=?", (value,))
    adapter = FakeAdapter()

    result = ReleaseCoordinator(database, adapter, clock=lambda: NOW).publish(request)

    assert result.status == "BLOCKED"
    assert "publish" not in adapter.calls


def test_published_operation_remains_idempotent_when_current_references_later_expire(tmp_path):
    database = _database(tmp_path)
    request = _request(database, tmp_path / "workspace")
    published = ReleaseResult(
        "PUBLISHED", "PUBLISHED", "https://www.npmjs.com/package/example", "remote-1", "Read back", False
    )
    adapter = FakeAdapter(publish_results=[published], inspect_results=[published])
    coordinator = ReleaseCoordinator(database, adapter, clock=lambda: NOW)

    first = coordinator.publish(request)
    operation = _operation(database)
    operation_id = str(operation["operation_id"])
    stale_references = tuple(replace(reference, accessed_at=NOW - 2 * 24 * 60 * 60)
                             for reference in request.authorization.current_references)
    stale_request = replace(request, authorization=replace(request.authorization, current_references=stale_references))

    repeated = coordinator.publish(stale_request)

    assert first.status == repeated.status == "PUBLISHED"
    assert database.release_operation(operation_id)["state"] == "PUBLISHED"
    assert adapter.calls.count("publish") == 1


def test_rebuilt_artifact_cannot_replace_existing_version(tmp_path):
    database = _database(tmp_path)
    workspace = tmp_path / "workspace"
    original = _request(database, workspace, artifact_bytes=b"artifact-v1")
    adapter = FakeAdapter()
    coordinator = ReleaseCoordinator(database, adapter, clock=lambda: NOW)
    coordinator.publish(original)

    rebuilt = _request(database, workspace, artifact_bytes=b"artifact-v2")
    result = coordinator.publish(rebuilt)

    assert result.status == "BLOCKED"
    assert adapter.calls.count("publish") == 1
    assert "artifact" in result.summary.casefold() or "version" in result.summary.casefold()


def test_uncertain_operation_inspects_remote_before_explicit_retry(tmp_path):
    database = _database(tmp_path)
    request = _request(database, tmp_path / "workspace")
    adapter = FakeAdapter(
        publish_results=[
            ReleaseResult("UNCERTAIN", "TIMEOUT", None, None, "Publish response timed out", True),
            ReleaseResult("PUBLISHED", "CREATED", "https://www.npmjs.com/package/example", "remote-2", "Created", False),
        ],
        inspect_results=[
            ReleaseResult("FAILED", "NOT_FOUND", None, None, "Remote release not found", False),
            ReleaseResult("PUBLISHED", "PUBLISHED", "https://www.npmjs.com/package/example", "remote-2", "Read back", False),
        ],
    )
    coordinator = ReleaseCoordinator(database, adapter, clock=lambda: NOW)

    uncertain = coordinator.publish(request)
    assert uncertain.status == "UNCERTAIN"
    assert _operation(database)["state"] == "UNCERTAIN"

    published = coordinator.publish(request)
    assert published.status == "PUBLISHED"
    assert adapter.calls == ["preflight", "publish", "inspect", "preflight", "publish", "inspect"]


def test_inspect_confirms_remote_state_before_marking_published(tmp_path):
    database = _database(tmp_path)
    request = _request(database, tmp_path / "workspace")
    adapter = FakeAdapter(
        publish_results=[ReleaseResult("UNCERTAIN", "TIMEOUT", None, None, "Timed out", True)],
        inspect_results=[
            ReleaseResult("PUBLISHED", "PUBLISHED", "https://www.npmjs.com/package/example", "remote-3", "Read back", False)
        ],
    )
    coordinator = ReleaseCoordinator(database, adapter, clock=lambda: NOW)

    uncertain = coordinator.publish(request)
    operation_id = str(_operation(database)["operation_id"])
    assert uncertain.status == "UNCERTAIN"

    inspected = coordinator.inspect(operation_id)
    operation = database.release_operation(operation_id)
    assert inspected.status == "PUBLISHED"
    assert operation is not None
    assert operation["state"] == "PUBLISHED"
    assert operation["remote_id"] == "remote-3"
    assert operation["confirmed_at"] == NOW


def test_interrupted_publishing_operation_requires_confirmed_absence_before_retry(tmp_path):
    database = _database(tmp_path)
    request = _request(database, tmp_path / "workspace")
    reference_ids = [str(row["id"]) for row in database.coding_task_references("task-1")]
    artifact_hash = hashlib.sha256(b"artifact-v1").hexdigest()
    operation_id = "relop_interrupted"
    database.create_release_operation(
        operation_id=operation_id,
        task_id="task-1",
        provider_id="npm",
        idempotency_key="a" * 64,
        target="@example/widget",
        version="1.2.3",
        channel="latest",
        artifact_hashes=[artifact_hash],
        reference_ids=reference_ids,
        state="READY",
        now=NOW,
    )
    database.claim_release_operation(operation_id, now=NOW)
    with pytest.raises(ValueError, match="absence inspection"):
        database.update_release_operation(operation_id, state="READY", now=NOW + 1)
    adapter = FakeAdapter(
        inspect_results=[ReleaseResult("FAILED", "NOT_FOUND", None, None, "Remote release not found", False)]
    )
    coordinator = ReleaseCoordinator(
        database, adapter, clock=lambda: NOW + 1, request_resolver=lambda _operation: request
    )

    coordinator.inspect(operation_id)

    operation = database.release_operation(operation_id)
    assert operation is not None
    assert operation["state"] == "READY"


def test_artifact_outside_workspace_blocks_before_adapter_publish(tmp_path):
    database = _database(tmp_path)
    request = _request(database, tmp_path / "workspace")
    external = tmp_path / "outside.tgz"
    external.write_bytes(b"outside")
    request = replace(request, artifact_paths=(external,))
    adapter = FakeAdapter()

    result = ReleaseCoordinator(database, adapter, clock=lambda: NOW).publish(request)

    assert result.status == "BLOCKED"
    assert "publish" not in adapter.calls


def test_v40_migration_preserves_tasks_and_saved_provider_references(tmp_path):
    database = _database(tmp_path)
    references = _evidence(database)
    with database.connect() as db:
        db.execute("DROP TABLE IF EXISTS coding_release_operations")
        db.execute("UPDATE schema_version SET version=40")

    database.initialize()

    with database.connect() as db:
        version = db.execute("SELECT version FROM schema_version").fetchone()[0]
        task = db.execute("SELECT id FROM coding_tasks WHERE id='task-1'").fetchone()
    assert SCHEMA_VERSION == 41
    assert version == 41
    assert task is not None
    assert [row["url"] for row in database.coding_task_references("task-1")] == [
        references[0].url, references[1].url
    ]
