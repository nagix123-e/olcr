"""Recovery transport must preserve source, scope and real behavior."""
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from olcr_api.runtime import Runtime
from olcr_api.source_repair import compile_repair, repair_schema, observe_test_locals
import test_runtime_managed_context as runtime_fixtures


class SourceRepairCompilerTests(unittest.TestCase):
    def test_html_text_boundaries_are_checked_without_rejecting_foreign_markup_or_script_strings(self):
        for tag in ('textarea','script','style','title','iframe','xmp','noembed','noframes'):
            with self.subTest(tag=tag):
                errors=[]
                self.assertFalse(Runtime._structural_ok(Path('page.html'),f'<{tag} /><button>Action</button>',{},errors))
                self.assertIn('requires an explicit',errors[0])
                self.assertTrue(Runtime._structural_ok(Path('page.html'),f'<{tag}></{tag}><button>Action</button>',{}))
        self.assertTrue(Runtime._structural_ok(Path('page.html'),'<svg><title /></svg><input /><br />',{}))
        self.assertTrue(Runtime._structural_ok(Path('page.html'),'<script>const sample="<textarea/>";</script>',{}))

    def test_obsolete_binding_can_be_deleted_without_replacing_neighboring_lines(self):
        source='function connect() {}\nif (true) {\n const connect = connect;\n connect();\n}\n'
        payload,_=compile_repair({'reason':'Remove the shadowing alias','edits':[
            {'action':'delete','path':'ui.js','start_line':3,'end_line':3}]},{'ui.js':source})
        self.assertEqual('function connect() {}\nif (true) {\n connect();\n}\n',payload['operations'][0]['content'])

    def test_multiple_original_ranges_preserve_every_other_line_and_unicode(self):
        sources={'logic.py':'# 元の状態\r\nvalue = 1\r\n\r\nprint(value)\r\n'}
        payload,evidence=compile_repair({'reason':'Fix value and output','edits':[
            {'path':'logic.py','start_line':2,'end_line':2,'replacement':'value = 2'},
            {'path':'logic.py','start_line':4,'end_line':4,'replacement':'print(value + 1)'}]},sources)
        self.assertEqual('# 元の状態\r\nvalue = 2\r\n\r\nprint(value + 1)\r\n',payload['operations'][0]['content'])
        self.assertEqual(2,len(evidence))
        self.assertEqual(64,len(evidence[0]['snapshot_sha256']))
        self.assertEqual('value = 1',sources['logic.py'].splitlines()[1])

    def test_invalid_targets_ranges_overlap_and_unchanged_edits_fail_closed(self):
        sources={'logic.py':'value = 1\nprint(value)\n'}
        valid={'path':'logic.py','start_line':1,'end_line':1,'replacement':'value = 2'}
        cases=[{**valid,'path':'../outside.py'},{**valid,'start_line':0},
               {**valid,'end_line':3},{**valid,'start_line':2,'end_line':1},
               {**valid,'start_line':True},{**valid,'replacement':'value = 1'},
               {**valid,'content':'unexpected field'}]
        for edit in cases:
            with self.subTest(edit=edit),self.assertRaises(ValueError):
                compile_repair({'reason':'fix','edits':[edit]},sources)
        with self.assertRaisesRegex(ValueError,'overlap'):
            compile_repair({'reason':'fix','edits':[valid,valid]},sources)
        branches=repair_schema(sources)['properties']['edits']['items']['anyOf']
        self.assertEqual(['logic.py'],sorted({b['properties']['path']['const'] for b in branches}))


class SourceRepairRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.harness=runtime_fixtures.RealRuntimeManagedContextTests()
        self.harness.setUp()
        self.addCleanup(self.harness.tearDown)
        self.workspace=self.harness.workspace
        (self.workspace/'logic.py').write_text('value = 1\n# preserve this\n')
        self.context={**self.harness.managed_context,'coding_model':'role:long_implementation',
            'coding_diagnostic_model':'role:short_coding_fix_debug',
            'implementation_plan_artifact':{'file_manifest':[{'path':'logic.py','action':'modify'}]},
            'implementation_contract':{'original_request':'Expose value=2 and preserve other lines'},
            'repair_diagnostics':{'typed_failure_class':'PREIMAGE_MISMATCH',
                'verification_failures':[{'name':'actual value','detail':'Expected 2, got 1'}]}}

    def execute(self, edits, side_effect=None):
        calls=[]
        class Provider:
            def generate(inner,messages,model,**kwargs):
                calls.append((messages,model,kwargs))
                if side_effect:side_effect()
                return {'text':json.dumps({'reason':'Correct the actual exported value','edits':edits})}
        runtime=Runtime(self.harness.runtime.settings,self.harness.db,self.harness.runtime.retrieval,Provider())
        task,response=runtime.execute('Repair observed failure',workspace_root=str(self.workspace),managed_context=self.context)
        return task,response,calls

    def test_structural_failure_uses_only_diagnosed_path_and_bounded_line_edits(self):
        source='const renderDishList=()=>1;\nconst renderDishList=()=>2;\n'
        target=self.workspace/'src'/'ui.js'
        target.parent.mkdir()
        target.write_text(source)
        package=self.workspace/'package.json'
        package.write_text('{"type":"commonjs"}\n')
        test_file=self.workspace/'tests'/'logic.test.js'
        test_file.parent.mkdir()
        test_file.write_text("require('node:test')('smoke',()=>{});\n")
        calls=[]
        class Provider:
            def generate(inner,messages,model,**kwargs):
                calls.append(kwargs)
                return {'text':json.dumps({'reason':'Remove the duplicate declaration',
                    'edits':[{'action':'delete','path':'src/ui.js','start_line':2,'end_line':2}]})}
        runtime=Runtime(self.harness.runtime.settings,self.harness.db,self.harness.runtime.retrieval,Provider())
        context={**self.harness.managed_context,'coding_model':'role:long_implementation',
            'implementation_plan_artifact':{'file_manifest':[
                {'path':'src/ui.js','action':'modify','owner_phase':'p1'},
                {'path':'package.json','action':'modify','owner_phase':'p1'},
                {'path':'tests/logic.test.js','action':'modify','owner_phase':'p1'}]},
            'approved_scopes':[{'requested_scope':['src/ui.js','package.json','tests/logic.test.js']}],
            'implementation_contract':{'phase_id':'p1','original_request':'Fix src/ui.js'},
            'repair_diagnostics':{'typed_failure_class':'WRITE_CONTENT_INVALID',
                'invalid_source_paths':['src/ui.js','../outside.js'],
                'typed_error':'SyntaxError: Identifier renderDishList has already been declared'}}
        task,response=runtime.execute('Fix the duplicate declaration in src/ui.js',
            workspace_root=str(self.workspace),managed_context=context)
        self.assertEqual('completed',task.state.value,response)
        self.assertEqual(1,len(calls))
        schema=calls[0]['format']
        self.assertIn('edits',schema['properties'])
        path_values={branch['properties']['path']['const'] for branch in schema['properties']['edits']['items']['anyOf']}
        self.assertEqual({'src/ui.js'},path_values)
        self.assertEqual(4096,calls[0]['max_tokens'])
        self.assertIs(False,calls[0]['think'])
        self.assertEqual('const renderDishList=()=>1;\n',target.read_text())
        self.assertEqual('{"type":"commonjs"}\n',package.read_text())
        self.assertEqual("require('node:test')('smoke',()=>{});\n",test_file.read_text())
        transport=next(op for op in task.tool_executions if op['tool']=='source_repair_transport')
        self.assertEqual(['src/ui.js'],transport['input']['paths'])

    def test_structural_failure_outside_approved_manifest_stops_before_model_call(self):
        source='const renderDishList=()=>1;\nconst renderDishList=()=>2;\n'
        target=self.workspace/'src'/'ui.js'
        target.parent.mkdir()
        target.write_text(source)
        calls=[]
        class Provider:
            def generate(inner,messages,model,**kwargs):
                calls.append(kwargs)
                raise AssertionError('unapproved structural target must not reach the model')
        runtime=Runtime(self.harness.runtime.settings,self.harness.db,self.harness.runtime.retrieval,Provider())
        context={**self.harness.managed_context,
            'implementation_plan_artifact':{'file_manifest':[{'path':'src/ui.js','action':'modify','owner_phase':'p1'}]},
            'approved_scopes':[{'requested_scope':['src/ui.js']}],
            'implementation_contract':{'phase_id':'p1','original_request':'Fix src/ui.js'},
            'repair_diagnostics':{'typed_failure_class':'WRITE_CONTENT_INVALID',
                'invalid_source_paths':['../outside.js'],'typed_error':'SyntaxError'}}
        task,response=runtime.execute('Fix the duplicate declaration',workspace_root=str(self.workspace),managed_context=context)
        self.assertEqual('failed',task.state.value)
        self.assertIn('STRUCTURAL_REPAIR_SCOPE_UNAVAILABLE',task.error)
        self.assertEqual([],calls)
        self.assertEqual(source,target.read_text())

    def test_actual_node_assertion_values_are_observed_without_changing_the_project(self):
        source="const test=require('node:test');\nconst assert=require('node:assert/strict');\ntest('order',()=>{\n const given=['A','B','C'];\n const actual=[...given].reverse();\n const secretToken='must not be captured';\n assert.deepEqual(actual,given);\n});\n"
        path=self.workspace/'order.test.cjs';path.write_text(source)
        state=observe_test_locals(self.workspace,{'order.test.cjs':source},[
            {'source_locations':[{'path':'order.test.cjs','line':7}]}])
        self.assertEqual('ISOLATED_SOURCE_COPY',state[0]['probe_origin'])
        self.assertEqual('A',state[0]['locals']['given']['entries']['0'])
        self.assertEqual('C',state[0]['locals']['actual']['entries']['0'])
        self.assertNotIn('secretToken',state[0]['locals'])
        self.assertEqual(source,path.read_text())

    def test_syntax_error_gets_one_correction_before_any_source_write(self):
        source='const value=1;\nmodule.exports={value};\n'
        target=self.workspace/'logic.cjs';target.write_text(source)
        self.context['implementation_plan_artifact']['file_manifest']=[{'path':'logic.cjs','action':'modify'}]
        calls=[]
        class Provider:
            def generate(inner,messages,model,**kwargs):
                calls.append(messages)
                self.assertEqual(source,target.read_text())
                if len(calls)==1:
                    edit={'path':'logic.cjs','start_line':2,'end_line':2,'replacement':'const value=2;'}
                else:
                    self.assertIn('already been declared',messages[-1]['content'])
                    self.assertIn('1: const value=1;',messages[-1]['content'])
                    self.assertNotIn('REJECTED_PROPOSAL:',messages[-1]['content'])
                    edit={'path':'logic.cjs','start_line':1,'end_line':1,'replacement':'const value=2;'}
                return {'text':json.dumps({'reason':'Fix value','edits':[edit]})}
        runtime=Runtime(self.harness.runtime.settings,self.harness.db,self.harness.runtime.retrieval,Provider())
        task,response=runtime.execute('Repair current failure',workspace_root=str(self.workspace),managed_context=self.context)
        self.assertEqual('completed',task.state.value,response)
        self.assertEqual(2,len(calls))
        self.assertEqual('const value=2;\nmodule.exports={value};\n',target.read_text())
        self.assertEqual('IMPLEMENTATION_SOURCE_REPAIR_CORRECTION',task.model_calls[-1]['stage'])

    def test_javascript_syntax_check_uses_bundled_node_when_path_has_no_node(self):
        system_node=shutil.which('node')
        if not system_node:self.skipTest('Node.js is required for this parser regression')
        resources=self.workspace/'node-tools'
        bundled_node=resources/'node'/'bin'/'node'
        bundled_node.parent.mkdir(parents=True)
        bundled_node.symlink_to(system_node)
        source="""function loadApp(implPath) {
  const implPath = require.resolve(implPath);
  return implPath;
}
"""
        diagnostics=[]
        with patch('olcr_api.runtime.shutil.which',return_value=None), \
             patch('olcr_api.node_mcp_runtime.runtime_root',return_value=resources):
            valid=Runtime._structural_ok(self.workspace/'tests/app.test.js',source,{},diagnostics)
        self.assertFalse(valid)
        self.assertTrue(any('Identifier \'implPath\' has already been declared' in item for item in diagnostics),diagnostics)

    def test_test_module_loading_repair_is_focused_and_disables_reasoning(self):
        (self.workspace/'tests').mkdir()
        source="""const { test } = require('node:test');
function loadApp(implPath) {
  return require(implPath);
}
test('loads the application', () => {});
"""
        target=self.workspace/'tests/app.test.js';target.write_text(source)
        self.context['implementation_plan_artifact']['file_manifest']=[{'path':'tests/app.test.js','action':'modify'}]
        self.context['implementation_contract']['original_request']='Keep the meal planner behavior and repair its test harness.'
        self.context['repair_diagnostics'].update(source_bound_repair=True,project_test_setup_repair=True,
            verification_failures=[{'name':'Node.js module loading',
                'detail':'TypeError [ERR_INVALID_ARG_TYPE]: The "path" argument must be of type string. Received an instance of Object',
                'source_locations':[{'path':'tests/app.test.js','line':3}]}])
        calls=[]
        class Provider:
            def generate(inner,messages,model,**kwargs):
                calls.append((messages,model,kwargs))
                return {'text':json.dumps({'reason':'Resolve the input path before requiring the application',
                    'edits':[{'path':'tests/app.test.js','start_line':3,'end_line':3,
                        'replacement':'  const resolvedImplPath = require.resolve(implPath);\n  return require(resolvedImplPath);'}]})}
        runtime=Runtime(self.harness.runtime.settings,self.harness.db,self.harness.runtime.retrieval,Provider())
        configured_on={'mode':'split','efforts':{'brain':'auto','coder':'on','short_coding':'on','general_inference':'auto'}}
        with patch('olcr_api.brain_models.active_configuration',return_value=configured_on):
            task,response=runtime.execute('Repair the observed project-test loading failure',
                workspace_root=str(self.workspace),managed_context=self.context)
        self.assertEqual('completed',task.state.value,response)
        self.assertFalse(calls[0][2]['think'])
        self.assertTrue(calls[0][2]['force_no_thinking'])
        prompt=calls[0][0][-1]['content']
        self.assertIn('PROJECT_TEST_SETUP_REPAIR',prompt)
        self.assertIn('Module._load expects a string request',calls[0][0][0]['content'])
        self.assertIn('Do not copy or reimplement application behavior in the test file',calls[0][0][0]['content'])
        self.assertNotIn('TEST_ORACLE_RULE',prompt)
        self.assertIn('const resolvedImplPath = require.resolve(implPath);',target.read_text())

    def test_undefined_reference_in_test_only_frames_uses_test_harness_repair(self):
        (self.workspace/'tests').mkdir()
        source="""const { test } = require('node:test');
function buildPlan() { save(); }
test('build plan', () => buildPlan());
"""
        target=self.workspace/'tests/app.test.js';target.write_text(source)
        self.context['implementation_plan_artifact']['file_manifest']=[{'path':'tests/app.test.js','action':'modify'}]
        self.context['implementation_contract']['original_request']='Keep the application behavior and repair the project test harness.'
        self.context['repair_diagnostics'].update(source_bound_repair=True,verification_failures=[{
            'name':'recipe model: empty state','detail':'ReferenceError: save is not defined',
            'source_locations':[{'path':'tests/app.test.js','line':2}]}])
        calls=[]
        class Provider:
            def generate(inner,messages,model,**kwargs):
                calls.append((messages,kwargs))
                return {'text':json.dumps({'reason':'Use the actual application surface in this test',
                    'edits':[{'path':'tests/app.test.js','start_line':2,'end_line':2,
                        'replacement':'function buildPlan() { return []; }'}]})}
        runtime=Runtime(self.harness.runtime.settings,self.harness.db,self.harness.runtime.retrieval,Provider())
        task,response=runtime.execute('Repair the observed project-test failure',
            workspace_root=str(self.workspace),managed_context=self.context)
        self.assertEqual('completed',task.state.value,response)
        self.assertFalse(calls[0][1]['think'])
        self.assertTrue(calls[0][1]['force_no_thinking'])
        prompt='\n'.join(message['content'] for message in calls[0][0])
        self.assertIn('PROJECT_TEST_SETUP_REPAIR',prompt)
        self.assertIn('Do not copy or reimplement application behavior in the test file',prompt)

    def test_temporal_dead_zone_repair_prompt_identifies_the_uninitialized_binding(self):
        (self.workspace/'tests').mkdir()
        source="""const { test } = require('node:test');
function loadApp() {
  const module = App;
  return module;
}
const App = loadApp();
test('loads the application', () => {});
"""
        target=self.workspace/'tests/app.test.js';target.write_text(source)
        self.context['implementation_plan_artifact']['file_manifest']=[{'path':'tests/app.test.js','action':'modify'}]
        self.context['implementation_contract']['original_request']='Preserve application behavior and repair test startup.'
        self.context['repair_diagnostics'].update(source_bound_repair=True,verification_failures=[{
            'name':'Node.js module loading',
            'detail':"ReferenceError: Cannot access 'App' before initialization\n    at loadApp (tests/app.test.js:3:18)",
            'source_locations':[{'path':'tests/app.test.js','line':3}]}])
        calls=[]
        class Provider:
            def generate(inner,messages,model,**kwargs):
                calls.append((messages,kwargs))
                return {'text':json.dumps({'reason':'Initialize the public API after loading the source',
                    'edits':[{'path':'tests/app.test.js','start_line':3,'end_line':3,
                        'replacement':'  return sandbox.MEAL_PLANNER;'}]})}
        runtime=Runtime(self.harness.runtime.settings,self.harness.db,self.harness.runtime.retrieval,Provider())
        task,response=runtime.execute('Repair the observed project-test startup failure',
            workspace_root=str(self.workspace),managed_context=self.context)
        self.assertEqual('completed',task.state.value,response)
        self.assertFalse(calls[0][1]['think'])
        self.assertTrue(calls[0][1]['force_no_thinking'])
        prompt='\n'.join(message['content'] for message in calls[0][0])
        self.assertIn('TEMPORAL_DEAD_ZONE_REPAIR',prompt)
        self.assertIn('binding "App"',prompt)
        self.assertIn('before initialization',prompt)

    def test_mixed_app_and_test_creation_receives_approved_runner_contract(self):
        app,test='lib.cjs','tests/lib.test.cjs'
        self.context.update(implementation_plan_artifact={'file_manifest':[
            {'path':name,'action':'create'} for name in (app,test)]},repair_diagnostics={},
            implementation_contract={'original_request':'Create a library with working tests.'},
            project_verification_command=['node','--test',test])
        prompts=[]
        class Provider:
            def generate(inner,messages,model,**kwargs):
                prompts.append(messages[-1]['content'])
                return {'text':json.dumps({'change_required':True,'source_inspected':True,
                    'condition_evaluated':True,'reason_code':'requested_change','operations':[
                        {'op':'write','path':app,'content':'module.exports=1;'},
                        {'op':'write','path':test,'content':"require('node:test')('library',()=>require('node:assert/strict').equal(require('../lib.cjs'),1));"}]})}
        runtime=Runtime(self.harness.runtime.settings,self.harness.db,self.harness.runtime.retrieval,Provider())
        task,response=runtime.execute('Implement the library and its tests',workspace_root=str(self.workspace),managed_context=self.context)
        self.assertEqual('completed',task.state.value,response)
        self.assertIn('The approved verifier is node --test',prompts[0])
        self.assertIn('no Jest globals',prompts[0])

    def test_test_authoring_phase_forces_no_thinking_even_if_coder_effort_is_on(self):
        target='tests/app.test.js'
        self.context['implementation_plan_artifact']['file_manifest']=[{'path':target,'action':'create'}]
        self.context['implementation_contract']={'original_request':'Create a focused Node.js test file.'}
        self.context['repair_diagnostics']={}
        calls=[]
        prompts=[]
        class Provider:
            def generate(inner,messages,model,**kwargs):
                calls.append(kwargs)
                prompts.append(messages)
                return {'text':json.dumps({'change_required':True,'source_inspected':True,
                    'condition_evaluated':True,'reason_code':'requested_change','operations':[
                        {'op':'write','path':target,'content':"const { test } = require('node:test');\n"}]})}
        runtime=Runtime(self.harness.runtime.settings,self.harness.db,self.harness.runtime.retrieval,Provider())
        configured_on={'mode':'split','efforts':{'brain':'auto','coder':'on','short_coding':'on','general_inference':'auto'}}
        with patch('olcr_api.brain_models.active_configuration',return_value=configured_on):
            task,response=runtime.execute('Create the planned project tests',
                workspace_root=str(self.workspace),managed_context=self.context)
        self.assertEqual('completed',task.state.value,response)
        self.assertFalse(calls[0]['think'])
        self.assertTrue(calls[0]['force_no_thinking'])
        self.assertTrue((self.workspace/target).is_file())
        prompt='\n'.join(message['content'] for message in prompts[0])
        self.assertIn('PROJECT_TEST_AUTHORING',prompt)
        self.assertIn('Do not copy or reimplement application behavior in the test file',prompt)

    def test_application_path_type_error_does_not_get_test_loader_repair_instructions(self):
        (self.workspace/'tests').mkdir()
        app=self.workspace/'app.js';app.write_text('const value = 1;\nmodule.exports = {value};\n')
        test=self.workspace/'tests/app.test.js';test.write_text("require('../app.js');\n")
        self.context['implementation_plan_artifact']['file_manifest']=[
            {'path':'app.js','action':'modify'},{'path':'tests/app.test.js','action':'modify'}]
        self.context['repair_diagnostics'].update(source_bound_repair=True,verification_failures=[{
            'name':'planner suite','detail':'TypeError [ERR_INVALID_ARG_TYPE]: The "path" argument must be of type string. Received an instance of Object',
            'source_locations':[{'path':'app.js','line':1},{'path':'tests/app.test.js','line':1}]}])
        prompts=[]
        class Provider:
            def generate(inner,messages,model,**kwargs):
                prompts.append(messages)
                return {'text':json.dumps({'reason':'Correct the application value',
                    'edits':[{'action':'replace','path':'app.js','start_line':1,'end_line':1,
                        'replacement':'const value = 2;'}]})}
        runtime=Runtime(self.harness.runtime.settings,self.harness.db,self.harness.runtime.retrieval,Provider())
        task,response=runtime.execute('Repair the observed runtime exception',
            workspace_root=str(self.workspace),managed_context=self.context)
        self.assertEqual('completed',task.state.value,response)
        prompt='\n'.join(message['content'] for message in prompts[0])
        self.assertNotIn('PROJECT_TEST_SETUP_REPAIR',prompt)
        self.assertNotIn('Module._load expects a string request',prompt)

    def test_raw_text_html_boundary_gets_corrected_before_controls_can_be_swallowed(self):
        source='<textarea id="items"></textarea>\n<button>Apply</button>\n'
        target=self.workspace/'index.html';target.write_text(source)
        self.context['implementation_plan_artifact']['file_manifest']=[{'path':'index.html','action':'modify'}]
        calls=[]
        class Provider:
            def generate(inner,messages,model,**kwargs):
                calls.append((messages,kwargs))
                self.assertEqual(source,target.read_text())
                if len(calls)==2:self.assertIn('requires an explicit </textarea>',messages[-1]['content'])
                replacement='<textarea id="items" rows="4"'+(' />'if len(calls)==1 else '></textarea>')
                return {'text':json.dumps({'reason':'Resize the actual text control','edits':[
                    {'action':'replace','path':'index.html','start_line':1,'end_line':1,'replacement':replacement}]})}
        runtime=Runtime(self.harness.runtime.settings,self.harness.db,self.harness.runtime.retrieval,Provider())
        task,response=runtime.execute('Repair current failure',workspace_root=str(self.workspace),managed_context=self.context)
        self.assertEqual('completed',task.state.value,response)
        self.assertEqual(2,len(calls))
        self.assertTrue(all(call[1]['force_no_thinking'] for call in calls))
        self.assertTrue(all(call[1]['think'] is False for call in calls))
        self.assertEqual('<textarea id="items" rows="4"></textarea>\n<button>Apply</button>\n',target.read_text())

    def test_rejected_patch_recovers_with_one_grounded_diagnosis_and_actual_source_change(self):
        task,response,calls=self.execute([{'path':'logic.py','start_line':1,'end_line':1,'replacement':'value = 2'}])
        self.assertEqual('completed',task.state.value,response)
        self.assertEqual('value = 2\n# preserve this\n',(self.workspace/'logic.py').read_text())
        self.assertEqual(['role:long_implementation'],[c[1] for c in calls])
        self.assertEqual('IMPLEMENTATION_SOURCE_REPAIR',task.model_calls[0]['stage'])
        self.assertIn('1: value = 1',calls[0][0][-1]['content'])
        self.assertEqual('OUTSIDE_SENTINEL_ORIGINAL',self.harness.sentinel.read_text())

    def test_concurrent_edit_is_not_overwritten_by_compiled_full_write(self):
        task,response,_=self.execute([{'path':'logic.py','start_line':1,'end_line':1,'replacement':'value = 2'}],
            side_effect=lambda:(self.workspace/'logic.py').write_text('value = 99\n# user edit\n'))
        self.assertEqual('failed',task.state.value)
        self.assertIn('preimage mismatch',response)
        self.assertEqual('value = 99\n# user edit\n',(self.workspace/'logic.py').read_text())

    def test_crlf_snapshot_does_not_report_a_false_concurrent_edit(self):
        (self.workspace/'logic.py').write_bytes(b'value = 1\r\n# preserve this\r\n')
        task,response,_=self.execute([{'path':'logic.py','start_line':1,'end_line':1,'replacement':'value = 2'}])
        self.assertEqual('completed',task.state.value,response)
        self.assertEqual(b'value = 2\r\n# preserve this\r\n',(self.workspace/'logic.py').read_bytes())

    def test_noop_or_unauthorized_proposals_cannot_mutate_workspace(self):
        for edit in [{'path':'logic.py','start_line':1,'end_line':1,'replacement':'value = 1'},
                     {'path':'../sentinel-b.txt','start_line':1,'end_line':1,'replacement':'oops'}]:
            with self.subTest(edit=edit):
                task,response,_=self.execute([edit])
                self.assertEqual('failed',task.state.value,response)
                self.assertEqual('value = 1\n# preserve this\n',(self.workspace/'logic.py').read_text())
                self.assertEqual('OUTSIDE_SENTINEL_ORIGINAL',self.harness.sentinel.read_text())

    def test_browser_exceptions_survive_alongside_node_assertions(self):
        self.context['repair_diagnostics']['acceptance_failures']=[{'ids':['DONE:1'],
            'browser_errors':[{'message':'Missing application binding','source_paths':['ui.js']}]}]
        task,response,calls=self.execute([{'path':'logic.py','start_line':1,'end_line':1,'replacement':'value = 2'}])
        self.assertEqual('completed',task.state.value,response)
        prompt=calls[0][0][-1]['content']
        self.assertIn('Expected 2, got 1',prompt)
        self.assertIn('Missing application binding',prompt)

    def test_failed_ids_are_bound_to_the_actual_requirement_in_the_repair_prompt(self):
        self.context['implementation_contract']['verify']=['Allow independently editable input lines','Unrelated typography']
        self.context['repair_diagnostics'].update(verification_failures=[],
            acceptance_failures=[{'ids':['VERIFY:1'],'error':'Two supplied lines collapsed into one'},
                {'ids':['VERIFY:2'],'error':'Deferred unrelated visual failure'}])
        task,response,calls=self.execute([{'action':'replace','path':'logic.py','start_line':1,'end_line':1,'replacement':'value = 2'}])
        self.assertEqual('completed',task.state.value,response)
        prompt=calls[0][0][-1]['content']
        self.assertIn('"VERIFY:1": "Allow independently editable input lines"',prompt)
        self.assertNotIn('Unrelated typography',prompt)
        self.assertNotIn('Deferred unrelated visual failure',prompt)

    def test_format_switch_preserves_the_fresh_host_source_diagnosis(self):
        self.context['current_source_defect']={'review':{'verdict':'SOURCE_DEFECT',
            'reason':'The current initialization reads a shadowed binding.',
            'source_fragment':'value = 1'},'failures':[{'ids':['VERIFY:1'],'error':'Current observed counterexample'}]}
        task,response,calls=self.execute([{'action':'replace','path':'logic.py','start_line':1,'end_line':1,
            'replacement':'value = 2'}])
        self.assertEqual('completed',task.state.value,response)
        self.assertIn('The current initialization reads a shadowed binding.',calls[0][0][-1]['content'])
        self.assertIn('Current observed counterexample',calls[0][0][-1]['content'])

    def test_recovery_transport_stays_selected_after_a_rejected_proposal(self):
        del self.context['repair_diagnostics']['typed_failure_class']
        self.context['repair_diagnostics']['source_bound_repair']=True
        task,response,_=self.execute([{'path':'logic.py','start_line':1,'end_line':1,'replacement':'value = 2'}])
        self.assertEqual('completed',task.state.value,response)
        self.assertTrue(any(op['tool']=='source_repair_transport' for op in task.tool_executions))

    def test_browser_only_repair_cannot_revert_passing_tests_from_stale_history(self):
        (self.workspace/'test_logic.py').write_text('assert 1 == 1\n')
        self.context['implementation_plan_artifact']['file_manifest'].append({'path':'test_logic.py','action':'modify'})
        self.context['repair_diagnostics'].update(verification_failures=[],
            acceptance_failures=[{'browser_errors':[{'message':'Application initialization failed'}]}],
            previous_failed_repairs=[{'failures':[{'test':'old failure','error':'STALE_RESOLVED_TEST_EXPECTATION'}]}])
        task,response,calls=self.execute([{'path':'test_logic.py','start_line':1,'end_line':1,'replacement':'assert True'}])
        self.assertEqual('failed',task.state.value,response)
        self.assertEqual('assert 1 == 1\n',(self.workspace/'test_logic.py').read_text())
        branches=calls[0][2]['format']['properties']['edits']['items']['anyOf']
        self.assertEqual(['logic.py'],sorted({b['properties']['path']['const'] for b in branches}))
        self.assertNotIn('STALE_RESOLVED_TEST_EXPECTATION',calls[0][0][-1]['content'])

    def test_node_test_can_repair_markup_it_actually_reads(self):
        (self.workspace/'index.html').write_text('<h1>Old</h1>\n')
        (self.workspace/'logic.py').write_text("source = open('index.html').read()\nassert 'New' in source\n")
        self.context['implementation_plan_artifact']['file_manifest'].append({'path':'index.html','action':'modify'})
        task,response,_=self.execute([{'path':'index.html','start_line':1,'end_line':1,'replacement':'<h1>New</h1>'}])
        self.assertEqual('completed',task.state.value,response)
        self.assertEqual('<h1>New</h1>\n',(self.workspace/'index.html').read_text())

    def test_failure_frame_is_not_hidden_by_source_inventory_limit(self):
        paths=[f'part{i:02}.py' for i in range(12)]
        for name in paths:(self.workspace/name).write_text('value = 1\n')
        self.context['implementation_plan_artifact']['file_manifest']=[{'path':name,'action':'modify'}for name in paths]
        self.context['repair_diagnostics']['verification_failures'][0]['source_locations']=[{'path':paths[-1],'line':1}]
        task,response,calls=self.execute([{'path':paths[-1],'start_line':1,'end_line':1,'replacement':'value = 2'}])
        self.assertEqual('completed',task.state.value,response)
        self.assertIn('FILE '+paths[-1],calls[0][0][-1]['content'])
        self.assertEqual('value = 2\n',(self.workspace/paths[-1]).read_text())

    def test_authorized_text_source_is_not_hidden_by_language_extension(self):
        (self.workspace/'engine.rs').write_text('const VALUE: usize = 1;\n')
        self.context['implementation_plan_artifact']['file_manifest']=[{'path':'engine.rs','action':'modify'}]
        self.context['repair_diagnostics']['verification_failures'][0]['source_locations']=[{'path':'engine.rs','line':1}]
        task,response,calls=self.execute([{'action':'replace','path':'engine.rs','start_line':1,'end_line':1,
            'replacement':'const VALUE: usize = 2;'}])
        self.assertEqual('completed',task.state.value,response)
        self.assertIn('1: const VALUE: usize = 1;',calls[0][0][-1]['content'])
        self.assertEqual('const VALUE: usize = 2;\n',(self.workspace/'engine.rs').read_text())


if __name__=='__main__':
    unittest.main()
