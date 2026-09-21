import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

import olcr_api.app as api
from olcr_api.db import Database


class ResumeStateHydrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db = api.db
        self.db = Database(str(Path(self.tmp.name) / "resume.sqlite"))
        self.db.initialize()
        api.db = self.db
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()
        (self.workspace / "node_modules").mkdir()
        self.package_json = self.workspace / "package.json"
        self.lockfile = self.workspace / "package-lock.json"
        self.package_json.write_text(json.dumps({"dependencies": {"react": "^19.0.0"}}), encoding="utf-8")
        self.lockfile.write_text("{\"lockfileVersion\": 3}", encoding="utf-8")
        self.task_id = "resume-hydration"
        self.db.create_coding_task(self.task_id, "conversation", "implement", "RUNNING", "QWEN_IMPLEMENTATION", {
            "phases": [{"id": "p1", "status": "pending", "dependencies": []}],
        }, time.time())
        self.db.update_coding_task(self.task_id, current_phase_id="p1")

    def tearDown(self):
        api.db = self.old_db
        self.tmp.cleanup()

    def _records(self):
        return [{"package": "react", "version_range": None, "dependency_kind": "dependencies", "phase_id": "p1"}]

    def _report(self):
        package_hash = hashlib.sha256(self.package_json.read_bytes()).hexdigest()
        lock_hash = hashlib.sha256(self.lockfile.read_bytes()).hexdigest()
        return {
            "phase_id": "p1", "attempt": 0, "plan_revision": 0, "status": "FAIL",
            "typed_execution_summary": {
                "state": "failed", "operations": [{
                    "tool": "package_install", "status": "success",
                    "input": {"packages": ["react"], "workspace_root": str(self.workspace)},
                    "output": {
                        "status": "PASS", "package_manager": "npm", "packages_authorized": ["react"],
                        "workspace_root": str(self.workspace), "process_started": True, "exit_code": 0,
                        "package_json_after": {"exists": True, "sha256": package_hash},
                        "lockfile_after": {"package-lock.json": {"exists": True, "sha256": lock_hash}},
                    },
                }, {
                    "tool": "workspace_refresh", "status": "success",
                    "output": {"node_modules_exists": True},
                }],
            },
        }

    def test_package_evidence_survives_database_reopen_and_restores(self):
        api._save_report(self.task_id, "p1", 0, self._report(), "PASS")
        reopened = Database(self.db.path)
        reopened.initialize()
        api.db = reopened
        task = reopened.coding_task(self.task_id)
        self.assertEqual("PASS", task["resume_state"]["package_install_evidence"]["p1"]["status"])
        ok, evidence, state = api._reconcile_package_install_resume(
            self.task_id, {"id": "p1"}, 0, str(self.workspace), self._records())
        self.assertTrue(ok)
        self.assertTrue(evidence)
        self.assertEqual("PACKAGE_INSTALL", state["owner"])
        self.assertTrue(state["dependencies_satisfied"])

    def test_changed_package_file_invalidates_with_reason(self):
        api._save_report(self.task_id, "p1", 0, self._report(), "PASS")
        self.package_json.write_text('{"dependencies":{"react":"^19.0.0"},"scripts":{"build":"vite build"}}', encoding="utf-8")
        ok, evidence, state = api._reconcile_package_install_resume(
            self.task_id, {"id": "p1"}, 0, str(self.workspace), self._records())
        self.assertFalse(ok)
        self.assertEqual([], evidence)
        self.assertEqual("INVALIDATED", state["status"])
        self.assertEqual("PACKAGE_JSON_HASH_CHANGED", state["resume_evidence_invalidation_reason"])

    def test_removed_lockfile_invalidates_without_greenfield_reset(self):
        api._save_report(self.task_id, "p1", 0, self._report(), "PASS")
        self.lockfile.unlink()
        ok, _, state = api._reconcile_package_install_resume(
            self.task_id, {"id": "p1"}, 0, str(self.workspace), self._records())
        self.assertFalse(ok)
        self.assertEqual("LOCKFILE_MISSING:package-lock.json", state["resume_evidence_invalidation_reason"])
        self.assertEqual("p1", state["phase_id"])
