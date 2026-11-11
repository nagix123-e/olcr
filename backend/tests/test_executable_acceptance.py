import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from olcr_api.coding_verification import (
    normalize_static_verification, validate_verification, execution_command,
)
from olcr_api.coding_tasks import evaluate_phase


class ExecutableAcceptanceTests(unittest.TestCase):
    def test_native_es_modules_load_with_browser_compatible_resource_types(self):
        (self.root/'index.html').write_text('<link rel="stylesheet" href="styles.css">'
            '<output id="result"></output><script type="module" src="app.mjs"></script>')
        (self.root/'styles.css').write_text('#result{color:rgb(10,20,30)}')
        (self.root/'data.json').write_text('{"label":"Native module"}')
        (self.root/'value.MJS').write_text('export const value=7;')
        (self.root/'app.mjs').write_text("import {value} from './value.MJS';"
            "const response=await fetch('./data.json');const data=await response.json();"
            "document.querySelector('#result').textContent=data.label+' '+value;")
        result=self.execute([{'ids':['DONE:1'],'kind':'BROWSER','code':
            "await expect(page.locator('#result')).toHaveText('Native module 7');"
            "assert.equal(await page.locator('#result').evaluate(el=>getComputedStyle(el).color),'rgb(10, 20, 30)');"}])
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_vite_preview_keeps_declared_node_tests_when_vitest_is_installed(self):
        modules=Path(__file__).resolve().parents[2]/'frontend/node_modules'
        if not (modules/'vitest/vitest.mjs').is_file():
            self.skipTest('Local Vitest installation is unavailable')
        (self.root/'node_modules').symlink_to(modules,target_is_directory=True)
        (self.root/'logic.test.mjs').write_text("import {test} from 'node:test';"
            "import assert from 'node:assert/strict';test('real native assertion',()=>assert.equal(2+2,4));")
        for package in [{'scripts':{'test':'node --test'}},{}]:
            with self.subTest(package=package):
                (self.root/'package.json').write_text(json.dumps(package))
                result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':
                    'const result=await runTests();assert.equal(result.tests,1);'}],_olcr_vite_preview='.olcr-preview')
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
                self.assertEqual('--test',payload['test_runs'][0]['command'][1])

    def test_vite_preview_preserves_the_declared_vitest_runner_and_real_failure(self):
        modules=Path(__file__).resolve().parents[2]/'frontend/node_modules'
        if not (modules/'vitest/vitest.mjs').is_file():
            self.skipTest('Local Vitest installation is unavailable')
        (self.root/'node_modules').symlink_to(modules,target_is_directory=True)
        (self.root/'package.json').write_text(json.dumps({'scripts':{'test':'vitest run'}}))
        (self.root/'logic.test.mjs').write_text("import {test} from 'node:test';"
            "import assert from 'node:assert/strict';test('native assertion',()=>assert.equal(2+2,4));")
        result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':'await runTests();assert.ok(true);'}],
            _olcr_vite_preview='.olcr-preview')
        self.assertNotEqual(0,result.returncode,result.stdout)
        payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
        self.assertTrue(payload['test_runs'][0]['command'][1].endswith('/vitest/vitest.mjs'))
        self.assertEqual([],payload['passed_ids'])

    def test_native_test_acceptance_requires_an_executed_pass_and_retains_skip_counts(self):
        # Node counts an empty file container as a passing test. It cannot
        # provide the executed pass missing from an otherwise skipped suite.
        (self.root/'empty.test.mjs').write_text('')
        prefix="import {test,describe} from 'node:test';import assert from 'node:assert/strict';"
        skipped="test.skip('skipped assertion',()=>assert.fail('must not run'));"
        todo="test.todo('future assertion');"
        for body,should_pass in [
                (skipped,False),(todo,False),("describe('empty suite',()=>{});",False),
                (skipped+todo+"test('real pass',()=>assert.equal(2+2,4));",True)]:
            with self.subTest(body=body):
                (self.root/'logic.test.mjs').write_text(prefix+body)
                result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':
                    'const result=await runTests();assert.ok(result.tests>0);assert.equal(result.returncode,0);'}],
                    requires_tests=True)
                self.assertEqual(should_pass,result.returncode==0,result.stdout+result.stderr)
                payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
                self.assertEqual(should_pass,'VERIFY:1' in payload['passed_ids'])
                if should_pass:
                    self.assertEqual(1,payload['test_runs'][0]['passed'])
                    self.assertEqual(1,payload['test_runs'][0]['skipped'])
                    self.assertEqual(1,payload['test_runs'][0]['todo'])

    def test_vitest_acceptance_requires_an_executed_pass_and_retains_skip_counts(self):
        modules=Path(__file__).resolve().parents[2]/'frontend/node_modules'
        if not (modules/'vitest/vitest.mjs').is_file():
            self.skipTest('Local Vitest installation is unavailable')
        (self.root/'node_modules').symlink_to(modules,target_is_directory=True)
        (self.root/'package.json').write_text(json.dumps({'scripts':{'test':'vitest run'}}))
        prefix="import {test,describe,expect} from 'vitest';"
        skipped="test.skip('skipped assertion',()=>expect(false).toBe(true));"
        todo="test.todo('future assertion');"
        for body,should_pass in [
                (skipped,False),(todo,False),("describe('empty suite',()=>{});",False),
                (skipped+todo+"test('real pass',()=>expect(2+2).toBe(4));",True)]:
            with self.subTest(body=body):
                (self.root/'logic.test.mjs').write_text(prefix+body)
                result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':
                    'const result=await runTests();assert.ok(result.tests>0);assert.equal(result.returncode,0);'}],
                    requires_tests=True,_olcr_vite_preview='.olcr-preview')
                self.assertEqual(should_pass,result.returncode==0,result.stdout+result.stderr)
                payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
                self.assertEqual(should_pass,'VERIFY:1' in payload['passed_ids'])
                if should_pass:
                    self.assertEqual(1,payload['test_runs'][0]['passed'])
                    self.assertEqual(1,payload['test_runs'][0]['skipped'])
                    self.assertEqual(1,payload['test_runs'][0]['todo'])

    def test_storage_scenario_keeps_authored_required_number_and_select_setup(self):
        from olcr_api.coding_verification import compile_browser_observations
        html=('<button id="add-recipe">Add recipe</button><form id="recipe-form" hidden>'
            '<input id="recipe-name" required><input id="recipe-cost" type="number" required min="0">'
            '<select id="recipe-category" required size="2"><option value="" selected>Choose</option>'
            '<option value="main">Main</option></select><button type="submit">Save</button></form>'
            '<ul id="recipe-list"><li class="empty">No recipes</li></ul><div id="toast"></div>'
            '<script src="app.js"></script>')
        script=("function showToast(text){document.querySelector('#toast').textContent=text;}"
            "document.querySelector('#add-recipe').onclick=()=>document.querySelector('#recipe-form').hidden=false;"
            "document.querySelector('#recipe-form').onsubmit=event=>{event.preventDefault();"
            "try{localStorage.setItem('recipes','[]');}catch(error){showToast('Save error');}};")
        (self.root/'index.html').write_text(html);(self.root/'app.js').write_text(script)
        value={'checks':[
            {'ids':['VERIFY:1'],'kind':'BROWSER','scenario':'empty_data','actions':[],
             'observations':[{'selector':'#recipe-list','measure':'text','operator':'includes','expected':'No recipes'}]},
            {'ids':['VERIFY:1'],'kind':'BROWSER','scenario':'storage_error','actions':[
                {'action':'click','selector':'#add-recipe','value':None},
                {'action':'fill','selector':'#recipe-name','value':'Real recipe'},
                {'action':'fill','selector':'#recipe-cost','value':'1'},
                {'action':'click','selector':'#recipe-category option[value="main"]','value':None},
                {'action':'click','selector':'#recipe-form button[type="submit"]','value':None},
                {'action':'storage_failure','selector':'localStorage','value':True}],
             'observations':[{'selector':'#toast','measure':'text','operator':'includes','expected':'Save error'},
                {'selector':'#recipe-category','measure':'value','operator':'eq','expected':'main'}]}]}
        criteria={'VERIFY:1':'In the browser, handle empty data and save errors.'}
        compiled=compile_browser_observations(value,criteria,{'index.html':html,'app.js':script})
        result=self.execute(compiled['checks'])
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_literal_native_select_and_reload_actions_prove_restored_ui_state(self):
        from olcr_api.coding_verification import compile_browser_observations, validate_verification
        (self.root/'index.html').write_text('''<input id="dish" value="">
          <select id="slot"><option value="">Choose</option><option value="monday-dinner">Monday dinner</option></select>
          <button id="assign" type="button">Assign</button><output id="assigned">Unassigned</output>
          <script src="app.js"></script>''')
        (self.root/'app.js').write_text("const slot=document.querySelector('#slot');const dish=document.querySelector('#dish');const assigned=document.querySelector('#assigned');function render(){assigned.textContent=localStorage.getItem('assigned')||'Unassigned';}document.querySelector('#assign').addEventListener('click',()=>{localStorage.setItem('assigned',dish.value+':'+slot.value);render();});render();")
        criteria={'VERIFY:1':'In the browser, manually assign a dish to a weekday meal slot and verify it is restored after reload.'}
        value={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','actions':[
            {'action':'fill','selector':'#dish','value':'Miso soup'},
            {'action':'select','selector':'#slot','value':'monday-dinner'},
            {'action':'click','selector':'#assign','value':None},
            {'action':'reload','selector':'page','value':None}],
            'observations':[{'selector':'#assigned','measure':'text','operator':'eq','expected':'Miso soup:monday-dinner'}]}]}
        compiled=compile_browser_observations(value,criteria,{'index.html':(self.root/'index.html').read_text(),
            'app.js':(self.root/'app.js').read_text()})
        self.assertEqual([],validate_verification(compiled,criteria,self.root))
        self.assertIn('.selectOption(',compiled['checks'][0]['code'])
        self.assertIn('await page.reload()',compiled['checks'][0]['code'])
        result=self.execute(compiled['checks'])
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_external_script_parse_error_has_real_source_location_even_without_a_js_stack(self):
        (self.root/'index.html').write_text('<button id="start">Start</button><script src="app.js"></script>')
        (self.root/'app.js').write_text("const good=1;\nconst broken=</div>;\n")
        result=self.execute([{'ids':['DONE:1'],'kind':'BROWSER','code':"assert.equal(await page.locator('#start').count(),1);"}])
        self.assertNotEqual(0,result.returncode)
        payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
        failure=payload['failures'][0]
        self.assertTrue(failure['project_source_error'],failure)
        locations=[loc for error in failure['browser_errors'] for loc in error['source_locations']]
        self.assertTrue(any(loc['path']=='app.js' and loc['line']==2 and '</div>' in loc['source_line'] for loc in locations),locations)
        self.assertFalse(failure.get('contract_error'))

    def test_inline_script_parse_error_is_attributed_to_html_not_verifier_code(self):
        (self.root/'index.html').write_text('<button>Start</button>\n<script>\nconst broken=</div>;\n</script>')
        result=self.execute([{'ids':['DONE:1'],'kind':'BROWSER','code':"assert.equal(await page.locator('button').count(),1);"}])
        payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
        failure=payload['failures'][0]
        self.assertTrue(failure['project_source_error'],failure)
        self.assertTrue(any(loc['path']=='index.html' and loc['line']==3 for error in failure['browser_errors'] for loc in error['source_locations']))

    def test_owned_responsive_checks_measure_two_viewports_and_do_not_click_the_app(self):
        from olcr_api.coding_verification import standard_verification_checks
        (self.root/'styles.css').write_text('body{margin:0}main{max-width:100%}[hidden]{display:none}')
        (self.root/'index.html').write_text('<link rel="stylesheet" href="styles.css"><main><button id="start" onclick="document.querySelector(\'output\').textContent=\'changed\'">Start</button><output>unchanged</output></main><dialog hidden><input name="name"></dialog>')
        criteria={'VERIFY:1':'styles.css provides a responsive layout for mobile and desktop.'}
        checks=standard_verification_checks(criteria)
        self.assertEqual(2,len(checks))
        for check in checks:check['code']+="assert.equal(await page.locator('output').textContent(),'unchanged');"
        result=self.execute(checks,collect_observations=True)
        self.assertEqual(0,result.returncode,result.stdout)
        data=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
        facts=[value for item in data['browser_observations'] for value in item['layout_observations']]
        self.assertEqual([375,1280],[item['viewport']['width'] for item in facts])
        self.assertTrue(all(item['stylesheet_loaded'] and item['controls'][0]['actionable'] for item in facts))

    def test_owned_layout_rejects_overflow_unloaded_css_and_obstructed_controls(self):
        from olcr_api.coding_verification import standard_verification_checks
        checks=standard_verification_checks({'VERIFY:1':'styles.css provides a responsive layout.'})
        cases=[('main{width:1000px}','<main><button>Start</button></main>','styles.css','overflows'),
            ('body{margin:0}','<main><button>Start</button></main>','other.css','stylesheet was not loaded'),
            ('body{margin:0}#overlay{position:fixed;inset:0;background:white;z-index:5;display:block}',
             '<main><button>Start</button></main><div id="overlay" hidden></div>','styles.css','obstructed')]
        for css,body,linked,expected in cases:
            with self.subTest(expected=expected):
                (self.root/'styles.css').write_text(css);(self.root/'other.css').write_text('body{margin:0}')
                (self.root/'index.html').write_text('<link rel="stylesheet" href="'+linked+'">'+body)
                result=self.execute(checks)
                self.assertNotEqual(0,result.returncode)
                self.assertIn(expected,result.stdout)

    def test_layout_compilation_does_not_discard_the_second_control_or_region_observation(self):
        from olcr_api.coding_verification import compile_browser_observations
        observations=[{'selector':'#first','measure':'visible','operator':'eq','expected':True},
            {'selector':'#second','measure':'visible','operator':'eq','expected':True},
            {'selector':'#panel','measure':'overflow_x','operator':'eq','expected':False}]
        value={'checks':[{'ids':['DONE:1'],'kind':'BROWSER','actions':[{'action':'viewport','selector':'page','value':viewport}],
            'observations':observations} for viewport in [{'width':375,'height':812},{'width':1280,'height':800}]]}
        result=compile_browser_observations(value,{'DONE:1':'Responsive layout for mobile and desktop.'})
        self.assertTrue(all('#second' in check['code'] and '#panel' in check['code'] for check in result['checks']))
        self.assertIn('el.scrollWidth>el.clientWidth',result['checks'][0]['code'])

    def test_specific_layout_requirements_are_not_replaced_with_generic_geometry(self):
        from olcr_api.coding_verification import standard_verification_checks,layout_browser_criteria
        for text in ['Responsive layout uses a hamburger menu on mobile and a sidebar on desktop.',
                     'Responsive layout has two columns on desktop and one on mobile.',
                     'スマートフォンとデスクトップで使いやすいレスポンシブレイアウトで、文字サイズを切り替える。']:
            self.assertFalse(layout_browser_criteria({'DONE:1':text}))
            self.assertEqual([],standard_verification_checks({'DONE:1':text}))

    def test_owned_layout_does_not_treat_legitimately_disabled_controls_as_broken(self):
        from olcr_api.coding_verification import standard_verification_checks
        (self.root/'index.html').write_text('<main><fieldset disabled><button>Disabled</button></fieldset><button aria-disabled="true">Not ready</button><button id="ready">Ready</button></main>')
        result=self.execute(standard_verification_checks({'DONE:1':'Responsive layout for mobile and desktop.'}),collect_observations=True)
        self.assertEqual(0,result.returncode,result.stdout)
        data=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
        self.assertTrue(all([c['id'] for c in item['layout_observations'][0]['controls']]==['ready'] for item in data['browser_observations']))

    def test_anonymous_verifier_evaluation_error_does_not_gain_application_source_ownership(self):
        result=self.execute([{'ids':['DONE:1'],'kind':'BROWSER','code':"await page.evaluate(()=>{throw new SyntaxError('check eval failure')});"}])
        failure=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])['failures'][0]
        self.assertFalse(failure.get('project_source_error'))
        self.assertIn('check eval failure',failure['error'])

    def test_typed_workflow_can_add_twelve_items_without_losing_actions_and_stays_bounded(self):
        from olcr_api.coding_verification import compile_browser_observations, MAX_OBSERVATION_ACTIONS
        (self.root/'index.html').write_text('<input id="entry"><button id="add">Add</button><ul id="items"></ul><script src="app.js"></script>')
        (self.root/'app.js').write_text("document.querySelector('#add').onclick=()=>{const li=document.createElement('li');li.textContent=document.querySelector('#entry').value;document.querySelector('#items').append(li);};")
        actions=[action for number in range(12)for action in [
            {'action':'fill','selector':'#entry','value':f'item {number}'},
            {'action':'click','selector':'#add','value':None}]]
        value={'checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','actions':actions,
            'observations':[{'selector':'#items li','measure':'count','operator':'eq','expected':12},
                {'selector':'#items li','measure':'text','operator':'eq','expected':'\n'.join(f'item {n}'for n in range(12))}]}]}
        result=self.execute(compile_browser_observations(value)['checks'])
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        value['checks'][0]['actions']=[actions[0]]*(MAX_OBSERVATION_ACTIONS+1)
        with self.assertRaisesRegex(ValueError,'at most'):
            compile_browser_observations(value)

    def test_typed_check_cannot_require_both_single_line_tag_and_preserved_newlines(self):
        from olcr_api.coding_verification import compile_browser_observations
        value={'checks':[{'ids':['VERIFY:1'],'kind':'BROWSER',
            'actions':[{'action':'fill','selector':'#editor','value':'alpha\nbeta'}],
            'observations':[{'selector':'#editor','measure':'tag','operator':'eq','expected':'input'},
                {'selector':'#editor','measure':'value','operator':'eq','expected':'alpha\nbeta'}]}]}
        with self.assertRaisesRegex(ValueError,'Contradictory check'):
            compile_browser_observations(value,{'VERIFY:1':'Preserve each supplied line'})

    def test_typed_text_observation_reads_a_real_collection_and_rejects_missing_targets(self):
        from olcr_api.coding_verification import compile_browser_observations
        (self.root/'index.html').write_text('<ul id="items"><li>alpha</li><li>beta</li></ul>')
        value={'checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','actions':[],
            'observations':[{'selector':'#items li','measure':'text','operator':'eq','expected':'alpha\nbeta'}]}]}
        compiled=compile_browser_observations(value,{'VERIFY:1':'Read displayed items'})
        self.assertEqual(0,self.execute(compiled['checks']).returncode)
        value['checks'][0]['observations'][0].update(selector='.missing',expected='')
        self.assertNotEqual(0,self.execute(compile_browser_observations(value,{'VERIFY:1':'Read displayed items'})['checks']).returncode)

    def test_owned_single_criterion_gate_needs_a_matching_actual_command_without_duplicate_prose(self):
        phase={'id':'final','host_verification':True,'execution_mode':'VERIFICATION_ONLY',
            'done':['Browser loads'],'verify':[],'verification_command':['node','owned-check.cjs'],
            'verification_targets':['index.html']}
        report={'status':'PASS','criteria_evidence':{'Browser loads':True},'typed_execution_summary':{'operations':[
            {'tool':'verification_command','status':'success','input':{'command':phase['verification_command'],'write_scope':'NONE'},
             'output':{'returncode':0,'read_only_targets':['index.html'],'changed_targets':[],'zero_tests':False,
                       'target_sha256':{'index.html':hashlib.sha256((self.root/'index.html').read_bytes()).hexdigest()}}}]}}
        self.assertTrue(evaluate_phase(phase,report,workspace_root=str(self.root))['phase_complete'])
        invalid=copy.deepcopy(report)
        invalid['typed_execution_summary']['operations'][0]['input']['command']=['node','different-check.cjs']
        self.assertFalse(evaluate_phase(phase,invalid,workspace_root=str(self.root))['phase_complete'])
        self.assertFalse(evaluate_phase({**phase,'host_verification':False},report,workspace_root=str(self.root))['phase_complete'])

    def test_replanning_cannot_drop_owned_browser_verification_or_repair_authority(self):
        from olcr_api.coding_verification import inherit_owned_verification
        old={'phases':[{'id':'final','execution_mode':'VERIFICATION_ONLY','host_verification':True,
            'done':['Browser opens'],'verify':['Original browser requirement']}]}
        new={'scope':{'allowed':['app.js']},'file_manifest':[{'path':'app.js','owner_phase':'build'}],
            'phases':[{'id':'build','execution_mode':'IMPLEMENTATION'},
                {'id':'renamed','execution_mode':'VERIFICATION_ONLY','done':['Tests pass'],
                 'verify':[],'dependencies':[],'verification_command':['node --test actual.test.js']}],
            'tasks':[{'task_id':'build-task','phase_id':'build'}, {'task_id':'verify-task','phase_id':'renamed'}]}
        snapshot=copy.deepcopy(new)
        result=inherit_owned_verification(new,old)
        final=result['phases'][-1]
        self.assertTrue(final['host_verification'])
        self.assertTrue(final['verification_repair_allowed'])
        self.assertIn('Original browser requirement',final['verify'])
        self.assertIn('Browser opens',final['done'])
        self.assertEqual(['build'],final['dependencies'])
        self.assertEqual(['node --test actual.test.js'],final['verification_command'])
        self.assertEqual(['build-task'],result['tasks'][-1]['depends_on'])
        self.assertEqual(snapshot,new)
        self.assertEqual(snapshot['scope'],result['scope'])
        self.assertEqual(snapshot['file_manifest'],result['file_manifest'])
        self.assertEqual(result,inherit_owned_verification(result,result,['final']))
        forbidden=copy.deepcopy(old)
        forbidden['phases'][0]['verification_repair_allowed']=False
        self.assertFalse(inherit_owned_verification(new,forbidden)['phases'][-1]['verification_repair_allowed'])

    def test_historical_owned_runner_restores_dropped_gate_without_touching_source_owners(self):
        from olcr_api.coding_verification import inherit_owned_verification
        plan={'phases':[{'id':'old-final','execution_mode':'IMPLEMENTATION'}],
              'file_manifest':[{'path':'app.js','owner_phase':'old-final'}]}
        restored=inherit_owned_verification(plan,plan,['old-final'])
        self.assertEqual('IMPLEMENTATION',restored['phases'][0]['execution_mode'])
        self.assertEqual(2,len(restored['phases']))
        self.assertEqual(['old-final'],restored['phases'][1]['dependencies'])
        self.assertEqual(restored,inherit_owned_verification(restored,restored,['old-final']))
        self.assertEqual(plan,inherit_owned_verification(plan,plan), 'No provenance means no new host authority')

    def test_owned_final_gate_is_not_inserted_before_a_downstream_implementation(self):
        from olcr_api.coding_verification import inherit_owned_verification
        old={'phases':[{'id':'gate','execution_mode':'VERIFICATION_ONLY','host_verification':True,
                       'done':['Browser opens'],'verify':['Original behavior']} ]}
        plan={'phases':[{'id':'first','execution_mode':'IMPLEMENTATION','dependencies':[]},
                       {'id':'gate','execution_mode':'VERIFICATION_ONLY','dependencies':['first']},
                       {'id':'second','execution_mode':'IMPLEMENTATION','dependencies':['gate']}]}
        result=inherit_owned_verification(plan,old)
        self.assertEqual(4,len(result['phases']))
        self.assertEqual(['first'],result['phases'][1]['dependencies'])
        self.assertEqual(['gate'],result['phases'][2]['dependencies'])
        final=result['phases'][-1]
        self.assertNotEqual('gate',final['id'])
        self.assertEqual(['first','second'],final['dependencies'])
        self.assertEqual(result,inherit_owned_verification(result,result,['gate']))

    def test_single_string_direct_test_argv_is_normalized_without_shell_interpretation(self):
        from olcr_api.coding_verification import normalized_verification_argv
        self.assertEqual(['node','--test','folder with spaces/logic.test.js'],
            normalized_verification_argv(['node --test "folder with spaces/logic.test.js"']))
        self.assertEqual(['python3','-m','unittest'],normalized_verification_argv(['python3 -m unittest']))
        for value in (['node --test; touch unexpected'],['node --test | cat'],['node $(echo bad)'],
                      ['sh -c "node --test"'],['please run the tests'],['node --test "']):
            self.assertIsNone(normalized_verification_argv(value),value)
        with tempfile.TemporaryDirectory() as directory:
            executable=Path(directory)/'binary with spaces';executable.write_text('')
            self.assertEqual([str(executable)],normalized_verification_argv([str(executable)]))

    def test_truncated_verifier_recovers_only_complete_json_check_objects(self):
        from olcr_api.coding_verification import complete_verification_prefix
        value={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'NODE','code':"assert.ok(readFile('app.js'));"},
            {'ids':['VERIFY:2'],'kind':'BROWSER','code':"assert.equal(await page.locator('button').textContent(),'選ぶ');"}]}
        raw=json.dumps(value,ensure_ascii=False)
        truncated=raw[:raw.index('assert.equal')+12]
        recovered=complete_verification_prefix(truncated)
        self.assertEqual(value['checks'][:1],recovered['checks'])
        self.assertIsNone(complete_verification_prefix('prose '+truncated))
        self.assertIsNone(complete_verification_prefix('{"operations":[{"op":"write"}'))

    def test_original_specification_bullets_are_retained_as_browser_checks(self):
        from olcr_api.coding_verification import bind_original_acceptance
        plan={'file_manifest':[{'path':'index.html'}],'phases':[{'id':'p','host_verification':True,'verify':[]}]}
        original='Create an app.\n【仕様】\n\n- 1行に1つ入力できる\n- 履歴を5件表示する\n\nRun tests afterward.'
        result=bind_original_acceptance(plan,original)
        self.assertEqual(2,len(result['phases'][0]['verify']))
        self.assertEqual(result,bind_original_acceptance(result,original))
        self.assertEqual(plan['file_manifest'],result['file_manifest'])

    def test_original_per_line_input_requirement_removes_conflicting_planner_single_line_rule(self):
        from olcr_api.coding_verification import bind_original_acceptance
        conflicting='index.html contains a single-line input field for dish names.'
        plan={'phases':[{'id':'acceptance','host_verification':True,'done':[],
            'verify':[conflicting,'The button shows a large result.']}],
            'tasks':[{'phase_id':'acceptance','verification':[conflicting,'Run node:test.']}]}
        original='【仕様】\n- 料理名を1行に1つ入力できる'
        result=bind_original_acceptance(plan,original)
        final=result['phases'][0]
        self.assertNotIn(conflicting,final['verify'])
        self.assertIn('The button shows a large result.',final['verify'])
        self.assertIn('料理名を1行に1つ入力できる',final['verify'][-1])
        self.assertNotIn(conflicting,result['tasks'][0]['verification'])
        self.assertIn('Run node:test.',result['tasks'][0]['verification'])
        self.assertEqual(result,bind_original_acceptance(result,original))

    def test_old_host_checks_cannot_prove_a_new_requirement(self):
        phase={'id':'p','execution_mode':'VERIFICATION_ONLY','host_verification':True,'done':['Old behavior'],'verify':['New behavior']}
        report={'status':'PASS','criteria_evidence':{'Old behavior':True},'typed_execution_summary':{'operations':[
            {'tool':'verification_command','status':'success','output':{'returncode':0}}]}}
        result=evaluate_phase(phase,report,workspace_root=str(self.root))
        self.assertFalse(result['phase_complete'])
        self.assertIn('VERIFY:1',result['required_not_run_ids'])

    def test_failed_test_execution_is_not_missing_package_installation(self):
        from olcr_api.coding_tasks import _installation_criterion
        self.assertFalse(_installation_criterion('Run the test script from package.json'))
        self.assertFalse(_installation_criterion('No external dependencies are installed'))
        self.assertTrue(_installation_criterion('Dependencies are installed with npm install'))
        criterion='Run the test script from package.json'
        phase={'id':'p','done':['All tests pass'],'verify':[criterion],'execution_mode':'VERIFICATION_ONLY','host_verification':True}
        report={'status':'FAIL','criteria_evidence':{'All tests pass':False,criterion:False},'test_fail':['observed assertion failure'],
                'typed_execution_summary':{'operations':[{'tool':'verification_command','status':'failed','output':{'returncode':1}}]}}
        result=evaluate_phase(phase,report,workspace_root=str(self.root))
        self.assertFalse(result['phase_complete'])
        self.assertFalse(result['required_not_run_present'])
        self.assertNotIn('PACKAGE_MANAGER_COMMAND',result['required_not_run_executor_capability'])

    def test_module_loading_error_is_not_replaced_by_generic_file_container_failure(self):
        (self.root/'broken.js').write_text('module.exports={missingExport};')
        (self.root/'broken.test.js').write_text("require('./broken.js');require('node:test')('unreached',()=>{});")
        result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':'await runTests();assert.ok(true);'}])
        self.assertNotEqual(0,result.returncode)
        payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
        failure=payload['test_runs'][0]['failures'][0]
        self.assertEqual('Node.js module loading',failure['name'])
        self.assertIn('ReferenceError: missingExport is not defined',failure['detail'])
        self.assertIn('broken.js',failure['detail'])

    def test_both_verifiers_attribute_late_file_load_failure_and_then_expose_real_assertion(self):
        from olcr_api.verification_diagnostics import node_test_run, project_test_failure_diagnosis
        good=self.root/'a.test.cjs'
        bad=self.root/'z with spaces.test.cjs'
        good.write_text("require('node:test')('healthy',()=>{});")
        body="describe('contract',()=>it('real assertion',()=>require('node:assert/strict').equal(1,2)));"
        bad.write_text("'use strict';\n"+body)
        for missing_import in (True,False):
            if not missing_import:
                bad.write_text("const {describe,it}=require('node:test');\n"+body)
            result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':'await runTests();assert.ok(true);'}])
            self.assertNotEqual(0,result.returncode)
            host=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])['test_runs'][0]
            command=[host['command'][0],'--test','--test-reporter=tap',str(good),str(bad)]
            direct=subprocess.run(command,cwd=self.root,capture_output=True,text=True,timeout=20)
            self.assertNotEqual(0,direct.returncode)
            for run in (host,node_test_run(command,direct.stdout,direct.returncode,self.root)):
                with self.subTest(missing_import=missing_import,command=run['command']):
                    diagnosis=project_test_failure_diagnosis({'test_runs':[run]},
                        {f.name:f.read_text() for f in (good,bad)})
                    failures=run['failures']
                    if missing_import:
                        self.assertEqual(1,run['registered_tests'])
                        self.assertEqual(1,len(failures))
                        self.assertIn('describe is not defined',failures[0]['detail'])
                        self.assertEqual((bad.name,2),tuple(failures[0]['source_locations'][0][k] for k in ('path','line')))
                        self.assertEqual([bad.name],diagnosis['repair_paths'])
                    else:
                        self.assertTrue(any(f['name']=='real assertion' for f in failures))
                        self.assertEqual('INCONCLUSIVE',diagnosis['verdict'])
                        self.assertEqual([],diagnosis['repair_paths'])

    def test_native_setup_repair_runs_real_assertions_and_preserves_shadowed_matchers(self):
        from olcr_api.source_repair import native_registration_repair,compile_repair
        from olcr_api.verification_diagnostics import project_test_failure_diagnosis
        test=self.root/'value.test.mjs'
        scoped="function custom(expect) { expect(4).toBe(9); }"
        test.write_text("#!/usr/bin/env node\n'use strict';\n"+scoped+"\n"
            "describe('values',()=>{it('passes',()=>{expect({values:[4]}).toEqual({values:[4]});});"
            "it('fails',()=>{expect(4).toBe(9);});});\n")
        def verify():
            result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':'await runTests();assert.ok(true);'}])
            self.assertNotEqual(0,result.returncode)
            return json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])['test_runs'][0]
        before=verify()
        sources={test.name:test.read_text()}
        finding=project_test_failure_diagnosis({'test_runs':[before]},sources)
        edit=native_registration_repair(finding['failure_case'],sources)
        fixed=compile_repair(edit,sources)[0]['operations'][0]['content']
        self.assertIn(scoped,fixed)
        self.assertTrue(fixed.startswith("#!/usr/bin/env node\n'use strict';"))
        test.write_text(fixed)
        self.assertIn('expect(4).toBe(9)',fixed)
        registered=verify()
        self.assertEqual(2,registered['registered_tests'])
        self.assertFalse(any('failureType: \'subtestsFailed\'' in failure['detail']
                             for failure in registered['failures']))
        sources={test.name:fixed}
        finding=project_test_failure_diagnosis({'test_runs':[registered]},sources)
        edit=native_registration_repair(finding['failure_case'],sources)
        fixed=compile_repair(edit,sources)[0]['operations'][0]['content']
        self.assertIn(scoped,fixed)
        test.write_text(fixed)
        after=verify()
        self.assertEqual((1,1),(after['passed'],after['failed']))
        self.assertTrue(any(f['name']=='fails' and 'ERR_ASSERTION' in f['detail'] for f in after['failures']))
        self.assertEqual('INCONCLUSIVE',project_test_failure_diagnosis({'test_runs':[after]},{test.name:fixed})['verdict'])

    def test_test_result_aliases_refer_to_the_same_observed_run(self):
        (self.root/'one.test.js').write_text("require('node:test')('registered check',()=>require('node:assert/strict').equal(1,1));")
        result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':
            'const r=await runTests();assert.equal(r.tests,1);assert.equal(r.testCount,r.tests);assert.equal(r.returncode,0);assert.equal(r.exitCode,r.returncode);'}])
        self.assertEqual(0,result.returncode,result.stderr+result.stdout)

    def test_overlapping_checks_and_readonly_node_helpers_are_supported(self):
        checks=[{'ids':['VERIFY:1'],'kind':'NODE','code':"const fs=require('fs');assert.ok(fs.existsSync('app.js'));assert.equal(fs.existsSync('node_modules'),false);"},
                {'ids':['VERIFY:1'],'kind':'NODE','code':"assert.ok(readFile('app.js').includes('onclick'));"}]
        self.assertEqual([],validate_verification({'entrypoint':'index.html','checks':checks},{'VERIFY:1':'source check'},self.root))
        result=self.execute(checks)
        self.assertEqual(0,result.returncode,result.stderr+result.stdout)

    def test_check_can_import_tracked_assert_in_its_own_lexical_scope(self):
        from olcr_api.coding_verification import verification_syntax_errors
        value={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'NODE',
            'code':"const assert=require('node:assert/strict');assert.ok(readFile('app.js').includes('onclick'));"}]}
        self.assertEqual([],verification_syntax_errors(value))
        result=self.execute(value['checks'])
        self.assertEqual(0,result.returncode,result.stderr+result.stdout)
        value['checks'][0]['code']="const assert=require('node:assert/strict');readFile('app.js');"
        result=self.execute(value['checks'])
        self.assertNotEqual(0,result.returncode)
        self.assertIn('no observed assertions',result.stdout)

    def test_generated_check_cannot_replace_owned_helpers(self):
        from olcr_api.coding_verification import verification_syntax_errors
        code='const {runTests}={runTests:async()=>({tests:1,returncode:0})};assert.ok((await runTests()).tests>0);'
        value={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'NODE','code':code}]}
        self.assertTrue(verification_syntax_errors(value))
        result=self.execute(value['checks'])
        self.assertNotEqual(0,result.returncode)
        self.assertIn('already been declared',result.stdout)

    def test_standard_checks_group_only_equivalent_unambiguous_criteria(self):
        from olcr_api.coding_verification import standard_verification_checks
        criteria={'DONE:1':'All node:test tests pass.',
            'VERIFY:1':'Run the test script from package.json and confirm all tests pass.',
            'VERIFY:2':'Tests cover random selection.',
            'DONE:2':'The app loads in a browser without console errors.'}
        checks=standard_verification_checks(criteria)
        self.assertEqual([['DONE:1','VERIFY:1'],['DONE:2']],[check['ids'] for check in checks])
        self.assertNotIn('VERIFY:2',[key for check in checks for key in check['ids']])

    def test_command_exit_and_output_checks_are_owned_and_do_not_claim_behavior(self):
        from olcr_api.coding_verification import standard_verification_checks
        criteria={'VERIFY:1':'node --test math.test.js exits with code 0 and prints passing test results.',
                  'VERIFY:2':'Running node --test math.test.js passes without errors.',
                  'VERIFY:3':'node --test math.test.js exits with code 0 and verifies keyboard accessibility.'}
        checks=standard_verification_checks(criteria)
        self.assertEqual([['VERIFY:1','VERIFY:2']],[check['ids'] for check in checks])
        target=self.root/'math.test.js'
        target.write_text("require('node:test')('addition',()=>require('node:assert/strict').equal(1+1,2));")
        self.assertEqual(0,self.execute(checks).returncode)
        target.write_text("require('node:test')('addition',()=>require('node:assert/strict').equal(1+1,3));")
        self.assertNotEqual(0,self.execute(checks).returncode)

    def test_japanese_test_execution_uses_owned_checks_without_dropping_named_targets(self):
        from olcr_api.coding_verification import standard_verification_checks
        criteria={'DONE:1':'全テストが成功する','VERIFY:1':'テストが全項目成功する',
                  'VERIFY:2':'node --test logic.test.jsが0で終了する',
                  'VERIFY:3':'テストが空入力を網羅する','VERIFY:4':'テストコードを作成する'}
        checks=standard_verification_checks(criteria)
        self.assertEqual([['DONE:1','VERIFY:1','VERIFY:2']],[check['ids'] for check in checks])
        target=self.root/'logic.test.js'
        target.write_text("require('node:test')('real test',()=>require('node:assert/strict').equal(1,1));")
        self.assertEqual(0,self.execute(checks).returncode)
        target.write_text("require('node:test')('real test',()=>require('node:assert/strict').equal(1,2));")
        self.assertNotEqual(0,self.execute(checks).returncode)
        target.unlink()
        (self.root/'other.test.js').write_text("require('node:test')('other real test',()=>{});")
        self.assertNotEqual(0,self.execute(checks).returncode)

    def test_generated_code_line_frames_preserve_javascript_string_escapes(self):
        from olcr_api.coding_verification import normalize_verification_source, verification_syntax_errors
        lines=['const input="A\\nB";', 'assert.equal(input.split("\\n").length,2);']
        framed={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'NODE','code':lines}]}
        value=normalize_verification_source(json.loads(json.dumps(framed)))
        self.assertEqual(lines,framed['checks'][0]['code'])
        self.assertEqual('\n'.join(lines),value['checks'][0]['code'])
        self.assertEqual([],verification_syntax_errors(value))
        result=self.execute(value['checks'])
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(value,normalize_verification_source(value))

    def test_load_module_exposes_actual_commonjs_object_exports_by_name(self):
        (self.root/'logic.cjs').write_text('const api={compute:value=>value+1};module.exports=api;')
        result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':
            'const mod=await loadModule("logic.cjs");assert.equal(mod.compute,mod.default.compute);assert.equal(mod.compute(2),3);'}])
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_project_import_error_is_not_misclassified_as_check_author_error(self):
        (self.root/'logic.cjs').write_text('throw new ReferenceError("actual project failure");')
        result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':
            'const mod=await loadModule("logic.cjs");assert.ok(mod);'}])
        payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=')[1])
        self.assertFalse(payload['failures'][0]['contract_error'])
        result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':'unknownCheckHelper();assert.ok(true);'}])
        payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=')[1])
        self.assertTrue(payload['failures'][0]['contract_error'])

    def test_failed_test_summaries_keep_each_assertion_without_repeated_stacks(self):
        (self.root/'failed.test.js').write_text("const test=require('node:test');const assert=require('node:assert/strict');test('first condition',()=>assert.equal(1,2));test('second condition',()=>assert.equal('actual','expected'));")
        result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':'const r=await runTests();assert.ok(r.tests>0);'}])
        payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=')[1])
        failures=payload['test_runs'][0]['failures']
        self.assertEqual(['first condition','second condition'],[item['name'] for item in failures])
        self.assertIn('expected',failures[1]['detail'])
        self.assertNotIn('TestContext.<anonymous>',str(failures))

    def test_test_failure_preserves_actual_assertion_and_fixture_call_locations(self):
        source="const test=require('node:test');\nconst assert=require('node:assert/strict');\nfunction fixture(values){return values[2];}\ntest('fixture mismatch',()=>{\n  const actual=fixture(['A']);\n  assert.equal(actual,'A');\n});\n"
        (self.root/'fixture.test.js').write_text(source)
        result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':'await runTests();assert.ok(true);'}])
        payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
        failure=payload['test_runs'][0]['failures'][0]
        self.assertIn('undefined',failure['detail'])
        self.assertEqual({4,6},{frame['line'] for frame in failure['source_locations']})
        assertion=next(frame for frame in failure['source_locations'] if frame['line']==6)
        self.assertEqual('fixture.test.js',assertion['path'])
        self.assertIn("assert.equal(actual,'A')",assertion['source_line'])
        self.assertIn("fixture(['A'])",str(assertion['context']))

    def test_esm_assertion_frame_decodes_file_urls_with_spaces(self):
        folder=self.root/'project with spaces 日本語'
        folder.mkdir()
        source="import {test} from 'node:test';\nimport assert from 'node:assert/strict';\nfunction fixture(values){return values[2];}\ntest('esm fixture mismatch',()=>{\n  const actual=fixture(['A']);\n  assert.equal(actual,'A');\n});\n"
        (folder/'fixture.test.mjs').write_text(source)
        result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':'await runTests();assert.ok(true);'}])
        payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
        failure=payload['test_runs'][0]['failures'][0]
        self.assertNotEqual(0,result.returncode)
        self.assertEqual({4,6},{frame['line'] for frame in failure['source_locations']})
        assertion=next(frame for frame in failure['source_locations'] if frame['line']==6)
        self.assertEqual('project with spaces 日本語/fixture.test.mjs',assertion['path'])
        self.assertIn("assert.equal(actual,'A')",assertion['source_line'])
        self.assertIn("fixture(['A'])",str(assertion['context']))

    def test_module_loading_error_preserves_the_throwing_esm_source(self):
        folder=self.root/'module with spaces'
        folder.mkdir()
        (folder/'logic.mjs').write_text('export const value=1;\ndocument.addEventListener("DOMContentLoaded",()=>{});\n')
        (folder/'logic.test.mjs').write_text("import {test} from 'node:test';\nimport {value} from './logic.mjs';\ntest('real API',()=>{});\n")
        result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':'await runTests();assert.ok(true);'}])
        payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
        failure=payload['test_runs'][0]['failures'][0]
        self.assertEqual('Node.js module loading',failure['name'])
        self.assertIn('document is not defined',failure['detail'])
        frame=next(item for item in failure['source_locations'] if item['path']=='module with spaces/logic.mjs')
        self.assertEqual(2,frame['line'])
        self.assertIn('document.addEventListener',frame['source_line'])

    def test_failed_test_run_is_not_reexecuted_by_each_check(self):
        (self.root/'failed.test.js').write_text("const test=require('node:test');const fs=require('node:fs');test('failure',()=>{fs.appendFileSync('counter.txt','1');throw Error('actual failure')});")
        result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':
            'for(let i=0;i<2;i++){await assert.rejects(runTests(),/actual failure/);}'}])
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual('1',(self.root/'counter.txt').read_text())

    def test_browser_base_url_supports_relative_entrypoint_navigation(self):
        result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':
            'await page.goto(baseURL+"index.html");assert.equal(await page.locator("button").textContent(),"選ぶ");'}])
        self.assertEqual(0,result.returncode,result.stderr+result.stdout)

    def test_attachment_helper_observes_empty_dom_nodes_without_claiming_visibility(self):
        (self.root/'index.html').write_text('<textarea id="input"></textarea><ul id="history" style="margin:0;padding:0"></ul>')
        result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':
            'assert.equal(await page.locator("#history").isAttached(),true);'
            'assert.equal(await page.locator("#history").isVisible(),false);'
            'assert.equal(await page.locator("#absent").isAttached(),false);'}])
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':
            'assert.equal(await page.locator("#input").getAttribute("type"),"textarea");'}])
        failure=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])['failures'][0]
        field=failure['observed_page']['fields'][0]
        self.assertEqual('textarea',field['tag'])
        self.assertEqual('textarea',field['type'])
        self.assertIsNone(field['type_attribute'])

    def test_typed_browser_observations_compile_real_actions_without_javascript_quoting(self):
        from olcr_api.coding_verification import compile_browser_observations, verification_syntax_errors
        value={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER',
            'actions':[{'action':'fill','selector':'#input','value':'A\nB'},{'action':'click','selector':'#select','value':None}],
            'observations':[{'selector':'#input','measure':'tag','operator':'eq','expected':'textarea'},
                {'selector':'#result','measure':'text','operator':'one_of','expected':['A','B']},
                {'selector':'#result','measure':'visible','operator':'eq','expected':True}]}]}
        original=copy.deepcopy(value)
        compiled=compile_browser_observations(value)
        self.assertEqual(original,value)
        self.assertEqual([],verification_syntax_errors(compiled))
        self.assertEqual(0,self.execute(compiled['checks']).returncode)
        (self.root/'app.js').write_text("document.querySelector('#select').onclick=()=>document.querySelector('#result').textContent='wrong';")
        self.assertNotEqual(0,self.execute(compiled['checks']).returncode)

    def test_reload_observation_normalizes_dom_root_alias_and_schema_requires_page(self):
        from olcr_api.coding_verification import (compile_browser_observations,
            verification_observation_schema)
        criteria={'VERIFY:1':'Verify that saved recipes are restored after reloading the page.'}
        schema=verification_observation_schema(criteria)
        reload_branches=[]
        def collect(value):
            if isinstance(value,dict):
                properties=value.get('properties') or {}
                if (properties.get('action') or {}).get('enum')==['reload']:
                    reload_branches.append(value)
                for child in value.values():collect(child)
            elif isinstance(value,list):
                for child in value:collect(child)
        collect(schema)
        self.assertTrue(reload_branches)
        self.assertTrue(all(branch['properties']['selector'].get('enum')==['page']
                            for branch in reload_branches))

        value={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER',
            'actions':[{'action':'reload','selector':':root','value':None}],
            'observations':[{'selector':'#recipe-list li','measure':'count',
                             'operator':'eq','expected':1}]}]}
        original=copy.deepcopy(value)
        (self.root/'index.html').write_text('<ul id="recipe-list"><li>煮魚</li></ul>')
        compiled=compile_browser_observations(value,criteria)
        self.assertEqual(original,value)
        self.assertIn('await page.reload();',compiled['checks'][0]['code'])
        self.assertNotIn('page.locator(":root")',compiled['checks'][0]['code'])
        result=self.execute(compiled['checks'])
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_typed_browser_observations_keep_literal_data_and_reject_mutation_instructions(self):
        from olcr_api.coding_verification import compile_browser_observations
        literal='A";globalThis.__injected=1;//'
        value={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER',
            'actions':[{'action':'fill','selector':'#input','value':literal}],
            'observations':[{'selector':'#input','measure':'value','operator':'eq','expected':literal}]}]}
        compiled=compile_browser_observations(value)
        compiled['checks'][0]['code']+='\nassert.equal(await page.evaluate(()=>globalThis.__injected),undefined);'
        self.assertEqual(0,self.execute(compiled['checks']).returncode)
        for change in [{'actions':[{'action':'evaluate','selector':'body','value':'document.body.remove()'}]},
                       {'observations':[{'selector':'body','measure':'innerHTML','operator':'eq','expected':'fake'}]},
                       {'code':'assert.ok(true);'}]:
            bad=copy.deepcopy(value);bad['checks'][0].update(change)
            with self.assertRaises(ValueError):compile_browser_observations(bad)

    def test_structural_browser_schema_excludes_code_and_keeps_behavioral_loops_separate(self):
        from olcr_api.coding_verification import verification_schema, declarative_browser_criteria
        criteria={'VERIFY:1':'index.html exists and renders a Japanese UI with an input and a button.'}
        self.assertTrue(declarative_browser_criteria(criteria))
        item=verification_schema(criteria)['properties']['checks']['items']
        self.assertIn('observations',item['required'])
        self.assertNotIn('code',item['properties'])
        self.assertFalse(declarative_browser_criteria({'VERIFY:1':'In the browser, avoid consecutive repeated choices.'}))
        self.assertFalse(declarative_browser_criteria({'VERIFY:1':'In the browser, keep the latest five results.'}))

    def test_layout_observations_do_not_allow_unneeded_clicks_or_font_comparisons(self):
        from olcr_api.coding_verification import verification_schema, compile_browser_observations, layout_browser_criteria
        criteria={'VERIFY:1':'style.css exists and provides a mobile-friendly responsive layout.'}
        self.assertTrue(layout_browser_criteria(criteria))
        item=verification_schema(criteria)['properties']['checks']['items']['properties']
        self.assertEqual(1,item['actions']['maxItems'])
        viewport_actions=item['actions']['items']['anyOf']
        self.assertTrue(all(action['properties']['action']['enum']==['viewport'] for action in viewport_actions))
        measures={name for branch in item['observations']['items']['anyOf']for name in branch['properties']['measure']['enum']}
        self.assertNotIn('font_larger_than_body',measures)
        (self.root/'style.css').write_text('body { max-width: 100%; }')
        observations=[{'selector':'html','measure':'overflow_x','operator':'eq','expected':False},
            {'selector':'#select','measure':'visible','operator':'eq','expected':True}]
        value={'entrypoint':'index.html','checks':[
            {'ids':['VERIFY:1'],'kind':'BROWSER','actions':[{'action':'viewport','selector':'page',
                'value':{'width':375,'height':812}}],'observations':observations},
            {'ids':['VERIFY:1'],'kind':'BROWSER','actions':[{'action':'viewport','selector':'page',
                'value':{'width':1280,'height':800}}],'observations':observations}]}
        self.assertEqual(0,self.execute(compile_browser_observations(value,criteria)['checks'],mobile_criteria=['VERIFY:1']).returncode)
        invalid=copy.deepcopy(value)
        invalid['checks'][0]['actions']=[{'action':'click','selector':'#select','value':None}]
        with self.assertRaises(ValueError):compile_browser_observations(invalid,criteria)
        self.assertFalse(layout_browser_criteria({'VERIFY:1':'The mobile-friendly UI remains usable after clicking the button.'}))

    def test_unobserved_native_assert_import_explains_how_to_use_the_owned_helper(self):
        result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':
            "const native=await import('node:assert/strict');native.ok(readFile('app.js').length>0);"}])
        self.assertNotEqual(0,result.returncode)
        failure=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])['failures'][0]
        self.assertTrue(failure['contract_error'])
        self.assertIn('Use the supplied assert/expect helpers',failure['error'])
        self.assertNotIn('VERIFY:1',json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])['passed_ids'])

    def network_observations(self):
        from olcr_api.coding_verification import compile_browser_observations
        criteria={'VERIFY:1':'The app can be opened in a browser without external libraries or network calls.'}
        value={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'BROWSER','actions':[],
            'observations':[{'selector':'html','measure':'local_resources','operator':'eq','expected':True},
                {'selector':'html','measure':'external_request_count','operator':'eq','expected':0},
                {'selector':'package.json','measure':'dependency_count','operator':'eq','expected':0}]}]}
        return criteria,value,compile_browser_observations(value,criteria)

    def test_network_observations_resolve_bare_local_resources_and_optional_package(self):
        from olcr_api.coding_verification import verification_schema, compile_browser_observations
        criteria,value,compiled=self.network_observations()
        (self.root/'index.html').write_text((self.root/'index.html').read_text()+'<link rel="stylesheet" href="style.css">')
        (self.root/'style.css').write_text('body { color: black; }')
        result=self.execute(compiled['checks'],forbid_external_requests=True)
        self.assertEqual(0,result.returncode,result.stderr+result.stdout)
        item=verification_schema(criteria)['properties']['checks']['items']['properties']
        self.assertEqual(0,item['actions']['maxItems'])
        self.assertNotIn('code',item)
        measures={name for branch in item['observations']['items']['anyOf']for name in branch['properties']['measure']['enum']}
        self.assertIn('external_request_count',measures)
        value['checks'][0]['actions']=[{'action':'click','selector':'#select','value':None}]
        with self.assertRaises(ValueError):compile_browser_observations(value,criteria)

    def test_network_observations_reject_actual_declared_dependencies(self):
        _,_,compiled=self.network_observations()
        (self.root/'package.json').write_text('{"dependencies":{"vendor":"1.0"}}')
        result=self.execute(compiled['checks'],forbid_external_requests=True)
        self.assertNotEqual(0,result.returncode)
        failure=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])['failures'][0]
        self.assertIn('observed_2',failure['assertion_source']['snippet'])

    def test_named_node_test_requirement_uses_real_node_command_not_invented_plan_flag(self):
        from olcr_api.coding_verification import standard_verification_checks
        (self.root/'logic.test.js').write_text("const {test}=require('node:test');test('actual test',()=>{});")
        for text in ['Run node:test --test-path logic.test.js and confirm all tests pass.',
                     'node:test --test-path logic.test.js passes.']:
            checks=standard_verification_checks({'VERIFY:1':text})
            self.assertEqual(1,len(checks))
            self.assertIn('logic.test.js',checks[0]['code'])
            self.assertNotIn('--test-path',checks[0]['code'])
            result=self.execute(checks)
            self.assertEqual(0,result.returncode,result.stderr+result.stdout)

    def test_network_observations_block_attempted_external_fetch_and_record_only_origin(self):
        _,_,compiled=self.network_observations()
        (self.root/'app.js').write_text("fetch('https://example.invalid/?secret=MASKME').catch(()=>{});")
        result=self.execute(compiled['checks'],forbid_external_requests=True)
        self.assertNotEqual(0,result.returncode)
        value=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
        self.assertNotIn('VERIFY:1',value['passed_ids'])
        self.assertEqual(['https://example.invalid'],value['failures'][0]['external_requests'])
        self.assertNotIn('MASKME',result.stdout)

    def test_network_observations_block_websocket_handshake_without_connecting_to_external_server(self):
        _,_,compiled=self.network_observations()
        (self.root/'app.js').write_text("new WebSocket('wss://example.invalid/?secret=MASKME');")
        result=self.execute(compiled['checks'],forbid_external_requests=True)
        self.assertNotEqual(0,result.returncode)
        value=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
        self.assertNotIn('VERIFY:1',value['passed_ids'])
        self.assertEqual(['wss://example.invalid'],value['failures'][0]['external_requests'])
        self.assertNotIn('MASKME',result.stdout)

    def test_empty_input_checks_preserve_invalid_state_and_observe_existing_guidance(self):
        from olcr_api.coding_verification import verification_prompt
        criteria={'VERIFY:1':'In the browser, verify the original requirement: 空入力の場合は分かりやすいメッセージを表示する'}
        prompt=verification_prompt({'verify':[]},criteria['VERIFY:1'],{},criteria)
        self.assertIn('keep the tested input empty/invalid',prompt)
        self.assertIn('Do not click a disabled button',prompt)
        self.assertIn('need not be inside the result element',prompt)
        self.assertIn('HTML boolean attributes may have an empty string value',prompt)
        self.assertGreater(prompt.index('EMPTY_INPUT_FOCUS:'),prompt.index('ACTUAL_SOURCE:'))
        self.assertIn('both empty and whitespace-only',prompt)
        self.assertIn('do not require a nonempty result there',prompt)
        self.assertNotIn('HISTORY_FOCUS:',prompt)
        self.assertNotIn('MOBILE_FOCUS:',prompt)
        self.assertNotIn('NATIVE_SELECT_FOCUS:',prompt)
        self.assertLess(len(prompt),4000)
        (self.root/'index.html').write_text('<textarea id="input"></textarea><button id="select" disabled>選ぶ</button><p class="hint">候補を入力してください</p><output id="result"></output>')
        code=('await page.locator("#input").fill("  ");'
              'assert.equal((await page.locator("#input").inputValue()).trim(),"");'
              'await expect(page.locator("#select")).toBeDisabled();'
              'await expect(page.locator(".hint")).toBeVisible();'
              'assert.ok((await page.locator(".hint").textContent()).includes("入力してください"));')
        result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':code}])
        self.assertEqual(0,result.returncode,result.stderr+result.stdout)
        wrong='assert.ok((await page.locator("#result").textContent()).trim());'
        result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':wrong}])
        failure=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])['failures'][0]
        self.assertEqual([{'tag':'p','id':'','selector':'p.hint','text':'候補を入力してください','visible':True}],failure['observed_page']['messages'])
        self.assertTrue(failure['observed_page']['buttons'][0]['disabled'])
        (self.root/'index.html').write_text('<textarea id="input"></textarea><button id="select" disabled>選ぶ</button><p class="hint" hidden>候補を入力してください</p><output id="result"></output>')
        result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':code}])
        self.assertNotEqual(0,result.returncode)
        failure=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])['failures'][0]
        self.assertEqual([],failure['observed_page']['messages'])

    def test_verification_instructions_follow_the_selected_criterion_not_other_request_topics(self):
        from olcr_api.coding_verification import verification_prompt
        original='An app with multiline input, randomized selection, history and a mobile UI; run node:test.'
        cases=[('In the browser, verify the original requirement: 直近5回の結果を履歴として表示する',
                'HISTORY_FOCUS:', ['MOBILE_FOCUS:','EMPTY_INPUT_FOCUS:','NODE_FOCUS:']),
               ('In the browser, verify the original requirement: スマートフォンでも使いやすい見た目にする',
                'MOBILE_FOCUS:', ['HISTORY_FOCUS:','EMPTY_INPUT_FOCUS:','NODE_FOCUS:']),
               ('Tests cover history behavior using the actual exported API.',
                'NODE_FOCUS:', ['HISTORY_FOCUS:','MOBILE_FOCUS:','EMPTY_INPUT_FOCUS:'])]
        for requirement,expected,absent in cases:
            with self.subTest(requirement=requirement):
                prompt=verification_prompt({'verify':[requirement]},original,{'logic.test.js':'test source'}, {'VERIFY:1':requirement})
                self.assertIn(expected,prompt)
                for other in absent:self.assertNotIn(other,prompt)
                if expected!='NODE_FOCUS:':self.assertNotIn('test source',prompt)
                else:
                    self.assertIn('Imported bindings and local declarations are NOT exports',prompt)
                    self.assertIn('Never inspect a test-file namespace',prompt)

    def test_rendered_ui_and_mobile_checks_require_browser_in_both_validation_and_schema(self):
        from olcr_api.coding_verification import browser_criteria, verification_schema
        for requirement in [
                'index.html exists and renders a Japanese lunch roulette UI with an input area and button.',
                'style.css exists and provides a mobile-friendly responsive layout.',
                'app.js exists and wires the UI to the shared logic API.',
                'Enter an empty input and confirm a clear Japanese error message is displayed.',
                'Enter duplicate names and confirm duplicates are handled consistently.',
                '画面に入力欄と結果を表示する。']:
            with self.subTest(requirement=requirement):
                criteria={'VERIFY:1':requirement}
                self.assertEqual({'VERIFY:1'},browser_criteria(criteria))
                kind=verification_schema(criteria)['properties']['checks']['items']['properties']['kind']
                self.assertEqual(['BROWSER'],kind['enum'])
                value={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'NODE','code':'assert.ok(readFile("index.html"));'}]}
                self.assertTrue(any('BROWSER checks must cover' in error for error in validate_verification(value,criteria,self.root)))
        for requirement in ['logic.js exports the actual selection API.','Tests cover empty input and history behavior.',
                            'Enter duplicate values into the module API and confirm its output.',
                            'Type node --test in the shell and confirm all tests pass.',
                            'index.html exists.','mobile.js exists and exports a responsive API.']:
            self.assertEqual(set(),browser_criteria({'VERIFY:1':requirement}))

    def test_playwright_matchers_require_an_executed_assertion(self):
        result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':
            'await expect(page.locator("button")).toBeVisible();await expect(page.locator("button")).not.toHaveText("wrong");'}])
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':'expect(page.locator("button"));'}])
        self.assertNotEqual(0,result.returncode)
        self.assertIn('no observed assertions',result.stdout)

    def test_generated_checks_cannot_use_child_process_for_mutation(self):
        result=self.execute([{'ids':['VERIFY:1'],'kind':'NODE','code':"require('child_process').execFileSync('git',['commit']);assert.ok(true);"}])
        self.assertNotEqual(0,result.returncode)
        self.assertIn('Readonly verification supports',result.stdout)

    def test_json_extension_and_spaces_in_exact_paths_are_not_misparsed_as_js(self):
        from olcr_api.coding_tasks import _criterion_paths
        paths={'/tmp/project with spaces/package.json','/tmp/project with spaces/test.test.js'}
        self.assertEqual(['package.json'],_criterion_paths('package.json exists.',paths))
        self.assertEqual(['/tmp/project with spaces/package.json'],
                         _criterion_paths('/tmp/project with spaces/package.json exists.',paths))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.root = Path(self.directory.name)
        (self.root / 'index.html').write_text('<textarea id="input"></textarea><button id="select">選ぶ</button><output id="result"></output><script src="app.js"></script>')
        (self.root / 'app.js').write_text("document.querySelector('#select').onclick=()=>document.querySelector('#result').textContent=document.querySelector('#input').value.split('\\n')[0];")

    def tearDown(self):
        self.directory.cleanup()

    def execute(self, checks, **metadata):
        contract = self.root / 'contract.json'
        contract.write_text(json.dumps({'entrypoint':'index.html', 'checks':checks,**metadata}))
        command = execution_command(contract, self.root)
        return subprocess.run(command, capture_output=True, text=True, timeout=30)

    def test_browser_check_observes_real_dom_and_rejects_a_wrong_result(self):
        code = "await page.locator('#input').fill('A\\nB');await page.locator('#select').click();assert.equal(await page.locator('#result').textContent(),'A');"
        ok = self.execute([{'ids':['VERIFY:1'], 'kind':'BROWSER', 'code':code}])
        self.assertEqual(0, ok.returncode, ok.stderr+ok.stdout)
        bad = self.execute([{'ids':['VERIFY:1'], 'kind':'BROWSER', 'code':code.replace(",'A');", ",'B');")}])
        self.assertNotEqual(0, bad.returncode)
        self.assertIn('ERR_ASSERTION', bad.stdout)
        failure=json.loads(bad.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])['failures'][0]
        self.assertIn('A',failure['observed_page']['text'])
        self.assertEqual("'B'",failure['assertion']['expected'])
        self.assertIn('assert.equal',failure['assertion_source']['snippet'])

    def test_clicking_native_option_uses_real_select_semantics(self):
        (self.root/'index.html').write_text('''<select id="meal">
            <option value="breakfast">Breakfast</option>
            <option value="dinner">Dinner</option>
          </select><output id="selected"></output><script src="app.js"></script>''')
        (self.root/'app.js').write_text("document.querySelector('#meal').addEventListener('change',event=>document.querySelector('#selected').textContent=event.target.value);")
        result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':
            "await page.locator('#meal option[value=\"dinner\"]').click();await expect(page.locator('#selected')).toHaveText('dinner');"}])
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_unawaited_browser_results_cannot_pass_or_blame_the_application(self):
        for code in ["assert.ok(page.locator('#absent').count());",
                     "assert.equal(page.locator('#input').count()===1,true);",
                     "page.evaluate(()=>{throw new Error('discarded check rejection')});assert.ok(true);"]:
            with self.subTest(code=code):
                result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':code}])
                self.assertNotEqual(0,result.returncode)
                value=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
                self.assertNotIn('VERIFY:1',value['passed_ids'])
                self.assertTrue(value['failures'][0]['contract_error'])
                self.assertTrue(value['failures'][0]['unawaited_api_calls'])
                self.assertFalse(value['failures'][0].get('project_source_error'))

    def test_awaited_browser_results_and_matchers_keep_the_real_api(self):
        for code in ["assert.equal(await page.locator('#input').count(),1);",
                     "const text=page.locator('#select').textContent();assert.equal(await text,'選ぶ');",
                     "await expect(page.locator('#select')).toHaveText('選ぶ');",
                     "const [visible,text]=await Promise.all([page.locator('#input').isVisible(),page.locator('#select').textContent()]);assert.ok(visible);assert.equal(text,'選ぶ');",
                     "await assert.rejects(page.evaluate(()=>{throw new Error('check rejection')}),/check rejection/);"]:
            with self.subTest(code=code):
                result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':code}])
                self.assertEqual(0,result.returncode,result.stderr+result.stdout)

    def test_repeated_reads_or_skipped_clicks_are_not_a_repeat_counterexample(self):
        (self.root/'index.html').write_text('<textarea id="input"></textarea><button id="select">Choose</button><output id="result"></output><script src="app.js"></script>')
        (self.root/'app.js').write_text("let step=0;document.querySelector('#select').onclick=()=>document.querySelector('#result').textContent=['A','B','A','C','B','C','B','A'][step++%8];")
        spec={'ids':['VERIFY:1'],'button_labels':['Choose']}
        for code in [
            "await page.locator('#input').fill('A\\nB\\nC');for(let i=0;i<8;i++)await page.locator('#select').click();const a=await page.locator('#result').textContent();const b=await page.locator('#result').textContent();assert.notEqual(a,b);",
            "await page.locator('#input').fill('A\\nB\\nC');const values=[];for(let i=0;i<8;i++){await page.locator('#select').click();if(i%2===0)values.push(await page.locator('#result').textContent());}assert.notEqual(values[2],values[3]);"]:
            with self.subTest(code=code):
                result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':code}],selection_exclusion=spec)
                self.assertNotEqual(0,result.returncode)
                value=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
                self.assertFalse(value['failures'][0].get('observed_no_repeat'))

    def test_console_errors_fail_even_when_generated_listener_is_wrong(self):
        (self.root/'app.js').write_text("console.error('observed app error');")
        result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':"assert.equal((await page.locator('button').allTextContent())[0],'選ぶ');"}])
        self.assertNotEqual(0,result.returncode)
        self.assertIn('observed app error',result.stdout)

    def test_actual_browser_exception_survives_a_failing_generated_assertion(self):
        (self.root/'app.js').write_text("document.querySelector('#select').onclick=()=>missingApplicationFunction();")
        result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':
            "await page.locator('#select').click();assert.equal(await page.locator('#result').textContent(),'A');"}])
        self.assertNotEqual(0,result.returncode)
        payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
        failure=payload['failures'][0]
        self.assertTrue(failure.get('project_source_error'),json.dumps(payload,ensure_ascii=False))
        self.assertIn('missingApplicationFunction',failure['error'])
        self.assertIn('app.js',failure['browser_errors'][0]['source_paths'])
        frame=failure['browser_errors'][0]['source_locations'][0]
        self.assertEqual(('app.js',1),(frame['path'],frame['line']))
        self.assertIn('missingApplicationFunction',frame['source_line'])

    def test_check_evaluation_error_is_not_attributed_to_application_source(self):
        result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':
            'await page.evaluate(()=>missingCheckVariable());assert.ok(true);'}])
        self.assertNotEqual(0,result.returncode)
        failure=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])['failures'][0]
        self.assertFalse(failure.get('project_source_error',False))

    def test_observation_cannot_change_dom_or_css_to_make_a_check_pass(self):
        for observation in [
                "await page.evaluate(()=>{document.body.style.overflow='hidden';return true;});",
                "await page.locator('button').evaluate(el=>{el.textContent='fake';return true;});",
                "await page.locator('button').evaluateAll(els=>{els[0].textContent='fake';return true;});",
                "await page.$eval('button',el=>{el.textContent='fake';return true;});",
                "await page.$$eval('button',els=>{els[0].textContent='fake';return true;});"]:
            with self.subTest(observation=observation):
                result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER',
                    'code':observation+'assert.ok(true);'}])
                self.assertNotEqual(0,result.returncode)
                payload=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
                self.assertTrue(payload['failures'][0]['contract_error'])
                self.assertIn('modified the DOM',payload['failures'][0]['error'])

    def test_async_observation_cannot_escape_dom_guard_and_direct_page_replacement_is_rejected(self):
        for code in ["await page.evaluate(async()=>{await Promise.resolve();document.body.textContent='fake';return true;});",
                     "await page.locator('button').evaluateAll(async els=>{await Promise.resolve();els[0].textContent='fake';});",
                     "await page.setContent('<button>fake</button>');",
                     "await page.addStyleTag({content:'body {overflow:hidden}'});",
                     "await page.route('**/*',route=>route.fulfill({body:'fake'}));"]:
            with self.subTest(code=code):
                result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':code+'assert.ok(true);'}])
                self.assertNotEqual(0,result.returncode)
                value=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])
                self.assertTrue(value['failures'][0]['contract_error'])
                self.assertNotIn('VERIFY:1',value['passed_ids'])

    def test_observations_reject_property_cssom_storage_and_scheduled_mutations(self):
        (self.root/'index.html').write_text('<style>#result{display:none}</style>'
            '<input id="input" value="broken"><output id="result">original</output>')
        for callback in [
                "()=>{document.querySelector('#input').value='fake';return true;}",
                "()=>{const field=document.querySelector('#input');field.value='fake';return true;}",
                "()=>{document.styleSheets[0].insertRule('#result{display:block}',1);return true;}",
                "()=>{try{document.styleSheets[0].replace('#result{display:block}')}catch{}return true;}",
                "()=>{localStorage.setItem('result','fake');return true;}",
                "()=>{document.querySelector('#input').dispatchEvent(new Event('input'));return true;}",
                "()=>{Object.defineProperty(document.querySelector('#input'),'value',{value:'fake'});return true;}",
                "()=>{setTimeout(()=>document.querySelector('#input').value='fake',0);return true;}",
                "()=>{Promise.resolve().then(()=>document.querySelector('#input').value='fake');return true;}"]:
            with self.subTest(callback=callback):
                result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER',
                    'code':'await page.evaluate('+callback+');assert.ok(true);'}])
                self.assertNotEqual(0,result.returncode,result.stdout)
                failure=json.loads(result.stdout.split('OLCR_VERIFICATION_RESULT=',1)[1])['failures'][0]
                self.assertTrue(failure['contract_error'])
                self.assertFalse(failure.get('project_source_error'))

    def test_observations_allow_real_reads_and_local_accumulators(self):
        result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':
            "const result=await page.locator('textarea,button,output').evaluateAll(els=>{"
            "const names=[];let count=0;for(const el of els){names.push(el.id);count++;}"
            "const summary={};summary.count=count;return {names,count:summary.count};});"
            "assert.deepEqual(result,{names:['input','select','result'],count:3});"}])
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_observations_preserve_string_reads_and_restore_native_input_behavior(self):
        result=self.execute([{'ids':['VERIFY:1'],'kind':'BROWSER','code':
            "await page.locator('#input').fill('first');"
            "assert.equal(await page.locator('#input').evaluate(el=>el.value.replace('first','read')), 'read');"
            "await page.locator('#input').fill('second');"
            "assert.equal(await page.locator('#input').inputValue(),'second');"}])
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_readonly_check_without_assertions_cannot_pass(self):
        result = self.execute([{'ids':['VERIFY:1'], 'kind':'NODE', 'code':"readFile('app.js');"}])
        self.assertNotEqual(0, result.returncode)
        self.assertIn('no observed assertions', result.stdout)

    def test_zero_node_tests_cannot_pass(self):
        (self.root / 'empty.test.js').write_text('// deliberately zero tests')
        result = self.execute([{'ids':['VERIFY:1'], 'kind':'NODE', 'code':"const r=await runTests();assert.ok(r.tests>0);"}])
        self.assertNotEqual(0, result.returncode)
        self.assertIn('node:test failed', result.stdout)

    def test_contract_must_cover_all_ids_and_an_in_workspace_entrypoint(self):
        value={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'NODE','code':'assert.equal(1,1)'}]}
        self.assertTrue(validate_verification(value,{'VERIFY:1':'check','VERIFY:2':'other'}, self.root))
        value['entrypoint']='../index.html'
        self.assertTrue(validate_verification(value,{'VERIFY:1':'check'}, self.root))

    def test_browser_criteria_cannot_be_satisfied_by_source_only_checks(self):
        value={'entrypoint':'index.html','checks':[{'ids':['VERIFY:1'],'kind':'NODE','code':"assert.ok(readFile('index.html').includes('button'));"}]}
        self.assertTrue(validate_verification(value,{'VERIFY:1':'Click the button in a browser and observe the result'},self.root))
        value['checks'][0]['kind']='BROWSER'
        self.assertEqual([],validate_verification(value,{'VERIFY:1':'Click the button in a browser and observe the result'},self.root))

    def test_file_writes_and_boolean_claims_do_not_prove_browser_or_function_calls(self):
        for text in ['Open index.html in a browser and check its result.',
                     'Call the selection function and confirm history never exceeds 5 entries.',
                     'Run a quick Node.js check that imports app.js.']:
            phase={'id':'p','done':['app.js exists'],'verify':[text]}
            report={'phase_id':'p','status':'PASS','criteria_evidence':{text:True},
                    'typed_execution_summary':{'operations':[{'tool':'workspace_write','status':'success','output':{'path':'app.js'}}]}}
            result=evaluate_phase(phase, report, workspace_root=str(self.root))
            self.assertFalse(result['phase_complete'])
            self.assertIn('VERIFY:1',result['required_not_run_ids'])

    def test_integration_verification_is_deferred_without_scope_or_checkpoint_changes(self):
        plan={'file_manifest':[{'path':'index.html'},{'path':'app.js'}], 'phases':[
            {'id':'ui','execution_mode':'IMPLEMENTATION','done':['index.html exists'],'verify':['Click the button'],'dependencies':[]},
            {'id':'logic','execution_mode':'IMPLEMENTATION','done':['app.js exists'],'verify':['Run an import check'],'dependencies':['ui']},
            {'id':'verify','execution_mode':'VERIFICATION_ONLY','done':['tests pass'],'verify':['Run tests'],'dependencies':['logic']}]}
        before=copy.deepcopy(plan)
        scheduled=normalize_static_verification(plan,verification_requested=True)
        self.assertEqual(before,plan)
        self.assertEqual(before['file_manifest'],scheduled['file_manifest'])
        self.assertTrue(scheduled['phases'][-1]['host_verification'])
        self.assertIn('Click the button',scheduled['phases'][-1]['verify'])
        self.assertNotIn('Click the button',scheduled['phases'][0]['verify'])
        self.assertEqual(scheduled,normalize_static_verification(scheduled,verification_requested=True))
        plan['phases'][0]['checkpoint_on_failure']=True
        self.assertEqual(plan,normalize_static_verification(plan,verification_requested=True))

    def test_natural_combined_plan_with_legacy_scope_gets_owned_files_and_real_final_checks(self):
        paths=['index.html','style.css','app.js','logic.js','logic.test.js']
        plan={'scope':{'allowed':paths,'forbidden':['package.json']},'phases':[
            {'id':'logic','goal':'Create logic.js and logic.test.js','execution_mode':'IMPLEMENTATION_AND_VERIFICATION',
             'done':['logic.js runs in Node.js','logic.test.js contains tests'],
             'verify':['Run Node.js require/import check'],'dependencies':[]},
            {'id':'ui','goal':'Create index.html, style.css and app.js','execution_mode':'IMPLEMENTATION_AND_VERIFICATION',
             'done':['UI works in browser'],'verify':['Click the button'],'dependencies':['logic']},
            {'id':'final','goal':'Run final tests','execution_mode':'VERIFICATION_ONLY',
             'done':['Tests pass'],'verify':['Browser works'],'dependencies':['ui']}]}
        original=copy.deepcopy(plan)
        result=normalize_static_verification(plan,str(self.root),verification_requested=True)
        self.assertEqual(original,plan)
        self.assertEqual(paths,[entry['path'] for entry in result['file_manifest']])
        self.assertEqual(['ui','ui','ui','logic','logic'],[entry['owner_phase'] for entry in result['file_manifest']])
        self.assertEqual(['logic.js exists','logic.test.js exists'],result['phases'][0]['done'])
        self.assertEqual('IMPLEMENTATION',result['phases'][0]['execution_mode'])
        self.assertTrue(result['phases'][-1]['host_verification'])
        self.assertIn('Run Node.js require/import check',result['phases'][-1]['verify'])
        self.assertEqual(result,normalize_static_verification(result,str(self.root),verification_requested=True))
        command=['node','explicit-check.cjs']
        plan['phases'][0]['verification_command']=command
        preserved=normalize_static_verification(plan,str(self.root),verification_requested=True)
        self.assertEqual(command,preserved['phases'][0]['verification_command'])
        self.assertEqual('IMPLEMENTATION_AND_VERIFICATION',preserved['phases'][0]['execution_mode'])

    def test_japanese_particles_do_not_hide_exact_file_ownership(self):
        plan={'scope':{'allowed':['index.html','style.css','app.js','lunch-roulette.js','lunch-roulette.test.js']},
              'phases':[
                  {'id':'logic','goal':'共有モジュールとテストを作成する。',
                   'execution_mode':'IMPLEMENTATION_AND_VERIFICATION','dependencies':[],
                   'done':['lunch-roulette.jsにAPIを定義する','lunch-roulette.test.jsはnode:testを使用する'],
                   'verify':['Node.jsでrequire/import可能であることを確認する']},
                  {'id':'ui','goal':'画面を作成する。','execution_mode':'IMPLEMENTATION_AND_VERIFICATION',
                   'dependencies':['logic'],'done':['index.htmlにDOMを追加する','style.cssで装飾する',
                       'app.jsがlunch-roulette.jsのAPIを呼び出す'],'verify':['ブラウザで動作確認する']},
                  {'id':'final','execution_mode':'VERIFICATION_ONLY','dependencies':['ui'],
                   'done':['すべてのテストが成功する'],'verify':[]}]}
        result=normalize_static_verification(plan,str(self.root),verification_requested=True)
        self.assertEqual(['ui','ui','ui','logic','logic'],[e['owner_phase'] for e in result['file_manifest']])
        self.assertEqual('IMPLEMENTATION',result['phases'][0]['execution_mode'])
        self.assertIn(plan['phases'][0]['verify'][0],result['phases'][-1]['verify'])
        self.assertEqual(result,normalize_static_verification(result,str(self.root),verification_requested=True))
        # A filename mentioned only as part of another path is not its owner.
        for reference in ('other-index.htmlにDOMを追加する','dir/index.htmlにDOMを追加する','index.html.bakにDOMを追加する'):
            invalid=copy.deepcopy(plan)
            invalid['phases'][1]['done'][0]=reference
            self.assertEqual(invalid,normalize_static_verification(invalid,str(self.root),verification_requested=True))

    def test_old_host_schedule_is_upgraded_without_duplicating_preparation_conditions(self):
        plan={'file_manifest':[{'path':'index.html','owner_phase':'p'},{'path':'app.js','owner_phase':'p'}],
              'phases':[{'id':'p','execution_mode':'IMPLEMENTATION','done':['index.html exists','app.js exists'],
                         'verify':['Typed workspace writes and read-back succeed for the changed files.'],'dependencies':[]},
                        {'id':'v','execution_mode':'VERIFICATION_ONLY','host_verification':True,
                         'host_verification_version':2,'done':['Tests pass'],'verify':['Click the button'],'dependencies':['p']}]}
        result=normalize_static_verification(plan,verification_requested=True)
        self.assertEqual(3,result['phases'][-1]['host_verification_version'])
        self.assertEqual(['Click the button'],result['phases'][-1]['verify'])
        self.assertEqual(result,normalize_static_verification(result,verification_requested=True))

    def test_static_verifier_does_not_take_over_framework_or_other_language_workflows(self):
        plan={'file_manifest':[{'path':'index.html'},{'path':'app.js'}],
              'phases':[{'id':'p','execution_mode':'IMPLEMENTATION','done':['app.js exists'],
                         'verify':['Check the application'],'dependencies':[]}]}
        (self.root/'package.json').write_text('{"devDependencies":{"vite":"1.0.0"}}')
        self.assertEqual(plan,normalize_static_verification(plan,str(self.root)))
        (self.root/'package.json').unlink()
        for source in ('server.py','server.rs','server.go','App.tsx'):
            mixed={**plan,'file_manifest':[*plan['file_manifest'],{'path':source}]}
            self.assertEqual(mixed,normalize_static_verification(mixed,str(self.root)))

    def test_node_runtime_dev_dependency_keeps_static_acceptance_and_project_tests(self):
        plan={'file_manifest':[
                  {'path':'index.html','owner_phase':'build'},
                  {'path':'app.js','owner_phase':'build'},
                  {'path':'tests/logic.test.js','owner_phase':'build'}],
              'phases':[
                  {'id':'build','goal':'Create the standalone web application and its tests',
                   'status':'pending','execution_mode':'IMPLEMENTATION',
                   'done':['index.html exists','app.js exists','tests/logic.test.js exists'],
                   'verify':['Run the project tests'],'dependencies':[],'risks':[]},
                  {'id':'final','goal':'Verify the implemented application',
                   'status':'pending','execution_mode':'VERIFICATION_ONLY',
                   'done':['The application works'],'verify':['Check the application'],
                   'dependencies':['build'],'risks':[]}]}
        (self.root/'package.json').write_text(
            '{"scripts":{"test":"node --test"},"devDependencies":{"node":"^20.0.0"}}')

        scheduled=normalize_static_verification(plan,str(self.root),verification_requested=True)

        self.assertTrue(scheduled['phases'][-1]['host_verification'])
        self.assertEqual(['node','--test','tests/logic.test.js'],
                         scheduled['phases'][0]['verification_command'])
        self.assertEqual('IMPLEMENTATION_AND_VERIFICATION',
                         scheduled['phases'][0]['execution_mode'])

    def test_plan_without_final_verifier_gets_an_executable_acceptance_boundary(self):
        plan={'file_manifest':[{'path':'index.html','owner_phase':'p'},{'path':'app.js','owner_phase':'p'}],
              'phases':[{'id':'p','goal':'Implement app','execution_mode':'IMPLEMENTATION','status':'pending',
                         'done':['index.html exists','app.js exists'],'verify':['Click a button'],'dependencies':[],'risks':[]}]}
        result=normalize_static_verification(plan,verification_requested=True)
        self.assertEqual(2,len(result['phases']))
        self.assertEqual(['p'],result['phases'][-1]['dependencies'])
        self.assertTrue(result['phases'][-1]['host_verification'])
        self.assertIn('Click a button',result['phases'][-1]['verify'])
        self.assertEqual(result,normalize_static_verification(result,verification_requested=True))
        from olcr_api.coding_verification import standard_verification_checks
        from olcr_api.coding_verification import acceptance_criteria
        owned=standard_verification_checks(acceptance_criteria(result['phases'][-1]))
        self.assertTrue(any(check['kind']=='BROWSER' and 'DONE:1' in check['ids'] for check in owned))
        legacy=copy.deepcopy(result)
        legacy['phases'][-1]['done']=['Executable acceptance checks pass.']
        self.assertEqual(result,normalize_static_verification(legacy,verification_requested=True))


if __name__=='__main__':unittest.main()
