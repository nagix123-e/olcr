"""Isolated API smoke tests: no model, network, GUI or coding runner."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

_isolated = tempfile.TemporaryDirectory(prefix="olcr-graphify-api-", dir="/private/tmp")
with patch.dict(os.environ, {"OLCR_DB_PATH": str(Path(_isolated.name) / "startup.sqlite"),
                            "OLCR_ALLOWED_ROOTS": _isolated.name,
                            "OLCR_TASK_MANAGER_ENABLED": "false"}):
    import olcr_api.app as api

from fastapi.testclient import TestClient
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.graphify_rag import GraphifyRAG
import test_graphify_rag as fixtures


class Model:
    def __init__(self): self.messages = []
    def generate(self, messages, model, stream=False, **kwargs):
        self.messages.append(messages)
        result = {"text": "Graph-backed answer", "latency_ms": 0}
        return iter([result]) if stream else result


class GraphifyChatTests(unittest.TestCase):
    def setUp(self):
        api.stop_coding_scheduler()
        self.fixture = fixtures.GraphifyTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.old = api.db, api.settings
        api.db = Database(str(self.fixture.root / "api.sqlite")); api.db.initialize()
        api.rebuild(Settings(db_path=api.db.path, allowed_roots=(str(self.fixture.root),),
                             vector_enabled=False, task_manager_enabled=False, external_access_enabled=False))
        api.retrieval.graphify = GraphifyRAG()
        self.model = Model(); api.runtime.model = self.model
        api.db.create_project("graph", str(self.fixture.root), time.time(), "graph")
        api.db.create_conversation("graph", time.time(), "graph-chat", "graph")
        self.client = TestClient(api.app)
        self.addCleanup(self.restore)

    def restore(self):
        api.stop_coding_scheduler()
        api.db = self.old[0]; api.rebuild(self.old[1])

    def test_stream_repository_evidence(self):
        response = self.client.post("/api/chat/stream", json={"project_id": "graph", "conversation_id": "graph-chat", "message": "Explain alpha function"})
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        done = next(event for event in events if event["type"] == "done")
        self.assertTrue(any(row.get("method") == "graphify" for row in done["task"]["selected_context"]),
                        done["task"]["selected_context"])
        self.assertTrue(any("Graphify is the authoritative" in row["content"] for row in self.model.messages[-1]))

    def test_stream_explicit_search_synthesis(self):
        response = self.client.post("/api/chat/stream", json={"project_id": "graph", "conversation_id": "graph-chat", "message": "search alpha and explain"})
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        done = next(event for event in events if event["type"] == "done")
        self.assertTrue(any(row.get("method") == "graphify" for row in done["task"]["selected_context"]),
                        done["task"]["selected_context"])
        self.assertTrue(any("Graphify is the authoritative" in row["content"] for row in self.model.messages[-1]))

    def test_stream_explicit_search_keeps_evidence_with_large_skill_context(self):
        skill = {
            "skill": {"name": "large-test-skill", "source": "test", "content_hash": "test"},
            "content": "background " * 20_000,
            "routing_evidence": {"selected_skill": "large-test-skill"},
        }
        with patch.object(api, "_automatic_skill_selection", return_value=skill):
            response = self.client.post("/api/chat/stream", json={
                "project_id": "graph", "conversation_id": "graph-chat",
                "message": "search alpha and explain",
            })
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        done = next(event for event in events if event["type"] == "done")
        self.assertTrue(any(row.get("method") == "graphify" for row in done["task"]["selected_context"]),
                        done["task"]["selected_context"])
        prompt = "\n".join(row["content"] for row in self.model.messages[-1])
        self.assertIn("Selected evidence:", prompt)
        self.assertIn("alpha", prompt)

    def test_normal_chat_repository_evidence(self):
        response = self.client.post("/api/chat", json={"project_id": "graph", "conversation_id": "graph-chat", "message": "Explain alpha function"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(row.get("method") == "graphify" for row in response.json()["task"]["selected_context"]))

    def test_stream_without_graph_preserves_ordinary_chat(self):
        api.retrieval.graphify.enabled = False
        response = self.client.post("/api/chat/stream", json={"project_id": "graph", "conversation_id": "graph-chat", "message": "Write a short poem"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(any("Graphify" in row["content"] for row in self.model.messages[-1]))


if __name__ == "__main__":
    try: unittest.main()
    finally: api.stop_coding_scheduler(); _isolated.cleanup()
