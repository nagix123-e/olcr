"""Regressions for the multi-file planner run reported on 2026-10-28."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from olcr_api.coding_verification import (normalize_static_verification, standard_verification_checks,
    run_acceptance, execution_command)
from olcr_api.node_project_verification import _dependencies, is_vite_project
from olcr_api.coding_tasks import evaluate_phase


class ViteCodingRecoveryTests(unittest.TestCase):
    def test_vite_package_metadata_recognizes_zero_config_projects(self):
        for field in ('dependencies','devDependencies'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                (root/'package.json').write_text(json.dumps({field:{'vite':'8.2.2'}}))
                (root/'index.html').write_text('<script type="module" src="/src/main.ts"></script>')
                self.assertTrue(is_vite_project(root))

    def test_every_supported_vite_config_extension_is_recognized(self):
        for name in ('vite.config.js','vite.config.mjs','vite.config.ts',
                     'vite.config.cjs','vite.config.mts','vite.config.cts'):
            with self.subTest(config=name),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                (root/'package.json').write_text('{}')
                (root/'index.html').write_text('<div id="app"></div>')
                (root/name).write_text('export default {};')
                self.assertTrue(is_vite_project(root))
                plan={'file_manifest':[{'path':path,'owner_phase':'P1','action':'create'}
                    for path in ('package.json','index.html','src/main.ts',name)],
                    'phases':[{'id':'P1','execution_mode':'IMPLEMENTATION','done':['src/main.ts exists'],
                        'verify':['npm run build completes without errors'],'dependencies':[]}]}
                self.assertTrue(normalize_static_verification(plan,verification_requested=True)['phases'][-1].get('host_verification'))

    def test_zero_config_vite_plan_uses_package_metadata_for_its_owned_build_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'package.json').write_text(json.dumps({'devDependencies':{'vite':'8.2.2'}}))
            manifest=[{'path':path,'owner_phase':'P1','action':'create'}
                      for path in ('package.json','index.html','src/main.ts')]
            plan={'file_manifest':manifest,'phases':[{'id':'P1','execution_mode':'IMPLEMENTATION',
                'done':['src/main.ts renders the application'],'verify':['npm run build completes without errors'],
                'dependencies':[]}]}
            scheduled=normalize_static_verification(plan,str(root),verification_requested=True)
            self.assertTrue(scheduled['phases'][-1].get('host_verification'))
            self.assertEqual(manifest,scheduled['file_manifest'])
            self.assertEqual(scheduled,normalize_static_verification(scheduled,str(root),verification_requested=True))

    def test_plain_or_unreadable_packages_are_not_treated_as_vite_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'index.html').write_text('<div id="app"></div>')
            for content in ('{}','[]','invalid JSON',json.dumps({'scripts':{'build':'vite build'}}),
                            json.dumps({'devDependencies':{'vite-plugin-example':'1.0.0'}})):
                with self.subTest(package=content):
                    (root/'package.json').write_text(content)
                    self.assertFalse(is_vite_project(root))
    def test_deferred_artifacts_reuse_retained_writes_after_idempotent_retry(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as directory:
            root=Path(directory);source='export const value = 4;';(root/'logic.ts').write_text(source)
            digest=hashlib.sha256(source.encode()).hexdigest()
            phase={'id':'P1','execution_mode':'IMPLEMENTATION','done':['logic.ts exists'],
                   'verify':['Typed workspace writes and read-back succeed for the changed files.']}
            op={'tool':'workspace_write','status':'success','output':{'path':str(root/'logic.ts'),
                'post_apply_hash':digest,'net_changed':True}}
            prior={'phase_id':'P1','status':'PASS','final_worktree_mutations':1,
                   'typed_execution_summary':{'operations':[op],'worktree_state':'COMMITTED'}}
            latest={'phase_id':'P1','status':'PASS','final_worktree_mutations':0,
                    'typed_execution_summary':{'operations':[{**op,'output':{**op['output'],'net_changed':False}}]}}
            self.assertTrue(evaluate_phase(phase,latest,[prior],directory)['phase_complete'])
            self.assertFalse(evaluate_phase(phase,latest,[],directory)['phase_complete'])
            rolled_back={**prior,'typed_execution_summary':{**prior['typed_execution_summary'],'worktree_state':'ROLLED_BACK'}}
            self.assertFalse(evaluate_phase(phase,latest,[rolled_back],directory)['phase_complete'])
            (root/'logic.ts').write_text('changed outside the task')
            self.assertFalse(evaluate_phase(phase,latest,[prior],directory)['phase_complete'])

    def test_framework_checks_are_deferred_until_all_inputs_exist(self):
        manifest = [{'path': path, 'owner_phase': owner, 'action': 'create'} for owner, paths in (
            ('P1', ['package.json','vite.config.ts','tsconfig.json','index.html','src/main.tsx']),
            ('P2', ['src/App.tsx','src/styles.css']), ('P3', ['tests/plan.test.ts'])) for path in paths]
        plan = {'file_manifest': manifest, 'phases': [
            {'id':'P1','execution_mode':'IMPLEMENTATION','done':['src/main.tsx renders the application'],
             'verify':['npm install completes without errors','npm run build completes without errors'],'dependencies':[]},
            {'id':'P2','execution_mode':'IMPLEMENTATION','done':['UI works'],'verify':['The UI is usable on mobile.'],'dependencies':['P1']},
            {'id':'P3','execution_mode':'IMPLEMENTATION_AND_VERIFICATION','done':['Tests cover save errors'],
             'verify':['All tests pass.'],'dependencies':['P2']} ]}
        result = normalize_static_verification(plan,verification_requested=True)
        self.assertEqual(manifest, result['file_manifest'])
        self.assertEqual(['package.json exists','vite.config.ts exists','tsconfig.json exists','index.html exists','src/main.tsx exists'],result['phases'][0]['done'])
        final = result['phases'][-1]
        self.assertTrue(final['host_verification'])
        self.assertEqual(['P1','P2','P3'], final['dependencies'])
        self.assertIn('npm install completes without errors',final['verify'])
        self.assertIn('Tests cover save errors',final['verify'])
        self.assertEqual(result, normalize_static_verification(result,verification_requested=True))
        self.assertNotEqual(result, plan)

    def test_owned_build_checks_do_not_execute_criterion_text(self):
        checks = standard_verification_checks({'VERIFY:1':'npm install completes without errors',
            'VERIFY:2':'npm run build completes without errors', 'VERIFY:3':'npm run build && touch other'})
        self.assertEqual([['VERIFY:1'],['VERIFY:2']],[check['ids'] for check in checks])
        self.assertTrue(all('projectBuild.' in check['code'] for check in checks))

    def fixture(self, root, invalid=False, config='vite.config.ts'):
        modules = Path(__file__).resolve().parents[2] / 'frontend/node_modules'
        if not (modules/'vite/bin/vite.js').is_file():
            self.skipTest('Local frontend Vite installation is unavailable')
        package={'private':True,'type':'module','dependencies':{name:json.loads((modules/name/'package.json').read_text())['version']
            for name in ('vite','typescript','react','react-dom','vitest','@types/react','@types/react-dom')}}
        (root/'package.json').write_text(json.dumps(package))
        if config is not None:(root/config).write_text('export default {};')
        (root/'tsconfig.json').write_text(json.dumps({'compilerOptions':{'target':'ES2022','module':'ESNext',
            'moduleResolution':'Bundler','strict':True,'noEmit':True,'jsx':'react-jsx','skipLibCheck':True},'include':['src']}))
        (root/'index.html').write_text('<div id="app"></div><script type="module" src="/src/main.tsx"></script>')
        (root/'src').mkdir()
        (root/'src/main.tsx').write_text('import {useState} from "react";import {createRoot} from "react-dom/client";'
            'const value: string = '+('42' if invalid else '"Built TypeScript"')+';'
            'function App(){const [count,setCount]=useState(0);return <><h1>{value}</h1><button onClick={()=>setCount(count+1)}>Add</button><output>{count}</output></>;}'
            'createRoot(document.querySelector("#app")!).render(<App/>);')
        (root/'src/numbers.ts').write_text('export const initial = 4;')
        (root/'src/logic.ts').write_text('import {initial} from "./numbers";export function calculate(value:number){return initial+value;}')
        dependencies=_dependencies(package)
        identity=hashlib.sha256(json.dumps(dependencies,sort_keys=True).encode()).hexdigest()[:24]
        cache=root/'contracts/node-project-runtime'/identity
        cache.mkdir(parents=True)
        (cache/'node_modules').symlink_to(modules,target_is_directory=True)
        (cache/'ready.json').write_text('{}')
        contract=root/'contracts/checks.json'
        checks=standard_verification_checks({'BUILD':'npm run build completes without errors'})
        checks.append({'ids':['MODULE'],'kind':'NODE','code':'const logic=await loadModule("src/logic.ts");assert.equal(logic.calculate(3),7);'})
        checks.append({'ids':['UI'],'kind':'BROWSER','code':'assert.equal(await page.locator("h1").innerText(),"Built TypeScript");await page.getByRole("button",{name:"Add"}).click();assert.equal(await page.locator("output").innerText(),"1");'})
        contract.write_text(json.dumps({'entrypoint':'index.html','checks':checks}))
        return execution_command(contract,root)

    def test_real_typescript_build_and_browser_preview_preserve_sources(self):
        with tempfile.TemporaryDirectory(prefix='olcr-vite-test-',dir='/private/tmp') as directory:
            root=Path(directory);command=self.fixture(root)
            before={str(p.relative_to(root)):p.read_bytes() for p in root.glob('src/*')}
            with patch('olcr_api.node_project_verification.PackageManagerExecutor.install',side_effect=AssertionError('No downloads in this test')):
                result=run_acceptance(command,str(root),40)
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            self.assertTrue(result.project_build['build_passed'])
            self.assertTrue(result.project_build['typecheck_passed'])
            payload=json.loads(result.stdout.rsplit('OLCR_VERIFICATION_RESULT=',1)[1].splitlines()[0])
            self.assertEqual({'BUILD','MODULE','UI'},set(payload['passed_ids']))
            self.assertFalse((root/'.olcr-preview').exists())
            self.assertFalse((root/'node_modules').exists())
            self.assertEqual(before,{str(p.relative_to(root)):p.read_bytes() for p in root.glob('src/*')})

    def test_real_zero_config_and_mts_projects_use_the_isolated_built_preview(self):
        for config in (None,'vite.config.mts'):
            with self.subTest(config=config),tempfile.TemporaryDirectory(prefix='olcr-vite-detection-',dir='/private/tmp') as directory:
                root=Path(directory);command=self.fixture(root,config=config)
                self.assertTrue(is_vite_project(root))
                original=(root/'src/main.tsx').read_bytes()
                with patch('olcr_api.node_project_verification.PackageManagerExecutor.install',side_effect=AssertionError('No downloads')):
                    result=run_acceptance(command,str(root),40)
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                self.assertTrue(result.project_build['build_passed'])
                payload=json.loads(result.stdout.rsplit('OLCR_VERIFICATION_RESULT=',1)[1].splitlines()[0])
                self.assertEqual({'BUILD','MODULE','UI'},set(payload['passed_ids']))
                self.assertEqual(original,(root/'src/main.tsx').read_bytes())
                self.assertFalse((root/'.olcr-preview').exists())
                self.assertFalse((root/'node_modules').exists())

    def test_real_type_error_is_application_failure_without_source_changes(self):
        with tempfile.TemporaryDirectory(prefix='olcr-vite-error-',dir='/private/tmp') as directory:
            root=Path(directory);command=self.fixture(root,invalid=True)
            original=(root/'src/main.tsx').read_text()
            with patch('olcr_api.node_project_verification.PackageManagerExecutor.install',side_effect=AssertionError('No downloads')):
                result=run_acceptance(command,str(root),40)
            payload=json.loads(result.stdout.rsplit('OLCR_VERIFICATION_RESULT=',1)[1].splitlines()[0])
            self.assertNotEqual(0,result.returncode)
            self.assertFalse(result.project_build['typecheck_passed'])
            self.assertTrue(result.project_build['typecheck_executed'])
            self.assertFalse(result.project_build['build_executed'])
            self.assertFalse(payload['executor_error'])
            self.assertIn('TS2322',payload['failures'][0]['error'])
            self.assertEqual(original,(root/'src/main.tsx').read_text())

    def test_real_vitest_runs_registered_typescript_tests_and_reports_failure(self):
        with tempfile.TemporaryDirectory(prefix='olcr-vite-suite-',dir='/private/tmp') as directory:
            root=Path(directory);command=self.fixture(root)
            tests=root/'tests';tests.mkdir()
            source=tests/'plan.test.ts'
            source.write_text('import {test,expect} from "vitest";test("actual assertion",()=>expect(2+2).toBe(4));')
            contract=Path(command[2]);value=json.loads(contract.read_text());value['requires_tests']=True
            contract.write_text(json.dumps(value))
            with patch('olcr_api.node_project_verification.PackageManagerExecutor.install',side_effect=AssertionError('No downloads')):
                passed=run_acceptance(command,str(root),40)
                self.assertEqual(0,passed.returncode,passed.stdout+passed.stderr)
                payload=json.loads(passed.stdout.rsplit('OLCR_VERIFICATION_RESULT=',1)[1].splitlines()[0])
                self.assertEqual(1,payload['test_runs'][0]['registered_tests'])
                source.write_text(source.read_text().replace('toBe(4)','toBe(5)'))
                failed=run_acceptance(command,str(root),40)
                self.assertNotEqual(0,failed.returncode)
                payload=json.loads(failed.stdout.rsplit('OLCR_VERIFICATION_RESULT=',1)[1].splitlines()[0])
                self.assertEqual(1,payload['test_runs'][0]['failed'])
                self.assertFalse(payload.get('executor_error'))
