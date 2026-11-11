import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import olcr_api.app as api
from olcr_api.db import Database
from fastapi.testclient import TestClient


def one_phase_plan(goal: str) -> dict:
    return {"schema_version": 1, "original_goal": goal,
            "scope": {"allowed": ["backend"], "forbidden": []}, "assumptions": [],
            "phases": [{"id": "p1", "goal": "phase", "status": "pending",
                         "done": ["done"], "verify": ["manual check"],
                         "dependencies": [], "risks": []}],
            "max_retries_per_phase": 2, "requires_user_approval": True}


def pass_report(revision: int) -> dict:
    return {"phase_id": "p1", "plan_revision": revision, "attempt": 0,
            "status": "PASS", "implemented": ["done"], "changed_files": [],
            "test_executed": ["manual check"], "test_pass": ["manual check"],
            "test_fail": [], "build_executed": "NOT_RUN", "build_pass": "NOT_RUN",
            "errors": [], "blockers": [], "risks": [],
            "typed_execution_summary": {"state": "completed", "error": None, "operations": []},
            "manager_decision": {"decision": "PASS", "reason": "verified"}}


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db, self.old_runtime = api.db, api.runtime
        api.db = Database(str(Path(self.tmp.name) / "finalization.sqlite"))
        api.db.initialize()
        api.db.create_project("Project", self.tmp.name, time.time(), "project")
        api.db.create_conversation("Conversation", time.time(), "conversation", "project")

    def tearDown(self):
        api.runtime, api.db = self.old_runtime, self.old_db
        self.tmp.cleanup()

    def create_task(self, task_id: str, status: str = "RUNNING"):
        goal = "finish sample"
        task_plan = one_phase_plan(goal)
        api.db.create_coding_task(task_id, "conversation", goal, status, "NONE", task_plan, time.time())
        return task_plan

    def test_finalization_ignores_old_plan_revision_and_reports_all_diagnostics(self):
        task_plan = self.create_task("current-revision")
        api.db.update_coding_task("current-revision", replan_count=1)
        api._save_report("current-revision", "p1", 0, {**pass_report(0), "manager_decision": {"decision": "PASS"}}, "PASS")
        api._save_report("current-revision", "p1", 0, pass_report(1), "PASS")

        class Model:
            def __init__(self): self.calls = []
            def generate(self, messages, model, think=False, format=None):
                self.calls.append(messages[0]["content"])
                if "completion check" in messages[0]["content"]:
                    return {"text": '{"decision":"PASS","reason":"complete"}'}
                if "final report" in messages[0]["content"]:
                    return {"text": "final report"}
                raise AssertionError("unexpected model call")

        class Runtime: pass
        runtime = Runtime(); runtime.model = Model()
        api.runtime = runtime
        result = api._complete_task("current-revision", api.db.coding_task("current-revision"))
        task = api.db.coding_task("current-revision")
        self.assertEqual("Coding Task completed.", result)
        self.assertEqual("COMPLETED", task["status"])
        self.assertEqual([], runtime.model.calls)
        self.assertEqual("PASS", task["final_report_status"])
        self.assertIn("Implemented", task["final_report"]["text"])
        self.assertIn("Status: COMPLETED", task["final_report"]["text"])
        payload = TestClient(api.app).get("/api/coding-tasks/current-revision").json()
        self.assertIsInstance(payload["final_report"]["text"], str)

    def test_formatter_failure_is_resumable_and_resume_skips_phases_and_completion_check(self):
        task_plan = self.create_task("final-retry", "QUEUED")
        api._save_report("final-retry", "p1", 0, pass_report(0), "PASS")
        original = api.deterministic_final_report
        calls = {"count": 0}
        def fail_once(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary formatter failure")
            return original(*args, **kwargs)
        with patch.object(api, "deterministic_final_report", side_effect=fail_once):
            api._complete_task("final-retry", api.db.coding_task("final-retry"))
            failed = api.db.coding_task("final-retry")
            self.assertEqual("RESUMABLE", failed["status"])
            self.assertEqual("FINAL_REPORT", failed["recovery_action"])
            self.assertEqual("FORMATTER", failed["recovery_reason"])
            report_count = len(api.db.coding_phase_reports("final-retry"))
            api.db.enqueue_coding_task("final-retry")
            with patch.object(api, "_prepare_approved_plan_artifact",
                              side_effect=AssertionError("completed phases must not re-enter plan preparation")):
                api._run_managed_task("final-retry", self.tmp.name)
        done = api.db.coding_task("final-retry")
        self.assertEqual("COMPLETED", done["status"])
        self.assertEqual(report_count, len(api.db.coding_phase_reports("final-retry")))
        self.assertEqual(2, calls["count"])
        self.assertIn("Implemented", done["final_report"]["text"])

    def test_invalid_completion_check_is_recoverable_without_starting_final_report(self):
        self.create_task("invalid-completion")
        api._save_report("invalid-completion", "p1", 0, pass_report(0), "PASS")

        class Model:
            def __init__(self): self.calls = []
            def generate(self, messages, model, think=False, format=None):
                self.calls.append(messages[0]["content"])
                return {"text": "not valid completion JSON"}

        class Runtime: pass
        runtime = Runtime(); runtime.model = Model()
        api.runtime = runtime
        result = api._complete_task("invalid-completion", api.db.coding_task("invalid-completion"))
        task = api.db.coding_task("invalid-completion")
        self.assertEqual("Coding Task completed.", result)
        self.assertEqual("COMPLETED", task["status"])
        self.assertEqual(0, len(runtime.model.calls))

    def test_completed_task_final_report_lists_all_artifact_paths(self):
        task_plan = self.create_task("artifact-paths")
        first_path = Path(self.tmp.name) / "index.html"
        second_path = Path(self.tmp.name) / "style.css"
        api._save_report("artifact-paths", "p1", 0,
                         {**pass_report(0), "changed_files": [str(first_path)],
                          "typed_execution_summary": {"state": "completed", "operations": [
                              {"tool": "workspace_write", "status": "success",
                               "input": {"path": str(second_path)}, "output": {"path": str(second_path)}}]}},
                         "PASS")

        class Runtime: pass
        class Model:
            def __init__(self): self.calls = []
            def generate(self, messages, model, think=False, format=None): self.calls.append(messages)
        runtime = Runtime(); runtime.model = Model()
        old_runtime = api.runtime; api.runtime = runtime
        try:
            api._complete_task("artifact-paths", api.db.coding_task("artifact-paths"))
        finally:
            api.runtime = old_runtime
        final_text = api.db.coding_task("artifact-paths")["final_report"]["text"]
        self.assertIn("成果物ファイル:", final_text)
        self.assertIn(str(first_path), final_text)
        self.assertIn(str(second_path), final_text)
        self.assertIn("MCP_AVAILABLE=", final_text)
        self.assertIn("MCP_USED=", final_text)
        self.assertIn("MCP_PURPOSE=", final_text)
        self.assertIn("MCP_SKIP_REASON=", final_text)

    def test_completed_static_app_report_includes_local_http_launch_without_a_model(self):
        self.create_task('static-launch')
        entry=Path(self.tmp.name)/'画面 with spaces.html'
        entry.write_text('<script type="module" src="app.js"></script>')
        task=api.db.coding_task('static-launch')
        plan={**task['plan'],'file_manifest':[{'path':entry.name}]}
        api.db.update_coding_task('static-launch',plan=plan,requirements={
            'planning_policy':{'coherent_static_application':True},'dependency_requirements':[]})
        api._save_report('static-launch','p1',0,pass_report(0),'PASS')
        with patch.object(api,'_model_text',side_effect=AssertionError('No model is needed for launch guidance')):
            self.assertEqual('Coding Task completed.',api._complete_task('static-launch',api.db.coding_task('static-launch')))
        text=api.db.coding_task('static-launch')['final_report']['text']
        self.assertIn('## 起動方法',text)
        self.assertIn('--bind 127.0.0.1 --directory',text)
        self.assertIn('http://127.0.0.1:8000/%E7%94%BB%E9%9D%A2%20with%20spaces.html',text)
        self.assertNotIn('file://',text)

    def test_launch_guidance_does_not_guess_for_frameworks_missing_or_external_entrypoints(self):
        task={'requirements':{'planning_policy':{'coherent_static_application':True}}}
        plan={'file_manifest':[{'path':'missing.html'},{'path':'../outside.html'}]}
        self.assertEqual('report',api._append_static_app_launch('report',task,plan,self.tmp.name))
        (Path(self.tmp.name)/'index.html').write_text('<h1>app</h1>')
        plan={'file_manifest':[{'path':'index.html'}]}
        self.assertEqual('report',api._append_static_app_launch('report',{'requirements':{}},plan,self.tmp.name))
        dependent={'requirements':{**task['requirements'],'dependency_requirements':['react']}}
        self.assertEqual('report',api._append_static_app_launch('report',dependent,plan,self.tmp.name))

    def test_finalization_rejects_lifecycle_only_required_mcp_evidence(self):
        self.create_task("mcp-not-used")
        api.db.update_coding_task("mcp-not-used",
                                  requirements={"required_capabilities": [], "forbidden_capabilities": [],
                                                "task_profile": "GENERAL_CODING", "execution_mode": "NORMAL",
                                                "required_mcp": ["shadcn"], "required_mcps": ["shadcn"],
                                                "required_verification": [], "preflight": {"shadcn": "PASS"}},
                                  required_mcp=["shadcn"])
        api._mcp_evidence("mcp-not-used", "shadcn", status="PASS", purpose="preflight", tool_name="initialize", result={})
        api._mcp_evidence("mcp-not-used", "shadcn", status="PASS", purpose="preflight", tool_name="tools/list", result={})
        api._save_report("mcp-not-used", "p1", 0, pass_report(0), "PASS")
        result = api._complete_task("mcp-not-used", api.db.coding_task("mcp-not-used"))
        task = api.db.coding_task("mcp-not-used")
        self.assertIn("required MCP used: shadcn", result)
        self.assertEqual("RESUMABLE", task["status"])
        self.assertEqual("REQUIRED_MCP_UNVERIFIED", task["recovery_reason"])


if __name__ == "__main__":
    unittest.main()
