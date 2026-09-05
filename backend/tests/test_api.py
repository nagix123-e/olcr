import os
from pathlib import Path
import tempfile
import unittest

_tmp = tempfile.TemporaryDirectory()
os.environ["OLCR_DB_PATH"] = str(Path(_tmp.name) / "api.db")
os.environ["OLCR_ALLOWED_ROOTS"] = _tmp.name

try:
    from fastapi.testclient import TestClient
    from olcr_api.app import app, conversation_memory_constraint
    from olcr_cli.main import VERSION as CLI_VERSION
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "FastAPI test dependencies not installed")
class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.client = TestClient(app)
    def test_health(self):
        health=self.client.get("/api/health").json()
        self.assertEqual("ok", health["status"])
        self.assertEqual("0.5.0", CLI_VERSION)
        self.assertEqual(CLI_VERSION, health["version"])
        self.assertEqual("ready", health["model_configuration"])
        self.assertEqual(str(Path(_tmp.name) / "api.db"), health["db_path"])
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


if __name__ == "__main__": unittest.main()
