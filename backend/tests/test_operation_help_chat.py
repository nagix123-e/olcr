import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from olcr_api.db import Database
from olcr_api.config import Settings
import olcr_api.app as api

class OperationHelpChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'help.db');self.db.initialize()
        for p in [patch.object(api,'db',self.db),patch.object(api,'settings',Settings(external_access_enabled=False,graphify_enabled=False)),patch.object(api,'_gui_session_token','help-session'),patch.object(api,'_chat_request_intent',side_effect=AssertionError('help must precede action routing')),patch.object(api.runtime,'execute',side_effect=AssertionError('no model or web needed'))]:
            p.start();self.addCleanup(p.stop)
        self.client=TestClient(api.app);self.client.headers['X-OLCR-Session']='help-session'
    def test_projectless_help_is_answered_and_persisted_without_model_or_network(self):
        response=self.client.post('/api/chat',json={'message':'OLCRのカレンダー接続方法を教えて','project_id':None})
        self.assertEqual(200,response.status_code,response.text)
        data=response.json();self.assertEqual('OLCR_OPERATION_HELP',data['response_owner'])
        self.assertIn('Macのカレンダー',data['response'])
        saved=self.db.conversation(data['conversation_id'])
        self.assertIsNone(saved['project_id']);self.assertEqual(2,len(saved['messages']))
        self.assertEqual([],data['task']['model_calls'])
        self.assertTrue(data['knowledge_provenance']['verified'])
    def test_stream_and_english_have_same_help_owner(self):
        response=self.client.post('/api/chat/stream',headers={'X-OLCR-Language':'en'},json={'message':'How do I use voice input in OLCR?','project_id':None})
        self.assertEqual(200,response.status_code,response.text)
        events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        self.assertEqual(['meta','chunk','done'],[e['type'] for e in events])
        self.assertIn('Voice input',events[1]['text'])
        self.assertEqual('OLCR_OPERATION_HELP',events[-1]['response_owner'])

    def test_troubleshooting_and_multiple_feature_questions_bypass_generation(self):
        cases=[('Thinkingオンなのにログが出ない','ja','thinking-missing'),
               ('アカウントを4つ追加できますか？','ja','account-slots'),
               ('カレンダーの権限を拒否した後はどうする？','ja','calendar-denied'),
               ('How do file links handle spaces in paths?','en','file-links')]
        for question,language,topic in cases:
            with self.subTest(question=question):
                response=self.client.post('/api/chat',headers={'X-OLCR-Language':language},json={'message':question,'project_id':None})
                self.assertEqual(200,response.status_code,response.text)
                self.assertEqual(topic,response.json()['knowledge_provenance']['topics'][0])
                self.assertEqual([],response.json()['task']['model_calls'])
