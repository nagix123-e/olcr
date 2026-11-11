"""API ingress stays independent of the Coding Task lifecycle and transport."""
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

_import_profile = tempfile.TemporaryDirectory(dir="/private/tmp")
os.environ.setdefault("OLCR_DB_PATH", str(Path(_import_profile.name) / "import.sqlite"))
os.environ.setdefault("OLCR_ALLOWED_ROOTS", _import_profile.name)

from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api.api_acceptance import PRODUCTION_ACCEPTANCE_CASES
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.models import Route, Task, TaskState


class ApiOrchestratorBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse a real preloaded checkpoint rather than repeatedly testing its
        # startup in an ingress matrix; final provider routing is unchanged.
        cls.startup = patch.object(api, '_startup_laya_provider', return_value=(
            api.laya_provider.local, api._laya_startup_status))
        cls.startup.start()

    @classmethod
    def tearDownClass(cls):
        cls.startup.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db, self.old_settings = api.db, api.settings
        api.db = Database(str(Path(self.tmp.name) / "test.sqlite")); api.db.initialize()
        api.rebuild(Settings(db_path=api.db.path, allowed_roots=(self.tmp.name,),
                             external_access_enabled=True, task_manager_enabled=True))
        api.db.create_project("project", self.tmp.name, time.time(), "project")
        self.client = TestClient(api.app)

    def tearDown(self):
        api.db = self.old_db; api.rebuild(self.old_settings)
        self.tmp.cleanup()

    def task(self, status, pending=None):
        cid = str(__import__('uuid').uuid4())
        api.db.create_conversation("API and coding", time.time(), cid, "project")
        task = api.db.create_coding_task(cid, cid, "Implement a sample app", status, "NONE", None, time.time())
        if pending:
            api.db.update_coding_task(cid, pending_authorization=pending,
                recovery_reason="REPLAN_INEFFECTIVE" if pending.get('type') == 'REPLAN_INEFFECTIVE' else "NONE")
        return cid, api.db.coding_task(task['id'])

    def test_all_440_existing_api_prompts_are_independent_of_automatic_task_context(self):
        cases = [(row['tool_id'], row['user_prompt']) for row in PRODUCTION_ACCEPTANCE_CASES]
        plan = Path(__file__).resolve().parents[2] / 'docs/fuzzy-routing/plan-and-352-prompts.md'
        blocks = re.split(r'^### (\d+)\. `([^`]+)`[^\n]*$', plan.read_text(), flags=re.M)
        for index in range(1, len(blocks), 3):
            cases.extend((blocks[index + 1], prompt)
                         for prompt in re.findall(r'^\d+\. (.+)$', blocks[index + 2], re.M))
        self.assertGreaterEqual(len(cases), 440)
        for tool, prompt in cases:
            with self.subTest(tool=tool, prompt=prompt):
                value = api.ChatInput(message=prompt, coding_task_id='old-task', coding_followup='auto')
                self.assertTrue(api._independent_external_chat_turn(value))

    def test_provider_ingress_survives_pending_and_terminal_tasks_on_both_transports(self):
        prompts = [
            ('今日の東京の天気を教えて', 'weather.open_meteo'),
            ('100ドルは今何円？', 'currency.frankfurter'),
            ('Wikipediaで大谷翔平について教えて', 'knowledge.wikimedia'),
            ('PubChemでaspirinの分子量を教えて', 'chemistry.pubchem'),
            ('日本の2027年の祝日を調べて', 'calendar.public_holidays'),
            ('FREDで米国の政策金利FFRの直近12か月の推移を見せて', 'econ.fred'),
            ('USGSで最近のM5以上の地震を教えて', 'earth.earthquakes'),
        ]
        states = [('WAITING_FOR_PLAN_APPROVAL', None), ('WAITING_FOR_USER', None),
                  ('WAITING_FOR_USER', {'type': 'REQUIREMENTS_CLARIFICATION', 'questions': ['Target file?']}),
                  ('WAITING_FOR_USER', {'type': 'WORKSPACE_SETUP'}),
                  ('WAITING_FOR_USER', {'type': 'REPLAN_INEFFECTIVE'}),
                  ('WAITING_FOR_USER', {'type': 'PROTECTED_OPERATION', 'requested_scope': ['file write']}),
                  ('RUNNING', None), ('BLOCKED', None), ('RESUMABLE', None), ('COMPLETED', None)]

        def provider(tool, arguments):
            return {'tool_id': tool, 'provider': api.REGISTRY[tool].provider, 'fetched_at': '2026-10-19',
                    'response_kind': 'record_list', 'data': {'semantic_status': 'DATA', 'has_payload': True,
                    'items': [{'title': 'Verified fixture'}]},
                    'sources': [{'provider': api.REGISTRY[tool].provider, 'canonical_url': 'https://example.test/source'}]}

        def compose(prompt, result):
            task = Task(prompt); task.route = Route.EXTERNAL_API
            task.transition(TaskState.ROUTING); task.transition(TaskState.EXECUTING); task.transition(TaskState.COMPLETED)
            return task, 'Verified fixture: ' + result['tool_id']

        with patch.object(api, 'execute_external_tool', side_effect=provider) as execute, \
             patch.object(api, 'compose_external_result', side_effect=compose), \
             patch.object(api, 'external_tool_credential_setup', return_value=None), \
             patch.object(api, '_external_tool_available', return_value=True), \
             patch.object(api, '_coding_scheduler_wake') as wake, \
             patch.object(api.runtime.model, 'generate', side_effect=AssertionError('Ingress must not invoke a coding model')):
            for enabled in (True, False):
                api.settings.task_manager_enabled = enabled
                for status, pending in states:
                    cid, before = self.task(status, pending)
                    for stream in (False, True):
                        for prompt, tool in prompts:
                            with self.subTest(enabled=enabled, status=status, pending=pending, stream=stream, tool=tool):
                                execute.reset_mock()
                                response = self.client.post('/api/chat/stream' if stream else '/api/chat', json={
                                    'message': prompt, 'conversation_id': cid, 'project_id': 'project',
                                    'coding_task_id': cid, 'coding_followup': 'auto'})
                                self.assertEqual(200, response.status_code, response.text)
                                self.assertIn('Verified fixture: ' + tool, response.text)
                                self.assertNotIn('CODING_ORCHESTRATOR', response.text)
                                self.assertEqual(tool, execute.call_args.args[0])
                                self.assertEqual(before, api.db.coding_task(cid))
                                self.assertEqual(1, len(api.db.coding_tasks(cid)))
            wake.set.assert_not_called()

    def test_explicit_task_instructions_keep_task_ownership(self):
        for prompt in ('この計画で進めて', '続行', '確認が必要とはどういうこと',
                       'このTaskの状態を教えて', '余白を大きくして'):
            with self.subTest(prompt=prompt):
                self.assertFalse(api._independent_external_chat_turn(api.ChatInput(
                    message=prompt, coding_task_id='current', coding_followup='auto')))
        self.assertFalse(api._independent_external_chat_turn(api.ChatInput(
            message='このTaskに天気APIを追加して', coding_task_id='current', coding_followup='steer')))

    def test_explicit_ordinary_chat_bypasses_requirement_clarification_without_mutation(self):
        cid, before = self.task('WAITING_FOR_USER', {'type': 'REQUIREMENTS_CLARIFICATION', 'questions': ['Target file?']})
        with patch.object(api.runtime, 'execute') as execute, \
             patch.object(api, '_automatic_skill_selection', return_value=None):
            task = Task('Hi'); task.transition(TaskState.ROUTING); task.transition(TaskState.GENERATING); task.transition(TaskState.COMPLETED)
            execute.return_value = task, 'Hello from ordinary chat'
            response = self.client.post('/api/chat', json={'message': 'Hi', 'conversation_id': cid,
                                                         'project_id': 'project', 'coding_followup': 'chat'})
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn('Hello from ordinary chat', response.text)
        self.assertEqual(before, api.db.coding_task(cid))

    def test_orchestrator_off_does_not_attach_raw_coding_to_an_old_task(self):
        api.settings.task_manager_enabled = False
        cid, before = self.task('WAITING_FOR_USER', {'type': 'REQUIREMENTS_CLARIFICATION'})
        with patch.object(api.runtime, 'execute') as execute, \
             patch.object(api, '_automatic_skill_selection', return_value=None):
            task = Task('Update app.py'); task.transition(TaskState.ROUTING); task.transition(TaskState.GENERATING); task.transition(TaskState.COMPLETED)
            execute.return_value = task, 'Raw Brain response'
            response = self.client.post('/api/chat', json={'message': 'Update app.py', 'conversation_id': cid,
                'project_id': 'project', 'coding_task_id': cid, 'coding_followup': 'auto'})
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn('Raw Brain response', response.text)
        self.assertEqual(before, api.db.coding_task(cid))
        self.assertTrue(execute.called)
        self.assertNotIn('managed_context', execute.call_args.kwargs)

    def test_independent_api_still_obeys_external_access_gate_with_pending_task(self):
        api.settings.external_access_enabled = False
        cid, before = self.task('WAITING_FOR_PLAN_APPROVAL')
        for stream in (False, True):
            with self.subTest(stream=stream), patch.object(api, 'execute_external_tool') as execute:
                response = self.client.post('/api/chat/stream' if stream else '/api/chat', json={
                    'message': '今日の東京の天気を教えて', 'project_id': 'project', 'conversation_id': cid,
                    'coding_task_id': cid, 'coding_followup': 'auto'})
            self.assertEqual(200, response.status_code, response.text)
            self.assertIn('外部アクセス', response.text)
            execute.assert_not_called()
            self.assertEqual(before, api.db.coding_task(cid))

    def test_automatic_task_status_is_read_only_and_resume_keeps_control_owner(self):
        for state in ('RUNNING', 'BLOCKED', 'COMPLETED'):
            cid, before = self.task(state)
            response = self.client.post('/api/chat', json={'message': 'このTaskの状態を教えて',
                'conversation_id': cid, 'project_id': 'project', 'coding_task_id': cid, 'coding_followup': 'auto'})
            self.assertEqual(200, response.status_code, response.text)
            self.assertIn(state, response.json()['response'])
            self.assertEqual(before, api.db.coding_task(cid))
            self.assertEqual(1, len(api.db.coding_tasks(cid)))
        cid, before = self.task('RESUMABLE')
        with patch.object(api, '_resume_coding_task_control', return_value={
                'conversation_id': cid, 'coding_task_id': cid, 'response': 'Task control response'}) as resume:
            response = self.client.post('/api/chat/stream', json={'message': '続行', 'conversation_id': cid,
                'project_id': 'project', 'coding_task_id': cid, 'coding_followup': 'auto'})
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn('Task control response', response.text)
        resume.assert_called_once()
        self.assertEqual(cid, resume.call_args.args[0]['id'])
        self.assertEqual(before, api.db.coding_task(cid))


if __name__ == '__main__':
    unittest.main()
