import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api.db import Database
from olcr_api.config import Settings
from olcr_api.service_connectors import ServiceConnectors


class ConnectorChatIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'isolated.db');self.db.initialize()
        self.db.create_conversation('Connectors',1,'c',None)
        self.google=Mock();self.google.status.return_value={'state':'connected'};self.google.execute.return_value={'messages':[{'id':'example','snippet':'Meeting at noon'}]}
        self.settings=Settings(permission_level='automatic',external_access_enabled=True)
        self.service=ServiceConnectors(self.db,lambda:self.settings,self.google)
        self.service.set_category('mail',True);self.service.policy('service.google.gmail',{'enabled':True})
        for target,value in [('db',self.db),('settings',self.settings),('_gui_session_token','connector-unit-session'),('get_service_connectors',lambda:self.service)]:
            item=patch.object(api,target,value);item.start();self.addCleanup(item.stop)
        self.client=TestClient(api.app);self.client.headers['X-OLCR-Session']='connector-unit-session'

    def test_stream_routes_to_new_connector_without_model_or_coding_execution(self):
        with patch.object(api.runtime,'execute',side_effect=AssertionError('Must not call existing runtime')):
            response=self.client.post('/api/chat/stream',json={'message':'/service service.google.gmail list {}','conversation_id':'c'})
        self.assertEqual(200,response.status_code,response.text)
        self.assertIn('SERVICE_CONNECTOR',response.text);self.assertIn('Meeting at noon',response.text)
        self.google.execute.assert_called_once_with('service.google.gmail','list',{})
        with self.db.connect() as con:self.assertEqual(0,con.execute('SELECT count(*) FROM coding_tasks').fetchone()[0])

    def test_api_requires_existing_gui_session(self):
        self.assertEqual(401,self.client.get('/api/connectors',headers={'X-OLCR-Session':'bad'}).status_code)
        self.assertEqual(200,self.client.get('/api/connectors').status_code)
        self.settings.permission_level='confirm'
        response=self.client.post('/api/connectors/service.google.gmail/execute',json={'action':'list'})
        self.assertEqual(409,response.status_code)
        self.google.execute.assert_not_called()

    def test_natural_mail_request_reaches_review_before_help_in_both_transports(self):
        import json
        self.service.apple_mail=Mock()
        self.service.apple_mail.status.return_value={'state':'connected'}
        self.service.policy('service.apple.mail',{'enabled':True})
        request='friend@example.org 宛てに、私のアップルのメアドから、京都での旅行の予定をどうするか尋ねるメールを出して'
        for index,path in enumerate(('/api/chat','/api/chat/stream')):
            with self.subTest(path=path), patch.object(api.runtime,'_generate_brain',return_value={'text':json.dumps({'subject':'旅行の相談','body':'京都旅行の予定を教えてください。'})}), patch.object(api,'_operation_help_response',side_effect=AssertionError('Mail must not reach help')):
                response=self.client.post(path,json={'message':request,'conversation_id':'c','message_id':f'mail-{index}'})
            self.assertEqual(200,response.status_code,response.text)
            if path.endswith('/stream'):
                frames=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                self.assertEqual(frames[-1]['response_owner'],'ASSISTANT_WORKFLOW')
                block=next(f['block'] for f in frames if f['type']=='block')
            else:block=response.json()['blocks'][0]
            self.assertEqual((block['domain'],block['status']),('mail','review'))
        self.service.apple_mail.execute.assert_not_called()

    def test_mail_stream_reports_drafting_and_sending_before_provider_finishes(self):
        import json
        import threading
        self.service.apple_mail=Mock();self.service.apple_mail.status.return_value={'state':'connected'}
        self.service.policy('service.apple.mail',{'enabled':True})
        request='friend@example.org宛てにApple Mailで旅行の予定を尋ねるメールを送って'
        with patch.object(api.runtime,'_generate_brain',return_value={'text':json.dumps({'subject':'旅行の相談','body':'旅行の予定を教えてください。'})}):
            response=self.client.post('/api/chat/stream',json={'message':request,'conversation_id':'c'})
        frames=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        self.assertIn('メールの本文案を作成しています',frames[1]['reasoning']['text'])
        draft=next(f['block'] for f in frames if f['type']=='block')
        self.assertEqual(draft['status'],'review')

        started=threading.Event();finished=threading.Event()
        def delayed_send(*args):
            started.set()
            finished.wait(2)
            return {'sent':True}
        self.service.apple_mail.execute.side_effect=delayed_send
        with self.client.stream('POST','/api/chat/stream',json={'message':f"/workflow {draft['id']} confirm",'conversation_id':'c'}) as response:
            lines=iter(line for line in response.iter_lines() if line)
            first=json.loads(next(lines)[6:]);status=json.loads(next(lines)[6:])
            self.assertEqual(first['type'],'meta')
            self.assertIn('Apple Mailへ送信を依頼しています',status['reasoning']['text'])
            self.assertTrue(started.wait(.2))
            self.assertFalse(finished.is_set())
            finished.set()
            frames=[json.loads(line[6:]) for line in lines if line.startswith('data: ')]
        self.assertTrue(any(f.get('response_owner')=='ASSISTANT_WORKFLOW' for f in frames))
        result=next(f['block'] for f in frames if f['type']=='block')
        self.assertEqual(result['status'],'accepted')
        self.assertEqual(result['title'],'送信依頼済み')
        self.assertIn('Apple Mail', '\n'.join(result['details']))

    def test_purchase_stream_contains_review_block(self):
        self.service.policy('service.commerce.checkout',{'enabled':True,'purchase':True})
        import json
        order={'merchant':'shop.example','url':'https://shop.example/checkout','items':[{'name':'Book','quantity':1,'unit_price':'10'}],'subtotal':'10','shipping':'0','tax':'0','total':'10','currency':'USD'}
        response=self.client.post('/api/chat/stream',json={'message':'/service service.commerce.checkout review '+json.dumps(order),'conversation_id':'c'})
        self.assertEqual(200,response.status_code,response.text);self.assertIn('service_purchase',response.text)

    def test_malformed_command_and_provider_failure_return_controlled_errors(self):
        response=self.client.post('/api/chat/stream',json={'message':'/service service.google.gmail list {bad}','conversation_id':'c'})
        self.assertEqual(422,response.status_code)
        self.google.execute.side_effect=ValueError('CONNECTOR_RECONNECT_REQUIRED')
        response=self.client.post('/api/chat/stream',json={'message':'/service service.google.gmail list {}','conversation_id':'c'})
        self.assertEqual(422,response.status_code)
        self.assertIn('CONNECTOR_RECONNECT_REQUIRED',response.text)
