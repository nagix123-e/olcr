import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.retrieval import DisabledVectorStore, FTSRetriever, FileRetriever, RetrievalRouter
from olcr_api.runtime import Runtime


class DeterministicFileModel:
    """Returns the production typed-file operation payload and nothing else."""

    def __init__(self):
        self.calls = []

    def generate(self, messages, model, stream=False, think=None, format=None):
        self.calls.append({"messages": messages, "model": model, "think": think, "format": format})
        return {
            "text": json.dumps(
                {
                    "change_required": True,
                    "source_inspected": True,
                    "condition_evaluated": True,
                    "reason_code": "runtime_probe",
                    "operations": [
                        {
                            "op": "write",
                            "path": "runtime_probe.txt",
                            "content": "OLCR_REAL_RUNTIME_PROBE",
                        }
                    ],
                    "verification": "read the created probe file",
                }
            ),
            "latency_ms": 0,
        }


class OutOfScopeFileModel(DeterministicFileModel):
    def generate(self, messages, model, stream=False, think=None, format=None):
        self.calls.append({"messages": messages, "model": model, "think": think, "format": format})
        return {
            "text": json.dumps({
                "change_required": True,
                "source_inspected": True,
                "condition_evaluated": True,
                "reason_code": "scope_probe",
                "operations": [
                    {"op": "write", "path": "src/math.py", "content": "def add(a, b):\n    return a + b\n"},
                    {"op": "write", "path": "tests/test_math.py", "content": "changed\n"},
                ],
                "verification": "read back the authorized file",
            }),
            "latency_ms": 0,
        }


class PatchFileModel(DeterministicFileModel):
    def __init__(self, old="return a - b", new="return a + b"):
        super().__init__()
        self.old, self.new = old, new

    def generate(self, messages, model, stream=False, think=None, format=None):
        self.calls.append({"messages": messages, "model": model, "think": think, "format": format})
        return {"text": json.dumps({"operations": [{
            "op": "patch", "path": "src/math.py",
            "expected_old_fragment": self.old, "replacement_fragment": self.new,
        }]}), "latency_ms": 0}


class RealRuntimeManagedContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "workspace-a"
        self.workspace.mkdir()
        self.sentinel = self.base / "sentinel-b.txt"
        self.sentinel.write_text("OUTSIDE_SENTINEL_ORIGINAL", encoding="utf-8")
        self.db = Database(str(self.base / "olcr.sqlite"))
        self.db.initialize()
        settings = Settings(
            allowed_roots=(str(self.workspace),),
            db_path=self.db.path,
            main_model="deterministic-runtime-test",
        ).validated()
        retrieval = RetrievalRouter(
            FileRetriever([str(self.workspace)]),
            FTSRetriever(self.db),
            DisabledVectorStore(),
            False,
        )
        self.model = DeterministicFileModel()
        self.runtime = Runtime(settings, self.db, retrieval, self.model)
        self.managed_context = {
            "managed_coding_task": True,
            "operation_intent": "IMPLEMENTATION",
            "global_no_write": False,
            "workspace_root": str(self.workspace),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_runtime_managed_write_readback_and_global_no_write(self):
        request = (
            "新しいTask用の作業領域に必要なファイルを作成してください。\n"
            "このTask以外の既存ファイルは変更しないでください。"
        )
        task, response = self.runtime.execute(
            request,
            workspace_root=str(self.workspace),
            managed_context=self.managed_context,
        )

        probe = self.workspace / "runtime_probe.txt"
        write_operations = [item for item in task.tool_executions if item["tool"] == "workspace_write"]
        successful_operations = [item for item in write_operations if item["status"] == "success"]
        readbacks = [item for item in task.tool_executions if item["tool"] == "workspace_read" and item["status"] == "success"]

        self.assertEqual("IMPLEMENTATION", task.route.value)
        self.assertEqual("completed", task.state.value)
        self.assertTrue(self.model.calls)
        self.assertFalse(self.model.calls[0]["think"])
        self.assertIsInstance(self.model.calls[0]["format"], dict)
        self.assertIn("Return ONLY JSON", self.model.calls[0]["messages"][-1]["content"])
        self.assertIn('"operations"', self.model.calls[0]["messages"][-1]["content"])
        self.assertEqual("OLCR_REAL_RUNTIME_PROBE", probe.read_text(encoding="utf-8"))
        self.assertIn("Write: PASS; read-back: PASS", response)
        self.assertGreater(len(write_operations), 0)
        self.assertGreater(len(successful_operations), 0)
        self.assertGreater(len(readbacks), 0)
        self.assertEqual("OUTSIDE_SENTINEL_ORIGINAL", self.sentinel.read_text(encoding="utf-8"))

        global_no_write_request = (
            "ワークスペースのファイルには何も書き込まないでください。\n"
            "ファイルを作成・変更しないでください。\n"
            "実装はしないでください。"
        )
        denied, _ = self.runtime.execute(
            global_no_write_request,
            workspace_root=str(self.workspace),
            managed_context=self.managed_context,
        )

        self.assertEqual("denied", denied.state.value)
        self.assertEqual("explicit_no_write_request", denied.reason_category)
        self.assertFalse((self.workspace / "forbidden_probe.txt").exists())
        self.assertEqual(1, len(self.model.calls))

    def test_approved_scope_is_enforced_before_any_operation_is_written(self):
        model = OutOfScopeFileModel()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        source = self.workspace / "src" / "math.py"
        source.parent.mkdir()
        source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        tests = self.workspace / "tests" / "test_math.py"
        tests.parent.mkdir()
        tests.write_text("original\n", encoding="utf-8")

        task, response = runtime.execute(
            "Fix src/math.py and verify the focused test.",
            workspace_root=str(self.workspace),
            managed_context={
                **self.managed_context,
                "approved_scopes": [{"source": "original_request", "requested_scope": ["src/math.py"]}],
            },
        )

        self.assertEqual("failed", task.state.value)
        self.assertIn("authorized mutation scope", response)
        self.assertEqual("def add(a, b):\n    return a - b\n", source.read_text(encoding="utf-8"))
        self.assertEqual("original\n", tests.read_text(encoding="utf-8"))

    def test_python_source_is_supplied_to_typed_implementer(self):
        source = self.workspace / "src" / "math.py"
        source.parent.mkdir()
        source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        task, _ = self.runtime.execute(
            "Fix src/math.py.",
            workspace_root=str(self.workspace),
            managed_context=self.managed_context,
        )
        self.assertEqual("completed", task.state.value)
        self.assertIn("def add(a, b):", self.model.calls[-1]["messages"][-1]["content"])

    def test_exact_patch_applies_to_current_source(self):
        source = self.workspace / "src" / "math.py"
        source.parent.mkdir()
        source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        model = PatchFileModel()
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Fix src/math.py.", workspace_root=str(self.workspace), managed_context={
            **self.managed_context,
            "approved_scopes": [{"source": "original_request", "requested_scope": ["src/math.py"]}],
        })
        self.assertEqual("completed", task.state.value)
        self.assertIn("return a + b", source.read_text(encoding="utf-8"))
        self.assertTrue(any(item["tool"] == "operation_preflight" for item in task.tool_executions))

    def test_invalid_preimage_is_deterministic_and_rolls_back(self):
        source = self.workspace / "src" / "math.py"
        source.parent.mkdir()
        original = "def add(a, b):\n    return a - b\n"
        source.write_text(original, encoding="utf-8")
        model = PatchFileModel(old="return never_existed")
        runtime = Runtime(self.runtime.settings, self.db, self.runtime.retrieval, model)
        task, _ = runtime.execute("Fix src/math.py.", workspace_root=str(self.workspace), managed_context={
            **self.managed_context,
            "approved_scopes": [{"source": "original_request", "requested_scope": ["src/math.py"]}],
        })
        self.assertEqual("failed", task.state.value)
        self.assertEqual(original, source.read_text(encoding="utf-8"))
        failure = next(item for item in task.tool_executions if item["tool"] == "operation_application_failure")
        self.assertEqual("PREIMAGE_MISMATCH", failure["input"]["failure_class"])
        self.assertEqual("ROLLED_BACK", failure["output"]["worktree_state"])

    def test_managed_web_words_do_not_select_normal_chat_web_search(self):
        self.runtime.settings.web_mode = "auto"
        self.runtime.settings.web_provider = "brave"
        request = (
            "Implement the approved Webサイト. Resolve the GitHub Releases URL from the repository, "
            "then create index.html, links.html, and styles.css in the workspace."
        )
        with patch("olcr_api.runtime.brave_search") as search:
            task, _ = self.runtime.execute(
                request,
                workspace_root=str(self.workspace),
                managed_context=self.managed_context,
            )
        self.assertEqual("IMPLEMENTATION", task.route.value)
        self.assertTrue(any(item["tool"] == "workspace_write" for item in task.tool_executions))
        search.assert_not_called()

    def test_authorized_coding_web_support_returns_to_implementation(self):
        self.runtime.settings.web_mode = "auto"
        self.runtime.settings.web_provider = "brave"
        context = {**self.managed_context, "coding_web_support": True}
        request = "Search the web for the current documented API, then implement the required HTML file."
        source = {"url": "https://example.com/api", "title": "API documentation", "rank": 1}
        fetched = {"final_url": "https://example.com/api", "text": "bounded API reference"}
        with patch("olcr_api.runtime.brave_search", return_value=[source]) as search, patch("olcr_api.runtime.web_fetch", return_value=fetched):
            task, _ = self.runtime.execute(
                request,
                workspace_root=str(self.workspace),
                managed_context=context,
            )
        search.assert_called_once()
        self.assertEqual("IMPLEMENTATION", task.route.value)
        self.assertTrue(any(item["tool"] == "workspace_write" for item in task.tool_executions))


if __name__ == "__main__":
    unittest.main()
