import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from olcr_api.coding_subagent_executor import CodingSubagentExecutor
from olcr_api.coding_subagent_runtime import CodingSubagentCoordinator
from olcr_api.coding_subagents import assess_delegation, pin_subagent_model
from olcr_api.db import Database
from olcr_api.task_workspace import TaskWorkspaces


def plan_fixture():
    return {
        "phases": [
            {"id": "api", "goal": "Implement the backend API", "dependencies": [],
             "execution_mode": "IMPLEMENTATION", "done": ["API works"], "verify": []},
            {"id": "ui", "goal": "Implement the frontend UI", "dependencies": [],
             "execution_mode": "IMPLEMENTATION", "done": ["UI works"], "verify": []},
            {"id": "storage", "goal": "Implement persistent worker records", "dependencies": [],
             "execution_mode": "IMPLEMENTATION", "done": ["Records persist"], "verify": []},
        ],
        "file_manifest": [
            {"path": "backend/api.py", "action": "modify", "owner_phase": "api"},
            {"path": "frontend/src/App.tsx", "action": "modify", "owner_phase": "ui"},
            {"path": "database/agents.sql", "action": "modify", "owner_phase": "storage"},
        ],
    }


CONFIG = {"mode": "split", "selections": {
    "brain": "api:google:gemini-unified", "coder": "api:openai:gpt-coder",
    "short_coding": "mlx:ternary-bonsai", "general_inference": "mlx:ornith",
}}


class CodingSubagentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="olcr-subagent-integration-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.root)], check=True,
                       capture_output=True, text=True)
        for relative, content in (("backend/api.py", "before api\n"),
                                  ("frontend/src/App.tsx", "before ui\n"),
                                  ("database/agents.sql", "before storage\n")):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.db = Database(str(self.base / "db.sqlite"))
        self.db.initialize()
        self.db.create_project("Project", str(self.root), time.time(), "project-1")
        self.db.create_conversation("Conversation", time.time(), "conversation-1", "project-1")
        self.plan = plan_fixture()
        self.task = self.db.create_coding_task(
            "task-1", "conversation-1", "Implement the full feature", "RUNNING", "IMPLEMENTATION",
            self.plan, time.time())
        self.db.update_coding_task("task-1", plan_revision=4)
        self.workspaces = TaskWorkspaces(self.db)
        self.workspaces.preference("project-1", "direct")
        self.workspaces.reserve("task-1")
        self.workspaces.prepare("task-1")
        self.calls = []

        def generate(messages, model, **kwargs):
            self.calls.append((model, kwargs))
            context = json.loads(messages[-1]["content"])
            file = context["assigned_files"][0]
            content = "after " + context["phase_id"] + "\n"
            mutation = {"path": file["path"], "operation": "modify",
                        "expected_sha256": file["sha256"], "expected_mode": file["mode"],
                        "content": content}
            return {"text": json.dumps({"summary": "Implemented the assigned workstream",
                                         "findings": [], "proposed_mutations": [mutation]}),
                    "prompt_tokens": 50, "completion_tokens": 20, "latency_ms": 10}

        self.executor = CodingSubagentExecutor(generate, worker_timeout_seconds=2)
        self.coordinator = CodingSubagentCoordinator(self.db, self.workspaces, self.executor)
        self.binding = pin_subagent_model("role:long_implementation", CONFIG)

    def test_heavy_task_waits_for_separate_approval_and_never_treats_plan_approval_as_it(self):
        outcome = self.coordinator.coordinate(
            self.db.coding_task("task-1"), self.plan["phases"][0], plan=self.plan,
            plan_revision=4, effective_model="role:long_implementation",
            brain_configuration=CONFIG, setting_enabled=True, now=10)
        self.assertEqual("AWAITING_USER_APPROVAL", outcome.status)
        self.assertEqual(0, len(self.calls))
        self.assertEqual("AWAITING_USER_APPROVAL", outcome.proposal["status"])

    def test_setting_off_and_small_tasks_create_no_proposals_or_worker_calls(self):
        from olcr_api.coding_subagents import UserDirectiveEvidence
        direct = UserDirectiveEvidence("direct-message", "user", "サブエージェントをもちいて実行して")
        disabled = self.coordinator.coordinate(
            self.db.coding_task("task-1"), self.plan["phases"][0], plan=self.plan,
            plan_revision=4, effective_model="role:long_implementation",
            brain_configuration=CONFIG, setting_enabled=False, direct_evidence=direct)
        self.assertEqual(("SKIPPED", "SETTING_OFF"), (disabled.status, disabled.reason))
        small = plan_fixture()
        small["phases"] = small["phases"][:2]
        small["file_manifest"] = small["file_manifest"][:2]
        result = self.coordinator.coordinate(
            self.db.coding_task("task-1"), small["phases"][0], plan=small,
            plan_revision=4, effective_model="role:long_implementation",
            brain_configuration=CONFIG, setting_enabled=True)
        self.assertEqual("SKIPPED", result.status)
        self.assertEqual([], self.db.coding_subagent_proposals("task-1"))
        self.assertEqual([], self.calls)

    def test_task_specific_approval_runs_pinned_workers_and_applies_scoped_results(self):
        pending = self.coordinator.coordinate(
            self.db.coding_task("task-1"), self.plan["phases"][0], plan=self.plan,
            plan_revision=4, effective_model="role:long_implementation",
            brain_configuration=CONFIG, setting_enabled=True, now=10)
        approved = self.db.approve_coding_subagent_proposal(
            pending.proposal["id"], task_id="task-1", plan_revision=4,
            plan_hash=pending.proposal["plan_hash"], proposal_hash=pending.proposal["proposal_hash"],
            model_binding=pending.proposal["model_binding"], now=20, setting_enabled=True)

        result = self.coordinator.coordinate(
            self.db.coding_task("task-1"), self.plan["phases"][0], plan=self.plan,
            plan_revision=4, effective_model="role:long_implementation",
            brain_configuration=CONFIG, setting_enabled=True, now=30)

        self.assertEqual("SUCCEEDED", result.status)
        self.assertEqual("SUCCEEDED", result.proposal["status"])
        self.assertEqual(2, len(result.results))
        self.assertTrue(all(call[0] == "role:long_implementation" for call in self.calls))
        self.assertTrue(all(call[1]["_fallback_allowed"] is False for call in self.calls))
        self.assertEqual("before api\n", (self.root / "backend/api.py").read_text())
        self.assertEqual("after ui\n", (self.root / "frontend/src/App.tsx").read_text())
        self.assertEqual("after storage\n", (self.root / "database/agents.sql").read_text())
        self.assertEqual("AUTHORIZED", approved["status"])
        self.assertEqual({"SUCCEEDED"}, {item["status"] for item in result.proposal["workers"]})

        next_phase = self.coordinator.coordinate(
            self.db.coding_task("task-1"), self.plan["phases"][1], plan=self.plan,
            plan_revision=4, effective_model="role:long_implementation",
            brain_configuration=CONFIG, setting_enabled=True, completed_phase_ids={"api"},
            now=40)
        self.assertEqual("PLAN_DELEGATION_ALREADY_CONSIDERED", next_phase.reason)
        self.assertEqual(1, len(self.db.coding_subagent_proposals("task-1")))
        self.assertEqual(2, len(self.calls))

    def test_direct_user_message_authorizes_without_a_second_approval(self):
        message_id = "user-directive-1"
        self.db.add_message("conversation-1", "user", "サブエージェントを用いて実行して", 5,
                            message_id, "task-1")
        from olcr_api.coding_subagents import UserDirectiveEvidence
        evidence = UserDirectiveEvidence(message_id, "user", "サブエージェントを用いて実行して")
        outcome = self.coordinator.coordinate(
            self.db.coding_task("task-1"), self.plan["phases"][0], plan=self.plan,
            plan_revision=4, effective_model="role:long_implementation",
            brain_configuration=CONFIG, setting_enabled=True,
            direct_evidence=evidence, now=10)
        self.assertEqual("SUCCEEDED", outcome.status)
        self.assertEqual("USER_DIRECTIVE", outcome.proposal["authorization"]["source"])
        self.assertEqual(message_id, outcome.proposal["authorization"]["source_id"])
        self.assertEqual(2, len(outcome.proposal["workers"]))

    def test_unified_model_is_pinned_for_every_worker(self):
        unified = {"mode": "unified", "selections": {
            **CONFIG["selections"], "brain": "api:google:gemini-unified"}}
        outcome = self.coordinator.coordinate(
            self.db.coding_task("task-1"), self.plan["phases"][0], plan=self.plan,
            plan_revision=4, effective_model="role:long_implementation",
            brain_configuration=unified, setting_enabled=True, now=10)
        self.assertEqual("AWAITING_USER_APPROVAL", outcome.status)
        self.assertEqual("api:google:gemini-unified", outcome.proposal["model_binding"]["model_id"])


if __name__ == "__main__":
    unittest.main()
