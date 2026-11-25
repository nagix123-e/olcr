"""Launch questions use the current workspace without executing anything."""
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import olcr_api.app as api
from olcr_api.db import Database


@pytest.fixture
def workspace_chat(tmp_path, monkeypatch):
    root = tmp_path / "site with spaces"
    root.mkdir()
    db = Database(str(tmp_path / "chat.sqlite"))
    db.initialize()
    db.create_project("Sample site", str(root), time.time(), "site")
    db.create_conversation("Site task", time.time(), "chat", "site")
    db.create_coding_task("build", "chat", "Implement the site", "COMPLETED", "NONE", None, time.time())
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api, "_gui_session_token", "launch-help-test")
    client = TestClient(api.app, headers={"X-OLCR-Session": "launch-help-test"})
    return client, db, root


@pytest.mark.parametrize("stream", [False, True])
def test_completed_task_launch_question_uses_local_vite_evidence(workspace_chat, stream):
    client, db, root = workspace_chat
    (root / "package.json").write_text(json.dumps({"devDependencies": {"vite": "^8.0.0"}}))
    (root / "package-lock.json").write_text("{}")
    (root / "index.html").write_text("<html></html>")
    before = db.coding_task("build")
    with patch.object(api, "execute_external_tool", side_effect=AssertionError("no external lookup")), \
         patch.object(api.runtime, "execute", side_effect=AssertionError("no Brain execution")), \
         patch.object(api, "_coding_scheduler_wake") as wake:
        response = client.post("/api/chat/stream" if stream else "/api/chat", json={
            "message": "ターミナルからこれを起動する方法を教えて", "project_id": "site",
            "conversation_id": "chat", "coding_task_id": "build", "coding_followup": "auto",
        })
    assert response.status_code == 200, response.text
    assert "WORKSPACE_LAUNCH_HELP" in response.text
    assert "./node_modules/.bin/vite" in response.text
    assert "npm run dev" not in response.text
    assert db.coding_task("build") == before
    assert len(db.coding_tasks("chat")) == 1
    assert len(db.conversation("chat")["messages"]) == 2
    wake.set.assert_not_called()


@pytest.mark.parametrize(("manifest", "marker", "expected"), [
    ({"packageManager": "pnpm@10.0.0", "scripts": {"dev": "vite"}}, None, "pnpm run dev"),
    ({"scripts": {"start": "node app.js"}}, "package-lock.json", "npm run start"),
    (None, "index.html", "python3 -m http.server"),
    ({"name": "undocumented"}, None, "起動コマンドを確認できません"),
])
def test_launch_answer_uses_available_project_metadata(workspace_chat, manifest, marker, expected):
    client, db, root = workspace_chat
    if manifest is not None:
        (root / "package.json").write_text(json.dumps(manifest))
    if marker:
        (root / marker).touch()
    response = client.post("/api/chat", json={
        "message": "このアプリの起動手順を教えて", "conversation_id": "chat",
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["response_owner"] == "WORKSPACE_LAUNCH_HELP"
    assert expected in data["response"]
    assert len(db.coding_tasks("chat")) == 1


def test_projectless_question_requests_target_instead_of_search(workspace_chat):
    client, _, _ = workspace_chat
    response = client.post("/api/chat", headers={"X-OLCR-Language": "en"}, json={
        "message": "How do I run this project locally?", "project_id": None,
    })
    assert response.status_code == 200, response.text
    assert "I couldn't locate the workspace" in response.json()["response"]


def test_launch_help_uses_selected_task_workspace(workspace_chat):
    client, _, root = workspace_chat
    worktree = root.parent / "task worktree"
    worktree.mkdir()
    (worktree / "package.json").write_text('{"packageManager":"npm@11","scripts":{"dev":"vite"}}')
    with patch.object(api.task_workspaces.service, "root_for", return_value=str(worktree)) as selected:
        response = client.post("/api/chat", json={
            "message": "ターミナルからこれを起動する方法を教えて", "conversation_id": "chat",
            "coding_task_id": "build", "coding_followup": "auto",
        })
    assert response.status_code == 200, response.text
    assert str(worktree) in response.json()["response"]
    assert str(root) not in response.json()["response"]
    selected.assert_called_once_with("build", str(root))


def test_symlinked_manifest_is_not_used(workspace_chat):
    client, _, root = workspace_chat
    outside = root.parent / "outside.json"
    outside.write_text('{"packageManager":"npm@11","scripts":{"dev":"vite"}}')
    (root / "package.json").symlink_to(outside)
    response = client.post("/api/chat", json={
        "message": "このアプリの起動手順を教えて", "conversation_id": "chat",
    })
    assert response.status_code == 200, response.text
    assert "package.json を読み取れない" in response.json()["response"]
    assert "npm run dev" not in response.json()["response"]


@pytest.mark.parametrize("message", [
    "これをターミナルから起動して",
    "起動方法も教えて。先に src/app.js を修正して",
    "Webでこのアプリの起動方法を教えて",
    "How do I start PostgreSQL on Ubuntu?",
    "> このアプリの起動手順を教えて",
])
def test_launch_help_does_not_capture_other_requests(message):
    from olcr_api.workspace_launch import requested

    assert not requested(message)
