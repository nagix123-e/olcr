import json
import tempfile
import time
import unittest
from pathlib import Path

import olcr_api.app as api
from olcr_api.coding_tasks import validate_phase_report
from olcr_api.db import Database
from olcr_api.retrieval import DisabledVectorStore, FTSRetriever, FileRetriever, RetrievalRouter
from olcr_api.runtime import Runtime
from olcr_api.config import Settings


def approved_plan(goal):
    return {
        "schema_version": 1,
        "original_goal": goal,
        "scope": {"allowed": ["workspace-a"], "forbidden": []},
        "assumptions": [],
        "phases": [
            {
                "id": "p1",
                "goal": "Create the deterministic P1 probe file.",
                "status": "pending",
                "done": ["runtime_p1.txt exists with OLCR_P1_REAL_RUNTIME"],
                "verify": ["typed filesystem write and production read-back succeed"],
                "dependencies": [],
                "risks": [],
            },
            {
                "id": "p2",
                "goal": "Create the deterministic P2 follow-up file.",
                "status": "pending",
                "done": ["runtime_p2.txt exists with OLCR_P2_FOLLOW_UP"],
                "verify": ["typed filesystem write and production read-back succeed"],
                "dependencies": ["p1"],
                "risks": [],
            },
        ],
        "max_retries_per_phase": 2,
        "requires_user_approval": True,
    }


class ManagedRunnerModel:
    def __init__(self):
        self.implementation_phases = []
        self.gemma_reviews = 0
        self.gemma_completion_checks = 0

    def generate(self, messages, model, stream=False, think=None, format=None):
        system = messages[0]["content"]
        request = messages[-1]["content"]
        if "Gemma review mode" in system:
            self.gemma_reviews += 1
            return {"text": '{"decision":"PASS","reason":"typed write and read-back verified"}', "latency_ms": 0}
        if "Gemma completion check mode" in system:
            self.gemma_completion_checks += 1
            return {"text": '{"decision":"PASS","reason":"all typed phase evidence is complete"}', "latency_ms": 0}
        if "Qwen final report mode" in system:
            return {"text": "Managed runner integration completed.", "latency_ms": 0}

        if "Create the deterministic P1 probe file" in request:
            self.implementation_phases.append("p1")
            path, content = "runtime_p1.txt", "OLCR_P1_REAL_RUNTIME"
        elif "Create the deterministic P2 follow-up file" in request:
            self.implementation_phases.append("p2")
            path, content = "runtime_p2.txt", "OLCR_P2_FOLLOW_UP"
        else:
            raise AssertionError("unexpected model request")
        return {
            "text": json.dumps(
                {
                    "change_required": True,
                    "source_inspected": True,
                    "condition_evaluated": True,
                    "reason_code": "managed_runner_probe",
                    "operations": [{"op": "write", "path": path, "content": content}],
                    "verification": "read the created file",
                }
            ),
            "latency_ms": 0,
        }


class ManagedRunnerRealRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "workspace-a"
        self.workspace.mkdir()
        self.sentinel = self.base / "sentinel-b.txt"
        self.sentinel.write_text("OUTSIDE_SENTINEL_ORIGINAL", encoding="utf-8")

        self.old_db, self.old_runtime = api.db, api.runtime
        api.db = Database(str(self.base / "manager.sqlite"))
        api.db.initialize()
        settings = Settings(
            allowed_roots=(str(self.workspace),),
            db_path=api.db.path,
            main_model="deterministic-managed-runner-test",
        ).validated()
        retrieval = RetrievalRouter(
            FileRetriever([str(self.workspace)]),
            FTSRetriever(api.db),
            DisabledVectorStore(),
            False,
        )
        self.model = ManagedRunnerModel()
        api.runtime = Runtime(settings, api.db, retrieval, self.model)
        api.db.create_project("Project", str(self.workspace), time.time(), "project")
        api.db.create_conversation("Conversation", time.time(), "conversation", "project")

    def tearDown(self):
        api.runtime, api.db = self.old_runtime, self.old_db
        self.tmp.cleanup()

    def test_managed_runner_real_runtime_creates_valid_report_and_selects_p2(self):
        goal = (
            "新しいTask用の使い捨て作業領域に必要なファイルを作成する。\n"
            "このTask以外の既存ファイルは変更しない。\n"
            "no commit; no package; no deploy; no publish"
        )
        api.db.create_coding_task(
            "managed-real-runtime",
            "conversation",
            goal,
            "QUEUED",
            "NONE",
            approved_plan(goal),
            time.time(),
        )

        self.assertEqual("Coding Task completed.", api.run_managed_task("managed-real-runtime", str(self.workspace)))

        p1_file = self.workspace / "runtime_p1.txt"
        self.assertEqual("OLCR_P1_REAL_RUNTIME", p1_file.read_text(encoding="utf-8"))
        self.assertEqual("OUTSIDE_SENTINEL_ORIGINAL", self.sentinel.read_text(encoding="utf-8"))
        self.assertEqual(["p1", "p2"], self.model.implementation_phases)

        reports = api.db.coding_phase_reports("managed-real-runtime")
        p1_reports = [row for row in reports if row["phase_id"] == "p1"]
        self.assertEqual(1, len(p1_reports))
        p1 = p1_reports[0]
        report = p1["structured_report"]
        self.assertEqual("PASS", p1["validation_status"])
        self.assertEqual([], validate_phase_report(report, "p1", 0))
        self.assertEqual("PASS", report["manager_decision"]["decision"])
        self.assertEqual(0, p1["attempt"])
        self.assertIn(str(p1_file), report["changed_files"])

        typed = report["typed_execution_summary"]
        self.assertEqual("completed", typed["state"])
        self.assertTrue(any(item["tool"] == "workspace_write" and item["status"] == "success" for item in typed["operations"]))
        self.assertTrue(any(item["tool"] == "workspace_read" and item["status"] == "success" for item in typed["operations"]))
        self.assertEqual("pass", api.db.coding_task("managed-real-runtime")["plan"]["phases"][0]["status"])
        self.assertEqual("pass", api.db.coding_task("managed-real-runtime")["plan"]["phases"][1]["status"])
        self.assertEqual(0, api.db.coding_task("managed-real-runtime")["retry_count"])
        # Phase and completion outcomes derive from typed evidence.  Coding
        # execution must not invoke the retired Gemma review/completion gates.
        self.assertEqual(0, self.model.gemma_reviews)
        self.assertEqual(0, self.model.gemma_completion_checks)

    def test_gemma_pass_is_rejected_without_authoritative_phase_evidence(self):
        goal = "Create a workspace file."
        plan = approved_plan(goal)
        phase = plan["phases"][0]
        api.db.create_coding_task("missing-evidence", "conversation", goal, "RUNNING", "NONE", plan, time.time())
        missing_evidence = {
            "phase_id": "p1",
            "attempt": 0,
            "status": "PASS",
            "implemented": ["model prose only"],
            "changed_files": [],
            "test_executed": [],
            "test_pass": [],
            "test_fail": [],
            "build_executed": "NOT_RUN",
            "build_pass": "NOT_RUN",
            "errors": [],
            "blockers": [],
            "risks": [],
            "typed_execution_summary": {"state": "completed", "error": None, "operations": []},
        }
        report_row = api._save_report("missing-evidence", "p1", 0, missing_evidence, "PASS")

        decision = api._review_phase("missing-evidence", api.db.coding_task("missing-evidence"), phase, report_row, [])

        self.assertEqual("RETRY", decision)
        saved = api.db.coding_phase_reports("missing-evidence")[0]["structured_report"]
        self.assertEqual("RETRY", saved["manager_decision"]["decision"])


if __name__ == "__main__":
    unittest.main()
