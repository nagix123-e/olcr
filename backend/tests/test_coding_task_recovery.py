import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import olcr_api.app as api
from olcr_api.coding_tasks import MAX_SUBSTANTIAL_REPLANS_PER_TASK
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
            return {"text": json.dumps(plan("fix sample"))}
        if "Gemma review" in system or "completion check" in system:
            return {"text": '{"decision":"PASS","reason":"verified"}'}
        if "final report" in system:
            return {"text": "completed"}
        raise AssertionError("unexpected resumed recovery model call")


class CodingTaskRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db, self.old_runtime = api.db, api.runtime
        api.db = Database(str(Path(self.tmp.name) / "recovery.sqlite"))
        api.db.initialize()
        api.db.create_project("Project", self.tmp.name, time.time(), "project")
        api.db.create_conversation("Conversation", time.time(), "conversation", "project")
        self.client = TestClient(api.app)

    def tearDown(self):
        api.runtime, api.db = self.old_runtime, self.old_db
        self.tmp.cleanup()

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

    @unittest.skip("OBSOLETE_TEST_EXPECTATION: pre-Orchestrator Gemma/guard lifecycle")
    def test_resume_from_replan_limit_starts_one_fresh_bounded_epoch(self):
        task_id = "replan-human-epoch"
        task_plan = plan("fix sample")
        task_plan["phases"] = [task_plan["phases"][0]]
        api.db.create_coding_task(task_id, "conversation", "fix sample", "RESUMABLE", "NONE", task_plan, time.time())
        api.db.update_coding_task(task_id, replan_count=MAX_SUBSTANTIAL_REPLANS_PER_TASK, replan_count_in_epoch=MAX_SUBSTANTIAL_REPLANS_PER_TASK, recovery_epoch=0, recovery_action="REPLAN_CONTINUATION", recovery_reason="REPLAN_LIMIT")

        class Runtime:
            def __init__(self):
                self.model = ResumeEpochModel()
                self.executions = 0

            def execute(self, request, **kwargs):
                self.executions += 1
                task = Task(request)
                task.transition(TaskState.ROUTING)
                task.transition(TaskState.GENERATING)
                task.transition(TaskState.COMPLETED)
                return task, "implemented"

        runtime = Runtime()
        api.runtime = runtime
        response = self.client.post("/api/chat", json={"message": "再開", "conversation_id": "conversation", "project_id": "project"})
        self.assertEqual(200, response.status_code)
        queued = response.json()
        self.assertEqual(task_id, queued["coding_task_id"])
        queued_task = api.db.coding_task(task_id)
        self.assertEqual("QUEUED", queued_task["status"])
        self.assertEqual(1, queued_task["recovery_epoch"])
        self.assertEqual(0, queued_task["replan_count_in_epoch"])

        deadline = time.time() + 2
        while time.time() < deadline and api.db.coding_task(task_id)["status"] != "COMPLETED":
            time.sleep(0.01)
        task = api.db.coding_task(task_id)
        self.assertEqual("COMPLETED", task["status"])
        self.assertEqual(1, runtime.model.replans)
        self.assertEqual(1, task["recovery_epoch"])
        self.assertEqual(1, task["replan_count_in_epoch"])
        self.assertEqual(MAX_SUBSTANTIAL_REPLANS_PER_TASK + 1, task["replan_count"])

    def test_model_failure_is_resumable_and_the_scheduler_keeps_running(self):
        task_id = "model-failure"
        api.db.create_coding_task(task_id, "conversation", "fix sample", "QUEUED", "NONE", plan("fix sample"), time.time())
        api.db.enqueue_coding_task(task_id)

        class Runtime:
            model = object()

            def execute(self, request, **kwargs):
                raise ModelFailure("unavailable", "model connection reset")

        api.runtime = Runtime()
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


if __name__ == "__main__":
    unittest.main()
