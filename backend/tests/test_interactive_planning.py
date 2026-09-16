import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import olcr_api.app as api
from olcr_api.db import Database
from olcr_api.interactive_planning import parse_pending_questions, resolve_short_reply
from olcr_api.models import Task


QUESTIONS = [
    {"id": "Q1", "question": "画面構成", "options": {"A": "最小MVP", "B": "拡張"}, "state": "PENDING"},
    {"id": "Q2", "question": "保存方法", "options": {"A": "ローカル", "B": "同期"}, "state": "PENDING"},
    {"id": "Q3", "question": "操作", "options": {"A": "簡易", "B": "詳細"}, "state": "PENDING"},
]


class InteractivePlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db, self.old_settings = api.db, api.settings
        api.db = Database(str(Path(self.tmp.name) / "planning.sqlite")); api.db.initialize()
        api.db.create_project("project", self.tmp.name, time.time(), "project")
        api.db.create_conversation("conversation", time.time(), "conversation", "project")
        self.client = TestClient(api.app)

    def tearDown(self):
        api.db, api.settings = self.old_db, self.old_settings
        self.tmp.cleanup()

    def session(self, questions=QUESTIONS, *, sil=False, sui=False):
        return api.db.create_interactive_planning("conversation", questions,
                                                  {"SIL": sil, "SUI": sui})

    def reply(self, text):
        return self.client.post("/api/chat", json={"project_id": "project", "conversation_id": "conversation", "message": text})

    def test_all_a_resolves_every_pending_question_without_coding_task(self):
        self.session()
        result = self.reply("全部A")
        saved = api.db.active_interactive_planning("conversation")
        self.assertEqual(200, result.status_code)
        self.assertIsNone(saved)
        with api.db.connect() as db:
            row = db.execute("SELECT * FROM interactive_planning_sessions WHERE conversation_id=?", ("conversation",)).fetchone()
        session = api.db._interactive_planning_row(row)
        self.assertEqual({"Q1": "A", "Q2": "A", "Q3": "A"}, session["decisions"])
        self.assertEqual([], api.db.coding_tasks("conversation"))
        self.assertIn("開発計画", result.json()["response"])

    def test_all_numeric_and_single_choice_use_option_positions(self):
        self.session()
        result = self.reply("全部1")
        self.assertIn("Q1: A", result.json()["response"])
        self.session([{**QUESTIONS[0]}])
        result = self.reply("A")
        self.assertIn("Q1: A", result.json()["response"])

    def test_partial_batch_and_recommended_keep_answer_provenance(self):
        self.session()
        result = self.reply("1と2はA、3はB")
        self.assertIn("Q1: A", result.json()["response"])
        self.assertIn("Q3: B", result.json()["response"])
        self.session()
        self.reply("全部おすすめ")
        with api.db.connect() as db:
            row = db.execute("SELECT * FROM interactive_planning_sessions WHERE conversation_id=? ORDER BY planning_revision DESC LIMIT 1", ("conversation",)).fetchone()
        session = api.db._interactive_planning_row(row)
        self.assertTrue(all(answer["answer_source"] == "ASSUMPTION_RECOMMENDED" for answer in session["answered_questions"]))

    def test_reload_stale_revision_and_format_opt_out_are_preserved(self):
        old = self.session()
        current = self.session([{**QUESTIONS[0]}], sil=False, sui=False)
        self.assertEqual("SUPERSEDED", api.db.update_interactive_planning(old["id"], status="SUPERSEDED")["status"])
        reopened = Database(api.db.path); reopened.initialize()
        self.assertEqual(current["id"], reopened.active_interactive_planning("conversation")["id"])
        result = self.reply("全部A")
        with api.db.connect() as db:
            row = db.execute("SELECT * FROM interactive_planning_sessions WHERE id=?", (current["id"],)).fetchone()
        saved = api.db._interactive_planning_row(row)
        self.assertEqual({"SIL": False, "SUI": False}, saved["format_state"])
        self.assertIn("開発計画", result.json()["response"])

    def test_no_pending_all_a_clarifies_in_japanese(self):
        result = self.reply("全部A")
        self.assertIn("選択できる計画質問がありません", result.json()["response"])
        self.assertEqual([], api.db.coding_tasks("conversation"))

    def test_question_parser_and_short_resolver_cover_tetris_five_questions(self):
        text = "\n".join(f"質問{i}: 選択{i}\nA. A案\nB. B案" for i in range(1, 6))
        questions = parse_pending_questions(text)
        resolved = resolve_short_reply("全部A", questions)
        self.assertEqual(5, len(questions))
        self.assertEqual({f"Q{i}" for i in range(1, 6)}, set(resolved["answers"]))

    def test_assistant_questions_are_persisted_then_continue_after_conversation_reload(self):
        response = "質問1: 画面\nA. 最小MVP\nB. 拡張\n質問2: 操作\nA. 簡易\nB. 詳細"
        with patch.object(api.runtime, "execute", return_value=(Task("planning"), response)):
            initial = self.reply("実装計画だけを作って")
        self.assertEqual(response, initial.json()["response"])
        active = api.db.active_interactive_planning("conversation")
        self.assertEqual(2, len(active["pending_questions"]))
        self.assertEqual(["user", "assistant"], [item["role"] for item in api.db.conversation("conversation")["messages"]])
        reopened = Database(api.db.path); reopened.initialize()
        self.assertEqual(2, len(reopened.active_interactive_planning("conversation")["pending_questions"]))
        continued = self.reply("全部A")
        self.assertIn("Q1: A", continued.json()["response"])
        self.assertEqual([], api.db.coding_tasks("conversation"))
