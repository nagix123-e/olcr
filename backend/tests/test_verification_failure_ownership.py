"""Regressions for the saved 3 -> 4 phase meal-planner failure."""
import copy
import tempfile
import unittest
from pathlib import Path

from olcr_api.coding_verification import normalize_static_verification, compile_browser_observations, verify_required_scenario_capabilities


class VerificationFailureOwnershipTests(unittest.TestCase):
    def test_shopping_list_object_rows_with_unspecified_order_are_test_defect(self):
        from olcr_api.verification_diagnostics import node_test_run, project_test_failure_diagnosis
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            output=("not ok 1 - shopping list contains meal ingredients\n"
                "  ---\n  duration_ms: 1.2\n  location: 'tests/logic.test.js:76:1'\n"
                "  failureType: 'testCodeFailure'\n"
                "  error: |\n    Expected values to be strictly deep-equal:\n"
                "  code: 'ERR_ASSERTION'\n  name: 'AssertionError'\n"
                "  expected:\n    0:\n      name: '牛乳'\n      quantity: 1\n      unit: '個'\n"
                "    1:\n      name: '卵'\n      quantity: 2\n      unit: '個'\n"
                "    2:\n      name: '米'\n      quantity: 1\n      unit: '個'\n"
                "  actual:\n    0:\n      name: '牛乳'\n      quantity: 1\n      unit: '個'\n"
                "    1:\n      name: '米'\n      quantity: 1\n      unit: '個'\n"
                "    2:\n      name: '卵'\n      quantity: 2\n      unit: '個'\n"
                "  operator: 'deepStrictEqual'\n  ...\n"
                "# tests 1\n# pass 0\n# fail 1\n")
            run=node_test_run(['node','--test','tests/logic.test.js'],output,1,root)
            failure=run['failures'][0]
            self.assertEqual('deepStrictEqual',failure.get('assertion_operator'))
            self.assertEqual([{'name':'牛乳','quantity':1,'unit':'個'},
                {'name':'卵','quantity':2,'unit':'個'},{'name':'米','quantity':1,'unit':'個'}],
                failure.get('expected'))
            sources={'tests/logic.test.js':"assert.deepStrictEqual(shoppingList, expected);",'src/logic.js':'return sortedItems;'}
            finding=project_test_failure_diagnosis({'test_runs':[run]},sources,
                requirements=['食材から買い物リストを作る'],
                file_manifest=[{'path':'tests/logic.test.js','action':'create'}])
        self.assertEqual('CHECK_DEFECT',finding['verdict'])
        self.assertFalse(finding.get('project_test_setup_repair'))
        self.assertEqual(['tests/logic.test.js'],finding['repair_paths'])

    def test_unrequested_shopping_list_order_mismatch_is_a_test_defect(self):
        from olcr_api.verification_diagnostics import node_test_run, project_test_failure_diagnosis
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            output=("✖ meal ingredients form a unique shopping list (1.2ms)\n"
                "ℹ tests 1\nℹ pass 0\nℹ fail 1\nℹ duration_ms 5\n"
                "✖ failing tests:\n\ntest at src/tests/logic.test.js:10:42\n"
                "✖ meal ingredients form a unique shopping list (1.2ms)\n"
                "  AssertionError [ERR_ASSERTION]: Expected values to be strictly deep-equal:\n"
                "  code: 'ERR_ASSERTION'\n  name: 'AssertionError'\n"
                "  expected:\n    0: 'ねぎ'\n    1: '豆腐'\n    2: '魚'\n"
                "  actual:\n    0: 'ねぎ'\n    1: '魚'\n    2: '豆腐'\n"
                "  operator: 'deepStrictEqual'\n")
            run=node_test_run(['node','--test','src/tests/logic.test.js'],output,1,root)
            sources={'src/tests/logic.test.js':"assert.deepEqual(shoppingList(plan), ['ねぎ','豆腐','魚']);",
                'src/logic.js':'return uniqueIngredients(plan);'}
            finding=project_test_failure_diagnosis({'test_runs':[run]},sources,
                requirements=['材料から重複のない買い物リストを作る'],
                file_manifest=[{'path':'src/tests/logic.test.js','action':'create'}])
        self.assertEqual('CHECK_DEFECT',finding['verdict'])
        self.assertEqual(['src/tests/logic.test.js'],finding['repair_paths'])
        from olcr_api import app as api
        from unittest.mock import patch
        with patch.object(api,'_model_text',side_effect=AssertionError('Measured order-only mismatch needs no model review')):
            routed=api._review_project_test_failures('order-only','買い物リストを材料から作る',
                {'VERIFY:1':'材料から買い物リストを作る'},sources,{'test_runs':[run]},
                file_manifest=[{'path':'src/tests/logic.test.js','action':'create'}])
        self.assertEqual('CHECK_DEFECT',routed['verdict'])
        self.assertEqual(['src/tests/logic.test.js'],routed['repair_paths'])

    def test_shopping_list_name_count_rows_allow_unspecified_order(self):
        from olcr_api.verification_diagnostics import project_test_failure_diagnosis
        expected=[{'name':'ねぎ','count':1},{'name':'卵','count':2},{'name':'米','count':3}]
        actual=[{'name':'ねぎ','count':1},{'name':'米','count':3},{'name':'卵','count':2}]
        failure={'name':'shopping list counts ingredients','failure_type':'testCodeFailure',
            'assertion_operator':'deepStrictEqual','expected':expected,'actual':actual,
            'source_locations':[{'path':'tests/logic.test.js','line':44}]}
        sources={'tests/logic.test.js':'assert.deepEqual(buildShoppingList(plan, recipes), expected);',
                 'src/logic.js':'return [...quantities].map(([name, count]) => ({ name, count }));'}

        finding=project_test_failure_diagnosis({'test_runs':[{'returncode':1,'failures':[failure]}]},sources,
            requirements=['材料の買い物リストを作成する'],
            file_manifest=[{'path':'tests/logic.test.js','action':'create'}])

        self.assertEqual('CHECK_DEFECT',finding['verdict'])
        self.assertEqual(['tests/logic.test.js'],finding['repair_paths'])

        from olcr_api import app as api
        from unittest.mock import patch
        observed={'test_runs':[{'returncode':1,'failures':[failure]}]}
        manifest=[{'path':'tests/logic.test.js','action':'create'},
                  {'path':'src/logic.js','action':'create'}]
        with patch.object(api,'_model_text',side_effect=AssertionError('Exact order-only mismatches need no model review')):
            routed=api._review_project_test_failures('shopping-count-order',
                'Create a shopping list from recipe ingredients.',
                {'VERIFY:1':'Create a shopping list from recipe ingredients.'},sources,observed,
                file_manifest=manifest)
        self.assertEqual('CHECK_DEFECT',routed['verdict'])
        self.assertEqual(['tests/logic.test.js'],routed['repair_paths'])

        for changed,requirements in (
            (dict(failure,actual=[{'name':'ねぎ','count':1},{'name':'米','count':4},{'name':'卵','count':2}]),
             ['材料の買い物リストを作成する']),
            (dict(failure,actual=actual+[{'name':'卵','count':2}]),
             ['材料の買い物リストを作成する']),
            (failure,['材料の買い物リストを指定順で表示する']),
        ):
            with self.subTest(changed=changed,requirements=requirements):
                finding=project_test_failure_diagnosis({'test_runs':[{'returncode':1,'failures':[changed]}]},
                    sources,requirements=requirements,file_manifest=[{'path':'tests/logic.test.js','action':'create'}])
                self.assertEqual('INCONCLUSIVE',finding['verdict'])

    def test_order_mismatch_recovery_requires_order_free_requirement_and_permutation(self):
        from olcr_api.verification_diagnostics import project_test_failure_diagnosis
        failure={'assertion_operator':'deepStrictEqual','expected':['ねぎ','豆腐','魚'],
            'actual':['ねぎ','魚','豆腐'],'source_locations':[{'path':'tests/logic.test.js','line':10}]}
        sources={'tests/logic.test.js':"assert.deepEqual(list, ['ねぎ','豆腐','魚']);",'logic.js':'return list;'}
        manifest=[{'path':'tests/logic.test.js','action':'create'}]
        cases=[
            (['材料から買い物リストを指定順で表示する'],manifest,'INCONCLUSIVE'),
            (['材料から買い物リストを作成する'],[], 'INCONCLUSIVE'),
            (['材料から買い物リストを作成する'],manifest,'CHECK_DEFECT'),
        ]
        for expected,files,verdict in cases:
            with self.subTest(expected=expected,files=files):
                current=dict(failure)
                if verdict=='INCONCLUSIVE' and expected==['材料から買い物リストを作成する'] and files:
                    current['actual']=['ねぎ','豆腐','鮭']
                finding=project_test_failure_diagnosis({'test_runs':[{'failures':[current]}]},sources,
                    requirements=expected,file_manifest=files)
                self.assertEqual(verdict,finding['verdict'])

    def test_inconclusive_test_review_is_prompted_to_audit_fixture_preconditions(self):
        import json
        from unittest.mock import patch
        from olcr_api import app as api
        original='A complete weekly plan must obey the budget cap and avoid consecutive identical meals.'
        test_source="it('respects the budget',()=>generatePlan([cheap,expensive],{budgetLimit:210}));"
        sources={'src/tests/logic.test.js':test_source,'src/logic.js':'throw new Error("No feasible plan");'}
        failure={'name':'respects the budget','failure_type':'testCodeFailure',
            'detail':'No feasible plan','source_locations':[{'path':'src/tests/logic.test.js','line':1},
                {'path':'src/logic.js','line':1}]}
        response=json.dumps({'verdict':'INCONCLUSIVE','reason':'Fixture feasibility not established.',
            'requirement':'','source_fragment':'','test_fragment':''})
        with patch.object(api,'_model_text',return_value=response) as review:
            finding=api._review_project_test_failure('fixture-audit',original,{'VERIFY:1':original},
                sources,failure)
        self.assertEqual('INCONCLUSIVE',finding['verdict'])
        self.assertEqual(1,review.call_count)
        self.assertIn('audit the concrete test fixture',
            review.call_args.args[4][1].get('content',''))

    def test_project_test_commands_are_recognized_across_supported_runtimes(self):
        from olcr_api.verification_diagnostics import is_project_test_command
        cases=[
            (['node','--test','tests/app.test.js'],True),
            (['python3','-m','unittest'],True),
            (['python3.12','-m','pytest'],True),
            (['pytest','-q'],True),
            (['go','test','./...'],True),
            (['cargo','test'],True),
            (['npm','run','test:unit'],True),
            (['npx','jest','--runInBand'],True),
            (['mvn','-q','test'],True),
            (['dotnet','test','App.Tests.csproj'],True),
            (['npm','run','build'],False),
            (['npm','run','build','--','tests/test_helper.py'],False),
            (['vite','build'],False),
        ]
        for command,expected in cases:
            with self.subTest(command=command):
                self.assertEqual(expected,is_project_test_command(command))

    def test_project_test_paths_cover_common_language_conventions(self):
        from olcr_api.verification_diagnostics import is_project_test_path
        for path in ['tests/test_logic.py','scripts/run_tests.py','src/logic.test.ts',
                     'src/logic_test.go','src/LogicTests.cs','spec/models/user_spec.rb']:
            with self.subTest(path=path):
                self.assertTrue(is_project_test_path(path))
        self.assertFalse(is_project_test_path('tests/fixture.json'))
        self.assertFalse(is_project_test_path('src/logic.py'))

    def test_test_repair_requires_an_exact_reviewed_path(self):
        from olcr_api.verification_diagnostics import reviewed_project_test_paths
        sources={'tests/app.test.js':'','tests/test_logic.py':'','app.js':''}
        self.assertEqual([],reviewed_project_test_paths({'verdict':'CHECK_DEFECT'},sources))
        self.assertEqual(['tests/app.test.js'],reviewed_project_test_paths(
            {'verdict':'CHECK_DEFECT','repair_paths':['tests/app.test.js','app.js','missing.test.js']},sources))

    def test_test_producer_runs_registered_tests_before_phase_completion(self):
        plan={'file_manifest':[
            {'path':'index.html','owner_phase':'app'},
            {'path':'app.js','owner_phase':'app'},
            {'path':'tests/planner.test.js','owner_phase':'tests'}], 'phases':[
            {'id':'app','execution_mode':'IMPLEMENTATION','done':['App works'],'verify':[], 'dependencies':[]},
            {'id':'tests','execution_mode':'IMPLEMENTATION','done':['Tests cover the app'],
             'verify':['Tests pass'],'dependencies':['app']},
            {'id':'final','execution_mode':'VERIFICATION_ONLY','done':['Browser works'],'verify':[], 'dependencies':['tests']}]}
        original=copy.deepcopy(plan)
        result=normalize_static_verification(plan,verification_requested=True)
        phase=result['phases'][1]
        self.assertEqual(['node','--test','tests/planner.test.js'],phase.get('verification_command'))
        self.assertEqual('IMPLEMENTATION_AND_VERIFICATION',phase['execution_mode'])
        self.assertEqual({'index.html','app.js','tests/planner.test.js'},set(phase['verification_targets']))
        self.assertEqual(original,plan)
        self.assertEqual(result,normalize_static_verification(result))

    def test_condition_alias_and_output_element_are_recognized(self):
        html='''<html lang="ja"><button id="btn-new-meal">料理追加</button>
          <form id="meal-form"><input id="m-name"><input id="m-budget" type="number">
          <button type="submit">保存</button></form>
          <input id="plan-budget" type="number"><input id="plan-allergen">
          <button id="btn-generate">献立生成</button><output id="plan-status"></output></html>'''
        criteria={'DONE:1':'空データと条件に合う料理がない場合にエラーを表示する。'}
        sources={'index.html':html,'app.js':"showToast('条件に合う料理がありません');"}
        self.assertFalse(verify_required_scenario_capabilities(criteria,sources))
        value={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER','scenario':'empty_data',
            'actions':[],'observations':[{'selector':'#plan-status','measure':'text','operator':'includes','expected':'空'}]}]}
        checks=compile_browser_observations(value,criteria,sources)['checks']
        check=next(check for check in checks if '#plan-budget' in check['code'])
        self.assertIn('#plan-status',check['code'])

    def test_no_match_scenario_supports_an_always_visible_form_without_add_button(self):
        criteria={'VERIFY:10':'空データと条件に合う料理がない場合の画面表示を確認する。'}
        html='''<html lang="ja"><div id="notice" hidden></div>
          <form id="recipe-form"><input id="recipe-name" required>
          <input id="recipe-time" type="number" value="30" required>
          <input id="recipe-cost" type="number" value="500" required>
          <button id="save-recipe" type="submit">料理を登録</button></form>
          <input id="budget-limit" type="number"><button id="clear-plan">献立をクリア</button>
          <button id="generate">献立を自動作成</button>
          <div id="recipe-list"></div></html>'''
        app="""function notify(message) { document.querySelector('#notice').textContent=message; }
        function finishPlan(assigned) {
          notify(assigned ? `${assigned}食分の献立を作成しました。条件に合う料理がない枠は未設定です。`
            : '条件に合う料理がありません。料理や条件を確認してください。');
        }"""
        sources={'index.html':html,'src/ui.js':app}
        self.assertFalse(verify_required_scenario_capabilities(criteria,sources))
        value={'entrypoint':'index.html','checks':[{'ids':['VERIFY:10'],'kind':'BROWSER',
            'scenario':'empty_data','actions':[],'observations':[
                {'selector':'#recipe-list','measure':'count','operator':'eq','expected':0}]}]}
        result=compile_browser_observations(value,criteria,sources)
        no_match=next(check for check in result['checks'] if '#budget-limit' in check['code'])
        for selector in ('#recipe-name','#recipe-cost','#save-recipe','#budget-limit','#generate','#notice'):
            self.assertIn(selector,no_match['code'])
        self.assertNotIn('#clear-plan',no_match['code'])
        self.assertIn('条件に合う料理がありません。',no_match['code'])
        self.assertNotIn('条件に合う料理がない枠は未設定です。',no_match['code'])
        hidden_sources={**sources,'index.html':html.replace('<form id="recipe-form">',
                                                            '<form id="recipe-form" hidden>')}
        diagnostics=verify_required_scenario_capabilities(criteria,hidden_sources)
        self.assertTrue(diagnostics)
        self.assertIn('a source-backed no-match interaction',
                      diagnostics[0]['failures'][0]['missing_capabilities'])

    def test_static_scan_uncertainty_never_establishes_a_source_defect(self):
        criteria={'DONE:1':'Handle empty data and no matching dishes under budget conditions in the browser.'}
        diagnostics=verify_required_scenario_capabilities(criteria,{'index.html':'<div id="root"></div>'})
        self.assertEqual('INCONCLUSIVE',diagnostics[0]['review']['verdict'])

    def test_author_can_use_real_allergy_control_without_host_budget_selector(self):
        criteria={'DONE:1':'空データと条件に合う料理がない場合にエラーを表示する。'}
        value={'entrypoint':'index.html','checks':[{'ids':['DONE:1'],'kind':'BROWSER','scenario':'empty_data',
            'actions':[],'observations':[{'selector':'#message','measure':'text','operator':'includes','expected':'空'}]},
            {'ids':['DONE:1'],'kind':'BROWSER',
            'scenario':'no_matching_items','actions':[
                {'action':'fill','selector':'#avoid','value':'卵'},
                {'action':'click','selector':'#generate','value':None}],
            'observations':[{'selector':'#message','measure':'text','operator':'includes','expected':'該当なし'}]}]}
        result=compile_browser_observations(value,criteria,{'index.html':'<input id="avoid"><button id="generate">作成</button><p id="message"></p>'})
        self.assertTrue(any('#avoid' in check['code'] for check in result['checks']))

    def test_node_harness_error_is_attributed_to_tests_and_preserves_first_error(self):
        from olcr_api.verification_diagnostics import node_test_run, project_test_failure_diagnosis
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'tests').mkdir()
            test=root/'tests/app.test.js';test.write_text('describe("app",()=>{});')
            output=f'# {test}:1\n# describe("app",()=>{{}});\n# ReferenceError: describe is not defined\n#     at {test}:1:1\n'+'# stack noise\n'*800+'# tests 1\n# pass 0\n# fail 1\n'
            run=node_test_run(['node','--test','tests/app.test.js'],output,1,root)
            self.assertIn('describe is not defined',run['failures'][0]['detail'])
            finding=project_test_failure_diagnosis({'test_runs':[run],'output':output[-4000:]},{'tests/app.test.js':test.read_text(),'app.js':'function app(){}'})
            self.assertEqual('CHECK_DEFECT',finding['verdict'])
            self.assertTrue(finding.get('project_test_setup_repair'))
            self.assertEqual(['tests/app.test.js'],finding['repair_paths'])

    def test_assertion_failure_does_not_automatically_authorize_source_or_test_rewrite(self):
        from olcr_api.verification_diagnostics import project_test_failure_diagnosis
        finding=project_test_failure_diagnosis({'output':'AssertionError: 1 !== 2','test_runs':[]},
            {'tests/app.test.js':'assert.equal(app(),2);','app.js':'function app(){return 1;}'})
        self.assertEqual('INCONCLUSIVE',finding['verdict'])
        self.assertFalse(finding.get('repair_paths'))

    def test_node_spec_reporter_assertion_is_not_misclassified_as_module_loading(self):
        from olcr_api.verification_diagnostics import node_test_run, project_test_failure_diagnosis
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            test=root/'value.test.js'
            test.write_text("const test=require('node:test');const assert=require('node:assert/strict');"
                "test('value',()=>assert.equal(require('./value.js').value,1));")
            output=("✖ value (0.6ms)\nℹ tests 1\nℹ suites 0\nℹ pass 0\nℹ fail 1\n"
                "ℹ cancelled 0\nℹ skipped 0\nℹ todo 0\nℹ duration_ms 27.0\n\n"
                "✖ failing tests:\n\ntest at value.test.js:1:40\n✖ value (0.6ms)\n"
                "  AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:\n"
                "  0 !== 1\n\n"
                f"      at TestContext.<anonymous> ({test}:1:60)\n"
                "      at Test.runInAsyncScope (node:async_hooks:227:14)\n")
            run=node_test_run(['node','--test','value.test.js'],output,1,root)
            finding=project_test_failure_diagnosis({'test_runs':[run]},
                {'value.test.js':test.read_text(),'value.js':'module.exports={value:0};'})
        self.assertEqual(1,run['registered_tests'])
        self.assertNotEqual('Node.js module loading',run['failures'][0]['name'])
        self.assertEqual('INCONCLUSIVE',finding['verdict'])

    def test_tap_form_of_missing_expect_is_a_test_setup_defect(self):
        from olcr_api.verification_diagnostics import project_test_failure_diagnosis
        finding=project_test_failure_diagnosis({'test_runs':[{'failures':[{
            'detail':"error: 'expect is not a function'\nname: 'TypeError'",
            'source_locations':[{'path':'tests/app.test.js','line':5}]}]}]},
            {'tests/app.test.js':"const {expect}=require('node:test');"})
        self.assertEqual('CHECK_DEFECT',finding['verdict'])

    def test_node_module_loader_path_type_error_is_a_test_setup_defect(self):
        from olcr_api.verification_diagnostics import project_test_failure_diagnosis
        failure={'name':'planner suite','detail':"failureType: 'hookFailed'\nerror: 'The \"path\" argument must be of type string. Received an instance of Object'\ncode: 'ERR_INVALID_ARG_TYPE'",
            'source_locations':[{'path':'tests/meal-planner.test.js','line':25},
                                {'path':'tests/meal-planner.test.js','line':25}]}
        finding=project_test_failure_diagnosis({'test_runs':[{'failures':[failure]}]},
            {'tests/meal-planner.test.js':'Module._load = function(request) { return original(request); }',
             'app.js':'function start() {}'})
        self.assertEqual('CHECK_DEFECT',finding['verdict'])
        self.assertEqual(['tests/meal-planner.test.js'],finding['repair_paths'])

    def test_node_module_loader_type_error_with_application_frame_still_requires_review(self):
        from olcr_api.verification_diagnostics import project_test_failure_diagnosis
        finding=project_test_failure_diagnosis({'test_runs':[{'failures':[{
            'name':'planner suite','detail':'The "path" argument must be of type string. Received an instance of Object',
            'source_locations':[{'path':'tests/meal-planner.test.js','line':25},
                                {'path':'app.js','line':81}]}]}]},
            {'tests/meal-planner.test.js':'require(impl);','app.js':'path.resolve(value);'})
        self.assertEqual('INCONCLUSIVE',finding['verdict'])

    def test_undefined_binding_in_test_only_frames_is_a_test_harness_defect(self):
        from olcr_api.verification_diagnostics import node_test_run, project_test_failure_diagnosis
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);test=root/'tests/meal-planner.test.js';test.parent.mkdir()
            source='function buildPlan() { save(); }\n'
            test.write_text(source)
            output=(f'# Subtest: recipe model: empty state\n'
                '# ReferenceError: save is not defined\n'
                f'#     at Object.buildPlan ({test}:171:7)\n'
                f'#     at TestContext.<anonymous> ({test}:182:22)\n'
                '# tests 1\n# pass 0\n# fail 1\n')
            run=node_test_run(['node','--test','tests/meal-planner.test.js'],output,1,root)
            finding=project_test_failure_diagnosis({'test_runs':[run]},
                {'tests/meal-planner.test.js':source,'app.js':'function save() {}'})
        self.assertEqual('CHECK_DEFECT',finding['verdict'])
        self.assertEqual(['tests/meal-planner.test.js'],finding['repair_paths'])

    def test_temporal_dead_zone_during_test_module_loading_is_a_test_harness_defect(self):
        from olcr_api.verification_diagnostics import node_test_run, project_test_failure_diagnosis
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);test=root/'tests/meal-planner.test.js';test.parent.mkdir()
            source='function loadApp() { const module = App; return module; }\nconst App = loadApp();\n'
            test.write_text(source)
            output=(f'# {test}:1\n# function loadApp() {{ const module = App; return module; }}\n'
                "# ReferenceError: Cannot access 'App' before initialization\n"
                f'#     at loadApp ({test}:1:31)\n'
                f'#     at Object.<anonymous> ({test}:2:13)\n'
                '# tests 0\n# pass 0\n# fail 1\n')
            run=node_test_run(['node','--test','tests/meal-planner.test.js'],output,1,root)
            finding=project_test_failure_diagnosis({'test_runs':[run]},
                {'tests/meal-planner.test.js':source,'app.js':'global.MEAL_PLANNER = App;'})
        self.assertEqual('Node.js module loading',run['failures'][0]['name'])
        self.assertEqual('CHECK_DEFECT',finding['verdict'])
        self.assertEqual(['tests/meal-planner.test.js'],finding['repair_paths'])

    def test_temporal_dead_zone_with_application_frame_still_requires_review(self):
        from olcr_api.verification_diagnostics import project_test_failure_diagnosis
        finding=project_test_failure_diagnosis({'test_runs':[{'failures':[{
            'name':'Node.js module loading',
            'detail':"ReferenceError: Cannot access 'App' before initialization",
            'source_locations':[{'path':'tests/meal-planner.test.js','line':47},
                                {'path':'app.js','line':507}]}]}]},
            {'tests/meal-planner.test.js':'const module = App;','app.js':'global.MEAL_PLANNER = App;'})
        self.assertEqual('INCONCLUSIVE',finding['verdict'])

    def test_undefined_binding_with_application_frame_is_not_test_harness_defect(self):
        from olcr_api.verification_diagnostics import project_test_failure_diagnosis
        finding=project_test_failure_diagnosis({'test_runs':[{'failures':[{
            'name':'recipe model: empty state','detail':'ReferenceError: save is not defined',
            'source_locations':[{'path':'app.js','line':171},
                                {'path':'tests/meal-planner.test.js','line':182}]}]}]},
            {'tests/meal-planner.test.js':'app.buildPlan()','app.js':'function buildPlan() { save(); }'})
        self.assertEqual('INCONCLUSIVE',finding['verdict'])

    def test_task_created_node_runtime_failure_limits_repair_to_throwing_source(self):
        from olcr_api.verification_diagnostics import project_test_runtime_source_diagnosis
        sources={
            'tests/logic.test.js':"test('first plan', () => buildMealPlan(...));",
            'src/logic.js':'function buildMealPlan() {\n  return recentByMeal["月|朝食"];\n}',
        }
        observed={'test_runs':[{'returncode':1,'failures':[{
            'name':'first plan','failure_type':'testCodeFailure',
            'detail':"error: 'Cannot read properties of undefined'\nname: 'TypeError'",
            'source_locations':[{'path':'tests/logic.test.js','line':1},
                                {'path':'src/logic.js','line':2,'column':10},
                                {'path':'tests/logic.test.js','line':1}],
        }]}]}
        manifest=[{'path':'tests/logic.test.js','action':'create'},
                  {'path':'src/logic.js','action':'create'}]

        finding=project_test_runtime_source_diagnosis(observed,sources,manifest)

        self.assertEqual('SOURCE_DEFECT',finding['verdict'])
        self.assertEqual(['src/logic.js'],finding['repair_paths'])
        self.assertEqual('return recentByMeal["月|朝食"];',finding['source_fragment'])

    def test_task_created_runtime_failure_is_repairable_amid_other_failures(self):
        from olcr_api.verification_diagnostics import project_test_runtime_source_diagnosis
        sources={
            'tests/logic.test.js': "assert.deepEqual(Object.keys(emptyPlan()), DAYS);\n"
                "generatePlan(recipes);\nplan['月']['朝食'];\nplan['月']['昼食'];",
            'src/logic.js': "const DAYS = ['月','火','水','木','金','土','日'];\n"
                "function emptyPlan() { return Object.fromEntries(DAYS.map((_, day) => [day, {}])); }\n"
                "function generatePlan() { plan[day][meal] = selected.id; }",
        }
        failures=[
            {'name':'empty plan uses weekday names','failure_type':'testCodeFailure',
             'detail':'AssertionError [ERR_ASSERTION]: Expected values to be strictly deep-equal',
             'source_locations':[{'path':'tests/logic.test.js','line':1}]},
            {'name':'generates a complete plan','failure_type':'testCodeFailure',
             'detail':"error: \"Cannot set properties of undefined (setting '朝食')\"\nname: 'TypeError'",
             'source_locations':[{'path':'tests/logic.test.js','line':2},
                                 {'path':'src/logic.js','line':3,'column':30}]},
            {'name':'empty matching set remains empty','failure_type':'testCodeFailure',
             'detail':"error: \"Cannot read properties of undefined (reading '朝食')\"\nname: 'TypeError'",
             'source_locations':[{'path':'tests/logic.test.js','line':3}]},
            {'name':'shopping list accepts an empty plan','failure_type':'testCodeFailure',
             'detail':"error: \"Cannot set properties of undefined (setting '朝食')\"\nname: 'TypeError'",
             'source_locations':[{'path':'tests/logic.test.js','line':4}]},
        ]
        observed={'test_runs':[{'returncode':1,'failures':failures}]}
        manifest=[{'path':'tests/logic.test.js','action':'create'},
                  {'path':'src/logic.js','action':'create'}]

        finding=project_test_runtime_source_diagnosis(observed,sources,manifest)

        self.assertEqual('SOURCE_DEFECT',finding['verdict'])
        self.assertEqual(['src/logic.js'],finding['repair_paths'])
        self.assertEqual({'path':'src/logic.js','line':3},finding['source_location'])
        self.assertEqual(4,len(finding['failure_cases']))

        from olcr_api import app as api
        from unittest.mock import patch
        with patch.object(api,'_model_text',side_effect=AssertionError('A grounded app runtime frame needs no model review')):
            routed=api._review_project_test_failures('meal-planner-cluster',
                'Assign meals by weekday and meal type.',{'VERIFY:1':'The app generates the weekly plan.'},
                sources,observed,file_manifest=manifest)
        self.assertEqual('SOURCE_DEFECT',routed['verdict'])
        self.assertEqual(['src/logic.js'],routed['repair_paths'])

    def test_multiple_distinct_application_runtime_roots_remain_inconclusive(self):
        from olcr_api.verification_diagnostics import project_test_runtime_source_diagnosis
        sources={'tests/logic.test.js':"test('one', first); test('two', second);",
                 'src/logic.js':'function first() { throw new TypeError("one"); }\n'
                                'function second() { throw new TypeError("two"); }'}
        observed={'test_runs':[{'returncode':1,'failures':[
            {'name':'one','failure_type':'testCodeFailure','detail':"TypeError: one\\nname: 'TypeError'",
             'source_locations':[{'path':'tests/logic.test.js','line':1},{'path':'src/logic.js','line':1}]},
            {'name':'two','failure_type':'testCodeFailure','detail':"TypeError: two\\nname: 'TypeError'",
             'source_locations':[{'path':'tests/logic.test.js','line':1},{'path':'src/logic.js','line':2}]},
        ]}]}
        manifest=[{'path':'tests/logic.test.js','action':'create'},
                  {'path':'src/logic.js','action':'create'}]

        finding=project_test_runtime_source_diagnosis(observed,sources,manifest)

        self.assertEqual('INCONCLUSIVE',finding['verdict'])

    def test_runtime_repair_fallback_does_not_rewrite_existing_tests_or_assertions(self):
        from olcr_api.verification_diagnostics import project_test_runtime_source_diagnosis
        sources={"tests/app.test.js":"test('app',()=>app());",'src/app.js':'function app(){ throw new TypeError("bad"); }'}
        failure={'name':'app','failure_type':'testCodeFailure',
            'detail':"error: 'bad'\nname: 'TypeError'",
            'source_locations':[{'path':'tests/app.test.js','line':1},{'path':'src/app.js','line':1}]}
        observed={'test_runs':[{'returncode':1,'failures':[failure]}]}

        existing_test=project_test_runtime_source_diagnosis(observed,sources,[
            {'path':'tests/app.test.js','action':'modify'},{'path':'src/app.js','action':'modify'}])
        assertion=project_test_runtime_source_diagnosis({'test_runs':[{'returncode':1,'failures':[{
            **failure,'detail':"error: 'expected mismatch'\nname: 'AssertionError'"}]}]},sources,[
            {'path':'tests/app.test.js','action':'create'},{'path':'src/app.js','action':'create'}])

        self.assertEqual('INCONCLUSIVE',existing_test['verdict'])
        self.assertEqual('INCONCLUSIVE',assertion['verdict'])

    def test_application_syntax_error_imported_by_test_is_not_a_harness_defect(self):
        from olcr_api.verification_diagnostics import project_test_failure_diagnosis
        finding=project_test_failure_diagnosis({'test_runs':[{'failures':[{
            'detail':'SyntaxError: Missing initializer',
            'source_locations':[{'path':'app.js','line':1},{'path':'app.test.js','line':3}]}]}]},
            {'app.js':'const broken;','app.test.js':"require('./app.js')"})
        self.assertEqual('INCONCLUSIVE',finding['verdict'])

    def test_error_kind_and_location_from_unrelated_failures_are_not_combined(self):
        from olcr_api.verification_diagnostics import project_test_failure_diagnosis
        finding=project_test_failure_diagnosis({'test_runs':[{'failures':[
            {'name':'Node.js module loading','detail':'SyntaxError: Missing initializer','source_locations':[{'path':'app.js'}]},
            {'name':'assertion','detail':'AssertionError: wrong value','source_locations':[{'path':'app.test.js'}]}]}]},
            {'app.js':'const broken;','app.test.js':'assert.equal(actual,expected);'})
        self.assertEqual('INCONCLUSIVE',finding['verdict'])

    def test_causal_app_frame_outside_declared_test_targets_still_requires_review(self):
        from olcr_api.verification_diagnostics import project_test_failure_diagnosis
        finding=project_test_failure_diagnosis({'test_runs':[{'failures':[{
            'detail':"error: 'exports is not defined in ES module scope'",
            'source_locations':[{'path':'app.test.js'},{'path':'app.js'}]}]}]},
            {'app.test.js':"require('./app.js');"})
        self.assertEqual('INCONCLUSIVE',finding['verdict'])

    def test_node_tap_keeps_leaf_failures_separate_from_suite_rollups(self):
        from olcr_api.verification_diagnostics import node_test_run
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            test=root/'tests/item.test.js';test.parent.mkdir()
            app=root/'src/planner.js';app.parent.mkdir()
            output=(
                '# Subtest: inventory\n'
                '    # Subtest: rejects an unnamed item\n'
                '    not ok 1 - rejects an unnamed item\n'
                '      ---\n'
                f"      location: '{test}:8:3'\n"
                "      failureType: 'testCodeFailure'\n"
                '      error: |-\n'
                '        AssertionError: expected false to be true\n'
                '        stack: |-\n'
                f'          at TestContext.<anonymous> ({test}:10:5)\n'
                '      ...\n'
                '    # Subtest: assigns a schedule slot\n'
                '    not ok 2 - assigns a schedule slot\n'
                '      ---\n'
                f"      location: '{test}:20:3'\n"
                "      failureType: 'testCodeFailure'\n"
                '      error: "Cannot read properties of undefined"\n'
                '      stack: |-\n'
                f'        at assignSlot (file://{app}:41:9)\n'
                f'        at TestContext.<anonymous> ({test}:22:5)\n'
                '      ...\n'
                '    1..2\n'
                'not ok 1 - inventory\n'
                '  ---\n'
                "  failureType: 'subtestsFailed'\n"
                "  error: '2 subtests failed'\n"
                '  ...\n'
                '# tests 2\n# pass 0\n# fail 2\n')
            run=node_test_run(['node','--test','tests/item.test.js'],output,1,root)
        self.assertEqual(2,len(run['failures']))
        self.assertEqual(['rejects an unnamed item','assigns a schedule slot'],
                         [failure['name'] for failure in run['failures']])
        self.assertNotIn('assigns a schedule slot',run['failures'][0]['detail'])
        self.assertEqual({'tests/item.test.js'},
                         {location['path'] for location in run['failures'][0]['source_locations']})
        self.assertEqual({'src/planner.js','tests/item.test.js'},
                         {location['path'] for location in run['failures'][1]['source_locations']})

    def test_project_test_review_scopes_check_repairs_to_exact_failing_test_quote(self):
        import json
        from unittest.mock import patch
        import olcr_api.app as api
        sources={'index.html':'<main></main>',
            'src/item.js':'function isValid(item) { return Boolean(item.name); }',
            'tests/item.test.js':'assert.ok(blankItem.isValid());\n'}
        finding_input={'name':'accepts a blank item','detail':'AssertionError: expected valid item',
            'source_locations':[{'path':'tests/item.test.js','line':1}]}
        response=json.dumps({'verdict':'CHECK_DEFECT','reason':'The assertion contradicts the stated empty-item rule.',
            'requirement':'A blank item is not a saved record.','source_fragment':'',
            'test_fragment':'assert.ok(blankItem.isValid());'})
        with patch.object(api,'_model_text',return_value=response) as model:
            finding=api._review_project_test_failure('item-review',
                'A blank item is not a saved record.',{'VERIFY:1':'Blank items are invalid.'},sources,finding_input)
        self.assertEqual('CHECK_DEFECT',finding['verdict'])
        self.assertEqual(['tests/item.test.js'],finding['repair_paths'])
        self.assertEqual('assert.ok(blankItem.isValid());',finding['test_fragment'])
        self.assertNotIn('src/item.js',finding['repair_paths'])
        self.assertIn('accepts a blank item',model.call_args.args[4][1]['content'])

    def test_quote_from_an_unexecuted_neighbor_cannot_authorize_assertion_repair(self):
        from olcr_api.app import _project_test_quote_paths
        sources={'tests/value.test.js':"describe('value',()=>{\n  assert.equal(item,3);\n});"}
        failure={'source_locations':[{'path':'tests/value.test.js','line':1}]}
        self.assertEqual([],_project_test_quote_paths('assert.equal(item,3);',sources,test_only=True,failure=failure))
        self.assertEqual(list(sources),_project_test_quote_paths("describe('value'",sources,test_only=True,failure=failure))

    def test_project_test_review_advances_to_later_failure_without_combining_ownership(self):
        import json
        import tempfile
        from unittest.mock import patch
        import olcr_api.app as api
        from olcr_api.verification_diagnostics import node_test_run
        original='Calculate each invoice total from the listed item prices.'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            test_path=root/'tests/invoice.test.js';test_path.parent.mkdir()
            test_path.write_text('assert.equal(invoiceTotal(items), expected);\n')
            source_path=root/'src/invoice.js';source_path.parent.mkdir()
            source_path.write_text('total += item.price + 1;\n')
            output=(
                '# Subtest: invoice\n'
                '    # Subtest: checks the label\n'
                '    not ok 1 - checks the label\n'
                '      ---\n'
                f"      location: '{test_path}:1:3'\n"
                "      failureType: 'testCodeFailure'\n"
                '      error: "AssertionError: label mismatch"\n'
                '      ...\n'
                '    # Subtest: calculates invoice total\n'
                '    not ok 2 - calculates invoice total\n'
                '      ---\n'
                f"      location: '{test_path}:1:3'\n"
                "      failureType: 'testCodeFailure'\n"
                '      error: "AssertionError: total mismatch"\n'
                '      stack: |-\n'
                f'        at total (file://{source_path}:1:1)\n'
                f'        at TestContext.<anonymous> ({test_path}:1:3)\n'
                '      ...\n'
                '    1..2\n'
                'not ok 1 - invoice\n'
                '  ---\n'
                "  failureType: 'subtestsFailed'\n"
                "  error: '2 subtests failed'\n"
                '  ...\n'
                '# tests 2\n# pass 0\n# fail 2\n')
            run=node_test_run(['node','--test','tests/invoice.test.js'],output,1,root)
            sources={'tests/invoice.test.js':test_path.read_text(),
                     'src/invoice.js':source_path.read_text()}
            observed={'passed':False,'output':output,'test_runs':[run]}
        responses=[
            json.dumps({'verdict':'INCONCLUSIVE','reason':'The first failure does not establish ownership.',
                'requirement':'','source_fragment':'','test_fragment':''}),
            json.dumps({'verdict':'SOURCE_DEFECT','reason':'The implementation adds an amount not present in the request.',
                'requirement':original,'source_fragment':'total += item.price + 1;','test_fragment':''})]
        with patch.object(api,'_model_text',side_effect=responses) as model:
            finding=api._review_project_test_failures('invoice-review',original,
                {'VERIFY:1':original},sources,observed)
        self.assertEqual('SOURCE_DEFECT',finding['verdict'])
        self.assertEqual(['src/invoice.js'],finding['repair_paths'])
        self.assertEqual('calculates invoice total',finding['failure_case']['name'])
        self.assertEqual(2,model.call_count)
        self.assertNotIn('calculates invoice total',model.call_args_list[0].args[4][1]['content'])

    def test_created_node_test_runtime_failure_routes_to_source_only_retry_without_model_loop(self):
        import olcr_api.app as api
        from unittest.mock import patch
        sources={"tests/logic.test.js":"test('first plan', () => buildMealPlan(...));",
                 'src/logic.js':'function buildMealPlan() {\n  return recentByMeal["月|朝食"];\n}'}
        failure={'name':'first plan','failure_type':'testCodeFailure',
            'detail':"error: 'Cannot read properties of undefined'\nname: 'TypeError'",
            'source_locations':[{'path':'tests/logic.test.js','line':1},
                                {'path':'src/logic.js','line':2,'column':10},
                                {'path':'tests/logic.test.js','line':1}]}
        observed={'test_runs':[{'command':['node','--test','tests/logic.test.js'],
            'returncode':1,'failures':[failure]}]}
        manifest=[{'path':'tests/logic.test.js','action':'create'},
                  {'path':'src/logic.js','action':'create'}]

        with patch.object(api,'_model_text') as model:
            finding=api._review_project_test_failures('generated-test-runtime',
                'Handle an empty previous meal-plan state.',{'DONE:1':'Handle empty meal-plan state.'},
                sources,observed,file_manifest=manifest)

        self.assertEqual('SOURCE_DEFECT',finding['verdict'])
        self.assertEqual(['src/logic.js'],finding['repair_paths'])
        model.assert_not_called()

    def test_project_test_review_stops_at_fixed_case_budget(self):
        import json
        from unittest.mock import patch
        import olcr_api.app as api
        original='The requested calculation uses each listed price.'
        failures=[{'name':f'case {index}','detail':'assertion failed',
            'source_locations':[{'path':'tests/pricing.test.js','line':index+1}]}
            for index in range(5)]
        sources={'tests/pricing.test.js':'assert.equal(actual, expected);',
                 'src/pricing.js':'return items.reduce((sum,item)=>sum+item.price,0);'}
        inconclusive=json.dumps({'verdict':'INCONCLUSIVE','reason':'No ownership evidence.',
            'requirement':'','source_fragment':'','test_fragment':''})
        with patch.object(api,'_model_text',return_value=inconclusive) as model:
            finding=api._review_project_test_failures('pricing-review',original,
                {'VERIFY:1':original},sources,{'test_runs':[{'failures':failures}]})
        self.assertEqual('INCONCLUSIVE',finding['verdict'])
        self.assertEqual(api.VERIFICATION_PROJECT_TEST_CASE_LIMIT,model.call_count)

    def test_project_test_review_rejects_paraphrased_or_nonexistent_quotes(self):
        import json
        from unittest.mock import patch
        import olcr_api.app as api
        original='An empty draft is not a saved item.'
        sources={'tests/item.test.js':'assert.ok(blankItem.isValid());',
                 'src/item.js':'return Boolean(item.name);'}
        failure={'name':'empty draft','detail':'AssertionError: false',
            'source_locations':[{'path':'tests/item.test.js','line':1}]}
        responses=[
            {'verdict':'CHECK_DEFECT','reason':'A paraphrased rule.','requirement':'Blank records are invalid.',
             'source_fragment':'','test_fragment':'assert.ok(blankItem.isValid());'},
            {'verdict':'CHECK_DEFECT','reason':'An invented assertion.','requirement':original,
             'source_fragment':'','test_fragment':'assert.ok(true);'},
            {'verdict':'SOURCE_DEFECT','reason':'An invented implementation line.','requirement':original,
             'source_fragment':'return true;','test_fragment':''}]
        for response in responses:
            with self.subTest(verdict=response['verdict']), patch.object(api,'_model_text',
                    return_value=json.dumps(response)):
                finding=api._review_project_test_failure('strict-quotes',original,
                    {'VERIFY:1':original},sources,failure)
                self.assertEqual('INCONCLUSIVE',finding['verdict'])
                self.assertFalse(finding.get('repair_paths'))

    def test_project_test_review_keeps_unparsed_failure_from_another_runner(self):
        from olcr_api.verification_diagnostics import project_test_review_cases
        observed={'test_runs':[
            {'command':['node','--test'],'returncode':1,'failures':[
                {'name':'assertion one','detail':'AssertionError: mismatch'}]},
            {'command':['python3','-m','unittest'],'returncode':1,'failures':[],
             'output':'FAIL: test_two\nAssertionError: mismatch'}]}
        cases=project_test_review_cases(observed)
        self.assertEqual(['assertion one','Unparsed project-test failure'],
                         [case['name'] for case in cases])
        self.assertIn('test_two',cases[1]['detail'])
