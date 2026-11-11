"""Resume control feedback must survive the persisted-task HTTP response."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import olcr_api.app as api
from olcr_api.db import Database


class TaskControlFeedbackTests(unittest.TestCase):
    def setUp(self):
        api.stop_coding_scheduler()
        temporary = tempfile.TemporaryDirectory(prefix="olcr-control-feedback-")
        self.addCleanup(temporary.cleanup)
        database = Database(str(Path(temporary.name) / "control.db"))
        database.initialize()
        database.create_project("fixture", temporary.name, time.time(), "p")
        database.create_conversation("fixture", time.time(), "c", "p")
        database.create_coding_task("t", "c", "Fix fixture", "BLOCKED", "NONE", None, time.time())
        self.addCleanup(patch.stopall)
        patch.object(api, "db", database).start()
        patch.object(api, "settings", api.settings.with_overrides({"permission_level": "automatic"})).start()
        patch.object(api, "_gui_session_token", "").start()
        self.client = TestClient(api.app)

    def test_resume_feedback_is_returned_with_the_authoritative_task(self):
        for status, message in (("BLOCKED", "Recovery denied: bounded budget exhausted."),
                                ("QUEUED", "The saved task was queued.")):
            with self.subTest(status=status):
                def control(task, **_):
                    api.db.update_coding_task("t", status=status)
                    return {"coding_task_id": "t", "response": message,
                            "progress_event": {"type": "coding_task_progress", "status": status}}

                with patch.object(api, "_resume_coding_task_control", side_effect=control):
                    response = self.client.patch("/api/coding-tasks/t", json={"resume": True})
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual("t", response.json()["id"])
                self.assertEqual(status, response.json()["status"])
                self.assertEqual(message, response.json().get("control_response"))
                self.assertNotIn("control_response", api.db.coding_task("t"))


if __name__ == "__main__":
    unittest.main()
