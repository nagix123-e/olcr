"""Real split-runtime verification and durable chat-control regressions."""
import copy
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import olcr_api.app as api
from olcr_api import coding_verification as verification
from olcr_api.db import Database
from olcr_api.file_conversion import conversion_target
from olcr_api.node_mcp_runtime import verification_browser_executable


class CodingExecutorRegressions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='olcr-coding-regression-', dir='/private/tmp')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = Database(str(self.root / 'isolated.db'))
        self.database.initialize()
        self.database.create_project('fixture', str(self.root), time.time(), 'project')
        self.database.create_conversation('fixture', time.time(), 'conversation', 'project')
        self.addCleanup(patch.stopall)
        patch.object(api, 'db', self.database).start()
        patch.object(api, '_gui_session_token', '').start()
        patch.object(api, 'settings', api.settings.with_overrides({'task_manager_enabled': True})).start()
        (self.root / 'index.html').write_text('<button id="add">Add</button><output id="count">0</output><script src="app.js"></script>')
        (self.root / 'app.js').write_text("let count=Number(localStorage.getItem('count')||0);const output=document.querySelector('#count');output.textContent=count;document.querySelector('#add').onclick=()=>{output.textContent=++count;localStorage.setItem('count',count);};")

    def phase(self, checks):
        contract = self.root / 'contract.json'
        contract.write_text(json.dumps({'entrypoint': 'index.html', 'checks': checks}))
        return {'id': 'verify', 'execution_mode': 'VERIFICATION_ONLY', 'host_verification': True,
                'done': ['Application works'], 'verify': [],
                'host_criteria': {identifier: identifier for check in checks for identifier in check['ids']},
                'verification_targets': ['index.html', 'app.js'],
                'verification_command': verification.execution_command(contract, self.root)}

    def test_split_node_and_browser_run_large_mixed_acceptance_contract(self):
        code = "for(let n=1;n<=12;n++){await page.locator('#add').click();await expect(page.locator('#count')).toHaveText(String(n));}await page.reload();await expect(page.locator('#count')).toHaveText('12');"
        checks = [{'ids': [f'CASE:{n}'], 'kind': 'BROWSER', 'code': code} for n in range(12)]
        checks.append({'ids': ['LOGIC'], 'kind': 'NODE', 'code': "assert.ok(readFile('app.js').includes('localStorage'));"})
        phase = self.phase(checks)
        before = {name: (self.root / name).read_bytes() for name in phase['verification_targets']}
        execution, result = api._run_read_only_verification(phase, str(self.root))
        self.assertTrue(result['passed'], result)
        self.assertEqual(12, result['browser_checks'])
        self.assertEqual(13, len(result['passed_criteria_ids']))
        self.assertFalse(result['executor_error'])
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})
        # A failing observed application assertion must retain application ownership.
        phase = self.phase([{'ids': ['BAD'], 'kind': 'BROWSER', 'code': "assert.equal(await page.locator('#count').textContent(),'wrong');"}])
        _, result = api._run_read_only_verification(phase, str(self.root))
        self.assertFalse(result['passed'])
        self.assertFalse(result['executor_error'])
        self.assertEqual(['BAD'], result['failures'][0]['ids'])

    def test_blocked_remote_stylesheet_does_not_fail_a_local_behavior_check(self):
        (self.root/'index.html').write_text('<link rel="stylesheet" href="https://fonts.example.invalid/style.css"><button id="add">Add</button><output id="count">0</output><script src="app.js"></script>')
        phase=self.phase([{'ids':['LOCAL'],'kind':'BROWSER','code':"await page.locator('#add').click();assert.equal(await page.locator('#count').textContent(),'1');"}])
        _,result=api._run_read_only_verification(phase,str(self.root))
        self.assertTrue(result['passed'],result)
        (self.root/'app.js').write_text("console.error('Application initialization failed')")
        _,result=api._run_read_only_verification(phase,str(self.root))
        self.assertFalse(result['passed'])

    def test_node_only_check_does_not_prepare_browser(self):
        with patch.object(verification, 'verification_browser_executable', side_effect=AssertionError('No browser needed')):
            phase = self.phase([{'ids': ['NODE'], 'kind': 'NODE', 'code': "assert.ok(readFile('index.html').includes('output'));"}])
            _, result = api._run_read_only_verification(phase, str(self.root))
        self.assertTrue(result['passed'], result)

    def test_dialog_listener_can_be_removed_and_replaced_before_an_action(self):
        (self.root/'index.html').write_text('<button onclick="document.querySelector(\'output\').textContent=prompt(\'Name\')">Ask</button><output></output>')
        code="const old=dialog=>dialog.dismiss();page.on('dialog',old);page.off('dialog',old);page.once('dialog',dialog=>dialog.accept('Ada'));await page.getByRole('button',{name:'Ask'}).click();assert.equal(await page.locator('output').textContent(),'Ada');"
        _,result=api._run_read_only_verification(self.phase([{'ids':['DIALOG'],'kind':'BROWSER','code':code}]),str(self.root))
        self.assertTrue(result['passed'],result)

    def test_double_dialog_response_is_a_check_defect_not_a_runner_crash(self):
        code="page.once('dialog',dialog=>dialog.accept('one'));page.once('dialog',dialog=>dialog.accept('two'));"
        _,result=api._run_read_only_verification(self.phase([{'ids':['DIALOG'],'kind':'BROWSER','code':code}]),str(self.root))
        self.assertFalse(result['passed'])
        self.assertFalse(result['executor_error'])
        self.assertTrue(result['failures'][0]['contract_error'])
        self.assertIn('multiple pending dialog handlers',result['failures'][0]['error'])

    def test_legacy_saved_command_gets_current_browser_without_new_checks(self):
        phase = self.phase([{'ids': ['BROWSER'], 'kind': 'BROWSER', 'code': "assert.equal(await page.locator('#count').textContent(),'0');"}])
        phase['verification_command'] = phase['verification_command'][:5]
        with patch.object(api, '_model_text', side_effect=AssertionError('No model needed')):
            _, result = api._run_read_only_verification(phase, str(self.root))
        self.assertTrue(result['passed'], result)

    def test_browser_startup_failure_emits_typed_executor_error(self):
        phase = self.phase([{'ids': ['BROWSER'], 'kind': 'BROWSER', 'code': 'assert.ok(true);'}])
        command = [*phase['verification_command'][:5], str(self.root / 'missing-chromium')]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=20)
        payload = json.loads(completed.stdout.split('OLCR_VERIFICATION_RESULT=', 1)[1])
        self.assertNotEqual(0, completed.returncode)
        self.assertTrue(payload['executor_error'])
        self.assertEqual([], payload['passed_ids'])
        self.assertTrue(payload['failures'][0]['executor_error'])

    def test_host_crash_without_summary_is_executor_failure_not_source_failure(self):
        phase = self.phase([{'ids': ['NODE'], 'kind': 'NODE', 'code': 'assert.ok(true);'}])
        crash = subprocess.CompletedProcess(phase['verification_command'], 1, '', 'TypeError: reading path')
        with patch.object(api, 'run_acceptance', return_value=crash):
            _, result = api._run_read_only_verification(phase, str(self.root))
        self.assertFalse(result['passed'])
        self.assertTrue(result['executor_error'])
        self.assertTrue(result['failures'][0]['contract_error'])

    def test_missing_browser_during_preparation_pauses_without_replanning(self):
        goal = 'Build an app and verify the app in the browser.'
        phase = {'id': 'verify', 'execution_mode': 'VERIFICATION_ONLY', 'host_verification': True,
                 'done': ['Application works'], 'verify': [], 'dependencies': []}
        plan = {'schema_version': 1, 'original_goal': goal, 'scope': {'allowed': ['index.html', 'app.js'], 'forbidden': []},
                'assumptions': [], 'phases': [phase], 'max_retries_per_phase': 2, 'requires_user_approval': True}
        api.db.create_coding_task('task', 'conversation', goal, 'QUEUED', 'NONE', plan, time.time())
        with patch.object(api, '_host_verification_phase', side_effect=verification.VerificationExecutorError('Browser unavailable')), \
             patch.object(api, '_replan_task', side_effect=AssertionError('Do not replan source')), \
             patch.object(api.runtime, 'execute', side_effect=AssertionError('Do not edit source')):
            response = api._run_managed_task('task', str(self.root))
        self.assertIn('executor is unavailable', response)
        task = api.db.coding_task('task')
        self.assertEqual('RESUMABLE', task['status'])
        self.assertEqual(0, task['replan_count'])
        self.assertEqual('VERIFICATION_EXECUTOR', task['recovery_reason'])

    def test_historical_host_crash_recovers_executor_ownership(self):
        runner = Path(verification.__file__).with_name('coding_verification_runner.cjs')
        report = {'errors': ['verification command failed'], 'typed_execution_summary': {'operations': [
            {'tool': 'verification_command', 'status': 'failed', 'input': {'command': ['node', str(runner), 'contract.json']},
             'output': {'returncode': 1, 'output': 'TypeError: reading path', 'failures': [], 'changed_targets': []}}]}}
        restored = api._restore_legacy_executor_error(report)
        self.assertEqual('VERIFICATION_EXECUTOR', restored['manager_decision']['retry_channel'])
        self.assertTrue(restored['typed_execution_summary']['operations'][0]['output']['executor_error'])
        self.assertNotIn('manager_decision', report, 'Do not mutate saved evidence in place')
        application = copy.deepcopy(report)
        application['typed_execution_summary']['operations'][0]['input']['command'] = ['node', '--test']
        self.assertEqual(application, api._restore_legacy_executor_error(application))

    def test_host_probe_crash_does_not_reauthor_valid_checks(self):
        api.db.create_coding_task('task', 'conversation', 'Build an app', 'RUNNING', 'NONE', None, time.time())
        phase = {'id':'final', 'host_verification':True, 'execution_mode':'VERIFICATION_ONLY',
                 'done':['The app loads in a browser without console errors.'], 'verify':[]}
        plan = {'file_manifest':[{'path':'index.html'}, {'path':'app.js'}]}
        crash = subprocess.CompletedProcess(['node'], 1, '', 'Host runtime crashed')
        with patch.object(api, 'run_acceptance', return_value=crash), \
             patch.object(api, '_model_text', side_effect=AssertionError('Do not reauthor checks for a host crash')):
            with self.assertRaisesRegex(verification.VerificationExecutorError, 'Host runtime crashed'):
                api._host_verification_phase('task', phase, plan, str(self.root))

    def test_legacy_archive_resume_restores_coding_context_for_both_chat_transports(self):
        api.db.create_coding_task('task', 'conversation', 'Build an app', 'RESUMABLE', 'NONE', None, time.time())
        api.db.update_coding_task('task', archived=True)
        with patch.object(api.runtime, 'execute', side_effect=AssertionError('No normal model')), \
             patch.object(api._coding_scheduler_wake,'set'):
            client = TestClient(api.app)
            for endpoint in ['/api/chat', '/api/chat/stream']:
                response = client.post(endpoint, json={'message': '再開して。', 'conversation_id': 'conversation', 'project_id': 'project'})
                self.assertEqual(200, response.status_code, response.text)
                self.assertIn('task', response.text)
                self.assertNotIn('スクレイプ', response.text)
        self.assertFalse(api.db.coding_task('task')['archived'])

    def test_short_retry_uses_the_same_coding_task_in_both_transports(self):
        api.db.create_coding_task('task', 'conversation', 'Build an app', 'RESUMABLE', 'NONE', None, time.time())
        client = TestClient(api.app)
        with patch.object(api, '_resume_coding_task_control', return_value={
                'conversation_id':'conversation', 'coding_task_id':'task', 'response':''}) as resume, \
             patch.object(api.runtime, 'execute', side_effect=AssertionError('No normal model')):
            for endpoint, message in [('/api/chat','再試行して。'),('/api/chat/stream','retry!')]:
                response=client.post(endpoint,json={'message':message,'conversation_id':'conversation','project_id':'project'})
                self.assertEqual(200,response.status_code,response.text)
                self.assertEqual('task',resume.call_args.args[0]['id'])
            self.assertEqual(2,resume.call_count)
        self.assertEqual(1,len(api.db.coding_tasks('conversation')))

    def test_legacy_browser_manifest_must_point_inside_its_runtime(self):
        (self.root / 'browser').mkdir()
        binary = self.root / 'browser/chromium'
        binary.touch(); binary.chmod(0o755)
        (self.root / 'runtime-manifest.json').write_text(json.dumps({'browser': {'path': 'browser/chromium'}}))
        self.assertEqual(binary, verification_browser_executable(self.root))
        (self.root / 'runtime-manifest.json').write_text(json.dumps({'browser': {'path': '../outside/chromium'}}))
        self.assertIsNone(verification_browser_executable(self.root))

    def test_implementation_instructions_do_not_become_file_conversions(self):
        for message in [
            'Fix test_scheduler.py to support package and top-level imports. Only four files may change.',
            'Change this function to support empty input.',
            'Update the app to handle save failures; change only app.js.',
        ]:
            self.assertIsNone(conversion_target(message), message)
        self.assertEqual('png', conversion_target('convert this to png'))
        self.assertEqual('pdf', conversion_target('Change this file to PDF'))


if __name__ == '__main__':
    unittest.main()
