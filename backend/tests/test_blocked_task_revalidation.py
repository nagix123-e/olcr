import hashlib
import json
import tempfile
import time
import unittest
import asyncio
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import olcr_api.app as api
from olcr_api.coding_tasks import MAX_BLOCKER_REOPEN_EPOCHS
from olcr_api.db import Database
from olcr_api.models import Route, TaskState


class BlockedTaskRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db = api.db
        api.db = Database(str(Path(self.tmp.name) / "blocked.sqlite"))
        api.db.initialize()
        # Revalidation queues only a fixture task.  Keep the process-wide
        # scheduler from racing the temporary database teardown; scheduler
        # dispatch is covered by the existing managed-runner tests.
        self.scheduler_wake_patch = patch.object(api._coding_scheduler_wake, "set")
        self.scheduler_wake_patch.start()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()
        api.db.create_project("Project", str(self.workspace), time.time(), "project")
        api.db.create_conversation("Conversation", time.time(), "conversation", "project")

    def tearDown(self):
        api.db = self.old_db
        self.scheduler_wake_patch.stop()
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

    def _zero_progress_report(self, attempt, *, epoch=0, failure_class="EXECUTOR_ERROR"):
        return {
            "phase_id": "p1", "attempt": attempt, "plan_revision": 0, "status": "FAIL",
            "changed_files": [], "phase_progress": False, "accepted_mutations": 0,
            "files_written": 0, "execution_attempt_started": True,
            "execution_attempt_completed": True, "recovery_epoch": epoch,
            "attempt_id": f"fixture:{epoch}:{attempt}",
            "typed_execution_summary": {
                "state": "failed", "failure_class": failure_class,
                "worktree_state": "UNCHANGED", "accepted_mutations": 0,
                "files_written": 0, "phase_progress": False,
                "rejected_operations": [{
                    "category": failure_class, "path": "package.json",
                    "reason": "package.json dependency declarations are owned by PACKAGE_INSTALL",
                }],
            },
            "manager_decision": {"decision": "RETRY"},
        }

    def _invalid_write_report(self, attempt=0, *, rollback_result="PASS", changed_files=None):
        operation = {"tool": "operation_application_failure", "status": "failure",
                    "input": {"failure_class": "WRITE_CONTENT_INVALID"},
                    "output": {"failure": "invalid full-file content", "worktree_state": "ROLLED_BACK",
                               "rollback_result": rollback_result,
                               "operation_diagnostic": {"operation_kind": "write", "relative_target_path": "phase prose",
                                                        "validation_stage": "CONTENT_VALIDATION",
                                                        "reason_subtype": "INVALID_FULL_FILE_CONTENT",
                                                        "rollback_result": rollback_result}}}
        return {"phase_id": "p1", "attempt": attempt, "plan_revision": 0, "status": "FAIL",
                "changed_files": list(changed_files or []), "phase_progress": False,
                "accepted_mutations": 0, "files_written": 0, "final_worktree_mutations": 0,
                "execution_attempt_started": True, "execution_attempt_completed": True,
                "typed_execution_summary": {"state": "failed", "failure_class": "WRITE_CONTENT_INVALID",
                    "worktree_state": "ROLLED_BACK", "accepted_mutations": 0, "files_written": 0,
                    "final_worktree_mutations": 0, "phase_progress": False,
                    "operations": [operation], "operation_diagnostics": [operation["output"]["operation_diagnostic"]]},
                "manager_decision": {"decision": "RETRY"}}

    def _preimage_failure_report(self, attempt=0, *, rollback_result="PASS", matching_hashes=True):
        before_hash = "same-preimage-hash"
        diagnostic = {
            "patch_context": "EXPECTED_OLD_FRAGMENT", "patch_old_match_count": 0,
            "executor_pre_apply_hash": before_hash,
            "actual_file_content_hash": before_hash if matching_hashes else "changed-hash",
            "transaction_base_hash": before_hash, "preimage_failure_reason": "STALE_OLD_TEXT",
        }
        operation = {
            "tool": "operation_application_failure", "status": "failure",
            "input": {"failure_class": "PREIMAGE_MISMATCH"},
            "output": {"failure": "stale patch fragment", "worktree_state": "ROLLED_BACK",
                       "rollback_result": rollback_result},
        }
        return {
            "phase_id": "p1", "attempt": attempt, "plan_revision": 0, "status": "FAIL",
            "changed_files": [], "phase_progress": False, "accepted_mutations": 0,
            "files_written": 0, "final_worktree_mutations": 0,
            "execution_attempt_started": True, "execution_attempt_completed": True,
            "typed_execution_summary": {
                "state": "failed", "failure_class": "PREIMAGE_MISMATCH",
                "worktree_state": "ROLLED_BACK", "attempted_mutations": 0,
                "accepted_mutations": 0, "files_written": 0, "final_worktree_mutations": 0,
                "operations": [operation], "preimage_diagnostics": [diagnostic],
            },
            "manager_decision": {"decision": "RETRY"},
        }

    def test_chat_resume_reopens_only_a_fully_rolled_back_invalid_write(self):
        task = self._task("invalid-write-recovery")
        for attempt in (0, 1):
            api.db.add_coding_phase_report(task["id"], "p1", attempt,
                                           self._invalid_write_report(attempt), "PASS", time.time())
        with patch.object(api._coding_scheduler_wake, "set") as wake:
            result = api.chat(api.ChatInput(message="再開して", conversation_id="conversation", project_id="project"))
        reopened = api.db.coding_task(task["id"])
        self.assertEqual("", result["response"])
        self.assertEqual(task["id"], result["coding_task_id"])
        self.assertEqual("QUEUED", reopened["status"])
        self.assertEqual(1, reopened["recovery_epoch"])
        self.assertEqual("p1", reopened["current_phase_id"])
        self.assertEqual("IMPLEMENTATION", reopened["resume_state"]["retry_channel"])
        self.assertTrue(wake.called)

    def test_invalid_write_revalidation_stays_blocked_without_rollback_proof_or_zero_delta(self):
        cases = (("rollback-failed", "FAILED", []),
                 ("changed-path", "PASS", ["outside-scope.py"]))
        for task_id, rollback_result, changed_files in cases:
            with self.subTest(task_id=task_id):
                task = self._task(task_id)
                for attempt in (0, 1):
                    api.db.add_coding_phase_report(task["id"], "p1", attempt,
                        self._invalid_write_report(attempt, rollback_result=rollback_result,
                                                   changed_files=changed_files), "PASS", time.time())
                result = api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
                persisted = api.db.coding_task(task["id"])
                self.assertIn("BLOCKER_REVALIDATION=STILL_BLOCKED", result["response"])
                self.assertEqual("BLOCKED", persisted["status"])
                self.assertEqual(0, persisted["recovery_epoch"])

    def test_preimage_failure_reopens_only_with_rollback_proof_and_uses_explicit_verifier_revision(self):
        task = self._task("preimage-recovery")
        plan = task["plan"]
        phase = plan["phases"][0]
        phase["verify"] = ["python3 -B -m unittest discover -s OLCR_E2E_LONG_R17_20260924 -v passes"]
        phase["verification_command"] = ["python3", "-B", "-m", "unittest", "discover", "-s",
                                          "OLCR_E2E_LONG_R17_20260924", "-v"]
        api.db.update_coding_task(task["id"], plan=plan)
        correction = ("同じtask_id preimage-recoveryを再開。verifierを修正してください: "
                      "python3 -B -m unittest discover -s . -v")
        api.db.add_message("conversation", "user", correction, time.time(), "preimage-revision")
        for attempt in (0, 1):
            api.db.add_coding_phase_report(task["id"], "p1", attempt,
                self._preimage_failure_report(attempt), "PASS", time.time())

        with patch.object(api._coding_scheduler_wake, "set") as wake:
            result = api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
        reopened = api.db.coding_task(task["id"])
        self.assertEqual("", result["response"])
        self.assertEqual("QUEUED", reopened["status"])
        self.assertEqual(1, reopened["recovery_epoch"])
        self.assertEqual("REPLAN_CONTINUATION", reopened["recovery_action"])
        self.assertEqual("USER_REVISION", reopened["recovery_reason"])
        self.assertIsNone(reopened["pending_authorization"])
        self.assertIn("discover -s . -v", reopened["resume_state"]["continuation_user_revision"])
        self.assertTrue(wake.called)

    def test_chat_resume_description_with_word_retry_does_not_enter_completed_repair_route(self):
        task = self._task("preimage-chat-route")
        plan = task["plan"]
        plan["phases"][0]["verify"] = [
            "python3 -B -m unittest discover -s OLCR_E2E_LONG_R17_20260924 -v passes"]
        api.db.update_coding_task(task["id"], plan=plan)
        for attempt in (0, 1):
            api.db.add_coding_phase_report(task["id"], "p1", attempt,
                self._preimage_failure_report(attempt), "PASS", time.time())
        message = ("同じCoding Taskを再開してください。task_id=preimage-chat-route。 "
                   "verifier: python3 -B -m unittest discover -s . -v。 "
                   "Retry skipping successful work remains required.")
        api.db.add_message("conversation", "user", message, time.time(), "explicit-correction")
        with patch.object(api._coding_scheduler_wake, "set") as wake:
            result = api.chat(api.ChatInput(message="再開して", conversation_id="conversation", project_id="project"))
        reopened = api.db.coding_task(task["id"])
        self.assertEqual(task["id"], result["coding_task_id"])
        self.assertEqual("", result["response"])
        self.assertEqual("QUEUED", reopened["status"])
        self.assertEqual("USER_REVISION", reopened["recovery_reason"])
        self.assertIsNone(reopened["pending_authorization"])
        self.assertIn("discover -s . -v", reopened["resume_state"]["continuation_user_revision"])
        self.assertTrue(wake.called)

    def test_legacy_user_revision_is_migrated_without_authorization_gate(self):
        task = self._task("legacy-user-revision")
        revision = ("同じCoding Task task_id=legacy-user-revision のverifier訂正: "
                    "python3 -B -m unittest discover -s . -v")
        api.db.update_coding_task(task["id"], status="RESUMABLE", recovery_action="RECOVERY_REVIEW",
            recovery_reason="MODEL_CALL", replan_count=4, task_total_recovery_model_calls=3,
            pending_authorization={"type": "USER_REVISION", "state": "PENDING",
                                  "user_revision": revision})
        with patch.object(api._coding_scheduler_wake, "set") as wake:
            result = api.chat(api.ChatInput(message="再開して", conversation_id="conversation", project_id="project"))
        resumed = api.db.coding_task(task["id"])
        self.assertEqual(task["id"], result["coding_task_id"])
        self.assertEqual("", result["response"])
        self.assertEqual("QUEUED", resumed["status"])
        self.assertEqual("REPLAN_LIMIT", resumed["recovery_reason"])
        self.assertIsNone(resumed["pending_authorization"])
        self.assertIn("discover -s . -v", resumed["resume_state"]["continuation_user_revision"])
        self.assertEqual(1, resumed["human_recovery_epochs"])
        self.assertTrue(wake.called)

    def test_preimage_failure_remains_blocked_without_complete_rollback_hash_evidence(self):
        for task_id, rollback_result, matching_hashes in (
                ("preimage-rollback-failed", "FAILED", True),
                ("preimage-hash-mismatch", "PASS", False)):
            with self.subTest(task_id=task_id):
                task = self._task(task_id)
                for attempt in (0, 1):
                    api.db.add_coding_phase_report(task["id"], "p1", attempt,
                        self._preimage_failure_report(attempt, rollback_result=rollback_result,
                                                      matching_hashes=matching_hashes), "PASS", time.time())
                result = api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
                persisted = api.db.coding_task(task["id"])
                self.assertIn("BLOCKER_REVALIDATION=STILL_BLOCKED", result["response"])
                self.assertEqual("BLOCKED", persisted["status"])
                self.assertEqual(0, persisted["recovery_epoch"])

    def test_unknown_or_missing_evidence_stays_blocked(self):
        task = self._task()
        api._save_report(task["id"], "p1", 0, self._ownership_report(), "PASS")
        result = api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
        self.assertTrue(result["response"].startswith("BLOCKER_REVALIDATION=INDETERMINATE:"))
        self.assertEqual("BLOCKED", api.db.coding_task(task["id"])["status"])

    def test_verified_zero_test_false_pass_reopens_same_completed_task_once(self):
        task_id = "false-pass-repair"
        paths = ["fixture/__init__.py", "fixture/domain.py", "fixture/scheduler.py",
                 "fixture/test_scheduler.py"]
        plan = self._plan() | {"file_manifest": [{"path": path, "operation": "create"} for path in paths]}
        api.db.create_coding_task(task_id, "conversation", "repair false pass", "COMPLETED", "NONE", plan, time.time())
        operations = [{"tool": "workspace_write", "input": {"path": str(self.workspace / path)},
                       "output": {"bytes": 0}} for path in paths]
        operations.append({"tool": "verification_command", "output": {
            "returncode": 0, "output": "Ran 0 tests in 0.000s\\nOK"}})
        report = {"status": "PASS", "typed_execution_summary": {"operations": operations}}
        api.db.add_coding_phase_report(task_id, "p1", 0, report, "PASS", time.time())
        current = api.db.coding_task(task_id)
        before_scopes = current.get("approved_scopes")
        evidence = api._completed_task_false_pass_evidence(current)
        self.assertEqual(paths, evidence["manifest_paths"])
        with patch.object(api._coding_scheduler_wake, "set") as wake:
            result = api._reopen_completed_task_for_verified_false_pass(
                current, source="TEST_EXPLICIT_GUI_REPAIR")
        reopened = api.db.coding_task(task_id)
        self.assertEqual(task_id, result["coding_task_id"])
        self.assertEqual("QUEUED", reopened["status"])
        self.assertEqual(1, reopened["human_recovery_epochs"])
        self.assertEqual(1, reopened["recovery_epoch"])
        self.assertEqual(before_scopes, reopened.get("approved_scopes"))
        self.assertEqual(1, len(api.db.coding_tasks("conversation")))
        self.assertTrue(wake.called)

    def test_false_pass_reopen_rejects_incomplete_manifest_or_nonzero_writes(self):
        task_id = "false-pass-reject"
        paths = ["fixture/a.py", "fixture/b.py"]
        plan = self._plan() | {"file_manifest": [{"path": path} for path in paths]}
        api.db.create_coding_task(task_id, "conversation", "repair false pass", "COMPLETED", "NONE", plan, time.time())
        operations = [{"tool": "workspace_write", "input": {"path": str(self.workspace / paths[0])},
                       "output": {"bytes": 0}},
                      {"tool": "workspace_write", "input": {"path": str(self.workspace / paths[1])},
                       "output": {"bytes": 12}},
                      {"tool": "verification_command", "output": {
                          "returncode": 0, "output": "Ran 0 tests in 0.000s\\nOK"}}]
        api.db.add_coding_phase_report(task_id, "p1", 0,
            {"status": "PASS", "typed_execution_summary": {"operations": operations}}, "PASS", time.time())
        self.assertIsNone(api._completed_task_false_pass_evidence(api.db.coding_task(task_id)))
        self.assertEqual("COMPLETED", api.db.coding_task(task_id)["status"])

    def test_false_pass_repair_continues_same_epoch_when_old_pass_short_circuits(self):
        task_id = "false-pass-same-epoch"
        paths = ["fixture/__init__.py", "fixture/domain.py", "fixture/scheduler.py",
                 "fixture/test_scheduler.py"]
        plan = self._plan() | {"file_manifest": [{"path": path, "action": "create"} for path in paths]}
        plan["phases"][0]["status"] = "pass"
        api.db.create_coding_task(task_id, "conversation", "repair false pass", "COMPLETED", "NONE", plan, time.time())
        api.db.update_coding_task(task_id, recovery_epoch=2)
        operations = [{"tool": "workspace_write", "input": {"path": str(self.workspace / path)},
                       "output": {"bytes": 0}} for path in paths]
        operations.append({"tool": "verification_command", "output": {
            "returncode": 0, "output": "Ran 0 tests in 0.000s\\nOK"}})
        api.db.add_coding_phase_report(task_id, "p1", 0,
            {"status": "PASS", "recovery_epoch": 2, "typed_execution_summary": {"operations": operations}},
            "PASS", time.time())
        with patch.object(api._coding_scheduler_wake, "set"):
            api._reopen_completed_task_for_verified_false_pass(
                api.db.coding_task(task_id), source="TEST_EXPLICIT_GUI_REPAIR")
            # Model the observed false completion: the authorized epoch had no
            # execution report, but stale phase PASS evidence completed it.
            api.db.update_coding_task(task_id, status="COMPLETED")
            result = api._requeue_authorized_false_pass_epoch(
                api.db.coding_task(task_id), source="TEST_STALE_PHASE_RECONCILIATION")
        continued = api.db.coding_task(task_id)
        self.assertEqual(task_id, result["coding_task_id"])
        self.assertEqual("QUEUED", continued["status"])
        self.assertEqual(3, continued["recovery_epoch"])
        self.assertEqual(1, continued["human_recovery_epochs"])
        self.assertEqual("pending", continued["plan"]["phases"][0]["status"])
        retry_report = {"status": "FAIL", "recovery_epoch": 3, "attempt_id": "fixture:3:p1:0",
                        "execution_attempt_started": True, "execution_attempt_completed": True,
                        "manager_decision": {"decision": "RETRY"},
                        "typed_execution_summary": {"operations": []}}
        api.db.add_coding_phase_report(task_id, "p1", 0, retry_report, "PASS", time.time())
        self.assertFalse(api._phase_passed_in_epoch(
            task_id, continued["plan"]["phases"][0], continued["plan_revision"], 3))
        api.db.update_coding_task(task_id, status="RESUMABLE", queue_order=None, retry_count=1)
        with patch.object(api._coding_scheduler_wake, "set") as retry_wake:
            retry_result = api._requeue_authorized_false_pass_epoch(
                api.db.coding_task(task_id), source="TEST_BOUNDED_IMPLEMENTER_RETRY")
        retried = api.db.coding_task(task_id)
        self.assertEqual(task_id, retry_result["coding_task_id"])
        self.assertEqual("QUEUED", retried["status"])
        self.assertEqual(3, retried["recovery_epoch"])
        self.assertEqual(1, retried["human_recovery_epochs"])
        self.assertEqual(1, retried["retry_count"])
        self.assertTrue(retry_wake.called)
        pass_report = {**retry_report, "attempt_id": "fixture:3:p1:1",
                       "manager_decision": {"decision": "PASS"}}
        api.db.add_coding_phase_report(task_id, "p1", 1, pass_report, "PASS", time.time())
        self.assertTrue(api._phase_passed_in_epoch(
            task_id, retried["plan"]["phases"][0], retried["plan_revision"], 3))
        self.assertEqual(1, len(api.db.coding_tasks("conversation")))

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

    def test_reopened_epoch_ignores_historical_no_progress_before_new_attempt(self):
        task = self._task("epoch-isolation")
        api._save_report(task["id"], "p1", 0, self._ownership_report(package=True), "PASS")
        for attempt in (0, 1):
            api.db.add_coding_phase_report(task["id"], "p1", attempt,
                                           self._zero_progress_report(attempt), "PASS", time.time())
        with patch.object(api._coding_scheduler_wake, "set"):
            result = api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
        reopened = api.db.coding_task(task["id"])
        self.assertEqual("", result["response"])
        self.assertEqual("QUEUED", reopened["status"])
        self.assertEqual(1, reopened["recovery_epoch"])
        self.assertGreaterEqual(reopened["resume_state"]["lifetime_no_progress_count"], 2)
        self.assertEqual(0, reopened["resume_state"]["epoch_no_progress_count"])
        self.assertEqual("DISPATCH_IMPLEMENTATION", reopened["resume_state"]["next_task_action"])
        self.assertFalse(api._repeated_zero_progress_failure(task["id"], 1)[0])
        self.assertEqual([], api._phase_reports_for_epoch(task["id"], "p1", 0, 1))

    def test_epoch_no_progress_requires_two_new_execution_reports(self):
        task = self._task("epoch-attempts")
        api._save_report(task["id"], "p1", 0, self._ownership_report(package=True), "PASS")
        with patch.object(api._coding_scheduler_wake, "set"):
            api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
        first = self._zero_progress_report(0, epoch=1)
        api._save_report(task["id"], "p1", 0, first, "PASS")
        self.assertEqual(1, api._repeated_zero_progress_failure(task["id"], 1)[2])
        self.assertFalse(api._repeated_zero_progress_failure(task["id"], 1)[0])
        api._save_report(task["id"], "p1", 1, self._zero_progress_report(1, epoch=1), "PASS")
        repeated, signature, count = api._repeated_zero_progress_failure(task["id"], 1)
        self.assertTrue(repeated)
        self.assertTrue(signature)
        self.assertEqual(2, count)
        self.assertEqual(2, api.db.coding_task(task["id"])["resume_state"]["epoch_no_progress_count"])

    def test_scheduler_epoch_dispatch_reaches_implementer_before_no_progress(self):
        task = self._task("epoch-dispatch")
        api._save_report(task["id"], "p1", 0, self._ownership_report(package=True), "PASS")
        for attempt in (0, 1):
            api.db.add_coding_phase_report(task["id"], "p1", attempt,
                                           self._zero_progress_report(attempt), "PASS", time.time())
        with patch.object(api._coding_scheduler_wake, "set"):
            api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
        execution = SimpleNamespace(state=TaskState.COMPLETED, error=None, route=Route.IMPLEMENTATION,
                                    tool_executions=[], model_calls=[])
        report = {"phase_id": "p1", "attempt": 0, "plan_revision": 0, "status": "FAIL",
                  "changed_files": [], "phase_progress": False, "accepted_mutations": 0,
                  "files_written": 0, "typed_execution_summary": {
                      "state": "failed", "failure_class": "EXECUTOR_ERROR",
                      "worktree_state": "UNCHANGED", "accepted_mutations": 0,
                      "files_written": 0, "phase_progress": False,
                      "rejected_operations": [],
                  }}
        with patch.object(api.runtime, "execute", return_value=(execution, "")) as execute, \
             patch.object(api, "_report_from_execution", return_value=report), \
             patch.object(api, "validate_phase_report", return_value=[]), \
             patch.object(api, "_review_phase", return_value="BLOCKED"):
            api._run_managed_task(task["id"], str(self.workspace))
        self.assertEqual(1, execute.call_count)
        persisted = api.db.coding_task(task["id"])
        self.assertEqual(1, persisted["recovery_epoch"])
        self.assertEqual("BLOCKED", persisted["status"])
        current_reports = api._phase_reports_for_epoch(task["id"], "p1", 0, 1)
        self.assertEqual(1, len(current_reports))
        self.assertEqual(1, persisted["resume_state"]["epoch_execution_attempts"])

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

    def test_model_timeout_reopens_only_after_repeated_zero_mutation_evidence(self):
        task = self._task("model-timeout-recovery")
        timeout = "dedicated MLX runtime timed out; OLCR_MLX_PROGRESS={\"event\": \"bonsai_generation_progress\", \"generated_tokens\": 3840, \"max_tokens\": 4096}"
        for attempt in (0, 1):
            report = {
                "phase_id": "p1", "attempt": attempt, "plan_revision": 0, "status": "FAIL",
                "errors": [timeout], "changed_files": [], "execution_attempt_started": True,
                "execution_attempt_completed": True, "accepted_mutations": 0, "files_written": 0,
                "typed_execution_summary": {
                    "state": "failed", "failure_class": "UNKNOWN", "error": timeout,
                    "worktree_state": "UNKNOWN", "attempted_mutations": 0,
                    "accepted_mutations": 0, "files_written": 0,
                    "operations": [{"tool": "workspace_list", "status": "success"}],
                },
            }
            api._save_report(task["id"], "p1", attempt, report, "PASS")

        with patch.object(api._coding_scheduler_wake, "set") as wake:
            result = api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")

        reopened = api.db.coding_task(task["id"])
        self.assertEqual("MODEL_CALL_TIMEOUT", reopened["resume_state"]["last_blocker_revalidation"]["blocker_class"])
        self.assertEqual("", result["response"])
        self.assertEqual(task["id"], result["coding_task_id"])
        self.assertEqual("QUEUED", reopened["status"])
        self.assertEqual(1, reopened["recovery_epoch"])
        self.assertEqual("IMPLEMENTATION", reopened["resume_state"]["retry_channel"])
        self.assertTrue(wake.called)

    def test_model_timeout_with_mutation_evidence_stays_blocked(self):
        task = self._task("model-timeout-with-write")
        timeout = "dedicated MLX runtime timed out"
        for attempt in (0, 1):
            report = {
                "phase_id": "p1", "attempt": attempt, "plan_revision": 0, "status": "FAIL",
                "errors": [timeout], "changed_files": ["frontend/app.py"],
                "typed_execution_summary": {
                    "state": "failed", "failure_class": "UNKNOWN", "error": timeout,
                    "attempted_mutations": 0, "accepted_mutations": 0, "files_written": 0,
                    "operations": [{"tool": "workspace_list", "status": "success"}],
                },
            }
            api._save_report(task["id"], "p1", attempt, report, "PASS")
        result = api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
        self.assertIn("BLOCKER_REVALIDATION=STILL_BLOCKED", result["response"])
        self.assertEqual("BLOCKED", api.db.coding_task(task["id"])["status"])

    def test_model_timeout_evidence_survives_later_interrupted_zero_write_attempt(self):
        task = self._task("model-timeout-followed-by-interruption")
        timeout = "dedicated MLX runtime timed out"
        reports = [
            {"errors": [timeout], "typed_error": timeout},
            {"errors": ["OLCR_MLX_PROGRESS bonsai_generation_progress"], "typed_error":
             "OLCR_MLX_PROGRESS bonsai_generation_progress"},
        ]
        for attempt, item in enumerate(reports):
            api._save_report(task["id"], "p1", attempt, {
                "phase_id": "p1", "attempt": attempt, "plan_revision": 0, "status": "FAIL",
                "errors": item["errors"], "changed_files": [], "phase_progress": False,
                "execution_attempt_started": True, "execution_attempt_completed": True,
                "accepted_mutations": 0, "files_written": 0,
                "typed_execution_summary": {
                    "state": "failed", "failure_class": "UNKNOWN", "error": item["typed_error"],
                    "attempted_mutations": 0, "accepted_mutations": 0, "files_written": 0,
                    "operations": [{"tool": "workspace_list", "status": "success"}],
                },
            }, "PASS")

        result = api._revalidate_blocked_task(api.db.coding_task(task["id"]), source="TEST")
        reopened = api.db.coding_task(task["id"])
        self.assertEqual("MODEL_CALL_TIMEOUT", reopened["resume_state"]["last_blocker_revalidation"]["blocker_class"])
        self.assertEqual("QUEUED", reopened["status"])
        self.assertEqual(1, reopened["recovery_epoch"])
        self.assertEqual("", result["response"])

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
