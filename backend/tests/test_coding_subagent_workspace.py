import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from olcr_api.db import Database
from olcr_api.task_workspace import TaskWorkspaces, WorkspaceError
from olcr_api.coding_subagents import plan_content_hash


MODEL_BINDING = {
    "requested_model": "role:long_implementation", "model_id": "api:openai:gpt-coder",
    "provider_id": "openai", "runtime_id": "brain-provider:openai", "model_revision": None,
    "brain_mode": "split", "selected_slot": "coder",
    "implementation_role": "long_implementation", "fallback_models": [], "fallback_policy": "NONE",
}


class CodingSubagentWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="olcr-subagent-workspace-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.root)], check=True,
                       capture_output=True, text=True)
        self.path = self.root / "backend" / "module.py"
        self.path.parent.mkdir()
        self.path.write_text("old\n", encoding="utf-8")
        self.db = Database(str(self.base / "db.sqlite"))
        self.db.initialize()
        self.db.create_project("Project", str(self.root), time.time(), "project-1")
        self.db.create_conversation("Conversation", time.time(), "conversation-1", "project-1")
        self.plan = {
            "phases": [{"id": "phase-1", "goal": "Implement module", "dependencies": [],
                        "execution_mode": "IMPLEMENTATION"}],
            "file_manifest": [{"path": "backend/module.py", "action": "modify",
                                "owner_phase": "phase-1"}],
        }
        self.db.create_coding_task("task-1", "conversation-1", "Implement module", "RUNNING",
                                   "IMPLEMENTATION", self.plan, time.time())
        self.db.update_coding_task("task-1", plan_revision=4)
        self.workspaces = TaskWorkspaces(self.db)
        self.workspaces.preference("project-1", "direct")
        self.workspaces.reserve("task-1")
        self.workspaces.prepare("task-1")
        self._insert_running_worker()

    def _insert_running_worker(self, *, role="implementation_worker", assigned=None):
        now = time.time()
        paths = assigned or ["backend/module.py"]
        with self.db.connect() as connection:
            connection.execute("""INSERT INTO coding_subagent_proposals(
                id,task_id,phase_id,plan_revision,plan_hash,assessment_hash,proposal_hash,
                model_binding_json,role_ids_json,status,authorization_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'RUNNING',?,?,?)""", (
                    "proposal-1", "task-1", "phase-1", 4, plan_content_hash(self.plan), "b" * 64, "c" * 64,
                    json.dumps(MODEL_BINDING, sort_keys=True), json.dumps([role]),
                    json.dumps({"source": "TASK_APPROVAL"}), now, now))
            connection.execute("""INSERT INTO coding_subagent_runs(
                id,proposal_id,task_id,phase_id,plan_revision,role_id,prompt_version,
                model_binding_json,assigned_paths_json,status,usage_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'RUNNING','{}',?,?)""", (
                    "worker-1", "proposal-1", "task-1", "phase-1", 4, role,
                    "2026-11-v1", json.dumps(MODEL_BINDING, sort_keys=True),
                    json.dumps(paths), now, now))

    def test_host_reads_only_phase_owned_non_sensitive_files_with_preimages(self):
        values = self.workspaces.read_coding_subagent_files(
            "task-1", "phase-1", 4, ["backend/module.py"])
        self.assertEqual(1, len(values))
        self.assertEqual("old\n", values[0]["content"])
        self.assertEqual(hashlib.sha256(b"old\n").hexdigest(), values[0]["sha256"])
        self.assertEqual(0o644, values[0]["mode"])

    def test_worker_mutation_is_serially_applied_and_checkpointed(self):
        before = hashlib.sha256(b"old\n").hexdigest()
        result = self.workspaces.apply_coding_subagent_mutations(
            "task-1", "worker-1", [{"path": "backend/module.py", "operation": "modify",
                "expected_sha256": before, "expected_mode": 0o644, "content": "new\n"}], now=20.0)
        self.assertEqual(["backend/module.py"], result["paths"])
        self.assertEqual("new\n", self.path.read_text(encoding="utf-8"))
        self.db.update_coding_task("task-1", status="RESUMABLE")
        changes = self.workspaces.changes("task-1")
        self.assertEqual(["backend/module.py"], [item["path"] for item in changes["files"]])
        self.assertTrue(changes["files"][0]["revertible"])
        with self.db.connect() as connection:
            event = connection.execute(
                "SELECT data_json FROM coding_task_events WHERE kind='CODING_SUBAGENT_MUTATIONS_APPLIED'").fetchone()
        self.assertIsNotNone(event)
        self.assertNotIn("new\\n", event[0])

    def test_worker_cannot_escape_its_assigned_file_scope(self):
        with self.assertRaisesRegex(WorkspaceError, "SCOPE_REJECTED"):
            self.workspaces.apply_coding_subagent_mutations(
                "task-1", "worker-1", [{"path": "backend/other.py", "operation": "create",
                    "expected_sha256": None, "expected_mode": None, "content": "new\n"}], now=20.0)
        self.assertFalse((self.root / "backend" / "other.py").exists())

    def test_paused_parent_rejects_a_worker_mutation_at_the_apply_boundary(self):
        self.db.update_coding_task("task-1", pause_requested=True)
        with self.assertRaisesRegex(WorkspaceError, "PARENT_PAUSED"):
            self.workspaces.apply_coding_subagent_mutations(
                "task-1", "worker-1", [{"path": "backend/module.py", "operation": "modify",
                    "expected_sha256": hashlib.sha256(b"old\n").hexdigest(),
                    "expected_mode": 0o644, "content": "agent edit\n"}], now=20.0)
        self.assertEqual("old\n", self.path.read_text(encoding="utf-8"))

    def test_pause_boundary_waits_for_any_in_flight_file_apply(self):
        root = self.workspaces.root_for("task-1")
        started = threading.Event()
        finished = threading.Event()

        def wait_at_boundary():
            started.set()
            self.workspaces.wait_for_coding_subagent_mutations("task-1")
            finished.set()

        with self.workspaces.lock(root):
            thread = threading.Thread(target=wait_at_boundary)
            thread.start()
            self.assertTrue(started.wait(timeout=1))
            self.assertFalse(finished.wait(timeout=0.05))
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(finished.is_set())

    def test_stale_preimage_and_changed_plan_are_rejected(self):
        self.path.write_text("external edit\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceError, "PREIMAGE_CHANGED"):
            self.workspaces.apply_coding_subagent_mutations(
                "task-1", "worker-1", [{"path": "backend/module.py", "operation": "modify",
                    "expected_sha256": hashlib.sha256(b"old\n").hexdigest(),
                    "expected_mode": 0o644, "content": "agent edit\n"}], now=20.0)
        self.path.write_text("old\n", encoding="utf-8")
        self.db.update_coding_task("task-1", plan_revision=5)
        with self.assertRaisesRegex(WorkspaceError, "STALE_PLAN"):
            self.workspaces.apply_coding_subagent_mutations(
                "task-1", "worker-1", [{"path": "backend/module.py", "operation": "modify",
                    "expected_sha256": hashlib.sha256(b"old\n").hexdigest(),
                    "expected_mode": 0o644, "content": "agent edit\n"}], now=21.0)

    def test_read_only_role_and_sensitive_paths_are_rejected(self):
        path = self.root / ".env"
        path.write_text("TOKEN=secret", encoding="utf-8")
        self.plan["file_manifest"].append({"path": ".env", "action": "modify", "owner_phase": "phase-1"})
        self.db.update_coding_task("task-1", plan=self.plan)
        with self.assertRaisesRegex(WorkspaceError, "SCOPE_REJECTED"):
            self.workspaces.read_coding_subagent_files("task-1", "phase-1", 4, [".env"])
        with self.db.connect() as connection:
            connection.execute("UPDATE coding_subagent_runs SET role_id='repo_explorer',status='RUNNING' WHERE id='worker-1'")
            connection.execute("UPDATE coding_subagent_proposals SET role_ids_json='[\"repo_explorer\"]' WHERE id='proposal-1'")
            connection.execute("UPDATE coding_subagent_proposals SET status='RUNNING',plan_hash=? WHERE id='proposal-1'",
                               (plan_content_hash(self.plan),))
        with self.assertRaisesRegex(WorkspaceError, "ROLE_READ_ONLY"):
            self.workspaces.apply_coding_subagent_mutations(
                "task-1", "worker-1", [{"path": "backend/module.py", "operation": "modify",
                    "expected_sha256": hashlib.sha256(b"old\n").hexdigest(),
                    "expected_mode": 0o644, "content": "agent edit\n"}], now=22.0)

    def test_overlapping_or_unsupported_mutations_are_rejected_atomically(self):
        before = hashlib.sha256(b"old\n").hexdigest()
        with self.assertRaisesRegex(WorkspaceError, "INVALID_MUTATION"):
            self.workspaces.apply_coding_subagent_mutations(
                "task-1", "worker-1", [
                    {"path": "backend/module.py", "operation": "modify", "expected_sha256": before,
                     "expected_mode": 0o644, "content": "new\n"},
                    {"path": "backend/module.py", "operation": "modify", "expected_sha256": before,
                     "expected_mode": 0o644, "content": "second\n"},
                ], now=23.0)
        self.assertEqual("old\n", self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
