import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Never open the real application profile, even when this test runs alone.
_import_profile = tempfile.TemporaryDirectory(prefix="olcr-memory-api-")
with patch.dict(os.environ, {"OLCR_DB_PATH": str(Path(_import_profile.name) / "import.db"),
                            "OLCR_ALLOWED_ROOTS": _import_profile.name}):
    from fastapi.testclient import TestClient
    import olcr_api.app as api

from olcr_api.config import Settings
from olcr_api.db import Database


class MemorySettingsAPITests(unittest.TestCase):
    def setUp(self):
        self.profile = tempfile.TemporaryDirectory(prefix="olcr-memory-settings-")
        self.addCleanup(self.profile.cleanup)
        self.db = Database(str(Path(self.profile.name) / "test.db"))
        self.db.initialize()
        self.initial = Settings(db_path=self.db.path, context_budget=4096,
                                conversation_memory_cross_project_enabled=True)
        self.client = TestClient(api.app)

    def test_narrow_patch_retains_memory_and_round_trips_from_database(self):
        self.db.create_conversation("Fixture", time.time(), "chat")
        self.db.add_message("chat", "user", "stored memory", time.time(), "u")
        self.db.add_message("chat", "assistant", "retained", time.time(), "a")
        turn = self.db.completed_turns()[0]
        self.db.save_memory_embedding(turn, self.initial.embedding_model, [1., 0.], time.time(),
                                      api.ConversationMemory.INDEX_VERSION)
        def apply(candidate):
            api.settings = candidate
        with patch.object(api, "db", self.db), patch.object(api, "settings", self.initial), patch.object(api, "rebuild", side_effect=apply):
            response = self.client.patch("/api/settings/memory", json={
                "conversation_memory_cross_project_enabled": False,
                "conversation_memory_min_relevance": .8})
            self.assertEqual(200, response.status_code, response.text)
            public = self.client.get("/api/settings").json()
            self.assertFalse(public["conversation_memory_cross_project_enabled"])
            self.assertEqual(.8, public["conversation_memory_min_relevance"])
            self.assertEqual(4096, public["context_budget"])
            persisted = self.initial.with_overrides(self.db.load_application_settings())
            self.assertFalse(persisted.conversation_memory_cross_project_enabled)
            self.assertEqual(.8, persisted.conversation_memory_min_relevance)
            rows = self.db.memory_embeddings(self.initial.embedding_model, 2, api.ConversationMemory.INDEX_VERSION)
            self.assertEqual(1, len(rows))
            self.assertEqual("retained", rows[0]["assistant_message"])

    def test_empty_unknown_invalid_or_nonfinite_patch_never_persists(self):
        cases = ({}, {"context_budget": 8000}, {"conversation_memory_min_relevance": -.1},
                 {"conversation_memory_min_relevance": 1.1}, {"conversation_memory_min_relevance": True},
                 {"conversation_memory_cross_project_enabled": "false"})
        with patch.object(api, "db", self.db), patch.object(api, "settings", self.initial), patch.object(api, "rebuild") as rebuild:
            for payload in cases:
                response = self.client.patch("/api/settings/memory", json=payload)
                self.assertEqual(422, response.status_code, response.text)
            for text in ('NaN', 'Infinity', '-Infinity'):
                response = self.client.patch("/api/settings/memory", content='{"conversation_memory_min_relevance":'+text+'}',
                                             headers={"content-type": "application/json"})
                self.assertEqual(422, response.status_code, response.text)
            rebuild.assert_not_called()
            self.assertEqual({}, self.db.load_application_settings())
