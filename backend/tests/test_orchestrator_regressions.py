"""Execution-boundary regressions from the lunch-roulette stop/retry reports."""
import copy
import hashlib
import json
import subprocess
import sqlite3
import tempfile
import time
import threading
import unittest
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import olcr_api.app as api
from olcr_api.coding_tasks import evaluate_phase
from olcr_api.db import Database
from olcr_api.models import Task, TaskState
from olcr_api.ollama import ModelFailure


def _project_test_source_review(messages, source_path):
    if 'You assign ownership for exactly one failed project test' not in messages[0]['content']:
        return None
    payload=json.loads(messages[-1]['content'].split('\n')[-1])
    fragment=payload['current_application_sources'].get(source_path)
    if not isinstance(fragment,str) or not fragment:
        return {'text':json.dumps({'verdict':'INCONCLUSIVE','reason':'No current source quote is available.',
            'requirement':'','source_fragment':'','test_fragment':''})}
    return {'text':json.dumps({'verdict':'SOURCE_DEFECT','reason':'The current source causes the registered project test to fail.',
        'requirement':payload['original_request'],'source_fragment':fragment,'test_fragment':''})}


def _verification_schema_ids(schema):
    result=set()
    def visit(value):
        if isinstance(value,dict):
            ids=(value.get('properties') or {}).get('ids')
            if isinstance(ids,dict):
                result.update((ids.get('items') or {}).get('enum') or [])
            for child in value.values():visit(child)
        elif isinstance(value,list):
            for child in value:visit(child)
    visit(schema or {})
    return sorted(result)


def _verification_schema_allows_code(schema):
    def visit(value):
        if isinstance(value,dict):
            if 'code' in (value.get('properties') or {}):return True
            return any(visit(child) for child in value.values())
        if isinstance(value,list):return any(visit(child) for child in value)
        return False
    return visit(schema or {})


class OrchestratorRegressions(unittest.TestCase):
    def test_internal_verification_contract_failure_is_not_project_test_failure(self):
        command=['node','--test','app.test.js']
        report={'status':'FAIL','test_fail':['old verifier TypeError'],
            'typed_execution_summary':{'operations':[{'tool':'verification_command','status':'failed',
                'output':{'returncode':1,'failures':[{'ids':['DONE:1'],'contract_error':True,
                    'error':'old verifier TypeError'}]}}]}}
        self.assertFalse(api._reported_project_test_failure(report,command))

    def test_real_project_test_failure_still_triggers_reobservation(self):
        command=['node','--test','app.test.js']
        report={'status':'FAIL','test_fail':['AssertionError: expected 2 to equal 1'],
            'typed_execution_summary':{'operations':[{'tool':'verification_command','status':'failed',
                'output':{'returncode':1,'failures':[{'ids':['TEST:1'],'error':'AssertionError'}]}}]}}
        self.assertTrue(api._reported_project_test_failure(report,command))

    def test_a_known_verification_command_cannot_be_skipped_for_empty_verify_prose(self):
        expectations=api._phase_execution_expectations({'execution_mode':'IMPLEMENTATION_AND_VERIFICATION',
            'done':['Tests pass.'],'verify':[],'verification_command':['node','--test']})
        self.assertTrue(expectations['requires_verification'])

    def test_legacy_harness_failure_is_reobserved_before_an_early_replan(self):
        sources={'index.html':'<h1>App</h1>','app.js':'window.app=1;',
            'app.test.js':"describe('app',()=>it('value',()=>expect(1).toBe(1)));"}
        for path,source in sources.items():(self.root/path).write_text(source)
        phase={**self.phase,'goal':'Repair project tests','done':['The project tests run'],
            'verify':['Tests pass'],'verification_command':['node','--test','app.test.js'],
            'verification_targets':list(sources)}
        plan={**self.plan,'scope':{'allowed':list(sources),'forbidden':[]},'phases':[phase],
            'file_manifest':[{'path':p,'action':'modify','owner_phase':'p1'} for p in sources]}
        api.db.create_coding_task('legacy-harness','conversation','Repair the project tests and run tests.',
            'QUEUED','NONE',plan,time.time())
        api.db.add_coding_phase_report('legacy-harness','p1',2,{'phase_id':'p1','attempt':2,
            'plan_revision':0,'recovery_epoch':0,'status':'FAIL','test_fail':['old truncated error'],
            'errors':['verification command failed'],'changed_files':[],
            'typed_execution_summary':{'operations':[],'state':'failed'},
            'manager_decision':{'decision':'REPLAN_REQUIRED'}},'PASS',time.time())
        with patch.object(api.runtime,'execute',side_effect=RuntimeError('current test repair')) as execute, \
             patch.object(api,'_replan_task',side_effect=AssertionError('Legacy test setup must be rechecked')):
            with self.assertRaisesRegex(RuntimeError,'current test repair'):
                api._run_managed_task('legacy-harness',str(self.root))
        self.assertEqual(['app.test.js'],[e['path'] for e in execute.call_args.kwargs['managed_context']['implementation_plan_artifact']['file_manifest']])

        diagnostics=execute.call_args.kwargs['managed_context']['repair_diagnostics']
        self.assertTrue(diagnostics['project_test_setup_repair'])
        self.assertIn('describe is not defined',diagnostics['verification_failures'][0]['detail'])
        self.assertEqual('app.test.js',diagnostics['verification_failures'][0]['source_locations'][0]['path'])

        row=api.db.coding_phase_reports('legacy-harness')[-1]
        api.db.update_coding_phase_report(row['id'],{**row['structured_report'],'project_test_defect':True},'PASS')
        with patch.object(api.runtime,'execute',side_effect=RuntimeError('resumed test-only repair')) as execute, \
             patch.object(api,'_replan_task',side_effect=AssertionError('Saved ownership must be reobserved')):
            with self.assertRaisesRegex(RuntimeError,'resumed test-only repair'):
                api._run_managed_task('legacy-harness',str(self.root))
        self.assertEqual(['app.test.js'],[e['path'] for e in execute.call_args.kwargs['managed_context']['implementation_plan_artifact']['file_manifest']])

        (self.root/'app.test.js').write_text("require('node:test')('now passes',()=>require('node:assert/strict').equal(1,1));")
        with patch.object(api.runtime,'execute',side_effect=AssertionError('A fixed test must be verified without writes')), \
             patch.object(api,'_review_phase',return_value='BLOCKED'), \
             patch.object(api,'_replan_task',side_effect=AssertionError('Do not replan a passing test')):
            api._run_managed_task('legacy-harness',str(self.root))
        self.assertTrue(api.db.coding_phase_reports('legacy-harness')[-1]['structured_report']['test_pass'])

        (self.root/'app.test.js').write_text("require('node:test')('new assertion',()=>require('node:assert/strict').equal(1,2));")
        row=api.db.coding_phase_reports('legacy-harness')[-1]
        api.db.update_coding_phase_report(row['id'],{**row['structured_report'],'status':'FAIL',
            'project_test_defect':True,'test_fail':['New failing assertion'],
            'manager_decision':{'decision':'REPLAN_REQUIRED'}},'PASS')
        api.db.update_coding_task('legacy-harness',status='QUEUED')
        current_failure=(None,{'passed':False,'output':'AssertionError: expected one, received two',
            'failures':[{'name':'new assertion','detail':'expected one, received two'}],
            'test_runs':[{'failures':[{'name':'new assertion','detail':'expected one, received two'}]}],
            'test_diagnosis':{'verdict':'INCONCLUSIVE','reason':'An assertion failure alone does not assign ownership.'},
            'executor_error':False})
        with patch.object(api,'_run_read_only_verification',return_value=current_failure), \
             patch.object(api.runtime,'execute',side_effect=AssertionError('Unattributed failure grants no source writes')), \
             patch.object(api,'_review_project_test_failures',return_value={'verdict':'INCONCLUSIVE','reason':'Not attributable'}) as review, \
             patch.object(api,'_replan_task',side_effect=AssertionError('Do not replan an unattributed failure')):
            api._run_managed_task('legacy-harness',str(self.root))
        self.assertEqual(1,review.call_count)
        self.assertEqual('VERIFICATION_CONTRACT_INVALID',api.db.coding_task('legacy-harness')['recovery_reason'])

    def test_first_node_assertion_failure_is_reviewed_before_any_repair_or_replan(self):
        sources={'index.html':'<h1>App</h1>','app.js':'window.app=1;',
            'app.test.js':"const {test}=require('node:test');test('app',()=>{});"}
        for path,source in sources.items():(self.root/path).write_text(source)
        phase={**self.phase,'goal':'Implement and verify the requested behavior',
            'done':['The requested behavior is implemented'],'verify':['The project tests pass'],
            'verification_command':['node','--test','app.test.js'],'verification_targets':list(sources)}
        plan={**self.plan,'scope':{'allowed':list(sources),'forbidden':[]},'phases':[phase],
            'file_manifest':[{'path':p,'action':'modify','owner_phase':'p1'} for p in sources]}
        api.db.create_coding_task('first-test-failure','conversation','Implement the requested behavior and run the project tests.',
            'QUEUED','NONE',plan,time.time())
        api.db.add_coding_phase_report('first-test-failure','p1',1,{'phase_id':'p1','attempt':1,
            'plan_revision':0,'recovery_epoch':0,'status':'FAIL','test_fail':[],
            'errors':['verification command failed'],'changed_files':[],
            'typed_execution_summary':{'operations':[{'tool':'verification_command','status':'failed',
                'input':{'command':phase['verification_command']},'output':{'returncode':1}}],'state':'failed'},
            'manager_decision':{'decision':'RETRY','retry_channel':'IMPLEMENTATION'}},'PASS',time.time())
        current_failure=(None,{'passed':False,'output':'AssertionError: expected one, received zero',
            'failures':[{'name':'app assertion','detail':'Expected one, received zero'}],
            'test_runs':[{'failures':[{'name':'app assertion','detail':'Expected one, received zero'}]}],
            'test_diagnosis':{'verdict':'INCONCLUSIVE','reason':'An assertion failure alone does not assign ownership.'},
            'executor_error':False})
        with patch.object(api,'_run_read_only_verification',return_value=current_failure) as verify, \
             patch.object(api,'_review_project_test_failures',return_value={'verdict':'INCONCLUSIVE',
                'reason':'The current assertion cannot be tied to a requirement or source defect.'}) as review, \
             patch.object(api.runtime,'execute',side_effect=AssertionError('No write is authorized before review')) as execute, \
             patch.object(api,'_replan_task',side_effect=AssertionError('An inconclusive test failure must not replan')):
            result=api._run_managed_task('first-test-failure',str(self.root))
        self.assertIn('attributable requirement/source review',result)
        self.assertEqual(1,verify.call_count)
        self.assertEqual(1,review.call_count)
        execute.assert_not_called()
        self.assertEqual('VERIFICATION_CONTRACT_INVALID',
            api.db.coding_task('first-test-failure')['recovery_reason'])

    def test_python_project_test_failure_uses_the_same_pre_repair_review_gate(self):
        (self.root/'tests').mkdir()
        (self.root/'tests/test_logic.py').write_text('def test_value(): assert 1 == 2\n')
        phase={**self.phase,'goal':'Implement and verify the requested behavior',
            'done':['The requested behavior is implemented'],'verify':['The project tests pass'],
            'verification_command':['python3','-m','unittest','tests.test_logic'],
            'verification_targets':['tests/test_logic.py']}
        plan={**self.plan,'scope':{'allowed':['tests/test_logic.py'],'forbidden':[]},'phases':[phase],
            'file_manifest':[{'path':'tests/test_logic.py','action':'modify','owner_phase':'p1'}]}
        api.db.create_coding_task('python-test-failure','conversation','Implement the requested behavior and run the project tests.',
            'QUEUED','NONE',plan,time.time())
        api.db.add_coding_phase_report('python-test-failure','p1',1,{'phase_id':'p1','attempt':1,
            'plan_revision':0,'recovery_epoch':0,'status':'FAIL','test_fail':['AssertionError: 1 != 2'],
            'errors':['verification command failed'],'changed_files':[],
            'typed_execution_summary':{'operations':[],'state':'failed'},
            'manager_decision':{'decision':'RETRY','retry_channel':'IMPLEMENTATION'}},'PASS',time.time())
        current_failure=(None,{'passed':False,'output':'AssertionError: 1 != 2',
            'failures':[],'test_runs':[{'command':['python3','-m','unittest','tests.test_logic'],
                'returncode':1,'failures':[],'output':'AssertionError: 1 != 2'}],
            'test_diagnosis':{'verdict':'INCONCLUSIVE'},'executor_error':False})
        with patch.object(api,'_run_read_only_verification',return_value=current_failure) as verify, \
             patch.object(api,'_review_project_test_failures',return_value={'verdict':'INCONCLUSIVE',
                'reason':'The assertion cannot be attributed to the request or implementation.'}) as review, \
             patch.object(api.runtime,'execute',side_effect=AssertionError('No implementation write before review')) as execute, \
             patch.object(api,'_replan_task',side_effect=AssertionError('An unattributed test failure must not replan')):
            api._run_managed_task('python-test-failure',str(self.root))
        self.assertEqual(1,verify.call_count)
        self.assertEqual(1,review.call_count)
        execute.assert_not_called()

    def test_project_harness_repair_dispatch_can_only_write_the_approved_test(self):
        sources={'index.html':'<h1>App</h1>','app.js':'window.start=()=>1;',
                 'app.test.js':"const {test,expect}=require('node:test');test('app',()=>expect(1).toBe(1));"}
        for path,source in sources.items():(self.root/path).write_text(source)
        phase={**self.phase,'goal':'Final acceptance','execution_mode':'IMPLEMENTATION_AND_VERIFICATION',
            'verification_repair_allowed':True,'host_project_test_defect':True,
            'host_project_test_setup_repair':True,
            'host_project_test_repair_paths':['app.test.js'],
            'done':['All tests pass.'],'verify':['The project tests pass.']}
        plan={**self.plan,'file_manifest':[{'path':p,'action':'modify'} for p in sources],
            'scope':{'allowed':list(sources),'forbidden':[]},'phases':[phase]}
        api.db.create_coding_task('harness-scope','conversation','Build the app and run tests.',
            'QUEUED','NONE',plan,time.time())
        with patch.object(api.runtime,'execute',side_effect=RuntimeError('inspect dispatch')) as execute:
            with self.assertRaisesRegex(RuntimeError,'inspect dispatch'):
                api._run_managed_task('harness-scope',str(self.root))
        context=execute.call_args.kwargs['managed_context']
        self.assertEqual(['app.test.js'],[e['path'] for e in context['implementation_plan_artifact']['file_manifest']])
        self.assertTrue(context['repair_diagnostics']['source_bound_repair'])
        self.assertTrue(context['repair_diagnostics']['project_test_setup_repair'])
        self.assertEqual(sources['app.js'],(self.root/'app.js').read_text())

    def test_reviewed_test_oracle_repair_is_not_mislabeled_as_test_setup_failure(self):
        sources={'index.html':'<h1>App</h1>','app.js':'window.value=1;',
            'app.test.js':"const {test}=require('node:test');test('value',()=>require('node:assert/strict').equal(window.value,2));"}
        for path,source in sources.items():(self.root/path).write_text(source)
        phase={**self.phase,'goal':'Implement and verify the requested behavior',
            'done':['The requested behavior is implemented'],'verify':['The project tests pass'],
            'verification_command':['node','--test','app.test.js'],'verification_targets':list(sources)}
        plan={**self.plan,'scope':{'allowed':list(sources),'forbidden':[]},'phases':[phase],
            'file_manifest':[{'path':p,'action':'modify','owner_phase':'p1'} for p in sources]}
        api.db.create_coding_task('test-oracle-repair','conversation','Display the value 1 and run the project tests.',
            'QUEUED','NONE',plan,time.time())
        api.db.add_coding_phase_report('test-oracle-repair','p1',1,{'phase_id':'p1','attempt':1,
            'plan_revision':0,'recovery_epoch':0,'status':'FAIL','test_fail':['Expected 2, received 1'],
            'errors':['verification command failed'],'changed_files':[],
            'typed_execution_summary':{'operations':[],'state':'failed'},
            'manager_decision':{'decision':'RETRY','retry_channel':'IMPLEMENTATION'}},'PASS',time.time())
        current_failure=(None,{'passed':False,'output':'AssertionError: Expected 2, received 1',
            'failures':[{'name':'value assertion','detail':'Expected 2, received 1'}],
            'test_runs':[{'failures':[{'name':'value assertion','detail':'Expected 2, received 1'}]}],
            'test_diagnosis':{'verdict':'INCONCLUSIVE','reason':'An assertion failure alone does not assign ownership.'},
            'executor_error':False})
        with patch.object(api,'_run_read_only_verification',return_value=current_failure), \
             patch.object(api,'_review_project_test_failures',return_value={'verdict':'CHECK_DEFECT',
                'reason':'The expected value contradicts the original requirement.',
                'repair_paths':['app.test.js'],'evidence_origin':'CURRENT_PROJECT_TEST_EXECUTION',
                'project_test_setup_repair':False}) as review, \
             patch.object(api.runtime,'execute',side_effect=RuntimeError('inspect reviewed repair scope')) as execute:
            with self.assertRaisesRegex(RuntimeError,'inspect reviewed repair scope'):
                api._run_managed_task('test-oracle-repair',str(self.root))
        self.assertEqual(1,review.call_count)
        context=execute.call_args.kwargs['managed_context']
        self.assertEqual(['app.test.js'],[entry['path'] for entry in
            context['implementation_plan_artifact']['file_manifest']])
        self.assertIs(context['repair_diagnostics']['project_test_setup_repair'],False)
        request=execute.call_args.args[0]
        self.assertIn('PROJECT_TEST_ORACLE_REPAIR',request)
        self.assertNotIn('PROJECT_TEST_SETUP_REPAIR:',request)

    def test_reviewed_source_defect_grants_only_the_matching_manifest_file(self):
        sources={'index.html':'<output id="value"></output><script src="app.js"></script>',
            'app.js':'document.querySelector("#value").textContent="0";',
            'unrelated.js':'window.unrelated="keep";',
            'app.test.js':"const {test}=require('node:test');test('value',()=>{});"}
        for path,source in sources.items():(self.root/path).write_text(source)
        phase={**self.phase,'goal':'Implement and verify the requested behavior',
            'done':['The requested behavior is implemented'],'verify':['The project tests pass'],
            'verification_command':['node','--test','app.test.js'],'verification_targets':list(sources)}
        plan={**self.plan,'scope':{'allowed':list(sources),'forbidden':[]},'phases':[phase],
            'file_manifest':[{'path':p,'action':'modify','owner_phase':'p1'} for p in sources]}
        api.db.create_coding_task('reviewed-source-repair','conversation','Display the value 1 and run the project tests.',
            'QUEUED','NONE',plan,time.time())
        api.db.add_coding_phase_report('reviewed-source-repair','p1',1,{'phase_id':'p1','attempt':1,
            'plan_revision':0,'recovery_epoch':0,'status':'FAIL','test_fail':['Expected 1, received 0'],
            'errors':['verification command failed'],'changed_files':[],
            'typed_execution_summary':{'operations':[],'state':'failed'},
            'manager_decision':{'decision':'RETRY','retry_channel':'IMPLEMENTATION'}},'PASS',time.time())
        current_failure=(None,{'passed':False,'output':'AssertionError: Expected 1, received 0',
            'failures':[{'name':'value assertion','detail':'Expected 1, received 0'}],
            'test_runs':[{'failures':[{'name':'value assertion','detail':'Expected 1, received 0'}]}],
            'test_diagnosis':{'verdict':'INCONCLUSIVE','reason':'An assertion failure alone does not assign ownership.'},
            'executor_error':False})
        with patch.object(api,'_run_read_only_verification',return_value=current_failure), \
             patch.object(api,'_review_project_test_failures',return_value={'verdict':'SOURCE_DEFECT',
                'reason':'Current source renders zero but the original request requires one.',
                'source_fragment':'textContent="0"','repair_paths':['app.js']}) as review, \
             patch.object(api.runtime,'execute',side_effect=RuntimeError('inspect reviewed source scope')) as execute:
            with self.assertRaisesRegex(RuntimeError,'inspect reviewed source scope'):
                api._run_managed_task('reviewed-source-repair',str(self.root))
        self.assertEqual(1,review.call_count)
        context=execute.call_args.kwargs['managed_context']
        self.assertEqual(['app.js'],[entry['path'] for entry in
            context['implementation_plan_artifact']['file_manifest']])
        self.assertEqual(['app.js'],context['repair_diagnostics']['reviewed_source_paths'])
        self.assertEqual('window.unrelated="keep";',(self.root/'unrelated.js').read_text())

    def test_host_project_test_source_review_returns_exact_repair_paths(self):
        sources={'index.html':'<output id="value"></output>','app.js':'window.value=0;',
            'unrelated.js':'window.keep=true;','tests/test_logic.py':'def test_value(): assert True\n'}
        for path,source in sources.items():
            target=self.root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(source)
        original='Display the value 1.'
        api.db.create_coding_task('host-source-scope','conversation',original,'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':[],'verify':[original],
            'verification_command':['python3','-m','unittest','tests.test_logic'],
            'verification_targets':list(sources)}
        plan={'file_manifest':[{'path':path} for path in sources]}
        current=(None,{'passed':False,'executor_error':False,'output':'AssertionError: expected 1, got 0',
            'failures':[],'test_runs':[{'command':phase['verification_command'],'returncode':1,
                'failures':[],'output':'AssertionError: expected 1, got 0'}]})
        with patch.object(api,'_run_read_only_verification',return_value=current), \
             patch.object(api,'_review_project_test_failures',return_value={'verdict':'SOURCE_DEFECT',
                'reason':'Current source value contradicts the original request.',
                'requirement':original,'source_fragment':'window.value=0;','repair_paths':['app.js']}) as review:
            result=api._host_verification_phase('host-source-scope',phase,plan,str(self.root))
        self.assertEqual(1,review.call_count)
        self.assertTrue(result['host_source_defect'])
        self.assertEqual(['app.js'],result['host_source_defect_repair_paths'])

    def test_failed_project_harness_repair_keeps_the_same_plan_without_replanning(self):
        from dataclasses import replace
        from olcr_api.runtime import Runtime
        sources={'index.html':'<h1>App</h1>','app.js':'window.start=()=>1;',
                 'app.test.js':"const {test,expect}=require('node:test');test('app',()=>expect(1).toBe(1));"}
        for path,source in sources.items():(self.root/path).write_text(source)
        class Model:
            def generate(self,*args,**kwargs):
                return {'text':json.dumps({'change_required':True,'source_inspected':True,'condition_evaluated':True,
                    'reason_code':'Still broken test repair','operations':[{'op':'write','path':'app.test.js',
                    'content':sources['app.test.js']+'\n// attempted repair'}],'verification':'Host verifies'})}
        phase={**self.phase,'goal':'Final acceptance','execution_mode':'VERIFICATION_ONLY',
            'host_verification':True,'host_verification_version':3,'verification_repair_allowed':True,
            'done':['All tests pass.'],'verify':[]}
        plan={**self.plan,'file_manifest':[{'path':p,'action':'modify'} for p in sources],
            'scope':{'allowed':list(sources),'forbidden':[]},'phases':[phase]}
        api.db.create_coding_task('harness-no-replan','conversation','Build the app and run tests.',
            'QUEUED','NONE',plan,time.time())
        api.runtime=Runtime(replace(api.settings,allowed_roots=[str(self.root)]),api.db,api.retrieval,Model())
        with patch.object(api,'_review_phase',return_value='REPLAN_REQUIRED'), \
             patch.object(api,'_replan_task',side_effect=AssertionError('Harness failures must not grow the plan')):
            api._run_managed_task('harness-no-replan',str(self.root))
        saved=api.db.coding_task('harness-no-replan')
        self.assertEqual('RESUMABLE',saved['status'])
        self.assertEqual('PROJECT_TEST_REPAIR_EXHAUSTED',saved['recovery_reason'])
        self.assertEqual(0,saved['replan_count'])
        self.assertEqual(['p1'],[p['id'] for p in saved['plan']['phases']])
        api.db.update_coding_task('harness-no-replan',status='QUEUED')
        with patch.object(api.runtime,'execute',side_effect=RuntimeError('resumed harness repair')), \
             patch.object(api,'_replan_task',side_effect=AssertionError('Resume must not grow the plan')):
            with self.assertRaisesRegex(RuntimeError,'resumed harness repair'):
                api._run_managed_task('harness-no-replan',str(self.root))
        self.assertEqual(0,api.db.coding_task('harness-no-replan')['replan_count'])

    def test_shared_hook_failure_is_focused_once_without_dropping_affected_tests(self):
        hook={'path':'logic.test.js','line':20,'source_line':'history.length=0;'}
        failures=[{'name':f'case {i}','detail':f"location: test:{i}\nfailureType: 'hookFailed'\n  error: 'history is not defined'\n  code: 'ERR_TEST_FAILURE'",
            'source_locations':[{'path':'logic.test.js','line':i},hook]} for i in range(8)]
        report={'typed_execution_summary':{'operations':[{'tool':'verification_command','output':{'test_runs':[{'failures':failures}]}}]}}
        context=api._bounded_review_context(report)
        self.assertEqual(1,len(context['verification_failures']))
        self.assertEqual([f'case {i}' for i in range(8)],context['verification_failures'][0]['affected_tests'])
        self.assertIn(hook,context['verification_failures'][0]['source_locations'])
        self.assertNotIn('affected_tests',failures[0],'Stored execution evidence remains unchanged')
        different=copy.deepcopy(report)
        different['typed_execution_summary']['operations'][0]['output']['test_runs'][0]['failures'][1]['source_locations'][1]={**hook,'line':21}
        self.assertEqual(2,len(api._bounded_review_context(different)['verification_failures']))

    def test_a_different_module_loading_exception_is_not_a_stalled_repair(self):
        def report(error):
            return {'accepted_mutations':1,'typed_execution_summary':{'operations':[
                {'tool':'verification_command','output':{'failures':[{'ids':['REQUIRED_TESTS']}],
                 'test_runs':[{'registered_tests':0,'passed':0,'failures':[
                     {'name':'Node.js module loading','detail':'error: '+error+'\nsource'},
                     {'name':'logic.test.js','detail':'test failed'}]}]}}]}}
        before,after=report('document is not defined'),report('beforeEach is not defined')
        self.assertFalse(api._verification_repair_stalled([before,after]))
        self.assertTrue(api._verification_repair_stalled([after,copy.deepcopy(after)]))

    def test_stalled_repair_history_keeps_measured_assertions_and_is_bounded(self):
        reports=[{'attempt':i,'plan_revision':1,'changed_files':['logic.test.js'],
            'manager_decision':{'diagnosis':{'specific_fix':'UNVERIFIED ADVICE'}},
            'typed_execution_summary':{'operations':[{'tool':'verification_command','output':{'test_runs':[
                {'failures':[{'name':'history oracle','detail':"error: expected "+str(i)+'\n  code: ERR_ASSERTION',
                    'source_locations':[{'path':'logic.test.js','line':12,'source_line':'a'*2000}]}]}]}}]}} for i in range(20)]
        history=api._failed_repair_history(reports)
        self.assertEqual([17,18,19],[record['attempt'] for record in history])
        self.assertIn('expected 19',history[-1]['failures'][0]['error'])
        self.assertEqual(300,len(history[-1]['failures'][0]['assertions'][0]['source_line']))
        self.assertNotIn('UNVERIFIED ADVICE',json.dumps(history))
        self.assertEqual([],api._failed_repair_history([{'typed_execution_summary':{'operations':[]}}]))

    def test_planning_repairs_serialization_conditions_once_without_accepting_them(self):
        malformed=copy.deepcopy(self.plan)
        malformed['phases'][0]['done']=['],','verify:[']
        for task_id,repaired,accepted in (('criteria-valid',self.plan,True),('criteria-invalid',malformed,False)):
            with self.subTest(accepted=accepted):
                api.db.create_coding_task(task_id,'conversation','Fix probe','PLANNING','NONE',None,time.time())
                with patch.object(api,'_model_text',side_effect=[json.dumps(malformed),json.dumps(repaired)]) as model:
                    result,_=api._generate_plan(task_id,'Fix probe','Plan the repair','QWEN_PLANNING')
                self.assertEqual(2,model.call_count)
                self.assertEqual(accepted,result is not None)
                if result:
                    self.assertEqual(['probe.py exists'],result['phases'][0]['done'])
                self.assertIn('observable condition',model.call_args.args[4][-1]['content'])

    def test_persisted_serialization_criteria_cannot_reenter_the_implementation_loop(self):
        plan=copy.deepcopy(self.plan)
        plan['phases'][0]['done']=['],','verify:[']
        api.db.create_coding_task('plan-fragments','conversation','Fix probe','RUNNING','NONE',plan,time.time())
        with patch.object(api,'_replan_task',return_value='bounded criteria repair') as repair, patch.object(api.runtime,'execute') as execute:
            result=api._run_managed_task('plan-fragments',str(self.root))
        self.assertEqual('bounded criteria repair',result)
        execute.assert_not_called()
        self.assertIn('Invalid plan criteria',repair.call_args.args[3])
        self.assertEqual(set(),repair.call_args.args[2])

    def test_node_assertion_is_not_hidden_by_browser_wrapper_with_long_success_tap(self):
        actual={'name':'history order','detail':'Expected latest dishes, actual reversed',
                'source_locations':[{'path':'logic.test.js','line':9}]}
        wrapper={'ids':['DONE:1'],'error':'Error: node:test failed: '+('ok successful test\n'*150)}
        browser={'ids':['VERIFY:2'],'error':'page error','browser_errors':[{'message':'module is not defined'}]}
        report={'test_fail':[wrapper['error']],'typed_execution_summary':{'operations':[
            {'tool':'verification_command','output':{'failures':[wrapper,browser],
                'test_runs':[{'registered_tests':12,'passed':11,'failed':1,'failures':[actual]}]}}]}}
        context=api._bounded_review_context(report)
        self.assertEqual([actual],context['verification_failures'])
        self.assertIn('history order',context['test_fail'][0])
        self.assertNotIn('ok successful test',context['test_fail'][0])
        self.assertEqual([['VERIFY:2']],[f['ids'] for f in context['acceptance_failures']])
        self.assertEqual(wrapper['error'],report['test_fail'][0])

    def test_replan_retains_omitted_verified_prefix_and_rebuilds_graph(self):
        completed={'id':'created','goal':'Create files','status':'pass','done':['Files exist'],
            'verify':['Typed readback'],'dependencies':[],'risks':[],'execution_mode':'IMPLEMENTATION'}
        repair={'id':'repair','goal':'Repair assertion','status':'pending','done':['Assertion passes'],
            'verify':['Run tests'],'dependencies':[],'risks':[],'execution_mode':'IMPLEMENTATION'}
        current=api.derive_task_graph({**self.plan,'phases':[completed]})
        current['tasks'][0]['required_context']=['Persisted verified API']
        current['tasks'][0]['required_mcp']=['playwright']
        proposal=api.derive_task_graph({**self.plan,'phases':[repair]})
        retained=api._retain_completed_replan_phases(proposal,current,{'created'})
        self.assertEqual(['created','repair'],[p['id'] for p in retained['phases']])
        self.assertEqual(completed,retained['phases'][0])
        self.assertEqual({'created','repair'},{t['phase_id'] for t in retained['tasks']})
        self.assertEqual(current['tasks'][0],retained['tasks'][0])
        self.assertEqual(['repair'],[p['id'] for p in proposal['phases']])
        self.assertEqual(retained,api._retain_completed_replan_phases(retained,current,{'created'}))

    def test_replan_does_not_invent_completion_or_hide_changed_contract(self):
        original={'id':'created','goal':'Create files','status':'pass','done':['Files exist'],
            'verify':['Typed readback'],'dependencies':[],'risks':[],'execution_mode':'IMPLEMENTATION'}
        current={'phases':[original]}
        pending={**original,'status':'pending'}
        restored=api._retain_completed_replan_phases({'phases':[pending]},current,{'created'})
        self.assertEqual('pending',restored['phases'][0]['status'])
        changed={**pending,'done':['New requirement']}
        self.assertEqual(changed,api._retain_completed_replan_phases({'phases':[changed]},current,{'created'})['phases'][0])
        self.assertEqual([],api._retain_completed_replan_phases({'phases':[]},current,set())['phases'])
        self.assertEqual([],api._retain_completed_replan_phases({'phases':[]},{'phases':[pending]},{'created'})['phases'])

    def test_legacy_os_launch_error_is_restored_without_relabeling_a_real_test_failure(self):
        original={'attempt':2,'plan_revision':1,'recovery_epoch':0,'status':'FAIL',
            'manager_decision':{'decision':'REPLAN_REQUIRED'},'typed_execution_summary':{'operations':[
            {'tool':'verification_command','status':'failed','output':{'error':
             "verification command failed: [Errno 2] No such file or directory: 'node --test test.js'",'changed_targets':[]}}]}}
        restored=api._restore_legacy_executor_error(original)
        self.assertEqual('REPLAN_REQUIRED',original['manager_decision']['decision'])
        self.assertEqual('VERIFICATION_EXECUTOR',restored['manager_decision']['retry_channel'])
        self.assertEqual(2,restored['attempt'])
        self.assertEqual(1,restored['plan_revision'])
        self.assertEqual(0,restored['recovery_epoch'])
        self.assertTrue(restored['verification_pending'])
        self.assertEqual(restored,api._restore_legacy_executor_error(restored))
        for override in ({'returncode':1},{'changed_targets':['app.js']},{'error':'verification command timed out'}):
            actual=copy.deepcopy(original)
            actual['typed_execution_summary']['operations'][0]['output'].update(override)
            self.assertEqual(actual,api._restore_legacy_executor_error(actual))

    def test_partial_check_coverage_is_not_an_application_failure(self):
        phase={'host_verification':True,'done':['Tests pass'],'verify':['Browser works']}
        report={'errors':['verification command failed'],'test_fail':['old incomplete-coverage output'],
                'typed_execution_summary':{'operations':[{'tool':'verification_command','status':'failed',
                    'output':{'returncode':0,'zero_tests':False,'passed_criteria_ids':['DONE:1'],
                              'changed_targets':[],'failures':[]}}]}}
        self.assertTrue(api._verification_coverage_pending(phase,report))
        for override in ({'returncode':1},{'failures':[{'error':'actual assertion failed'}]},
                         {'changed_targets':['app.js']},{'zero_tests':True},
                         {'passed_criteria_ids':['UNKNOWN']},{'passed_criteria_ids':['DONE:1','VERIFY:1']}):
            invalid=copy.deepcopy(report)
            invalid['typed_execution_summary']['operations'][0]['output'].update(override)
            self.assertFalse(api._verification_coverage_pending(phase,invalid))
        self.assertFalse(api._verification_coverage_pending(phase,{**report,'errors':['source operation failed']}))

    def test_resolving_actual_browser_exceptions_extends_repairs_without_oscillation_credit(self):
        phase={'host_verification':True,'done':['Application works'],'verify':[]}
        def report(message,changed):
            return {'final_worktree_mutations':changed,'accepted_mutations':changed,'typed_execution_summary':{'operations':[
                {'tool':'verification_command','input':{'command':['owned','same-diagnostic']},'output':{
                    'passed_criteria_ids':[],'failures':[{'ids':['DONE:1'],'project_source_error':True,
                        'browser_errors':[{'message':message,'source_paths':['app.js']}]}]}}]}}
        a,b=report('A is not defined',0),report('B is not defined',1)
        self.assertFalse(api._verification_repair_stalled([a,b]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit(phase,b,[a]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit(phase,report('A is not defined',1),[a,b]))
        oscillating=[a,b,report('A is not defined',1),b]*5
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit(phase,oscillating[-1],oscillating[:-1]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE,api._phase_retry_limit(phase,report('B is not defined',0),[a]))

    def test_real_application_exception_is_diagnostic_only_and_needs_no_model_verdict(self):
        html='<input id="input"><button id="select">選ぶ</button><output id="result"></output><script src="app.js"></script>'
        (self.root/'index.html').write_text(html)
        (self.root/'app.js').write_text("document.querySelector('#select').onclick=()=>missingApplicationFunction();")
        api.db.create_coding_task('runtime-error','conversation','Show the entered value after clicking.',
                                 'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':[],
               'verify':['In the browser, show the entered value after clicking.']}
        plan={'file_manifest':[{'path':'index.html'},{'path':'app.js'}]}
        response=json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':
            "await page.locator('#input').fill('A');await page.locator('#select').click();assert.equal(await page.locator('#result').textContent(),'A');"}]})
        with patch.object(api,'_model_text',return_value=response) as model:
            diagnostic=api._host_verification_phase('runtime-error',phase,plan,str(self.root))
            self.assertTrue(diagnostic['host_runtime_diagnostic'])
            self.assertEqual(1,model.call_count)
            again=api._host_verification_phase('runtime-error',phase,plan,str(self.root))
            self.assertTrue(again['host_runtime_diagnostic'])
            self.assertEqual(1,model.call_count)
            (self.root/'app.js').write_text("document.querySelector('#select').onclick=()=>document.querySelector('#result').textContent=document.querySelector('#input').value;")
            complete=api._host_verification_phase('runtime-error',diagnostic,plan,str(self.root))
        self.assertEqual(2,model.call_count,'A resolved diagnostic is not proof of final acceptance')
        self.assertFalse(complete['host_runtime_diagnostic'])
        self.assertNotEqual(diagnostic['verification_command'],complete['verification_command'])
        self.assertTrue(api._run_read_only_verification(complete,str(self.root))[1]['passed'])

    def test_semantic_check_repair_stays_with_acceptance_designer_and_preserves_behavior(self):
        (self.root/'index.html').write_text('<input id="input"><button id="select">選ぶ</button><output id="result"></output><script src="app.js"></script>')
        (self.root/'app.js').write_text("document.querySelector('#select').onclick=()=>document.querySelector('#result').textContent=document.querySelector('#input').value;")
        original='In the browser, show the entered value after clicking.'
        api.db.create_coding_task('semantic-check','conversation',original,'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':[],'verify':[original]}
        plan={'file_manifest':[{'path':'index.html'},{'path':'app.js'}]}
        def check(code):return json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':code}]})
        wrong=check("const accidental='OUTDATED_CHECK_COPY';\nassert.equal(await page.locator('#result').textContent(),'A');")
        correct=check("await page.locator('#input').fill('A');await page.locator('#select').click();assert.equal(await page.locator('#result').textContent(),'A');")
        review={'verdict':'CHECK_DEFECT','reason':'The initial result is empty; click first.',
            'requirement':original,'source_fragment':'output id=result'}
        valid={**review,'verdict':'CHECK_VALID','reason':'The check now exercises the required click.'}
        with patch.object(api,'_model_text',side_effect=[wrong,json.dumps(review),correct,json.dumps(valid)]) as model:
            executable=api._host_verification_phase('semantic-check',phase,plan,str(self.root))
        self.assertEqual([api.settings.planner_model]*4,[call.args[3] for call in model.call_args_list])
        self.assertIn('do not preserve a known incorrect expectation',model.call_args_list[2].args[4][-1]['content'])
        self.assertIn('FINAL_REPAIR_FINDING',model.call_args_list[2].args[4][-1]['content'])
        self.assertNotIn('OUTDATED_CHECK_COPY',model.call_args_list[2].args[4][-1]['content'])
        self.assertTrue(api._run_read_only_verification(executable,str(self.root))[1]['passed'])

    def test_node_import_repair_removes_invalid_namespace_expectations_without_ui_instructions(self):
        (self.root/'index.html').write_text('<p>Actual fixture</p>')
        (self.root/'package.json').write_text('{"type":"module"}')
        (self.root/'logic.js').write_text('export function answer(){return 42;}')
        (self.root/'logic.test.js').write_text("import {test} from 'node:test';import assert from 'node:assert/strict';import {answer} from './logic.js';test('actual answer',()=>assert.equal(answer(),42));")
        original='logic.test.js imports the actual logic.js API.'
        api.db.create_coding_task('node-import','conversation',original,'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':[],'verify':[original]}
        plan={'file_manifest':[{'path':name} for name in ['index.html','package.json','logic.js','logic.test.js']]}
        def check(code):return json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'NODE','code':code}]})
        wrong=check("const testModule=await loadModule('logic.test.js');assert.equal(typeof testModule.answer,'function');")
        correct=check("assert.ok(readFile('logic.test.js').includes(\"import {answer} from './logic.js'\"));const api=await loadModule('logic.js');assert.equal(api.answer(),42);assert.ok((await runTests()).tests>0);")
        review={'verdict':'CHECK_DEFECT','reason':'Imported bindings are local; the test module does not export answer.',
            'requirement':original,'source_fragment':"import {answer} from './logic.js'"}
        valid={**review,'verdict':'CHECK_VALID','reason':'Actual import, API call and test execution are verified.'}
        with patch.object(api,'_model_text',side_effect=[wrong,json.dumps(review),correct,json.dumps(valid)]) as model:
            result=api._host_verification_phase('node-import',phase,plan,str(self.root))
        prompt=model.call_args_list[2].args[4][-1]['content']
        self.assertIn('NODE_REPAIR_RULE:',prompt)
        self.assertIn('Remove invalid testModule namespace assertions',prompt)
        self.assertNotIn('For this empty-input check',prompt)
        self.assertNotIn('messages contains visible text',prompt)
        self.assertTrue(api._run_read_only_verification(result,str(self.root))[1]['passed'])

    def test_long_review_cannot_push_visible_guidance_out_of_the_bounded_repair_finding(self):
        (self.root/'index.html').write_text('<textarea id="input"></textarea><button id="select" disabled>選ぶ</button><p class="hint">候補を入力してください</p><output id="result"></output>')
        original='In the browser, show a clear message for empty input.'
        api.db.create_coding_task('visible-guidance','conversation',original,'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':[],'verify':[original]}
        plan={'file_manifest':[{'path':'index.html'}]}
        def check(code):return json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':code}]})
        wrong=check('assert.ok((await page.locator("#result").textContent()).trim());')
        correct=check('await page.locator("#input").fill("");await expect(page.locator(".hint")).toBeVisible();assert.equal(await page.locator(".hint").textContent(),"候補を入力してください");assert.equal(await page.locator("#result").textContent(),"");')
        review={'verdict':'CHECK_DEFECT','reason':'Use the visible hint; the initial result is empty. '+('advisory '*150),
                'requirement':original,'source_fragment':'quoted source '*150}
        valid={'verdict':'CHECK_VALID','reason':'Actual visible guidance is checked with empty input.',
               'requirement':original,'source_fragment':'<p class="hint">候補を入力してください</p>'}
        with patch.object(api,'_model_text',side_effect=[wrong,json.dumps(review),correct,json.dumps(valid)]) as model:
            executable=api._host_verification_phase('visible-guidance',phase,plan,str(self.root))
        prompt=model.call_args_list[2].args[4][-1]['content']
        finding=prompt.split('FINAL_REPAIR_FINDING (check against the original requirement): ',1)[1][:2200]
        self.assertIn('p.hint',finding)
        self.assertIn('候補を入力してください',finding)
        self.assertIn('"disabled": true',finding)
        self.assertEqual(1,prompt.count('"selector": "p.hint"'),'Measured repair evidence belongs in one place')
        self.assertNotIn('\\"selector\\": \\"p.hint\\"',prompt,'Do not repeat evidence as an escaped error string')
        self.assertNotIn('quoted source quoted source',finding)
        review_prompt=model.call_args_list[3].args[4][-1]['content']
        self.assertIn('A static hint is valid feedback',review_prompt)
        self.assertIn('Unless the HUMAN explicitly requires feedback only after submission',review_prompt)
        self.assertGreater(review_prompt.index('EMPTY_GUIDANCE_RULE:'),review_prompt.index('observed_successful_checks'))
        review_data,end=json.JSONDecoder().raw_decode(review_prompt.split('\n',1)[1])
        self.assertTrue(review_prompt.split('\n',1)[1][end:].startswith('\nEMPTY_GUIDANCE_RULE:'))
        self.assertEqual('observed_successful_checks',list(review_data)[-1])
        measured=review_data['observed_successful_checks']
        self.assertTrue(measured[0]['passed'])
        self.assertEqual('候補を入力してください',measured[0]['observed_page']['messages'][0]['text'])
        self.assertIn({'selector':'#result','text':''},measured[0]['reads'])
        self.assertTrue(api._run_read_only_verification(executable,str(self.root))[1]['passed'])

    def test_large_action_snapshots_cannot_cut_off_the_actual_repair_diagnosis(self):
        finding={'failures':[{'ids':['VERIFY:1'],'error':'Observed output differs',
            'observed_page':{'messages':[{'selector':'p.hint','text':'Use the visible hint','visible':True}],
                'buttons':[{'id':'submit','text':'Apply','disabled':True}]},
            'browser_actions':[{'action':'click','selector':'#submit','outcome':'completed',
                'state_before':{'text':'large state '*1000},'state_after':{'text':'large state '*1000}}]*8}],
            'review':{'verdict':'CHECK_DEFECT','verification_form':'STATEFUL_CODE',
                'reason':'EXACT_CORRECTION_MARKER: compare independently captured values.',
                'requirement':'Preserve the required state relationship'}}
        raw=json.dumps(finding,ensure_ascii=False)
        self.assertGreater(raw.index('EXACT_CORRECTION_MARKER'),2200)
        compact=api._bounded_verification_repair_finding(raw)
        self.assertLessEqual(len(compact),2200)
        data=json.loads(compact)
        self.assertIn('EXACT_CORRECTION_MARKER',data['review']['reason'])
        self.assertEqual('STATEFUL_CODE',data['review']['verification_form'])
        self.assertEqual('Use the visible hint',data['failures'][0]['observed_page']['messages'][0]['text'])
        self.assertTrue(data['failures'][0]['observed_page']['buttons'][0]['disabled'])
        self.assertNotIn('large state',compact)

    def test_check_authoring_resume_keeps_failure_advice_only_for_the_same_source(self):
        html='<textarea id="input"></textarea><button id="select" disabled>選ぶ</button><p class="hint">候補を入力してください</p><output id="result"></output>'
        original='In the browser, show a clear message for empty input.'
        phase={'id':'final','host_verification':True,'done':[],'verify':[original]}
        plan={'file_manifest':[{'path':'index.html'}]}
        def check(code):return json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':code}]})
        wrong=check('const marker="BAD_DRAFT_MARKER";\nassert.ok((await page.locator("#result").textContent()).trim());')
        correct=check('await page.locator("#input").fill("");await expect(page.locator(".hint")).toBeVisible();assert.equal(await page.locator(".hint").textContent(),"候補を入力してください");')
        review={'verdict':'CHECK_DEFECT','reason':'Use the existing visible hint, not an initial result.',
                'requirement':original,'source_fragment':'<p class="hint">候補を入力してください</p>'}
        for changed in (False,True):
            with self.subTest(source_changed=changed):
                identifier='resume-check-'+str(changed)
                (self.root/'index.html').write_text(html)
                api.db.create_coding_task(identifier,'conversation',original,'RUNNING','NONE',self.plan,time.time())
                with patch.object(api,'_alternate_coding_model',return_value='alternate-verifier'), \
                     patch.object(api,'_model_text',side_effect=[wrong,json.dumps(review)]*3) as model:
                    with self.assertRaisesRegex(RuntimeError,'VERIFICATION_CONTRACT_INVALID'):
                        api._host_verification_phase(identifier,phase,plan,str(self.root))
                self.assertEqual(6,model.call_count,'Two designer attempts and one alternate are independently reviewed, then stop')
                directory=Path(api.db.path).parent/'coding-verification'/identifier
                draft=next(directory.glob('*-authoring.json'))
                self.assertIn('BAD_DRAFT_MARKER',draft.read_text())
                if changed:(self.root/'index.html').write_text(html+'<!-- changed source -->')
                valid={**review,'verdict':'CHECK_VALID','reason':'The actual visible guidance is checked with empty input.'}
                responses=[correct] if changed else [correct,json.dumps(valid)]
                with patch.object(api,'_model_text',side_effect=responses) as model:
                    executable=api._host_verification_phase(identifier,phase,plan,str(self.root))
                prompt=model.call_args_list[0].args[4][-1]['content']
                self.assertEqual(1 if changed else 2,model.call_count)
                self.assertEqual(not changed,'FINAL_REPAIR_FINDING' in prompt)
                self.assertNotIn('BAD_DRAFT_MARKER',prompt)
                self.assertFalse(draft.exists())
                self.assertTrue(api._run_read_only_verification(executable,str(self.root))[1]['passed'])

    def test_distinct_measured_check_failures_can_advance_but_oscillation_stops(self):
        (self.root/'index.html').write_text('<textarea id="input"></textarea><button id="select" disabled>選ぶ</button><p class="hint">候補を入力してください</p><output id="result"></output>')
        original='In the browser, show a clear message for empty input.'
        phase={'id':'final','host_verification':True,'done':[],'verify':[original]}
        plan={'file_manifest':[{'path':'index.html'}]}
        def check(code):return json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':code}]})
        a=check('assert.ok((await page.locator("#result").textContent()).trim());')
        b=check('assert.equal(await page.locator("#select").isDisabled(),false);')
        correct=check('await expect(page.locator(".hint")).toBeVisible();assert.equal(await page.locator(".hint").textContent(),"候補を入力してください");')
        review={'verdict':'CHECK_DEFECT','reason':'Use actual visible guidance and disabled state.',
                'requirement':original,'source_fragment':'<p class="hint">候補を入力してください</p>'}
        for oscillates in (False,True):
            with self.subTest(oscillates=oscillates):
                identifier='check-progress-'+str(oscillates)
                api.db.create_coding_task(identifier,'conversation',original,'RUNNING','NONE',self.plan,time.time())
                final=a if oscillates else correct
                final_review=review if oscillates else {**review,'verdict':'CHECK_VALID'}
                with patch.object(api,'_model_text',side_effect=[a,json.dumps(review),b,json.dumps(review),final,json.dumps(final_review)]) as model:
                    if oscillates:
                        with self.assertRaisesRegex(RuntimeError,'VERIFICATION_CONTRACT_INVALID'):
                            api._host_verification_phase(identifier,phase,plan,str(self.root))
                    else:
                        executable=api._host_verification_phase(identifier,phase,plan,str(self.root))
                self.assertEqual(6,model.call_count)
                if not oscillates:self.assertTrue(api._run_read_only_verification(executable,str(self.root))[1]['passed'])
        identifier='check-progress-cap'
        api.db.create_coding_task(identifier,'conversation',original,'RUNNING','NONE',self.plan,time.time())
        responses=[]
        for expected in ['A','B','C','D']:
            responses.extend([check('assert.equal(await page.locator("#result").textContent(),'+json.dumps(expected)+');'),json.dumps(review)])
        with patch.object(api,'_model_text',side_effect=responses) as model:
            with self.assertRaisesRegex(RuntimeError,'VERIFICATION_CONTRACT_INVALID'):
                api._host_verification_phase(identifier,phase,plan,str(self.root))
        self.assertEqual(8,model.call_count,'Even distinct observed failures cannot exceed four authoring attempts')

    def test_valid_focused_check_is_not_rejected_by_an_extra_1600_character_limit(self):
        text='日本語の説明文です。'*200
        (self.root/'index.html').write_text('<p id="message">'+text+'</p>')
        original='In the browser, display the full Japanese help text.'
        api.db.create_coding_task('long-check','conversation',original,'RUNNING','NONE',self.plan,time.time())
        lines=['const expected = [',*[json.dumps(text[i:i+700],ensure_ascii=False)+',' for i in range(0,len(text),700)],
               '].join("");','assert.equal(await page.locator("#message").textContent(),expected);']
        self.assertGreater(len('\n'.join(lines)),1600)
        self.assertTrue(all(len(line)<=1000 for line in lines))
        response=json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':lines}]})
        with patch.object(api,'_model_text',return_value=response) as model:
            executable=api._host_verification_phase('long-check',{'id':'final','host_verification':True,'done':[],
                'verify':[original]},{'file_manifest':[{'path':'index.html'}]},str(self.root))
        self.assertEqual(1,model.call_count)
        self.assertTrue(api._run_read_only_verification(executable,str(self.root))[1]['passed'])

    def test_structured_ui_observations_are_compiled_and_verified_by_the_managed_host(self):
        (self.root/'index.html').write_text('<textarea id="input"></textarea><button id="select">選ぶ</button><output id="result"></output><script src="app.js"></script>')
        (self.root/'app.js').write_text("document.querySelector('#select').onclick=()=>document.querySelector('#result').textContent=document.querySelector('#input').value;")
        original='index.html exists and renders a UI with an input area and select button.'
        api.db.create_coding_task('typed-ui','conversation',original,'RUNNING','NONE',self.plan,time.time())
        response={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER',
            'actions':[{'action':'fill','selector':'#input','value':'A'},{'action':'click','selector':'#select','value':None}],
            'observations':[{'selector':'#input','measure':'tag','operator':'eq','expected':'textarea'},
                {'selector':'#result','measure':'text','operator':'eq','expected':'A'}]}]}
        with patch.object(api,'_model_text',return_value=json.dumps(response)) as model:
            executable=api._host_verification_phase('typed-ui',{'id':'final','host_verification':True,'done':[],
                'verify':[original]},{'file_manifest':[{'path':'index.html'},{'path':'app.js'}]},str(self.root))
        schema=model.call_args.kwargs['structured_schema']['properties']['checks']['items']
        self.assertIn('observations',schema['required'])
        self.assertNotIn('code',schema['properties'])
        result=api._run_read_only_verification(executable,str(self.root))[1]
        self.assertTrue(result['passed'])
        self.assertEqual(['VERIFY:1'],result['passed_criteria_ids'])

    def test_standard_check_added_after_model_check_needs_no_author_or_semantic_repair_call(self):
        (self.root/'index.html').write_text('<textarea></textarea><script src="app.js"></script>')
        (self.root/'app.js').write_text('/* local app */')
        criterion='index.html contains an actual textarea source.'
        offline='The app can be opened in a browser without external libraries or network calls.'
        api.db.create_coding_task('owned-tail','conversation',offline,'RUNNING','NONE',self.plan,time.time())
        response={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'NODE',
            'code':"assert.ok(readFile('index.html').includes('<textarea>'));"}]}
        standard=api.standard_verification_checks
        calls=[]
        def after_initial(criteria):
            calls.append(criteria)
            return [] if len(calls)==1 else standard(criteria)
        with patch.object(api,'standard_verification_checks',side_effect=after_initial), \
             patch.object(api,'_model_text',return_value=json.dumps(response)) as model, \
             patch.object(api,'_review_verification_failure') as review:
            phase=api._host_verification_phase('owned-tail',{'id':'final','host_verification':True,
                'done':[],'verify':[criterion,offline]},{'file_manifest':[{'path':'index.html'},{'path':'app.js'}]},str(self.root))
        self.assertEqual(1,model.call_count)
        review.assert_not_called()
        result=api._run_read_only_verification(phase,str(self.root))[1]
        self.assertTrue(result['passed'])
        self.assertEqual(['VERIFY:1','VERIFY:2'],result['passed_criteria_ids'])

    def test_actual_page_use_recovers_wrong_node_executor_without_regenerating_assertions(self):
        (self.root/'index.html').write_text('<textarea id="input"></textarea>')
        criterion='Put a value into the control and inspect it.'
        for index,code in enumerate([
                "await page.locator('#input').fill('real value');assert.equal(await page.locator('#input').inputValue(),'real value');",
                "await page.fill('#input','real value');assert.equal(await page.inputValue('#input'),'real value');"]):
            identifier='executor-recovery-'+str(index)
            api.db.create_coding_task(identifier,'conversation',criterion,'RUNNING','NONE',self.plan,time.time())
            response={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'NODE','code':code}]}
            with patch.object(api,'_model_text',return_value=json.dumps(response)) as model:
                phase=api._host_verification_phase(identifier,{'id':'final','host_verification':True,
                    'done':[],'verify':[criterion]},{'file_manifest':[{'path':'index.html'}]},str(self.root))
            self.assertEqual(1,model.call_count)
            contract=json.loads(Path(phase['verification_command'][2]).read_text())
            self.assertEqual('BROWSER',contract['checks'][0]['kind'])
            self.assertEqual(code,contract['checks'][0]['code'])
            result=api._run_read_only_verification(phase,str(self.root))[1]
            self.assertTrue(result['passed'])
            self.assertEqual(1,result['browser_checks'])

    def test_protocol_only_repair_does_not_invite_semantic_reinterpretation_after_resume(self):
        (self.root/'index.html').write_text('<textarea id="input"></textarea>')
        criterion='In the browser, retain the value entered into the input.'
        api.db.create_coding_task('protocol-only','conversation',criterion,'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':[],'verify':[criterion]}
        plan={'file_manifest':[{'path':'index.html'}]}
        def response(code):return json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':code}]})
        broken=response('await nonexistentObservationHelper();assert.ok(true);')
        fixed=response("await page.locator('#input').fill('actual value');assert.equal(await page.locator('#input').inputValue(),'actual value');")
        with patch.object(api,'_model_text',return_value=broken) as model, \
             patch.object(api,'_review_verification_failure') as review:
            with self.assertRaisesRegex(RuntimeError,'VERIFICATION_CONTRACT_INVALID'):
                api._host_verification_phase('protocol-only',phase,plan,str(self.root))
        self.assertEqual(2,model.call_count)
        review.assert_not_called()
        directory=Path(api.db.path).parent/'coding-verification'/'protocol-only'
        draft=json.loads(next(directory.glob('*-authoring.json')).read_text())
        self.assertIs(False,draft['semantic_repair_required'])
        with patch.object(api,'_model_text',return_value=fixed) as model, \
             patch.object(api,'_review_verification_failure') as review:
            executable=api._host_verification_phase('protocol-only',phase,plan,str(self.root))
        self.assertEqual(1,model.call_count)
        review.assert_not_called()
        self.assertTrue(api._run_read_only_verification(executable,str(self.root))[1]['passed'])

    def test_pause_after_check_review_preserves_both_failed_draft_and_accepted_progress(self):
        (self.root/'index.html').write_text('<input id="input"><button id="select">Choose</button><output id="result"></output><script src="app.js"></script>')
        (self.root/'app.js').write_text("document.querySelector('#select').onclick=()=>document.querySelector('#result').textContent=document.querySelector('#input').value;")
        original='In the browser, display the entered value after clicking.'
        identifier='review-pause'
        api.db.create_coding_task(identifier,'conversation',original,'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':[],'verify':[original]}
        plan={'file_manifest':[{'path':'index.html'},{'path':'app.js'}]}
        def response(code):return json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':code}]})
        wrong=response("assert.equal(await page.locator('#result').textContent(),'A');")
        code="await page.locator('#input').fill('A');await page.locator('#select').click();assert.equal(await page.locator('#result').textContent(),'A');"
        def stop_review(*args,**kwargs):
            api.db.update_coding_task(identifier,pause_requested=True)
            return {'verdict':'CHECK_VALID' if kwargs.get('observations') else 'CHECK_DEFECT',
                'reason':'Observe the actual result after the click.','requirement':original,'source_fragment':'result'}
        with patch.object(api,'_model_text',return_value=wrong) as model, \
             patch.object(api,'_review_verification_failure',side_effect=stop_review):
            api._host_verification_phase(identifier,phase,plan,str(self.root))
        self.assertEqual(1,model.call_count)
        directory=Path(api.db.path).parent/'coding-verification'/identifier
        draft=json.loads(next(directory.glob('*-authoring.json')).read_text())
        self.assertTrue(draft['semantic_repair_required'])
        self.assertIn('CHECK_DEFECT',draft['finding'])
        api.db.update_coding_task(identifier,status='RUNNING',pause_requested=False)
        with patch.object(api,'_model_text',return_value=response(code)) as model, \
             patch.object(api,'_review_verification_failure',side_effect=stop_review):
            api._host_verification_phase(identifier,phase,plan,str(self.root))
        self.assertEqual(1,model.call_count)
        self.assertFalse(list(directory.glob('*-authoring.json')))
        self.assertEqual('RESUMABLE',api.db.coding_task(identifier)['status'])
        api.db.update_coding_task(identifier,status='RUNNING',pause_requested=False)
        with patch.object(api,'_model_text',side_effect=AssertionError('Accepted checks must survive the pause')):
            executable=api._host_verification_phase(identifier,phase,plan,str(self.root))
        contract=json.loads(Path(executable['verification_command'][2]).read_text())
        self.assertEqual(code,contract['checks'][0]['code'])
        self.assertTrue(api._run_read_only_verification(executable,str(self.root))[1]['passed'])

    def test_original_user_behavior_is_checked_before_broad_planner_ui_wording(self):
        (self.root/'index.html').write_text('<input id="input"><button id="select">選ぶ</button><output id="result"></output><script src="app.js"></script>')
        (self.root/'app.js').write_text("const input=document.getElementById('input');const rows=input.value.split('\\n');")
        original='料理名を1行に1つ入力できる'
        phase={'id':'final','host_verification':True,'done':[],'verify':[
            'In the browser, verify the overall UI and every initial state.',
            'In the browser, verify the original requirement: '+original]}
        plan={'file_manifest':[{'path':'index.html'},{'path':'app.js'}]}
        for name,code in [
            ('locator',"await page.locator('#input').fill('A\\nB');assert.equal(await page.locator('#input').inputValue(),'A\\nB');"),
            ('role',"await page.getByRole('textbox').fill('A\\nB');assert.equal(await page.getByRole('textbox').inputValue(),'A\\nB');"),
            ('page',"await page.fill('#input','A\\nB');assert.equal(await page.inputValue('#input'),'A\\nB');")]:
            with self.subTest(api=name):
                task_id='original-first-'+name
                api.db.create_coding_task(task_id,'conversation',original,'RUNNING','NONE',self.plan,time.time())
                candidate=json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:2'],'kind':'BROWSER','code':code}]})
                with patch.object(api,'_model_text',return_value=candidate) as model:
                    diagnostic=api._host_verification_phase(task_id,phase,plan,str(self.root))
                identifiers=model.call_args_list[0].kwargs['structured_schema']['properties']['checks']['items']['properties']['ids']['items']['enum']
                self.assertEqual(['VERIFY:2'],identifiers)
                self.assertTrue(diagnostic['host_partial_verification'])
                self.assertFalse(api._run_read_only_verification(diagnostic,str(self.root))[1]['passed'])
                self.assertEqual(2,len(phase['verify']))
                self.assertEqual(1,model.call_count,'Measured single-line input limitations do not need a model verdict')

    def test_multiline_control_diagnosis_requires_actual_attempt_and_matching_source(self):
        criteria={'VERIFY:1':'In the browser, verify the original requirement: 料理名を1行に1つ入力できる'}
        sources={'index.html':"<input id='input' type='text'>",'app.js':"const rows=input.value.split('\\n');"}
        candidate={'checks':[{'ids':['VERIFY:1'],'code':"await page.locator('#input').fill('A\\nB');const value=await page.locator('#input').inputValue();assert.equal(value,'A\\nB');"}]}
        failure={'kind':'BROWSER','ids':['VERIFY:1'],
            'assertion':{'operator':'strictEqual','raw_actual':'A B','raw_expected':'A\nB'},
            'input_actions':[{'value':'A\nB','selector':'#input'}],
            'observed_page':{'fields':[{'id':'input','tag':'input','type':'text','value':'A B'}]}}
        diagnosed=api._observed_multiline_input_defect(criteria,sources,candidate,[failure])
        self.assertEqual('SOURCE_DEFECT',diagnosed['verdict'])
        self.assertEqual('OBSERVED_INPUT_CAPABILITY',diagnosed['evidence_origin'])
        # maxlength can fail before browser input-action instrumentation is
        # recorded. The actual truncated value and matching source attribute
        # still provide deterministic source evidence.
        limited_sources={'index.html':'<input type="text" id="input" maxlength="1">'}
        limited_candidate={'checks':[{'ids':['VERIFY:1'],'kind':'BROWSER',
            'code':"await page.locator('#input').fill('ラーメン');const value=await page.locator('#input').inputValue();assert.equal(value,'ラーメン');"}]}
        limited_failure={'kind':'BROWSER','ids':['VERIFY:1'],
            'assertion':{'operator':'strictEqual','raw_actual':'ラ','raw_expected':'ラーメン'},
            'input_actions':[],
            'observed_page':{'fields':[{'id':'input','tag':'input','type':'text','value':'ラ'}]}}
        diagnosed=api._observed_multiline_input_defect(criteria,limited_sources,limited_candidate,[limited_failure])
        self.assertEqual('SOURCE_DEFECT',diagnosed['verdict'])
        self.assertEqual('OBSERVED_MAXLENGTH_TRUNCATION',diagnosed['evidence_origin'])
        self.assertEqual('ラーメン',diagnosed['attempted_value'])
        limited_original='仕様:\n- 料理名を1行に1つ入力できる'
        with patch.object(api,'_model_text',side_effect=AssertionError('Observed maxlength truncation must not re-enter check-authoring')):
            reviewed=api._review_verification_failure('maxlength-loop',limited_original,criteria,limited_sources,
                limited_candidate,[limited_failure])
        self.assertEqual('SOURCE_DEFECT',reviewed['verdict'])
        self.assertIsNone(api._observed_multiline_input_defect(criteria,limited_sources,limited_candidate,
            [{**limited_failure,'observed_page':{'fields':[{'id':'input','tag':'input','type':'text','value':'別'}]}}]))
        self.assertIsNone(api._observed_multiline_input_defect({'VERIFY:1':'Enter a name'},limited_sources,
            limited_candidate,[limited_failure]))
        # Real acceptance code often asserts the observable consequence (for
        # example, that the three entered dishes remain three lines) instead
        # of comparing the input value directly. The rendered field plus the
        # recorded multiline action is enough to identify this source defect.
        count_candidate={'checks':[{'ids':['VERIFY:1'],'code':"await page.locator('#input').fill('A\\nB\\nC');assert.equal((await page.locator('#input').inputValue()).split('\\n').length,3);"}]}
        count_failure={'kind':'BROWSER','ids':['VERIFY:1'],
            'assertion':{'operator':'==','raw_actual':1,'raw_expected':3},
            'input_actions':[{'value':'A\nB\nC','selector':'#input'}],
            'observed_page':{'fields':[{'id':'input','tag':'input','type':'text','value':'A B C'}]}}
        diagnosed=api._observed_multiline_input_defect(criteria,sources,count_candidate,[count_failure])
        self.assertEqual('SOURCE_DEFECT',diagnosed['verdict'])
        selection_criteria={'VERIFY:24':'In the browser, choose one of the supplied dishes at random.'}
        original='仕様:\n- 料理名を1行に1つ入力できる\n- 候補から1つを選ぶ'
        selection_candidate={'checks':[{**count_candidate['checks'][0],'ids':['VERIFY:24']}]}
        diagnosed=api._observed_multiline_input_defect(selection_criteria,sources,selection_candidate,
            [{'kind':'BROWSER','ids':['VERIFY:24'],'assertion':count_failure['assertion'],
              'input_actions':count_failure['input_actions'],'observed_page':count_failure['observed_page']}],original)
        self.assertEqual('SOURCE_DEFECT',diagnosed['verdict'],
            'The failing selection check can expose the original multiline-input defect even when its own criterion is about selection.')
        with patch.object(api,'_model_text',side_effect=AssertionError('Observed source mismatch must not re-enter check-authoring')):
            reviewed=api._review_verification_failure('multiline-loop',original,selection_criteria,sources,
                selection_candidate,[{'kind':'BROWSER','ids':['VERIFY:24'],'assertion':count_failure['assertion'],
                    'input_actions':count_failure['input_actions'],'observed_page':count_failure['observed_page']}])
        self.assertEqual('SOURCE_DEFECT',reviewed['verdict'])
        for override in ({'contract_error':True},{'input_actions':[]},
                         {'input_actions':[{'value':'A B','selector':'#input'}]},
                         {'input_actions':[{'value':'A\nB','selector':'#different'}]},
                         {'observed_page':{'fields':[{'id':'input','tag':'textarea','type':'textarea','value':'A B'}]}},
                         {'ids':['UNKNOWN']}):
            self.assertIsNone(api._observed_multiline_input_defect(criteria,sources,candidate,[{**failure,**override}]))
        self.assertIsNone(api._observed_multiline_input_defect({'VERIFY:1':'Display multiline output'},sources,candidate,[failure]))
        self.assertIsNone(api._observed_multiline_input_defect(criteria,{'index.html':'<input id="other">'},candidate,[failure]))
        self.assertEqual('OBSERVED_INPUT_CAPABILITY',api._observed_multiline_input_defect(
            criteria,{'index.html':sources['index.html']},candidate,[failure])['evidence_origin'])

    def test_real_multiline_input_mismatch_is_routed_as_a_source_repair(self):
        (self.root/'index.html').write_text('<label for="dish-input">料理名</label><input id="dish-input" type="text">')
        original='料理名を1行に1つ入力できる'
        api.db.create_coding_task('observed-line-input','conversation',original,'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','execution_mode':'VERIFICATION_ONLY','host_verification':True,
            'done':[],'verify':['In the browser, verify the original requirement: '+original]}
        candidate=json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':
            "await page.locator('#dish-input').fill('カレー\\n丼');assert.equal(await page.locator('#dish-input').inputValue(),'カレー\\n丼');"}]})
        with patch.object(api,'_model_text',return_value=candidate) as author:
            executable=api._host_verification_phase('observed-line-input',phase,
                {'file_manifest':[{'path':'index.html'}]},str(self.root))
        self.assertEqual(1,author.call_count)
        self.assertTrue(executable['host_source_defect'])
        self.assertFalse(executable['host_runtime_diagnostic'])
        self.assertEqual(['index.html'],executable['host_source_defect_repair_paths'])

    def test_browser_runner_records_a_completed_fill_that_the_app_immediately_resets(self):
        (self.root/'index.html').write_text(
            '<label for="budget">予算</label><input id="budget" type="number" value="300">'
            '<script src="app.js"></script>')
        (self.root/'app.js').write_text(
            "const budget=document.querySelector('#budget');"
            "budget.addEventListener('input',()=>{budget.value='300';});")
        original='Respect the maximum budget and report when no meal fits.'
        contract={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER',
            'code':"await page.locator('#budget').fill('0');assert.equal(await page.locator('#budget').inputValue(),'0');"}]}
        contract_path=self.root/'contract.json'
        contract_path.write_text(json.dumps(contract))
        phase={'verification_command':api.acceptance_command(contract_path,self.root),
            'verification_targets':['index.html','app.js'],'host_criteria':{'VERIFY:1':original}}
        _,result=api._run_read_only_verification(phase,str(self.root))
        self.assertFalse(result['passed'])
        action=next(item for item in result['failures'][0]['browser_actions'] if item['action']=='fill')
        self.assertEqual('completed',action['outcome'])
        self.assertEqual('0',action['requested_value'])
        self.assertEqual('300',action['observed_value'])
        self.assertEqual('number',action['field_type'])
        source_map={'index.html':(self.root/'index.html').read_text(),
            'app.js':(self.root/'app.js').read_text()}
        with patch.object(api,'_model_text',side_effect=AssertionError('Measured input reset must bypass model review')):
            finding=api._review_verification_failure('runner-fill-reset',original,phase['host_criteria'],
                source_map,contract,result['failures'])
        self.assertEqual('SOURCE_DEFECT',finding['verdict'])
        self.assertEqual('app.js',finding['source_path'])
        self.assertEqual('OBSERVED_FILL_VALUE_RESET',finding['evidence_origin'])

    def test_observed_valid_fill_reset_is_classified_as_source_defect_without_model_review(self):
        original='Respect the maximum budget and report when no meal fits.'
        criteria={'VERIFY:1':'In the browser, verify the original requirement: '+original}
        sources={'index.html':'<label for="budget">Budget</label><input id="budget" type="number">',
            'src/ui.js':"const budget=document.querySelector('#budget');budget.addEventListener('input',()=>{budget.value='300';});"}
        candidate={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER',
            'code':"await page.locator('#budget').fill('0');assert.equal(await page.locator('#notice').textContent(),'No meals fit');"}]}
        failure={'kind':'BROWSER','ids':['VERIFY:1'],'browser_actions':[{'action':'fill','selector':'#budget',
            'outcome':'completed','field_id':'budget','field_type':'number','requested_value':'0',
            'observed_value':'300','previous_value':'300',
            'state_before':{'fields':[{'id':'budget','type':'number','value':'300'}]},
            'state_after':{'fields':[{'id':'budget','type':'number','value':'300'}]}}]}
        with patch.object(api,'_model_text',side_effect=AssertionError('Measured input reset must bypass model review')):
            finding=api._review_verification_failure('budget-fill-reset',original,criteria,sources,candidate,[failure])
        self.assertEqual('SOURCE_DEFECT',finding['verdict'])
        self.assertEqual('OBSERVED_FILL_VALUE_RESET',finding['evidence_origin'])
        self.assertEqual(original,finding['requirement'])
        self.assertIn("budget.addEventListener('input'",finding['source_fragment'])
        self.assertEqual('0',finding['requested_value'])
        self.assertEqual('300',finding['observed_value'])

    def test_fill_reset_diagnosis_rejects_invalid_numeric_values_unrelated_controls_and_unmatched_sources(self):
        original='Respect the maximum budget and report when no meal fits.'
        criteria={'VERIFY:1':'In the browser, verify the original requirement: '+original}
        source="const budget=document.querySelector('#budget');budget.addEventListener('input',()=>{budget.value='300';});"
        sources={'index.html':'<input id="budget" type="number">','src/ui.js':source}
        candidate={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER',
            'code':"await page.locator('#budget').fill('0');"}]}
        base={'kind':'BROWSER','ids':['VERIFY:1'],'browser_actions':[{'action':'fill','selector':'#budget',
            'outcome':'completed','field_id':'budget','field_type':'number','requested_value':'0',
            'previous_value':'300','observed_value':'300'}]}
        invalid=[
            {'browser_actions':[{'action':'fill','selector':'#budget','outcome':'completed','field_id':'budget',
                'field_type':'number','requested_value':'1e','previous_value':'300','observed_value':''}]},
            {'browser_actions':[{'action':'fill','selector':'#budget','outcome':'completed','field_id':'budget',
                'field_type':'number','requested_value':'0','previous_value':'300','observed_value':'250'}]},
            {'browser_actions':[{'action':'fill','selector':'#api-key','outcome':'completed','field_id':'api-key',
                'field_type':'text','requested_value':'hidden-value','previous_value':'','observed_value':''}]},
            {'checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':"await page.locator('#other').fill('0');"}]},
            {'sources':{'index.html':sources['index.html'],'src/ui.js':"budget.addEventListener('input',()=>{render();});"}},
        ]
        for override in invalid:
            with self.subTest(override=override):
                diagnosed=api._observed_fill_value_reset_defect(criteria,override.get('sources',sources),
                    {**candidate,'checks':override.get('checks',candidate['checks'])},
                    [{**base,**override}],original)
                self.assertIsNone(diagnosed)

    def test_real_budget_fill_reset_routes_only_the_current_ui_source_for_repair(self):
        (self.root/'index.html').write_text(
            '<label for="budget">予算</label><input id="budget" type="number" value="300">'
            '<button id="generate">Create plan</button><output id="message"></output>'
            '<script src="app.js"></script>')
        (self.root/'app.js').write_text(
            "const budget=document.querySelector('#budget');"
            "budget.addEventListener('input',()=>{budget.value='300';});"
            "document.querySelector('#generate').addEventListener('click',()=>{"
            "document.querySelector('#message').textContent=Number(budget.value)>=300?'Plan generated':'No meals fit';});")
        original='Respect the maximum budget and report when no meal fits.'
        task_id='real-budget-fill-reset'
        api.db.create_coding_task(task_id,'conversation',original,'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','execution_mode':'VERIFICATION_ONLY','host_verification':True,'done':[],
            'verify':['In the browser, verify the original requirement: '+original]}
        plan={'file_manifest':[{'path':'index.html'},{'path':'app.js'}]}
        candidate=json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':
            "await page.locator('#budget').fill('0');await page.locator('#generate').click();"
            "assert.equal(await page.locator('#message').textContent(),'No meals fit');"}]})
        with patch.object(api,'_model_text',return_value=candidate) as author:
            diagnostic=api._host_verification_phase(task_id,phase,plan,str(self.root))
        self.assertEqual(1,author.call_count,'Observed fill resets must route deterministically without model review')
        self.assertTrue(diagnostic['host_source_defect'])
        self.assertEqual(['app.js'],diagnostic['host_source_defect_repair_paths'])
        review=diagnostic['host_source_defect_evidence']['review']
        self.assertEqual('OBSERVED_FILL_VALUE_RESET',review['evidence_origin'])
        self.assertEqual('0',review['requested_value'])
        self.assertEqual('300',review['observed_value'])

    def test_failed_probe_keeps_explicitly_passing_checks_and_repairs_only_failed_ids(self):
        (self.root/'index.html').write_text('<main>verification fixture</main>')
        (self.root/'module.js').write_text('const value=1; const marker="ready";')
        original='The module has value 1 and marker ready.'
        api.db.create_coding_task('scoped-check-repair','conversation',original,'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':['The module has value 1.'],
               'verify':['The module marker is ready.']}
        plan={'file_manifest':[{'path':'index.html'},{'path':'module.js'}]}
        initial=json.dumps({'entrypoint':'index.html','checks':[
            {'ids':['DONE:1'],'kind':'NODE','code':"assert.ok(readFile('module.js').includes('value=1'));"},
            {'ids':['VERIFY:1'],'kind':'NODE','code':"assert.ok(readFile('module.js').includes('missing-marker'));"}]})
        corrected=json.dumps({'entrypoint':'index.html','checks':[
            {'ids':['VERIFY:1'],'kind':'NODE','code':"assert.ok(readFile('module.js').includes('marker=\\\"ready\\\"'));"}]})
        reviews=[json.dumps({'verdict':'CHECK_DEFECT','reason':'The assertion expects a marker that is absent from the human requirement.',
                             'requirement':'The module marker is ready.','source_fragment':'const marker="ready";'}),
                 json.dumps({'verdict':'CHECK_VALID','reason':'The corrected assertion proves the requested marker.',
                             'requirement':'The module marker is ready.','source_fragment':'const marker="ready";'})]
        responses=iter([initial,reviews[0],corrected,reviews[1]])
        with patch.object(api,'_model_text',side_effect=lambda *args,**kwargs:next(responses)) as model:
            executable=api._host_verification_phase('scoped-check-repair',phase,plan,str(self.root))
        self.assertEqual(4,model.call_count)
        retry_schema=model.call_args_list[2].kwargs['structured_schema']
        self.assertEqual(['VERIFY:1'],retry_schema['properties']['checks']['items']['properties']['ids']['items']['enum'])
        contract=json.loads(Path(executable['verification_command'][2]).read_text())
        self.assertEqual({'DONE:1','VERIFY:1'},{identifier for check in contract['checks'] for identifier in check['ids']})
        self.assertTrue(api._run_read_only_verification(executable,str(self.root))[1]['passed'])

    def test_successful_contract_clears_only_its_matching_stale_failure(self):
        (self.root/'index.html').write_text('<main>verification fixture</main>')
        (self.root/'module.js').write_text('module.exports={value:1};')
        original='The module exports value 1.'
        api.db.create_coding_task('clear-contract-failure','conversation',original,'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':['The module exports value 1.'],'verify':[]}
        plan={'file_manifest':[{'path':'index.html'},{'path':'module.js'}]}
        response=json.dumps({'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'NODE',
            'code':"const api=await loadModule('module.js');assert.equal(api.value,1);"}]})
        resume=api.db.coding_task('clear-contract-failure')['resume_state'] or {}
        api.db.update_coding_task('clear-contract-failure',resume_state={**resume,
            'verification_preparation_failure':{'signature':'old-signature','error':'old'}})
        with patch.object(api,'_model_text',return_value=response), patch.object(api,'_review_verification_failure',return_value={'verdict':'CHECK_VALID'}):
            api._host_verification_phase('clear-contract-failure',phase,plan,str(self.root))
        self.assertEqual('old-signature',api.db.coding_task('clear-contract-failure')['resume_state']
                         ['verification_preparation_failure']['signature'])
        api._clear_verification_preparation_failure('clear-contract-failure','old-signature')
        self.assertNotIn('verification_preparation_failure',api.db.coding_task('clear-contract-failure')['resume_state'])

    def test_observed_repeat_is_a_source_counterexample_without_a_model_verdict(self):
        (self.root/'index.html').write_text('<textarea id="input"></textarea><button id="select">Choose</button><output id="result"></output><script src="app.js"></script>')
        source="let step=0;document.querySelector('#select').onclick=()=>{const values=document.querySelector('#input').value.split('\\n').map(value=>value.trim()).filter(Boolean);document.querySelector('#result').textContent=values[Math.floor(step++/2)%values.length];};"
        (self.root/'app.js').write_text(source)
        requirement='候補が2つ以上あるときは直前と同じ候補が連続しない。'
        original='候補を1行に1つ入力し、「Choose」で選ぶ。\n'+requirement
        api.db.create_coding_task('observed-repeat','conversation',original,'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':[],'verify':[
            'In the browser, verify the original requirement: '+requirement]}
        plan={'file_manifest':[{'path':'index.html'},{'path':'app.js'}]}
        code="await page.locator('#input').fill('A\\nB\\nC');const observed=[];for(let i=0;i<8;i++){await page.locator('#select').click();observed.push(await page.locator('#result').textContent());}assert.equal(observed.length,8);"
        candidate=json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':code}]})
        with patch.object(api,'_model_text',return_value=candidate) as model:
            executable=api._host_verification_phase('observed-repeat',phase,plan,str(self.root))
        self.assertEqual(1,model.call_count)
        failed=api._run_read_only_verification(executable,str(self.root))[1]
        self.assertFalse(failed['passed'])
        witness=failed['failures'][0]['observed_no_repeat']
        self.assertEqual(witness['previous'],witness['selected'])
        self.assertGreater(witness['current_click'],witness['previous_click'])
        self.assertGreaterEqual(len(set(witness['observed_results'])),2)
        # Repair only this test fixture; the same owned check must then pass.
        (self.root/'app.js').write_text(source.replace('Math.floor(step++/2)','step++'))
        self.assertTrue(api._run_read_only_verification(executable,str(self.root))[1]['passed'])

    def test_selection_observation_policy_requires_an_original_explicit_rule(self):
        original='候補を1行に1つ入力し、「Choose」で選ぶ。候補が2つ以上なら同じ候補が連続しない。'
        rule='候補が2つ以上なら同じ候補が連続しない。'
        criteria={'VERIFY:1':'In the browser, verify the original requirement: '+rule}
        sources={'app.js':"const values=input.value.split('\\n').filter(Boolean);"}
        self.assertEqual(['VERIFY:1'],api._selection_exclusion_spec(original,criteria,sources)['ids'])
        self.assertIsNone(api._selection_exclusion_spec(original,{'VERIFY:1':rule},sources))
        self.assertIsNone(api._selection_exclusion_spec(original,criteria,{'app.js':sources['app.js']+'setTimeout(()=>{},100);'}))
        self.assertIsNone(api._selection_exclusion_spec(original,criteria,{'app.js':'const values=[];'}))

    def test_first_confirmed_source_defect_is_repaired_before_other_checks_are_authored(self):
        (self.root/'index.html').write_text('<h1>App</h1>')
        (self.root/'logic.js').write_text('module.exports={value:0};')
        original='The value must be 1. The heading contains App.'
        api.db.create_coding_task('incremental','conversation',original,'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':['The value must be 1.'],
               'verify':['The heading contains App.']}
        plan={'file_manifest':[{'path':'index.html'},{'path':'logic.js'}]}
        calls=[]
        def model(task_id,status,activity,role,messages,**kwargs):
            if 'You review the meaning' in messages[0]['content']:
                calls.append('review')
                return json.dumps({'verdict':'SOURCE_DEFECT','reason':'Actual value is zero.',
                    'requirement':'The value must be 1.','source_fragment':'module.exports={value:0};'})
            calls.append('author')
            return json.dumps({'entrypoint':'index.html','checks':[
                {'ids':['DONE:1'],'kind':'NODE','code':"const m=await loadModule('logic.js');assert.equal(m.value,1);"},
                {'ids':['VERIFY:1'],'kind':'NODE','code':"assert.ok(readFile('index.html').includes('App'));"}]})
        with patch.object(api,'_model_text',side_effect=model):
            partial=api._host_verification_phase('incremental',phase,plan,str(self.root))
            self.assertEqual(['author','review'],calls)
            again=api._host_verification_phase('incremental',phase,plan,str(self.root))
            self.assertEqual(['author','review'],calls,'Do not author more checks against the same broken source')
            self.assertFalse(api._run_read_only_verification(partial,str(self.root))[1]['passed'])
            # This fixture repair stands in for a separately verified source mutation.
            (self.root/'logic.js').write_text('module.exports={value:1};')
            complete=api._host_verification_phase('incremental',partial,plan,str(self.root))
        self.assertEqual(['author','review'],calls)
        self.assertFalse(complete.get('host_partial_verification',False))
        self.assertTrue(api._run_read_only_verification(complete,str(self.root))[1]['passed'])

    def test_host_verification_authors_all_missing_criteria_in_one_model_call(self):
        (self.root/'index.html').write_text('<h1>App</h1>')
        (self.root/'logic.js').write_text('module.exports={value:1};')
        api.db.create_coding_task('batched-contract','conversation','The API exposes value 1.',
                                 'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':['The exported value is 1.'],
               'verify':['The API can be imported by Node.js.']}
        calls=[]
        def model(task_id,status,activity,role,messages,**kwargs):
            calls.append(messages[-1]['content'])
            return json.dumps({'entrypoint':'index.html','checks':[
                {'ids':['DONE:1'],'kind':'NODE','code':"const api=await loadModule('logic.js');assert.equal(api.value,1);"},
                {'ids':['VERIFY:1'],'kind':'NODE','code':"const api=await loadModule('logic.js');assert.ok(api);"}]})
        with patch.object(api,'_model_text',side_effect=model) as author:
            executable=api._host_verification_phase('batched-contract',phase,{'file_manifest':[
                {'path':'index.html'},{'path':'logic.js'}]},str(self.root))
        self.assertEqual(1,len(calls),'Missing criteria must not fan out into one slow model call per bullet')
        self.assertIn('DONE:1',calls[0])
        self.assertIn('VERIFY:1',calls[0])
        schema=author.call_args.kwargs['structured_schema']['properties']
        self.assertEqual(2,schema['checks']['minItems'])
        self.assertEqual(2,schema['checks']['maxItems'])
        self.assertEqual(1,schema['checks']['items']['properties']['ids']['maxItems'])
        self.assertFalse(executable['host_partial_verification'])
        self.assertTrue(api._run_read_only_verification(executable,str(self.root))[1]['passed'])

    def test_verifier_authoring_failure_preserves_the_completed_source_repair_and_node_run(self):
        from dataclasses import replace
        from olcr_api.runtime import Runtime
        sources={'index.html':'<h1>App</h1>','value.js':'module.exports={value:0};',
                 'value.test.js':"require('node:test')('value',()=>require('node:assert/strict').equal(require('./value.js').value,1));"}
        for path,source in sources.items():(self.root/path).write_text(source)
        class Model:
            def generate(self,messages,model,**kwargs):
                if 'edits' in kwargs.get('format',{}).get('properties',{}):
                    return {'text':json.dumps({'reason':'Restore the required exported value','edits':[
                        {'path':'value.js','start_line':1,'end_line':1,'replacement':'module.exports={value:1};'}]})}
                return {'text':json.dumps({'change_required':True,'source_inspected':True,'condition_evaluated':True,
                    'reason_code':'Repair actual failing assertion','operations':[{'op':'patch','path':'value.js',
                        'expected_old_fragment':'value:0','replacement_fragment':'value:1'}],'verification':'Host verifies'})}
        phase={**self.phase,'goal':'Final acceptance','execution_mode':'VERIFICATION_ONLY',
               'host_verification':True,'host_verification_version':3,'verification_repair_allowed':True,
               'done':['All tests pass.'],'verify':['Source exists']}
        plan={**self.plan,'file_manifest':[{'path':path,'action':'modify'} for path in sources],
              'scope':{'allowed':list(sources),'forbidden':[]},'phases':[phase]}
        api.db.create_coding_task('repair-checkpoint','conversation','Fix the offline app and run tests.',
                                 'QUEUED','NONE',plan,time.time())
        api.runtime=Runtime(replace(api.settings,allowed_roots=[str(self.root)]),api.db,api.retrieval,Model())
        original=api._host_verification_phase
        def author(*args,**kwargs):
            if 'value:1' in (self.root/'value.js').read_text():
                raise RuntimeError('VERIFICATION_CONTRACT_INVALID: controlled author failure')
            return original(*args,**kwargs)
        with patch.object(api,'_host_verification_phase',side_effect=author), \
                patch.object(api,'_review_verification_failure',return_value={'verdict':'SOURCE_DEFECT','reason':'The actual value is zero, contradicting the required value one.','source_fragment':'value:0'}), \
                patch.object(api,'_review_project_test_failures',return_value={'verdict':'SOURCE_DEFECT',
                    'reason':'The current module value does not satisfy the project test.',
                    'requirement':'Fix the offline app and run tests.','source_fragment':'module.exports={value:0};',
                    'repair_paths':['value.js'],'evidence_origin':'CURRENT_PROJECT_TEST_REVIEW'}), \
                patch.object(api,'coding_debug_reference_context',return_value=''):
            api.run_managed_task('repair-checkpoint',str(self.root))
        saved=api.db.coding_task('repair-checkpoint')
        self.assertEqual('RESUMABLE',saved['status'])
        self.assertEqual('VERIFICATION_CONTRACT_INVALID',saved['recovery_reason'])
        reports=api.db.coding_phase_reports('repair-checkpoint')
        report=reports[-1]['structured_report']
        self.assertTrue(report['verification_pending'])
        self.assertIn(str(self.root/'value.js'),report['changed_files'])
        self.assertTrue(report['test_pass'])
        self.assertTrue(any(r['passed']==1 for op in report['typed_execution_summary']['operations']
                            for r in (op.get('output') or {}).get('test_runs') or []))
        self.assertIn('value.js',saved['resume_state']['stop_progress']['text'])
        self.assertIn('controlled author failure',saved['resume_state']['stop_progress']['text'])
        self.assertEqual(0,saved['recovery_epoch'])
        api.db.update_coding_task('repair-checkpoint',status='QUEUED',pause_requested=0,recovery_action='NONE')
        response=json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'NODE',
            'code':"assert.ok(readFile('value.js').includes('value:1'));"}]})
        with patch.object(api.runtime,'execute',side_effect=AssertionError('Completed source repair must not repeat')), \
                patch.object(api,'_model_text',return_value=response), patch.object(api,'_review_verification_failure',return_value={'verdict':'CHECK_VALID'}):
            api.run_managed_task('repair-checkpoint',str(self.root))
        self.assertEqual('COMPLETED',api.db.coding_task('repair-checkpoint')['status'])
        self.assertEqual('module.exports={value:1};',(self.root/'value.js').read_text())

    def test_resume_natural_japanese_plan_reuses_saved_writes_before_dispatching_ui(self):
        plan={**self.plan,'scope':{'allowed':['index.html','app.js','logic.js','logic.test.js'],'forbidden':[]},
              'phases':[
                  {'id':'p1','goal':'ロジックとテストを作成する','status':'pending',
                   'execution_mode':'IMPLEMENTATION_AND_VERIFICATION','dependencies':[],
                   'done':['logic.jsにAPIを定義する','logic.test.jsはnode:testを使用する'],
                   'verify':['Node.jsでrequire/import可能であることを確認する']},
                  {'id':'p2','goal':'UIを作成する','status':'pending','execution_mode':'IMPLEMENTATION_AND_VERIFICATION',
                   'done':['index.htmlに画面を作成する','app.jsがlogic.jsのAPIを呼び出す'],
                   'verify':['ブラウザで動作確認する'],'dependencies':['p1']},
                  {'id':'p3','goal':'統合テスト','status':'pending','execution_mode':'VERIFICATION_ONLY',
                   'done':['テストが成功する'],'verify':[],'dependencies':['p2']}]}
        operations=[]
        for name in ('logic.js','logic.test.js'):
            target=self.root/name;target.write_text('// Saved model output')
            operations.extend([
                {'tool':'workspace_write','status':'success','output':{'path':str(target),'net_changed':True}},
                {'tool':'workspace_read','status':'success','output':{'path':str(target)}}])
        api.db.create_coding_task('reschedule','conversation','Create the offline app and verify it in the browser','QUEUED','NONE',plan,time.time())
        api.db.update_coding_task('reschedule',current_phase_id='p1',recovery_epoch=2,
            task_total_plan_repair_count=3,task_total_recovery_model_calls=5)
        api.db.add_coding_phase_report('reschedule','p1',2,{
            'phase_id':'p1','attempt':2,'plan_revision':0,'recovery_epoch':2,'status':'PASS',
            'execution_attempt_started':True,'execution_attempt_completed':True,
            'changed_files':[str(self.root/'logic.js'),str(self.root/'logic.test.js')],
            'errors':[],'test_fail':[],'typed_execution_summary':{'operations':operations,'state':'completed'},
            'manager_decision':{'decision':'REPLAN_REQUIRED','reason':'Import check has no executor'}},'PASS',time.time())
        with patch.object(api.runtime,'execute',side_effect=RuntimeError('UI dispatch reached')) as execute, \
                patch.object(api,'_replan_task') as replan, patch.object(api,'_model_text') as model:
            with self.assertRaisesRegex(RuntimeError,'UI dispatch reached'):
                api._run_managed_task('reschedule',str(self.root))
        replan.assert_not_called();model.assert_not_called()
        self.assertEqual('p2',execute.call_args.kwargs['managed_context']['implementation_contract']['phase_id'])
        saved=api.db.coding_task('reschedule')
        self.assertEqual((2,3,5),(saved['recovery_epoch'],saved['task_total_plan_repair_count'],saved['task_total_recovery_model_calls']))
        self.assertEqual('PASS',api.db.coding_phase_reports('reschedule')[0]['structured_report']['manager_decision']['decision'])
        self.assertTrue(saved['plan']['phases'][-1]['host_verification'])
        self.assertIn(plan['phases'][0]['verify'][0],saved['plan']['phases'][-1]['verify'])

    def test_structured_planning_has_room_for_the_whole_phase_contract(self):
        api.db.create_coding_task('planning-budget','conversation','Plan a coding task',
                                 'PLANNING','NONE',None,time.time())
        with patch.object(api.runtime.model,'generate',return_value={'text':'{}'}) as generate:
            api._model_text('planning-budget','PLANNING','QWEN_PLANNING',api.settings.planner_model,
                [{'role':'user','content':'Plan the project'}],structured_schema={'type':'object','properties':{'phases':{'type':'array'}}})
        self.assertEqual(8192,generate.call_args.kwargs['max_tokens'])
        self.assertEqual(api.settings.planner_model,generate.call_args.args[1])

    def test_lean_planner_bounds_summaries_and_records_raw_responses(self):
        goal='Create a new app and run tests.'
        api.db.create_coding_task('plan-bounds','conversation',goal,'PLANNING','NONE',None,time.time())
        with patch.object(api.runtime.model,'generate',return_value={'text':'invalid plan','finish_reason':'length','completion_tokens':8192}) as generate:
            api._generate_plan('plan-bounds',goal,'Plan the supplied specification.','QWEN_PLANNING',
                {'planning_policy':{'mode':'LEAN'}})
        schema=generate.call_args.kwargs['format']
        self.assertEqual(goal,schema['properties']['original_goal']['const'])
        self.assertNotIn('tasks',schema['properties'])
        self.assertIn('file_manifest',schema['required'])
        self.assertIn('owner_phase',schema['properties']['file_manifest']['items']['required'])
        self.assertEqual(12,schema['properties']['scope']['properties']['forbidden']['maxItems'])
        self.assertEqual(80,schema['properties']['scope']['properties']['allowed']['maxItems'])
        phases=schema['properties']['phases']
        self.assertEqual(4,phases['maxItems'])
        self.assertEqual(500,phases['items']['properties']['goal']['maxLength'])
        self.assertEqual(8,phases['items']['properties']['verify']['maxItems'])
        records=[json.loads(p.read_text()) for p in (Path(api.db.path).parent/'coding-model-responses').glob('*.json')]
        records=[r for r in records if r.get('task_id')=='plan-bounds']
        self.assertEqual(2,len(records))
        self.assertEqual({'QWEN_PLANNING','QWEN_PLAN_SCHEMA_REPAIR'},{r['stage'] for r in records})
        self.assertTrue(all(r['response']=='invalid plan' and r['finish_reason']=='length' for r in records))

    def test_coherent_static_plan_bound_does_not_truncate_an_existing_recovery_graph(self):
        requirements={'planning_policy':{'mode':'LEAN','coherent_static_application':True},
                      '_replan_phase_limit':2}
        with patch.object(api,'_model_text',return_value=None) as generate,\
             patch.object(api,'_record_recovery_model_call',return_value=True):
            for recovery,bound in ((False,2),(True,2)):
                api._generate_plan('coherent','Build the supplied offline app.','Plan the supplied definition.',
                    'QWEN_REPLANNING' if recovery else 'QWEN_PLANNING',requirements,recovery=recovery)
                schema=generate.call_args.kwargs['structured_schema']
                self.assertEqual(bound,schema['properties']['phases']['maxItems'])
                self.assertIn('owner_phase',schema['properties']['file_manifest']['items']['required'])
            explicit={**requirements,'_explicit_instruction_revision':True}
            api._generate_plan('coherent','Build the supplied offline app.','Plan the supplied definition.',
                'QWEN_REPLANNING',explicit,recovery=True)
            self.assertEqual(4,generate.call_args.kwargs['structured_schema']['properties']['phases']['maxItems'])

    def test_automatic_replanning_limits_proposal_to_existing_unfinished_phases(self):
        second={'id':'p2','goal':'Verify the application','status':'pending','done':['checks pass'],
                'verify':['Run the approved checks'],'dependencies':['p1'],'risks':[],
                'execution_mode':'VERIFICATION_ONLY'}
        plan={**self.plan,'phases':[self.phase,second]}
        api.db.create_coding_task('phase-limit','conversation','Fix probe and run tests','QUEUED','NONE',plan,time.time())
        requirements={'planning_policy':{'mode':'LEAN'}}
        api.db.update_coding_task('phase-limit',requirements=requirements)
        with patch.object(api,'_generate_plan',return_value=(None,None)) as generate:
            api._replan_task('phase-limit',api.db.coding_task('phase-limit'),set(),'an implementation phase failed')
        self.assertEqual(2,generate.call_args.kwargs['requirements']['_replan_phase_limit'])

    def test_automatic_replanning_rejects_a_provider_response_over_the_phase_limit(self):
        second={'id':'p2','goal':'Verify the application','status':'pending','done':['checks pass'],
                'verify':['Run the approved checks'],'dependencies':['p1'],'risks':[],
                'execution_mode':'VERIFICATION_ONLY'}
        plan={**self.plan,'phases':[self.phase,second]}
        api.db.create_coding_task('phase-overflow','conversation','Fix probe and run tests','QUEUED','NONE',plan,time.time())
        proposed={**plan,'phases':[self.phase,second,{'id':'p3','goal':'Extra recovery phase','status':'pending',
            'done':['extra work passes'],'verify':['extra work is checked'],'dependencies':[],
            'risks':[],'execution_mode':'IMPLEMENTATION'}]}
        with patch.object(api,'_generate_plan',return_value=(proposed,'{}')):
            response=api._replan_task('phase-overflow',api.db.coding_task('phase-overflow'),set(),'an implementation phase failed')
        saved=api.db.coding_task('phase-overflow')
        self.assertIn('phase limit',response)
        self.assertEqual('RESUMABLE',saved['status'])
        self.assertEqual('REPLAN_PHASE_LIMIT',saved['recovery_reason'])
        self.assertEqual(2,len(saved['plan']['phases']))

    def test_automatic_replanning_preserves_phase_identity_when_provider_renames_same_count(self):
        second={'id':'p2','goal':'Update the requested screen','status':'pending','done':['screen updated'],
                'verify':[],'dependencies':['p1'],'risks':[],'execution_mode':'IMPLEMENTATION'}
        current_plan={**self.plan,'phases':[self.phase,second]}
        api.db.create_coding_task('phase-rename','conversation','Fix probe','QUEUED','NONE',current_plan,time.time())
        api.db.update_coding_task('phase-rename',requirements={'planning_policy':{'mode':'LEAN'}})
        renamed=copy.deepcopy(current_plan)
        renamed['phases'][0]['id']='replacement-a'
        renamed['phases'][1]['id']='replacement-b'
        renamed['phases'][1]['dependencies']=['replacement-a']
        with patch.object(api,'_generate_plan',return_value=(renamed,'{}')):
            response=api._replan_task('phase-rename',api.db.coding_task('phase-rename'),set(),
                                      'an implementation phase failed')

        saved=api.db.coding_task('phase-rename')
        self.assertNotIn('phase limit',response)
        self.assertEqual(['p1','p2'],[phase['id'] for phase in saved['plan']['phases']])
        self.assertEqual(['p1'],saved['plan']['phases'][1]['dependencies'])

    def test_actual_verification_improvement_extends_only_the_bounded_source_budget(self):
        phase={'host_verification':True}
        def report(passed,changed):
            return {'plan_revision':0,'recovery_epoch':1,'accepted_mutations':changed,'typed_execution_summary':{'operations':[
                {'tool':'verification_command','output':{'passed_criteria_ids':['x']*0,'test_runs':[{'registered_tests':7,'passed':passed}]}}]}}
        history=[report(0,0),report(2,1),report(2,1)]
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+2,api._phase_retry_limit(phase,report(5,1),history))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit(phase,report(2,1),history))

    def test_readonly_failures_do_not_consume_mutation_progress_budget(self):
        report={'execution_expectations':{'requires_repo_mutation':False},'typed_execution_summary':{'operations':[
            {'tool':'verification_command','status':'failed','output':{'returncode':1}}]}}
        self.assertEqual('',api._zero_progress_signature(report))
        report['execution_expectations']['requires_repo_mutation']=True
        self.assertTrue(api._zero_progress_signature(report))

    def test_initial_readonly_acceptance_does_not_spend_a_source_repair(self):
        phase={'host_verification':True,'verification_repair_allowed':True}
        baseline={'execution_expectations':{'requires_repo_mutation':False},
                  'typed_execution_summary':{'operations':[{'tool':'verification_command','status':'failed'}]}}
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit(phase,baseline,[]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit(phase,baseline,[baseline]*10))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE,api._phase_retry_limit({'host_verification':True},baseline,[]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE,api._phase_retry_limit(phase,{'recovery_epoch':1},[baseline]))

    def test_legacy_long_model_repairs_get_only_one_existing_debug_role_handoff_credit(self):
        phase={'host_verification':True,'verification_repair_allowed':True}
        prior={'implementation_model':api.settings.long_implementation_model,'test_fail':['actual failure']}
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit(phase,prior,[]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit(phase,prior,[prior]*10))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE,api._phase_retry_limit({**phase,'model_role':'role:long_implementation'},prior,[]))

    def test_syntax_encoding_recovery_is_separate_from_one_role_handoff(self):
        phase={'host_verification':True,'verification_repair_allowed':True}
        report={'implementation_model':api.settings.long_implementation_model,'test_fail':['actual failure'],
                'typed_execution_summary':{'operations':[{'tool':'operation_application_failure','output':{
                    'operation_diagnostic':{'reason_subtype':'STRUCTURAL_VALIDATION_FAILED','relative_target_path':'app.js'}}}]}}
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+2,api._phase_retry_limit(phase,report,[]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+2,api._phase_retry_limit(phase,report,[report]*20))
        self.assertEqual(['app.js'],api._bounded_review_context(report)['invalid_source_paths'])
        self.assertEqual('WRITE_CONTENT_INVALID',api._bounded_review_context(report)['typed_failure_class'])

    def test_cosmetic_edits_with_the_same_failure_allow_one_focused_handoff(self):
        def report(changed=1,passed=('VERIFY:1',),tests=13,command='same',epoch=0,contract_error=False,kind='NODE'):
            return {'accepted_mutations':changed,'plan_revision':0,'recovery_epoch':epoch,
                'implementation_model':api.settings.short_coding_model,
                'typed_execution_summary':{'operations':[{'tool':'verification_command','input':{'command':command},
                    'output':{'passed_criteria_ids':list(passed),'test_runs':[{'passed':tests,'registered_tests':13}],
                        'failures':[{'ids':['VERIFY:2'],'kind':kind,'contract_error':contract_error}]}}]}}
        baseline=report(changed=0)
        cosmetic=report()
        self.assertTrue(api._verification_repair_stalled([baseline,cosmetic]))
        self.assertFalse(api._verification_repair_stalled([baseline,report(changed=0)]))
        self.assertFalse(api._verification_repair_stalled([baseline,report(passed=('VERIFY:1','VERIFY:3'))]))
        self.assertFalse(api._verification_repair_stalled([report(changed=0,tests=12),report(tests=13)]))
        self.assertTrue(api._verification_repair_stalled([baseline,report(tests=14)]))
        self.assertTrue(api._verification_repair_stalled([report(changed=0,kind='BROWSER'),report(tests=14,kind='BROWSER')]))
        self.assertFalse(api._verification_repair_stalled([baseline,report(command='repaired-contract')]))
        self.assertFalse(api._verification_repair_stalled([baseline,report(epoch=1)]))
        self.assertFalse(api._verification_repair_stalled([baseline,report(contract_error=True)]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit({},cosmetic,[baseline]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit({},cosmetic,[baseline,cosmetic]*10))

    def test_more_green_tests_do_not_extend_a_failed_browser_repair(self):
        def report(count,changed):
            return {'accepted_mutations':changed,'implementation_model':api.settings.long_implementation_model,
                'typed_execution_summary':{'operations':[{'tool':'verification_command','input':{'command':'same'},
                    'output':{'passed_criteria_ids':['DONE:1'],'test_runs':[{'registered_tests':count,'passed':count}],
                        'failures':[{'ids':['VERIFY:1'],'kind':'BROWSER'}]}}]}}
        before=report(13,0)
        after=report(14,1)
        self.assertTrue(api._verification_repair_stalled([before,after]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE,api._phase_retry_limit({'host_verification':True},after,[before]))

    def test_different_node_assertion_with_equal_pass_count_does_not_force_role_handoff(self):
        def report(name,changed):
            return {'accepted_mutations':changed,'typed_execution_summary':{'operations':[
                {'tool':'verification_command','input':{'command':['owned','same']},'output':{
                    'passed_criteria_ids':[],'failures':[{'ids':['REQUIRED_TESTS'],'kind':'NODE'}],
                    'test_runs':[{'registered_tests':7,'passed':6,'failures':[{'name':name}]}]}}]}}
        before=report('avoid previous dish',0)
        after=report('isolated history starts empty',1)
        self.assertFalse(api._verification_repair_stalled([before,after]))
        self.assertTrue(api._verification_repair_stalled([after,after]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE,api._phase_retry_limit({'host_verification':True},after,[before]))

    def test_acceptance_counterexample_survives_bounded_retry_context(self):
        failure={'ids':['VERIFY:1'],'error':'AssertionError: wrong result\n    at '+('stack '*1000),
            'check_source':"await page.locator('#input').fill('A');assert.equal(result,'A');",
            'assertion':{'actual':"'B'",'expected':"'A'",'operator':'strictEqual'},
            'observed_page':{'text':'Result: B','fields':[{'id':'input','value':'A'}]},
            'browser_errors':[{'message':'Assignment to constant variable.','source_paths':['app.js'],
                'source_locations':[{'path':'app.js','line':42,'column':5,'source_line':'state = recordLunch(state, result.selected);'}]}]}
        report={'test_fail':[json.dumps(failure)],'typed_execution_summary':{'operations':[
            {'tool':'verification_command','output':{'failures':[failure]}}]}}
        context=api._bounded_review_context(report)
        item=context['acceptance_failures'][0]
        self.assertEqual(failure['check_source'],item['check_source'])
        self.assertEqual(failure['assertion'],item['assertion'])
        self.assertEqual('A',item['observed_page']['fields'][0]['value'])
        self.assertNotIn('stack',item['error'])
        self.assertEqual(failure['browser_errors'][0]['source_locations'],item['browser_errors'][0]['source_locations'])

    def test_new_browser_progress_is_preserved_when_another_test_regresses(self):
        phase={'host_verification':True,'done':['Tests pass'],'verify':['First behavior','Second behavior']}
        def report(ids,passed,changed):
            return {'plan_revision':0,'recovery_epoch':0,'final_worktree_mutations':changed,
                'typed_execution_summary':{'operations':[{'tool':'verification_command','input':{'command':['owned-runner','same-contract']},
                    'output':{'passed_criteria_ids':ids,'test_runs':[{'registered_tests':13,'passed':passed}]}}]}}
        initial=report(['VERIFY:1'],13,0)
        progress=report(['VERIFY:2'],12,1)
        regained=report(['VERIFY:1'],13,1)
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit(phase,progress,[initial]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit(phase,regained,[initial,progress]))
        additions=[report([],n,1) for n in range(20)]
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+2,api._phase_retry_limit({'host_verification':True},additions[-1],additions[:-1]))

    def test_accepted_idempotent_patch_is_not_filesystem_progress(self):
        report={'accepted_mutations':1,'files_written':1,'final_worktree_mutations':0,
                'mutation_classification':'IDEMPOTENT_NOOP','implementation_model':api.settings.short_coding_model,
                'test_fail':['actual assertion failure'],'typed_execution_summary':{'operations':[]}}
        self.assertTrue(api._zero_progress_signature(report))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit({},report,[]))
        report['manager_decision']={'decision':'PASS'}
        self.assertEqual('',api._zero_progress_signature(report))

    def test_restored_noops_allow_one_untried_handoff_then_retain_the_stop_rule(self):
        api.db.create_coding_task('restored-noops','conversation','Fix actual failing tests','RUNNING','NONE',self.plan,time.time())
        def record(attempt,model):
            report={'phase_id':'p1','attempt':attempt,'plan_revision':0,'recovery_epoch':0,
                'execution_attempt_started':True,'implementation_model':model,'accepted_mutations':1,
                'final_worktree_mutations':0,'mutation_classification':'IDEMPOTENT_NOOP',
                'test_fail':['actual failing test'],'typed_execution_summary':{'operations':[]}}
            api.db.add_coding_phase_report('restored-noops','p1',attempt,report,'PASS',time.time())
        record(0,api.settings.short_coding_model)
        record(1,api.settings.short_coding_model)
        repeated,signature,count=api._repeated_zero_progress_failure('restored-noops',0)
        self.assertFalse(repeated)
        self.assertEqual(2,count)
        record(2,api.settings.long_implementation_model)
        repeated,signature,count=api._repeated_zero_progress_failure('restored-noops',0)
        self.assertTrue(repeated)
        self.assertEqual(3,count)

    def test_verifier_repair_does_not_consume_source_correction_budget(self):
        current={'plan_revision':2,'recovery_epoch':3}
        prior={**current,'typed_execution_summary':{'operations':[{'output':{'failures':[{'contract_error':True}]}}]}}
        phase={'host_verification':True}
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit(phase,current,[prior,prior]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE+1,api._phase_retry_limit(phase,prior,[]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE,api._phase_retry_limit(phase,{**current,'recovery_epoch':4},[prior]))
        self.assertEqual(api.MAX_RETRIES_PER_PHASE,api._phase_retry_limit({},current,[prior]))

    def test_direct_model_failure_preserves_resumable_progress(self):
        api.db.create_coding_task('model-error','conversation','Fix probe','RUNNING','NONE',self.plan,time.time())
        with patch.object(api,'_run_managed_task',side_effect=ModelFailure('mlx_runtime','observed model failure')):
            with self.assertRaises(ModelFailure):
                api.run_managed_task('model-error',str(self.root))
        saved=api.db.coding_task('model-error')
        self.assertEqual('RESUMABLE',saved['status'])
        self.assertEqual('MODEL_CALL',saved['recovery_reason'])
        self.assertTrue(saved['resume_state']['stop_progress'])

    def test_unexpected_runner_failure_does_not_leave_running_state(self):
        api.db.create_coding_task('runner-error','conversation','Fix probe','RUNNING','NONE',self.plan,time.time())
        with patch.object(api,'_run_managed_task',side_effect=TypeError('internal provider failure')):
            with self.assertRaises(TypeError):
                api.run_managed_task('runner-error',str(self.root))
        saved=api.db.coding_task('runner-error')
        self.assertEqual('RESUMABLE',saved['status'])
        self.assertEqual('RUNNER',saved['recovery_reason'])
        self.assertNotIn('runner-error',api._active_runners)
        self.assertTrue(saved['resume_state']['stop_progress'])

    def test_saved_host_pass_is_invalidated_by_new_requirements_or_changed_source(self):
        target=self.root/'index.html';target.write_text('<h1>App</h1>')
        phase={**self.phase,'host_verification':True,'execution_mode':'VERIFICATION_ONLY',
               'done':['Original requirement'],'verify':['Read-only verification succeeds'],'status':'pass'}
        api.db.create_coding_task('host-pass','conversation','Verify actual source','RUNNING','NONE',
                                 {**self.plan,'phases':[phase]},time.time())
        report={'phase_id':'p1','attempt':0,'plan_revision':0,'recovery_epoch':0,'status':'PASS',
            'criteria_evidence':{'Original requirement':True,'Read-only verification succeeds':True},'manager_decision':{'decision':'PASS'},
            'typed_execution_summary':{'operations':[{'tool':'verification_command','status':'success','output':{
                'returncode':0,'target_sha256':{'index.html':hashlib.sha256(target.read_bytes()).hexdigest()}}}]}}
        api.db.add_coding_phase_report('host-pass','p1',0,report,'PASS',time.time())
        self.assertTrue(api._phase_passed_in_epoch('host-pass',phase,0,0,str(self.root)))
        self.assertFalse(api._phase_passed_in_epoch('host-pass',{**phase,'verify':['New requirement']},0,0,str(self.root)))
        target.write_text('<h1>Changed source</h1>')
        self.assertFalse(api._phase_passed_in_epoch('host-pass',phase,0,0,str(self.root)))

    def test_failing_node_tests_are_repaired_before_authoring_browser_checks(self):
        (self.root/'index.html').write_text('<h1>App</h1><script>console.error("actual browser failure")</script>')
        (self.root/'package.json').write_text('{"type":"module"}')
        (self.root/'module.test.js').write_text("const test=require('node:test');test('registered test',()=>{});")
        api.db.create_coding_task('preflight','conversation','Create an app and run node:test.',
                                 'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':['All node:test tests pass.',
                'The app loads in a browser without console errors.'],'verify':[]}
        plan={'file_manifest':[{'path':p} for p in ['index.html','package.json','module.test.js']]}
        with patch.object(api,'_model_text',side_effect=AssertionError('standard checks must not need model authoring')), \
             patch.object(api,'_review_verification_failure',return_value={'verdict':'CHECK_DEFECT','reason':'The test deliberately throws before exercising the app.'}), \
             patch.object(api,'_review_project_test_failures',return_value={'verdict':'CHECK_DEFECT',
                'reason':'The registered test deliberately throws before exercising the app.',
                'repair_paths':['module.test.js'],'evidence_origin':'CURRENT_PROJECT_TEST_REVIEW'}):
            failed=api._host_verification_phase('preflight',phase,plan,str(self.root))
            self.assertTrue(failed['host_test_preflight'])
            execution,result=api._run_read_only_verification(failed,str(self.root))
            self.assertFalse(result['passed'])
            self.assertTrue(result['test_runs'])
            self.assertEqual([],result['passed_criteria_ids'])
            self.assertEqual([['REQUIRED_TESTS']],[failure['ids'] for failure in result['failures']])
            self.assertEqual(0,result['browser_checks'])
            (self.root/'package.json').write_text('{}')
            full=api._host_verification_phase('preflight',phase,plan,str(self.root))
            self.assertFalse(full.get('host_test_preflight'))
            execution,result=api._run_read_only_verification(full,str(self.root))
            (self.root/'module.test.js').write_text("require('node:test')('real test',()=>{throw Error('regression')});")
            cached=api._host_verification_phase('preflight',phase,plan,str(self.root))
            self.assertTrue(cached.get('host_test_preflight'))
            cached_result=api._run_read_only_verification(cached,str(self.root))[1]
        self.assertFalse(result['passed'],'Green Node tests alone must not prove the browser criteria')
        self.assertEqual(0,result['test_runs'][0]['returncode'])
        self.assertIn('actual browser failure',result['output'])
        self.assertTrue(cached_result['test_runs'][0]['failed'])
        self.assertEqual(0,cached_result['browser_checks'],'A failing test preflight is not a measured browser failure')

    def test_explicit_verification_executor_is_preserved(self):
        phase={'id':'verify','host_verification':True,'verification_command':['node','--test'],
               'verification_targets':['actual.test.js'],'done':['Tests pass'],'verify':[]}
        with patch.object(api,'_model_text',side_effect=AssertionError('Do not replace the approved executor')):
            result=api._host_verification_phase('explicit',phase,{},str(self.root))
        self.assertEqual(phase,result)

    def test_replanned_node_only_command_cannot_replace_owned_browser_acceptance(self):
        (self.root/'index.html').write_text('<h1>Actual app</h1>')
        (self.root/'logic.js').write_text('module.exports={};')
        (self.root/'logic.test.js').write_text("require('node:test')('real test',()=>require('node:assert/strict').equal(1,1));")
        original='Verify the browser heading and node:test.'
        api.db.create_coding_task('node-browser','conversation',original,'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'verification_command':['node --test logic.test.js'],
               'verification_targets':['logic.test.js'],'done':['The app loads in a browser without console errors.'],
               'verify':['In the browser, heading is Actual app.']}
        plan={'file_manifest':[{'path':name} for name in ('index.html','logic.js','logic.test.js')]}
        value={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':
            "assert.equal(await page.locator('h1').textContent(),'Actual app');"}]}
        with patch.object(api,'_model_text',return_value=json.dumps(value)):
            materialized=api._host_verification_phase('node-browser',phase,plan,str(self.root))
        self.assertEqual(['node','--test','logic.test.js'],materialized['host_declared_test_command'])
        execution,result=api._run_read_only_verification(materialized,str(self.root))
        self.assertTrue(result['passed'],result['output'])
        self.assertEqual(1,result['test_runs'][0]['registered_tests'])
        self.assertGreaterEqual(result['browser_checks'],2)
        self.assertEqual({'DONE:1','VERIFY:1'},set(result['passed_criteria_ids']))

    def test_resuming_replacement_plan_restores_owned_gate_from_actual_runner_provenance(self):
        sources={'index.html':'<h1>Actual app</h1>','app.js':'const appReady=true;',
            'logic.test.js':"require('node:test')('real test',()=>require('node:assert/strict').equal(1,1));"}
        for name,source in sources.items():(self.root/name).write_text(source)
        phase={'id':'final','goal':'Verify the browser application','status':'pending','execution_mode':'VERIFICATION_ONLY',
            'done':['The app loads in a browser without console errors.'],'verify':['In the browser, heading is Actual app.'],
            'dependencies':[],'risks':[],'verification_command':['node --test logic.test.js'],
            'verification_targets':['logic.test.js']}
        plan={**self.plan,'scope':{'allowed':list(sources),'forbidden':[]},'phases':[phase],
              'file_manifest':[{'path':name,'action':'modify','required':True} for name in sources]}
        api.db.create_coding_task('lost-gate','conversation','Verify the browser heading and node:test.','QUEUED','NONE',plan,time.time())
        api.db.update_coding_task('lost-gate',plan_revision=1)
        runner=Path(api.__file__).with_name('coding_verification_runner.cjs').resolve()
        api.db.add_coding_phase_report('lost-gate','final',0,{'plan_revision':0,'status':'FAIL',
            'typed_execution_summary':{'operations':[{'tool':'verification_command','status':'failed',
                'input':{'command':['node',str(runner),'previous-contract.json']},'output':{'returncode':1}}]}},'PASS',time.time())
        check={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':
            "assert.equal(await page.locator('h1').textContent(),'Actual app');"}]}
        with patch.object(api,'_model_text',return_value=json.dumps(check)),\
             patch.object(api.runtime,'execute',side_effect=AssertionError('Real verification must run without source edits')),\
             patch.object(api,'_complete_task',return_value='verified'):
            self.assertEqual('verified',api._run_managed_task('lost-gate',str(self.root)))
        task=api.db.coding_task('lost-gate')
        final=task['plan']['phases'][0]
        self.assertTrue(final['host_verification'])
        self.assertTrue(final['verification_repair_allowed'])
        latest=api.db.coding_phase_reports('lost-gate')[-1]['structured_report']
        commands=[op for op in latest['typed_execution_summary']['operations'] if op['tool']=='verification_command']
        self.assertEqual(1,commands[-1]['output']['test_runs'][0]['registered_tests'])
        self.assertGreaterEqual(commands[-1]['output']['browser_checks'],2)
        self.assertEqual('PASS',latest['manager_decision']['decision'])

    def test_single_string_test_command_executes_and_its_evidence_remains_bound(self):
        (self.root/'logic.test.js').write_text("require('node:test')('real test',()=>require('node:assert/strict').equal(1,1));")
        phase={'id':'p1','execution_mode':'VERIFICATION_ONLY','done':['Tests pass'],'verify':['Tests pass'],
            'verification_command':['node --test logic.test.js'],'verification_targets':['logic.test.js']}
        execution,result=api._run_read_only_verification(phase,str(self.root))
        self.assertTrue(result['passed'],result['output'])
        report={'status':'PASS','test_executed':[' '.join(result['command'])],'test_pass':['executed'],
                'criteria_evidence':{'Tests pass':result['passed']},
                'typed_execution_summary':{'operations':execution.tool_executions}}
        evaluation=evaluate_phase(phase,report,workspace_root=str(self.root))
        self.assertTrue(evaluation['phase_complete'],evaluation)
        invalid=copy.deepcopy(report)
        invalid['typed_execution_summary']['operations'][0]['input']['command'][0]='/unrelated/node'
        self.assertFalse(evaluate_phase(phase,invalid,workspace_root=str(self.root))['phase_complete'])

    def test_executor_start_failure_pauses_without_source_repairs_or_replanning(self):
        (self.root/'probe.py').write_text('ORIGINAL_SOURCE')
        phase={**self.phase,'execution_mode':'VERIFICATION_ONLY','done':['Tests pass'],'verify':['Tests pass'],
            'verification_command':['/missing-test-executable'],'verification_targets':['probe.py']}
        plan={**self.plan,'phases':[phase]}
        api.db.create_coding_task('missing-executor','conversation','Run tests','QUEUED','NONE',plan,time.time())
        with patch.object(api.runtime,'execute',side_effect=AssertionError('No source repair for executor failures')),\
             patch.object(api,'_replan_task',side_effect=AssertionError('No architecture replan for missing executable')),\
             patch.object(api,'run_acceptance',side_effect=FileNotFoundError('Missing test executable')):
            response=api._run_managed_task('missing-executor',str(self.root))
        self.assertIn('executor failed',response)
        task=api.db.coding_task('missing-executor')
        self.assertEqual('RESUMABLE',task['status'])
        self.assertEqual(0,task['replan_count'])
        self.assertEqual('ORIGINAL_SOURCE',(self.root/'probe.py').read_text())
        reports=api.db.coding_phase_reports('missing-executor')
        self.assertEqual(1,len(reports))
        self.assertEqual('VERIFICATION_EXECUTOR',reports[0]['structured_report']['manager_decision']['retry_channel'])
        # A host-marked phase must also preserve its explicit executor on
        # resume, rather than author a different passing check or edit source.
        api.db.update_coding_task('missing-executor',status='QUEUED',plan={**plan,'phases':[{**phase,'host_verification':True}]})
        with patch.object(api.runtime,'execute',side_effect=AssertionError('No source repair on executor resume')),\
             patch.object(api,'_model_text',side_effect=AssertionError('Do not replace an unavailable explicit executor')),\
             patch.object(api,'_replan_task',side_effect=AssertionError('Do not replan for an executor start failure')),\
             patch.object(api,'run_acceptance',side_effect=FileNotFoundError('Still missing test executable')):
            response=api._run_managed_task('missing-executor',str(self.root))
        self.assertIn('verification executor is unavailable',response)
        self.assertEqual('RESUMABLE',api.db.coding_task('missing-executor')['status'])
        self.assertEqual(1,len(api.db.coding_phase_reports('missing-executor')))
        self.assertIn('Still missing test executable',api.db.coding_task('missing-executor')['resume_state']['verification_preparation_failure']['error'])
        self.assertEqual(0,api.db.coding_task('missing-executor')['recovery_epoch'])

    def test_incomplete_acceptance_checks_are_preserved_and_only_missing_ids_are_generated(self):
        (self.root/'index.html').write_text('<h1>App</h1>')
        api.db.create_coding_task('partial','conversation','Verify the actual app','RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':['Source exists','Heading contains App'],'verify':[]}
        def response(ids):
            return json.dumps({'entrypoint':'index.html','checks':[{'ids':ids,'kind':'NODE','code':"assert.ok(readFile('index.html').includes('App'));"}]})
        with patch.object(api,'_model_text',side_effect=[response(['DONE:1']),response(['DONE:2'])]) as model:
            result=api._host_verification_phase('partial',phase,{'file_manifest':[{'path':'index.html'}]},str(self.root))
        prompt=model.call_args.args[4][1]['content']
        requested=json.loads(prompt.split('CRITERIA:\n',1)[1].split('\nACTUAL_SOURCE:',1)[0])
        self.assertEqual({'DONE:2':'Heading contains App'},requested)
        contract=json.loads(Path(result['verification_command'][2]).read_text())
        self.assertEqual([['DONE:1'],['DONE:2']],[c['ids'] for c in contract['checks']])

    def test_acceptance_cache_preserves_checks_for_repairs_and_invalidates_on_user_instruction(self):
        (self.root/'index.html').write_text('<h1>App</h1>')
        api.db.create_coding_task('cached','conversation','Verify the actual app','RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':['Application works'],'verify':[]}
        contract=json.dumps({'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'NODE','code':"assert.ok(readFile('index.html').includes('App'));"}]})
        with patch.object(api,'_model_text',return_value=contract) as model,patch.object(api.task_workspaces.service,'instruction_context',return_value=''):
            api._host_verification_phase('cached',phase,{'file_manifest':[{'path':'index.html'}]},str(self.root))
            (self.root/'index.html').write_text('<h1>App repaired</h1>')
            api._host_verification_phase('cached',phase,{'file_manifest':[{'path':'index.html'}]},str(self.root))
            self.assertEqual(1,model.call_count)
            with patch.object(api.task_workspaces.service,'instruction_context',return_value='Make the heading red'):
                api._host_verification_phase('cached',phase,{'file_manifest':[{'path':'index.html'}]},str(self.root))
            self.assertEqual(2,model.call_count)
            self.assertIn('Make the heading red',model.call_args.args[4][1]['content'])

    def test_repaired_check_context_does_not_leak_into_the_next_criterion(self):
        (self.root/'index.html').write_text('<h1>App</h1>')
        api.db.create_coding_task('no-leak','conversation','Verify source','RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':['Source exists','Source contains App'],'verify':[]}
        prompts=[]
        def author(task_id,status,activity,role,messages,**kwargs):
            if 'You review the meaning' in messages[0]['content']:
                return json.dumps({'verdict':'CHECK_VALID','reason':'Original assertion preserved.',
                    'requirement':'Source exists','source_fragment':''})
            prompt=messages[1]['content'];prompts.append(prompt)
            identifier='DONE:2' if len(prompts)==3 else 'DONE:1'
            code=("oldBrokenHelper();assert.ok(true);" if len(prompts)==1
                  else "assert.ok(readFile('index.html').includes('App'));")
            return json.dumps({'entrypoint':'index.html','checks':[{'ids':[identifier],'kind':'NODE','code':code}]})
        with patch.object(api,'_model_text',side_effect=author):
            api._host_verification_phase('no-leak',phase,{'file_manifest':[{'path':'index.html'}]},str(self.root))
        self.assertEqual(3,len(prompts))
        self.assertIn('oldBrokenHelper',prompts[1])
        self.assertNotIn('oldBrokenHelper',prompts[2])
        self.assertNotIn('Repair these failed check APIs',prompts[2])

    def test_invalid_acceptance_authoring_is_journaled(self):
        (self.root/'index.html').write_text('<h1>App</h1>')
        api.db.create_coding_task('checks','conversation','Verify the actual app','RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':['Application works'],'verify':[]}
        alternate='alternate-verifier'
        with patch.object(api,'_alternate_coding_model',return_value=alternate), \
             patch.object(api,'_model_text',return_value='invalid JSON') as model:
            with self.assertRaisesRegex(RuntimeError,'VERIFICATION_CONTRACT_INVALID'):
                api._host_verification_phase('checks',phase,{'file_manifest':[{'path':'index.html'}]},str(self.root))
        self.assertIsNotNone(alternate)
        self.assertEqual([api.settings.planner_model,alternate],
                         [call.args[3] for call in model.call_args_list])
        journal=list((self.root/'coding-verification/checks').glob('*-response-*.json'))
        self.assertEqual(2,len(journal))
        self.assertTrue(all(json.loads(p.read_text())['validation_errors'] for p in journal))

    def test_unchanged_verification_failure_is_not_replayed_after_resume(self):
        (self.root/'index.html').write_text('<main>App</main>')
        task_id='verification-no-progress'
        api.db.create_coding_task(task_id,'conversation','Verify the page behavior.','RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':['The page behavior is verified.'],'verify':[]}
        plan={'file_manifest':[{'path':'index.html'}]}
        first_error=None
        with patch.object(api,'_model_text',return_value='not JSON'):
            try:
                api._host_verification_phase(task_id,phase,plan,str(self.root))
            except RuntimeError as exc:
                first_error=exc
        self.assertIsNotNone(first_error)
        self.assertTrue(getattr(first_error,'verification_signature',None))
        api._record_verification_preparation_failure(task_id,phase['id'],first_error)
        with patch.object(api,'_model_text',side_effect=AssertionError('Unchanged verification failure must not invoke the model again')):
            with self.assertRaisesRegex(RuntimeError,'same verification contract already failed'):
                api._host_verification_phase(task_id,phase,plan,str(self.root))
        # A changed implementation is new evidence and can enter normal
        # verification preparation again.
        (self.root/'index.html').write_text('<main>Changed</main>')
        with patch.object(api,'_model_text',return_value=json.dumps({'entrypoint':'index.html','checks':[
            {'ids':['DONE:1'],'kind':'NODE','code':"assert.ok(readFile('index.html').includes('Changed'));"}]})) as model, patch.object(api,'_review_verification_failure',return_value={'verdict':'CHECK_VALID'}):
            executable=api._host_verification_phase(task_id,phase,plan,str(self.root))
        self.assertEqual(1,model.call_count)
        self.assertTrue(api._run_read_only_verification(executable,str(self.root))[1]['passed'])

    def test_invalid_acceptance_authoring_does_not_leave_task_running(self):
        phase={**self.phase,'execution_mode':'VERIFICATION_ONLY','host_verification':True,
               'host_verification_version':2,'done':['Application works'],'verify':[]}
        api.db.create_coding_task('invalid','conversation','Verify the actual app','RUNNING','NONE',
                                 {**self.plan,'phases':[phase]},time.time())
        with patch.object(api,'_host_verification_phase',side_effect=RuntimeError('VERIFICATION_CONTRACT_INVALID: missing checks')):
            api.run_managed_task('invalid',str(self.root))
        saved=api.db.coding_task('invalid')
        self.assertEqual('RESUMABLE',saved['status'])
        self.assertEqual('VERIFICATION_CONTRACT_INVALID',saved['recovery_reason'])
        self.assertTrue(saved['resume_state']['stop_progress'])

    def test_valid_javascript_with_wrong_ui_expectations_is_repaired_as_a_check(self):
        source='<textarea id="input"></textarea>'
        (self.root/'index.html').write_text(source)
        api.db.create_coding_task('semantic-check','conversation','Input preserves multiple lines.',
                                 'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':[],
               'verify':['In the browser, input preserves multiple lines.']}
        calls=[]
        def model(task_id,status,activity,role,messages,**kwargs):
            if 'You review the meaning' in messages[0]['content']:
                calls.append('review')
                return json.dumps({'verdict':'CHECK_DEFECT' if calls.count('author')==1 else 'CHECK_VALID','reason':'Check the actual multiline input.',
                    'requirement':'Input preserves multiple lines.','source_fragment':''})
            calls.append('author')
            code=("await page.locator('#input').fill('A');await page.locator('#input').fill('B');"
                  "assert.equal(await page.locator('#input').inputValue(),'A\\nB');" if calls.count('author')==1
                  else "const text=['A','B'].join(String.fromCharCode(10));await page.locator('#input').fill(text);assert.equal(await page.locator('#input').inputValue(),text);")
            return json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':code}]})
        with patch.object(api,'_model_text',side_effect=model):
            result=api._host_verification_phase('semantic-check',phase,{'file_manifest':[{'path':'index.html'}]},str(self.root))
        self.assertEqual(['author','review','author','review'],calls)
        self.assertEqual(source,(self.root/'index.html').read_text())
        self.assertTrue(api._run_read_only_verification(result,str(self.root))[1]['passed'])
        journals=[json.loads(p.read_text()) for p in (self.root/'coding-verification/semantic-check').glob('*response*.json')]
        self.assertTrue(any((entry.get('failure_review') or {}).get('verdict')=='CHECK_DEFECT' for entry in journals))

    def test_browser_check_source_runtime_error_uses_literal_repair_even_if_review_requests_code(self):
        source='<main><h1>Meal planner</h1></main>'
        (self.root/'index.html').write_text(source)
        task_id='browser-check-source-runtime-error'
        criterion='In the browser, verify that the page shows a visible heading.'
        api.db.create_coding_task(task_id,'conversation','Verify the heading is visible.','RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':[criterion],'verify':[]}
        authors=[]
        reviews=[]

        def model(task_id,status,activity,role,messages,**kwargs):
            if 'You review the meaning' in messages[0]['content']:
                reviews.append(1)
                return json.dumps({'verdict':'CHECK_DEFECT' if len(reviews)==1 else 'CHECK_VALID',
                    'verification_form':'STATEFUL_CODE' if len(reviews)==1 else 'LITERAL_OBSERVATIONS',
                    'reason':'The failure comes from the generated check source; the browser requirement remains mandatory.',
                    'requirement':'The page shows a visible heading.','source_fragment':''})
            authors.append(1)
            if len(authors)==1:
                code='throw new Error("Invalid or unexpected token in verifier check source; readFile is not defined");'
                return json.dumps({'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER','code':code}]})
            schema=kwargs.get('structured_schema') or {}
            properties=schema.get('properties',{}).get('checks',{}).get('items',{}).get('properties',{})
            self.assertNotIn('code',properties,'A check-source runtime error must move the retry to host-compiled observations')
            return json.dumps({'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER','actions':[],
                'observations':[{'selector':'h1','measure':'visible','operator':'eq','expected':True}]}]})

        with patch.object(api,'_model_text',side_effect=model):
            result=api._host_verification_phase(task_id,phase,{'file_manifest':[{'path':'index.html'}]},str(self.root))
        self.assertEqual(2,len(authors))
        self.assertEqual(2,len(reviews))
        self.assertEqual(source,(self.root/'index.html').read_text())
        self.assertTrue(api._run_read_only_verification(result,str(self.root))[1]['passed'])

    def test_wrong_node_api_expectation_is_repaired_as_a_check_before_source_mutation(self):
        (self.root/'index.html').write_text('<h1>App</h1>')
        source='module.exports={choose:()=>({selected:null})};'
        (self.root/'logic.js').write_text(source)
        api.db.create_coding_task('node-meaning','conversation','Return an empty selection for no candidates.',
                                 'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':[],
               'verify':['Return an empty selection for no candidates.']}
        calls=[]
        def model(task_id,status,activity,role,messages,**kwargs):
            if 'You review the meaning' in messages[0]['content']:
                calls.append('review')
                return json.dumps({'verdict':'CHECK_DEFECT' if calls.count('author')==1 else 'CHECK_VALID',
                    'reason':'Observe the actual selection field, not an invented primitive return shape.',
                    'requirement':'Return an empty selection for no candidates.','source_fragment':''})
            calls.append('author')
            code=("const api=await loadModule('logic.js');assert.equal(api.choose([]),null);" if calls.count('author')==1
                  else "const api=await loadModule('logic.js');assert.equal(api.choose([]).selected,null);")
            return json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'NODE','code':code}]})
        with patch.object(api,'_model_text',side_effect=model):
            result=api._host_verification_phase('node-meaning',phase,{'file_manifest':[
                {'path':'index.html'},{'path':'logic.js'}]},str(self.root))
        self.assertEqual(['author','review','author','review'],calls)
        self.assertEqual(source,(self.root/'logic.js').read_text())
        self.assertTrue(api._run_read_only_verification(result,str(self.root))[1]['passed'])

    def test_source_defect_review_requires_actual_requirement_and_source_quotations(self):
        for requirement,fragment,expected in [
                ('Echo input','result="wrong"','SOURCE_DEFECT'),
                ('Invented behavior','result="wrong"','INCONCLUSIVE'),
                ('Echo input','nonexistent source','INCONCLUSIVE')]:
            with self.subTest(requirement=requirement,fragment=fragment):
                response=json.dumps({'verdict':'SOURCE_DEFECT','reason':'Observed mismatch',
                    'requirement':requirement,'source_fragment':fragment})
                with patch.object(api,'_model_text',return_value=response):
                    review=api._review_verification_failure('review','Echo input',{},
                        {'app.js':'result="wrong"'}, {},[{'error':'assertion failed'}])
                self.assertEqual(expected,review['verdict'])

    def test_source_defect_review_repairs_quote_format_once_before_pausing(self):
        responses=[
            json.dumps({'verdict':'SOURCE_DEFECT','reason':'The current value contradicts the request.',
                'requirement':'Display the saved meal in the plan.','source_fragment':'plan.textContent = meal.name;'}),
            json.dumps({'verdict':'SOURCE_DEFECT','reason':'The current value contradicts the request.',
                'requirement':'The plan must display saved meals.','source_fragment':'plan.textContent = "";'})]
        with patch.object(api,'_model_text',side_effect=responses) as review:
            result=api._review_verification_failure('quote-repair','The plan must display saved meals.',{},
                {'app.js':'plan.textContent = "";'}, {},[{'error':'assertion failed'}])
        self.assertEqual('SOURCE_DEFECT',result['verdict'])
        self.assertEqual(2,review.call_count)

    def test_repaired_browser_check_cannot_pass_by_weakening_the_requirement(self):
        source='<textarea id="input"></textarea>'
        (self.root/'index.html').write_text(source)
        api.db.create_coding_task('weak-check','conversation','Input preserves multiple lines.',
                                 'RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':[],
               'verify':['In the browser, input preserves multiple lines.']}
        authors=[]
        def model(task_id,status,activity,role,messages,**kwargs):
            if 'You review the meaning' in messages[0]['content']:
                return json.dumps({'verdict':'CHECK_DEFECT','reason':'The check no longer proves multiple lines.',
                    'requirement':'Input preserves multiple lines.','source_fragment':''})
            authors.append(1)
            code=("await page.locator('#input').fill('A');assert.equal(await page.locator('#input').inputValue(),'A\\nB');"
                  if len(authors)==1 else "await page.locator('#input').fill('A');assert.ok((await page.locator('#input').inputValue()).length>0);")
            return json.dumps({'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':code}]})
        with patch.object(api,'_model_text',side_effect=model):
            with self.assertRaisesRegex(RuntimeError,'VERIFICATION_CONTRACT_INVALID'):
                api._host_verification_phase('weak-check',phase,{'file_manifest':[{'path':'index.html'}]},str(self.root))
        self.assertEqual(3,len(authors),'The first passing probe allows one repair; repeating the weakened check still stops')
        directory=Path(api.db.path).parent/'coding-verification'/'weak-check'
        accepted=[path for path in directory.glob('*.json')
                  if len(path.stem.split('-'))==2 and all(len(part)==16 for part in path.stem.split('-'))]
        self.assertEqual([],accepted,'A weakened passing probe must never become an accepted contract')
        self.assertEqual(source,(self.root/'index.html').read_text())

    def test_node_file_container_is_not_an_executed_unit_test(self):
        from olcr_api.node_mcp_runtime import runtime_root
        node=runtime_root()/'node/bin/node'
        target=self.root/'empty.test.js';target.write_text('// No registered tests')
        execution,result=api._run_read_only_verification({'verification_command':[str(node),'--test',target.name],
            'verification_targets':[target.name]},str(self.root))
        self.assertFalse(result['passed'])
        self.assertTrue(result['zero_tests'])

    def test_check_author_error_regenerates_checks_without_rewriting_source(self):
        from dataclasses import replace
        from olcr_api.runtime import Runtime
        sources={'index.html':'<h1>App</h1>','app.js':'const value=1;',
            'app.test.js':"const {test}=require('node:test');test('app source exists',()=>{});"}
        calls=[]
        class Model:
            def generate(self,messages,model,**kwargs):
                if 'You review the meaning' in messages[0]['content']:
                    return {'text':json.dumps({'verdict':'CHECK_VALID','reason':'Original assertion preserved.',
                        'requirement':'Source exists','source_fragment':''})}
                if 'You author an internal, read-only verification contract' in messages[0]['content']:
                    criteria=api.acceptance_criteria(api.db.coding_task('contract-repair')['plan']['phases'][-1])
                    calls.append('checks')
                    code='unknownVerificationHelper();assert.ok(true);' if calls.count('checks')==1 else "assert.ok(readFile('app.js').includes('value=1'));"
                    return {'text':json.dumps({'entrypoint':'index.html','checks':[
                        {'ids':[identifier],'kind':'NODE','code':[code]} for identifier in criteria]})}
                calls.append('implement')
                return {'text':json.dumps({'change_required':True,'source_inspected':True,'condition_evaluated':True,
                    'reason_code':'fixture','operations':[{'op':'write','path':p,'content':v} for p,v in sources.items()],
                    'verification':'host verifies'})}
        plan={**self.plan,'file_manifest':[{'path':p,'action':'create','owner_phase':'p1'} for p in sources],
              'scope':{'allowed':list(sources),'forbidden':[]},'phases':[
                  {**self.phase,'done':[p+' exists' for p in sources],'verify':[]},
                  {'id':'p2','goal':'Check the source','status':'pending','done':['Source exists'],
                   'verify':['Source exists'],'dependencies':['p1'],'execution_mode':'VERIFICATION_ONLY',
                   'host_verification':True,'host_verification_version':3,
                   'host_test_preflight':True,'risks':[]}]}
        api.db.create_coding_task('contract-repair','conversation','Create a static HTML and JavaScript app and run its tests.','RUNNING','NONE',plan,time.time())
        # Old builds could persist the same bad check twice. Those records
        # must not veto the bounded authoring/probe repair on a later resume.
        for attempt in range(2):
            report={'phase_id':'p2','attempt':attempt,'plan_revision':0,'recovery_epoch':0,
                'status':'FAIL','test_fail':['old verifier TypeError'],
                'execution_expectations':{'requires_repo_mutation':False},
                'typed_execution_summary':{'operations':[{'tool':'verification_command',
                    'status':'failed','output':{'returncode':1,'failures':[
                        {'ids':['DONE:1'],'error':'old verifier TypeError','contract_error':True}]}}]}}
            api.db.add_coding_phase_report('contract-repair','p2',attempt,report,'PASS',time.time())
        api.runtime=Runtime(replace(api.settings,allowed_roots=[str(self.root)]),api.db,api.retrieval,Model())
        with patch.object(api,'_replan_task',side_effect=AssertionError('must repair checks instead of replan')), \
             patch.object(api,'coding_debug_reference_context',return_value=''), \
             patch.object(api,'_alternate_coding_model',return_value='alternate-verifier'):
            api.run_managed_task('contract-repair',str(self.root))
        self.assertEqual(['implement','checks','checks'],calls)
        self.assertEqual('COMPLETED',api.db.coding_task('contract-repair')['status'])
        self.assertEqual(sources['app.js'],(self.root/'app.js').read_text())

    def test_real_acceptance_failure_repairs_source_then_finishes_without_replanning(self):
        from dataclasses import replace
        from olcr_api.runtime import Runtime
        sources = {
            'index.html':'<textarea id="input"></textarea><button id="select">選ぶ</button><output id="result"></output><script src="app.js"></script>',
            'app.js':"document.querySelector('#select').onclick=()=>document.querySelector('#result').textContent='wrong';",
            'math.js':'module.exports = {add:(a,b)=>a+b};',
            'math.test.js':"const test=require('node:test');const assert=require('node:assert/strict');const {add}=require('./math.js');test('addition',()=>assert.equal(add(1,2),3));"}
        fixed = "document.querySelector('#select').onclick=()=>document.querySelector('#result').textContent=document.querySelector('#input').value;"
        calls=[]
        implementation_requests=[]
        implementation_models=[]
        class Model:
            def generate(self, messages, model, **kwargs):
                request=messages[-1]['content']
                if 'You review the meaning' in messages[0]['content']:
                    calls.append('review')
                    return {'text':json.dumps({'verdict':'SOURCE_DEFECT','reason':'Click handler ignores the input.',
                        'requirement':'Click the button and observe the result in the browser.',
                        'source_fragment':sources['app.js']})}
                if 'You author an internal, read-only verification contract' in messages[0]['content']:
                    calls.append('checks')
                    criteria=api.acceptance_criteria(api.db.coding_task('acceptance')['plan']['phases'][-1])
                    browser_ids=[k for k,v in criteria.items() if any(s in v.lower() for s in ('browser','click'))]
                    node_ids=[k for k in criteria if k not in browser_ids]
                    requested=_verification_schema_ids(kwargs.get('format'))
                    if requested and not _verification_schema_allows_code(kwargs.get('format')):
                        return {'text':json.dumps({'entrypoint':'index.html','checks':[
                            {'ids':[identifier],'kind':'BROWSER',
                             'actions':[{'action':'fill','selector':'#input','value':'A'},
                                        {'action':'click','selector':'#select','value':None}],
                             'observations':[{'selector':'#result','measure':'text','operator':'eq','expected':'A'}]}
                            for identifier in requested if identifier in browser_ids]})}
                    return {'text':json.dumps({'entrypoint':'index.html','checks':[
                        {'ids':node_ids,'kind':'NODE','code':'const r=await runTests();assert.ok(r.tests>0);'},
                        {'ids':browser_ids,'kind':'BROWSER','code':"await page.locator('#input').fill('A');await page.locator('#select').click();assert.equal(await page.locator('#result').textContent(),'A');"}]})}
                if 'actual acceptance checks failed' in request:
                    calls.append('repair')
                    operations=[{'op':'patch','path':'app.js','expected_old_fragment':sources['app.js'],'replacement_fragment':fixed}]
                else:
                    calls.append('implement')
                    operations=[{'op':'write','path':p,'content':content} for p,content in sources.items()]
                implementation_requests.append(request)
                implementation_models.append(model)
                return {'text':json.dumps({'change_required':True,'source_inspected':True,'condition_evaluated':True,
                    'reason_code':'fixture','operations':operations,'verification':'host verifies'})}
        plan={**self.plan,'assumptions':['Use one approved shared API object.'],
              'file_manifest':[{'path':p,'action':'create','owner_phase':'p1'} for p in sources],
              'scope':{'allowed':list(sources),'forbidden':[]},'phases':[
                  {**self.phase,'goal':'Create the static UI and its test module','done':[p+' exists' for p in sources],
                   'verify':['Run tests','Open index.html in a browser and confirm the result.']},
                  {'id':'p2','goal':'Verify final behavior','status':'pending','done':['All tests pass'],
                   'verify':['Click the button and observe the result in the browser.'],'dependencies':['p1'],
                   'execution_mode':'VERIFICATION_ONLY','risks':[]}]}
        api.db.create_coding_task('acceptance','conversation','Create a static HTML/JavaScript app and run tests.','RUNNING','NONE',plan,time.time())
        api.runtime=Runtime(replace(api.settings,allowed_roots=[str(self.root)]),api.db,api.retrieval,Model())
        with patch.object(api,'_replan_task',side_effect=AssertionError('must execute/repair instead of replan loop')), \
             patch.object(api,'coding_debug_reference_context',return_value=''):
            api.run_managed_task('acceptance',str(self.root))
        self.assertEqual(['implement','checks','review','repair','checks','checks','checks'],calls)
        self.assertEqual([api.settings.long_implementation_model,api.settings.short_coding_model],implementation_models)
        self.assertTrue(all('Use one approved shared API object.' in request for request in implementation_requests))
        repair_request=next(request for request in implementation_requests if 'OBSERVED_ACCEPTANCE_COUNTEREXAMPLES:' in request)
        self.assertIn('"check_source":',repair_request)
        self.assertIn('"observed_page":',repair_request)
        self.assertEqual('COMPLETED',api.db.coding_task('acceptance')['status'])
        self.assertEqual(fixed,(self.root/'app.js').read_text())
        rows=api.db.coding_phase_reports('acceptance')
        self.assertTrue(any(r['structured_report'].get('observed_source_defect') for r in rows),
                        'The source contradiction is persisted even when it is repaired before final verification.')
        first_verification=next(row['structured_report'] for row in rows if row['phase_id']=='p2')
        self.assertEqual(1,len([op for op in first_verification['typed_execution_summary']['operations']
                               if op.get('tool')=='verification_command']))
        self.assertEqual('PASS',rows[-1]['structured_report']['manager_decision']['decision'])

    def test_fresh_source_defect_review_does_not_undo_a_stalled_repair_role_handoff(self):
        from dataclasses import replace
        from olcr_api.runtime import Runtime
        source="document.querySelector('#select').onclick=()=>document.querySelector('#result').textContent='wrong';"
        fixed="document.querySelector('#select').onclick=()=>document.querySelector('#result').textContent='ready';"
        sources={'index.html':'<button id="select">Select</button><output id="result"></output><script src="app.js"></script>',
                 'app.js':source}
        repair_models=[]
        class Model:
            def generate(inner,messages,model,**kwargs):
                if 'You review the meaning' in messages[0]['content']:
                    return {'text':json.dumps({'verdict':'SOURCE_DEFECT','reason':'The completed click returns the wrong result.',
                        'requirement':'Click the button and observe ready in the browser.','source_fragment':source})}
                if 'You author an internal, read-only verification contract' in messages[0]['content']:
                    criteria=json.loads(messages[-1]['content'].split('FINAL_FOCUS: ')[1].split('\n')[0])
                    return {'text':json.dumps({'entrypoint':'index.html','checks':[{'ids':[key],'kind':'BROWSER',
                        'code':"await page.locator('#select').click();assert.equal(await page.locator('#result').textContent(),'ready');"}
                        for key in criteria]})}
                if 'edits' in (kwargs.get('format') or {}).get('properties',{}):
                    repair_models.append(model)
                    return {'text':json.dumps({'reason':'Correct the actual click result','edits':[
                        {'action':'replace','path':'app.js','start_line':1,'end_line':1,'replacement':fixed}]})}
                if 'actual acceptance checks failed' in messages[-1]['content']:
                    repair_models.append(model)
                    operations=[{'op':'patch','path':'app.js','expected_old_fragment':source,
                        'replacement_fragment':source if len(repair_models)==1 else fixed}]
                else:operations=[{'op':'write','path':name,'content':content}for name,content in sources.items()]
                return {'text':json.dumps({'change_required':True,'source_inspected':True,'condition_evaluated':True,
                    'reason_code':'fixture','operations':operations,'verification':'host verifies'})}
        plan={**self.plan,'file_manifest':[{'path':name,'action':'create','owner_phase':'p1'}for name in sources],
            'scope':{'allowed':list(sources),'forbidden':[]},'phases':[
                {**self.phase,'done':[name+' exists'for name in sources],'verify':[]},
                {'id':'p2','goal':'Verify final behavior','status':'pending','done':['The button produces the required result.'],
                 'verify':['Click the button and observe ready in the browser.'],'dependencies':['p1'],
                 'execution_mode':'VERIFICATION_ONLY','risks':[]}]}
        api.db.create_coding_task('source-handoff','conversation','Create a static HTML/JavaScript app and verify the button behavior in the browser.',
                                 'RUNNING','NONE',plan,time.time())
        api.runtime=Runtime(replace(api.settings,allowed_roots=[str(self.root)]),api.db,api.retrieval,Model())
        with patch.object(api,'_replan_task',side_effect=AssertionError('Must preserve the repair handoff')), \
             patch.object(api,'coding_debug_reference_context',return_value=''):
            result=api.run_managed_task('source-handoff',str(self.root))
        self.assertEqual([api.settings.short_coding_model,api.settings.long_implementation_model],repair_models,result)
        self.assertEqual('COMPLETED',api.db.coding_task('source-handoff')['status'])
        self.assertEqual(fixed,(self.root/'app.js').read_text())

    def test_resumed_test_registration_can_earn_only_one_additional_attempt(self):
        phase={'verification_command':['node','--test']}
        recovered={'attempt':18,'recovery_epoch':1,'project_test_registration_recovered':True}
        self.assertEqual(19,api._phase_retry_limit(phase,recovered,[]))
        self.assertEqual(19,api._phase_retry_limit(phase,{**recovered,'attempt':19},[recovered]))
        self.assertLess(api._phase_retry_limit(phase,{'attempt':20,'recovery_epoch':2},[recovered]),19)

    def test_earned_progress_retry_is_dispatched_beyond_the_initial_limit(self):
        from dataclasses import replace
        from olcr_api.runtime import Runtime
        sources={'index.html':'<h1>Progress fixture</h1>', 'score.js':'module.exports={score:0};',
            'score.test.js':"const test=require('node:test');const assert=require('node:assert/strict');const {score}=require('./score.js');for(let i=1;i<=3;i++)test('condition '+i,()=>assert.ok(score>=i));"}
        repairs=[]
        model_trace=[]
        self_root=self.root
        class Model:
            def generate(self,messages,model,**kwargs):
                model_trace.append({'model':model,'system':messages[0]['content'][:100],
                    'prompt_tail':messages[-1]['content'][-500:],
                    'schema_keys':list((kwargs.get('format') or {}).get('properties') or {})})
                reviewed=_project_test_source_review(messages,'score.js')
                if reviewed:return reviewed
                if 'edits' in (kwargs.get('format') or {}).get('properties',{}):
                    current=(self_root/'score.js').read_text()
                    value=int(current.split('score:',1)[1].split('}',1)[0])
                    repairs.append(value+1)
                    updated=current.replace(f'score:{value}',f'score:{value+1}',1)
                    return {'text':json.dumps({'reason':'Advance the next failing acceptance condition','edits':[
                        {'action':'replace','path':'score.js','start_line':1,'end_line':1,'replacement':updated}]})}
                if 'You author an internal, read-only verification contract' in messages[0]['content']:
                    criteria=api.acceptance_criteria(api.db.coding_task('earned')['plan']['phases'][-1])
                    return {'text':json.dumps({'entrypoint':'index.html','checks':[{'ids':list(criteria),'kind':'NODE',
                        'code':'const r=await runTests();assert.ok(r.tests>0);'}]})}
                if 'actual acceptance checks failed' in messages[-1]['content']:
                    value=len(repairs)
                    repairs.append(value+1)
                    operations=[{'op':'patch','path':'score.js','expected_old_fragment':f'score:{value}',
                                 'replacement_fragment':f'score:{value+1}'}]
                else:
                    operations=[{'op':'write','path':p,'content':v} for p,v in sources.items()]
                return {'text':json.dumps({'change_required':True,'source_inspected':True,'condition_evaluated':True,
                    'reason_code':'fixture','operations':operations,'verification':'host verifies'})}
        plan={**self.plan,'file_manifest':[{'path':p,'action':'create','owner_phase':'p1'} for p in sources],
            'scope':{'allowed':list(sources),'forbidden':[]},'phases':[
                {**self.phase,'done':[p+' exists' for p in sources],'verify':[]},
                {'id':'p2','goal':'Run tests','status':'pending','done':['All tests pass.'],
                 'verify':[],'dependencies':['p1'],'execution_mode':'VERIFICATION_ONLY','risks':[]}]}
        api.db.create_coding_task('earned','conversation','Create a static HTML/JavaScript app and run tests.',
                                 'RUNNING','NONE',plan,time.time())
        api.runtime=Runtime(replace(api.settings,allowed_roots=[str(self.root)]),api.db,api.retrieval,Model())
        with patch.object(api,'_replan_task',side_effect=AssertionError('observed progress must continue')), \
             patch.object(api,'coding_debug_reference_context',return_value=''):
            api.run_managed_task('earned',str(self.root))
        self.assertEqual([1,2,3],repairs,repr(model_trace))
        self.assertEqual('COMPLETED',api.db.coding_task('earned')['status'])
        self.assertEqual(3,max(row['attempt'] for row in api.db.coding_phase_reports('earned') if row['phase_id']=='p1'))

    def test_failed_idempotent_repair_hands_off_at_the_next_model_invocation(self):
        from dataclasses import replace
        from olcr_api.runtime import Runtime
        sources={'index.html':'<h1>Fixture</h1>','value.js':'module.exports={value:0};',
            'value.test.js':"const test=require('node:test');const assert=require('node:assert/strict');test('value',()=>assert.equal(require('./value.js').value,1));"}
        repair_models=[]
        repair_trace=[]
        self_root=self.root
        class Model:
            def generate(self,messages,model,**kwargs):
                repair_trace.append({'model':model,'system':messages[0]['content'][:100],
                    'prompt_tail':messages[-1]['content'][-500:],
                    'schema_keys':list((kwargs.get('format') or {}).get('properties') or {}),
                    'source':(self_root/'value.js').read_text() if (self_root/'value.js').is_file() else None})
                reviewed=_project_test_source_review(messages,'value.js')
                if reviewed:return reviewed
                if 'edits' in (kwargs.get('format') or {}).get('properties',{}):
                    repair_models.append(model)
                    current=(self_root/'value.js').read_text()
                    updated=(current if len(repair_models)==1 else
                             current.replace('value:0','value:1').replace('first:0','first:1'))
                    return {'text':json.dumps({'reason':'Fix the observed assertion','edits':[
                        {'action':'replace','path':'value.js','start_line':1,'end_line':1,'replacement':updated}]})}
                if 'You author an internal, read-only verification contract' in messages[0]['content']:
                    criteria=api.acceptance_criteria(api.db.coding_task('handoff')['plan']['phases'][-1])
                    return {'text':json.dumps({'entrypoint':'index.html','checks':[{'ids':list(criteria),'kind':'NODE',
                        'code':'const r=await runTests();assert.ok(r.tests>0);'}]})}
                if 'actual acceptance checks failed' in messages[-1]['content']:
                    repair_models.append(model)
                    operations=[{'op':'patch','path':'value.js','expected_old_fragment':'value:0',
                                 'replacement_fragment':'value:0' if len(repair_models)==1 else 'value:1'}]
                else:
                    operations=[{'op':'write','path':p,'content':v} for p,v in sources.items()]
                return {'text':json.dumps({'change_required':True,'source_inspected':True,'condition_evaluated':True,
                    'reason_code':'fixture','operations':operations,'verification':'host verifies'})}
        plan={**self.plan,'file_manifest':[{'path':p,'action':'create','owner_phase':'p1'} for p in sources],
            'scope':{'allowed':list(sources),'forbidden':[]},'phases':[
                {**self.phase,'done':[p+' exists' for p in sources],
                 'verify':['The registered Node.js project test passes.'],
                 'verification_command':['node','--test','value.test.js'],
                 'verification_targets':list(sources)},
                {'id':'p2','goal':'Run tests','status':'pending','done':['All tests pass.'],
                 'verify':['The registered Node.js project test passes.'],
                 'verification_command':['node','--test','value.test.js'],
                 'verification_targets':list(sources),
                 'dependencies':['p1'],'execution_mode':'VERIFICATION_ONLY','risks':[]}]}
        api.db.create_coding_task('handoff','conversation','Fix the broken app and run tests.',
                                 'RUNNING','NONE',plan,time.time())
        api.runtime=Runtime(replace(api.settings,allowed_roots=[str(self.root)]),api.db,api.retrieval,Model())
        with patch.object(api,'_replan_task',side_effect=AssertionError('must hand off before replanning')), \
             patch.object(api,'coding_debug_reference_context',return_value=''):
            api.run_managed_task('handoff',str(self.root))
        self.assertEqual([api.settings.short_coding_model,api.settings.long_implementation_model],repair_models,repr(repair_trace))
        summaries=[{key:(row.get('structured_report') or {}).get(key) for key in
                    ('attempt','test_fail','errors','project_test_defect','project_test_diagnosis','source_repair_attempt','implementation_model','mutation_classification')}
                   for row in api.db.coding_phase_reports('handoff')]
        self.assertEqual('COMPLETED',api.db.coding_task('handoff')['status'],repr({'trace':repair_trace,'reports':summaries}))

    def test_a_past_handoff_does_not_pin_a_new_failure_to_the_writer(self):
        from dataclasses import replace
        from olcr_api.runtime import Runtime
        sources={'index.html':'<h1>Fixture</h1>','value.js':'module.exports={first:0,second:0};',
            'value.test.js':"const test=require('node:test');const assert=require('node:assert/strict');const value=require('./value.js');test('first',()=>assert.equal(value.first,1));test('second',()=>assert.equal(value.second,1));"}
        repair_models=[]
        repair_trace=[]
        self_root=self.root
        class Model:
            def generate(self,messages,model,**kwargs):
                repair_trace.append({'model':model,'system':messages[0]['content'][:100],
                    'prompt_tail':messages[-1]['content'][-500:],
                    'schema_keys':list((kwargs.get('format') or {}).get('properties') or {}),
                    'source':(self_root/'value.js').read_text() if (self_root/'value.js').is_file() else None})
                reviewed=_project_test_source_review(messages,'value.js')
                if reviewed:return reviewed
                if 'edits' in (kwargs.get('format') or {}).get('properties',{}):
                    repair_models.append(model)
                    current=(self_root/'value.js').read_text()
                    if len(repair_models)==1:
                        updated=current
                    elif len(repair_models)==2:
                        updated=current.replace('first:0','first:1')
                    else:
                        updated=current.replace('second:0','second:1')
                    return {'text':json.dumps({'reason':'Fix the observed assertion','edits':[
                        {'action':'replace','path':'value.js','start_line':1,'end_line':1,'replacement':updated}]})}
                if 'You author an internal, read-only verification contract' in messages[0]['content']:
                    criteria=api.acceptance_criteria(api.db.coding_task('handoff-progress')['plan']['phases'][-1])
                    return {'text':json.dumps({'entrypoint':'index.html','checks':[{'ids':list(criteria),'kind':'NODE',
                        'code':'const r=await runTests();assert.ok(r.tests>0);'}]})}
                if kwargs.get('format') and 'actual acceptance checks failed' in messages[-1]['content']:
                    repair_models.append(model)
                    count=len(repair_models)
                    operations=[{'op':'patch','path':'value.js','expected_old_fragment':'second:0' if count==3 else 'first:0',
                        'replacement_fragment':'second:1' if count==3 else 'first:0' if count==1 else 'first:1'}]
                else:
                    operations=[{'op':'write','path':path,'content':content} for path,content in sources.items()]
                return {'text':json.dumps({'change_required':True,'source_inspected':True,'condition_evaluated':True,
                    'reason_code':'fixture','operations':operations,'verification':'host verifies'})}
        plan={**self.plan,'file_manifest':[{'path':path,'action':'create','owner_phase':'p1'} for path in sources],
            'scope':{'allowed':list(sources),'forbidden':[]},'phases':[
                {**self.phase,'done':[path+' exists' for path in sources],'verify':[]},
                {'id':'p2','goal':'Run tests','status':'pending','done':['All tests pass.'],'verify':[],
                 'dependencies':['p1'],'execution_mode':'VERIFICATION_ONLY','risks':[]}]}
        api.db.create_coding_task('handoff-progress','conversation','Fix the broken app and run tests.',
                                 'RUNNING','NONE',plan,time.time())
        api.runtime=Runtime(replace(api.settings,allowed_roots=[str(self.root)]),api.db,api.retrieval,Model())
        with patch.object(api,'_replan_task',side_effect=AssertionError('real progress must continue')), \
             patch.object(api,'coding_debug_reference_context',return_value=''):
            api.run_managed_task('handoff-progress',str(self.root))
        self.assertEqual([api.settings.short_coding_model,api.settings.long_implementation_model,api.settings.short_coding_model],repair_models,repr(repair_trace))
        self.assertEqual('COMPLETED',api.db.coding_task('handoff-progress')['status'])

    def test_followup_is_not_runnable_until_workspace_mode_is_reserved(self):
        task = self.completed_phase_task()
        service = api.task_workspaces.service
        reserve = service.reserve
        def reserve_checked(identifier):
            saved = api.db.coding_task(identifier)
            self.assertEqual('RESUMABLE', saved['status'])
            self.assertEqual('FOLLOWUP_PREPARING', saved['activity'])
            self.assertEqual(['first', 'second'], saved['requirements']['followup_instruction_ids'])
            return reserve(identifier)
        with patch.object(service, 'reserve', side_effect=reserve_checked):
            child = api._new_coding_followup(task, 'Improve layout', 'first', instruction_ids=['first', 'second'])
        self.assertEqual('QUEUED', child['status'])
        self.assertEqual('QWEN_PLANNING', child['activity'])
        self.assertIsNotNone(child['queue_order'])

    def test_plan_approval_queues_only_current_unprotected_revision(self):
        task = self.completed_phase_task()
        api.db.update_coding_task(task['id'], status='WAITING_FOR_PLAN_APPROVAL', requirements={'planning_only': True})
        api.db.update_coding_task(task['id'], plan_revision=1)
        with self.assertRaisesRegex(RuntimeError, 'PLAN_CHANGED'):
            api.db.approve_coding_plan(task['id'], 0)
        self.assertEqual('WAITING_FOR_PLAN_APPROVAL', api.db.coding_task(task['id'])['status'])
        api.db.update_coding_task(task['id'], pending_authorization={'type': 'SCOPE'})
        with self.assertRaisesRegex(RuntimeError, 'PLAN_CHANGED'):
            api.db.approve_coding_plan(task['id'], 1)
        api.db.update_coding_task(task['id'], pending_authorization=None)
        saved = api.db.approve_coding_plan(task['id'], 1)
        self.assertEqual('QUEUED', saved['status'])
        self.assertFalse(saved['requirements']['planning_only'])
        self.assertEqual(['probe.py'], saved['approved_scopes'][-1]['requested_scope'])
        with self.assertRaisesRegex(RuntimeError, 'PLAN_CHANGED'):
            api.db.approve_coding_plan(task['id'], 1)
        self.assertEqual(1, len(api.db.coding_task(task['id'])['approved_scopes']))

    def test_workspace_preparation_does_not_hold_bookkeeping_lock_while_capturing(self):
        task = self.completed_phase_task()
        service = api.task_workspaces.service
        original_capture = service.capture
        def capture(identifier):
            def probe():
                acquired = service._lock.acquire(timeout=.5)
                if acquired: service._lock.release()
                return acquired
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertTrue(pool.submit(probe).result(1), 'capture must not monopolize shared bookkeeping')
            original_capture(identifier)
        with patch.object(service, 'capture', side_effect=capture):
            service.prepare(task['id'])

    def test_followup_retry_acknowledges_only_persisted_child_batch(self):
        task = self.completed_phase_task()
        service = api.task_workspaces.service
        service.steer(task['id'], 'Make text larger', 0, queued=True, message_id='first-followup')
        first = api._new_coding_followup(task, 'Make text larger', 'first-followup', instruction_ids=['first-followup'])
        service.steer(task['id'], 'Add keyboard support', 0, queued=True, message_id='later-followup')
        api.db.update_coding_task(task['id'], status='COMPLETED')
        api._dispatch_coding_followups(task['id'])
        statuses = {r['id']: r['status'] for r in service.steering(task['id'])}
        self.assertEqual('APPLIED', statuses['first-followup'])
        self.assertEqual('WAITING', statuses['later-followup'])
        api._dispatch_coding_followups(task['id'])
        self.assertEqual('Make text larger', api.db.coding_task(first['id'])['original_goal'])
        children = [t for t in api.db.coding_tasks('conversation') if t['id'] != task['id']]
        self.assertEqual(2, len(children))
        self.assertTrue(any(t['original_goal'] == 'Add keyboard support' for t in children))
        self.assertTrue(all(r['status'] == 'APPLIED' for r in service.steering(task['id'])))

    def test_final_report_recovery_rechecks_failed_evidence(self):
        task = self.completed_phase_task()
        api.db.update_coding_task(task['id'], recovery_action='FINAL_REPORT')
        row = api.db.coding_phase_reports(task['id'])[-1]
        report = row['structured_report']
        report['manager_decision'] = {'decision': 'RETRY'}
        api.db.update_coding_phase_report(row['id'], report, 'PASS')
        with patch.object(api, 'deterministic_final_report', side_effect=AssertionError('failed evidence must not publish')):
            api._run_managed_task(task['id'], str(self.root))
        self.assertEqual('COMPLETION_EVIDENCE', api.db.coding_task(task['id'])['recovery_reason'])

    def test_worker_start_failure_does_not_reserve_queue_slot_forever(self):
        task = self.completed_phase_task()
        api.db.enqueue_coding_task(task['id'])
        class FailedThread:
            def __init__(self, **kwargs): pass
            def start(self): raise RuntimeError('thread resource unavailable')
        api._coding_scheduler_stop.clear()
        with patch.object(api.threading, 'Thread', FailedThread):
            with self.assertRaises(RuntimeError): api._coding_scheduler_tick(2)
        self.assertNotIn(task['id'], api._scheduler_jobs)
        self.assertEqual('QUEUED', api.db.coding_task(task['id'])['status'])

    def verified_report(self):
        target = self.root / 'probe.py'
        return {'typed_execution_summary': {'operations': [{
            'tool': 'verification_command', 'status': 'success',
            'output': {'target_sha256': {'probe.py': hashlib.sha256(target.read_bytes()).hexdigest()}}
        }]}}

    def test_verification_hashes_detect_content_changes_and_latest_verification(self):
        task = self.completed_phase_task(original_goal='Fix probe and run tests')
        old = self.verified_report()
        self.assertEqual([], api._verification_content_gaps(task['id'], [old]))
        (self.root / 'probe.py').write_text('answer = 43\n')
        self.assertTrue(api._verification_content_gaps(task['id'], [old]))
        self.assertEqual([], api._verification_content_gaps(task['id'], [old, self.verified_report()]))
        (self.root / 'probe.py').unlink()
        self.assertTrue(api._verification_content_gaps(task['id'], [old]))

    def test_content_changed_during_final_format_does_not_publish_completion(self):
        task = self.completed_phase_task(original_goal='Fix probe and run tests')
        report = {**self.verified_report(), 'phase_id': 'p1'}
        def format_report(*args, **kwargs):
            (self.root / 'probe.py').write_text('answer = 0\n')
            return 'finished'
        with patch.object(api, 'deterministic_final_report', side_effect=format_report):
            api._final_report_only(task['id'], task, task['plan'], [report])
        self.assertEqual('VERIFICATION_CONTENT_CHANGED', api.db.coding_task(task['id'])['recovery_reason'])
        saved = api.db.coding_task(task['id'])
        self.assertEqual('pending', saved['plan']['phases'][0]['status'])
        api.db.enqueue_coding_task(task['id'])
        with patch.object(api, '_replan_task', return_value='verification replanned') as replan:
            self.assertEqual('verification replanned', api._run_managed_task(task['id'], str(self.root)))
        self.assertEqual(set(), replan.call_args.args[2])
        with api.db.connect() as conn:
            self.assertEqual(0, conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0])

    def test_verifier_records_hash_from_original_bytes(self):
        target = self.root / 'probe.test.js'
        target.write_text('// fixture\n')
        phase = {'verification_command': ['node', '--test', target.name], 'verification_targets': [target.name]}
        completed = subprocess.CompletedProcess(phase['verification_command'], 0, '# tests 1\n# pass 1\n', '')
        with patch.object(api, 'run_acceptance', return_value=completed):
            execution, result = api._run_read_only_verification(phase, str(self.root))
        self.assertTrue(result['passed'])
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),
                         execution.tool_executions[0]['output']['target_sha256'][target.name])

    def test_scheduler_start_during_stop_cannot_clear_stop_signal(self):
        entered = threading.Event()
        release = threading.Event()
        class FakeThread:
            def is_alive(self): return not release.is_set()
            def join(self, timeout=None):
                entered.set()
                release.wait(2)
        api._coding_scheduler_thread = FakeThread()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(api.stop_coding_scheduler, 2)
            try:
                self.assertTrue(entered.wait(2))
                api.start_coding_scheduler()
                self.assertTrue(api._coding_scheduler_stop.is_set())
                self.assertTrue(api._coding_scheduler_stopping)
            finally:
                release.set()
                future.result(3)
        self.assertIsNone(api._coding_scheduler_thread)
        self.assertFalse(api._coding_scheduler_stopping)

    def test_saved_artifact_root_cannot_replace_selected_workspace(self):
        foreign = self.root / 'foreign'; foreign.mkdir()
        plan = {**self.plan, 'implementation_plan_artifact': {'target_root': str(foreign),
                    'file_manifest': [{'path': 'probe.py', 'action': 'create'}]}}
        api.db.create_coding_task('wrong-root', 'conversation', 'Fix probe', 'RUNNING', 'NONE', plan, time.time())
        with patch.object(api.runtime, 'execute', side_effect=AssertionError('wrong workspace must not dispatch')), \
             patch.object(api.task_workspaces.service, 'capture_manifest', side_effect=AssertionError('wrong workspace must not capture')):
            api._run_managed_task('wrong-root', str(self.root))
        self.assertEqual('WORKSPACE_IDENTITY_MISMATCH', api.db.coding_task('wrong-root')['recovery_reason'])
        self.assertFalse((foreign / 'probe.py').exists())

    def test_scope_approval_acknowledges_only_its_instruction_batch(self):
        api.db.create_coding_task('steer', 'conversation', 'Fix probe', 'RUNNING', 'NONE', self.plan, time.time())
        service = api.task_workspaces.service
        service.steer('steer', 'Add keyboard support', 0, message_id='approved-instruction')
        def replan(task_id, managed, completed, reason):
            api.db.update_coding_task(task_id, status='WAITING_FOR_USER', pending_plan=self.plan,
                pending_authorization={'state': 'PENDING', 'requested_scope': ['probe.py']}, pending_user_confirmation=True)
            return 'approval required'
        with patch.object(api, '_replan_task', side_effect=replan):
            api._apply_task_steering('steer', api.db.coding_task('steer'))
        service.steer('steer', 'Also improve colors', 0, message_id='later-instruction')
        api.db.accept_pending_plan('steer')
        statuses = {row['id']: row['status'] for row in service.steering('steer')}
        self.assertEqual('APPLIED', statuses['approved-instruction'])
        self.assertEqual('QUEUED', statuses['later-instruction'])
        self.assertIsNone(api.db.coding_task('steer')['pending_plan'])

    def test_failed_instruction_can_be_retried_at_explicit_resume_boundary(self):
        api.db.create_coding_task('steer', 'conversation', 'Fix probe', 'RUNNING', 'NONE', self.plan, time.time())
        service = api.task_workspaces.service
        service.steer('steer', 'Keep keyboard support', 0)
        with patch.object(api, '_replan_task', side_effect=ValueError('provider disconnected')):
            with self.assertRaises(ValueError): api._apply_task_steering('steer', api.db.coding_task('steer'))
        self.assertEqual('NEEDS_REVIEW', service.steering('steer')[0]['status'])
        # Interrupted steering is never replayed on resume alone. The user
        # must explicitly retry the retained instruction.
        self.assertIsNone(api._apply_task_steering('steer', api.db.coding_task('steer')))
        service.update_instruction('steer', service.steering('steer')[0]['id'],
                                   'retry', 'Keep keyboard support', 'NEEDS_REVIEW')
        def replan(task_id, managed, completed, reason):
            self.assertIn('Keep keyboard support', reason)
            api.db.update_coding_task(task_id, plan_revision=1)
            return 'replanned'
        with patch.object(api, '_replan_task', side_effect=replan):
            self.assertEqual('replanned', api._apply_task_steering('steer', api.db.coding_task('steer')))
        self.assertEqual('APPLIED', service.steering('steer')[0]['status'])

    def test_scheduler_concurrent_start_creates_one_owner(self):
        ready = threading.Barrier(9)
        release = threading.Event()
        created = []
        class FakeThread:
            def __init__(self, **kwargs):
                self.alive = False
                created.append(self)
            def is_alive(self): return self.alive
            def start(self):
                time.sleep(.01)
                self.alive = True
        def start():
            ready.wait(2)
            release.wait(2)
            api.start_coding_scheduler()
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(start) for _ in range(8)]
            ready.wait(2)
            try:
                with patch.object(api.threading, 'Thread', FakeThread):
                    release.set()
                    for future in futures: future.result(2)
                self.assertEqual(1, len(created))
            finally:
                api._coding_scheduler_thread = None

    def completed_phase_task(self, identifier='finishing', original_goal='Fix probe'):
        plan = copy.deepcopy(self.plan)
        plan['phases'][0]['status'] = 'pass'
        (self.root / 'probe.py').write_text('answer = 42\n')
        api.db.create_coding_task(identifier, 'conversation', original_goal, 'RUNNING', 'NONE', plan, time.time())
        api._save_report(identifier, 'p1', 0, {
            'phase_id': 'p1', 'attempt': 0, 'plan_revision': 0, 'status': 'PASS',
            'errors': [], 'blockers': [], 'test_fail': [], 'changed_files': ['probe.py'],
            'typed_execution_summary': {'state': 'completed', 'operations': [
                {'tool': 'workspace_write', 'status': 'success', 'output': {'path': 'probe.py'}},
                {'tool': 'workspace_read', 'status': 'success', 'output': {'path': 'probe.py'}}]},
            'manager_decision': {'decision': 'PASS'},
        }, 'PASS')
        return api.db.coding_task(identifier)

    def test_completion_state_and_chat_are_atomic_and_idempotent(self):
        task = self.completed_phase_task()
        with api.db.connect() as conn:
            conn.execute("CREATE TRIGGER reject_completion BEFORE INSERT ON messages WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT,'disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            api.db.save_coding_completion(task['id'], 0, 'finished')
        saved = api.db.coding_task(task['id'])
        self.assertEqual('RUNNING', saved['status'])
        self.assertNotEqual('PASS', saved['final_report_status'])
        with api.db.connect() as conn:
            self.assertEqual(0, conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
            conn.execute('DROP TRIGGER reject_completion')
        api.db.save_coding_completion(task['id'], 0, 'finished')
        api.db.save_coding_completion(task['id'], 0, 'finished')
        self.assertEqual('COMPLETED', api.db.coding_task(task['id'])['status'])
        with api.db.connect() as conn:
            self.assertEqual(1, conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0])

    def test_pause_and_archive_during_final_format_prevent_completion(self):
        for flag in ['pause_requested', 'archived']:
            with self.subTest(flag=flag):
                task = self.completed_phase_task(flag)
                def format_report(*args, **kwargs):
                    api.db.update_coding_task(flag, **{flag: True})
                    return 'completed report'
                with patch.object(api, 'deterministic_final_report', side_effect=format_report):
                    api._complete_task(flag, task)
                saved = api.db.coding_task(flag)
                self.assertEqual('RESUMABLE', saved['status'])
                self.assertTrue(saved[flag])
                self.assertNotEqual('PASS', saved['final_report_status'])

    def test_plan_changed_during_format_never_publishes_old_report(self):
        task = self.completed_phase_task()
        def format_report(*args, **kwargs):
            api.db.update_coding_task(task['id'], plan_revision=1)
            return 'old revision report'
        with patch.object(api, 'deterministic_final_report', side_effect=format_report):
            api._complete_task(task['id'], task)
        saved = api.db.coding_task(task['id'])
        self.assertEqual(1, saved['plan_revision'])
        self.assertNotEqual('COMPLETED', saved['status'])
        self.assertNotEqual('PASS', saved['final_report_status'])

    def test_instruction_arriving_during_format_is_applied_before_completion(self):
        task = self.completed_phase_task()
        api.task_workspaces.service
        def format_report(*args, **kwargs):
            api.task_workspaces.service.steer(task['id'], 'Make result accessible', 0)
            return 'stale final report'
        def replan(task_id, managed, completed, reason):
            self.assertIn('Make result accessible', reason)
            api.db.update_coding_task(task_id, plan_revision=1, status='QUEUED')
            return 'replanning instruction'
        with patch.object(api, 'deterministic_final_report', side_effect=format_report), patch.object(api, '_replan_task', side_effect=replan):
            self.assertEqual('replanning instruction', api._complete_task(task['id'], task))
        self.assertNotEqual('COMPLETED', api.db.coding_task(task['id'])['status'])
        self.assertEqual('APPLIED', api.task_workspaces.service.steering(task['id'])[0]['status'])

    def test_completion_rechecks_current_file_and_manager_decision(self):
        for failure in ['deleted', 'retry']:
            with self.subTest(failure=failure):
                task = self.completed_phase_task(failure)
                if failure == 'deleted':
                    (self.root / 'probe.py').unlink()
                else:
                    row = api.db.coding_phase_reports(failure)[0]
                    api.db.update_coding_phase_report(row['id'], {**row['structured_report'], 'manager_decision': {'decision': 'RETRY'}}, 'PASS')
                with patch.object(api, 'deterministic_final_report', side_effect=AssertionError('must not format an incomplete task')):
                    api._complete_task(failure, task)
                self.assertEqual('COMPLETION_EVIDENCE', api.db.coding_task(failure)['recovery_reason'])

    def test_completion_uses_latest_plan_instead_of_a_stale_caller_snapshot(self):
        stale = self.completed_phase_task()
        changed = copy.deepcopy(stale['plan'])
        changed['phases'][0]['id'] = 'new-phase'
        changed['phases'][0]['status'] = 'pending'
        api.db.update_coding_task(stale['id'], plan=changed, plan_revision=1)
        with patch.object(api, 'deterministic_final_report', side_effect=AssertionError('old snapshot must not complete new phase')):
            api._complete_task(stale['id'], stale)
        self.assertEqual('COMPLETION_EVIDENCE', api.db.coding_task(stale['id'])['recovery_reason'])

    def test_empty_phase_plan_cannot_be_completed(self):
        api.db.create_coding_task('empty', 'conversation', 'Fix probe', 'RUNNING', 'NONE', {**self.plan, 'phases': []}, time.time())
        with patch.object(api, 'deterministic_final_report', side_effect=AssertionError('no completed phases')):
            api._complete_task('empty', api.db.coding_task('empty'))
        self.assertEqual('COMPLETION_EVIDENCE', api.db.coding_task('empty')['recovery_reason'])

    def test_distinct_workspace_roots_can_execute_concurrently(self):
        other = self.root / 'other-project'; other.mkdir()
        api.db.create_project('other', str(other), time.time(), 'other-project')
        api.db.create_conversation('other', time.time(), 'other-conversation', 'other-project')
        service = api.task_workspaces.service
        for identifier, conversation, project in [('one','conversation','project'), ('two','other-conversation','other-project')]:
            service.preference(project, 'direct')
            api.db.create_coding_task(identifier, conversation, 'Fix probe', 'QUEUED', 'NONE', self.plan, time.time())
            service.reserve(identifier)
        entered = threading.Barrier(3)
        release = threading.Event()
        def run(identifier):
            with service.executing(identifier):
                entered.wait(2)
                self.assertTrue(release.wait(2))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, identifier) for identifier in ['one','two']]
            try:
                entered.wait(2)
            finally:
                release.set()
            for future in futures: future.result(2)

    def test_duplicate_enqueue_preserves_fifo_and_archived_task_is_not_queued(self):
        for identifier in ['one', 'two']:
            api.db.create_coding_task(identifier, 'conversation', 'Fix probe', 'RESUMABLE', 'NONE', self.plan, time.time())
            api.db.enqueue_coding_task(identifier)
        first_order = api.db.coding_task('one')['queue_order']
        api.db.enqueue_coding_task('one')
        self.assertEqual(first_order, api.db.coding_task('one')['queue_order'])
        self.assertEqual('one', api.db.next_queued_coding_task()['id'])
        api.db.update_coding_task('two', archived=True, status='RESUMABLE', queue_order=None)
        self.assertEqual('RESUMABLE', api.db.enqueue_coding_task('two')['status'])

    def test_job_reloads_cancelled_queue_entry_before_workspace_preparation(self):
        api.db.create_coding_task('queued', 'conversation', 'Fix probe', 'QUEUED', 'QWEN_PLANNING', None, time.time())
        stale = api.db.coding_task('queued')
        api.db.update_coding_task('queued', pause_requested=True)
        with patch.object(api, '_coding_workspace_ready', side_effect=AssertionError('cancelled job must not create a workspace')):
            api._coding_job(stale)
        self.assertTrue(api.db.coding_task('queued')['pause_requested'])

    def test_workspace_boundary_serializes_same_root_and_releases_after_exception(self):
        service = api.task_workspaces.service
        service.preference('project', 'direct')
        for identifier in ['one', 'two']:
            api.db.create_coding_task(identifier, 'conversation', 'Fix probe', 'QUEUED', 'NONE', self.plan, time.time())
            service.reserve(identifier)
        first_entered, second_attempted, second_entered, release = [threading.Event() for _ in range(4)]
        def first():
            try:
                with service.executing('one'):
                    first_entered.set()
                    self.assertTrue(release.wait(2))
                    raise ValueError('simulated executor failure')
            except ValueError:
                pass
        def second():
            second_attempted.set()
            with service.executing('two'):
                second_entered.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first)
            self.assertTrue(first_entered.wait(2))
            b = pool.submit(second)
            self.assertTrue(second_attempted.wait(2))
            try:
                self.assertFalse(second_entered.wait(.1))
            finally:
                release.set()
            a.result(2); b.result(2)
        self.assertTrue(second_entered.is_set())
        self.assertEqual({}, service._running)

    def test_report_shape_repair_preserves_failure_and_never_invents_test_or_file_evidence(self):
        original = {"phase_id": "wrong", "attempt": 99, "status": "FAIL",
                    "errors": "actual parser failure", "test_executed": None,
                    "test_pass": None, "test_fail": "node exited with 1", "changed_files": None,
                    "typed_execution_summary": {"state": "failed", "operations": []},
                    "criteria_evidence": {"test passed": False}, "plan_revision": 4}
        repaired = api._repair_phase_report_shape(original, "p1", 2)
        self.assertEqual([], api.validate_phase_report(repaired, "p1", 2))
        self.assertEqual("FAIL", repaired["status"])
        self.assertEqual(["actual parser failure"], repaired["errors"])
        self.assertEqual(["node exited with 1"], repaired["test_fail"])
        self.assertEqual([], repaired["test_pass"])
        self.assertEqual([], repaired["changed_files"])
        self.assertEqual(original["typed_execution_summary"], repaired["typed_execution_summary"])
        self.assertEqual(original["criteria_evidence"], repaired["criteria_evidence"])
        self.assertEqual(4, repaired["plan_revision"])

    def test_verification_only_missing_executor_repairs_plan_without_calling_file_implementer(self):
        phase = {"id": "p1", "goal": "Verify the application", "status": "pending",
                 "execution_mode": "VERIFICATION_ONLY", "done": ["verification complete"],
                 "verify": ["tests pass"], "dependencies": [], "risks": []}
        plan = {**self.plan, "phases": [phase]}
        api.db.create_coding_task("verify-only", "conversation", "Verify existing app", "RUNNING", "NONE", plan, time.time())
        with patch.object(api.runtime, "execute", side_effect=AssertionError("verification must not write")), \
             patch.object(api, "_replan_task", return_value="repairing verifier") as repair:
            self.assertEqual("repairing verifier", api._run_managed_task("verify-only", str(self.root)))
        self.assertIn("no executable verification contract", repair.call_args.args[3])
        self.assertIn("verification_targets", repair.call_args.args[4]["specific_fix"])

    def test_prior_permission_failure_does_not_block_current_successful_repair(self):
        phase = {"id": "P1", "goal": "create probe.py", "execution_mode": "IMPLEMENTATION",
                 "done": ["probe.py exists"], "verify": ["read-back check"], "dependencies": [], "risks": []}
        target = self.root / "probe.py"
        target.write_text("answer = 42\n")
        prior = {"phase_id": "P1", "plan_revision": 0, "status": "BLOCKED", "errors": ["permission denied"],
                 "typed_execution_summary": {"state": "denied", "operations": []}}
        current = {"phase_id": "P1", "plan_revision": 0, "status": "PASS", "changed_files": ["probe.py"],
                   "errors": [], "blockers": [], "test_fail": [], "typed_execution_summary": {
                       "state": "completed", "operations": [
                           {"tool": "workspace_write", "status": "success", "output": {"path": "probe.py"}},
                           {"tool": "workspace_read", "status": "success", "output": {"path": "probe.py"}}]}}
        result = evaluate_phase(phase, current, [prior], str(self.root))
        self.assertTrue(result["phase_complete"])
        current["status"] = "BLOCKED"
        current["errors"] = ["permission denied"]
        current["typed_execution_summary"]["state"] = "denied"
        self.assertFalse(evaluate_phase(phase, current, [prior], str(self.root))["phase_complete"])

    def test_path_evidence_does_not_conflate_directories_or_ambiguous_basenames(self):
        from olcr_api.coding_tasks import _criterion_satisfied
        report = {"status": "PASS"}
        cases = [
            ("frontend/index.js exists", {"backend/index.js": True}, False),
            ("index.js exists", {"backend/index.js": True, "frontend/index.js": True}, False),
            ("frontend/index.js exists", {"frontend/index.js": True, "backend/index.js": False}, True),
            ("index.js exists", {"/tmp/project/frontend/index.js": True}, True),
            ("frontend/index.js and frontend/style.css exist", {"frontend/index.js": True}, False),
        ]
        for criterion, paths, expected in cases:
            with self.subTest(criterion=criterion, paths=paths):
                satisfied, _ = _criterion_satisfied(criterion, report, {}, [], paths)
                self.assertEqual(expected, satisfied)

    def test_workspace_inventory_only_does_not_prove_ui_behavior(self):
        from olcr_api.coding_tasks import _criterion_satisfied
        satisfied, _ = _criterion_satisfied("The UI displays a clear error message", {"status": "PASS"}, {},
            [{"tool": "workspace_list", "status": "success", "output": {"files": ["index.html"]}}], {})
        self.assertFalse(satisfied)

    def test_empty_success_shaped_report_does_not_prove_ui_or_browser_behavior(self):
        phase = {"id": "P1", "execution_mode": "IMPLEMENTATION", "goal": "create UI",
                 "done": ["The UI displays a clear Japanese message when input is empty."],
                 "verify": ["Click the button and confirm history shows 5 previous results."],
                 "dependencies": [], "risks": []}
        report = {"status": "PASS", "implemented": [], "changed_files": [], "errors": [],
                  "test_fail": [], "test_pass": [], "test_executed": [], "build_pass": "NOT_RUN",
                  "build_executed": "NOT_RUN", "typed_execution_summary": {"operations": []}}
        result = evaluate_phase(phase, report, workspace_root=str(self.root))
        self.assertFalse(result["phase_complete"])
        self.assertEqual(0, result["done_satisfied"])
        self.assertEqual(0, result["verify_satisfied"])

    def setUp(self):
        self.scheduler_running = bool(api._coding_scheduler_thread and api._coding_scheduler_thread.is_alive())
        api.stop_coding_scheduler()
        self.directory = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.root = Path(self.directory.name)
        self.old_db, self.old_runtime = api.db, api.runtime
        api.db = Database(str(self.root / 'audit.sqlite'))
        api.db.initialize()
        api.db.create_project('audit', str(self.root), time.time(), 'project')
        api.db.create_conversation('audit', time.time(), 'conversation', 'project')
        self.phase = {'id': 'p1', 'goal': 'Fix probe.py', 'status': 'pending',
                      'done': ['probe.py exists'], 'verify': ['manual check'],
                      'dependencies': [], 'risks': [], 'execution_mode': 'IMPLEMENTATION'}
        self.plan = {'schema_version': 1, 'original_goal': 'Fix probe',
                     'scope': {'allowed': ['probe.py'], 'forbidden': []}, 'assumptions': [],
                     'phases': [self.phase], 'max_retries_per_phase': 2, 'requires_user_approval': True}

    def test_approved_empty_greenfield_manifest_is_repaired_before_phase_dispatch(self):
        class ImplementerReached(Exception):
            pass

        workspace = self.root / "approved-site"
        workspace.mkdir()
        goal = "Build an OLCR marketing website with the selected frontend stack"
        plan = {
            "schema_version": 1,
            "original_goal": goal,
            "scope": {"allowed": [], "forbidden": []},
            "assumptions": [],
            "file_manifest": [],
            "blueprint": {
                "source_phases": [{"id": "p1", "goal": "Build the approved public product website",
                                   "done": ["The homepage contains seven responsive sections"]}],
                "preflight": {
                    "workspace_state": {"root": str(workspace), "greenfield": True,
                                         "project_state": "EMPTY_GREENFIELD", "file_count": 0,
                                         "new_project_intent": True},
                    "mutation_scope_contract": {"write_allowed": [], "read_only": []},
                },
            },
            "phases": [
                {"id": "major-blueprint", "kind": "ORCHESTRATOR_BLUEPRINT",
                 "goal": "Repository context and implementation blueprint", "status": "pending",
                 "done": ["The implementation blueprint is persisted"], "verify": [],
                 "dependencies": [], "risks": [], "execution_mode": "IMPLEMENTATION"},
                {"id": "major-1", "goal": "Implement approved deliverables", "status": "pending",
                 "done": ["The approved public product website is implemented"], "verify": [],
                 "dependencies": ["major-blueprint"], "risks": [],
                 "execution_mode": "IMPLEMENTATION", "requires_dependency_installation": True},
            ],
            "max_retries_per_phase": 2,
            "requires_user_approval": True,
        }
        requirements = {
            "task_profile": "FRONTEND_ONLY_MARKETING_SITE",
            "new_project_intent": True,
            "required_stack": ["Vite", "React", "TypeScript", "Tailwind", "shadcn", "Anime.js v4"],
            "execution_policy": {"continuation_policy": "CONTINUOUS"},
            "verification_requested": False,
        }
        api.db.create_coding_task("empty-approved-manifest", "conversation", goal,
                                  "QUEUED", "NONE", plan, time.time())
        api.db.update_coding_task("empty-approved-manifest", requirements=requirements,
            approved_scopes=[{"source": "explicit_plan_approval", "requested_scope": []}])

        class Runtime:
            def execute(self, request, **kwargs):
                raise ImplementerReached

        old_runtime = api.runtime
        api.runtime = Runtime()
        try:
            with patch.object(api.task_workspaces.service, "executing", return_value=nullcontext(str(workspace))):
                with self.assertRaises(ImplementerReached):
                    api._run_managed_task("empty-approved-manifest", str(workspace))
        finally:
            api.runtime = old_runtime

        saved = api.db.coding_task("empty-approved-manifest")["plan"]
        artifact = saved["implementation_plan_artifact"]
        self.assertEqual(
            ["index.html", "package.json", "src/App.tsx", "src/index.css", "src/main.tsx", "vite.config.ts"],
            [entry["path"] for entry in artifact["file_manifest"]],
        )
        self.assertTrue((workspace / "src" / "App.tsx").is_file())

    def tearDown(self):
        api.stop_coding_scheduler()
        api.db, api.runtime = self.old_db, self.old_runtime
        self.directory.cleanup()
        if self.scheduler_running:
            api.start_coding_scheduler()

    def waiting_task(self):
        api.db.create_coding_task('task', 'conversation', 'Fix probe', 'WAITING_FOR_USER', 'NONE', self.plan, time.time())
        api.db.update_coding_task('task', current_phase_id='p1', pending_user_confirmation=1,
            pending_authorization={'type': 'REPLAN_INEFFECTIVE'}, recovery_reason='REPLAN_INEFFECTIVE')
        api.db.add_coding_phase_report('task', 'p1', 2, {
            'phase_id': 'p1', 'attempt': 2, 'plan_revision': 0, 'recovery_epoch': 0,
            'execution_attempt_started': True, 'execution_attempt_completed': True,
            'status': 'FAIL', 'errors': ['probe is missing'], 'changed_files': [],
            'typed_execution_summary': {'operations': [], 'state': 'failed'},
            'manager_decision': {'decision': 'REPLAN_REQUIRED', 'reason': 'phase retry budget exhausted'},
        }, 'PASS', time.time())
        return api.db.coding_task('task')

    def retry_runtime(self, fail=False):
        calls = []
        root = self.root
        class Runtime:
            def execute(self, request, **kwargs):
                calls.append(request)
                execution = Task(request)
                execution.transition(TaskState.ROUTING)
                execution.transition(TaskState.EXECUTING)
                if fail:
                    execution.error = 'the repair failed'
                    execution.transition(TaskState.FAILED)
                else:
                    target = root / 'probe.py'
                    target.write_text('answer = 42\n')
                    execution.tool_executions = [
                        {'tool': 'workspace_write', 'status': 'success', 'output': {'path': str(target), 'net_changed': True}},
                        {'tool': 'workspace_read', 'status': 'success', 'output': {'path': str(target)}},
                    ]
                    execution.transition(TaskState.COMPLETED)
                return execution, 'repair result'
        api.runtime = Runtime()
        return calls

    def test_retry_dispatches_implementation_despite_saved_replan_decision(self):
        task = self.waiting_task()
        calls = self.retry_runtime()
        with patch.object(api._coding_scheduler_wake, 'set'), patch.object(api, '_complete_task', return_value='done'), \
             patch.object(api, '_replan_task', side_effect=AssertionError('must not replan')):
            api._resume_coding_task_control(task, source='AUDIT')
            self.assertEqual('done', api._run_managed_task('task', str(self.root)))
        saved = api.db.coding_task('task')
        self.assertEqual(1, len(calls))
        self.assertIn('probe is missing', calls[0])
        self.assertIn('"original_request": "Fix probe"', calls[0])
        self.assertIn('Attempt: 3', calls[0])
        self.assertEqual('pass', saved['plan']['phases'][0]['status'])
        self.assertTrue(saved['resume_state']['ineffective_replan_retry_requested']['consumed_at'])
        self.assertEqual(0, saved['replan_count'])
        self.assertEqual(1, saved['task_total_recovery_model_calls'])

    def test_failed_direct_retry_does_not_call_the_same_planner_again(self):
        task = self.waiting_task()
        calls = self.retry_runtime(fail=True)
        with patch.object(api._coding_scheduler_wake, 'set'), \
             patch.object(api, '_replan_task', side_effect=AssertionError('must not replan')):
            api._resume_coding_task_control(task, source='AUDIT')
            api._run_managed_task('task', str(self.root))
        self.assertEqual(1, len(calls))
        saved = api.db.coding_task('task')
        self.assertEqual('WAITING_FOR_USER', saved['status'])
        self.assertIn('the repair failed', saved['resume_state']['stop_progress']['text'])

    def test_stale_retry_grant_cannot_bypass_a_new_plan_revision(self):
        task = self.waiting_task()
        with patch.object(api._coding_scheduler_wake, 'set'):
            api._resume_coding_task_control(task, source='AUDIT')
        api.db.update_coding_task('task', plan_revision=1)
        # The report belongs to revision 1; the explicit retry grant belongs to 0.
        report = api.db.coding_phase_reports('task')[0]['structured_report']
        api.db.add_coding_phase_report('task', 'p1', 2, {**report, 'plan_revision': 1}, 'PASS', time.time())
        calls = self.retry_runtime()
        with patch.object(api, '_replan_task', return_value='replan'):
            self.assertEqual('replan', api._run_managed_task('task', str(self.root)))
        self.assertEqual([], calls)

    def test_interrupted_direct_retry_keeps_the_phase_continuation(self):
        task = self.waiting_task()
        with patch.object(api._coding_scheduler_wake, 'set'):
            api._resume_coding_task_control(task, source='AUDIT')
        self.retry_runtime()
        with patch.object(api.runtime, 'execute', side_effect=ModelFailure('unavailable', 'connection reset')), \
             patch.object(api, '_replan_task', side_effect=AssertionError('must not replan')):
            with self.assertRaises(ModelFailure):
                api._run_managed_task('task', str(self.root))
        saved = api.db.coding_task('task')
        self.assertFalse(saved['resume_state']['ineffective_replan_retry_requested'].get('consumed_at'))
        api._transition_resumable('task', 'MODEL_CALL', 'RECOVERABLE_INTERNAL')
        calls = self.retry_runtime()
        with patch.object(api._coding_scheduler_wake, 'set'), patch.object(api, '_complete_task', return_value='done'), \
             patch.object(api, '_replan_task', side_effect=AssertionError('must not replan')):
            api._resume_coding_task_control(api.db.coding_task('task'), source='AUDIT')
            self.assertEqual('done', api._run_managed_task('task', str(self.root)))
        self.assertEqual(1, len(calls))

    def test_simultaneous_retry_controls_reserve_only_one_attempt(self):
        stale = self.waiting_task()
        with patch.object(api._coding_scheduler_wake, 'set'), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: api._resume_coding_task_control(stale, source='AUDIT'), range(2)))
        saved = api.db.coding_task('task')
        self.assertEqual('QUEUED', saved['status'])
        self.assertEqual(1, saved['task_total_recovery_model_calls'])
        self.assertEqual(['task', 'task'], [result['coding_task_id'] for result in results])

    def test_replan_uses_persisted_counters_not_runner_snapshot(self):
        stale = self.waiting_task()
        api.db.update_coding_task('task', replan_count=2, replan_count_in_epoch=0)
        with patch.object(api, '_generate_plan', return_value=(None, None)):
            api._replan_task('task', stale, set(), 'audit')
        self.assertEqual(3, api.db.coding_task('task')['replan_count'])

    def test_replanning_preserves_the_task_planning_depth_and_module_contract(self):
        api.db.create_coding_task('lean-recovery','conversation','Fix probe','QUEUED','NONE',self.plan,time.time())
        requirements={'planning_policy':{'mode':'LEAN'},'mutation_mode':'FIX'}
        api.db.update_coding_task('lean-recovery',requirements=requirements)
        with patch.object(api,'_generate_plan',return_value=(None,None)) as generate:
            api._replan_task('lean-recovery',api.db.coding_task('lean-recovery'),set(),'An assertion failed')
        self.assertEqual({**requirements,'_replan_phase_limit':1,'_preserve_completed_phase_ids':[]},
                         generate.call_args.kwargs['requirements'])
        self.assertTrue(generate.call_args.kwargs['recovery'])
        self.assertIn('Preserve the existing module loading strategy',generate.call_args.args[2])

    def test_node_test_with_zero_tests_is_not_success(self):
        target = self.root / 'probe.test.js'
        target.write_text('// no registered tests\n')
        phase = {'verification_command': ['node', '--test', target.name], 'verification_targets': [target.name]}
        completed = subprocess.CompletedProcess(phase['verification_command'], 0, '# tests 0\n# pass 0\n', '')
        with patch.object(api, 'run_acceptance', return_value=completed):
            execution, result = api._run_read_only_verification(phase, str(self.root))
        self.assertFalse(result['passed'])
        self.assertEqual(TaskState.FAILED, execution.state)

    def test_real_node_test_success_and_failure_are_recorded(self):
        from olcr_api.node_mcp_runtime import runtime_root
        packaged=runtime_root()
        if not packaged:
            self.skipTest('bundled Node runtime is unavailable')
        node=packaged/'node/bin/node'
        target = self.root / 'probe.test.js'
        phase = {'verification_command': [str(node), '--test', target.name], 'verification_targets': [target.name]}
        for passes in (True, False):
            with self.subTest(passes=passes):
                body = '' if passes else 'throw new Error("expected failure");'
                source = 'const test = require("node:test"); test("registered case", () => {' + body + '});\n'
                target.write_text(source)
                execution, result = api._run_read_only_verification(phase, str(self.root))
                self.assertEqual(passes, result['passed'])
                self.assertIn('registered case', result['output'])
                self.assertEqual(source, target.read_text())
                self.assertEqual(TaskState.COMPLETED if passes else TaskState.FAILED, execution.state)

    def test_verifier_restores_deleted_or_modified_targets_after_timeout(self):
        target = self.root / 'probe.py'
        phase = {'verification_command': ['python3', target.name], 'verification_targets': [target.name]}
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                target.write_bytes(b'original\n')
                def timeout(*args, **kwargs):
                    target.unlink() if deleted else target.write_bytes(b'changed\n')
                    raise subprocess.TimeoutExpired(args[0], 20)
                with patch.object(api, 'run_acceptance', side_effect=timeout):
                    _, result = api._run_read_only_verification(phase, str(self.root))
                self.assertFalse(result['passed'])
                self.assertFalse(result['executor_error'],'A running test that hangs is not a missing executable')
                self.assertEqual(['probe.py'], result['changed_targets'])
                self.assertEqual(b'original\n', target.read_bytes())

    def test_verifier_proof_is_current_and_bound_to_the_planned_command(self):
        phase = {**self.phase, 'verify': ['tests pass'], 'verification_command': ['node', '--test', 'probe.py'], 'verification_targets': ['probe.py']}
        op = {'tool': 'verification_command', 'status': 'success',
              'input': {'command': phase['verification_command'], 'write_scope': 'NONE'},
              'output': {'returncode': 0, 'read_only_targets': ['probe.py'], 'changed_targets': [], 'zero_tests': False}}
        report = {'phase_id': 'p1', 'status': 'PASS', 'plan_revision': 0, 'criteria_evidence': {'probe.py exists': True, 'tests pass': True},
                  'changed_files': ['probe.py'], 'accepted_mutations': 1, 'typed_execution_summary': {'operations': [op]}}
        report['typed_execution_summary']['operations'].append({'tool': 'workspace_write', 'status': 'success', 'output': {'path': 'probe.py'}})
        (self.root / 'probe.py').write_text('original')
        self.assertTrue(evaluate_phase(phase, report, workspace_root=str(self.root))['phase_complete'])
        for variation in ('wrong-command', 'zero-tests', 'previous-attempt', 'wrong-targets'):
            invalid = copy.deepcopy(report)
            if variation == 'wrong-command':
                invalid['typed_execution_summary']['operations'][0]['input']['command'] = ['node', '-v']
            elif variation == 'zero-tests':
                invalid['typed_execution_summary']['operations'][0]['output']['zero_tests'] = True
            elif variation == 'wrong-targets':
                invalid['typed_execution_summary']['operations'][0]['output']['read_only_targets'] = ['other.py']
            else:
                invalid['typed_execution_summary']['operations'] = []
            self.assertFalse(evaluate_phase(phase, invalid, [report], str(self.root))['phase_complete'], variation)

    def test_verifier_restoration_does_not_follow_a_replacement_symlink(self):
        target = self.root / 'probe.py'
        target.write_bytes(b'original\n')
        with tempfile.TemporaryDirectory(dir='/private/tmp') as outside:
            external = Path(outside) / 'outside.py'
            external.write_bytes(b'outside\n')
            phase = {'verification_command': ['python3', target.name], 'verification_targets': [target.name]}
            def replace(*args, **kwargs):
                target.unlink()
                target.symlink_to(external)
                return subprocess.CompletedProcess(args[0], 0, '', '')
            with patch.object(api, 'run_acceptance', side_effect=replace):
                _, result = api._run_read_only_verification(phase, str(self.root))
            self.assertFalse(result['passed'])
            self.assertFalse(target.is_symlink())
            self.assertEqual(b'original\n', target.read_bytes())
            self.assertEqual(b'outside\n', external.read_bytes())

    def test_typecheck_execution_without_pass_is_not_final_acceptance(self):
        managed = {'original_goal':'Build the app and run typecheck',
                   'requirements': {'required_verification': ['typecheck']}}
        self.assertIn('required typecheck verification', api._final_acceptance_gaps(managed, [{'test_executed': ['npm run typecheck']}]))
        self.assertEqual([], api._final_acceptance_gaps(managed, [{'test_pass': ['npm run typecheck (exit_code=0)']}]))

    def test_verifier_restoration_rejects_a_replaced_parent(self):
        parent = self.root / 'nested'
        parent.mkdir()
        (parent / 'probe.py').write_bytes(b'original\n')
        with tempfile.TemporaryDirectory(dir='/private/tmp') as outside:
            external = Path(outside) / 'probe.py'
            external.write_bytes(b'outside\n')
            phase = {'verification_command': ['python3', 'nested/probe.py'], 'verification_targets': ['nested/probe.py']}
            def replace(*args, **kwargs):
                parent.rename(self.root / 'saved')
                parent.symlink_to(outside, target_is_directory=True)
                return subprocess.CompletedProcess(args[0], 0, '', '')
            with patch.object(api, 'run_acceptance', side_effect=replace):
                execution, result = api._run_read_only_verification(phase, str(self.root))
            self.assertFalse(result['passed'])
            self.assertIn('restoration failed', execution.error)
            self.assertEqual(b'outside\n', external.read_bytes())

    def test_blocked_runner_persists_progress_and_reason(self):
        self.waiting_task()
        api.db.update_coding_task('task', status='QUEUED', pending_authorization=None)
        def block(*args):
            api.db.update_coding_task('task', status='BLOCKED', recovery_reason='GLOBAL_RECOVERY_LIMIT')
            return 'blocked'
        service = api.task_workspaces.service
        with patch.object(service, 'prepare'), patch.object(service, 'root_for', return_value=str(self.root)), \
             patch.object(service, 'executing', return_value=nullcontext()), patch.object(api, '_run_managed_task', side_effect=block):
            api.run_managed_task('task', str(self.root))
        text = api.db.coding_task('task')['resume_state']['stop_progress']['text']
        self.assertIn('自動復旧の回数上限', text)
        self.assertIn('0 / 1 完了', text)
        self.assertNotIn('再開後は', text)

    def test_scheduler_recovers_from_transient_database_failure(self):
        calls = []
        def tick(concurrency):
            calls.append(concurrency)
            if len(calls) == 1:
                raise sqlite3.OperationalError('database is locked')
            api._coding_scheduler_stop.set()
        api._coding_scheduler_stop.clear()
        with patch.object(api, '_coding_scheduler_tick', side_effect=tick), patch.object(api._coding_scheduler_wake, 'wait'):
            api._coding_scheduler()
        self.assertEqual(2, len(calls))

    def test_successful_unit_tests_do_not_require_an_unrequested_build(self):
        phase = {**self.phase, 'verify': ['unit tests pass']}
        report = {'phase_id': 'p1', 'status': 'PASS', 'accepted_mutations': 1,
                  'criteria_evidence': {'probe.py exists': True},
                  'test_executed': ['node --test'], 'test_pass': ['node --test'],
                  'build_executed': 'NOT_RUN', 'build_pass': 'NOT_RUN',
                  'typed_execution_summary': {'operations': [
                      {'tool': 'workspace_write', 'status': 'success', 'output': {'path': 'probe.py'}},
                      {'tool': 'verification_command', 'status': 'success', 'output': {'returncode': 0}}]}}
        (self.root / 'probe.py').write_text('original')
        self.assertTrue(evaluate_phase(phase, report, workspace_root=str(self.root))['phase_complete'])
