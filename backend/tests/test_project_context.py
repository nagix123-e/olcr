import tempfile
import time
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import olcr_api.app as api
from olcr_api.db import Database
from olcr_api.models import Task


class ProjectContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db = api.db
        api.db = Database(str(Path(self.tmp.name) / "context.sqlite")); api.db.initialize()
        api.db.create_project("Tetris", self.tmp.name, time.time(), "tetris")
        api.db.create_project("OLCR", self.tmp.name, time.time(), "olcr")
        api.db.create_conversation("Tetris", time.time(), "tetris-chat", "tetris")
        api.db.create_conversation("OLCR", time.time(), "olcr-chat", "olcr")
        self.client = TestClient(api.app)

    def tearDown(self):
        api.db = self.old_db
        self.tmp.cleanup()

    def send(self, conversation, project, message, **extra):
        return self.client.post("/api/chat", json={"conversation_id": conversation, "project_id": project, "message": message, **extra})

    def test_context_survives_reload_and_is_used_for_image_follow_up(self):
        with patch.object(api.runtime, "execute", return_value=(Task("first"), "noted")):
            self.assertEqual(200, self.send("tetris-chat", "tetris", "ReactでテトリスのWebゲームを作る").status_code)
        reopened = Database(api.db.path); reopened.initialize()
        self.assertEqual("Tetris web game", reopened.load_settings()["conversation_project_context:tetris-chat"]["current_subject"])
        captured = {}
        def image(message, image, context):
            captured["context"] = context
            return Task(message), "grounded"
        with patch.object(api.runtime, "execute_image", side_effect=image):
            result = self.send("tetris-chat", "tetris", "スクショを見ればわかるように、そもそもゲームが開始しない", image={"name": "screen.png", "mime_type": "image/png", "data_url": "data:image/png;base64,AA=="})
        self.assertEqual(200, result.status_code)
        self.assertIn('"current_subject": "Tetris web game"', captured["context"])
        self.assertIn("React", captured["context"])
        self.assertIn("The current project does not start.", captured["context"])
        self.assertIn("ACTIVE_CONVERSATION", captured["context"])

    def test_project_context_is_isolated_and_current_turn_can_change_subject(self):
        with patch.object(api.runtime, "execute", return_value=(Task("x"), "ok")):
            self.send("tetris-chat", "tetris", "テトリスを作る")
            self.send("olcr-chat", "olcr", "OLCR Webサイトを作る")
        captured = {}
        def execute(message, approved, context, **kwargs):
            captured["context"] = context
            return Task(message), "ok"
        with patch.object(api.runtime, "execute", side_effect=execute):
            self.send("tetris-chat", "tetris", "別件としてOLCRのWebサイトを直したい")
        self.assertIn('"current_subject": "OLCR website"', captured["context"])
        self.assertNotIn('"project_id": "olcr"', captured["context"])

    def test_planning_and_coding_contexts_are_bounded(self):
        api.db.create_interactive_planning("tetris-chat", [{"id":"Q1", "question":"操作", "options":{"A":"キー"}}], {"SIL": False, "SUI": True})
        api.db.create_coding_task("task-1", "tetris-chat", "ゲームを開始できるようにする", "QUEUED", "PLANNING", None, time.time())
        captured = {}
        def execute(message, approved, context, **kwargs):
            captured["context"] = context
            return Task(message), "ok"
        with patch.object(api.runtime, "execute", side_effect=execute):
            self.send("tetris-chat", "tetris", "原因を確認して")
        self.assertIn("INTERACTIVE_PLANNING_STATE", captured["context"])
        self.assertIn("ACTIVE_CODING_TASK_STATE", captured["context"])
        self.assertLess(len(captured["context"]), 12000)

    def test_current_web_request_remains_isolated_from_prior_project_context(self):
        with patch.object(api.runtime, "execute", return_value=(Task("x"), "ok")):
            self.send("tetris-chat", "tetris", "テトリスを作る")
        captured = {}
        def execute(message, approved, context, **kwargs):
            captured["context"] = context
            return Task(message), "ok"
        with patch.object(api.runtime, "execute", side_effect=execute):
            self.send("tetris-chat", "tetris", "今日のニュースをweb検索して")
        self.assertNotIn("ACTIVE_PROJECT_CONTEXT", captured["context"])

    def test_image_main_model_receives_vision_and_same_project_context(self):
        captured = {}
        visual = {"elements": [], "text": [{"content": "Score 0 / Lines 0 / Level 1", "confidence": 0.99}],
                  "relationships": [], "anomalies": [], "confidence": 0.99, "uncertainty": []}
        def brain(messages, text):
            captured["packet"] = messages[0]["content"]
            return {"text": "The rendered Tetris UI has not entered gameplay."}
        with patch.object(api.runtime.model, "vision", return_value={"text": json.dumps(visual)}), \
             patch.object(api.runtime.retrieval, "retrieve", return_value=([], "none")), \
             patch.object(api.runtime, "_generate_brain", side_effect=brain):
            result = self.send("tetris-chat", "tetris", "スクショを見ればわかるように、そもそもゲームが開始しない",
                               image={"name": "screen.png", "mime_type": "image/png", "data_url": "data:image/png;base64,AA=="})
        self.assertEqual(200, result.status_code)
        self.assertIn("CURRENT_ATTACHMENT_VISION_EVIDENCE", captured["packet"])
        self.assertIn("Score 0 / Lines 0 / Level 1", captured["packet"])
        self.assertIn("ACTIVE_PROJECT_CONTEXT", captured["packet"])
        self.assertIn('"current_subject": "Tetris project"', captured["packet"])
        self.assertLess(captured["packet"].index("CURRENT_ATTACHMENT_VISION_EVIDENCE"), captured["packet"].index("ACTIVE_PROJECT_AND_CONVERSATION_CONTEXT"))

    def test_project_screenshot_repair_request_starts_coding_task(self):
        with patch.object(api.settings, "task_manager_enabled", True), \
             patch.object(api.runtime, "execute_image", side_effect=AssertionError("repair must use Coding Task")):
            result = self.send(
                "tetris-chat", "tetris",
                "スクショを見ればわかるように、そもそも今実装中のゲームが開始しない問題を解決して",
                image={"name": "screen.png", "mime_type": "image/png", "data_url": "data:image/png;base64,AA=="},
            )
        self.assertEqual(200, result.status_code)
        self.assertIn("coding_task_id", result.json())
        task = api.db.coding_task(result.json()["coding_task_id"])
        self.assertIn(task["status"], {"QUEUED", "PLANNING", "RESUMABLE"})


if __name__ == "__main__":
    unittest.main()
