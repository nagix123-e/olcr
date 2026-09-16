import tempfile
import time
import unittest
from pathlib import Path

import olcr_api.app as api
from olcr_api.coding_tasks import validate_plan
from olcr_api.db import Database


def plan(goal: str) -> dict:
    phases=[{"id":f"p{i}","goal":f"phase {i}","status":"pending","done":["done"],"verify":["manual check"],"dependencies":[] if i==1 else [f"p{i-1}"],"risks":["risk"]} for i in range(1,4)]
    tasks=[{"task_id":f"t{i}","phase_id":f"p{i}","goal":f"deliverable {i}","domain":"frontend","depends_on":[] if i==1 else [f"t{i-1}"],"required_context":[],"required_mcp":[],"change_scope":["frontend"],"verification":["manual check"],"done_condition":["done"],"retry_state":{}} for i in range(1,4)]
    return {"schema_version":1,"original_goal":goal,"scope":{"allowed":["frontend"],"forbidden":[]},"assumptions":[],"phases":phases,"tasks":tasks,"max_retries_per_phase":2,"requires_user_approval":True}


class SubtaskProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db=api.db
        api.db=Database(str(Path(self.tmp.name)/"subtasks.sqlite"));api.db.initialize()
        api.db.create_project("project",self.tmp.name,time.time(),"project")
        api.db.create_conversation("conversation",time.time(),"conversation","project")
        self.goal="fix sample"; self.plan=plan(self.goal)
        self.assertEqual([],validate_plan(self.plan,self.goal))
        api.db.create_coding_task("task","conversation",self.goal,"WAITING_FOR_PLAN_APPROVAL","NONE",self.plan,time.time())
        api._initialize_subtask_progress("task",self.plan)

    def tearDown(self):
        api.db=self.old_db
        self.tmp.cleanup()

    def test_waiting_running_done_and_reload_are_persisted(self):
        self.assertEqual(["WAITING"]*3,[entry["status"] for entry in api.db.coding_task("task")["subtask_progress"]])
        api._set_subtask_state("task","p1","RUNNING",attempt=0)
        api._set_subtask_state("task","p1","DONE",attempt=0,report={"status":"PASS","errors":[],"blockers":[],"test_fail":[],"build_executed":"DONE","build_pass":"PASS"},decision="PASS")
        reopened=Database(str(Path(self.tmp.name)/"subtasks.sqlite"));reopened.initialize()
        state=reopened.coding_task("task")["subtask_progress"]
        self.assertEqual("DONE",state[0]["status"])
        self.assertEqual("PASS",state[0]["verification_status"])
        self.assertEqual("WAITING",state[1]["status"])

    def test_failed_partial_and_verification_are_separate(self):
        failure={"status":"FAIL","errors":["test command exited 1"],"blockers":[],"test_fail":["test_widget"],"build_executed":"DONE","build_pass":"FAIL"}
        api._set_subtask_state("task","p1","FAILED",attempt=2,report=failure)
        api._set_subtask_state("task","p2","PARTIAL",attempt=0,report={"status":"PASS","errors":[],"blockers":[],"test_fail":[],"build_executed":"NOT_RUN","build_pass":"NOT_RUN"})
        state=api.db.coding_task("task")["subtask_progress"]
        self.assertEqual(("FAILED","FAILED"),(state[0]["status"],state[0]["verification_status"]))
        self.assertEqual(("PARTIAL","NOT_RUN"),(state[1]["status"],state[1]["verification_status"]))
        self.assertEqual("test command exited 1",state[0]["failure_summary"])

    def test_only_one_subtask_can_run(self):
        api._set_subtask_state("task","p1","RUNNING",attempt=0)
        with self.assertRaisesRegex(RuntimeError,"multiple running"):
            api._set_subtask_state("task","p2","RUNNING",attempt=0)

    def test_legacy_plan_has_no_fabricated_subtask_history(self):
        legacy={key:value for key,value in self.plan.items() if key!="tasks"}
        api.db.create_coding_task("legacy","conversation",self.goal,"RESUMABLE","NONE",legacy,time.time())
        api._initialize_subtask_progress("legacy",legacy)
        self.assertIsNone(api.db.coding_task("legacy")["subtask_progress"])

