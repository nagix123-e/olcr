"""Current-source ownership across verifier, repair and resume boundaries."""
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import olcr_api.app as api
from olcr_api.db import Database


class VerificationRepairLifecycleTests(unittest.TestCase):
    def test_verification_source_context_does_not_silently_lose_large_file_tail(self):
        source='<!--'+('padded source '*1500)+'-->\n<h1>ready connected TAIL_SOURCE_SENTINEL</h1>'
        (self.root/'index.html').write_text(source)
        def author(*args,**kwargs):
            self.assertIn('TAIL_SOURCE_SENTINEL',args[4][-1]['content'])
            return json.dumps(self.contract())
        with patch.object(api,'_model_text',side_effect=author):
            phase=api._host_verification_phase('task',self.phase,self.plan,str(self.root))
        self.assertTrue(api._run_read_only_verification(phase,str(self.root))[1]['passed'])
        self.assertEqual(source,(self.root/'index.html').read_text())

    def test_actual_browser_startup_error_hands_off_source_before_layout_authoring_and_recovers(self):
        (self.root/'index.html').write_text('<link rel="stylesheet" href="styles.css"><main><button id="start">Start</button></main><script src="app.js"></script>')
        (self.root/'styles.css').write_text('body{margin:0}main{max-width:100%}')
        source=self.root/'app.js';source.write_text('const good=1;\nconst broken=</div>;')
        phase={**self.phase,'done':['スマートフォンとデスクトップの両方で画面が使いやすい。'],
            'verify':['styles.css exists and provides a responsive layout.']}
        plan={'file_manifest':[{'path':path} for path in ['index.html','styles.css','app.js']]}
        with patch.object(api,'_model_text',side_effect=AssertionError('Layout/startup is a measured host check')) as author:
            first=api._host_verification_phase('task',phase,plan,str(self.root))
            self.assertTrue(first['host_source_defect'])
            self.assertTrue(api._verification_repair_authorized(first,{}))
            review=first['host_source_defect_evidence']['review']
            self.assertEqual(('app.js',2), (review['source_path'],review['line']))
            self.assertIn('</div>',review['source_fragment'])
            source.write_text('const good=1;')
            final=api._host_verification_phase('task',first,plan,str(self.root))
            result=api._run_read_only_verification(final,str(self.root))[1]
        self.assertFalse(final.get('host_source_defect'))
        self.assertTrue(result['passed'],result)
        self.assertEqual(0,author.call_count)

    def test_scenario_compiler_discovery_exception_cannot_grant_source_writes(self):
        (self.root/'index.html').write_text('<div id="items">空データ</div>')
        phase={**self.phase,'done':['In the browser, handle empty data and no matching items under budget conditions.']}
        candidate={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER','scenario':'empty_data',
            'actions':[],'observations':[{'selector':'#items','measure':'text','operator':'includes','expected':'空データ'}]}]}
        with patch.object(api,'_model_text',return_value=json.dumps(candidate)):
            with self.assertRaisesRegex(RuntimeError,'VERIFICATION_CONTRACT_INVALID'):
                api._host_verification_phase('task',phase,self.plan,str(self.root))
        self.assertEqual('<div id="items">空データ</div>',(self.root/'index.html').read_text())

    def test_startup_probe_preserves_the_cached_selected_html_entrypoint(self):
        (self.root/'selected.html').write_text('<h1>Ready</h1>')
        (self.root/'unrelated.html').write_text('<script>const broken=</div>;</script>')
        phase={**self.phase,'done':['The app loads in a browser without console errors.'],'verify':[]}
        with patch.object(api,'_model_text',side_effect=AssertionError('Owned startup needs no model')):
            first=api._host_verification_phase('task',phase,{'file_manifest':[{'path':'selected.html'},{'path':'unrelated.html'}]},str(self.root))
            contract_path=Path(first['verification_command'][2])
            contract_path.with_name(contract_path.stem+'-runtime-diagnostic.json').write_text(json.dumps({
                'entrypoint':'unrelated.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER',
                    'code':'assert.equal(await page.locator("html").count(),1);'}]}))
            second=api._host_verification_phase('task',first,{'file_manifest':[{'path':'unrelated.html'},{'path':'selected.html'}]},str(self.root))
            result=api._run_read_only_verification(second,str(self.root))[1]
        self.assertFalse(second.get('host_source_defect'))
        self.assertTrue(result['passed'],result)
        self.assertEqual('selected.html',json.loads(Path(second['verification_command'][2]).read_text())['entrypoint'])

    def test_scenario_compiler_fills_omissions_and_repairs_storage_action_order(self):
        from olcr_api.coding_verification import compile_browser_observations
        (self.root/'index.html').write_text('''<button id="btn-add-dish">料理を追加</button>
            <button id="btn-plan">献立を作成</button><input id="budget-limit">
            <div id="dish-list"></div><div id="meal-list"></div><div id="toast"></div>
            <form id="modal-form"><input id="name"><input id="cost" type="number">
              <button type="submit">保存</button></form>''')
        (self.root/'app.js').write_text("function save(){try{localStorage.setItem('x','1')}catch(e){showToast('保存に失敗しました')}} function noMatch(){showToast('条件に合う料理がありません')}")
        criteria={'DONE:3':'In the browser, handle empty data, no matching dishes under budget constraints, and storage errors.'}
        candidate={'entrypoint':'index.html','checks':[
            {'ids':['DONE:3'],'kind':'BROWSER','scenario':'empty_data','actions':[],
             'observations':[{'selector':'#dish-list .empty','measure':'text','operator':'includes','expected':'料理がありません'}]},
            {'ids':['DONE:3'],'kind':'BROWSER','scenario':'empty_data','actions':[],
             'observations':[{'selector':'#dish-list .empty','measure':'text','operator':'includes','expected':'料理がありません'}]},
            {'ids':['DONE:3'],'kind':'BROWSER','scenario':'storage_error','actions':[
                {'action':'storage_failure','selector':'localStorage','value':True},
                {'action':'click','selector':'#btn-plan','value':None}],
             'observations':[{'selector':'#toast','measure':'text','operator':'includes','expected':'保存に失敗しました'}]},
        ]}

        compiled=compile_browser_observations(candidate,criteria,{
            'index.html':(self.root/'index.html').read_text(),'app.js':(self.root/'app.js').read_text()})

        checks=[check for check in compiled['checks'] if check['ids']==['DONE:3']]
        self.assertEqual(3,len(checks))
        codes=[check['code'] for check in checks]
        storage=next(code for code in codes if 'setStorageFailure(true)' in code)
        self.assertLess(storage.index('setStorageFailure(true)'),storage.index("#btn-add-dish"))
        self.assertIn('#modal-form button[type=',storage)
        self.assertIn('submit',storage)
        no_match=next(code for code in codes if "#budget-limit" in code)
        self.assertIn("#budget-limit",no_match)
        self.assertIn("#btn-plan",no_match)
        self.assertTrue(any("#dish-list .empty" in code for code in codes))

    def test_storage_error_scenario_uses_the_recipe_form_not_an_unrelated_settings_form(self):
        from olcr_api.coding_verification import compile_browser_observations
        html='''<section>
            <form id="plan-settings">
              <label>アレルゲン<input id="plan-allergies" type="text"></label>
              <label>1品の予算<input id="max-cost" type="number"></label>
              <label>週予算<input id="weekly-budget" type="number"></label>
            </form>
            <button id="generate-plan" type="button">献立を自動作成</button>
            <button id="new-recipe" type="button">料理を追加</button>
            <div id="recipes-empty">料理がありません</div>
            <dialog id="recipe-dialog"><form id="recipe-form">
              <input id="recipe-id" type="hidden">
              <input id="recipe-name" required>
              <input id="recipe-cost" type="number">
              <button type="submit">保存する</button>
            </form></dialog>
            <span id="save-status" role="status">保存済み</span>
            <p id="notice" role="status"></p>
          </section>'''
        script="function saveState(){try{localStorage.setItem('x','1')}catch(e){showNotice('データを保存できませんでした')}} function generate(){if(!result.success){showNotice(`${result.reason}。料理や条件を見直してください。`)}}"
        criteria={'DONE:1':'In the browser, handle empty data, no matching dishes under budget, and a save error.'}
        candidate={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER',
            'scenario':'empty_data','actions':[],
            'observations':[{'selector':'#recipes-empty','measure':'text','operator':'includes',
                             'expected':'料理がありません'}]},
            {'ids':['DONE:1'],'kind':'BROWSER','scenario':'storage_error','actions':[],
             'observations':[{'selector':'#notice','measure':'text','operator':'includes',
                              'expected':'データを保存できませんでした'}]}]}

        compiled=compile_browser_observations(candidate,criteria,{'index.html':html,'app.js':script})

        self.assertEqual(3,len(compiled['checks']),repr(compiled))
        code=next(check['code'] for check in compiled['checks'] if 'setStorageFailure(true)' in check['code'])
        self.assertIn('#new-recipe',code)
        self.assertIn('#recipe-name',code)
        self.assertNotIn('#recipe-id',code)
        self.assertIn('#recipe-form button[type=',code)
        self.assertIn('submit',code)
        self.assertNotIn('#plan-settings button[type=submit]',code)
        self.assertNotIn('#plan-allergies',code)
        self.assertIn('#notice',code)
        self.assertNotIn('#save-status',code)
        no_match=next(check['code'] for check in compiled['checks'] if '#weekly-budget' in check['code'])
        self.assertIn('#generate-plan',no_match)
        self.assertIn('料理や条件を見直してください。',no_match)
        self.assertIn('#notice',no_match)
        self.assertNotIn('#save-status',no_match)

    def test_scenario_compiler_reads_form_fields_from_javascript_inner_html(self):
        from olcr_api.coding_verification import compile_browser_observations
        html='''<html lang="ja"><button data-tab="meals">料理管理</button>
            <button data-tab="plan">献立</button><input id="budget-limit">
            <div id="dish-list"></div><div id="toast"></div></html>'''
        script='''const form = document.createElement('form');
          form.innerHTML = '<input name="name" required>' +
            '<input name="cost" type="number">' +
            '<button type="submit">保存</button>';
          const btn = document.createElement('button');
          btn.textContent = '献立を作成'; el.appendChild(btn);
          function save(){try{localStorage.setItem('x','1')}catch(e){showToast('保存に失敗しました')}}
          function noMatch(){showToast('条件に合う料理がありません')}'''
        criteria={'DONE:3':'In the browser, handle empty data, no matching dishes under budget constraints, and storage errors.'}
        candidate={'entrypoint':'index.html','checks':[
            {'ids':['DONE:3'],'kind':'BROWSER','scenario':'empty_data','actions':[],
             'observations':[{'selector':'#dish-list .empty','measure':'text','operator':'includes','expected':'料理がありません'}]},
            {'ids':['DONE:3'],'kind':'BROWSER','scenario':'storage_error','actions':[],
             'observations':[{'selector':'#toast','measure':'text','operator':'includes','expected':'保存に失敗しました'}]},
        ]}

        compiled=compile_browser_observations(candidate,criteria,{'index.html':html,'app.js':script})

        checks=[check for check in compiled['checks'] if check['ids']==['DONE:3']]
        self.assertEqual(3,len(checks))
        no_match=next(check['code'] for check in checks if '#budget-limit' in check['code'])
        self.assertIn(r'input[name=\"name\"]',no_match)
        self.assertIn(r'input[name=\"cost\"]',no_match)
        self.assertIn('text=献立を作成',no_match)
        plan_tab='button[data-tab=\\"plan\\"]'
        self.assertIn(plan_tab,no_match)
        self.assertLess(no_match.index(plan_tab),no_match.index('text=献立を作成'))

    def test_discovery_failure_reaches_check_author_without_granting_source_repair(self):
        (self.root/'index.html').write_text('<div id="app"></div>')
        phase={**self.phase,'done':['In the browser, handle empty data and no matching dishes under budget conditions.']}
        with patch.object(api,'_model_text',side_effect=RuntimeError('author_called')) as author:
            with self.assertRaisesRegex(RuntimeError,'author_called'):
                api._host_verification_phase('task',phase,self.plan,str(self.root))
        self.assertEqual(1,author.call_count)
        self.assertIn('ADVISORY_UI_DISCOVERY',author.call_args.args[4][1]['content'])

    def test_output_limit_retries_with_smaller_check_batch_and_completes_contract(self):
        markers=['alpha','bravo','charlie','delta']
        (self.root/'index.html').write_text(' '.join(markers))
        phase={**self.phase,'done':[f'The page contains {marker}.' for marker in markers],'verify':[]}
        requested_batches=[]

        def author(_task_id,_status,_activity,_model,messages,structured_schema=None):
            focus=json.loads(messages[-1]['content'].split('FINAL_FOCUS: ',1)[1].split('\n',1)[0])
            requested_batches.append(list(focus))
            if len(focus)>1 and requested_batches[-1]==['DONE:1','DONE:2']:
                raise api.ModelFailure('output_limit','Model exhausted its output budget before completing structured JSON')
            return json.dumps({'entrypoint':'index.html','checks':[
                {'ids':[identifier],'kind':'NODE',
                 'code':f"assert.ok(readFile('index.html').includes({json.dumps(markers[int(identifier.split(':')[1])-1])}));"}
                for identifier in focus]},ensure_ascii=False)

        with patch.object(api,'_model_text',side_effect=author) as model:
            final=api._host_verification_phase('task',phase,self.plan,str(self.root))

        self.assertEqual(['DONE:1','DONE:2'],requested_batches[0])
        self.assertEqual(['DONE:1'],requested_batches[1])
        self.assertEqual([['DONE:1'],['DONE:2'],['DONE:3','DONE:4']],requested_batches[1:])
        self.assertEqual(4,model.call_count)
        result=api._run_read_only_verification(final,str(self.root))[1]
        self.assertTrue(result['passed'],result)

    def test_single_browser_output_limit_retries_as_literal_observations(self):
        (self.root/'index.html').write_text('<h1>ready</h1>')
        phase={**self.phase,'done':[],
            'verify':['In the browser, verify the original requirement: Show the ready heading.']}
        response={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','actions':[],
            'observations':[{'selector':'h1','measure':'text','operator':'eq','expected':'ready'}]}]}

        with patch.object(api,'_model_text',side_effect=[
                api.ModelFailure('output_limit','structured output was truncated'),
                json.dumps(response)],) as author, \
             patch.object(api,'_review_verification_failure',return_value={'verdict':'CHECK_VALID','reason':'The heading is observed'}) as review:
            final=api._host_verification_phase('task',phase,self.plan,str(self.root))

        self.assertEqual(2,author.call_count)
        self.assertNotIn('code',author.call_args.kwargs['structured_schema']['properties']['checks']['items']['properties'])
        self.assertEqual(1,review.call_count)
        result=api._run_read_only_verification(final,str(self.root))[1]
        self.assertTrue(result['passed'],result)

    def test_batching_upgrade_reuses_previous_contract_without_losing_accepted_checks(self):
        import hashlib
        (self.root/'index.html').write_text('<h1>ready connected</h1>')
        criteria=api.acceptance_criteria(self.phase)
        old_identity=hashlib.sha256(json.dumps({'criteria':criteria,
            'request':'Build a working local application','executor_version':31},sort_keys=True).encode()).hexdigest()[:16]
        directory=self.root/'coding-verification/task';directory.mkdir(parents=True)
        name=hashlib.sha256(b'final').hexdigest()[:16]+'-'+old_identity+'.json'
        (directory/name).write_text(json.dumps(self.contract()))

        with patch.object(api,'_model_text',side_effect=AssertionError('A valid previous contract must be reused')):
            final=api._host_verification_phase('task',self.phase,self.plan,str(self.root))

        result=api._run_read_only_verification(final,str(self.root))[1]
        self.assertTrue(result['passed'],result)

    def test_failing_project_test_setup_is_repaired_before_browser_check_authoring(self):
        (self.root/'index.html').write_text('<h1>ready connected</h1>')
        tests=self.root/'tests';tests.mkdir()
        test=tests/'app.test.js';test.write_text('const module.exports = {};')
        plan={'file_manifest':[{'path':'index.html'},{'path':'tests/app.test.js'}]}
        with patch.object(api,'_model_text',side_effect=AssertionError('No browser authoring before tests run')):
            result=api._host_verification_phase('task',self.phase,plan,str(self.root))
        self.assertTrue(result['host_project_test_defect'])
        self.assertEqual(['tests/app.test.js'],result['host_project_test_repair_paths'])
        self.assertTrue(api._verification_repair_authorized(result,{}))
        self.assertNotIn('host_source_defect',result)
        observed=api._run_read_only_verification(result,str(self.root))[1]
        self.assertFalse(observed['passed'])
        self.assertEqual('CHECK_DEFECT',observed['test_diagnosis']['verdict'])
        test.write_text("const {test}=require('node:test');const assert=require('node:assert/strict');test('actual behavior',()=>assert.equal(1+1,2));")
        with patch.object(api,'_model_text',return_value=json.dumps(self.contract())):
            final=api._host_verification_phase('task',result,plan,str(self.root))
        self.assertFalse(final.get('host_project_test_defect'))
        observed=api._run_read_only_verification(final,str(self.root))[1]
        self.assertTrue(observed['passed'],observed)
        self.assertGreater(observed['test_runs'][0]['registered_tests'],0)

    def test_temporal_dead_zone_test_loader_failure_routes_to_test_only_repair(self):
        (self.root/'index.html').write_text('<h1>ready connected</h1>')
        tests=self.root/'tests';tests.mkdir()
        test=tests/'meal-planner.test.js'
        test.write_text("function loadApp() { const module = App; return module; }\nconst App = loadApp();\n")
        plan={'file_manifest':[{'path':'index.html'},{'path':'tests/meal-planner.test.js'}]}
        with patch.object(api,'_model_text',side_effect=AssertionError('Known test-only TDZ needs no model review')) as review:
            result=api._host_verification_phase('task',self.phase,plan,str(self.root))
        self.assertEqual(0,review.call_count)
        self.assertTrue(result['host_project_test_defect'])
        self.assertEqual(['tests/meal-planner.test.js'],result['host_project_test_repair_paths'])
        self.assertEqual('CHECK_DEFECT',result['host_current_command_evidence']['test_diagnosis']['verdict'])

    def test_project_assertion_failure_requires_review_and_cannot_change_the_application_by_default(self):
        (self.root/'index.html').write_text('<h1>ready</h1>')
        tests=self.root/'tests';tests.mkdir()
        (tests/'app.test.js').write_text("const {test}=require('node:test');const assert=require('node:assert/strict');test('assertion',()=>assert.equal(1,2));")
        phase={**self.phase,'verification_command':['node','--test','tests/app.test.js'],
            'verification_targets':['index.html','tests/app.test.js']}
        plan={'file_manifest':[{'path':'index.html'},{'path':'tests/app.test.js'}]}
        with patch.object(api,'_review_project_test_failures',return_value={'verdict':'INCONCLUSIVE','reason':'Cannot attribute assertion'}) as review:
            with self.assertRaisesRegex(RuntimeError,'current evidence does not establish whether the test or application'):
                api._host_verification_phase('task',phase,plan,str(self.root))
        self.assertEqual(1,review.call_count)
        self.assertTrue(review.call_args.args[4]['test_runs'])
        self.assertEqual('<h1>ready</h1>',(self.root/'index.html').read_text())

    def test_inconclusive_project_assertion_does_not_authorize_source_retry_during_implementation(self):
        (self.root/'index.html').write_text('<h1>ready</h1>')
        tests=self.root/'tests';tests.mkdir()
        (tests/'app.test.js').write_text("const {test}=require('node:test');const assert=require('node:assert/strict');test('assertion',()=>assert.equal(1,2));")
        phase={**self.phase,'execution_mode':'IMPLEMENTATION_AND_VERIFICATION',
            'verification_command':['node','--test','tests/app.test.js'],
            'verification_targets':['index.html','tests/app.test.js']}
        plan={'file_manifest':[{'path':'index.html'},{'path':'tests/app.test.js'}]}
        with patch.object(api,'_review_project_test_failures',return_value={'verdict':'INCONCLUSIVE',
                'reason':'Cannot attribute assertion'}) as review:
            with self.assertRaisesRegex(RuntimeError,'current evidence does not establish whether the test or application'):
                api._host_verification_phase('task',phase,plan,str(self.root))
        self.assertEqual(1,review.call_count)
        self.assertTrue(review.call_args.args[4]['test_runs'])
        self.assertEqual('<h1>ready</h1>',(self.root/'index.html').read_text())

    def test_explicit_node_only_executor_keeps_selection_and_reviews_harness_ownership(self):
        (self.root/'index.html').write_text('<h1>ready</h1>')
        (self.root/'selected.test.js').write_text("const {test,expect}=require('node:test');test('selected',()=>expect(1).toBe(1));")
        (self.root/'other.test.js').write_text("throw Error('UNRELATED_TEST_EXECUTED');")
        command=['node','--test','selected.test.js']
        phase={**self.phase,'done':['All tests pass.'],'verification_command':command,
               'verification_targets':['selected.test.js','index.html']}
        plan={'file_manifest':[{'path':p} for p in ['index.html','selected.test.js','other.test.js']]}
        with patch.object(api,'_model_text',side_effect=AssertionError('Known harness error needs no model')):
            result=api._host_verification_phase('task',phase,plan,str(self.root))
        self.assertEqual(command,result['verification_command'])
        self.assertEqual(phase['verification_targets'],result['verification_targets'])
        self.assertEqual(['selected.test.js'],result['host_project_test_repair_paths'])
        self.assertNotIn('UNRELATED_TEST_EXECUTED',result['host_current_command_evidence']['output'])

    def test_direct_test_commands_need_an_executed_passing_test(self):
        (self.root/'index.html').write_text('<h1>ready</h1>')
        test=self.root/'selected.test.js'
        cases=[("test.skip('skipped',()=>{});",False),
               ("test.todo('todo');",False),
               ("test.skip('skipped',()=>{});test('executed',()=>{});",True)]
        for body,expected in cases:
            with self.subTest(body=body):
                test.write_text("const {test}=require('node:test');"+body)
                phase={**self.phase,'verification_command':['node','--test',test.name],
                    'verification_targets':[test.name,'index.html']}
                result=api._run_read_only_verification(phase,str(self.root))[1]
                self.assertEqual(expected,result['passed'],result)
        python_test=self.root/'test_skipped.py'
        python_test.write_text("import unittest\nclass Skipped(unittest.TestCase):\n @unittest.skip('pending')\n def test_pending(self): pass\n")
        phase={**self.phase,'verification_command':[sys.executable,'-m','unittest','test_skipped'],
            'verification_targets':[python_test.name,'index.html']}
        self.assertFalse(api._run_read_only_verification(phase,str(self.root))[1]['passed'])

    def test_producer_and_final_runner_recognize_tests_directory_files_consistently(self):
        (self.root/'index.html').write_text('<h1>ready connected</h1>')
        (self.root/'tests').mkdir()
        (self.root/'tests/test.js').write_text("require('node:test')('registered',()=>require('node:assert/strict').equal(2,2));")
        plan={'file_manifest':[{'path':'index.html'},{'path':'tests/test.js'}]}
        with patch.object(api,'_model_text',return_value=json.dumps(self.contract())):
            phase=api._host_verification_phase('task',self.phase,plan,str(self.root))
        observed=api._run_read_only_verification(phase,str(self.root))[1]
        self.assertTrue(observed['passed'],observed)
        self.assertEqual(1,observed['test_runs'][0]['passed'])

    def test_actual_tap_registration_location_does_not_reclassify_application_syntax_as_test_setup(self):
        (self.root/'app.js').write_text('const broken;')
        (self.root/'app.test.js').write_text("require('node:test')('loads app',()=>require('./app.js'));")
        phase={'verification_command':['node','--test','app.test.js'],'verification_targets':['app.js','app.test.js']}
        observed=api._run_read_only_verification(phase,str(self.root))[1]
        self.assertFalse(observed['passed'])
        self.assertEqual('INCONCLUSIVE',observed['test_diagnosis']['verdict'])

    def test_storage_failure_fault_uses_real_save_ui_and_restores_writes(self):
        from olcr_api.coding_verification import compile_browser_observations
        (self.root/'index.html').write_text('<button id="save">Save</button><output id="status"></output><script src="app.js"></script>')
        (self.root/'app.js').write_text("document.querySelector('#save').onclick=()=>{try{localStorage.setItem('saved','yes');document.querySelector('#status').textContent='saved'}catch(e){document.querySelector('#status').textContent='save failed'}}")
        candidate=compile_browser_observations({'entrypoint':'index.html','checks':[{
            'ids':['DONE:1'],'kind':'BROWSER','actions':[
                {'action':'storage_failure','selector':'localStorage','value':True},
                {'action':'click','selector':'#save','value':None}],
            'observations':[{'selector':'#status','measure':'text','operator':'eq','expected':'save failed'}]}]},
            {'DONE:1':'Saving reports a storage failure'})
        candidate['checks'][0]['code']+="\nawait setStorageFailure(false); await page.locator('#save').click(); await expect(page.locator('#status')).toHaveText('saved');"
        contract=self.root/'fault-check.json'
        contract.write_text(json.dumps(candidate))
        phase={**self.phase,'verification_command':api.acceptance_command(contract,self.root),'verification_targets':['index.html','app.js'],'host_criteria':{'DONE:1':'Saving reports a storage failure'}}
        result=api._run_read_only_verification(phase,str(self.root))
        self.assertTrue(result[1]['passed'],result[1])

    def test_repair_evidence_matches_focused_criterion_when_checks_are_reversed(self):
        checks=[{'ids':['VERIFY:2'],'code':'check second input'}, {'ids':['VERIFY:1'],'code':'check first input'}]
        self.assertEqual([checks[1]],api._focused_verification_checks(checks,{'VERIFY:1':'first requirement'}))

    def test_bounded_repair_keeps_required_budget_input_beyond_first_fields(self):
        finding={'review':{'verdict':'CHECK_DEFECT','reason':'Fill budget before generation','source_fragment':'budget.value'},
                 'failures':[{'ids':['DONE:1'],'observed_page':{'fields':[{'id':str(i),'value':'x'}for i in range(6)]+[{'id':'budget','value':''}]}}]}
        result=api._bounded_verification_repair_finding(json.dumps(finding))
        self.assertIn('budget',result)
        self.assertIn('source_fragment',json.loads(result)['review'])
        self.assertIn('budget',[f['id']for f in json.loads(result)['failures'][0]['observed_page']['fields']])

    def test_selector_repair_receives_controls_beyond_first_two_and_locator_error(self):
        buttons=[{'text':name,'tag':'BUTTON','id':str(i)} for i,name in enumerate(['前へ','次へ','買い物リスト','料理リスト（0）'])]
        context=api._verification_failure_context([{'ids':['VERIFY:5'],'error':"Timeout\ngetByRole('tab', {name:'料理リスト'})",'observed_page':{'buttons':buttons,'fields':[{'id':str(i),'value':'x'} for i in range(6)]}}])
        observed=context[0]['observed_page']
        self.assertEqual(buttons,observed['buttons'])
        self.assertEqual(6,len(observed['fields']))
        self.assertIn("getByRole('tab'",context[0]['error'])

    def test_reviewed_progress_survives_a_different_check_failing_in_a_partial_batch(self):
        (self.root/'index.html').write_text('<p id="one">one</p><p id="two">two</p><p id="three">three</p>')
        phase={**self.phase,'done':[],'verify':[
            f'In the browser, verify the original requirement: Display {name}.'for name in ['one','two','three']]}
        def check(number,name):
            return {'ids':[f'VERIFY:{number}'],'kind':'BROWSER',
                'code':f'assert.equal(await page.locator("#{name}").textContent(),"{name}");'}
        first,second,third=[check(n,name)for n,name in enumerate(['one','two','three'],1)]
        with patch.object(api,'_model_text',return_value=json.dumps({'entrypoint':'index.html','checks':[first]})), \
             patch.object(api,'_review_verification_failure',return_value={'verdict':'CHECK_VALID'}), \
             patch.object(api,'_pause_at_checkpoint',return_value=True):
            api._host_verification_phase('task',phase,self.plan,str(self.root))
        bad={**second,'code':'assert.equal(false,true);'}
        batches=[[bad,third],[second]]
        with patch.object(api,'_model_text',side_effect=[json.dumps({'entrypoint':'index.html','checks':items})for items in batches])as author, \
             patch.object(api,'_review_verification_failure',side_effect=[
                 {'verdict':'CHECK_DEFECT','source_fragment':'budget.value'},{'verdict':'CHECK_VALID'},{'verdict':'CHECK_VALID'}])as review:
            final=api._host_verification_phase('task',phase,self.plan,str(self.root))
        self.assertEqual(2,author.call_count)
        self.assertNotIn(bad['code'],author.call_args_list[1].args[4][1]['content'])
        self.assertIn('IDs and format only; rejected executable omitted',author.call_args_list[1].args[4][1]['content'])
        self.assertIn('budget.value',author.call_args_list[1].args[4][1]['content'])
        self.assertEqual([['VERIFY:2'],['VERIFY:2'],['VERIFY:3']],[[identifier
            for item in call.args[4]['checks']for identifier in item['ids']]for call in review.call_args_list])
        value=json.loads(Path(final['verification_command'][2]).read_text())
        self.assertEqual(['VERIFY:1','VERIFY:2','VERIFY:3'],value['_olcr_semantically_reviewed_ids'])
        self.assertTrue(api._run_read_only_verification(final,str(self.root))[1]['passed'])

    def test_recovery_can_capture_intermediate_outputs_for_a_stateful_workflow(self):
        (self.root/'index.html').write_text('<input id="amount"><button id="add">Add</button><output id="total">0</output><script src="app.js"></script>')
        (self.root/'app.js').write_text("let total=0;document.querySelector('#add').onclick=()=>{total+=Number(document.querySelector('#amount').value);document.querySelector('#total').textContent=total;};")
        phase={**self.phase,'done':['In the browser, adding amounts preserves the intermediate running balances.'],'verify':[]}
        plan={'file_manifest':[{'path':'index.html'},{'path':'app.js'}]}
        bad={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER','code':['const = ;']}]}
        weak={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER','actions':[],
            'observations':[{'selector':'#total','measure':'text','operator':'eq','expected':'0'}]}]}
        good={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER','code':[
            'const seen=[];',
            'for(const amount of [2,3,-1]){',
            'await page.locator("#amount").fill(String(amount));',
            'await page.locator("#add").click();',
            'seen.push(Number(await page.locator("#total").textContent()));',
            '}',
            'assert.deepEqual(seen,[2,5,4]);']} ]}
        with patch.object(api,'_model_text',side_effect=[json.dumps(bad),json.dumps(weak),json.dumps(good)])as author, \
             patch.object(api,'_review_verification_failure',side_effect=[
                 {'verdict':'CHECK_DEFECT','reason':'Initial zero does not prove intermediate totals','verification_form':'STATEFUL_CODE'},
                 {'verdict':'CHECK_VALID','reason':'Each actual intermediate output is compared','verification_form':'STATEFUL_CODE'}])as review:
            final=api._host_verification_phase('task',phase,plan,str(self.root))
        self.assertEqual(3,author.call_count)
        self.assertEqual(2,review.call_count)
        item=author.call_args.kwargs['structured_schema']['properties']['checks']['items']['properties']
        self.assertIn('code',item)
        self.assertNotIn('observations',item,'A stateful repair cannot downgrade back to final-state literals')
        self.assertTrue(api._run_read_only_verification(final,str(self.root))[1]['passed'])
        (self.root/'app.js').write_text("document.querySelector('#add').onclick=()=>document.querySelector('#total').textContent=document.querySelector('#amount').value;")
        self.assertFalse(api._run_read_only_verification(final,str(self.root))[1]['passed'])

    def test_passing_checks_preserved_from_a_failed_batch_still_need_semantic_review(self):
        (self.root/'index.html').write_text('<textarea id="items"></textarea><p id="hint">Enter items</p>')
        phase={**self.phase,'done':[],'verify':[
            'In the browser, verify the original requirement: Each line is one item.',
            'In the browser, verify the original requirement: Show input guidance.']}
        first={'ids':['VERIFY:1'],'kind':'BROWSER','code':"await page.locator('#items').fill('one\\ntwo');assert.equal(await page.locator('#items').inputValue(),'one\\ntwo');"}
        second={'ids':['VERIFY:2'],'kind':'BROWSER','code':"assert.equal(await page.locator('#hint').textContent(),'Enter items');"}
        batches=[[{**first,'code':'assert.equal(false,true);'},{**second,'code':'assert.ok(true);'}],[first],[second]]
        with patch.object(api,'_model_text',side_effect=[json.dumps({'entrypoint':'index.html','checks':checks})for checks in batches])as author, \
             patch.object(api,'_review_verification_failure',side_effect=[
                 {'verdict':'CHECK_DEFECT','reason':'The first check proves nothing'},
                 {'verdict':'CHECK_VALID','reason':'The first requirement now has evidence'},
                 {'verdict':'CHECK_DEFECT','reason':'The preserved passing guidance check is a tautology'},
                 {'verdict':'CHECK_VALID','reason':'The guidance text is observed'}])as review:
            final=api._host_verification_phase('task',phase,self.plan,str(self.root))
        self.assertEqual(3,author.call_count)
        self.assertEqual(4,review.call_count)
        value=json.loads(Path(final['verification_command'][2]).read_text())
        self.assertEqual(['VERIFY:1','VERIFY:2'],value['_olcr_semantically_reviewed_ids'])
        self.assertTrue(api._run_read_only_verification(final,str(self.root))[1]['passed'])

    def test_stalled_semantic_authoring_gets_one_alternate_writer_with_independent_review(self):
        (self.root/'index.html').write_text('<h1>ready</h1>')
        phase={**self.phase,'done':['The heading is ready.'],'verify':[]}
        bad={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'NODE','code':'assert.equal(false,true);'}]}
        good={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'NODE',
            'code':"assert.ok(readFile('index.html').includes('ready'));"}]}
        with patch.object(api,'_model_text',side_effect=[json.dumps(value)for value in [bad,bad,good]])as author, \
             patch.object(api,'_review_verification_failure',side_effect=[
                 {'verdict':'CHECK_DEFECT','reason':'Observe the heading rather than a constant'},
                 {'verdict':'CHECK_DEFECT','reason':'The same constant still proves nothing'},
                 {'verdict':'CHECK_VALID','reason':'Actually observes the heading'}])as review:
            final=api._host_verification_phase('task',phase,self.plan,str(self.root))
        alternate=api._alternate_coding_model(api.settings.planner_model)
        self.assertIsNotNone(alternate)
        self.assertEqual([api.settings.planner_model,api.settings.planner_model,alternate],
            [call.args[3]for call in author.call_args_list])
        self.assertEqual(3,review.call_count)
        self.assertTrue(api._run_read_only_verification(final,str(self.root))[1]['passed'])
        self.assertEqual('<h1>ready</h1>',(self.root/'index.html').read_text())

    def test_old_malformed_html_is_a_source_defect_not_a_reason_to_weaken_the_selector(self):
        source='<textarea id="items"/><button id="apply">Apply</button><output id="result"></output>'
        (self.root/'index.html').write_text(source)
        phase={**self.phase,'done':['Clicking Apply updates the output in the browser.'],'verify':[]}
        check={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER',
            'code':"await page.locator('#apply').click();assert.equal(await page.locator('#result').textContent(),'ready');"}]}
        with patch.object(api,'_model_text',return_value=json.dumps(check))as author:
            final=api._host_verification_phase('task',phase,self.plan,str(self.root))
        self.assertEqual(1,author.call_count,'The current HTML parser supplies evidence without a speculative model review')
        self.assertTrue(final['host_source_defect'])
        self.assertEqual('CURRENT_HTML_BOUNDARY_PARSE',final['host_source_defect_evidence']['review']['evidence_origin'])
        self.assertEqual(source,(self.root/'index.html').read_text())

    def test_rejected_batch_is_reauthored_together_then_requests_only_missing_ids(self):
        source=self.root/'index.html';source.write_text('<h1>ready connected</h1>')
        invalid=self.contract()
        for check in invalid['checks']:check['code']='assert.equal(false,true);'
        responses=[invalid,*({'entrypoint':'index.html','checks':[check]} for check in self.contract()['checks'])]
        with patch.object(api,'_model_text',side_effect=[json.dumps(value) for value in responses])as author, \
             patch.object(api,'_review_verification_failure',side_effect=[
                 {'verdict':'CHECK_DEFECT','reason':'The assertions do not observe the requested behavior'},
                 {'verdict':'CHECK_VALID','reason':'The real heading is observed'}]):
            final=api._host_verification_phase('task',self.phase,self.plan,str(self.root))
        focuses=[json.loads(call.args[4][-1]['content'].split('FINAL_FOCUS: ')[1].split('\n')[0])
            for call in author.call_args_list]
        self.assertEqual([['DONE:1','DONE:2'],['DONE:1','DONE:2'],['DONE:2']],[list(focus)for focus in focuses])
        self.assertTrue(api._run_read_only_verification(final,str(self.root))[1]['passed'])
        self.assertEqual('<h1>ready connected</h1>',source.read_text())

    def test_passing_original_requirement_check_is_reviewed_and_repaired_if_it_proves_too_little(self):
        (self.root/'index.html').write_text('<textarea id="items" placeholder="One item per line"></textarea>')
        phase={**self.phase,'done':[],'verify':['In the browser, verify the original requirement: Each line is one item.']}
        weak={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER',
            'code':"await page.locator('#items').fill('one');assert.equal(await page.locator('#items').inputValue(),'one');"}]}
        strong={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER',
            'code':"await page.locator('#items').fill('one\\ntwo');assert.deepEqual((await page.locator('#items').inputValue()).split('\\n'),['one','two']);"}]}
        with patch.object(api,'_model_text',side_effect=[json.dumps(weak),json.dumps(strong)])as author, \
             patch.object(api,'_review_verification_failure',side_effect=[
                 {'verdict':'CHECK_DEFECT','reason':'One value and a placeholder do not prove multiple input lines'},
                 {'verdict':'CHECK_VALID','reason':'Actually enters and observes two independent lines'}])as review:
            final=api._host_verification_phase('task',phase,self.plan,str(self.root))
        self.assertEqual(2,author.call_count)
        self.assertEqual(2,review.call_count)
        value=json.loads(Path(final['verification_command'][2]).read_text())
        self.assertIn('VERIFY:1',value['_olcr_semantically_reviewed_ids'])
        with patch.object(api,'_model_text',side_effect=AssertionError('Do not regenerate reviewed checks')):
            resumed=api._host_verification_phase('task',final,self.plan,str(self.root))
        self.assertTrue(api._run_read_only_verification(resumed,str(self.root))[1]['passed'])

    def test_old_passing_user_checks_without_semantic_evidence_are_not_reused_as_coverage(self):
        import hashlib
        (self.root/'index.html').write_text('<textarea id="items"></textarea>')
        phase={**self.phase,'done':[],'verify':['In the browser, verify the original requirement: Each line is one item.']}
        criteria=api.acceptance_criteria(phase)
        identity=hashlib.sha256(json.dumps({'criteria':criteria,'request':'Build a working local application',
            'executor_version':17},sort_keys=True).encode()).hexdigest()[:16]
        directory=self.root/'coding-verification/task';directory.mkdir(parents=True)
        (directory/(hashlib.sha256(b'final').hexdigest()[:16]+'-'+identity+'.json')).write_text(json.dumps(
            {'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','code':"await page.locator('#items').fill('one');assert.equal(await page.locator('#items').inputValue(),'one');"}]}))
        check={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER',
            'code':"await page.locator('#items').fill('one\\ntwo');assert.equal(await page.locator('#items').inputValue(),'one\\ntwo');"}]}
        with patch.object(api,'_model_text',return_value=json.dumps(check))as author, \
             patch.object(api,'_review_verification_failure',side_effect=[{'verdict':'CHECK_DEFECT','reason':'Tautology does not prove multiple lines'},{'verdict':'CHECK_VALID','reason':'Two lines observed'}])as review:
            final=api._host_verification_phase('task',phase,self.plan,str(self.root))
        self.assertEqual(1,author.call_count)
        self.assertEqual(2,review.call_count)
        self.assertTrue(api._run_read_only_verification(final,str(self.root))[1]['passed'])

    def test_recovery_context_preserves_source_transport_and_actual_preimage_fields(self):
        report={'typed_execution_summary':{'failure_class':'PREIMAGE_MISMATCH',
            'operations':[{'tool':'source_repair_transport','status':'selected'}],
            'preimage_diagnostics':[{'path':'logic.py','patch_old_match_count':0,
                'fragment_source_relation':'UNANCHORED_MODEL_FRAGMENT','preimage_failure_reason':'STALE_OLD_TEXT'}]}}
        context=api._bounded_review_context(report)
        self.assertTrue(context['source_bound_repair'])
        self.assertEqual('logic.py',context['preimage_diagnostics'][0]['path'])
        self.assertEqual('0',context['preimage_diagnostics'][0]['patch_old_match_count'])
        self.assertEqual('UNANCHORED_MODEL_FRAGMENT',context['preimage_diagnostics'][0]['fragment_source_relation'])

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(dir='/private/tmp')
        self.root=Path(self.directory.name)
        self.old_db=api.db
        api.db=Database(str(self.root/'audit.sqlite'))
        api.db.initialize()
        api.db.create_project('audit',str(self.root),time.time(),'project')
        api.db.create_conversation('audit',time.time(),'conversation','project')
        api.db.create_coding_task('task','conversation','Build a working local application',
                                  'RUNNING','NONE',None,time.time())
        self.phase={'id':'final','host_verification':True,'execution_mode':'VERIFICATION_ONLY',
                    'verification_repair_allowed':True,'done':['Heading ready','Action connected'],'verify':[]}
        self.plan={'file_manifest':[{'path':'index.html'}]}
        self.instruction_patch=patch.object(api.task_workspaces.service,'instruction_context',return_value='')
        self.instruction_patch.start()

    def tearDown(self):
        self.instruction_patch.stop()
        api.db=self.old_db
        self.directory.cleanup()

    def contract(self, first='ready', second='connected'):
        return {'entrypoint':'index.html','checks':[
            {'ids':['DONE:1'],'kind':'NODE','code':f'assert.ok(readFile("index.html").includes({json.dumps(first)}));'},
            {'ids':['DONE:2'],'kind':'NODE','code':f'assert.ok(readFile("index.html").includes({json.dumps(second)}));'}]}

    def test_each_repair_reclassifies_current_failures_and_cannot_reuse_a_fixed_defect(self):
        source=self.root/'index.html';source.write_text('<h1>pending</h1>')
        reviews=[]
        def review(task, original, criteria, sources, candidate, failures, *args, **kwargs):
            reviews.append((copy.deepcopy(sources),copy.deepcopy(failures)))
            return {'verdict':'SOURCE_DEFECT','reason':'Current behavior is missing',
                    'requirement':'Action connected','source_fragment':sources['index.html']}
        with patch.object(api,'_model_text',return_value=json.dumps(self.contract())) as author, \
             patch.object(api,'_review_verification_failure',side_effect=review):
            first=api._host_verification_phase('task',self.phase,self.plan,str(self.root))
            self.assertTrue(first['host_source_defect'])
            source.write_text('<h1>ready</h1>')
            # Re-enter using the materialized owned command, as a restored
            # task can do. This must not bypass current-source preparation.
            second=api._host_verification_phase('task',first,self.plan,str(self.root))
            self.assertTrue(second['host_source_defect'])
            self.assertEqual(['DONE:2'],[i for f in reviews[-1][1] for i in f['ids']])
            self.assertEqual('<h1>ready</h1>',reviews[-1][0]['index.html'])
            source.write_text('<h1>ready connected</h1>')
            final=api._host_verification_phase('task',second,self.plan,str(self.root))
        self.assertEqual(1,author.call_count,'Valid checks survive actual repairs')
        self.assertEqual(2,len(reviews),'Passing source needs no model review')
        self.assertFalse(final['host_source_defect'])
        self.assertFalse(final['host_partial_verification'])
        self.assertFalse(api._verification_repair_authorized(final,{'test_fail':['old defect']}))
        self.assertTrue(api._run_read_only_verification(final,str(self.root))[1]['passed'])

    def test_invalid_cached_check_is_repaired_without_granting_application_writes(self):
        source=self.root/'index.html';source.write_text('<h1>ready old-api connected</h1>')
        with patch.object(api,'_model_text',return_value=json.dumps(self.contract(second='old-api'))):
            first=api._host_verification_phase('task',self.phase,self.plan,str(self.root))
        source.write_text('<h1>ready new-api connected</h1>')
        corrected=self.contract()['checks'][1]
        with patch.object(api,'_model_text',return_value=json.dumps({'entrypoint':'index.html','checks':[corrected]})) as author, \
             patch.object(api,'_review_verification_failure',side_effect=[
                 {'verdict':'CHECK_DEFECT','reason':'The old implementation name is not a human requirement'},
                 {'verdict':'CHECK_VALID','reason':'Checks the required action'}]):
            final=api._host_verification_phase('task',first,self.plan,str(self.root))
        self.assertEqual(1,author.call_count)
        self.assertIn('DONE:2',author.call_args.args[4][-1]['content'])
        value=json.loads(Path(final['verification_command'][2]).read_text())
        self.assertEqual([['DONE:1'],['DONE:2']],[check['ids'] for check in value['checks']])
        self.assertFalse(api._verification_repair_authorized(final,{'test_fail':['old-api missing']}))
        self.assertEqual('<h1>ready new-api connected</h1>',source.read_text())

    def test_mixed_cached_gaps_keep_test_execution_owned(self):
        import hashlib
        (self.root/'index.html').write_text('<h1>ready connected</h1>')
        (self.root/'logic.test.cjs').write_text("require('node:test')('real behavior',()=>require('node:assert/strict').equal(2,2));")
        phase={**self.phase,'done':['Heading ready','Action connected',
            'node --test logic.test.cjs exits with code 0 and prints passing test results.']}
        criteria=api.acceptance_criteria(phase)
        identity=hashlib.sha256(json.dumps({'criteria':criteria,'request':'Build a working local application',
            'executor_version':16},sort_keys=True).encode()).hexdigest()[:16]
        directory=self.root/'coding-verification/task';directory.mkdir(parents=True)
        legacy=directory/(hashlib.sha256(b'final').hexdigest()[:16]+'-'+identity+'.json')
        legacy.write_text(json.dumps({'entrypoint':'index.html','checks':[self.contract()['checks'][0]]}))
        candidate={'entrypoint':'index.html','checks':[self.contract()['checks'][1]]}
        with patch.object(api,'_model_text',return_value=json.dumps(candidate)) as author:
            final=api._host_verification_phase('task',phase,self.plan,str(self.root))
        self.assertEqual(1,author.call_count)
        focus=author.call_args.args[4][-1]['content'].split('FINAL_FOCUS: ')[1].split('\n')[0]
        self.assertEqual({'DONE:2':'Action connected'},json.loads(focus))
        result=api._run_read_only_verification(final,str(self.root))[1]
        self.assertTrue(result['passed'],result)
        self.assertFalse(api._verification_repair_authorized(final,{'test_fail':['old failure']}))

    def test_historical_assertions_do_not_authorize_host_repairs(self):
        previous={'test_fail':['a previous check failed']}
        self.assertFalse(api._verification_repair_authorized(self.phase,previous))
        self.assertFalse(api._verification_repair_authorized({**self.phase,'host_source_defect':True},previous))
        reviewed={**self.phase,'host_verification_current_source_reviewed':True}
        for reason in ('host_source_defect','host_runtime_diagnostic'):
            self.assertTrue(api._verification_repair_authorized({**reviewed,reason:True},{}))
            self.assertFalse(api._verification_repair_authorized(
                {**reviewed,reason:True,'verification_repair_allowed':False},previous))
            no_permission={**reviewed,reason:True}
            no_permission.pop('verification_repair_allowed')
            self.assertFalse(api._verification_repair_authorized(no_permission,previous))
        self.assertTrue(api._verification_repair_authorized({**self.phase,'host_test_preflight':True},previous))
        self.assertFalse(api._verification_repair_authorized({**self.phase,'host_test_preflight':True},{}))
        self.assertTrue(api._verification_repair_authorized({'verification_repair_allowed':True},previous))

    def test_repeated_existing_checks_are_not_progress_and_resume_cannot_restart_authoring(self):
        (self.root/'index.html').write_text('<h1>ready connected</h1>')
        only_first={'entrypoint':'index.html','checks':[self.contract()['checks'][0]]}
        with patch.object(api,'_model_text',return_value=json.dumps(only_first)) as author:
            with self.assertRaisesRegex(RuntimeError,'no missing acceptance criterion') as failure:
                api._host_verification_phase('task',self.phase,self.plan,str(self.root))
            self.assertEqual(3,author.call_count,'One useful response and two bounded rejected repeats')
            api._record_verification_preparation_failure('task','final',failure.exception)
            with self.assertRaisesRegex(RuntimeError,'unchanged source|unchanged task'):
                api._host_verification_phase('task',self.phase,self.plan,str(self.root))
            self.assertEqual(3,author.call_count,'Resume with unchanged evidence cannot replenish the loop')
        cached=list((self.root/'coding-verification/task').glob('*.json'))
        self.assertTrue(any(json.loads(path.read_text()).get('checks')==only_first['checks'] for path in cached))

    def test_explicit_human_retry_can_repair_checks_without_changing_application(self):
        (self.root/'index.html').write_text('<h1>ready connected</h1>')
        only_first={'entrypoint':'index.html','checks':[self.contract()['checks'][0]]}
        with patch.object(api,'_model_text',return_value=json.dumps(only_first)):
            with self.assertRaises(RuntimeError) as failure:
                api._host_verification_phase('task',self.phase,self.plan,str(self.root))
        api._record_verification_preparation_failure('task','final',failure.exception)
        api.db.update_coding_task('task',status='RESUMABLE',recovery_reason='VERIFICATION_CONTRACT_INVALID')
        api._resume_coding_task_control(api.db.coding_task('task'),source='EXPLICIT_GUI_RESUME_ACTION')
        with patch.object(api,'_model_text',return_value=json.dumps(self.contract())) as author, patch.object(api,'_review_verification_failure',return_value={'verdict':'CHECK_VALID','reason':'Source-grounded checks'}):
            final=api._host_verification_phase('task',self.phase,self.plan,str(self.root))
        self.assertTrue(author.called)
        self.assertTrue(api._run_read_only_verification(final,str(self.root))[1]['passed'])
        self.assertEqual('<h1>ready connected</h1>',(self.root/'index.html').read_text())

    def test_same_model_gets_bounded_third_attempt_for_repeated_browser_check_defects(self):
        (self.root/'index.html').write_text('''<input id="dish"><select id="slot">
          <option value="">Choose</option><option value="monday-dinner">Monday dinner</option></select>
          <button id="assign" type="button">Assign</button><output id="assigned"></output><script src="app.js"></script>''')
        (self.root/'app.js').write_text("const dish=document.querySelector('#dish');const slot=document.querySelector('#slot');const assigned=document.querySelector('#assigned');function render(){assigned.textContent=localStorage.getItem('assigned')||'Unassigned';}document.querySelector('#assign').addEventListener('click',()=>{localStorage.setItem('assigned',dish.value+':'+slot.value);render();});render();")
        phase={**self.phase,'done':['In the browser, manually assign a dish to a weekday meal slot and verify it is restored after reload.'],'verify':[]}
        weak_action={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER','code':
            "await page.locator('#assign').click();assert.equal(await page.locator('#assigned').textContent(),'Miso soup:monday-dinner');"}]}
        weak_static={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER','code':
            "assert.equal(await page.locator('#assigned').textContent(),'Unassigned');"}]}
        complete={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER','code':[
            "await page.locator('#dish').fill('Miso soup');",
            "await page.locator('#slot').selectOption('monday-dinner');",
            "await page.locator('#assign').click();",
            "await page.reload();",
            "await expect(page.locator('#assigned')).toHaveText('Miso soup:monday-dinner');"]}]}
        plan={'file_manifest':[{'path':'index.html'},{'path':'app.js'}]}
        with patch.object(api,'_alternate_coding_model',return_value=None), \
             patch.object(api,'_model_text',side_effect=[json.dumps(weak_action),json.dumps(weak_static),json.dumps(complete)]) as author, \
             patch.object(api,'_review_verification_failure',side_effect=[
                 {'verdict':'CHECK_DEFECT','verification_form':'STATEFUL_CODE','reason':'The assignment action is not set up.'},
                 {'verdict':'CHECK_DEFECT','verification_form':'STATEFUL_CODE','reason':'Static initial state does not prove reload persistence.'},
                 {'verdict':'CHECK_VALID','verification_form':'STATEFUL_CODE','reason':'The real selection is assigned and rendered after reload.'}]) as review:
            final=api._host_verification_phase('task',phase,plan,str(self.root))
        self.assertEqual(3,author.call_count)
        self.assertEqual(3,review.call_count)
        self.assertEqual(1,len({call.args[3] for call in author.call_args_list}),
            'A same-identity Brain configuration must not be logged as an alternate-model handoff')
        third_prompt=author.call_args_list[2].args[4][1]['content']
        self.assertIn('selectOption',third_prompt)
        self.assertIn('NATIVE_SELECT_FOCUS:',third_prompt)
        self.assertIn('reload',third_prompt.lower())
        self.assertTrue(api._run_read_only_verification(final,str(self.root))[1]['passed'])

    def test_explicit_retry_epoch_saturates_for_unchanged_verification_failure(self):
        (self.root/'index.html').write_text('<main>ready</main>')
        task_id='bounded-verification-retry'
        api.db.create_coding_task(task_id,'conversation','Verify the page behavior.','RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':['The page behavior is verified.'],'verify':[]}
        for expected_epoch in (0,1):
            with patch.object(api,'_model_text',return_value='not JSON'):
                with self.assertRaisesRegex(RuntimeError,'VERIFICATION_CONTRACT_INVALID') as failure:
                    api._host_verification_phase(task_id,phase,self.plan,str(self.root))
            api._record_verification_preparation_failure(task_id,'final',failure.exception)
            api.db.update_coding_task(task_id,status='RESUMABLE',recovery_reason='VERIFICATION_CONTRACT_INVALID')
            with patch.object(api.db,'enqueue_coding_task',side_effect=lambda current_id:
                    api.db.update_coding_task(current_id,status='QUEUED')), \
                 patch.object(api._coding_scheduler_wake,'set'):
                api._resume_coding_task_control(api.db.coding_task(task_id),source='EXPLICIT_GUI_RESUME_ACTION')
            saved=api.db.coding_task(task_id)
            self.assertEqual(min(expected_epoch+1,1),saved['resume_state']['verification_retry_epoch'])

        with patch.object(api,'_model_text',side_effect=AssertionError('Unchanged verification must not restart authoring')) as model:
            with self.assertRaisesRegex(RuntimeError,'same verification contract already failed'):
                api._host_verification_phase(task_id,phase,self.plan,str(self.root))
        model.assert_not_called()

    def test_explicit_retry_resets_an_exhausted_verification_budget_once(self):
        (self.root/'index.html').write_text('<main>ready</main>')
        task_id='budget-retry-once'
        api.db.create_coding_task(task_id,'conversation','Verify the page behavior.','RUNNING','NONE',self.plan,time.time())
        phase={'id':'final','host_verification':True,'done':['The page behavior is verified.'],'verify':[]}
        exhausted={'model_calls':api.VERIFICATION_MAX_MODEL_CALLS,
            'author_calls':api.VERIFICATION_MAX_AUTHOR_CALLS,
            'review_calls':api.VERIFICATION_MAX_REVIEW_CALLS,
            'author_call_limit':api.VERIFICATION_MAX_AUTHOR_CALLS,
            'exhaustion':'independent-review limit','_resume_budget_initialized':True,
            '_resume_budget_reset':False}
        with patch.object(api,'_model_text',return_value='not JSON') as first_author:
            with self.assertRaisesRegex(RuntimeError,'VERIFICATION_CONTRACT_INVALID') as first_failure:
                api._host_verification_phase(task_id,phase,self.plan,str(self.root),_budget=exhausted)
        first_author.assert_not_called()
        api._record_verification_preparation_failure(task_id,'final',first_failure.exception)
        api.db.update_coding_task(task_id,status='RESUMABLE',recovery_reason='VERIFICATION_CONTRACT_INVALID')
        with patch.object(api.db,'enqueue_coding_task',side_effect=lambda current_id:
                api.db.update_coding_task(current_id,status='QUEUED')), patch.object(api._coding_scheduler_wake,'set'):
            api._resume_coding_task_control(api.db.coding_task(task_id),source='EXPLICIT_GUI_RESUME_ACTION')

        retry_budget={}
        with patch.object(api,'_model_text',return_value='not JSON') as retry_author:
            with self.assertRaisesRegex(RuntimeError,'VERIFICATION_CONTRACT_INVALID') as retry_failure:
                api._host_verification_phase(task_id,phase,self.plan,str(self.root),_budget=retry_budget)
        self.assertGreater(retry_author.call_count,0)
        self.assertGreater(retry_budget.get('model_calls',0),0)
        api._record_verification_preparation_failure(task_id,'final',retry_failure.exception)
        api.db.update_coding_task(task_id,status='RESUMABLE',recovery_reason='VERIFICATION_CONTRACT_INVALID')
        with patch.object(api.db,'enqueue_coding_task',side_effect=lambda current_id:
                api.db.update_coding_task(current_id,status='QUEUED')), patch.object(api._coding_scheduler_wake,'set'):
            api._resume_coding_task_control(api.db.coding_task(task_id),source='EXPLICIT_GUI_RESUME_ACTION')
        with patch.object(api,'_model_text',side_effect=AssertionError('The single unchanged-source retry is exhausted')) as final_author:
            with self.assertRaisesRegex(RuntimeError,'same verification contract already failed'):
                api._host_verification_phase(task_id,phase,self.plan,str(self.root))
        final_author.assert_not_called()

    def test_mixed_check_api_error_cannot_hide_a_real_application_defect(self):
        source=self.root/'index.html';source.write_text('<h1>pending</h1>')
        candidate=self.contract()
        candidate['checks'][0]['code']="assert.missingMatcher('ready');"
        repaired={'entrypoint':'index.html','checks':[self.contract()['checks'][0]]}
        def review(task, original, criteria, sources, checks, failures, *args, **kwargs):
            self.assertEqual([['DONE:2']],[failure['ids'] for failure in failures])
            self.assertTrue(all(not failure.get('contract_error') for failure in failures))
            return {'verdict':'SOURCE_DEFECT','reason':'The real action is disconnected',
                    'requirement':'Action connected','source_fragment':sources['index.html']}
        with patch.object(api,'_model_text',side_effect=[json.dumps(candidate),json.dumps(repaired)]) as author, \
             patch.object(api,'_review_verification_failure',side_effect=review) as reviewer:
            broken=api._host_verification_phase('task',self.phase,self.plan,str(self.root))
            self.assertTrue(broken['host_source_defect'])
            source.write_text('<h1>ready connected</h1>')
            final=api._host_verification_phase('task',broken,self.plan,str(self.root))
        self.assertEqual(2,author.call_count)
        self.assertEqual(1,reviewer.call_count)
        self.assertFalse(final['host_source_defect'])
        self.assertTrue(api._run_read_only_verification(final,str(self.root))[1]['passed'])

    def test_browser_records_disconnected_actions_then_accepts_the_real_fix(self):
        source=self.root/'index.html'
        source.write_text('<button id="increment">Add</button><output id="count">0</output>')
        phase={**self.phase,'done':['In the browser, a click increments the displayed count']}
        contract={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER',
            'code':"await page.locator('#increment').click();assert.equal(await page.locator('#count').textContent(),'1');"}]}
        path=self.root/'contract.json';path.write_text(json.dumps(contract))
        phase.update(verification_command=api.acceptance_command(path,self.root),
                     verification_targets=['index.html'],host_criteria=api.acceptance_criteria(phase))
        failed=api._run_read_only_verification(phase,str(self.root))[1]
        self.assertFalse(failed['passed'])
        action=failed['failures'][0]['browser_actions'][0]
        self.assertEqual('completed',action['outcome'])
        self.assertFalse(action['observed_state_changed'])
        source.write_text(source.read_text()+'''<script>
          document.querySelector('#increment').addEventListener('click',()=>{
            document.querySelector('#count').textContent='1';
          });</script>''')
        self.assertTrue(api._run_read_only_verification(phase,str(self.root))[1]['passed'])

    def test_explicit_python_test_executor_rechecks_source_before_granting_repair(self):
        source=self.root/'config.py';source.write_text('VALUE=0\n')
        (self.root/'test_config.py').write_text('import unittest,config\n'
            'class ConfigTests(unittest.TestCase):\n'
            ' def test_value(self): self.assertEqual(config.VALUE,1)\n')
        phase={**self.phase,'done':['Tests pass'],'verification_command':[
            sys.executable,'-B','-m','unittest','test_config'],
            'verification_targets':['config.py','test_config.py']}
        plan={'file_manifest':[{'path':'config.py'},{'path':'test_config.py'}]}
        with patch.object(api,'_review_project_test_failures',return_value={
            'verdict':'SOURCE_DEFECT','reason':'The implementation value contradicts the requested value.',
            'requirement':'Tests pass','source_fragment':'VALUE=0','repair_paths':['config.py'],
            'evidence_origin':'CURRENT_PROJECT_TEST_REVIEW'}) as review:
            failed=api._host_verification_phase('task',phase,plan,str(self.root))
            self.assertTrue(api._verification_repair_authorized(failed,{}))
            self.assertIn('AssertionError',failed['host_current_command_evidence']['output'])
            self.assertEqual(['config.py'],failed['host_source_defect_repair_paths'])
            self.assertEqual(1,review.call_count)
            source.write_text('VALUE=1\n')
            passed=api._host_verification_phase('task',failed,plan,str(self.root))
        self.assertFalse(api._verification_repair_authorized(passed,{'test_fail':['old result']}))
        self.assertIsNone(passed.get('host_current_command_evidence'))
        self.assertEqual(phase['verification_command'],passed['verification_command'])

    def test_typed_recovery_executes_module_json_and_tests_without_generating_javascript_literals(self):
        (self.root/'index.html').write_text('<h1 data-label="a\'b">ready</h1>')
        (self.root/'logic.cjs').write_text('exports.measure=value=>value.length;')
        (self.root/'package.json').write_text('{"type":"commonjs"}')
        (self.root/'logic.test.cjs').write_text("require('node:test')('actual API',()=>require('node:assert/strict').equal(require('./logic.cjs').measure('abc'),3));")
        observations=[
            {'selector':'index.html','measure':'file_text','operator':'includes','expected':"a'b"},
            {'selector':'logic.cjs','measure':'module_has_export','operator':'eq','expected':True,'export_name':'measure'},
            {'selector':'logic.cjs','measure':'module_call','operator':'eq','expected':4,'export_name':'measure','arguments':['a"b\'']},
            {'selector':'package.json','measure':'json_field','operator':'eq','expected':'commonjs','property_path':['type']},
            {'selector':'logic.test.cjs','measure':'test_count','operator':'gt','expected':0},
            {'selector':'html','measure':'overflow_x','operator':'eq','expected':False}]
        contract=api.compile_browser_observations({'entrypoint':'index.html','checks':[
            {'ids':['DONE:1'],'kind':'BROWSER','actions':[{'action':'viewport','selector':'page',
                'value':{'width':320,'height':640}}],'observations':observations}]})
        self.assertEqual([],api.verification_syntax_errors(contract))
        path=self.root/'typed.json';path.write_text(json.dumps(contract))
        phase={**self.phase,'done':['Actual module and configuration work'],
               'verification_command':api.acceptance_command(path,self.root),
               'verification_targets':['index.html','logic.cjs','package.json','logic.test.cjs'],
               'host_criteria':{'DONE:1':'Actual module and configuration work'}}
        result=api._run_read_only_verification(phase,str(self.root))[1]
        self.assertTrue(result['passed'],result['output'])
        self.assertEqual(1,result['test_runs'][0]['registered_tests'])
        invalid=copy.deepcopy(observations[2]);invalid['arguments']=[{'code':'return true'}]
        with self.assertRaisesRegex(ValueError,'module arguments'):
            api.compile_browser_observations({'checks':[{'kind':'BROWSER','actions':[],'observations':[invalid]}]})

    def test_javascript_syntax_failure_switches_to_typed_recovery_and_requires_semantic_review(self):
        (self.root/'index.html').write_text('<h1>ready</h1>')
        (self.root/'logic.cjs').write_text('exports.measure=value=>value.length;')
        phase={**self.phase,'done':['The module measures a string correctly']}
        plan={'file_manifest':[{'path':'index.html'},{'path':'logic.cjs'}]}
        malformed={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'NODE',
                    'code':"const page = await page.locator('body');"}]}
        typed={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER','actions':[],
            'observations':[{'selector':'logic.cjs','measure':'module_call','operator':'eq','expected':3,
                             'export_name':'measure','arguments':['abc']}]}]}
        with patch.object(api,'_model_text',side_effect=[json.dumps(malformed),json.dumps(typed)]) as author, \
             patch.object(api,'_review_verification_failure',return_value={'verdict':'CHECK_VALID'}) as review:
            final=api._host_verification_phase('task',phase,plan,str(self.root))
        self.assertEqual(2,author.call_count)
        branches=author.call_args.kwargs['structured_schema']['properties']['checks']['items']['anyOf']
        self.assertNotIn('code',branches[0]['properties'])
        self.assertIn('code',branches[1]['properties'])
        self.assertEqual(1,review.call_count,'A syntax-safe check still must prove the original requirement')
        self.assertTrue(json.loads(Path(final['verification_command'][2]).read_text())['_olcr_typed_transport'])
        self.assertFalse(api._verification_repair_authorized(final,{'test_fail':['old syntax error']}))
        self.assertTrue(api._run_read_only_verification(final,str(self.root))[1]['passed'])

    def test_pause_after_first_syntax_error_preserves_typed_transport_for_resume(self):
        (self.root/'index.html').write_text('<h1>ready</h1>')
        phase={**self.phase,'done':['Heading ready']}
        malformed={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'NODE',
            'code':"const readFile = await loadModule('node:fs/promises');"}]}
        def paused(*args,**kwargs):
            api.db.update_coding_task('task',pause_requested=1)
            return json.dumps(malformed)
        with patch.object(api,'_model_text',side_effect=paused):
            api._host_verification_phase('task',phase,self.plan,str(self.root))
        drafts=list((self.root/'coding-verification/task').glob('*-authoring.json'))
        self.assertEqual(1,len(drafts))
        self.assertTrue(json.loads(drafts[0].read_text())['typed_transport'])
        api.db.update_coding_task('task',pause_requested=0,status='RUNNING')
        typed={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER','actions':[],
            'observations':[{'selector':'h1','measure':'text','operator':'eq','expected':'ready'}]}]}
        with patch.object(api,'_model_text',return_value=json.dumps(typed))as author, \
             patch.object(api,'_review_verification_failure',return_value={'verdict':'CHECK_VALID'}):
            final=api._host_verification_phase('task',phase,self.plan,str(self.root))
        self.assertEqual(1,author.call_count)
        branches=author.call_args.kwargs['structured_schema']['properties']['checks']['items']['anyOf']
        self.assertNotIn('code',branches[0]['properties'])
        self.assertIn('code',branches[1]['properties'])
        self.assertTrue(api._run_read_only_verification(final,str(self.root))[1]['passed'])


if __name__=='__main__':
    unittest.main()
