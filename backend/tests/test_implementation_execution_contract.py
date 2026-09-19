import json
import sys
import tempfile
import unittest
from pathlib import Path

import olcr_api.app as api
from olcr_api.models import Route, Task, TaskState


PHASE = {
    "id": "implementation",
    "goal": "create src/index.html",
    "done": ["src/index.html exists"],
    "verify": ["read-back check"],
    "dependencies": [],
    "risks": [],
}


class ImplementationExecutionContractTests(unittest.TestCase):
    def narrative_execution(self):
        task = Task("implementation")
        task.transition(TaskState.ROUTING)
        task.route = Route.IMPLEMENTATION
        task.transition(TaskState.EXECUTING)
        task.error = "implementation model did not return a valid file-operation plan"
        task.transition(TaskState.FAILED)
        return task

    def test_narrative_only_is_not_rendered_as_implemented_work(self):
        report = api._report_from_execution(
            PHASE, 0, self.narrative_execution(), "Here is what I will do next: create the file.")
        self.assertEqual("NARRATIVE_ONLY", report["implementation_result_kind"])
        self.assertEqual([], report["implemented"])
        self.assertTrue(report["execution_expectations"]["requires_repo_mutation"])

    def test_workspace_write_is_executed_even_when_response_is_brief(self):
        task = Task("implementation")
        task.transition(TaskState.ROUTING)
        task.route = Route.IMPLEMENTATION
        task.transition(TaskState.EXECUTING)
        task.tool_executions.append({"tool": "workspace_write", "status": "success", "output": {"path": "src/index.html"}})
        task.transition(TaskState.COMPLETED)
        report = api._report_from_execution(PHASE, 0, task, "Updated.")
        self.assertEqual("EXECUTED", report["implementation_result_kind"])
        self.assertEqual("PASS", report["status"])

    def test_browser_only_phase_is_explicitly_read_only(self):
        phase = {**PHASE, "goal": "browser verification", "done": ["browser checked"]}
        expectations = api._phase_execution_expectations(phase)
        self.assertTrue(expectations["read_only_allowed"])
        self.assertFalse(expectations["requires_repo_mutation"])

    def test_test_only_phase_is_read_only(self):
        phase = {"goal": "Run the focused test command", "done": ["tests pass"], "verify": ["pytest tests/test_math.py"]}
        expectations = api._phase_execution_expectations(phase)
        self.assertTrue(expectations["read_only_allowed"])
        self.assertFalse(expectations["requires_repo_mutation"])

    def test_explicit_verification_only_ignores_command_tokens(self):
        phase = {
            "id": "p2", "goal": "Execute the focused test to confirm fix", "status": "pending",
            "done": ["focused test passes"],
            "verify": ["run: npm test -- --testNamePattern='add' --write"],
            "dependencies": ["p1"], "risks": [], "execution_mode": "VERIFICATION_ONLY",
            "approved_scope": ["src/math.py"],
        }
        expectations = api._phase_execution_expectations(phase)
        self.assertEqual("VERIFICATION_ONLY", expectations["execution_mode"])
        self.assertTrue(expectations["execution_mode_valid"])
        self.assertTrue(expectations["read_only_allowed"])
        self.assertFalse(expectations["requires_repo_mutation"])

    def test_explicit_modes_are_authoritative_and_legacy_is_safe(self):
        implementation = {**PHASE, "execution_mode": "IMPLEMENTATION"}
        both = {**PHASE, "execution_mode": "IMPLEMENTATION_AND_VERIFICATION"}
        self.assertTrue(api._phase_execution_expectations(implementation)["requires_repo_mutation"])
        self.assertTrue(api._phase_execution_expectations(both)["requires_repo_mutation"])
        self.assertFalse(api._phase_execution_expectations(both)["read_only_allowed"])
        legacy = api._phase_execution_expectations(PHASE)
        self.assertEqual("LEGACY_TEXT_HEURISTIC", legacy["execution_mode_source"])
        self.assertFalse(legacy["read_only_allowed"])

    def test_contradictory_explicit_verification_metadata_fails_closed(self):
        phase = {**PHASE, "execution_mode": "VERIFICATION_ONLY", "requires_write_operation": True}
        expectations = api._phase_execution_expectations(phase)
        self.assertFalse(expectations["execution_mode_valid"])
        self.assertFalse(expectations["read_only_allowed"])
        self.assertTrue(expectations["requires_repo_mutation"])

    def test_verification_only_manifest_mutation_fails_closed(self):
        phase = {**PHASE, "execution_mode": "VERIFICATION_ONLY",
                 "file_manifest": [{"path": "src/math.py", "action": "modify"}]}
        self.assertIn("mutation manifest", " ".join(api.phase_execution_contract_errors(phase)))

    def test_read_only_verification_runs_without_mutating_test_target(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "src" / "math.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            test_file = root / "tests" / "test_math.py"
            test_file.write_text(
                "import unittest\nfrom src.math import add\nclass AddTest(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 3), 5)\n",
                encoding="utf-8")
            original = test_file.read_bytes()
            (root / ".benchmark_metadata.json").write_text(json.dumps({
                "verify_command": [sys.executable, "-m", "unittest", "tests.test_math"],
                "verification_targets": ["tests/test_math.py"],
            }), encoding="utf-8")
            task, result = api._run_read_only_verification(
                {"goal": "Run focused verification", "verify": ["tests pass"]}, str(root))
            self.assertTrue(result["passed"])
            self.assertEqual("completed", task.state.value)
            self.assertEqual(original, test_file.read_bytes())
            self.assertEqual("NONE", task.tool_executions[0]["input"]["write_scope"])

    def test_scope_contract_removes_verification_files_from_mutation_manifest(self):
        plan = {
            "scope": {"allowed": ["src/math.py"], "forbidden": []},
            "file_manifest": [
                {"path": "src/math.py", "action": "modify"},
                {"path": "tests/test_math.py", "action": "modify"},
            ],
            "tasks": [{"task_id": "t1", "change_scope": ["src/math.py", "tests/test_math.py"]}],
        }
        bound = api._bind_scope_contract(plan, {
            "write_allowed": ["src/math.py"],
            "read_only": ["tests/test_math.py"],
        })
        self.assertEqual(["src/math.py"], bound["scope"]["allowed"])
        self.assertEqual(["src/math.py"], [item["path"] for item in bound["file_manifest"]])
        self.assertEqual(["src/math.py"], bound["tasks"][0]["change_scope"])
        self.assertIn("tests/test_math.py", bound["scope"]["forbidden"])


if __name__ == "__main__":
    unittest.main()
