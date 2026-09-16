import json
import tempfile
import time
import unittest
from pathlib import Path

from olcr_api.config import Settings
from olcr_api.db import Database


class SettingsProjectContextIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.db = Database(str(Path(self.tmp.name) / "olcr.sqlite"))
        self.db.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_context_does_not_enter_application_settings_and_migrates(self):
        value = {"project_id": "p", "current_subject": "Tetris web game", "revision": 1}
        self.db.save_setting("conversation_project_context:c", value, time.time())
        result = self.db.migrate_legacy_conversation_project_contexts()
        self.assertEqual(1, result["migrated"])
        self.assertNotIn("conversation_project_context:c", self.db.load_application_settings())
        self.assertEqual(value, self.db.load_conversation_project_context("c"))
        self.assertEqual(0, self.db.migrate_legacy_conversation_project_contexts()["legacy_rows"])
        self.assertEqual(value, self.db.load_conversation_project_context("c"))

    def test_malformed_context_is_retained_but_never_breaks_settings(self):
        self.db.save_setting("conversation_project_context:bad", {"not": "a context"}, time.time())
        result = self.db.migrate_legacy_conversation_project_contexts()
        self.assertEqual(1, result["malformed"])
        self.assertNotIn("conversation_project_context:bad", self.db.load_application_settings())
        self.assertNotIn("conversation_project_context:bad", Settings.from_env().with_overrides(self.db.load_application_settings()).public_dict())

    def test_strict_unknown_application_setting_is_preserved(self):
        with self.assertRaises(ValueError):
            Settings.from_env().with_overrides({"genuinely_invalid_setting": True})

    def test_project_context_round_trip_uses_dedicated_store(self):
        value = {"project_id": "p", "workspace_path": self.tmp.name, "current_subject": "Tetris project", "revision": 2}
        self.db.save_conversation_project_context("c", value, time.time())
        reopened = Database(self.db.path)
        reopened.initialize()
        self.assertEqual(value, reopened.load_conversation_project_context("c"))
        with reopened.connect() as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM application_settings WHERE key LIKE 'conversation_project_context:%'").fetchone()[0])
