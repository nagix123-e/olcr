import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
os.environ["OLCR_DB_PATH"] = str(Path(_tmp.name) / "wikipedia-routing.sqlite")
os.environ["OLCR_ALLOWED_ROOTS"] = _tmp.name

from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api import external_tools
from olcr_api.db import Database
from olcr_api.config import Settings


def wikipedia_result(title="Anthropic"):
    return {"tool_id":"knowledge.wikimedia", "provider":"Wikipedia", "fetched_at":"2026-09-16T00:00:00Z",
            "data":{"title":title, "description":"AI safety company", "extract":"Anthropic is an artificial intelligence company.",
                    "page_url":"https://en.wikipedia.org/wiki/Anthropic", "language":"en"},
            "sources":[{"title":title,"provider":"Wikipedia","canonical_url":"https://en.wikipedia.org/wiki/Anthropic"}]}


class WikipediaChatRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db, self.old_settings = api.db, api.settings
        api.db = Database(str(Path(self.tmp.name) / "test.sqlite")); api.db.initialize()
        api.rebuild(Settings(db_path=str(Path(self.tmp.name) / "test.sqlite"), allowed_roots=(self.tmp.name,), external_access_enabled=True))
        api.db.create_project("project", self.tmp.name, time.time(), "project")
        api.db.create_conversation("conversation", time.time(), "conversation", "project")
        self.client = TestClient(api.app)

    def tearDown(self):
        api.db, api.settings = self.old_db, self.old_settings
        api.rebuild(self.old_settings)
        self.tmp.cleanup()

    def _chat(self, message, stream=False):
        endpoint = "/api/chat/stream" if stream else "/api/chat"
        response = self.client.post(endpoint, json={"message":message,"project_id":"project","conversation_id":"conversation"})
        return response

    def test_source_intent_and_subject_normalization(self):
        cases = {
            "search on wikipedia about anthropic": "anthropic",
            "search wikipedia for anthropic": "anthropic",
            "look up Anthropic on Wikipedia": "Anthropic",
            "anthropic wikipedia search": "anthropic",
            "WikipediaでAnthropicについて検索して": "Anthropic",
        }
        for message, query in cases.items():
            with self.subTest(message=message):
                tool, arguments = external_tools.route(message)
                self.assertEqual("knowledge.wikimedia", tool)
                self.assertEqual(query.casefold(), arguments["query"].casefold())

    def test_exact_gui_path_executes_wikipedia_and_persists_user_facing_response(self):
        with patch.object(api, "execute_external_tool", return_value=wikipedia_result()) as execute:
            response = self._chat("search on wikipedia about anthropic")
        self.assertEqual(200, response.status_code)
        body=response.json()
        self.assertIn("Anthropic", body["response"])
        self.assertIn("Wikipedia", body["response"])
        self.assertNotIn("retrieval_method", body["response"])
        self.assertNotIn("normalizer_diagnostics", body["response"])
        self.assertEqual("anthropic", execute.call_args.args[1]["query"])
        messages=api.db.conversation("conversation")["messages"]
        self.assertEqual(body["response"], messages[-1]["content"])

    def test_streaming_path_does_not_use_local_retrieval_or_emit_diagnostics(self):
        with patch.object(api, "execute_external_tool", return_value=wikipedia_result()) as execute, \
             patch.object(api.retrieval, "retrieve", side_effect=AssertionError("local retrieval must not run")):
            response=self._chat("search on wikipedia about anthropic", stream=True)
        self.assertEqual(200, response.status_code)
        wire=response.text
        self.assertIn("Anthropic", wire)
        self.assertNotIn("retrieval_method", wire)
        self.assertNotIn("normalizer_diagnostics", wire)
        self.assertEqual("anthropic", execute.call_args.args[1]["query"])

    def test_wikipedia_zero_results_are_user_facing(self):
        with patch.object(api, "execute_external_tool", side_effect=api.ExternalToolError("WIKIMEDIA_NOT_FOUND")):
            response=self._chat("search wikipedia for anthropic")
        self.assertEqual(200, response.status_code)
        self.assertIn("外部データ", response.json()["response"])
        self.assertNotIn("WIKIMEDIA_NOT_FOUND", response.json()["response"])
        self.assertNotIn("retrieval", response.json()["response"].lower())

    def test_wikipedia_provider_failure_is_user_facing(self):
        with patch.object(api, "execute_external_tool", side_effect=api.ExternalToolError("PROVIDER_UNAVAILABLE")):
            response=self._chat("search wikipedia for anthropic")
        self.assertEqual(200, response.status_code)
        self.assertIn("外部データ", response.json()["response"])
        self.assertNotIn("PROVIDER_UNAVAILABLE", response.json()["response"])
        self.assertNotIn("retrieval", response.json()["response"].lower())

    def test_wikipedia_failure_does_not_persist_diagnostics(self):
        with patch.object(api, "execute_external_tool", side_effect=api.ExternalToolError("WIKIMEDIA_NOT_FOUND")):
            response=self._chat("search wikipedia for anthropic")
        self.assertEqual(200, response.status_code)
        self.assertNotIn("WIKIMEDIA_NOT_FOUND", response.json()["response"])
        self.assertNotIn("retrieval", response.json()["response"].lower())
        self.assertEqual(response.json()["response"], api.db.conversation("conversation")["messages"][-1]["content"])

    def test_generic_and_local_routes_are_not_forced_to_wikipedia(self):
        generic=external_tools.route("search the web for Anthropic")
        self.assertTrue(generic is None or generic[0] != "knowledge.wikimedia")
        self.assertIsNone(external_tools.route("search for local-file-name"))
