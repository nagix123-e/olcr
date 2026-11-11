import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

_tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
os.environ["OLCR_DB_PATH"] = str(Path(_tmp.name) / "external-credentials.sqlite")
os.environ["OLCR_ALLOWED_ROOTS"] = _tmp.name

from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api import external_tools
from olcr_api.config import Settings
from olcr_api.db import Database


class ExternalCredentialSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db, self.old_settings = api.db, api.settings
        db_path = str(Path(self.tmp.name) / "test.sqlite")
        api.db = Database(db_path)
        api.db.initialize()
        api.rebuild(Settings(db_path=db_path, allowed_roots=(self.tmp.name,), external_access_enabled=False))
        api.db.create_project("project", self.tmp.name, time.time(), "project")
        api.db.create_conversation("conversation", time.time(), "conversation", "project")
        external_tools._RUNTIME_CREDENTIALS.pop("econ.fred", None)
        self.client = TestClient(api.app)

    def tearDown(self):
        external_tools._RUNTIME_CREDENTIALS.pop("econ.fred", None)
        api.db, api.settings = self.old_db, self.old_settings
        api.rebuild(self.old_settings)
        self.tmp.cleanup()

    def test_single_keychain_allowlist_covers_exactly_the_setup_field_handles(self):
        expected = {handle for tool_id, setup in external_tools._CREDENTIAL_SETUP.items()
                    if tool_id not in external_tools.PRODUCTION_EXCLUDED_API_IDS
                    for _field_id, _label, handle, _input_type in map(external_tools._credential_field_parts, setup["fields"])}
        handles = set(external_tools.credential_keychain_handles())
        self.assertTrue(expected <= handles)
        excluded = {
            handle
            for tool_id in external_tools.PRODUCTION_EXCLUDED_API_IDS
            for field in external_tools._CREDENTIAL_SETUP.get(tool_id, {}).get("fields", ())
            for _field_id, _label, handle, _input_type in (external_tools._credential_field_parts(field),)
        }
        self.assertFalse(excluded & handles)

    def test_production_excluded_provider_rejects_credential_submission(self):
        secret = "must-not-be-retained"
        for tool_id in external_tools.PRODUCTION_EXCLUDED_API_IDS:
            with self.subTest(tool_id=tool_id):
                self.assertIsNone(external_tools.credential_setup(tool_id))
                response = self.client.put("/api/external-tools/credentials", json={
                    "tool_id": tool_id, "credentials": {"api_key": secret}
                })
                self.assertEqual(422, response.status_code)
                self.assertIn("PROVIDER_NOT_IN_PRODUCTION", response.text)
                self.assertNotIn(secret, response.text)
                self.assertNotIn(tool_id, external_tools._RUNTIME_CREDENTIALS)

    def test_every_required_provider_has_safe_setup_metadata_and_sec_edgar_does_not(self):
        required_tools = {tool_id for tool_id, definition in external_tools.REGISTRY.items()
                          if definition.credential_requirement == "required"}
        active_setups = set(external_tools._CREDENTIAL_SETUP) - external_tools.PRODUCTION_EXCLUDED_API_IDS
        self.assertEqual(required_tools, active_setups)
        handles = set(external_tools.credential_keychain_handles())
        env_names = {
            env_name
            for tool_id, setup in external_tools._CREDENTIAL_SETUP.items()
            if tool_id in active_setups
            for _field_id, _label, env_name, _input_type in map(external_tools._credential_field_parts, setup["fields"])
        }
        with patch.object(external_tools, "_RUNTIME_CREDENTIALS", {}), \
             patch.dict(os.environ, {name: "" for name in env_names}):
            for tool_id, setup in external_tools._CREDENTIAL_SETUP.items():
                if tool_id not in active_setups:
                    continue
                with self.subTest(tool_id=tool_id):
                    block = external_tools.credential_setup(tool_id)
                    self.assertIsNotNone(block)
                    self.assertEqual("credential_setup", block["type"])
                    self.assertEqual(tool_id, block["tool_id"])
                    self.assertEqual("https", urlsplit(block["setup_url"]).scheme)
                    self.assertEqual(
                        [(field_id, label, env_name) for field_id, label, env_name, _input_type in map(external_tools._credential_field_parts, setup["fields"])],
                        [(field["id"], field["label"], field["keychain_handle"]) for field in block["fields"]],
                    )
                    self.assertTrue(all(field["input_type"] in {"text", "email", "password"} for field in block["fields"]))
                    self.assertTrue(all(field["keychain_handle"] in handles for field in block["fields"]))

        self.assertIsNone(external_tools.credential_setup("finance.sec_filings"))

    def test_stream_prompts_for_missing_key_before_disabled_access_without_calling_provider(self):
        prompt = "FREDで直近12か月の米国政策金利の推移を教えて"
        with patch.dict(os.environ, {"OLCR_FRED_API_KEY": ""}), \
             patch.object(api, "execute_external_tool", side_effect=AssertionError("provider must not be called")) as execute:
            response = self.client.post("/api/chat/stream", json={
                "message": prompt, "project_id": "project", "conversation_id": "conversation"})

        self.assertEqual(200, response.status_code)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        block_event = next(event for event in events if event.get("type") == "block")
        block = block_event["block"]
        self.assertEqual("credential_setup", block["type"])
        self.assertEqual("econ.fred", block["tool_id"])
        self.assertTrue(block["setup_url"].startswith("https://"))
        self.assertEqual(["api_key"], [field["id"] for field in block["fields"]])
        self.assertEqual("https://fred.stlouisfed.org/", block["setup_url"])
        self.assertEqual(["password"], [field["input_type"] for field in block["fields"]])
        self.assertNotIn("secret", json.dumps(block).lower())
        done = next(event for event in events if event.get("type") == "done")
        self.assertEqual("CREDENTIAL_SETUP_REQUIRED", done["task"]["error"])
        self.assertIn("外部アクセス設定も変更しません", next(event["text"] for event in events if event.get("type") == "chunk"))
        execute.assert_not_called()
        self.assertFalse(api.settings.external_access_enabled)
        conversation = api.db.conversation("conversation")
        self.assertEqual(block, conversation["messages"][-1]["blocks"][0])
        self.assertEqual(done["task"]["id"], conversation["messages"][-1]["task_id"])

    def test_excluded_census_is_not_routable_or_visible_as_a_credential_gate(self):
        prompt = "U.S. Censusで米国の最新人口推計を教えて"
        self.assertIsNone(external_tools.route(prompt))
        self.assertIsNone(external_tools.credential_setup("econ.us_census"))

    def test_excluded_reliefweb_is_not_routable_or_visible_as_a_credential_gate(self):
        prompt = "ReliefWebでスーダン情勢の最新人道支援レポートを探して"
        self.assertIsNone(external_tools.route(prompt))
        self.assertIsNone(external_tools.credential_setup("humanitarian.reliefweb"))

    def test_do_not_modify_files_does_not_suppress_explicit_world_bank_route(self):
        prompt = (
            "This is not a coding task. Do not modify files. Use the World Bank API to look up Japan's latest "
            "annual GDP in current US dollars; return the value, year, and source ID. The provider is explicitly World Bank."
        )
        self.assertFalse(api._external_data_prohibited(prompt))
        request = api.route_external_tool(prompt)
        self.assertIsNotNone(request)
        self.assertEqual("econ.worldbank", request[0])

        # This is only a current-data route/gate assertion. External Access
        # remains OFF, so no provider request or Normal Brain fallback is allowed.
        with patch.object(api, "execute_external_tool", side_effect=AssertionError("World Bank GET is gated")) as execute, \
             patch.object(api.runtime, "execute", side_effect=AssertionError("current-data request must not use Brain")) as run:
            response = self.client.post("/api/chat/stream", json={
                "message": prompt, "project_id": "project", "conversation_id": "conversation"})

        self.assertEqual(200, response.status_code)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        done = next(event for event in events if event.get("type") == "done")
        self.assertEqual("EXTERNAL_ACCESS_REQUIRED", done["task"]["error"])
        self.assertFalse(api.settings.external_access_enabled)
        execute.assert_not_called()
        run.assert_not_called()
        with api.db.connect() as conn:
            task = conn.execute(
                "SELECT route,error,selected_context_json FROM tasks WHERE id=?", (done["task"]["id"],)
            ).fetchone()
            execution = conn.execute(
                "SELECT status,error,latency_ms FROM tool_executions WHERE task_id=?", (done["task"]["id"],)
            ).fetchone()
        self.assertEqual("EXTERNAL_API", task["route"])
        self.assertEqual("EXTERNAL_ACCESS_REQUIRED", task["error"])
        self.assertEqual(("failed", "EXTERNAL_ACCESS_REQUIRED", 0.0), tuple(execution))
        self.assertIn("econ.worldbank", task["selected_context_json"])

    def test_key_submission_is_whitelisted_memory_only_and_never_echoes_secret(self):
        secret = "offline-test-key-not-a-real-credential"
        credentials = {"api_key": secret}
        with patch.dict(os.environ, {"OLCR_FRED_API_KEY": ""}):
            response = self.client.put("/api/external-tools/credentials", json={
                "tool_id": "econ.fred", "credentials": credentials})
            self.assertEqual(200, response.status_code)
            self.assertNotIn(secret, response.text)
            status = self.client.get("/api/external-tools").json()

        self.assertFalse(response.json()["external_access_enabled"])
        self.assertEqual("CONFIGURED", response.json()["status"])
        row = next(item for item in status["tools"] if item["tool_id"] == "econ.fred")
        self.assertEqual("Configured", row["credential"])
        self.assertIsNone(row["credential_setup"])
        self.assertNotIn(secret, json.dumps(status))
        with api.db.connect() as conn:
            persisted = " ".join(str(value) for row in conn.execute("SELECT value_json FROM application_settings") for value in row)
        self.assertNotIn(secret, persisted)

    def test_stored_credential_never_turns_external_access_on_or_bypasses_gate(self):
        secret = "offline-test-key-not-a-real-credential"
        prompt = "FREDで直近12か月の米国政策金利の推移を教えて"
        credentials = {"api_key": secret}
        with patch.dict(os.environ, {"OLCR_FRED_API_KEY": ""}):
            configured = self.client.put("/api/external-tools/credentials", json={
                "tool_id": "econ.fred", "credentials": credentials})
            self.assertEqual(200, configured.status_code)
            with patch.object(api, "execute_external_tool", side_effect=AssertionError("External Access is still OFF")) as execute:
                response = self.client.post("/api/chat/stream", json={
                    "message": prompt, "project_id": "project", "conversation_id": "conversation"})

        self.assertEqual(200, response.status_code)
        self.assertFalse(configured.json()["external_access_enabled"])
        self.assertFalse(api.settings.external_access_enabled)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertIn("外部アクセスはOFF", next(event["text"] for event in events if event.get("type") == "chunk"))
        self.assertFalse(any(event.get("type") == "block" for event in events))
        execute.assert_not_called()

    def test_rejects_unregistered_providers_or_field_names_without_retaining_values(self):
        secret = "offline-test-key-not-a-real-credential"
        for body in (
            {"tool_id": "finance.sec_filings", "credentials": {"api_key": secret}},
            {"tool_id": "econ.fred", "credentials": {"unexpected": secret}},
        ):
            with self.subTest(body=body):
                response = self.client.put("/api/external-tools/credentials", json=body)
                self.assertEqual(422, response.status_code)
                self.assertNotIn(secret, response.text)
                self.assertNotIn("econ.fred", external_tools._RUNTIME_CREDENTIALS)


if __name__ == "__main__":
    unittest.main()
