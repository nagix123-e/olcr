import tempfile
import unittest
import json
from pathlib import Path

from olcr_api.implementation_plan import prepare_implementation_plan
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.retrieval import DisabledVectorStore, FileRetriever, FTSRetriever, RetrievalRouter
from olcr_api.runtime import Runtime


class ImplementationPlanTests(unittest.TestCase):
    def test_manifest_is_normalized_and_create_is_safely_scaffolded(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            plan = {"phases": [{"id": "p1", "dependencies": [], "verify": ["typecheck"]}],
                    "file_manifest": [{"path": "src/App.tsx", "action": "create", "owner_phase": "p1"},
                                      {"path": "README.md", "action": "create"}]}
            prepared, artifact = prepare_implementation_plan("task", plan, directory, {"task_profile": "GENERAL_CODING", "requirements_hash": "abc"})
            self.assertEqual(str(Path(directory).resolve()), artifact["target_root"])
            self.assertEqual(2, artifact["file_manifest_count"])
            self.assertEqual(2, artifact["scaffold_created_count"])
            self.assertIn("implementation_plan_artifact", prepared)
            self.assertIn("export default", Path(directory, "src/App.tsx").read_text())

    def test_all_entries_are_validated_before_first_write(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            plan = {"phases": [], "file_manifest": [{"path": "first.ts", "action": "create"},
                                                       {"path": "../outside.ts", "action": "create"}]}
            with self.assertRaises(ValueError):
                prepare_implementation_plan("task", plan, directory)
            self.assertFalse(Path(directory, "first.ts").exists())

    def test_modify_requires_existing_file_and_does_not_truncate(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            target = Path(directory, "existing.ts")
            target.write_text("const preserved = true;\n")
            _, artifact = prepare_implementation_plan("task", {"phases": [], "file_manifest": [{"path": "existing.ts", "action": "modify"}]}, directory)
            self.assertEqual(1, artifact["file_manifest_modify_count"])
            self.assertEqual("const preserved = true;\n", target.read_text())

    def test_non_empty_create_conflict_fails_closed(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            target = Path(directory, "index.html")
            target.write_text("existing")
            with self.assertRaises(ValueError):
                prepare_implementation_plan("task", {"phases": [], "file_manifest": [{"path": "index.html", "action": "create"}]}, directory)

    def test_managed_project_root_is_authorized_even_when_global_retrieval_root_differs(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as global_directory, tempfile.TemporaryDirectory(dir="/private/tmp") as project_directory:
            db = Database(str(Path(global_directory, "olcr.db")))
            db.initialize()
            class Model:
                def generate(self, *_args, **_kwargs):
                    return {"text": json.dumps({"operations": [{"op": "write", "path": "index.html", "content": "<h1>ok</h1>"}]}), "latency_ms": 1, "prompt_tokens": 1, "completion_tokens": 1}
            retrieval = RetrievalRouter(FileRetriever([global_directory]), FTSRetriever(db), DisabledVectorStore(), False)
            runtime = Runtime(Settings(allowed_roots=(global_directory,), db_path=db.path, main_model="mock").validated(), db, retrieval, Model())
            task, _ = runtime.execute("Implement the requested project", workspace_root=project_directory,
                                     managed_context={"managed_coding_task": True, "operation_intent": "IMPLEMENTATION"})
            self.assertEqual("completed", task.state.value)
            self.assertEqual("<h1>ok</h1>", Path(project_directory, "index.html").read_text())


if __name__ == "__main__":
    unittest.main()
