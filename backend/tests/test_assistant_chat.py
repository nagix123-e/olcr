"""Exercise the real chat/stream/command integration with an isolated DB and no models."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from olcr_api.db import Database

_bootstrap = tempfile.TemporaryDirectory(prefix='olcr-assistant-test-')
with patch.dict(os.environ, {'OLCR_DB_PATH': str(Path(_bootstrap.name)/'bootstrap.db'),
                            'OLCR_VECTOR_ENABLED':'false', 'OLCR_GUI_SESSION_TOKEN':''}), \
     patch('olcr_api.laya_runtime.preflight', return_value={'ready':False,'health':'TEST','reason':'TEST_MODEL_DISABLED'}):
    import olcr_api.app as api


class AssistantChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db = Database(str(Path(self.temp.name)/'chat.db'))
        db.initialize()
        self.addCleanup(patch.stopall)
        patch.object(api, 'db', db).start()
        patch.object(api, 'settings', api.settings.with_overrides({'external_access_enabled':True,'permission_level':'automatic','task_manager_enabled':False})).start()
        patch.object(api, '_assistant_services', {}).start()
        patch.object(api, '_gui_session_token', '').start()
        self.execute = patch.object(api.runtime, 'execute', side_effect=AssertionError('Everyday actions must not call normal inference')).start()
        self.client = TestClient(api.app)

    def test_youtube_creator_requests_never_enter_normal_inference(self):
        patch.object(api,'settings',api.settings.with_overrides({'external_access_enabled':True,'permission_level':'automatic','task_manager_enabled':True})).start()
        requests=['Vaundyの まぶた をかけて','米津玄師のLemonを流して','YouTube MusicでVaundyのまぶたを再生して','VaundyのMVが見たい','クイズノックの動画で、しりとりに関係するものを見せて','クイズの解説動画が見たい','youtubeで、最新のpewdiepieの動画を出して','pewdiepieの動画を再生して','ようつべで東海オンエアのおすすめの動画を見せて']
        for route in ('/api/chat','/api/chat/stream'):
            for message in requests:
                with self.subTest(route=route,message=message),patch('olcr_api.assistant_actions.search_youtube',return_value=[{'id':'dQw4w9WgXcQ','title':'Video'}]) as search:
                    result=self.client.post(route,json={'message':message,'project_id':None})
                    self.assertEqual(200,result.status_code,result.text)
                    if route.endswith('/stream'):
                        events=[json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
                        block=next(event['block'] for event in events if event['type']=='block')
                    else:block=result.json()['blocks'][0]
                    self.assertEqual('media',block['kind'])
                    search.assert_called_once()
                    if '最新' in message:self.assertEqual(('pewdiepie',),search.call_args.args[:1]);self.assertEqual('date',search.call_args.kwargs['order'])
        self.execute.assert_not_called()
        with api.db.connect() as con:self.assertEqual(0,con.execute('SELECT count(*) FROM coding_tasks').fetchone()[0])

    def test_knowledge_youtube_supplements_are_opt_in_or_exact_title_matches(self):
        from olcr_api.knowledge_media import explicit_knowledge_video_request, youtube_query_for_knowledge
        from unittest.mock import Mock

        combined='PewDiePieについて教えて。関連するYouTube動画も見せて'
        self.assertTrue(explicit_knowledge_video_request(combined))
        self.assertEqual('PewDiePie',youtube_query_for_knowledge(combined,'PewDiePieは動画クリエイターです。'))
        self.assertFalse(explicit_knowledge_video_request('PewDiePieについて教えて'))
        self.assertIsNone(youtube_query_for_knowledge('今日は天気について教えて','晴れです。'))
        self.assertEqual('千と千尋の神隠し',youtube_query_for_knowledge('映画『千と千尋の神隠し』について教えて','『千と千尋の神隠し』は長編アニメ映画です。'))

        service=Mock();service.youtube_key='';service.network.return_value=None
        patch.object(api,'get_assistant_service',return_value=service).start()
        patch('olcr_api.assistant_integrations.search_youtube',return_value=[{'id':'dQw4w9WgXcQ','title':'PewDiePie: Video','url':'https://www.youtube.com/watch?v=dQw4w9WgXcQ'}]).start()
        response,blocks=api._knowledge_youtube_supplement(combined,'PewDiePie is a creator.','conversation',knowledge=True)
        self.assertEqual('PewDiePie is a creator.',response)
        self.assertEqual('media',blocks[0]['kind']);self.assertFalse(blocks[0]['autoplay'])
        self.assertEqual('PewDiePie',blocks[0]['query'])
        service.network.assert_called_once_with()
        response,blocks=api._knowledge_youtube_supplement('PewDiePieについて教えて','PewDiePie is a creator.','conversation',knowledge=True)
        self.assertEqual([],blocks)
        response,blocks=api._knowledge_youtube_supplement('関連する動画を教えて','[元動画](https://youtu.be/dQw4w9WgXcQ)','conversation',knowledge=True)
        self.assertEqual('元動画',response)
        self.assertEqual('dQw4w9WgXcQ',blocks[0]['candidates'][0]['id'])

    def test_normal_and_streaming_chat_share_state_and_idempotency(self):
        result = self.client.post('/api/chat', json={'message':'5分のタイマーをかけて','project_id':None,'message_id':'same'}).json()
        conversation = result['conversation_id']
        timer = result['blocks'][0]['timers'][0]
        again = self.client.post('/api/chat', json={'message':'5分のタイマーをかけて','project_id':None,'conversation_id':conversation,'message_id':'same'}).json()
        self.assertEqual(timer['id'], again['blocks'][0]['timers'][0]['id'])
        self.assertEqual(2, len(api.db.conversation(conversation)['messages']))
        streamed = self.client.post('/api/chat/stream', json={'message':'タイマーを一時停止して','conversation_id':conversation,'project_id':None}, headers={'X-OLCR-Language':'en'})
        self.assertEqual(200, streamed.status_code, streamed.text)
        events = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith('data: ')]
        block = next(e['block'] for e in events if e['type']=='block')
        self.assertEqual('paused', block['timers'][0]['status'])
        self.assertEqual('ASSISTANT_ACTION', events[-1]['response_owner'])
        self.assertIn('Here are your timers.', streamed.text)
        # A short voice follow-up is scoped by the preceding assistant action.
        resumed = self.client.post('/api/chat', json={'message':'再開して','conversation_id':conversation,'project_id':None}).json()
        self.assertEqual('running', resumed['blocks'][0]['timers'][0]['status'])
        self.execute.assert_not_called()

    def test_alarm_stream_and_contextual_stop_use_wall_clock_without_inference(self):
        streamed=self.client.post('/api/chat/stream',json={'message':'明日の午前7時半にアラームをかけて','project_id':None,'client_time_zone':'Asia/Tokyo'})
        self.assertEqual(200,streamed.status_code,streamed.text)
        events=[json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith('data: ')]
        alarm=next(e['block'] for e in events if e['type']=='block')['timers'][0]
        self.assertEqual('alarm',alarm['kind'])
        cid=next(e['conversation_id'] for e in events if e.get('conversation_id'))
        stopped=self.client.post('/api/chat',json={'message':'止めて','project_id':None,'conversation_id':cid}).json()
        self.assertEqual('cancelled',stopped['blocks'][0]['timers'][0]['status'])
        self.assertEqual(alarm['id'],stopped['blocks'][0]['timers'][0]['id'])
        self.execute.assert_not_called()

    def test_short_alarm_corrections_keep_action_ownership_in_chat_and_stream(self):
        from datetime import datetime
        now=datetime.fromisoformat('2026-10-29T17:39:00+09:00').timestamp()
        with patch('olcr_api.assistant_actions.time.time',return_value=now),patch.object(api,'settings',api.settings.with_overrides({'task_manager_enabled':True})):
            first=self.client.post('/api/chat',json={'message':'6時にアラームをセットして','project_id':None,'client_time_zone':'Asia/Tokyo'}).json()
            cid=first['conversation_id'];original=first['blocks'][0]['timers'][0]
            response=self.client.post('/api/chat/stream',json={'message':'今日の六時','conversation_id':cid,'project_id':None,'client_time_zone':'Asia/Tokyo'})
            self.assertEqual(200,response.status_code,response.text)
            events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
            self.assertEqual('ASSISTANT_ACTION',events[-1]['response_owner'])
            self.assertEqual('clarification',next(e['block'] for e in events if e['type']=='block')['kind'])
            changed=self.client.post('/api/chat',json={'message':'はい','conversation_id':cid,'project_id':None,'client_time_zone':'Asia/Tokyo'}).json()
            self.assertEqual(original['id'],changed['blocks'][0]['timers'][0]['id'])
            self.assertEqual(datetime.fromisoformat('2026-10-29T18:00:00+09:00').timestamp(),changed['blocks'][0]['timers'][0]['deadline'])
            self.assertEqual([],api.db.coding_tasks(cid));self.execute.assert_not_called()

    def test_slash_commands_do_not_require_a_project(self):
        catalog = self.client.get('/api/commands').json()['commands']
        self.assertIn('/timer start', {c['command'] for c in catalog})
        result = self.client.post('/api/commands', json={'text':'/timer start 10 minutes','project_id':None})
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual(600, result.json()['blocks'][0]['timers'][0]['duration'])
        self.assertIsNone(api.db.conversation(result.json()['conversation_id'])['project_id'])

    def test_only_unguessable_media_ticket_page_is_public(self):
        patch.object(api, '_gui_session_token', 'fixture-only-session').start()
        self.assertEqual(401, self.client.get('/api/assistant/settings').status_code)
        self.assertEqual(401, self.client.post('/api/assistant/calendar/credential/export').status_code)
        issued = self.client.post('/api/assistant/media/player', headers={'X-OLCR-Session':'fixture-only-session'}, json={'video_id':'dQw4w9WgXcQ'}).json()
        page = self.client.get(issued['path'])
        self.assertEqual(200, page.status_code, page.text)
        self.assertNotIn('fixture-only-session', page.text)
        self.assertEqual(404, self.client.get('/assistant-player/'+'x'*43).status_code)
        self.assertEqual(401, self.client.get('/assistant-player/not-a-ticket').status_code)


if __name__ == '__main__':
    unittest.main()
