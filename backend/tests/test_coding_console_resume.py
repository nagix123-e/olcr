"""Durable console restoration and conversational changes to existing work."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api.db import Database


class CodingConsoleResumeTests(unittest.TestCase):
    def setUp(self):
        api.stop_coding_scheduler()
        self.temporary=tempfile.TemporaryDirectory(prefix='olcr-console-',dir='/private/tmp')
        self.root=Path(self.temporary.name)
        self.old_db,self.old_settings=api.db,api.settings
        api.db=Database(str(self.root/'console.db'));api.db.initialize()
        api.settings=api.settings.with_overrides({'task_manager_enabled':True})
        api.db.create_project('fixture',str(self.root),time.time(),'p')
        api.db.create_conversation('fixture',time.time(),'c','p')
        self.client=TestClient(api.app)
        self.addCleanup(patch.stopall)
        patch.object(api._coding_scheduler_wake,'set').start()

    def tearDown(self):
        api.stop_coding_scheduler()
        api.db,api.settings=self.old_db,self.old_settings
        self.temporary.cleanup()

    def task(self,identifier='t',status='RESUMABLE',**changes):
        phase={'id':'p1','goal':'Adjust the button','status':'pending','done':['logic.py exists'],
               'verify':['Typed workspace writes and read-back succeed for the changed files.'],
               'dependencies':[],'risks':[],'execution_mode':'IMPLEMENTATION'}
        plan={'schema_version':1,'original_goal':'Adjust the existing button',
              'scope':{'allowed':['logic.py'],'forbidden':[]},'assumptions':[],
              'phases':[phase],'max_retries_per_phase':2,'requires_user_approval':True}
        api.db.create_coding_task(identifier,'c',plan['original_goal'],status,'NONE',plan,time.time())
        api.db.update_coding_task(identifier,current_phase_id='p1',recovery_reason='RESOURCE_CHECKPOINT',**changes)
        (self.root/'logic.py').write_text('value = 1\n')
        report={'phase_id':'p1','attempt':1,'plan_revision':0,'status':'FAIL',
                'errors':['saved failure'],'changed_files':['logic.py']}
        api.db.add_coding_phase_report(identifier,'p1',1,report,'PASS',time.time())
        return api.db.coding_task(identifier)

    def hide(self,identifier='t'):
        result=self.client.put('/api/coding-tasks/'+identifier+'/console',json={'hidden':True})
        self.assertEqual(200,result.status_code,result.text)
        return result.json()

    def test_close_saves_console_snapshot_without_archiving_or_stopping_work(self):
        before=self.task(status='RUNNING');after=self.hide()
        self.assertTrue(after['console_hidden'])
        for field in ('status','archived','pause_requested','plan','current_phase_id','retry_count'):
            self.assertEqual(before[field],after[field],field)
        reopened=Database(api.db.path);reopened.initialize()
        self.assertTrue(reopened.coding_task('t')['console_hidden'])
        events=reopened.coding_task_events('t')
        self.assertEqual('CONSOLE_HIDDEN',events[-1]['kind'])
        self.assertEqual('p1',events[-1]['data']['current_phase_id'])
        self.assertEqual([row['id'] for row in reopened.coding_phase_reports('t')],events[-1]['data']['report_ids'])
        logs=self.client.get('/api/coding-tasks/t/logs').json()
        self.assertEqual('saved failure',logs['reports'][0]['structured_report']['errors'][0])

    def test_resume_restores_the_same_saved_task_on_both_chat_transports(self):
        for index,endpoint in enumerate(('/api/chat','/api/chat/stream')):
            identifier='t'+str(index);before=self.task(identifier);self.hide(identifier)
            with patch.object(api.runtime,'execute',side_effect=AssertionError('No inline coding/rebuild')):
                result=self.client.post(endpoint,json={'conversation_id':'c','message':'再開して。'})
            self.assertEqual(200,result.status_code,result.text)
            self.assertIn(identifier,result.text)
            after=api.db.coding_task(identifier)
            self.assertEqual('QUEUED',after['status'])
            self.assertFalse(after['console_hidden'])
            self.assertFalse(after['archived'])
            self.assertEqual(before['plan'],after['plan'])
            self.assertEqual('p1',after['current_phase_id'])
            self.assertEqual(1,len(api.db.coding_phase_reports(identifier)))

    def test_legacy_close_archive_can_be_restored_with_its_original_history(self):
        self.task(archived=True)
        with patch.object(api.runtime,'execute',side_effect=AssertionError('No new task')):
            result=self.client.post('/api/chat',json={'conversation_id':'c','message':'再開して'})
        self.assertEqual(200,result.status_code,result.text)
        self.assertEqual('t',result.json()['coding_task_id'])
        self.assertFalse(api.db.coding_task('t')['archived'])
        self.assertEqual(1,len(api.db.coding_tasks('c')))
        self.assertEqual(1,len(api.db.coding_phase_reports('t')))

    def test_completed_console_restores_without_replaying_completed_work(self):
        self.task(status='COMPLETED');self.hide()
        with patch.object(api,'_resume_coding_task_control',side_effect=AssertionError('Do not rerun completed task')):
            response=self.client.post('/api/chat',json={'conversation_id':'c','message':'再開して'})
        self.assertEqual('t',response.json()['coding_task_id'])
        self.assertEqual('COMPLETED',api.db.coding_task('t')['status'])
        self.assertFalse(api.db.coding_task('t')['console_hidden'])

    def test_restoring_console_does_not_approve_pending_operations(self):
        pending={'type':'SCOPE_AUTHORIZATION','state':'PENDING','requested_scope':['outside.py']}
        self.task(status='WAITING_FOR_USER',pending_authorization=pending);self.hide()
        response=self.client.post('/api/chat',json={'conversation_id':'c','message':'再開して'})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(pending,api.db.coding_task('t')['pending_authorization'])
        self.assertEqual('WAITING_FOR_USER',api.db.coding_task('t')['status'])
        self.assertFalse(api.db.coding_task('t')['console_hidden'])

    def test_restore_cannot_select_a_task_in_another_conversation(self):
        self.task();api.db.create_conversation('other',time.time(),'other','p')
        response=self.client.post('/api/chat',json={'conversation_id':'other','message':'再開して','coding_task_id':'t'})
        self.assertEqual(409,response.status_code)

    def test_resume_control_accepts_polite_and_contextual_requests(self):
        for text in ('再開してください','コーディングを再開して','前の作業を続行してください',
                     'コーディングタスクに戻って','please resume'):
            self.assertTrue(api._coding_resume_control_event(text),text)
            self.assertFalse(api._independent_external_chat_turn(api.ChatInput(message=text)))

    def test_old_database_migration_preserves_reports_and_can_be_reopened(self):
        self.task()
        with api.db.connect() as conn:
            conn.execute('ALTER TABLE coding_tasks DROP COLUMN console_hidden')
            conn.execute('DROP TABLE coding_task_events')
            conn.execute('UPDATE schema_version SET version=36')
        api.db.initialize();api.db.initialize()
        self.assertFalse(api.db.coding_task('t')['console_hidden'])
        self.assertEqual(1,len(api.db.coding_phase_reports('t')))
        self.hide();self.assertEqual('CONSOLE_HIDDEN',api.db.coding_task_events('t')[-1]['kind'])

    def test_revision_change_during_instruction_receipt_is_rebased_once(self):
        self.task(status='RUNNING')
        original=api.task_workspaces.service.steer
        calls=[]
        def revised(identifier,text,revision,**kwargs):
            calls.append(revision)
            if len(calls)==1:
                api.db.update_coding_task(identifier,plan_revision=1)
            return original(identifier,text,revision,**kwargs)
        with patch.object(api.task_workspaces.service,'steer',side_effect=revised):
            result=api._managed_coding_chat(api.ChatInput(message='ボタンだけ青に変更して',
                coding_task_id='t',coding_followup='auto'),'c','revision-race')
        self.assertEqual('t',result['coding_task_id'])
        self.assertEqual([0,1],calls)
        rows=api.task_workspaces.service.steering('t')
        self.assertEqual(1,len(rows));self.assertEqual(1,rows[0]['received_revision'])

    def test_auto_changes_apply_at_next_boundary_and_explicit_queue_still_waits(self):
        task=self.task(status='RUNNING')
        for mode,identifier,status in [('auto','now','QUEUED'),('queue','later','WAITING')]:
            result=api._managed_coding_chat(api.ChatInput(message='ボタンの色だけ青に変更して',
                coding_task_id='t',coding_followup=mode),'c',identifier)
            self.assertEqual('t',result['coding_task_id'])
            rows=api.task_workspaces.service.steering('t')
            self.assertEqual(status,next(row for row in rows if row['id']==identifier)['status'])
        self.assertEqual('RUNNING',api.db.coding_task('t')['status'])
        self.assertEqual(2,len(api.db.coding_task_events('t')))

    def test_small_auto_change_to_paused_work_does_not_restart_the_whole_project(self):
        before=self.task()
        request='ボタンの色だけ青に変更して'
        result=api._managed_coding_chat(api.ChatInput(message=request,coding_task_id='t',coding_followup='auto'),'c','small-change')
        child=api.db.coding_task(result['coding_task_id'])
        self.assertNotEqual('t',child['id'])
        self.assertEqual(request,child['original_goal'])
        self.assertEqual('t',child['requirements']['prior_task_id'])
        self.assertEqual('SCOPED_CHANGE',child['requirements']['followup_kind'])
        self.assertEqual('RESUMABLE',api.db.coding_task('t')['status'])
        self.assertEqual(before['plan'],api.db.coding_task('t')['plan'])
        api.task_workspaces.service.prepare(child['id'])
        self.assertEqual(str(self.root),api.task_workspaces.service.root_for(child['id']))
        with patch.object(api.runtime,'execute',side_effect=AssertionError('No inline execution')):
            restored=api._managed_coding_chat(api.ChatInput(message='前の作業を再開して',
                coding_task_id=child['id'],coding_followup='auto'),'c','return-to-parent')
        self.assertEqual('t',restored['coding_task_id'])

    def test_casual_and_explanation_messages_do_not_become_implementation_requests(self):
        self.task(status='COMPLETED')
        for message in ('ありがとう','なぜこの実装にしたの？','Explain this change'):
            value=api.ChatInput(message=message,coding_task_id='t',coding_followup='auto')
            self.assertTrue(api._independent_external_chat_turn(value),message)
            self.assertIsNone(api._managed_coding_chat(value,'c','chat'))
        self.assertEqual(1,len(api.db.coding_tasks('c')))

    def test_late_instruction_is_saved_for_a_scoped_followup_not_marked_applied(self):
        task=self.task(status='FINAL_REPORTING');plan=copy.deepcopy(task['plan']);plan['phases'][0]['status']='pass'
        api.db.update_coding_task('t',plan=plan)
        result=api._managed_coding_chat(api.ChatInput(message='ボタンを青に変更して',
            coding_task_id='t',coding_followup='auto'),'c','late')
        self.assertEqual('t',result['coding_task_id'])
        self.assertEqual('WAITING',api.task_workspaces.service.steering('t')[0]['status'])

    def test_user_instruction_revision_is_not_limited_by_automatic_recovery_budget(self):
        task=self.task(status='RUNNING')
        api.db.update_coding_task('t',replan_count=99,replan_count_in_epoch=99,task_total_recovery_model_calls=99)
        task=api.db.coding_task('t');plan=copy.deepcopy(task['plan'])
        with patch.object(api,'_generate_plan',return_value=(plan,'{}')) as planner, \
             patch.object(api,'_run_managed_task',return_value='instruction-dispatched'), \
             patch.object(api,'_repeated_zero_progress_failure',return_value=(True,'old-failure',8)):
            result=api._replan_task('t',task,set(),'Explicit user instructions at a phase boundary:\nボタンの色だけ青に変更して')
        self.assertEqual('Coding Task was replanned and requeued.',result)
        self.assertTrue(planner.call_args.kwargs['requirements']['_explicit_instruction_revision'])
        updated=api.db.coding_task('t')
        self.assertEqual(99,updated['replan_count'])
        self.assertEqual(99,updated['task_total_recovery_model_calls'])
        self.assertEqual(1,updated['plan_revision'])
        self.assertIsNone(updated['pending_authorization'])

    def test_explicit_revision_planning_does_not_reserve_recovery_model_calls(self):
        task=self.task(status='RUNNING')
        with patch.object(api,'_record_recovery_model_call',side_effect=AssertionError('No automatic recovery charge')), \
             patch.object(api,'_model_text',return_value=json.dumps(task['plan'])):
            plan,_=api._generate_plan('t',task['original_goal'],'Apply current instruction','QWEN_REPLANNING',
                requirements={'_explicit_instruction_revision':True},recovery=True)
        self.assertIsNotNone(plan)
