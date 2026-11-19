import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from olcr_api.coding_subagents import assess_delegation, pin_subagent_model
from olcr_api.db import Database


def plan_fixture():
    return {
        "phases": [
            {"id": "api", "goal": "Implement the backend API", "dependencies": [],
             "execution_mode": "IMPLEMENTATION", "status": "pending"},
            {"id": "ui", "goal": "Implement the frontend UI", "dependencies": [],
             "execution_mode": "IMPLEMENTATION", "status": "pending"},
            {"id": "storage", "goal": "Implement persistent worker records", "dependencies": [],
             "execution_mode": "IMPLEMENTATION", "status": "pending"},
        ],
        "file_manifest": [
            {"path": "backend/api.py", "owner_phase": "api"},
            {"path": "frontend/src/App.tsx", "owner_phase": "ui"},
            {"path": "database/agents.sql", "owner_phase": "storage"},
        ],
    }


def brain_configuration():
    return {"mode": "split", "selections": {
        "brain": "api:google:gemini-unified", "coder": "api:openai:gpt-coder",
        "short_coding": "mlx:ternary-bonsai", "general_inference": "mlx:ornith",
    }}


class CodingSubagentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.profile = tempfile.TemporaryDirectory(prefix="olcr-coding-subagent-workflow-")
        self.addCleanup(self.profile.cleanup)
        self.db = Database(str(Path(self.profile.name) / "workflow.db"))
        self.db.initialize()
        self.plan = plan_fixture()
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO conversations(id,project_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("conversation-1", None, "Test", 1.0, 1.0),
            )
        self.task = self.db.create_coding_task("task-1", "conversation-1", "Build the feature",
                                               "WAITING_FOR_USER", "NONE", self.plan, 1.0)
        self.task = self.db.update_coding_task("task-1", plan_revision=4)
        self.assessment = assess_delegation(self.plan, task_id="task-1", plan_revision=4,
                                            setting_enabled=True)
        self.binding = pin_subagent_model("role:long_implementation", brain_configuration())
        self.proposal_id = "proposal-1"

    def create_proposal(self, *, proposal_id=None, authorization=None, binding=None):
        return self.db.create_coding_subagent_proposal(
            proposal_id or self.proposal_id,
            task_id="task-1",
            phase_id="api",
            plan_revision=4,
            plan_hash=self.assessment.plan_hash,
            assessment_hash=self.assessment.assessment_hash,
            proposal_hash="a" * 64,
            model_binding=(binding if isinstance(binding, dict) else asdict(binding or self.binding)),
            role_ids=["repo_explorer", "implementation_worker", "test_reviewer"],
            workers=[
                {"worker_id": "worker-1", "phase_id": "api", "role_id": "repo_explorer",
                 "prompt_version": "2026-11-v1", "assigned_paths": ["backend/api.py"]},
                {"worker_id": "worker-2", "phase_id": "ui", "role_id": "implementation_worker",
                 "prompt_version": "2026-11-v1", "assigned_paths": ["frontend/src/App.tsx"]},
                {"worker_id": "worker-3", "phase_id": "storage", "role_id": "test_reviewer",
                 "prompt_version": "2026-11-v1", "assigned_paths": ["database/agents.sql"]},
            ],
            now=10.0,
            setting_enabled=True,
            authorization=authorization,
        )

    def approve(self, proposal=None, *, binding=None):
        return self.db.approve_coding_subagent_proposal(
            proposal["id"] if proposal else self.proposal_id,
            task_id="task-1",
            plan_revision=4,
            plan_hash=self.assessment.plan_hash,
            proposal_hash="a" * 64,
            model_binding=asdict(binding or self.binding),
            now=20.0,
            setting_enabled=True,
        )

    def test_persists_approval_as_a_distinct_state_and_enforces_transitions(self):
        proposal = self.create_proposal()
        self.assertEqual("AWAITING_USER_APPROVAL", proposal["status"])
        self.assertEqual(3, len(proposal["workers"]))
        self.assertEqual({"AWAITING_USER_APPROVAL"}, {row["status"] for row in proposal["workers"]})

        approved = self.approve(proposal)
        self.assertEqual("AUTHORIZED", approved["status"])
        self.assertEqual("TASK_APPROVAL", approved["authorization"]["source"])
        with self.assertRaises(ValueError):
            self.approve(proposal)

        queued = self.db.transition_coding_subagent_proposal(
            proposal["id"], expected_status="AUTHORIZED", new_status="QUEUED", now=30.0)
        self.assertEqual("QUEUED", queued["status"])
        running = self.db.transition_coding_subagent_proposal(
            proposal["id"], expected_status="QUEUED", new_status="RUNNING", now=40.0)
        self.assertEqual("RUNNING", running["status"])
        completed = self.db.transition_coding_subagent_run(
            "worker-1", expected_status="AUTHORIZED", new_status="QUEUED", now=30.0)
        completed = self.db.transition_coding_subagent_run(
            "worker-1", expected_status="QUEUED", new_status="RUNNING", now=40.0)
        completed = self.db.transition_coding_subagent_run(
            "worker-1", expected_status="RUNNING", new_status="SUCCEEDED", now=50.0,
            summary="Reviewed backend scope", usage={"input_tokens": 100, "output_tokens": 20},
        )
        self.assertEqual("SUCCEEDED", completed["status"])
        with self.assertRaises(ValueError):
            self.db.transition_coding_subagent_proposal(
                proposal["id"], expected_status="RUNNING", new_status="AUTHORIZED", now=60.0)

    def test_duplicate_proposal_creation_is_idempotent(self):
        first = self.create_proposal()
        duplicate = self.create_proposal(proposal_id="proposal-duplicate")
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual([row["id"] for row in first["workers"]],
                         [row["id"] for row in duplicate["workers"]])
        self.assertEqual(3, len(self.db.coding_subagent_runs("task-1")))

    def test_stale_plan_revision_and_hash_cannot_be_approved(self):
        proposal = self.create_proposal()
        self.db.update_coding_task("task-1", plan_revision=5)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.approve(proposal)
        current = self.db.get_coding_subagent_proposal(proposal["id"], "task-1")
        self.assertEqual("INTERRUPTED", current["status"])
        self.assertEqual({"INTERRUPTED"}, {row["status"] for row in current["workers"]})

    def test_plan_content_change_invalidates_approval_even_without_revision_bump(self):
        proposal = self.create_proposal()
        changed_plan = plan_fixture()
        changed_plan["phases"][0]["goal"] = "Implement a different backend API"
        self.db.update_coding_task("task-1", plan=changed_plan)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.approve(proposal)

    def test_parent_restart_interrupts_unfinished_runs_without_replaying_them(self):
        proposal = self.create_proposal()
        approved = self.approve(proposal)
        self.db.transition_coding_subagent_proposal(
            approved["id"], expected_status="AUTHORIZED", new_status="QUEUED", now=30.0)
        self.db.transition_coding_subagent_proposal(
            approved["id"], expected_status="QUEUED", new_status="RUNNING", now=40.0)
        self.db.transition_coding_subagent_run(
            "worker-1", expected_status="AUTHORIZED", new_status="QUEUED", now=30.0)
        self.db.transition_coding_subagent_run(
            "worker-1", expected_status="QUEUED", new_status="RUNNING", now=40.0)

        recovered = self.db.interrupt_unfinished_coding_subagents(now=60.0)
        current = self.db.get_coding_subagent_proposal(approved["id"], "task-1")
        worker = next(row for row in current["workers"] if row["id"] == "worker-1")
        self.assertGreaterEqual(recovered["proposals"], 1)
        self.assertEqual("INTERRUPTED", current["status"])
        self.assertEqual("INTERRUPTED", worker["status"])
        self.assertIsNone(worker["safe_summary"])

    def test_declined_proposal_cannot_queue_worker_runs(self):
        proposal = self.create_proposal()
        declined = self.db.decline_coding_subagent_proposal(
            proposal["id"], task_id="task-1", plan_revision=4,
            plan_hash=self.assessment.plan_hash, proposal_hash="a" * 64, now=20.0)
        self.assertEqual("DECLINED", declined["status"])
        self.assertEqual({"CANCELLED"}, {row["status"] for row in declined["workers"]})
        with self.assertRaises(ValueError):
            self.db.transition_coding_subagent_run(
                "worker-1", expected_status="CANCELLED", new_status="QUEUED", now=30.0)

    def test_secret_model_fields_and_unbounded_usage_are_rejected(self):
        model_with_secret = {**asdict(self.binding), "api_key": "secret-do-not-store"}
        with self.assertRaisesRegex(ValueError, "model binding"):
            self.create_proposal(binding=model_with_secret)

        proposal = self.create_proposal()
        self.approve(proposal)
        self.db.transition_coding_subagent_proposal(
            proposal["id"], expected_status="AUTHORIZED", new_status="QUEUED", now=30.0)
        self.db.transition_coding_subagent_run(
            "worker-1", expected_status="AUTHORIZED", new_status="QUEUED", now=30.0)
        with self.assertRaisesRegex(ValueError, "usage"):
            self.db.transition_coding_subagent_run(
                "worker-1", expected_status="QUEUED", new_status="RUNNING", now=40.0,
                usage={"api_key": "secret-do-not-store"},
            )
        with self.db.connect() as connection:
            stored = " ".join(str(row[0]) for row in connection.execute(
                "SELECT model_binding_json FROM coding_subagent_proposals"))
        self.assertNotIn("secret-do-not-store", stored)


if __name__ == "__main__":
    unittest.main()
