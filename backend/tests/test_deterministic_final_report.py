import unittest

from olcr_api.coding_tasks import deterministic_final_report


def report(**overrides):
    value = {
        "phase_id": "p1", "status": "PASS", "implemented": [], "changed_files": [],
        "test_executed": [], "test_pass": [], "test_fail": [],
        "build_executed": "NOT_RUN", "build_pass": "NOT_RUN",
        "errors": [], "blockers": [], "risks": [],
        "typed_execution_summary": {"state": "completed", "error": None, "operations": []},
        "manager_decision": {"decision": "PASS"},
    }
    value.update(overrides)
    return value


class DeterministicFinalReportTests(unittest.TestCase):
    def test_completed_report_preserves_pass_not_run_and_runtime_not_run(self):
        value = report(
            implemented=["Write: PASS; read-back: PASS. Runtime behavior: NOT_RUN."],
            changed_files=["src/math.py"],
            test_executed=["python -m unittest tests.test_math"],
            test_pass=["python -m unittest tests.test_math (exit_code=0)"],
        )
        text = deterministic_final_report({"status": "COMPLETED"}, {"phases": [{"id": "p1", "status": "pass"}]}, [value])
        self.assertIn("Implemented", text)
        self.assertIn("Changed files\n- src/math.py", text)
        self.assertIn("PASS: python -m unittest tests.test_math (exit_code=0)", text)
        self.assertNotIn("UNVERIFIED: python -m unittest tests.test_math", text)
        self.assertIn("Build\n- NOT_RUN", text)
        self.assertIn("Runtime behavior: NOT_RUN", text)
        self.assertNotIn("runtime behavior was verified", text.lower())

    def test_failed_operation_is_not_reported_as_implemented(self):
        value = report(
            status="FAIL", changed_files=["src/failed.py"],
            test_fail=["python -m unittest tests.test_math (exit_code=1)"],
            errors=["patch precondition failed"],
            typed_execution_summary={"state": "failed", "error": "patch precondition failed", "operations": [
                {"tool": "workspace_write", "status": "failed", "output": {"path": "src/failed.py"}},
            ]},
        )
        text = deterministic_final_report({"status": "FAILED", "recovery_action": "RECOVERY_REVIEW", "recovery_reason": "VERIFY"}, {}, [value])
        implemented = text.split("Changed files", 1)[0]
        self.assertNotIn("src/failed.py", implemented)
        self.assertIn("FAIL: python -m unittest tests.test_math (exit_code=1)", text)
        self.assertIn("Unapplied operation: workspace_write (src/failed.py)", text)
        self.assertIn("Recovery: RECOVERY_REVIEW/VERIFY", text)

    def test_resumable_report_exposes_recovery_and_remaining_phase(self):
        text = deterministic_final_report(
            {"status": "RESUMABLE", "recovery_action": "RECOVERY_REVIEW", "recovery_reason": "PAUSED"},
            {"phases": [{"id": "p1", "goal": "done", "status": "pass"}, {"id": "p2", "goal": "continue", "status": "pending"}]},
            [report()],
            handoff={"next_phase_id": "p2", "next_constraints": ["continue safely"], "unresolved": []},
        )
        self.assertIn("Status: RESUMABLE", text)
        self.assertIn("Recovery: RECOVERY_REVIEW/PAUSED", text)
        self.assertIn("Remaining phase: continue", text)
        self.assertIn("Next\n- Constraint: continue safely", text)
        self.assertNotIn("Status: COMPLETED", text)

    def test_waiting_for_user_never_implies_authorization(self):
        text = deterministic_final_report(
            {"status": "WAITING_FOR_USER", "pending_authorization": {
                "requested_scope": ["git commit"], "reason": "scope expansion", "state": "PENDING",
            }}, {}, [report()],
        )
        self.assertIn("Status: WAITING_FOR_USER", text)
        self.assertIn("Authorization required for: git commit", text)
        self.assertIn("Authorize the requested scope to continue", text)
        self.assertNotIn("Authorization granted", text)

    def test_verification_fail_and_not_run_are_distinct(self):
        failed = report(status="FAIL", test_fail=["test command (exit_code=1)"])
        not_run = report(status="NOT_RUN", test_executed=[])
        text = deterministic_final_report({"status": "FAILED"}, {}, [failed, not_run])
        self.assertIn("FAIL: test command (exit_code=1)", text)
        self.assertIn("Phase p1 verification: NOT_RUN", text)
        self.assertNotIn("PASS: test command", text)

    def test_duplicate_evidence_is_deduplicated(self):
        value = report(
            changed_files=["b.py", "a.py", "a.py"],
            risks=["same risk", "same risk"],
            test_pass=["test (exit_code=0)", "test (exit_code=0)"],
            typed_execution_summary={"state": "completed", "operations": [
                {"tool": "workspace_write", "status": "success", "output": {"path": "a.py"}},
                {"tool": "workspace_write", "status": "success", "output": {"path": "a.py"}},
                {"tool": "workspace_write", "status": "success", "output": {"path": "b.py"}},
            ]},
        )
        text = deterministic_final_report({"status": "COMPLETED"}, {}, [value, value])
        self.assertEqual(1, text.count("- a.py"))
        self.assertEqual(1, text.count("- b.py"))
        self.assertEqual(1, text.count("Risk: same risk"))
        self.assertEqual(1, text.count("PASS: test (exit_code=0)"))

    def test_no_changed_files_and_no_risks_are_explicit(self):
        text = deterministic_final_report({"status": "COMPLETED"}, {}, [report()])
        self.assertIn("Changed files\n- None", text)
        self.assertIn("Risks\n- None", text)
        self.assertIn("TODO\n- None", text)

    def test_build_conflict_is_conservative(self):
        text = deterministic_final_report(
            {"status": "COMPLETED"}, {},
            [report(build_executed="PASS", build_pass="PASS"), report(build_executed="NOT_RUN", build_pass="NOT_RUN")],
        )
        self.assertIn("Build\n- UNKNOWN", text)
        self.assertIn("Evidence conflict: build status values", text)

    def test_artifact_and_mcp_appenders_remain_outside_core_formatter(self):
        text = deterministic_final_report({"status": "COMPLETED"}, {}, [report()])
        self.assertNotIn("成果物ファイル:", text)
        self.assertNotIn("MCP telemetry:", text)


if __name__ == "__main__":
    unittest.main()
