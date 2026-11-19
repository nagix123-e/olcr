import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_import_profile = tempfile.TemporaryDirectory(prefix="olcr-subagent-settings-api-")
with patch.dict(os.environ, {"OLCR_DB_PATH": str(Path(_import_profile.name) / "import.db"),
                            "OLCR_ALLOWED_ROOTS": _import_profile.name}):
    from fastapi.testclient import TestClient
    import olcr_api.app as api

from olcr_api.config import Settings
from olcr_api.db import Database

WARNING_VERSION = "2026-11-v1"

class CodingSubagentSettingsTests(unittest.TestCase):
    def setUp(self):
        self.profile = tempfile.TemporaryDirectory(prefix="olcr-subagent-settings-")
        self.addCleanup(self.profile.cleanup)
        self.db = Database(str(Path(self.profile.name) / "test.db"))
        self.db.initialize()
        self.initial = Settings(db_path=self.db.path, context_budget=4096, task_manager_enabled=False)
        self.client = TestClient(api.app)

    def api_settings(self):
        def apply(candidate):
            api.settings = candidate
        return (patch.object(api, "db", self.db), patch.object(api, "settings", self.initial),
                patch.object(api, "rebuild", side_effect=apply))

    def test_coding_subagents_default_off(self):
        self.assertFalse(Settings.from_env().coding_subagents_enabled)
        response = self.client.get("/api/settings")
        self.assertEqual(200, response.status_code, response.text)
        self.assertFalse(response.json()["coding_subagents_enabled"])
        self.assertEqual(WARNING_VERSION,
                         response.json()["coding_subagents_warning_version"])

    def test_coding_subagents_setting_is_independent_of_task_manager(self):
        for enabled in (False, True):
            candidate = Settings(task_manager_enabled=enabled)
            self.assertFalse(candidate.coding_subagents_enabled)
            self.assertFalse(candidate.with_overrides({"task_manager_enabled": not enabled})
                              .coding_subagents_enabled)

    def test_model_or_task_manager_update_cannot_enable_subagents(self):
        with self.api_settings()[0], self.api_settings()[1], self.api_settings()[2]:
            response = self.client.patch("/api/settings/capabilities", json={"task_manager_enabled": True})
            self.assertEqual(200, response.status_code, response.text)
            self.assertFalse(response.json()["coding_subagents_enabled"])
            response = self.client.put("/api/settings", json={
                "ollama_endpoint": "http://127.0.0.1:11434",
                "allowed_roots": [], "context_budget": 4096,
                "coding_subagents_enabled": True,
                "coding_subagents_warning_ack_revision": WARNING_VERSION,
            })
            self.assertEqual(422, response.status_code, response.text)
            self.assertFalse(api.settings.coding_subagents_enabled)

    def test_enabling_requires_current_warning_acknowledgement(self):
        with self.api_settings()[0], self.api_settings()[1], self.api_settings()[2]:
            for revision in (None, "old-warning", ""):
                payload = {"coding_subagents_enabled": True}
                if revision is not None:
                    payload["coding_subagents_warning_ack_revision"] = revision
                response = self.client.patch("/api/settings/capabilities", json=payload)
                self.assertEqual(422, response.status_code, response.text)
                self.assertFalse(api.settings.coding_subagents_enabled)
            response = self.client.patch("/api/settings/capabilities", json={
                "coding_subagents_enabled": True,
                "coding_subagents_warning_ack_revision": WARNING_VERSION,
            })
            self.assertEqual(200, response.status_code, response.text)
            self.assertTrue(response.json()["coding_subagents_enabled"])
            self.assertEqual(WARNING_VERSION,
                             response.json()["coding_subagents_warning_ack_revision"])

    def test_malformed_persisted_flag_and_stale_warning_ack_fail_closed(self):
        malformed = Settings().with_overrides({"coding_subagents_enabled": "true"})
        self.assertFalse(malformed.coding_subagents_enabled)
        stale = Settings().with_overrides({"coding_subagents_enabled": True,
                                           "coding_subagents_warning_ack_revision": "old-warning"})
        self.assertFalse(stale.coding_subagents_enabled)
        with self.assertRaises(ValueError):
            Settings(coding_subagents_enabled=True).validated()

    def test_generic_settings_input_rejects_malformed_subagent_flag(self):
        response = self.client.put("/api/settings", json={
            "ollama_endpoint": "http://127.0.0.1:11434",
            "allowed_roots": [], "context_budget": 4096,
            "coding_subagents_enabled": "true",
        })
        self.assertEqual(422, response.status_code, response.text)

    def test_generic_settings_save_preserves_an_unchanged_subagent_flag(self):
        with self.api_settings()[0], self.api_settings()[1], self.api_settings()[2]:
            payload = {"ollama_endpoint": "http://127.0.0.1:11434",
                       "allowed_roots": [], "context_budget": 4096,
                       "coding_subagents_enabled": False}
            response = self.client.put("/api/settings", json=payload)
            self.assertEqual(200, response.status_code, response.text)
            self.assertFalse(response.json()["coding_subagents_enabled"])
            response = self.client.patch("/api/settings/capabilities", json={
                "coding_subagents_enabled": True,
                "coding_subagents_warning_ack_revision": WARNING_VERSION,
            })
            self.assertEqual(200, response.status_code, response.text)
            payload["coding_subagents_enabled"] = True
            response = self.client.put("/api/settings", json=payload)
            self.assertEqual(200, response.status_code, response.text)
            self.assertTrue(response.json()["coding_subagents_enabled"])
            persisted = Settings(db_path=self.db.path).with_overrides(self.db.load_application_settings())
            self.assertTrue(persisted.coding_subagents_enabled)
            self.assertEqual(WARNING_VERSION, persisted.coding_subagents_warning_ack_revision)
            payload["coding_subagents_enabled"] = False
            response = self.client.put("/api/settings", json=payload)
            self.assertEqual(200, response.status_code, response.text)
            self.assertTrue(response.json()["coding_subagents_enabled"])

    def test_disable_stops_executor_before_snapshotting_active_workers_to_drain(self):
        self.db.create_conversation("Conversation", time.time(), "conversation-1")
        self.db.create_coding_task("task-late-worker", "conversation-1", "Build",
                                   "RUNNING", "IMPLEMENTATION", {}, time.time())
        now = time.time()
        with self.db.connect() as connection:
            connection.execute("""INSERT INTO coding_subagent_proposals(
                id,task_id,phase_id,plan_revision,plan_hash,assessment_hash,proposal_hash,
                model_binding_json,role_ids_json,status,authorization_json,created_at,updated_at)
                VALUES('proposal-late','task-late-worker','phase-1',0,?,?,?,?,?,'QUEUED','{}',?,?)""",
                ("a" * 64, "b" * 64, "c" * 64, "{}", '["implementation_worker"]', now, now))
            connection.execute("""INSERT INTO coding_subagent_runs(
                id,proposal_id,task_id,phase_id,plan_revision,role_id,prompt_version,
                model_binding_json,assigned_paths_json,status,usage_json,created_at,updated_at)
                VALUES('worker-late','proposal-late','task-late-worker','phase-1',0,
                'implementation_worker','2026-11-v1','{}','[\"backend/a.py\"]','QUEUED','{}',?,?)""",
                (now, now))

        enabled_settings = Settings(
            db_path=self.db.path, context_budget=4096, task_manager_enabled=False,
            coding_subagents_enabled=True,
            coding_subagents_warning_ack_revision=WARNING_VERSION)
        old_enabled = api.coding_subagent_executor.enabled
        self.addCleanup(api.coding_subagent_executor.set_enabled, old_enabled)
        events = []
        original_set_enabled = api.coding_subagent_executor.set_enabled
        injected = False

        def set_enabled(enabled):
            nonlocal injected
            events.append(("enabled", enabled))
            result = original_set_enabled(enabled)
            if enabled is False and not injected:
                injected = True
                with self.db.connect() as connection:
                    connection.execute("UPDATE coding_subagent_proposals SET status='RUNNING' WHERE id='proposal-late'")
                    connection.execute("UPDATE coding_subagent_runs SET status='RUNNING' WHERE id='worker-late'")
            return result

        def cancel_all(*args, **kwargs):
            events.append(("cancel", None))
            return Database.cancel_all_coding_subagents(self.db, *args, **kwargs)

        def drain(task_id):
            events.append(("drain", task_id))

        with patch.object(api, "db", self.db), patch.object(api, "settings", enabled_settings), \
                patch.object(api, "rebuild", side_effect=lambda candidate: setattr(api, "settings", candidate)), \
                patch.object(api.coding_subagent_executor, "set_enabled", side_effect=set_enabled), \
                patch.object(self.db, "cancel_all_coding_subagents", side_effect=cancel_all), \
                patch.object(api, "_drain_coding_subagent_mutations", side_effect=drain):
            response = self.client.patch("/api/settings/capabilities", json={
                "coding_subagents_enabled": False})
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn(("drain", "task-late-worker"), events)
        self.assertLess(events.index(("enabled", False)), events.index(("cancel", None)))
        self.assertLess(events.index(("cancel", None)), events.index(("drain", "task-late-worker")))


if __name__ == "__main__":
    unittest.main()
