from contextlib import ExitStack
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from olcr_api.db import Database
from olcr_api.config import Settings
from olcr_api.personal_tasks import PersonalTasks

class AssistantWorkflowApiTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory()
  with patch.dict('os.environ',{'OLCR_DB_PATH':str(Path(cls.tmp.name)/'startup.db')}):
   import olcr_api.app as api
  cls.api=api;api.stop_coding_scheduler()
 @classmethod
 def tearDownClass(cls):cls.tmp.cleanup()
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.db=Database(str(Path(self.tmp.name)/'db'));self.db.initialize()
  self.db.create_project('P',self.tmp.name,1,'p');self.db.create_project('Q',self.tmp.name,1,'q');self.db.create_conversation('C',1,'c','p')
  self.stack=ExitStack();self.addCleanup(self.stack.close)
  for field,value in [('db',self.db),('settings',Settings(permission_level='automatic',external_access_enabled=False)),('_gui_session_token','workflow-test')]:self.stack.enter_context(patch.object(self.api,field,value))
  self.stack.enter_context(patch.object(self.api.brain_models,'configuration',return_value={}))
  self.generate=self.stack.enter_context(patch.object(self.api.runtime,'_generate_brain',return_value={'text':json.dumps({'action':'task_create','task':{'title':'水を飲む','kind':'reminder','enabled':True},'schedule':'毎朝9時'})}))
  self.client=TestClient(self.api.app);self.client.headers['X-OLCR-Session']='workflow-test'
 def test_sync_create_stream_confirm_without_settings_or_coding(self):
  r=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p','message':'毎朝9時に水を飲むようリマインドして','message_id':'create'})
  self.assertEqual(r.status_code,200,r.text);block=r.json()['blocks'][0];self.assertEqual(block['type'],'assistant_workflow');self.assertEqual(block['status'],'review')
  self.assertFalse(any(b['type']=='settings_control' for b in r.json()['blocks']))
  r=self.client.post('/api/chat/stream',json={'conversation_id':'c','project_id':'p','message':block['actions'][0]['message'],'message_id':'confirm'})
  self.assertEqual(r.status_code,200,r.text);self.assertIn('ASSISTANT_WORKFLOW',r.text)
  self.assertEqual(len(PersonalTasks(self.db,lambda:{}).tasks()),1);self.assertEqual(self.generate.call_count,1)
  with self.db.connect() as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM coding_tasks').fetchone()[0],0)
 def test_wrong_project_auth_and_confirmation_mode_before_generation(self):
  body={'conversation_id':'c','project_id':'q','message':'毎朝9時に水を飲むようリマインドして'}
  self.assertEqual(self.client.post('/api/chat',json=body).status_code,403);self.generate.assert_not_called()
  self.assertEqual(self.client.post('/api/chat',json=body,headers={'X-OLCR-Session':'wrong'}).status_code,401)
  self.api.settings.permission_level='confirm';body['project_id']='p'
  self.assertEqual(self.client.post('/api/chat',json=body).status_code,409);self.generate.assert_not_called()
