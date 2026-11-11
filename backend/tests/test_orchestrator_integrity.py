"""Cross-boundary invariants for persisted coding execution and completion."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import olcr_api.app as api
from olcr_api.db import Database
from olcr_api.node_project_verification import run_vite_acceptance
from olcr_api.package_manager import PackageManagerError
from olcr_api.models import Task, TaskState, Route
from olcr_api.coding_tasks import evaluate_phase, marketing_design_structure_acceptance, frontend_stack_acceptance


class OrchestratorIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.scheduler_running = bool(api._coding_scheduler_thread and api._coding_scheduler_thread.is_alive())
        api.stop_coding_scheduler()
        self.tmp = tempfile.TemporaryDirectory(prefix='olcr-integrity-', dir='/private/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old_db = api.db
        api.db = Database(str(self.root / 'task.sqlite')); api.db.initialize()
        api.db.create_project('fixture',str(self.root),time.time(),'p')
        api.db.create_conversation('fixture',time.time(),'c','p')

    def tearDown(self):
        api.stop_coding_scheduler()
        api.db = self.old_db
        if self.scheduler_running:
            api.start_coding_scheduler()

    def report(self, *, operations=(), attempt=0, revision=0, mutations=0):
        return {'phase_id':'p1','attempt':attempt,'plan_revision':revision,'recovery_epoch':0,'status':'PASS',
                'implemented':[],'changed_files':[],'test_executed':[],'test_pass':[],'test_fail':[],
                'build_executed':'NOT_RUN','build_pass':'NOT_RUN','errors':[],'blockers':[],'risks':[],
                'final_worktree_mutations':mutations,
                'typed_execution_summary':{'state':'completed','operations':list(operations)},
                'manager_decision':{'decision':'PASS','reason':'saved executor evidence'}}

    def artifact_task(self, *, revision=0):
        path = self.root / 'logic.ts'; path.write_text('export const value = 4;')
        self.phase = {'id':'p1','goal':'Create module','status':'pass','execution_mode':'IMPLEMENTATION',
                      'done':['logic.ts exists'],'verify':['Typed workspace writes and read-back succeed for the changed files.'],
                      'dependencies':[],'risks':[]}
        plan={'schema_version':1,'original_goal':'Create module','scope':{'allowed':['logic.ts'],'forbidden':[]},
              'assumptions':[],'phases':[self.phase],'max_retries_per_phase':2,'requires_user_approval':True}
        api.db.create_coding_task('t','c','Create module','RUNNING','NONE',plan,time.time())
        api.db.update_coding_task('t',plan_revision=revision)
        write={'tool':'workspace_write','status':'success','output':{'path':str(path),'net_changed':True,
               'post_apply_hash':hashlib.sha256(path.read_bytes()).hexdigest()}}
        read={'tool':'workspace_read','status':'success','output':{'path':str(path)}}
        api._save_report('t','p1',0,self.report(operations=[write,read],mutations=1),'PASS')
        current=self.report(operations=[read],attempt=1,revision=revision)
        api._save_report('t','p1',1,current,'PASS')
        return api.db.coding_task('t')

    def test_completion_uses_the_same_accumulated_phase_evidence_as_review(self):
        task=self.artifact_task()
        self.assertTrue(api._phase_passed_in_epoch('t',self.phase,0,0,str(self.root)))
        with patch.object(api.runtime,'execute',side_effect=AssertionError('Do not reimplement retained artifacts')):
            self.assertEqual('Coding Task completed.',api._complete_task('t',task))
        self.assertEqual('COMPLETED',api.db.coding_task('t')['status'])

    def test_unrequested_acceptance_does_not_scan_project_or_dependencies(self):
        self.artifact_task()
        report=api.db.coding_phase_reports('t')[0]['structured_report']
        with patch('olcr_api.coding_tasks.marketing_design_structure_acceptance',
                   side_effect=AssertionError('No unrelated source scan')), \
             patch('olcr_api.coding_tasks.frontend_stack_acceptance',
                   side_effect=AssertionError('No unrelated package inspection')):
            self.assertTrue(evaluate_phase(self.phase,report,workspace_root=str(self.root))['phase_complete'])

    def test_dependency_and_generated_text_cannot_satisfy_project_structure(self):
        labels='header hero feature workflow development tools cta footer links product'
        for directory in ('node_modules/library','dist','target','coverage'):
            target=self.root/directory;target.mkdir(parents=True)
            (target/'decoy.js').write_text(labels)
        self.assertFalse(marketing_design_structure_acceptance(str(self.root)))
        (self.root/'App.tsx').write_text(labels)
        self.assertTrue(marketing_design_structure_acceptance(str(self.root)))

    def test_structure_scan_does_not_follow_external_symlinks(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as external:
            target=Path(external);(target/'decoy.js').write_text('header hero feature workflow development tools cta footer links visual')
            (self.root/'linked').symlink_to(target,target_is_directory=True)
            (self.root/'external.js').symlink_to(target/'decoy.js')
            self.assertFalse(marketing_design_structure_acceptance(str(self.root)))

    def test_malformed_package_metadata_is_unmet_acceptance_not_a_host_crash(self):
        for metadata in ('[]','{"dependencies":5}','{"devDependencies":"invalid"}'):
            with self.subTest(metadata=metadata):
                (self.root/'package.json').write_text(metadata)
                self.assertFalse(frontend_stack_acceptance(str(self.root)))

    def test_other_plan_revision_cannot_supply_missing_mutation_evidence(self):
        task=self.artifact_task(revision=1)
        self.assertFalse(api._phase_passed_in_epoch('t',self.phase,1,0,str(self.root)))
        self.assertIn('incomplete',api._complete_task('t',task))
        self.assertNotEqual('COMPLETED',api.db.coding_task('t')['status'])

    def test_deleted_artifact_invalidates_a_saved_pass_before_phase_skip(self):
        self.artifact_task();(self.root/'logic.ts').unlink()
        self.assertFalse(api._phase_passed_in_epoch('t',self.phase,0,0,str(self.root)))
        self.assertFalse(api._phase_passed_in_epoch('t',self.phase,0,1,str(self.root)))

    def test_runner_recreates_a_deleted_artifact_instead_of_looping_on_saved_pass(self):
        self.artifact_task();(self.root/'logic.ts').unlink()
        def execute(request,**kwargs):
            target=self.root/'logic.ts';target.write_text('export const value = 4;')
            task=Task(request);task.route=Route.IMPLEMENTATION
            task.transition(TaskState.ROUTING);task.transition(TaskState.EXECUTING)
            task.tool_executions=[{'tool':'workspace_write','status':'success','output':{'path':str(target),
                'net_changed':True,'post_apply_hash':hashlib.sha256(target.read_bytes()).hexdigest()}},
                {'tool':'workspace_read','status':'success','output':{'path':str(target)}}]
            task.transition(TaskState.COMPLETED)
            return task,'Restored the missing artifact'
        with patch.object(api.runtime,'execute',side_effect=execute) as implementation, \
             patch.object(api,'_replan_task',side_effect=AssertionError('Do not replan retained work')):
            self.assertEqual('Coding Task completed.',api._run_managed_task('t',str(self.root)))
        self.assertEqual(1,implementation.call_count)
        self.assertEqual('COMPLETED',api.db.coding_task('t')['status'])

    def test_approved_legacy_scope_is_bound_without_recalling_the_planner(self):
        (self.root/'app.js').write_text('export const app = true;')
        plan={'schema_version':1,'original_goal':'Update app.js','scope':{'allowed':['app.js'],'forbidden':[]},
              'assumptions':[],'phases':[{'id':'p1','goal':'Update app.js','status':'pending',
                  'execution_mode':'IMPLEMENTATION','done':['app.js exports app'],'verify':[],
                  'dependencies':[],'risks':[]}]}
        api.db.create_coding_task('legacy-manifest','fixture','Update app.js','QUEUED','NONE',plan,time.time())
        with patch.object(api,'_generate_plan',side_effect=AssertionError('legacy scope is already approved')):
            prepared,artifact=api._prepare_approved_plan_artifact(
                'legacy-manifest',api.db.coding_task('legacy-manifest'),plan,str(self.root))
        expected=[('app.js','modify','p1')]
        for value in (artifact,prepared):
            self.assertEqual(expected,[(item['path'],item['action'],item['owner_phase'])
                                       for item in value['file_manifest']])

    def verifier(self):
        (self.root/'package.json').write_text('{"devDependencies":{"vite":"1.0.0"}}')
        (self.root/'vite.config.ts').write_text('export default {};')
        (self.root/'index.html').write_text('<h1>fixture</h1>')
        (self.root/'main.ts').write_text('export const value=1;')
        contract=self.root/'checks.json';contract.write_text('{"entrypoint":"index.html","checks":[]}')
        runner=Path(api.__file__).with_name('coding_verification_runner.cjs')
        command=['node',str(runner),str(contract),str(self.root),'unused-runtime']
        phase={'id':'p1','goal':'Verify build','done':['Build compiles'],'verify':['Typecheck compiles'],
               'execution_mode':'VERIFICATION_ONLY','host_verification':True,
               'host_criteria':{'DONE:1':'Build compiles','VERIFY:1':'Typecheck compiles'},'verification_command':command,
               'verification_targets':['index.html','main.ts']}
        return phase,command

    def host_result(self, command, **changes):
        payload={'passed_ids':['DONE:1','VERIFY:1'],'test_runs':[],'browser_checks':0,'failures':[],**changes}
        result=subprocess.CompletedProcess(command,0,'OLCR_VERIFICATION_RESULT='+json.dumps(payload),'')
        result.project_build={'source_isolated':True,'install_passed':True,'build_executed':True,
                              'build_passed':True,'typecheck_executed':True,'typecheck_passed':True}
        return result

    def test_measured_build_and_typecheck_reach_final_acceptance(self):
        phase,command=self.verifier()
        with patch.object(api,'acceptance_command',return_value=command), \
             patch.object(api,'run_acceptance',return_value=self.host_result(command)):
            execution,result=api._run_read_only_verification(phase,str(self.root))
        self.assertTrue(result['passed'],result)
        report=api._report_from_execution(phase,0,execution,'Verification executed')
        self.assertEqual('PASS',report['status'])
        self.assertEqual([],report['errors'])
        self.assertEqual('PASS',report['build_pass'])
        self.assertEqual('PASS',report['typecheck_pass'])
        task={'requirements':{'required_verification':['build','typecheck']}}
        self.assertEqual([],api._final_acceptance_gaps(task,[report]))
        merged=self.report();api._merge_verification_execution(merged,execution)
        self.assertEqual('PASS',merged['build_pass'])

    def test_invalid_nested_result_becomes_executor_failure(self):
        phase,command=self.verifier()
        for changes in ({'test_runs':'bad'},{'test_runs':[{'failures':'bad'}]},
                        {'browser_observations':[None]},{'passed_ids':[{}]}):
            with self.subTest(changes=changes), patch.object(api,'acceptance_command',return_value=command), \
                 patch.object(api,'run_acceptance',return_value=self.host_result(command,**changes)):
                _,result=api._run_read_only_verification(phase,str(self.root))
            self.assertFalse(result['passed'])
            self.assertTrue(result['executor_error'])
            self.assertEqual([],result['test_runs'])

    def test_isolated_verification_never_rolls_back_an_unrelated_source_edit(self):
        phase,command=self.verifier()
        def changed(*args):
            (self.root/'main.ts').write_text('export const userEdit=2;')
            return self.host_result(command)
        with patch.object(api,'acceptance_command',return_value=command),patch.object(api,'run_acceptance',side_effect=changed):
            _,result=api._run_read_only_verification(phase,str(self.root))
        self.assertFalse(result['passed'])
        self.assertEqual(['main.ts'],result['changed_targets'])
        self.assertEqual('export const userEdit=2;',(self.root/'main.ts').read_text())

    def test_isolated_timeout_preserves_an_unrelated_source_edit(self):
        phase,command=self.verifier()
        def timeout(*args):
            (self.root/'main.ts').write_text('export const userEdit=3;')
            raise subprocess.TimeoutExpired(command,120)
        with patch.object(api,'acceptance_command',return_value=command),patch.object(api,'run_acceptance',side_effect=timeout):
            _,result=api._run_read_only_verification(phase,str(self.root))
        self.assertFalse(result['passed'])
        self.assertEqual('export const userEdit=3;',(self.root/'main.ts').read_text())

    def test_dependency_source_error_and_network_error_keep_different_owners(self):
        _,command=self.verifier()
        for category,executor in [('VERSION_NOT_FOUND',False),('PACKAGE_NOT_FOUND',False),
                                  ('PACKAGE_SPEC_INVALID',False),('DNS_OR_NETWORK_FAILURE',True)]:
            with self.subTest(category=category),patch('olcr_api.node_project_verification.PackageManagerExecutor.install',
                    side_effect=PackageManagerError('fixture failure',failure_class=category)):
                result=run_vite_acceptance(command,str(self.root),10,lambda *args: self.fail('No build after install failure'))
            payload=json.loads(result.stdout.split('=',1)[1])
            self.assertEqual(executor,payload['executor_error'])
            self.assertIn(category,payload['failures'][0]['error'])
            self.assertFalse(result.project_build['build_executed'])
            if not executor:
                self.assertEqual('package.json',payload['failures'][0]['source_locations'][0]['path'])

    def test_delayed_worker_error_cannot_reopen_a_completed_or_archived_task(self):
        for identifier,status,archived in [('done','COMPLETED',False),('archived','RESUMABLE',True)]:
            with self.subTest(identifier=identifier):
                api.db.create_coding_task(identifier,'c','fixture',status,'NONE',None,time.time())
                api.db.update_coding_task(identifier,archived=archived)
                before=copy.deepcopy(api.db.coding_task(identifier))
                api._transition_resumable(identifier,'SCHEDULER','RECOVERABLE_INTERNAL',exc=OSError('late failure'))
                after=api.db.coding_task(identifier)
                self.assertEqual(before['status'],after['status'])
                self.assertEqual(before['recovery_reason'],after['recovery_reason'])

    def test_telemetry_failure_preserves_committed_completion(self):
        task=self.artifact_task()
        with patch.object(api,'_finish_coding_telemetry',side_effect=OSError('telemetry disk error')):
            self.assertEqual('Coding Task completed.',api._complete_task('t',task))
        self.assertEqual('COMPLETED',api.db.coding_task('t')['status'])
