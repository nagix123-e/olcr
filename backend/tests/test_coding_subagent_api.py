import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

_import_profile = tempfile.TemporaryDirectory(prefix="olcr-subagent-api-import-")
with patch.dict(os.environ, {"OLCR_DB_PATH": str(Path(_import_profile.name) / "import.db"),
                            "OLCR_ALLOWED_ROOTS": _import_profile.name}):
    from fastapi.testclient import TestClient
    import olcr_api.app as api

from olcr_api.coding_subagents import assess_delegation, pin_subagent_model
from olcr_api.config import CODING_SUBAGENTS_WARNING_VERSION, Settings
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


class CodingSubagentApiTests(unittest.TestCase):
    def setUp(self):
        self.profile = tempfile.TemporaryDirectory(prefix="olcr-subagent-api-")
        self.addCleanup(self.profile.cleanup)
        self.old_db, self.old_settings = api.db, api.settings
        self.db = Database(str(Path(self.profile.name) / "test.db"))
        self.db.initialize()
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO conversations(id,project_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("conversation-1", None, "Test", 1.0, 1.0),
            )
        self.plan = plan_fixture()
        self.task = self.db.create_coding_task(
            "task-1", "conversation-1", "Build the feature", "WAITING_FOR_USER", "NONE",
            self.plan, 1.0)
        self.db.update_coding_task("task-1", plan_revision=4)
        self.settings = Settings(
            db_path=self.db.path, allowed_roots=[self.profile.name],
            coding_subagents_enabled=True,
            coding_subagents_warning_ack_revision=CODING_SUBAGENTS_WARNING_VERSION,
        )
        api.db, api.settings = self.db, self.settings
        self.configuration = brain_configuration()
        self.model_patch = patch.object(api, "_coding_phase_model",
                                        return_value="role:long_implementation")
        self.config_patch = patch.object(api.brain_models, "configuration",
                                         return_value=self.configuration)
        self.model_patch.start()
        self.config_patch.start()
        self.addCleanup(self.model_patch.stop)
        self.addCleanup(self.config_patch.stop)
        self.client = TestClient(api.app)
        self.assessment = assess_delegation(self.plan, task_id="task-1", plan_revision=4,
                                            setting_enabled=True)
        self.binding = asdict(pin_subagent_model("role:long_implementation", self.configuration))
        self.proposal = self.db.create_coding_subagent_proposal(
            "proposal-1", task_id="task-1", phase_id="api", plan_revision=4,
            plan_hash=self.assessment.plan_hash, assessment_hash=self.assessment.assessment_hash,
            proposal_hash="a" * 64, model_binding=self.binding,
            role_ids=["repo_explorer", "implementation_worker", "test_reviewer"],
            workers=[
                {"worker_id": "worker-1", "phase_id": "api", "role_id": "repo_explorer",
                 "prompt_version": "2026-11-v1", "assigned_paths": ["backend/api.py"]},
                {"worker_id": "worker-2", "phase_id": "ui", "role_id": "implementation_worker",
                 "prompt_version": "2026-11-v1", "assigned_paths": ["frontend/src/App.tsx"]},
                {"worker_id": "worker-3", "phase_id": "storage", "role_id": "test_reviewer",
                 "prompt_version": "2026-11-v1", "assigned_paths": ["database/agents.sql"]},
            ], now=10.0, setting_enabled=True,
        )

    def tearDown(self):
        api.db, api.settings = self.old_db, self.old_settings

    def decision(self, action="APPROVE_DELEGATION", **overrides):
        return {"action": action, "plan_revision": 4, "plan_hash": self.assessment.plan_hash,
                "proposal_hash": "a" * 64, **overrides}

    def post_decision(self, body=None):
        return self.client.post(
            "/api/coding-tasks/task-1/subagent-proposals/proposal-1/decision",
            json=body or self.decision())

    def test_approval_is_a_separate_one_shot_action(self):
        response = self.post_decision()
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("AUTHORIZED", response.json()["status"])
        self.assertNotIn("authorization", response.json())
        self.assertEqual(409, self.post_decision().status_code)

    def test_plan_approval_action_cannot_authorize_workers(self):
        response = self.post_decision(self.decision("APPROVE_PLAN"))
        self.assertEqual(422, response.status_code)
        self.assertEqual("AWAITING_USER_APPROVAL",
                         self.db.get_coding_subagent_proposal("proposal-1", "task-1")["status"])

    def test_decline_needs_no_model_call_and_cannot_be_replayed(self):
        with patch.object(api, "_coding_phase_model", side_effect=AssertionError("model lookup")):
            response = self.post_decision(self.decision("DECLINE_DELEGATION"))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("DECLINED", response.json()["status"])
        self.assertEqual(409, self.post_decision(self.decision("DECLINE_DELEGATION")).status_code)

    def test_current_model_change_invalidates_approval(self):
        changed = {**self.configuration,
                   "selections": {**self.configuration["selections"], "coder": "api:openai:other"}}
        with patch.object(api.brain_models, "configuration", return_value=changed):
            response = self.post_decision()
        self.assertEqual(409, response.status_code)
        self.assertEqual("CODING_SUBAGENT_MODEL_CHANGED", response.json()["detail"])
        self.assertEqual("AWAITING_USER_APPROVAL",
                         self.db.get_coding_subagent_proposal("proposal-1", "task-1")["status"])

    def test_stale_plan_cannot_be_approved(self):
        self.db.update_coding_task("task-1", plan_revision=5)
        response = self.post_decision()
        self.assertEqual(409, response.status_code)
        self.assertEqual("INTERRUPTED",
                         self.db.get_coding_subagent_proposal("proposal-1", "task-1")["status"])

    def test_feature_off_blocks_decisions(self):
        api.settings = self.settings.with_overrides({"coding_subagents_enabled": False})
        response = self.post_decision()
        self.assertEqual(409, response.status_code)
        self.assertEqual("CODING_SUBAGENTS_DISABLED", response.json()["detail"])

    def test_task_status_exposes_bounded_worker_progress_only(self):
        response = self.client.get("/api/coding-tasks/task-1")
        self.assertEqual(200, response.status_code, response.text)
        proposal = response.json()["subagent_proposals"][0]
        self.assertEqual("AWAITING_USER_APPROVAL", proposal["status"])
        self.assertNotIn("authorization", proposal)
        self.assertEqual(3, len(proposal["workers"]))


if __name__ == "__main__":
    unittest.main()
