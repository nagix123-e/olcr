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

    def test_retry_preserves_completed_prefix_and_actual_failure(self):
        report={'status':'PASS','errors':[],'test_fail':[],'build_executed':'NOT_RUN','build_pass':'NOT_RUN'}
        api._set_subtask_state('task','p1','DONE',report=report,decision='PASS')
        self.plan['phases'][0]['status']='pass'
        failure={'errors':['verification command failed'],'typed_execution_summary':{'operations':[
            {'tool':'verification_command','output':{'test_runs':[{'failures':[{'name':'history contains latest draws'}]}]}}]}}
        api._set_subtask_state('task','p2','FAILED',attempt=3,report=failure)
        for _ in range(3):
            api._initialize_subtask_progress('task',self.plan,preserve=True)
            api._set_subtask_state('task','p2','RUNNING',attempt=4)
        entries=api.db.coding_task('task')['subtask_progress']
        self.assertEqual(['DONE','RUNNING','WAITING'],[entry['status'] for entry in entries])
        self.assertEqual('PASS',entries[0]['verification_status'])
        self.assertEqual('history contains latest draws',entries[1]['failure_summary'])
        self.assertEqual(4,entries[1]['attempt'])

    def test_retry_restores_erased_completion_only_from_controller_evidence(self):
        self.plan['phases'][0]['status']='pass'
        self.plan['phases'][1]['status']='pass'
        api.db.add_coding_phase_report('task','p1',2,{
            'status':'PASS','build_pass':'NOT_RUN','manager_decision':{'decision':'PASS'}},'PASS',time.time())
        api._initialize_subtask_progress('task',self.plan,preserve=True)
        entries=api.db.coding_task('task')['subtask_progress']
        self.assertEqual(['DONE','WAITING','WAITING'],[entry['status'] for entry in entries])
        self.assertEqual('PASS',entries[0]['verification_status'])
        self.assertEqual(2,entries[0]['attempt'])

    def test_replan_explicit_prefix_resets_unfinished_graph_state(self):
        api._set_subtask_state('task','p1','DONE',report={'status':'PASS'},decision='PASS')
        api._set_subtask_state('task','p2','RUNNING',attempt=3)
        api._initialize_subtask_progress('task',self.plan,preserve=True,completed_phase_ids={'p1'})
        self.assertEqual(['DONE','WAITING','WAITING'],[entry['status'] for entry in api.db.coding_task('task')['subtask_progress']])

    def test_invalidated_phase_does_not_keep_a_stale_done_graph_row(self):
        api._set_subtask_state('task','p1','DONE',report={'status':'PASS'},decision='PASS')
        api._initialize_subtask_progress('task',self.plan,preserve=True)
        self.assertEqual('WAITING',api.db.coding_task('task')['subtask_progress'][0]['status'])

