import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api.db import Database
from olcr_api.config import Settings

class GuiFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.old_db, self.old_settings = api.db, api.settings
        api.db = Database(str(Path(self.tmp.name)/'test.sqlite'))
        api.db.initialize()
        api.rebuild(self.old_settings.with_overrides({
            'db_path': str(Path(self.tmp.name) / 'test.sqlite'),
            'task_manager_enabled': False,
        }))
        self.client = TestClient(api.app)
        self.project = self.client.post('/api/projects', json={'name':'A','workspace_path':self.tmp.name}).json()['id']
    def tearDown(self):
        api.db = self.old_db
        api.rebuild(self.old_settings)
        self.tmp.cleanup()
    def test_catalog_and_dispatch(self):
        commands=self.client.get('/api/commands').json()['commands']
        self.assertEqual(len(commands),len({x['id'] for x in commands}))
        self.assertEqual(422,self.client.post('/api/commands',json={'text':'/arbitrary'}).status_code)
        self.assertEqual(422,self.client.post('/api/commands',json={'text':'/context show'}).status_code)
        self.assertEqual(200,self.client.post('/api/commands',json={'text':'/memory off'}).status_code)
        self.assertFalse(api.settings.conversation_memory_enabled)
        self.assertEqual(200,self.client.post('/api/commands',json={'text':'/help'}).status_code)

    def test_coding_task_manager_setting_is_global_and_persistent(self):
        # A new installation with no persisted preference resolves to OFF.
        with patch.dict(os.environ, {}, clear=True):
            fresh = Settings.from_env()
            self.assertFalse(fresh.task_manager_enabled)
            self.assertFalse(fresh.with_overrides({}).task_manager_enabled)

        # The explicit preference is global and survives a normal reload.
        before = self.client.get('/api/settings').json()
        self.assertIn('task_manager_enabled', before)
        self.assertFalse(before['task_manager_enabled'])
        on_response = self.client.put('/api/settings', json={**before, 'task_manager_enabled': True})
        self.assertEqual(200, on_response.status_code)
        self.assertTrue(on_response.json()['task_manager_enabled'])
        reopened = Database(api.db.path)
        reopened.initialize()
        self.assertTrue(reopened.load_settings()['task_manager_enabled'])
        api.reload_settings()
        self.assertTrue(api.settings.task_manager_enabled)

        off_response = self.client.put('/api/settings', json={**on_response.json(), 'task_manager_enabled': False})
        response = off_response
        self.assertEqual(200, response.status_code)
        self.assertFalse(response.json()['task_manager_enabled'])
        self.assertFalse(reopened.load_settings()['task_manager_enabled'])
        api.reload_settings()
        self.assertFalse(api.settings.task_manager_enabled)

    def test_task_manager_off_bypasses_managed_creation_and_keeps_safe_non_manager_path(self):
        api.rebuild(api.settings.with_overrides({'task_manager_enabled': False}))
        conversation = self.client.post(f'/api/projects/{self.project}/conversations').json()['id']
        request = 'このリポジトリに認証機能を実装してください'
        normal_task = api.Task(request)
        with patch.object(api._coding_scheduler_wake, 'set') as wake, \
             patch.object(api.runtime, 'execute', return_value=(normal_task, '通常のBrain経路で回答しました。')) as execute:
            response = self.client.post('/api/chat', json={'project_id': self.project, 'conversation_id': conversation, 'message': request})
        self.assertEqual(200, response.status_code)
        self.assertFalse(api.settings.task_manager_enabled)
        self.assertEqual([], api.db.coding_tasks(conversation))
        self.assertFalse(wake.called)
        execute.assert_called_once()
        self.assertEqual('通常のBrain経路で回答しました。', response.json()['response'])

    def test_task_manager_on_uses_existing_managed_creation_path(self):
        api.rebuild(api.settings.with_overrides({'task_manager_enabled': True}))
        conversation = self.client.post(f'/api/projects/{self.project}/conversations').json()['id']
        request = 'このリポジトリに認証機能を実装してください'
        with patch.object(api._coding_scheduler_wake, 'set') as wake, \
             patch.object(api.runtime, 'execute', side_effect=AssertionError('managed route must not fall through')):
            response = self.client.post('/api/chat', json={'project_id': self.project, 'conversation_id': conversation, 'message': request})
        self.assertEqual(200, response.status_code)
        self.assertTrue(api.settings.task_manager_enabled)
        self.assertTrue(response.json().get('coding_task_id'))
        self.assertEqual('QUEUED', api.db.coding_task(response.json()['coding_task_id'])['status'])
        self.assertTrue(wake.called)
    def test_web_validation_and_secret(self):
        with patch.dict('os.environ',{'OLCR_WEB_BRAVE_API_KEY':'secret-test-value'}):
            response=self.client.get('/api/web/settings')
            self.assertNotIn('secret-test-value',response.text)
            self.assertEqual('Configured',response.json()['credentials']['brave'])
        before=api.settings.web_provider
        self.assertEqual(422,self.client.put('/api/web/settings',json={'web_mode':'auto','web_provider':'invalid'}).status_code)
        self.assertEqual(before,api.settings.web_provider)
    def test_context_isolation_reload_and_security(self):
        path=Path(self.tmp.name)/'core.txt';path.write_text('Project A context')
        base=f'/api/projects/{self.project}/core-context'
        self.assertEqual(200,self.client.post(base+'/load',json={'path':str(path)}).status_code)
        b=self.client.post('/api/projects',json={'name':'B'}).json()['id']
        self.assertEqual('',self.client.get(f'/api/projects/{b}/core-context').json()['content'])
        self.assertEqual('Project A context',self.client.get(base).json()['content'])
        path.write_text('reloaded')
        self.assertEqual('reloaded',self.client.post(base+'/reload').json()['content'])
        link=Path(self.tmp.name)/'link.txt';link.symlink_to(path)
        self.assertEqual(403,self.client.post(base+'/load',json={'path':str(link)}).status_code)
        self.assertEqual(422,self.client.post(base+'/load',json={'path':str(path.parent/'..'/'other')}).status_code)
        reopened=Database(api.db.path);reopened.initialize()
        self.assertEqual('reloaded',reopened.load_settings()['project_core_context:'+self.project])
