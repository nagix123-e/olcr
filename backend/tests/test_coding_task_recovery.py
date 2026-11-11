import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

import olcr_api.app as api
from olcr_api.coding_tasks import (MAX_HUMAN_RECOVERY_REPLAN_EPOCHS,
                                   MAX_SUBSTANTIAL_REPLANS_PER_TASK,
                                   TASK_GLOBAL_REPLAN_LIMIT,
                                   TASK_GLOBAL_RECOVERY_MODEL_CALL_LIMIT)
from olcr_api.db import Database
from olcr_api.models import Task, TaskState
from olcr_api.ollama import ModelFailure


def plan(goal):
    return {
        "schema_version": 1,
        "original_goal": goal,
        "scope": {"allowed": ["backend"], "forbidden": []},
        "assumptions": [],
        "phases": [
            {"id": f"p{index}", "goal": f"phase {index}", "status": "pending", "done": ["done"], "verify": ["manual check"], "dependencies": [] if index == 1 else [f"p{index - 1}"], "risks": []}
            for index in range(1, 4)
        ],
        "max_retries_per_phase": 2,
        "requires_user_approval": True,
    }


class LifecycleModel:
    def __init__(self):
        self.reviews = 0
        self.replans = 0
        self.recovery_reviews = 0

    def generate(self, messages, model, think=False, format=None):
        system = messages[0]["content"]
        if "Gemma review" in system:
            self.reviews += 1
            if self.reviews == 1:
                return {"text": '{"decision":"RETRY","reason":"retry 0"}'}
            if self.reviews == 2:
                return {"text": '{"decision":"RETRY","reason":"retry 1"}'}
            if self.reviews == 3:
                return {"text": '{"decision":"REPLAN_REQUIRED","reason":"retry exhausted"}'}
            return {"text": '{"decision":"PASS","reason":"verified"}'}
        if "Planning Mode" in system:
            self.replans += 1
            return {"text": json.dumps(plan("fix sample"))}
        if "completion check" in system:
            return {"text": '{"decision":"PASS","reason":"complete"}'}
        if "final report" in system:
            return {"text": "completed"}
        raise AssertionError("unexpected lifecycle model call")


class RecoveryReviewModel:
    def __init__(self):
        self.reviews = 0

    def generate(self, messages, model, think=False, format=None):
        system = messages[0]["content"]
        if "Gemma review" in system:
            self.reviews += 1
            return {"text": '{"decision":"PASS","reason":"saved typed evidence verified"}'}
        if "completion check" in system:
            return {"text": '{"decision":"PASS","reason":"complete"}'}
        if "final report" in system:
            return {"text": "completed"}
        raise AssertionError("recovery must not start a new implementation call")


class ResumeEpochModel:
    def __init__(self):
        self.replans = 0

    def generate(self, messages, model, think=False, format=None):
        system = messages[0]["content"]
        if "Planning Mode" in system:
            self.replans += 1
            revised = plan("fix sample")
            revised["phases"][0]["goal"] = "revised phase 1 after an explicitly granted recovery epoch"
            return {"text": json.dumps(revised)}
        if "Gemma review" in system or "completion check" in system:
            return {"text": '{"decision":"PASS","reason":"verified"}'}
        if "final report" in system:
            return {"text": "completed"}
        raise AssertionError("unexpected resumed recovery model call")


class CodingTaskRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.scheduler_running = bool(api._coding_scheduler_thread and api._coding_scheduler_thread.is_alive())
        api.stop_coding_scheduler()
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db, self.old_runtime = api.db, api.runtime
        api.db = Database(str(Path(self.tmp.name) / "recovery.sqlite"))
        api.db.initialize()
        api.db.create_project("Project", self.tmp.name, time.time(), "project")
        api.db.create_conversation("Conversation", time.time(), "conversation", "project")
        self.client = TestClient(api.app)

    def tearDown(self):
        api.stop_coding_scheduler()
        api.runtime, api.db = self.old_runtime, self.old_db
        self.tmp.cleanup()
        if self.scheduler_running:
            api.start_coding_scheduler()

    def test_structural_write_failure_reaches_distinct_model_retry(self):
        from contextlib import nullcontext

        task_id = "structural-write-retry"
        goal = "Create a small page"
        task_plan = plan(goal)
        task_plan["phases"] = [task_plan["phases"][0]]
        task_plan["phases"][0].update(
            goal="Create probe.js", done=["probe.js exists"], verify=["The source parses"],
            execution_mode="IMPLEMENTATION")
        task_plan["scope"]["allowed"] = ["probe.js"]
        manifest = [{"path": "probe.js", "action": "create", "required": True,
                     "owner_phase": "p1"}]
        task_plan["file_manifest"] = manifest
        task_plan["implementation_plan_artifact"] = {
            "target_root": self.tmp.name, "file_manifest": manifest}
        api.db.create_coding_task(task_id, "conversation", goal, "QUEUED", "NONE", task_plan, time.time())
        api.db.update_coding_task(task_id, current_phase_id="p1",
            approved_scopes=[{"source": "original_request", "requested_scope": ["probe.js"]}],
            subtask_progress=[{"task_id": "p1-task", "phase_id": "p1", "status": "FAILED",
                               "verification_status": "FAILED", "attempt": 0}])
        failure = {"tool": "operation_application_failure", "status": "failure",
            "output": {"worktree_state": "ROLLED_BACK", "operation_diagnostic": {
                "relative_target_path": "probe.js", "reason_subtype": "STRUCTURAL_VALIDATION_FAILED"}}}
        report = {"phase_id": "p1", "attempt": 0, "plan_revision": 0, "recovery_epoch": 0,
            "status": "FAIL", "implementation_model": api.settings.long_implementation_model,
            "errors": ["structural validation failed: [stdin]:1 SyntaxError"],
            "typed_execution_summary": {"state": "failed", "error": "structural validation failed",
                "failure_class": "WRITE_CONTENT_INVALID", "worktree_state": "ROLLED_BACK",
                "operations": [failure]}}
        api._save_report(task_id, "p1", 0, report, "PASS")

        class ReachedImplementer(Exception):
            pass

        class Runtime:
            def execute(self, request, **kwargs):
                self.managed_context = kwargs["managed_context"]
                raise ReachedImplementer

        runtime = Runtime()
        api.runtime = runtime
        with patch.object(api, "_alternate_coding_model", return_value="distinct-repair-model"), \
             patch.object(api.task_workspaces.service, "executing", return_value=nullcontext(self.tmp.name)):
            with self.assertRaises(ReachedImplementer):
                api._run_managed_task(task_id, self.tmp.name)

        self.assertEqual("distinct-repair-model", runtime.managed_context["coding_model"])

    def test_stop_interrupts_detached_planning_and_persists_resume_state(self):
        import threading
        from contextlib import nullcontext
        from olcr_api.generation_control import current_generation,check_cancelled
        task_id='pause-planning-worker'
        api.db.create_coding_task(task_id,'conversation','build a page','QUEUED','QWEN_PLANNING',None,time.time())
        started=threading.Event()
        def waiting_plan(managed):
            api.db.update_coding_task(task_id,status='PLANNING')
            generation=current_generation()
            self.assertIsNotNone(generation)
            started.set()
            generation.event.wait(3)
            check_cancelled()
            raise AssertionError('provider wait was not interrupted')
        with patch.object(api,'_coding_workspace_ready',return_value=True),patch.object(api.task_workspaces.service,'executing',return_value=nullcontext(self.tmp.name)),patch.object(api.task_workspaces.service,'recover_steering'),patch.object(api,'_dispatch_coding_followups'),patch.object(api,'_run_coding_planning',side_effect=waiting_plan):
            worker=threading.Thread(target=api._coding_job,args=(api.db.coding_task(task_id),),daemon=True)
            worker.start()
            self.assertTrue(started.wait(2))
            result=self.client.patch('/api/coding-tasks/'+task_id,json={'pause_requested':True})
            self.assertEqual(200,result.status_code)
            worker.join(2)
            self.assertFalse(worker.is_alive())
        task=api.db.coding_task(task_id)
        self.assertEqual('RESUMABLE',task['status'])
        self.assertEqual('PAUSED',task['recovery_reason'])
        self.assertTrue(task['resume_state'].get('stop_progress'))

    @unittest.skip("OBSOLETE_TEST_EXPECTATION: pre-Orchestrator Gemma/guard lifecycle")
    def test_valid_replan_starts_a_new_plan_revision_without_attempt_three(self):
        goal = "fix sample"
        task_id = "replan-continuation"
        api.db.create_coding_task(task_id, "conversation", goal, "QUEUED", "NONE", plan(goal), time.time())
        api.db.enqueue_coding_task(task_id)

        class Runtime:
            def __init__(self):
                self.model = LifecycleModel()
                self.executions = []

            def execute(self, request, **kwargs):
                self.executions.append(request)
                task = Task(request)
                task.transition(TaskState.ROUTING)
                task.transition(TaskState.GENERATING)
                task.transition(TaskState.COMPLETED)
                return task, "implemented"

        runtime = Runtime()
        api.runtime = runtime
        # A successful substantial replan returns to the live scheduler queue.
        # Its next dequeue must execute the new revision automatically, rather
        # than interpreting the old REPLAN_REQUIRED report again.
        api._coding_scheduler_wake.set()
        deadline = time.time() + 2
        while time.time() < deadline and api.db.coding_task(task_id)["status"] != "COMPLETED":
            time.sleep(0.01)

        task = api.db.coding_task(task_id)
        reports = api.db.coding_phase_reports(task_id)
        p1 = [row["structured_report"] for row in reports if row["phase_id"] == "p1"]
        self.assertEqual("COMPLETED", task["status"])
        self.assertEqual(1, task["replan_count"])
        self.assertEqual("NONE", task["recovery_action"])
        self.assertEqual("NONE", task["recovery_reason"])
        self.assertEqual(1, runtime.model.replans)
        self.assertEqual([0, 1, 2], [row["attempt"] for row in p1 if row.get("plan_revision", 0) == 0])
        self.assertEqual([0], [row["attempt"] for row in p1 if row.get("plan_revision") == 1])
        self.assertEqual(6, len(runtime.executions))
        self.assertNotIn(task_id, api._active_runners)

    @unittest.skip("OBSOLETE_TEST_EXPECTATION: pre-Orchestrator Gemma/guard lifecycle")
    def test_resume_queues_and_performs_one_saved_recovery_review_without_qwen_duplicate(self):
        goal = "fix sample"
        task_id = "resume-recovery"
        task_plan = plan(goal)
        task_plan["phases"] = [task_plan["phases"][0]]
        api.db.create_coding_task(task_id, "conversation", goal, "RESUMABLE", "NONE", task_plan, time.time())
        api.db.update_coding_task(task_id, recovery_action="RECOVERY_REVIEW", recovery_reason="SCHEDULER")
        report = {
            "phase_id": "p1", "attempt": 0, "status": "PASS", "implemented": ["saved result"], "changed_files": [],
            "test_executed": [], "test_pass": [], "test_fail": [], "build_executed": "NOT_RUN", "build_pass": "NOT_RUN",
            "errors": [], "blockers": [], "risks": [], "plan_revision": 0,
            "typed_execution_summary": {"state": "completed", "error": None, "operations": []},
        }
        api._save_report(task_id, "p1", 0, report, "PASS")

        class Runtime:
            def __init__(self):
                self.model = RecoveryReviewModel()
                self.executions = 0

            def execute(self, request, **kwargs):
                self.executions += 1
                raise AssertionError("saved recovery should review before implementation")

        runtime = Runtime()
        api.runtime = runtime
        response = self.client.patch(f"/api/coding-tasks/{task_id}", json={"resume": True})
        self.assertEqual(200, response.status_code)
        self.assertEqual("QUEUED", response.json()["status"])

        deadline = time.time() + 2
        while time.time() < deadline and api.db.coding_task(task_id)["status"] != "COMPLETED":
            time.sleep(0.01)
        task = api.db.coding_task(task_id)
        self.assertEqual("COMPLETED", task["status"])
        self.assertEqual("NONE", task["recovery_action"])
        self.assertEqual("NONE", task["recovery_reason"])
        self.assertEqual(1, runtime.model.reviews)
        self.assertEqual(0, runtime.executions)

    def test_replan_limit_is_a_resumable_replan_continuation_with_a_durable_reason(self):
        task_id = "replan-limit"
        api.db.create_coding_task(task_id, "conversation", "fix sample", "QUEUED", "NONE", plan("fix sample"), time.time())
        api.db.update_coding_task(task_id, replan_count=MAX_SUBSTANTIAL_REPLANS_PER_TASK, replan_count_in_epoch=MAX_SUBSTANTIAL_REPLANS_PER_TASK, queue_order=9)

        result = api._replan_task(task_id, api.db.coding_task(task_id) or {}, set(), "retry exhausted")
        task = api.db.coding_task(task_id)

        self.assertEqual("Coding Task could not continue after the replan limit.", result)
        self.assertEqual("RESUMABLE", task["status"])
        self.assertEqual("NONE", task["activity"])
        self.assertIsNone(task["queue_order"])
        self.assertEqual("REPLAN_CONTINUATION", task["recovery_action"])
        self.assertEqual("REPLAN_LIMIT", task["recovery_reason"])
        reopened = Database(api.db.path)
        reopened.initialize()
        persisted = reopened.coding_task(task_id)
        self.assertEqual(0, persisted["recovery_epoch"])
        self.assertEqual(MAX_SUBSTANTIAL_REPLANS_PER_TASK, persisted["replan_count_in_epoch"])
        self.assertEqual("REPLAN_LIMIT", persisted["recovery_reason"])

    def test_explicit_resume_at_global_replan_limit_grants_one_bounded_epoch(self):
        task_id = "replan-human-epoch"
        task_plan = plan("fix sample")
        task_plan["phases"] = [task_plan["phases"][0]]
        api.db.create_coding_task(task_id, "conversation", "fix sample", "BLOCKED", "NONE", task_plan, time.time())
        api.db.update_coding_task(task_id, replan_count=TASK_GLOBAL_REPLAN_LIMIT,
                                  replan_count_in_epoch=MAX_SUBSTANTIAL_REPLANS_PER_TASK,
                                  recovery_epoch=1, recovery_action="NONE",
                                  recovery_reason="GLOBAL_RECOVERY_LIMIT")

        # Exercise the same explicit chat-command path as the visible GUI,
        # while keeping the test focused on the durable bounded-epoch gate.
        with patch.object(api._coding_scheduler_wake, "set"):
            response = self.client.post("/api/chat", json={"message": "再開", "conversation_id": "conversation", "project_id": "project"})
        self.assertEqual(200, response.status_code)
        queued = response.json()
        self.assertEqual(task_id, queued["coding_task_id"])
        queued_task = api.db.coding_task(task_id)
        self.assertEqual("QUEUED", queued_task["status"])
        self.assertEqual(2, queued_task["recovery_epoch"])
        self.assertEqual(0, queued_task["replan_count_in_epoch"])
        self.assertEqual(1, queued_task["human_recovery_epochs"])
        self.assertEqual(TASK_GLOBAL_REPLAN_LIMIT + MAX_SUBSTANTIAL_REPLANS_PER_TASK,
                         api._task_global_replan_limit(queued_task))

    def test_global_replan_budget_allows_only_four_human_epochs(self):
        task_id = "replan-human-epoch-exhausted"
        task_plan = plan("fix sample")
        api.db.create_coding_task(task_id, "conversation", "fix sample", "BLOCKED", "NONE", task_plan, time.time())
        api.db.update_coding_task(task_id, replan_count=TASK_GLOBAL_REPLAN_LIMIT + MAX_HUMAN_RECOVERY_REPLAN_EPOCHS * MAX_SUBSTANTIAL_REPLANS_PER_TASK,
                                  human_recovery_epochs=MAX_HUMAN_RECOVERY_REPLAN_EPOCHS,
                                  recovery_action="NONE", recovery_reason="GLOBAL_RECOVERY_LIMIT")

        result = api._resume_coding_task_control(api.db.coding_task(task_id), source="explicit test resume")
        task = api.db.coding_task(task_id)
        self.assertEqual("BLOCKED", task["status"])
        self.assertEqual("GLOBAL_RECOVERY_LIMIT", task["recovery_reason"])
        self.assertEqual(MAX_HUMAN_RECOVERY_REPLAN_EPOCHS, task["human_recovery_epochs"])
        self.assertEqual(TASK_GLOBAL_REPLAN_LIMIT + MAX_HUMAN_RECOVERY_REPLAN_EPOCHS * MAX_SUBSTANTIAL_REPLANS_PER_TASK, task["replan_count"])
        self.assertEqual("BLOCKED", result["progress_event"]["status"])

    def test_final_human_epoch_can_resume_remaining_retry_without_granting_another_epoch(self):
        task_id = "same-final-replan-epoch"
        task_plan = plan("fix sample")
        task_plan["phases"] = [task_plan["phases"][0]]
        api.db.create_coding_task(task_id, "conversation", "fix sample", "BLOCKED", "NONE", task_plan, time.time())
        api.db.update_coding_task(
            task_id, replan_count=TASK_GLOBAL_REPLAN_LIMIT + 2 * MAX_SUBSTANTIAL_REPLANS_PER_TASK,
            replan_count_in_epoch=1, recovery_epoch=6,
            human_recovery_epochs=MAX_HUMAN_RECOVERY_REPLAN_EPOCHS,
            task_total_recovery_model_calls=TASK_GLOBAL_RECOVERY_MODEL_CALL_LIMIT - 1,
            recovery_action="NONE", recovery_reason="GLOBAL_RECOVERY_LIMIT")

        with patch.object(api._coding_scheduler_wake, "set"):
            response = self.client.patch(f"/api/coding-tasks/{task_id}", json={"resume": True})

        self.assertEqual(200, response.status_code)
        self.assertEqual(task_id, response.json()["id"])
        queued = api.db.coding_task(task_id)
        self.assertEqual("QUEUED", queued["status"])
        self.assertEqual("REPLAN_LIMIT", queued["recovery_reason"])
        self.assertEqual(6, queued["recovery_epoch"])
        self.assertEqual(1, queued["replan_count_in_epoch"])
        self.assertEqual(MAX_HUMAN_RECOVERY_REPLAN_EPOCHS, queued["human_recovery_epochs"])
        self.assertEqual(TASK_GLOBAL_RECOVERY_MODEL_CALL_LIMIT - 1,
                         queued["task_total_recovery_model_calls"])
        self.assertTrue(api._begin_human_recovery_epoch(queued))
        after = api.db.coding_task(task_id)
        self.assertEqual(6, after["recovery_epoch"])
        self.assertEqual(1, after["replan_count_in_epoch"])
        self.assertEqual(MAX_HUMAN_RECOVERY_REPLAN_EPOCHS, after["human_recovery_epochs"])

    def test_deterministic_verifier_fallback_changes_only_explicitly_corrected_command(self):
        allowed = ["__init__.py", "domain.py", "scheduler.py", "test_scheduler.py"]
        task_plan = plan("fix sample")
        task_plan["scope"]["allowed"] = allowed
        manifest = [{"path": path, "action": "modify"} for path in allowed]
        task_plan["file_manifest"] = manifest
        task_plan["implementation_plan_artifact"] = {
            "target_root": self.tmp.name, "file_manifest": manifest,
        }
        task_plan["phases"] = [task_plan["phases"][0]]
        phase = task_plan["phases"][0]
        phase["verification_targets"] = allowed
        phase["verification_command"] = ["python3", "-B", "-m", "unittest", "discover",
                                         "-s", "OLCR_E2E_LONG_R17_20260924", "-v"]
        phase["verify"] = ["python3 -B -m unittest discover -s OLCR_E2E_LONG_R17_20260924 -v passes with nonzero tests"]
        task = {"plan": task_plan, "original_goal": "fix sample",
                "approved_scopes": [{"requested_scope": allowed}]}
        revision = "Use exactly python3 -B -m unittest discover -s . -v in the same task."

        corrected = api._deterministic_verifier_recovery_plan(task, revision)

        self.assertIsNotNone(corrected)
        self.assertEqual(allowed, corrected["scope"]["allowed"])
        self.assertEqual(allowed, [item["path"] for item in corrected["file_manifest"]])
        self.assertEqual(["python3", "-B", "-m", "unittest", "discover", "-s", ".", "-v"],
                         corrected["phases"][0]["verification_command"])
        self.assertIn("python3 -B -m unittest discover -s . -v", corrected["phases"][0]["verify"][0])
        self.assertIsNone(api._deterministic_verifier_recovery_plan(task, "use another verifier"))

    def test_exhausted_model_budget_checkpoints_exact_verifier_fallback_without_a_model_call(self):
        task_id = "deterministic-final-verifier-replan"
        goal = "fix sample; run tests"
        allowed = ["__init__.py", "domain.py", "scheduler.py", "test_scheduler.py"]
        task_plan = plan(goal)
        task_plan["scope"]["allowed"] = allowed
        manifest = [{"path": path, "action": "modify"} for path in allowed]
        artifact = {"target_root": self.tmp.name, "file_manifest": manifest,
                    "file_manifest_count": len(manifest)}
        task_plan["file_manifest"] = manifest
        task_plan["implementation_plan_artifact"] = artifact
        task_plan["phases"] = [task_plan["phases"][0]]
        phase = task_plan["phases"][0]
        phase.update({
            "verification_targets": allowed,
            "verification_command": ["python3", "-B", "-m", "unittest", "discover", "-s",
                                     "OLCR_E2E_LONG_R17_20260924", "-v"],
            "verify": ["python3 -B -m unittest discover -s OLCR_E2E_LONG_R17_20260924 -v passes with nonzero tests"],
        })
        api.db.create_coding_task(task_id, "conversation", goal, "RESUMABLE", "NONE", task_plan, time.time())
        api.db.update_coding_task(
            task_id, plan=task_plan, plan_revision=0, current_phase_id="p1",
            approved_scopes=[{"requested_scope": allowed}],
            replan_count=TASK_GLOBAL_REPLAN_LIMIT + 2 * MAX_SUBSTANTIAL_REPLANS_PER_TASK - 3,
            replan_count_in_epoch=MAX_SUBSTANTIAL_REPLANS_PER_TASK,
            recovery_epoch=6, human_recovery_epochs=MAX_HUMAN_RECOVERY_REPLAN_EPOCHS,
            task_total_recovery_model_calls=TASK_GLOBAL_RECOVERY_MODEL_CALL_LIMIT,
            recovery_action="REPLAN_CONTINUATION", recovery_reason="REPLAN_VALIDATION")
        managed = api.db.coding_task(task_id)
        revision = "Correct verifier: python3 -B -m unittest discover -s . -v; keep same task and four files."

        with patch.object(api, "_generate_plan", side_effect=AssertionError("recovery budget is exhausted")), \
             patch.object(api, "prepare_implementation_plan",
                          side_effect=lambda _task_id, value, _root, _requirements, _workspace: (value, artifact)):
            result = api._replan_task(task_id, managed, set(), revision)

        saved = api.db.coding_task(task_id)
        self.assertEqual("Coding Task was replanned and requeued.", result)
        self.assertEqual(1, saved["plan_revision"])
        self.assertEqual(TASK_GLOBAL_RECOVERY_MODEL_CALL_LIMIT, saved["task_total_recovery_model_calls"])
        self.assertEqual(MAX_SUBSTANTIAL_REPLANS_PER_TASK, saved["replan_count_in_epoch"])
        self.assertEqual(["python3", "-B", "-m", "unittest", "discover", "-s", ".", "-v"],
                         saved["plan"]["phases"][0]["verification_command"])

    def test_final_epoch_resume_with_exhausted_models_uses_same_epoch_deterministic_recovery(self):
        task_id = "final-epoch-deterministic-resume"
        goal = "fix sample"
        allowed = ["__init__.py", "domain.py", "scheduler.py", "test_scheduler.py"]
        task_plan = plan(goal)
        task_plan["scope"]["allowed"] = allowed
        manifest = [{"path": path, "action": "modify"} for path in allowed]
        task_plan["file_manifest"] = manifest
        task_plan["implementation_plan_artifact"] = {
            "target_root": self.tmp.name, "file_manifest": manifest,
        }
        task_plan["phases"] = [task_plan["phases"][0]]
        task_plan["phases"][0].update({
            "verification_targets": allowed,
            "verification_command": ["python3", "-B", "-m", "unittest", "discover", "-s",
                                     "OLCR_E2E_LONG_R17_20260924", "-v"],
            "verify": ["python3 -B -m unittest discover -s OLCR_E2E_LONG_R17_20260924 -v passes with nonzero tests"],
        })
        revision = "Correct verifier to python3 -B -m unittest discover -s . -v; same task and four files only."
        api.db.create_coding_task(task_id, "conversation", goal, "RESUMABLE", "NONE", task_plan, time.time())
        api.db.update_coding_task(
            task_id, current_phase_id="p1", approved_scopes=[{"requested_scope": allowed}],
            recovery_epoch=6, replan_count=9, replan_count_in_epoch=2,
            human_recovery_epochs=MAX_HUMAN_RECOVERY_REPLAN_EPOCHS,
            task_total_recovery_model_calls=TASK_GLOBAL_RECOVERY_MODEL_CALL_LIMIT,
            recovery_action="REPLAN_CONTINUATION", recovery_reason="REPLAN_VALIDATION",
            resume_state={"continuation_user_revision": revision})

        with patch.object(api._coding_scheduler_wake, "set"):
            result = api._resume_coding_task_control(api.db.coding_task(task_id), source="explicit final-epoch test")

        queued = api.db.coding_task(task_id)
        self.assertEqual("QUEUED", queued["status"])
        self.assertEqual("REPLAN_LIMIT", queued["recovery_reason"])
        self.assertEqual(6, queued["recovery_epoch"])
        self.assertEqual(2, queued["replan_count_in_epoch"])
        self.assertEqual(MAX_HUMAN_RECOVERY_REPLAN_EPOCHS, queued["human_recovery_epochs"])
        self.assertEqual(TASK_GLOBAL_RECOVERY_MODEL_CALL_LIMIT, queued["task_total_recovery_model_calls"])
        self.assertEqual(task_id, result["coding_task_id"])

    def test_second_explicit_human_resume_grants_one_final_bounded_epoch(self):
        task_id = "replan-human-epoch-second"
        task_plan = plan("fix sample")
        api.db.create_coding_task(task_id, "conversation", "fix sample", "BLOCKED", "NONE", task_plan, time.time())
        api.db.update_coding_task(task_id,
                                  replan_count=TASK_GLOBAL_REPLAN_LIMIT + MAX_SUBSTANTIAL_REPLANS_PER_TASK,
                                  replan_count_in_epoch=MAX_SUBSTANTIAL_REPLANS_PER_TASK,
                                  recovery_epoch=2, human_recovery_epochs=1,
                                  recovery_action="NONE", recovery_reason="GLOBAL_RECOVERY_LIMIT")

        with patch.object(api._coding_scheduler_wake, "set"):
            response = self.client.post("/api/chat", json={"message": "再開", "conversation_id": "conversation", "project_id": "project"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(task_id, response.json()["coding_task_id"])
        queued_task = api.db.coding_task(task_id)
        self.assertEqual("QUEUED", queued_task["status"])
        self.assertEqual(3, queued_task["recovery_epoch"])
        self.assertEqual(0, queued_task["replan_count_in_epoch"])
        self.assertEqual(2, queued_task["human_recovery_epochs"])
        self.assertEqual(TASK_GLOBAL_REPLAN_LIMIT + 2 * MAX_SUBSTANTIAL_REPLANS_PER_TASK,
                         api._task_global_replan_limit(queued_task))

    def test_third_explicit_human_resume_grants_one_final_bounded_epoch(self):
        task_id = "replan-human-epoch-third"
        task_plan = plan("fix sample")
        api.db.create_coding_task(task_id, "conversation", "fix sample", "BLOCKED", "NONE", task_plan, time.time())
        resume_state = {"lifetime_no_progress_count": 3,
                        "last_failure_fingerprint": "same-failure",
                        "failure_fingerprints": ["same-failure"]}
        api.db.update_coding_task(task_id, resume_state=resume_state,
                                  replan_count=TASK_GLOBAL_REPLAN_LIMIT + 2 * MAX_SUBSTANTIAL_REPLANS_PER_TASK,
                                  replan_count_in_epoch=MAX_SUBSTANTIAL_REPLANS_PER_TASK,
                                  recovery_epoch=3, human_recovery_epochs=2,
                                  recovery_action="NONE", recovery_reason="NO_PROGRESS")

        with patch.object(api._coding_scheduler_wake, "set"):
            response = self.client.post("/api/chat", json={"message": "再開", "conversation_id": "conversation", "project_id": "project"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(task_id, response.json()["coding_task_id"])
        queued_task = api.db.coding_task(task_id)
        self.assertEqual("QUEUED", queued_task["status"])
        self.assertEqual(4, queued_task["recovery_epoch"])
        self.assertEqual(0, queued_task["replan_count_in_epoch"])
        self.assertEqual(3, queued_task["human_recovery_epochs"])
        self.assertEqual(TASK_GLOBAL_REPLAN_LIMIT + 3 * MAX_SUBSTANTIAL_REPLANS_PER_TASK,
                         api._task_global_replan_limit(queued_task))
        self.assertEqual(3, queued_task["resume_state"]["lifetime_no_progress_count"])
        self.assertEqual(["same-failure"], queued_task["resume_state"]["failure_fingerprints"])

    def test_explicit_resume_grants_final_epoch_after_model_call_failure(self):
        task_id = "replan-human-epoch-model-failure"
        task_plan = plan("fix sample")
        api.db.create_coding_task(task_id, "conversation", "fix sample", "RESUMABLE", "NONE", task_plan, time.time())
        api.db.update_coding_task(task_id,
                                  replan_count=TASK_GLOBAL_REPLAN_LIMIT + 2 * MAX_SUBSTANTIAL_REPLANS_PER_TASK,
                                  replan_count_in_epoch=1, recovery_epoch=4, human_recovery_epochs=3,
                                  task_total_recovery_model_calls=TASK_GLOBAL_RECOVERY_MODEL_CALL_LIMIT - 1,
                                  recovery_action="RECOVERY_REVIEW", recovery_reason="MODEL_CALL")

        with patch.object(api._coding_scheduler_wake, "set"):
            response = self.client.post("/api/chat", json={"message": "再開", "conversation_id": "conversation", "project_id": "project"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(task_id, response.json()["coding_task_id"])
        queued = api.db.coding_task(task_id)
        self.assertEqual("QUEUED", queued["status"])
        self.assertEqual(5, queued["recovery_epoch"])
        self.assertEqual(4, queued["human_recovery_epochs"])
        self.assertEqual(0, queued["replan_count_in_epoch"])
        self.assertEqual(TASK_GLOBAL_RECOVERY_MODEL_CALL_LIMIT - 1, queued["task_total_recovery_model_calls"])

    def test_model_failure_resume_is_denied_after_final_bounded_epoch(self):
        task_id = "replan-human-epoch-model-failure-exhausted"
        task_plan = plan("fix sample")
        api.db.create_coding_task(task_id, "conversation", "fix sample", "RESUMABLE", "NONE", task_plan, time.time())
        api.db.update_coding_task(task_id, replan_count=TASK_GLOBAL_REPLAN_LIMIT + 2 * MAX_SUBSTANTIAL_REPLANS_PER_TASK,
                                  recovery_epoch=5, human_recovery_epochs=4,
                                  task_total_recovery_model_calls=TASK_GLOBAL_RECOVERY_MODEL_CALL_LIMIT,
                                  recovery_action="RECOVERY_REVIEW", recovery_reason="MODEL_CALL")

        result = api._resume_coding_task_control(api.db.coding_task(task_id), source="explicit final-bound test")
        blocked = api.db.coding_task(task_id)
        self.assertEqual("BLOCKED", blocked["status"])
        self.assertEqual("GLOBAL_RECOVERY_LIMIT", blocked["recovery_reason"])
        self.assertEqual("BLOCKED", result["progress_event"]["status"])

    def test_explicit_gui_resume_grants_one_bounded_epoch_after_narrative_only_exhaustion(self):
        task_id = "narrative-only-explicit-recovery"
        task_plan = plan("repair the authorized fixture")
        allowed = ["OLCR_E2E_LONG_R17_20260924/__init__.py",
                   "OLCR_E2E_LONG_R17_20260924/domain.py",
                   "OLCR_E2E_LONG_R17_20260924/scheduler.py",
                   "OLCR_E2E_LONG_R17_20260924/test_scheduler.py"]
        task_plan["scope"]["allowed"] = list(allowed)
        task_plan["file_manifest"] = [{"path": path, "action": "modify"} for path in allowed]
        task_plan["implementation_plan_artifact"] = {
            "target_root": self.tmp.name,
            "file_manifest": [{"path": path, "action": "modify"} for path in allowed],
        }
        task_plan["phases"] = [task_plan["phases"][0]]
        api.db.create_coding_task(task_id, "conversation", "repair the authorized fixture", "BLOCKED", "NONE", task_plan, time.time())
        api.db.update_coding_task(task_id, current_phase_id="p1", retry_count=1,
                                  approved_scopes=[{"source": "original_request", "requested_scope": allowed}],
                                  subtask_progress=[{"task_id": "p1-task", "phase_id": "p1", "status": "FAILED",
                                                     "verification_status": "FAILED", "attempt": 2,
                                                     "failure_summary": "implementation operation was narrative-only"}],
                                  recovery_action="NONE", recovery_reason="NARRATIVE_ONLY_RETRY_EXHAUSTED")

        with patch.object(api._coding_scheduler_wake, "set"):
            response = self.client.patch(f"/api/coding-tasks/{task_id}", json={"resume": True})

        self.assertEqual(200, response.status_code)
        queued = api.db.coding_task(task_id)
        self.assertEqual(task_id, response.json()["id"])
        self.assertEqual("QUEUED", queued["status"])
        self.assertEqual(1, queued["recovery_epoch"])
        self.assertEqual(1, queued["human_recovery_epochs"])
        self.assertEqual(0, queued["retry_count"])
        self.assertEqual(0, queued["replan_count_in_epoch"])
        self.assertEqual(0, queued["task_total_recovery_model_calls"])
        self.assertEqual(allowed, queued["plan"]["scope"]["allowed"])
        self.assertEqual("WAITING", queued["subtask_progress"][0]["status"])
        self.assertEqual(1, len(api.db.coding_tasks("conversation")))

    def test_narrative_only_resume_stays_blocked_when_bounded_epoch_budget_is_exhausted(self):
        task_id = "narrative-only-epoch-budget-exhausted"
        task_plan = plan("repair sample")
        task_plan["scope"]["allowed"] = ["sample.py"]
        task_plan["file_manifest"] = [{"path": "sample.py", "action": "modify"}]
        task_plan["implementation_plan_artifact"] = {"target_root": self.tmp.name,
                                                       "file_manifest": [{"path": "sample.py", "action": "modify"}]}
        task_plan["phases"] = [task_plan["phases"][0]]
        api.db.create_coding_task(task_id, "conversation", "repair sample", "BLOCKED", "NONE", task_plan, time.time())
        api.db.update_coding_task(task_id, current_phase_id="p1", human_recovery_epochs=MAX_HUMAN_RECOVERY_REPLAN_EPOCHS,
                                  approved_scopes=[{"source": "original_request", "requested_scope": ["sample.py"]}],
                                  recovery_reason="NARRATIVE_ONLY_RETRY_EXHAUSTED")
        response = self.client.patch(f"/api/coding-tasks/{task_id}", json={"resume": True})
        self.assertEqual(200, response.status_code)
        self.assertEqual("BLOCKED", api.db.coding_task(task_id)["status"])
        self.assertEqual(0, api.db.coding_task(task_id)["recovery_epoch"])

    def test_narrative_only_resume_rejects_manifest_scope_mismatch(self):
        task_id = "narrative-only-scope-mismatch"
        task_plan = plan("repair sample")
        task_plan["scope"]["allowed"] = ["inside.py"]
        task_plan["file_manifest"] = [{"path": "inside.py", "action": "modify"}]
        task_plan["implementation_plan_artifact"] = {"target_root": self.tmp.name,
                                                       "file_manifest": [{"path": "inside.py", "action": "modify"}]}
        task_plan["phases"] = [task_plan["phases"][0]]
        api.db.create_coding_task(task_id, "conversation", "repair sample", "BLOCKED", "NONE", task_plan, time.time())
        api.db.update_coding_task(task_id, current_phase_id="p1",
                                  approved_scopes=[{"source": "original_request", "requested_scope": ["outside.py"]}],
                                  recovery_reason="NARRATIVE_ONLY_RETRY_EXHAUSTED")
        with patch.object(api._coding_scheduler_wake, "set"):
            response = self.client.patch(f"/api/coding-tasks/{task_id}", json={"resume": True})
        self.assertEqual(200, response.status_code)
        task = api.db.coding_task(task_id)
        self.assertEqual("BLOCKED", task["status"])
        self.assertEqual(0, task["recovery_epoch"])
        self.assertEqual(1, len(api.db.coding_tasks("conversation")))

    def test_model_failure_is_resumable_and_the_scheduler_keeps_running(self):
        task_id = "model-failure"
        api._coding_telemetry.pop(task_id, None)
        task_plan=plan("fix sample")
        task_plan["scope"]["allowed"]=["sample.py"]
        task_plan["phases"]=[task_plan["phases"][0]]
        task_plan["phases"][0].update(goal="Fix sample.py",done=["sample.py exists"],verify=[],
                                      execution_mode="IMPLEMENTATION")
        task_plan["file_manifest"]=[{"path":"sample.py","action":"modify","owner_phase":"p1"}]
        (Path(self.tmp.name)/"sample.py").write_text("sample = True\n")
        api.db.create_coding_task(task_id, "conversation", "fix sample", "QUEUED", "NONE", task_plan, time.time())
        api.db.enqueue_coding_task(task_id)

        class Runtime:
            model = object()

            def execute(self, request, **kwargs):
                telemetry = api._coding_telemetry.setdefault(task_id, api.CodingTelemetry(task_id, model_name="test-model"))
                now = time.time()
                telemetry.add_call(phase_id="p1", role="IMPLEMENTER", model="test-model", messages=[],
                                   result=None, started=now, finished=now, structured=False, thinking=False,
                                   success=False, failure_class="ModelFailure")
                raise ModelFailure("unavailable", "model connection reset")

        api.runtime = Runtime()
        api.start_coding_scheduler()
        api._coding_scheduler_wake.set()
        deadline = time.time() + 2
        while time.time() < deadline and api.db.coding_task(task_id)["status"] != "RESUMABLE":
            time.sleep(0.01)
        task = api.db.coding_task(task_id)

        self.assertEqual("RESUMABLE", task["status"])
        self.assertEqual("NONE", task["activity"])
        self.assertEqual("RECOVERY_REVIEW", task["recovery_action"])
        self.assertEqual("MODEL_CALL", task["recovery_reason"])
        self.assertNotIn(task_id, api._active_runners)
        persisted_telemetry = api.db.coding_telemetry(task_id)
        self.assertIsNotNone(persisted_telemetry)
        self.assertEqual(1, persisted_telemetry["model_call_count"])
        self.assertEqual("ModelFailure", persisted_telemetry["calls"][0]["failure_class"])
        api._coding_telemetry.pop(task_id, None)

    def test_missing_model_adapter_is_reported_as_a_model_failure(self):
        task_id='missing-model-adapter'
        api.db.create_coding_task(task_id,'conversation','plan the implementation',
                                  'PLANNING','QWEN_PLANNING',plan('plan the implementation'),time.time())
        api.runtime=SimpleNamespace()
        with self.assertRaises(ModelFailure):
            api._model_text(task_id,'PLANNING','QWEN_PLANNING','role:planner',
                            [{'role':'user','content':'Plan.'}])


if __name__ == "__main__":
    unittest.main()
