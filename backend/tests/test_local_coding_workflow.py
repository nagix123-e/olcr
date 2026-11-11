"""Exercise the real app execution boundaries without invoking generation models."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import olcr_api.app as api
from olcr_api.db import Database


class LocalCodingWorkflowTests(unittest.TestCase):
    def setUp(self):
        api.stop_coding_scheduler(timeout=5)
        self.temporary = tempfile.TemporaryDirectory()
        self.old_db = api.db
        api.db = Database(str(Path(self.temporary.name)/'db.sqlite')); api.db.initialize()
        self.root = Path(self.temporary.name)/'source'; self.root.mkdir()
        import subprocess
        for args in [('init','-b','main'),('config','user.name','Fixture'),('config','user.email','fixture@example.test')]:
            subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)
        (self.root/'a.py').write_text('original\n')
        subprocess.run(['git','-C',str(self.root),'add','.'],check=True,capture_output=True)
        subprocess.run(['git','-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','-C',str(self.root),'commit','-m','Initial'],check=True,capture_output=True)
        api.db.create_project('fixture',str(self.root),time.time(),'p')
        api.db.create_conversation('fixture',time.time(),'c','p')

    def tearDown(self):
        api.stop_coding_scheduler(timeout=5)
        api.db=self.old_db
        self.temporary.cleanup()

    def task(self,identifier,mode='direct',planning=False,goal='Fix a.py'):
        api.task_workspaces.service.preference('p',mode)
        task=api.db.create_coding_task(identifier,'c',goal,'QUEUED','QWEN_PLANNING' if planning else 'NONE',None if planning else {'phases':[]},time.time())
        api.task_workspaces.service.reserve(identifier)
        return task

    def test_runner_resolves_worktree_before_execution_and_legacy_tasks_keep_original_root(self):
        self.task('isolated',mode='worktree')
        with patch.object(api,'_run_managed_task',return_value='done') as run:
            api.run_managed_task('isolated',str(self.root))
        actual=run.call_args.args[1]
        self.assertNotEqual(str(self.root),actual)
        self.assertEqual('original\n',(Path(actual)/'a.py').read_text())
        api.db.create_coding_task('legacy','c','Fix a.py','QUEUED','NONE',{'phases':[]},time.time())
        with patch.object(api,'_run_managed_task',return_value='done') as run:
            api.run_managed_task('legacy',str(self.root))
        self.assertEqual(str(self.root.resolve()),run.call_args.args[1])

    def test_queued_instruction_replans_once_at_boundary_and_records_new_revision(self):
        task=self.task('task');api.task_workspaces.service.prepare('task')
        api.task_workspaces.service.steer('task','Keep the public API',0)
        def replan(task_id,managed,completed,reason):
            self.assertIn('Keep the public API',reason)
            api.db.update_coding_task(task_id,plan_revision=1)
            return 'replanned'
        with patch.object(api,'_replan_task',side_effect=replan) as planner:
            self.assertEqual('replanned',api._apply_task_steering('task',task))
            self.assertIsNone(api._apply_task_steering('task',api.db.coding_task('task')))
        self.assertEqual(1,planner.call_count)
        self.assertEqual('APPLIED',api.task_workspaces.service.steering('task')[0]['status'])

    def test_scaffold_checkpoint_does_not_claim_user_edits_to_existing_manifest_files(self):
        import hashlib
        self.task('task');api.task_workspaces.service.prepare('task')
        (self.root/'a.py').write_text('manual edit during planning\n')
        plan={'file_manifest':[{'path':'a.py','action':'modify'},{'path':'new.py','action':'create'}]}
        def prepare(*args,**kwargs):
            (self.root/'new.py').write_text('# scaffold\n')
            artifact={'file_manifest':plan['file_manifest'],'scaffold_created_hashes':{'new.py':hashlib.sha256(b'# scaffold\n').hexdigest()}}
            return {**plan,'implementation_plan_artifact':artifact},artifact
        with patch.object(api,'prepare_implementation_plan',side_effect=prepare):
            prepared,_=api._prepare_task_implementation_plan('task',plan,str(self.root),{})
        api.db.update_coding_task('task',plan=prepared,status='RESUMABLE')
        files={f['path']:f for f in api.task_workspaces.service.changes('task')['files']}
        self.assertFalse(files['a.py']['revertible'])
        self.assertTrue(files['new.py']['revertible'])

    def test_real_typed_executor_and_unittest_run_inside_worktree_without_editing_source(self):
        import hashlib
        import json
        import subprocess
        initial="def greet():\n    return 'before'\n"
        (self.root/'a.py').write_text(initial)
        (self.root/'test_a.py').write_text("import unittest\nfrom a import greet\nclass Greeting(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual('after', greet())\n")
        for args in [('add','.'),('commit','-m','Seed function fixture')]:
            subprocess.run(['git','-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','-C',str(self.root),*args],check=True,capture_output=True)
        task=self.task('actual','worktree',goal=
            'Fix a.py and run python3 -m unittest discover -s . -p test_a.py')
        plan={'schema_version':1,'original_goal':task['original_goal'],'scope':{'allowed':['a.py'],'forbidden':[]},
              'assumptions':[],'max_retries_per_phase':2,'requires_user_approval':True,
              'file_manifest':[{'path':'a.py','action':'modify','role':'implementation','required':True,'owner_phase':'p1'}],
              'phases':[{'id':'p1','goal':'Fix a.py greet result','status':'pending','dependencies':[],
                         'done':['a.py is updated'],'verify':['Greeting test passes'],'risks':[],
                         'execution_mode':'IMPLEMENTATION',
                         'verification_command':['python3','-m','unittest','discover','-s','.','-p','test_a.py'],
                         'verification_targets':['a.py','test_a.py']}]}
        api.db.update_coding_task('actual',plan=plan,approved_scopes=[{'source':'original_request','requested_scope':['a.py']}])
        class Model:
            def generate(self,messages,model,stream=False,think=None,format=None):
                return {'text':json.dumps({'operations':[{'op':'patch','path':'a.py','old_text':"return 'before'",'new_text':"return 'after'",'source_hash':hashlib.sha256(initial.encode()).hexdigest()}]})}
        with patch.object(api.runtime,'model',Model()),patch.object(api.runtime,'db',api.db):
            result=api.run_managed_task('actual',str(self.root))
        self.assertEqual('Coding Task completed.',result)
        self.assertEqual('COMPLETED',api.db.coding_task('actual')['status'])
        self.assertEqual(initial,(self.root/'a.py').read_text())
        root=Path(api.task_workspaces.service.root_for('actual'))
        self.assertIn("return 'after'",(root/'a.py').read_text())
        reports=api.db.coding_phase_reports('actual')
        self.assertTrue(reports[-1]['structured_report']['test_pass'])
        changes=api.task_workspaces.service.changes('actual')
        self.assertEqual(['a.py'],[file['path'] for file in changes['files']])
        self.assertTrue(changes['files'][0]['revertible'])

    def test_scheduler_runs_two_isolated_tasks_together(self):
        for identifier in ['one','two']: self.task(identifier,'worktree',planning=True)
        both=threading.Event(); release=threading.Event(); started=[]; guard=threading.Lock()
        def planning(task):
            with guard:
                started.append(task['id'])
                if len(started)==2: both.set()
            release.wait(5)
            api.db.update_coding_task(task['id'],status='RESUMABLE')
        try:
            with patch.object(api,'_run_coding_planning',side_effect=planning),patch.object(api.brain_models,'configuration',return_value={}),patch.dict(os.environ,{'OLCR_CODING_CONCURRENCY':'2'}):
                api.start_coding_scheduler();api._coding_scheduler_wake.set()
                self.assertTrue(both.wait(3),'isolated tasks should enter planning together')
                release.set();api.stop_coding_scheduler(timeout=5)
        finally: release.set()

    def test_scheduler_serializes_tasks_sharing_direct_workspace(self):
        for identifier in ['one','two']: self.task(identifier,planning=True)
        first=threading.Event();second=threading.Event();release=threading.Event();started=[]
        def planning(task):
            started.append(task['id'])
            if len(started)==1: first.set();release.wait(5)
            else: second.set()
            api.db.update_coding_task(task['id'],status='RESUMABLE')
        try:
            with patch.object(api,'_run_coding_planning',side_effect=planning),patch.object(api.brain_models,'configuration',return_value={}):
                api.start_coding_scheduler();api._coding_scheduler_wake.set()
                self.assertTrue(first.wait(3));self.assertFalse(second.wait(.15))
                release.set();self.assertTrue(second.wait(3));api.stop_coding_scheduler(timeout=5)
        finally: release.set()


if __name__=='__main__':unittest.main()
