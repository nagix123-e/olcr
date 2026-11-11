from contextlib import ExitStack
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from olcr_api.custom_agents import AgentStore,AgentDefinition
from olcr_api.db import Database


class AgentBuilderApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.startup=tempfile.TemporaryDirectory()
        with patch.dict('os.environ',{'OLCR_DB_PATH':str(Path(cls.startup.name)/'startup.db')}):
            import olcr_api.app as api
        cls.api=api;api.stop_coding_scheduler()

    @classmethod
    def tearDownClass(cls):cls.startup.cleanup()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(str(Path(self.tmp.name)/'api.db'));self.db.initialize()
        self.db.create_project('P',self.tmp.name,1,'p');self.db.create_project('Q',self.tmp.name,1,'q')
        self.db.create_conversation('C',1,'c','p')
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(self.api,'db',self.db))
        self.stack.enter_context(patch.object(self.api.brain_models,'configuration',return_value={}))
        self.generate=self.stack.enter_context(patch.object(self.api.runtime,'_generate_brain',side_effect=AssertionError('copy must not generate')))
        self.client=TestClient(self.api.app)

    def test_natural_creation_is_terminal_even_inside_custom_agent_chat(self):
        old=AgentStore(self.db).save(AgentDefinition(name='Existing'))
        conversation=AgentStore(self.db).bind(old,'p')
        with patch.object(self.api,'agent_chat',side_effect=AssertionError('must route to creation')):
            response=self.client.post('/api/chat',json={'conversation_id':conversation['id'],'project_id':'p',
                'message':'カスタムエージェントを作成\n名前: Copied\n指示: exact text\n','message_id':'copy'})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual('created',response.json()['blocks'][0]['status'])
        self.assertEqual(2,len(AgentStore(self.db).list()));self.generate.assert_not_called()

    def test_wrong_project_cannot_mutate_a_builder_draft(self):
        response=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'q','agent_builder':True,
            'message':'名前: A\n指示: source'})
        self.assertEqual(403,response.status_code)
        self.assertEqual([],AgentStore(self.db).list());self.assertEqual([],self.db.conversation('c')['messages'])

    def test_mode_questions_and_stream_entry_share_the_same_creation_state(self):
        from olcr_api.permissions import requires_request_approval
        self.assertTrue(requires_request_approval('POST','/api/agents/builder/chat'))
        first=self.client.post('/api/agents/builder/chat',json={'conversation_id':'c','project_id':'p','message':'指示: copied prompt'})
        self.assertEqual(200,first.status_code)
        self.assertEqual('collecting',first.json()['blocks'][0]['status'])
        second=self.client.post('/api/chat/stream',json={'conversation_id':'c','project_id':'p','message':'Name'})
        self.assertEqual(200,second.status_code,second.text)
        self.assertIn('"status": "created"',second.text)
        self.assertEqual('copied prompt',AgentStore(self.db).list()[0]['instructions']);self.generate.assert_not_called()

    def test_cancel_endpoint_clears_persisted_pending_mode(self):
        self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p','agent_builder':True,'message':'指示: source'})
        response=self.client.post('/api/agents/builder/c/cancel')
        self.assertEqual(200,response.status_code)
        self.assertEqual('cancelled',response.json()['blocks'][0]['status'])

    def test_development_planner_request_reaches_generation_and_saves_after_answer(self):
        self.generate.side_effect=[
            {'text':json.dumps({'action':'ask','questions':['計画はチェックリスト形式にしますか？']})},
            {'text':json.dumps({'action':'create','name':'開発計画サポート',
                               'instructions':'実装詳細を一問ずつ質問し、回答に基づいてチェックリスト形式の開発計画を立てる。'})}]
        first=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p',
            'message':'開発計画をユーザーに実装詳細について質問しながら立ててくれるエージェントを作りたい'})
        self.assertEqual(200,first.status_code,first.text)
        self.assertIn('チェックリスト',first.json()['response'])
        self.assertEqual('generate',first.json()['blocks'][0]['mode'])
        second=self.client.post('/api/agents/builder/chat',json={'conversation_id':'c','project_id':'p','message':'はい'})
        self.assertEqual(200,second.status_code,second.text)
        self.assertEqual('created',second.json()['blocks'][0]['status'])
        agents=AgentStore(self.db).list()
        self.assertEqual(1,len(agents));self.assertEqual('開発計画サポート',agents[0]['name'])
        self.assertIn('チェックリスト',self.generate.call_args.args[1])
        self.assertIn('do not ask the creator',self.generate.call_args.args[0][0]['content'])
