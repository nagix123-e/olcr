import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api.continuation import task_continuation_policy, major_phase_plan, phase_lifecycle_stages
from olcr_api.coding_tasks import derive_task_graph, normalize_coding_requirements, normalize_task_graph, validate_plan
from olcr_api.db import Database
from olcr_api.models import Task, TaskState


WEBSITE = Path(__file__).with_name("fixtures").joinpath("animated_website_19691.txt").read_text()


def plan(goal, count=2):
    phases = [{"id": f"p{i}", "goal": "Implement requested deliverable" if i < count else "Browser verification",
               "status": "pending", "done": ["Deliverable checked"], "verify": ["Focused checks pass"],
               "dependencies": [f"p{i-1}"] if i > 1 else [], "risks": []} for i in range(1, count + 1)]
    return {"schema_version": 1, "original_goal": goal, "scope": {"allowed": ["project files"], "forbidden": []},
            "assumptions": [], "phases": phases, "max_retries_per_phase": 2, "requires_user_approval": True}


def report(phase_id, attempt=0):
    return {"phase_id": phase_id, "attempt": attempt, "plan_revision": 0, "status": "PASS",
            "implemented": ["deliverable"], "changed_files": ["index.html"], "test_executed": ["focused check"],
            "test_pass": ["focused check"], "test_fail": [], "build_executed": "YES", "build_pass": "PASS",
            "errors": [], "blockers": [], "risks": [], "typed_execution_summary": {},
            "manager_decision": {"decision": "PASS"}}


class MajorPolicyTests(unittest.TestCase):
    def test_small_fix_and_one_file_feature_are_continuous(self):
        for goal in ("スマホでタイトルが右にはみ出すのでそこだけ直して", "Add a copy button in one React component"):
            value = task_continuation_policy(goal, normalize_coding_requirements(goal))
            self.assertEqual("SMALL", value["task_size"])
            self.assertEqual("CONTINUOUS", value["continuation_policy"])

    def test_medium_and_long_unstructured_requests_stay_continuous(self):
        for goal in ("Add an animation and tests to an existing component", "Adjust the heading. " * 2000):
            value = task_continuation_policy(goal, normalize_coding_requirements(goal))
            self.assertNotEqual("LARGE", value["task_size"])
            self.assertEqual("CONTINUOUS", value["continuation_policy"])

    def test_exact_website_is_large_and_normal(self):
        self.assertEqual(19691, len(WEBSITE))
        requirements = normalize_coding_requirements(WEBSITE)
        before = copy.deepcopy(requirements)
        policy = task_continuation_policy(WEBSITE, requirements)
        self.assertEqual("LARGE", policy["task_size"])
        self.assertEqual("MAJOR_CHECKPOINTS", policy["continuation_policy"])
        self.assertEqual("NORMAL", requirements["execution_mode"])
        self.assertEqual("FRONTEND_ONLY_MARKETING_SITE", requirements["task_profile"])
        self.assertEqual(before, requirements)

    def test_explicit_overrides(self):
        for suffix in ("止めずに最後まで実行して", "Execute uninterrupted"):
            goal = WEBSITE + "\n# Current Task\n" + suffix
            self.assertEqual("CONTINUOUS", task_continuation_policy(goal, normalize_coding_requirements(goal))["continuation_policy"])
        for suffix in ("大きいタスクは区切って止めて", "各大きな工程ごとに止めて", "フェーズごとに続行確認して", "pause after each major phase"):
            self.assertEqual("MAJOR_CHECKPOINTS", task_continuation_policy("Add a button. " + suffix, {})["continuation_policy"])

    def test_giant_phase_split_preserves_criteria_and_scope(self):
        original = plan(WEBSITE, 1)
        original["phases"][0].update(goal="Inspect repository; MCP preflight; install dependencies; implement homepage; browser verification; finalize report",
                                      done=["Homepage implemented", "Browser responsive checks pass"], verify=["typecheck passes", "Playwright checks pass"])
        normalized = major_phase_plan(original, task_continuation_policy(WEBSITE, normalize_coding_requirements(WEBSITE)))
        self.assertEqual(3, len(normalized["phases"]))
        self.assertEqual([], validate_plan(normalized, WEBSITE))
        self.assertEqual(original["scope"], normalized["scope"])
        self.assertEqual(original["phases"], normalized["blueprint"]["source_phases"])
        work = normalized["phases"][1]
        self.assertNotIn("inspection", phase_lifecycle_stages(work))
        self.assertNotIn("finalization", phase_lifecycle_stages(work))
        self.assertIn("typecheck passes", work["verify"])
        self.assertIn("Playwright checks pass", normalized["phases"][-1]["verify"])

    def test_preserves_medium_plan_and_bounds_large_plan(self):
        medium = plan("Add a component", 2)
        self.assertEqual(medium, major_phase_plan(medium, task_continuation_policy(medium["original_goal"], {})))
        large = plan(WEBSITE, 8)
        result = major_phase_plan(large, task_continuation_policy(WEBSITE, normalize_coding_requirements(WEBSITE)))
        self.assertLessEqual(len(result["phases"]), 5)
        self.assertEqual([], validate_plan(result, WEBSITE))

    def test_stale_empty_task_graph_is_rederived_from_valid_phases(self):
        value = plan("Fix the existing add function so the focused test passes.", 2)
        value["tasks"] = []
        self.assertEqual(["tasks must be a non-empty array"], validate_plan(value, value["original_goal"]))
        repaired = derive_task_graph(value)
        self.assertEqual([], validate_plan(repaired, value["original_goal"]))
        self.assertEqual(["p1", "p2"], [item["phase_id"] for item in repaired["tasks"]])


class MajorCheckpointIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db, self.old_settings = api.db, api.settings
        api.db = Database(str(Path(self.tmp.name, "test.sqlite")))
        api.db.initialize()
        api.rebuild(self.old_settings.with_overrides({"db_path": api.db.path, "task_manager_enabled": True}))
        api.db.create_project("test", self.tmp.name, time.time(), "project")
        api.db.create_conversation("test", time.time(), "conversation", "project")
        self.client = TestClient(api.app)

    def tearDown(self):
        api.db = self.old_db
        api.rebuild(self.old_settings)
        self.tmp.cleanup()

    def prepare(self, goal=WEBSITE, model_plan=None):
        task = api.db.create_coding_task("task", "conversation", goal, "QUEUED", "QWEN_PLANNING", None, time.time())
        def preflight(task_id, name, *args):
            for tool in ("initialize", "tools/list", "reference_query"):
                api._mcp_evidence(task_id, name, status="PASS", purpose="test", tool_name=tool, result={"reference": "test"})
            return {"status": "PASS"}, None
        with patch.object(api, "_generate_plan", return_value=(model_plan or plan(goal), None)), \
             patch.object(api, "_run_required_mcp", side_effect=preflight), \
             patch.object(api, "node_mcp_launch_command", return_value=["test-runtime"]), \
             patch.object(api._coding_scheduler_wake, "set"):
            api._run_coding_planning(task)
        return api.db.coding_task("task")

    def test_checkpoint_survives_restart_and_all_continue_paths_reuse_identity(self):
        saved = self.prepare()
        self.assertEqual("RESUMABLE", saved["status"])
        self.assertEqual("RESOURCE_CHECKPOINT", saved["recovery_reason"])
        self.assertIsNone(saved["pending_authorization"])
        self.assertEqual(3, len(saved["plan"]["phases"]))
        self.assertEqual("pass", saved["plan"]["phases"][0]["status"])
        reopened = Database(api.db.path)
        reopened.initialize(); reopened.recover_interrupted_coding_tasks()
        self.assertEqual(saved["batch_handoff"], reopened.coding_task("task")["batch_handoff"])
        for message in ("続行", "continue", "次へ", None):
            api.db.update_coding_task("task", status="RESUMABLE")
            with patch.object(api._coding_scheduler_wake, "set"), patch.object(api.runtime, "execute", side_effect=AssertionError("must not create a new task")):
                response = self.client.patch("/api/coding-tasks/task", json={"resume": True}) if message is None else self.client.post("/api/chat", json={"project_id": "project", "conversation_id": "conversation", "message": message})
            self.assertEqual(200, response.status_code, response.text)
            resumed = api.db.coding_task("task")
            self.assertEqual("QUEUED", resumed["status"])
            for key in ("id", "plan_revision", "plan", "requirements", "approved_scopes", "batch_handoff", "mcp_evidence"):
                self.assertEqual(saved[key], resumed[key], key)
            self.assertEqual(1, len(api.db.coding_tasks("conversation")))

    def test_continuous_override_and_small_task_queue_automatically(self):
        for goal in (WEBSITE + "\n# Current Task\nExecute uninterrupted", "スマホでタイトルが右にはみ出すのでそこだけ直して"):
            with self.subTest(goal=goal[:30]):
                # Separate conversation/task identity for the second case.
                with api.db.connect() as connection:
                    connection.execute("DELETE FROM coding_phase_reports")
                    connection.execute("DELETE FROM coding_tasks")
                saved = self.prepare(goal)
                self.assertEqual("QUEUED", saved["status"])
                self.assertEqual("CONTINUOUS", saved["requirements"]["execution_policy"]["continuation_policy"])
                self.assertNotEqual("RESOURCE_CHECKPOINT", saved["recovery_reason"])

    def test_retry_is_local_then_checkpoint_precedes_final_verification(self):
        saved = self.prepare()
        api.db.enqueue_coding_task("task")
        attempts = []
        def execute(*args, **kwargs):
            attempts.append(args[0])
            execution = Task("test"); execution.state = TaskState.COMPLETED
            return execution, "done"
        def review(task_id, managed, phase, row, *args):
            decision = "RETRY" if len(attempts) == 1 else "PASS"
            value = {**row["structured_report"], "manager_decision": {"decision": decision}}
            api.db.update_coding_phase_report(row["id"], value, "PASS")
            return decision
        with patch.object(api.runtime, "execute", side_effect=execute), \
             patch.object(api, "_report_from_execution", side_effect=lambda phase, attempt, *args: report(phase["id"], attempt)), \
             patch.object(api, "_review_phase", side_effect=review), \
             patch.object(api, "coding_knowledge_context_for_subtask", return_value=""):
            api._run_managed_task("task", self.tmp.name)
        after = api.db.coding_task("task")
        self.assertEqual(2, len(attempts))
        self.assertEqual("RESUMABLE", after["status"])
        self.assertEqual("RESOURCE_CHECKPOINT", after["recovery_reason"])
        self.assertEqual("major-2", after["batch_handoff"]["next_phase_id"])
        self.assertEqual(["index.html"], after["batch_handoff"]["changed_files"])
        self.assertEqual(saved["plan_revision"], after["plan_revision"])

    def test_failed_evidence_cannot_be_a_successful_checkpoint(self):
        saved = self.prepare()
        phase_id = saved["plan"]["phases"][1]["id"]
        bad = {**report(phase_id), "status": "FAIL", "errors": ["build failed"]}
        api.db.add_coding_phase_report("task", phase_id, 0, bad, "PASS", time.time())
        with self.assertRaisesRegex(RuntimeError, "successful phase evidence"):
            api._heavy_batch_checkpoint("task", saved["plan"], {"major-blueprint", phase_id})

    def test_explicit_continuous_execution_does_not_pause_between_successful_phases(self):
        self.prepare(WEBSITE + "\n# Current Task\nExecute uninterrupted")
        api._mcp_evidence("task", "playwright", status="PASS", purpose="test", tool_name="browser_check", result={"verified": True})
        def execute(*args, **kwargs):
            execution = Task("test"); execution.state = TaskState.COMPLETED
            return execution, "done"
        def review(task_id, managed, phase, row, *args):
            api.db.update_coding_phase_report(row["id"], {**row["structured_report"], "manager_decision": {"decision": "PASS"}}, "PASS")
            return "PASS"
        with patch.object(api.runtime, "execute", side_effect=execute) as executor, \
             patch.object(api, "_report_from_execution", side_effect=lambda phase, attempt, *args: report(phase["id"], attempt)), \
             patch.object(api, "_review_phase", side_effect=review), \
             patch.object(api, "coding_knowledge_context_for_subtask", return_value=""), \
             patch.object(api, "_complete_task", return_value="finished"):
            self.assertEqual("finished", api._run_managed_task("task", self.tmp.name))
        self.assertEqual(2, executor.call_count)
        self.assertNotEqual("RESOURCE_CHECKPOINT", api.db.coding_task("task")["recovery_reason"])

    def test_checkpoint_transaction_rolls_back_report_on_storage_failure(self):
        saved = self.prepare()
        before = api.db.coding_phase_reports("task")
        with api.db.connect() as connection:
            connection.execute("CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON coding_tasks WHEN NEW.recovery_reason='RESOURCE_CHECKPOINT' BEGIN SELECT RAISE(ABORT, 'checkpoint storage failure'); END")
        with self.assertRaisesRegex(Exception, "checkpoint storage failure"):
            api.db.save_coding_checkpoint("task", saved["plan"], saved["batch_handoff"], 1, 0, report("major-blueprint"))
        self.assertEqual(before, api.db.coding_phase_reports("task"))

    def test_authorization_is_preserved(self):
        saved = self.prepare()
        api.db.update_coding_task("task", pending_authorization={"requested_scope": ["deploy"]})
        self.assertIsNone(api._heavy_batch_checkpoint("task", saved["plan"], {"major-blueprint"}))
        response = self.client.patch("/api/coding-tasks/task", json={"resume": True})
        self.assertEqual(409, response.status_code)
        self.assertEqual({"requested_scope": ["deploy"]}, api.db.coding_task("task")["pending_authorization"])

    def test_protected_plan_waits_for_authorization_before_checkpoint(self):
        protected = plan(WEBSITE)
        protected["scope"]["allowed"].append("deploy")
        saved = self.prepare(model_plan=protected)
        self.assertEqual("WAITING_FOR_USER", saved["status"])
        self.assertIsNotNone(saved["pending_authorization"])
        self.assertEqual([], api.db.coding_phase_reports("task"))
        api.db.accept_pending_plan("task")
        api.db.enqueue_coding_task("task")
        with patch.object(api.runtime, "execute", side_effect=AssertionError("stop before implementation")):
            api._run_managed_task("task", self.tmp.name)
        after = api.db.coding_task("task")
        self.assertEqual("RESOURCE_CHECKPOINT", after["recovery_reason"])
        self.assertEqual("RESUMABLE", after["status"])
        self.assertTrue(after["approved_scopes"])

    def test_final_graph_cycle_fails_closed_before_execution(self):
        saved = self.prepare()
        cyclic = copy.deepcopy(saved["plan"])
        cyclic["phases"][0]["dependencies"] = [cyclic["phases"][1]["id"]]
        api.db.update_coding_task("task", plan=cyclic, status="QUEUED")
        with patch.object(api.runtime, "execute", side_effect=AssertionError("must stop before implementation")):
            response = api._run_managed_task("task", self.tmp.name)
        self.assertIn("final dependency graph validation failed", response)
        self.assertEqual("BLOCKED", api.db.coding_task("task")["status"])
        self.assertEqual("FINAL_GRAPH_VALIDATION", api.db.coding_task("task")["recovery_reason"])

    def test_replan_cannot_reintroduce_a_giant_unfinished_phase(self):
        saved = self.prepare()
        replacement = copy.deepcopy(saved["plan"])
        replacement["phases"][1]["goal"] = "Inspect repository; install dependencies; implement everything; verify browser; finalize"
        with patch.object(api, "_generate_plan", return_value=(replacement, None)):
            api._replan_task("task", saved, {"major-blueprint"}, "structural correction")
        after = api.db.coding_task("task")
        self.assertEqual("RESUMABLE", after["status"])
        self.assertEqual("REPLAN_CONTINUATION", after["recovery_action"])
        self.assertNotEqual("RESOURCE_CHECKPOINT", after["recovery_reason"])
        self.assertEqual(saved["plan"], after["plan"])
