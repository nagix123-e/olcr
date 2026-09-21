import hashlib
import json
import tempfile
import time
import unittest
import asyncio
from pathlib import Path
from unittest.mock import patch

import olcr_api.app as api
from olcr_api.coding_tasks import MAX_BLOCKER_REOPEN_EPOCHS
from olcr_api.db import Database


class BlockedTaskRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db = api.db
        api.db = Database(str(Path(self.tmp.name) / "blocked.sqlite"))
        api.db.initialize()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()
        api.db.create_project("Project", str(self.workspace), time.time(), "project")
        api.db.create_conversation("Conversation", time.time(), "conversation", "project")

    def tearDown(self):
        api.db = self.old_db
        self.tmp.cleanup()

    def _plan(self):
        record = {"package": "react", "version_range": None, "dependency_kind": "dependencies", "phase_id": "p1"}
        return {
            "schema_version": 1,
            "original_goal": "implement sample",
            "scope": {"allowed": ["frontend"], "forbidden": []},
            "phases": [{"id": "p1", "goal": "implement sample", "status": "pending", "done": ["file exists"],
                         "verify": [], "dependencies": [], "execution_mode": "IMPLEMENTATION",
                         "requires_dependency_installation": True, "dependency_requirements": [record]}],
            "max_retries_per_phase": 2,
            "requires_user_approval": False,
            "implementation_plan_artifact": {
                "target_root": str(self.workspace),
                "manifest_coverage": {"dependency_requirement_records": [record]},
            },
        }

    def _task(self, task_id="blocked"):
        task = api.db.create_coding_task(task_id, "conversation", "implement sample", "BLOCKED", "NONE", self._plan(), time.time())
        api.db.update_coding_task(task_id, current_phase_id="p1", recovery_reason="NO_PROGRESS")
        return api.db.coding_task(task_id)

    def _ownership_report(self, *, package=False):
        typed = {"state": "failed", "failure_class": "EXECUTOR_ERROR",
                 "rejected_operations": [{"category": "EXECUTOR_ERROR", "path": "package.json",
                                           "reason": "package.json dependency declarations are owned by PACKAGE_INSTALL"}],
                 "operations": []}
        if package:
            package_json = self.workspace / "package.json"
            lockfile = self.workspace / "package-lock.json"
            package_json.write_text(json.dumps({"dependencies": {"react": "^19.0.0"}}), encoding="utf-8")
            lockfile.write_text('{"lockfileVersion":3}', encoding="utf-8")
            typed["operations"] = [{"tool": "package_install", "status": "success", "output": {
                "package_manager": "npm", "packages_authorized": ["react"],
                "workspace_root": str(self.workspace), "process_started": True, "exit_code": 0,
                "package_json_after": {"exists": True, "sha256": hashlib.sha256(package_json.read_bytes()).hexdigest()},
                "lockfile_after": {"package-lock.json": {"exists": True, "sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest()}},
            }}, {"tool": "workspace_refresh", "status": "success", "output": {"node_modules_exists": False}}]
            typed["package_install_evidence"] = [typed["operations"][0]["output"] | {"status": "PASS"}]
        return {"phase_id": "p1", "attempt": 0, "plan_revision": 0,
                "typed_execution_summary": typed, "manager_decision": {"decision": "RETRY"}}

    def test_unknown_or_missing_evidence_stays_blocked(self):
        task = self._task()
        api._save_report(task["id"], "p1", 0, self._ownership_report(), "PASS")
        result = api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
        self.assertTrue(result["response"].startswith("BLOCKER_REVALIDATION=INDETERMINATE:"))
        self.assertEqual("BLOCKED", api.db.coding_task(task["id"])["status"])

    def test_package_ownership_blocker_reopens_same_phase(self):
        task = self._task()
        api._save_report(task["id"], "p1", 0, self._ownership_report(package=True), "PASS")
        result = api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
        reopened = api.db.coding_task(task["id"])
        self.assertEqual("", result["response"])
        self.assertEqual("QUEUED", reopened["status"])
        self.assertEqual(1, reopened["recovery_epoch"])
        self.assertEqual("p1", reopened["current_phase_id"])
        self.assertEqual("IMPLEMENTATION", reopened["resume_state"]["retry_channel"])
        entry = reopened["resume_state"]["package_install_evidence"]["p1"]
        self.assertEqual("PACKAGE_INSTALL", entry["owner"])
        self.assertTrue(entry["dependencies_satisfied"])
        self.assertEqual(0, reopened["replan_count"])

    def test_package_evidence_change_keeps_task_blocked_with_exact_reason(self):
        task = self._task("blocked-package-change")
        api._save_report(task["id"], "p1", 0, self._ownership_report(package=True), "PASS")
        (self.workspace / "package-lock.json").write_text('{"lockfileVersion":3,"packages":{}}', encoding="utf-8")
        result = api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
        self.assertIn("LOCKFILE_HASH_CHANGED:package-lock.json", result["response"])
        self.assertEqual("BLOCKED", api.db.coding_task(task["id"])["status"])

    def test_package_manager_failure_is_still_blocked(self):
        task = self._task("blocked-package-failure")
        report = {"phase_id": "p1", "attempt": 0, "plan_revision": 0,
                  "typed_execution_summary": {"state": "failed", "failure_class": "PACKAGE_INSTALL_FAILURE",
                    "operations": [{"tool": "package_install", "status": "failed",
                                     "output": {"process_started": True, "exit_code": 1}}]}}
        api._save_report(task["id"], "p1", 0, report, "PASS")
        result = api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
        self.assertIn("BLOCKER_REVALIDATION=STILL_BLOCKED", result["response"])
        self.assertIn("package-manager operation did not pass", result["response"])

    def test_reopen_bound_preserves_history(self):
        task = self._task()
        api._save_report(task["id"], "p1", 0, self._ownership_report(package=True), "PASS")
        for _ in range(MAX_BLOCKER_REOPEN_EPOCHS):
            current = api.db.coding_task(task["id"])
            api.db.update_coding_task(task["id"], status="BLOCKED", recovery_reason="NO_PROGRESS")
            result = api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
            self.assertEqual("", result["response"])
        api.db.update_coding_task(task["id"], status="BLOCKED", recovery_reason="NO_PROGRESS")
        result = api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
        final = api.db.coding_task(task["id"])
        self.assertIn("BLOCKER_REVALIDATION=STILL_BLOCKED", result["response"])
        self.assertEqual("BLOCKED", final["status"])
        self.assertEqual(MAX_BLOCKER_REOPEN_EPOCHS, final["recovery_epoch"])
        self.assertGreaterEqual(len(final["resume_state"]["blocker_history"]), MAX_BLOCKER_REOPEN_EPOCHS)

    def test_chat_continue_keeps_blocked_route_owned_by_orchestrator(self):
        task = self._task("blocked-chat")
        api._save_report(task["id"], "p1", 0, self._ownership_report(), "PASS")
        result = api.chat(api.ChatInput(message="続行", conversation_id="conversation", project_id="project"))
        self.assertEqual(task["id"], result["coding_task_id"])
        self.assertTrue(result["response"].startswith("BLOCKER_REVALIDATION=INDETERMINATE:"))
        self.assertEqual("BLOCKED", api.db.coding_task(task["id"])["status"])

    def test_stream_continue_keeps_blocked_response_owned_by_orchestrator(self):
        task = self._task("blocked-stream")
        api._save_report(task["id"], "p1", 0, self._ownership_report(), "PASS")

        async def read(response):
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
            return "".join(chunks)

        with patch.object(api, "chat", return_value={
            "conversation_id": "conversation", "coding_task_id": task["id"],
            "response": "BLOCKER_REVALIDATION=INDETERMINATE: evidence missing",
            "progress_event": {"type": "coding_task_progress", "task_id": task["id"], "status": "BLOCKED"},
        }) as routed:
            body = asyncio.run(read(api.stream_chat(api.ChatInput(
                message="続行", conversation_id="conversation", project_id="project"))))
        routed.assert_called_once()
        self.assertIn('"response_owner": "CODING_ORCHESTRATOR"', body)
        self.assertNotIn("NORMAL_BRAIN", body)


if __name__ == "__main__":
    unittest.main()
