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

from olcr_api.coding_subagents import (assess_delegation, delegation_proposal_hash,
    model_binding_payload, pin_subagent_model, workstreams_for_assignments)
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
        self.binding = model_binding_payload(pin_subagent_model(
            "role:long_implementation", self.configuration))
        self.workers = [
            {"worker_id": "worker-1", "phase_id": "api", "role_id": "repo_explorer",
             "prompt_version": "2026-11-v1", "assigned_paths": ["backend/api.py"]},
            {"worker_id": "worker-2", "phase_id": "ui", "role_id": "implementation_worker",
             "prompt_version": "2026-11-v1", "assigned_paths": ["frontend/src/App.tsx"]},
            {"worker_id": "worker-3", "phase_id": "storage", "role_id": "test_reviewer",
             "prompt_version": "2026-11-v1", "assigned_paths": ["database/agents.sql"]},
        ]
        self.proposal_hash = delegation_proposal_hash(self.assessment,
            pin_subagent_model("role:long_implementation", self.configuration),
            ("repo_explorer", "implementation_worker", "test_reviewer"),
            workstreams_for_assignments(self.assessment, self.workers))
        self.proposal = self.db.create_coding_subagent_proposal(
            "proposal-1", task_id="task-1", phase_id="api", plan_revision=4,
            plan_hash=self.assessment.plan_hash, assessment_hash=self.assessment.assessment_hash,
            proposal_hash=self.proposal_hash, model_binding=self.binding,
            role_ids=["repo_explorer", "implementation_worker", "test_reviewer"],
            workers=self.workers, now=10.0, setting_enabled=True,
        )
        self.db.update_coding_task("task-1", pending_authorization={
            "type": "CODING_SUBAGENT_APPROVAL", "proposal_id": self.proposal["id"],
            "plan_revision": 4, "plan_hash": self.assessment.plan_hash,
            "proposal_hash": self.proposal_hash,
        }, pending_user_confirmation=1)

    def tearDown(self):
        api.db, api.settings = self.old_db, self.old_settings

    def decision(self, action="APPROVE_DELEGATION", **overrides):
        return {"action": action, "plan_revision": 4, "plan_hash": self.assessment.plan_hash,
                "proposal_hash": self.proposal_hash, **overrides}

    def post_decision(self, body=None):
        return self.client.post(
            "/api/coding-tasks/task-1/subagent-proposals/proposal-1/decision",
            json=body or self.decision())

    def test_approval_is_a_separate_one_shot_action(self):
        with patch.object(self.db, "enqueue_coding_task", return_value={"status": "QUEUED"}) as enqueue:
            response = self.post_decision()
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("AUTHORIZED", response.json()["status"])
        self.assertNotIn("authorization", response.json())
        enqueue.assert_called_once_with("task-1")
        resumed = self.db.coding_task("task-1")
        self.assertEqual("RESUMABLE", resumed["status"])
        self.assertIsNone(resumed["pending_authorization"])
        self.assertEqual(409, self.post_decision().status_code)

    def test_plan_approval_action_cannot_authorize_workers(self):
        response = self.post_decision(self.decision("APPROVE_PLAN"))
        self.assertEqual(422, response.status_code)
        self.assertEqual("AWAITING_USER_APPROVAL",
                         self.db.get_coding_subagent_proposal("proposal-1", "task-1")["status"])

    def test_decline_needs_no_model_call_and_cannot_be_replayed(self):
        with patch.object(api, "_coding_phase_model", side_effect=AssertionError("model lookup")), \
                patch.object(self.db, "enqueue_coding_task", return_value={"status": "QUEUED"}) as enqueue:
            response = self.post_decision(self.decision("DECLINE_DELEGATION"))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("DECLINED", response.json()["status"])
        enqueue.assert_called_once_with("task-1")
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

    def test_configured_fallback_change_invalidates_approval(self):
        changed = {**self.configuration, "fallback_models": ["api:google:backup-coder"]}
        with patch.object(api.brain_models, "configuration", return_value=changed):
            response = self.post_decision()
        self.assertEqual(409, response.status_code)
        self.assertEqual("CODING_SUBAGENT_MODEL_CHANGED", response.json()["detail"])

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

    def test_phase_context_includes_only_that_phases_host_validated_worker_summary(self):
        with self.db.connect() as connection:
            connection.execute("""UPDATE coding_subagent_runs SET status='SUCCEEDED',
                safe_summary='Prepared the UI implementation'
                WHERE proposal_id='proposal-1' AND phase_id='ui'""")
        context = api._coding_subagent_phase_context("task-1", "ui", 4)
        self.assertIn("Prepared the UI implementation", context)
        self.assertIn("frontend/src/App.tsx", context)
        self.assertNotIn("backend/api.py", context)

    def test_worker_runtime_adapter_forwards_exact_selected_model_without_fallback(self):
        with patch.object(api.runtime.model, "generate", return_value={"text": "provider fake"}) as generate:
            result = api._generate_coding_subagent([], "role:long_implementation",
                stream=False, _fallback_allowed=False,
                _coding_subagent_binding=self.binding, _coding_subagent_task_id="task-1")
        self.assertEqual({"text": "provider fake"}, result)
        args, kwargs = generate.call_args
        self.assertEqual("role:long_implementation", args[1])
        self.assertFalse(kwargs["_fallback_allowed"])
        self.assertNotIn("_coding_subagent_binding", kwargs)
        self.assertNotIn("_coding_subagent_task_id", kwargs)

    def test_worker_runtime_adapter_restores_only_approval_snapshot_fallbacks(self):
        from olcr_api.brain_models import active_configuration
        fallback_binding = {**self.binding, "fallback_models": ["api:google:backup-coder"],
                            "fallback_policy": "CONFIGURED"}
        with patch.object(api.runtime.model, "generate",
                side_effect=lambda *args, **kwargs: active_configuration()) as generate:
            active = api._generate_coding_subagent([], "role:long_implementation",
                stream=False, _fallback_allowed=True, _coding_subagent_binding=fallback_binding,
                _coding_subagent_task_id="task-1")
        self.assertEqual(["api:google:backup-coder"], active["fallback_models"])
        self.assertTrue(generate.call_args.kwargs["_fallback_allowed"])

    def test_worker_runtime_adapter_rejects_inconsistent_fallback_snapshot(self):
        fallback_binding = {**self.binding, "fallback_models": ["api:google:backup-coder"],
                            "fallback_policy": "NONE"}
        with patch.object(api.runtime.model, "generate") as generate:
            with self.assertRaisesRegex(ValueError, "FALLBACK_BINDING_INVALID"):
                api._generate_coding_subagent([], "role:long_implementation",
                    stream=False, _coding_subagent_binding=fallback_binding,
                    _coding_subagent_task_id="task-1")
        generate.assert_not_called()

    def test_archiving_a_running_task_drains_subagent_mutations_after_cancellation(self):
        self.db.update_coding_task("task-1", status="RUNNING")
        order = []
        cancel = self.db.cancel_coding_subagents_for_task

        def record_cancel(*args, **kwargs):
            order.append("cancel")
            return cancel(*args, **kwargs)

        with patch.object(self.db, "cancel_coding_subagents_for_task", side_effect=record_cancel), \
                patch.object(api, "_drain_coding_subagent_mutations",
                             side_effect=lambda task_id: order.append("drain")):
            response = self.client.patch("/api/coding-tasks/task-1", json={"archived": True})
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()["pause_requested"])
        self.assertEqual(["cancel", "drain"], order)

    def test_pause_cancels_the_parent_generation_before_child_registration_can_race(self):
        order = []
        with patch.object(api, "cancel_coding_generation",
                          side_effect=lambda task_id: order.append("parent")), \
                patch.object(api.coding_subagent_executor, "cancel_task",
                             side_effect=lambda task_id: order.append("workers")), \
                patch.object(self.db, "cancel_coding_subagents_for_task",
                             side_effect=lambda *args, **kwargs: order.append("database")), \
                patch.object(api, "_drain_coding_subagent_mutations",
                             side_effect=lambda task_id: order.append("drain")):
            response = self.client.patch("/api/coding-tasks/task-1", json={"pause_requested": True})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(["parent", "workers", "database", "drain"], order)


if __name__ == "__main__":
    unittest.main()
