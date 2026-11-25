"""Plan validation failures remain actionable and recoverable."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import olcr_api.app as api
from olcr_api.db import Database
from olcr_api.ollama import ModelFailure


class CodingPlanDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.db = Database(str(Path(self.temp.name) / "coding.sqlite"))
        self.db.initialize()
        self.db.create_conversation("plan-diagnostic", time.time(), "plan diagnostic", None)
        self.db.create_coding_task(
            "plan-diagnostic-task", "plan-diagnostic", "Fix the requested issue.",
            "PLANNING", "QWEN_PLANNING", None, time.time(),
        )

    def test_failed_plan_and_repair_keep_bounded_validation_reason(self):
        first=ModelFailure("invalid_response","malformed structured planner response")
        first.response_text="not JSON"
        second=ModelFailure("invalid_response","malformed structured repair response")
        second.response_text="still not JSON"
        with patch.object(api, "db", self.db), patch.object(
            api, "_model_text", side_effect=[first, second]
        ) as model:
            plan, _ = api._generate_plan(
                "plan-diagnostic-task", "Fix the requested issue.", "Plan this task.", "QWEN_PLANNING"
            )

        self.assertIsNone(plan)
        self.assertEqual(2, model.call_count)
        task = self.db.coding_task("plan-diagnostic-task")
        diagnostic = task["resume_state"]["planning_validation_failure"]
        self.assertEqual(2, diagnostic["error_count"])
        self.assertEqual(2, len(diagnostic["errors"]))
        self.assertLessEqual(len(diagnostic["errors"][0]), 400)

    def test_successful_validation_clears_stale_plan_diagnostic(self):
        with patch.object(api, "db", self.db):
            api._store_plan_validation_diagnostic("plan-diagnostic-task", ["invalid phase"])
            api._store_plan_validation_diagnostic("plan-diagnostic-task", None)

        state = self.db.coding_task("plan-diagnostic-task")["resume_state"]
        self.assertNotIn("planning_validation_failure", state)


if __name__ == "__main__":
    unittest.main()
