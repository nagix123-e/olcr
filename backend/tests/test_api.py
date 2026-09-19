import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory()
os.environ["OLCR_DB_PATH"] = str(Path(_tmp.name) / "api.db")
os.environ["OLCR_ALLOWED_ROOTS"] = _tmp.name

try:
    from fastapi.testclient import TestClient
    from olcr_api.app import app, conversation_memory_constraint, db, settings
    from olcr_cli.main import VERSION as CLI_VERSION
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "FastAPI test dependencies not installed")
class APITests(unittest.TestCase):
    def test_gui_preflight_and_auth(self):
        import secrets
        token=secrets.token_urlsafe(32)
        origin="http://127.0.0.1:5173"
        with patch("olcr_api.app._gui_session_token", token):
            preflight=self.client.options("/api/projects",headers={"Origin":origin,"Access-Control-Request-Method":"GET","Access-Control-Request-Headers":"x-olcr-session,content-type"})
            self.assertEqual(200,preflight.status_code)
            self.assertEqual(origin,preflight.headers.get("access-control-allow-origin"))
            self.assertEqual(401,self.client.get("/api/projects").status_code)
            self.assertEqual(401,self.client.get("/api/projects",headers={"X-OLCR-Session":"invalid"}).status_code)
            self.assertEqual(200,self.client.get("/api/projects",headers={"X-OLCR-Session":token}).status_code)
            denied=self.client.options("/api/projects",headers={"Origin":"https://example.com","Access-Control-Request-Method":"GET"})
            self.assertEqual(400,denied.status_code)
    @classmethod
    def setUpClass(cls): cls.client = TestClient(app)
    def test_health(self):
        health=self.client.get("/api/health").json()
        self.assertEqual("ok", health["status"])
        self.assertEqual("0.6.0", CLI_VERSION)
        self.assertEqual(CLI_VERSION, health["version"])
        self.assertEqual("ready", health["model_configuration"])
        # Test discovery can import the app through another module first.  In
        # that case its temporary path is not the active configuration.  The
        # endpoint must report the database the already-running app uses.
        self.assertEqual(settings.db_path, health["db_path"])
        self.assertEqual(db.path, health["db_path"])
    def test_direct_chat(self):
        body = self.client.post("/api/chat", json={"message":"lowercase: HELLO"}).json()
        self.assertEqual("DIRECT", body["task"]["route"]); self.assertEqual(0, len(body["task"]["model_calls"]))
    def test_index_path_traversal(self):
        response = self.client.post("/api/files/index", json={"path":"/etc/passwd"})
        self.assertEqual(403, response.status_code)

    def test_memory_off_runtime_constraint_guards_unavailable_history(self):
        constraint = conversation_memory_constraint(False)
        self.assertIn("Do not invent replacement facts", constraint)
        self.assertIn("Current user message", constraint)
        self.assertEqual("", conversation_memory_constraint(True))

    def test_current_eonet_empty_is_terminal_on_chat_path(self):
        import olcr_api.app as api_module
        result = {"tool_id":"earth.natural_event", "provider":"NASA EONET",
                  "sources":[{"provider":"NASA EONET"}],
                  "data":{"items":[], "semantic_status":"EMPTY", "current_turn_evidence":True}}
        with patch.object(api_module.settings, "external_access_enabled", True), \
             patch.object(api_module, "route_external_tool", return_value=("earth.natural_event", {"status":"open"})), \
             patch.object(api_module, "execute_external_tool", return_value=result):
            response = self.client.post("/api/chat", json={"message":"現在進行中の自然災害", "project_id":api_module.db.default_project_id()})
        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertIn("今回のEONET検索では", body["response"])
        self.assertIn("見つかりませんでした", body["response"])
        self.assertNotIn("2023", body["response"])

    def test_current_eonet_data_is_terminal_on_chat_path(self):
        import olcr_api.app as api_module
        result = {"tool_id":"earth.natural_event", "provider":"NASA EONET",
                  "sources":[{"provider":"NASA EONET"}],
                  "data":{"items":[{"title":"Current wildfire"}], "semantic_status":"DATA", "current_turn_evidence":True}}
        with patch.object(api_module.settings, "external_access_enabled", True), \
             patch.object(api_module, "route_external_tool", return_value=("earth.natural_event", {"status":"open"})), \
             patch.object(api_module, "execute_external_tool", return_value=result), \
             patch.object(api_module.runtime, "compose_tool_result", side_effect=AssertionError("Brain must not rewrite EONET")):
            response = self.client.post("/api/chat", json={"message":"現在進行中として登録されている山火事や火山などの自然災害をいくつか教えてください。", "project_id":api_module.db.default_project_id()})
        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertIn("Current wildfire", body["response"])
        self.assertNotIn("2023", body["response"])

    def test_eonet_failure_is_terminal_on_chat_path(self):
        import olcr_api.app as api_module
        with patch.object(api_module.settings, "external_access_enabled", True), \
             patch.object(api_module, "route_external_tool", return_value=("earth.natural_event", {"status":"open"})), \
             patch.object(api_module, "execute_external_tool", side_effect=api_module.ExternalToolError("PROVIDER_UNAVAILABLE")):
            response = self.client.post("/api/chat", json={"message":"現在進行中の自然災害", "project_id":api_module.db.default_project_id()})
        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertIn("EONETから現在の自然災害データを取得できませんでした", body["response"])
        self.assertNotIn("見つかりませんでした", body["response"])

    def test_federal_empty_metadata_remains_terminal_on_chat_path(self):
        import olcr_api.app as api_module
        result = {"tool_id":"government.us_federal_register", "provider":"Federal Register",
                  "sources":[{"provider":"Federal Register"}],
                  "data":{"items":[], "semantic_status":"EMPTY", "item_count":0}}
        with patch.object(api_module.settings, "external_access_enabled", True), \
             patch.object(api_module, "route_external_tool", return_value=("government.us_federal_register", {"query":"artificial intelligence"})), \
             patch.object(api_module, "execute_external_tool", return_value=result):
            response = self.client.post("/api/chat", json={"message":"Federal Registerから人工知能を検索", "project_id":api_module.db.default_project_id()})
        self.assertEqual(200, response.status_code)
        self.assertIn("今回のFederal Register検索では", response.json()["response"])
        self.assertIn("見つかりませんでした", response.json()["response"])
        self.assertNotIn("取得できませんでした", response.json()["response"])

    def test_federal_failure_is_terminal_on_chat_path(self):
        import olcr_api.app as api_module
        with patch.object(api_module.settings, "external_access_enabled", True), \
             patch.object(api_module, "route_external_tool", return_value=("government.us_federal_register", {"query":"artificial intelligence"})), \
             patch.object(api_module, "execute_external_tool", side_effect=api_module.ExternalToolError("PROVIDER_UNAVAILABLE")):
            response = self.client.post("/api/chat", json={"message":"Federal Registerから人工知能に関係する最近のruleまたはnoticeを5件検索してください。", "project_id":api_module.db.default_project_id()})
        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertIn("Federal Registerからデータを取得できませんでした", body["response"])
        self.assertNotIn("見つかりませんでした", body["response"])

    def test_federal_data_uses_authoritative_terminal_renderer(self):
        import olcr_api.app as api_module
        result = {"tool_id":"government.us_federal_register", "provider":"Federal Register",
                  "sources":[{"provider":"Federal Register"}],
                  "data":{"items":[{"title":"AI notice", "document_number":"2026-001"}], "semantic_status":"DATA"}}
        with patch.object(api_module.settings, "external_access_enabled", True), \
             patch.object(api_module, "route_external_tool", return_value=("government.us_federal_register", {"query":"artificial intelligence"})), \
             patch.object(api_module, "execute_external_tool", return_value=result), \
             patch.object(api_module.runtime, "compose_tool_result", side_effect=AssertionError("Brain must not rewrite Federal Register")):
            response = self.client.post("/api/chat", json={"message":"Federal Registerから人工知能に関係する最近のruleまたはnoticeを検索してください。", "project_id":api_module.db.default_project_id()})
        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertIn("AI notice", body["response"])
        self.assertIn("2026-001", body["response"])

    def test_sympy_success_uses_concise_renderer_on_chat_path(self):
        import olcr_api.app as api_module
        result = {"tool_id":"math.symbolic", "provider":"SymPy",
                  "sources":[{"provider":"SymPy"}],
                  "data":{"operation":"factor", "expression":"x^4 - 1", "result":"(x - 1)*(x + 1)*(x^2 + 1)"}}
        with patch.object(api_module, "route_external_tool", return_value=("math.symbolic", {"operation":"factor", "expression":"x^4 - 1"})), \
             patch.object(api_module, "execute_external_tool", return_value=result), \
             patch.object(api_module.runtime, "compose_tool_result", side_effect=AssertionError("Brain must not rewrite SymPy")):
            response = self.client.post("/api/chat", json={"message":"x^4 - 1 を因数分解してください。可能なら記号計算ツールを使ってください。", "project_id":api_module.db.default_project_id()})
        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertIn("因数分解結果", body["response"])
        self.assertNotIn("from sympy import", body["response"])

    def test_currency_chat_uses_current_provider_value_without_brain_rewrite(self):
        import olcr_api.app as api_module
        result = {"tool_id": "currency.frankfurter", "provider": "Frankfurter",
                  "fetched_at": "2026-09-18T00:00:00+00:00",
                  "sources": [{"provider": "Frankfurter"}],
                  "data": {"base": "USD", "quote": "EUR", "amount": "100", "rate": "0.8721",
                           "converted_amount": "87.21", "rate_date": "2026-09-18"}}
        with patch.object(api_module.settings, "external_access_enabled", True), \
             patch.object(api_module, "route_external_tool", return_value=("currency.frankfurter", {"base": "USD", "quote": "EUR", "amount": 100})), \
             patch.object(api_module, "execute_external_tool", return_value=result), \
             patch.object(api_module.runtime, "compose_tool_result", side_effect=AssertionError("currency must not use Brain composition")):
            response = self.client.post("/api/chat", json={"message": "100ドルはユーロでいくら？", "project_id": api_module.db.default_project_id()})
        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertIn("87.21", body["response"])
        self.assertNotIn("92.50", body["response"])


if __name__ == "__main__": unittest.main()
