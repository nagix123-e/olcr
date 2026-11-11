import json
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from olcr_api.assistant_actions import AssistantActions, ActionError, action_intent, duration_seconds, event_proposal
from olcr_api.assistant_api import assistant_router
from olcr_api.assistant_integrations import GoogleCalendar, PlayerTickets, search_youtube, video_id
from olcr_api.db import Database


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(str(Path(self.temp.name) / 'actions.db'))
        self.db.initialize()
        self.db.create_conversation('Everyday', time.time(), 'chat', None)
        self.settings = SimpleNamespace(permission_level='automatic', external_access_enabled=True)
        self.calls = []
        self.events = {}

        def transport(request):
            self.calls.append(request)
            if str(request.url) == 'https://oauth2.googleapis.com/token':
                return httpx.Response(200, json={'refresh_token': 'test-refresh', 'access_token': 'test-access', 'expires_in': 3600})
            if request.method == 'POST':
                event = json.loads(request.content)
                self.events[event['id']] = {**event, 'etag': '"v1"'}
                return httpx.Response(200, json=self.events[event['id']])
            if request.method == 'PATCH':
                return httpx.Response(200, json={**json.loads(request.content), 'id': 'event1', 'etag': '"v2"'})
            if request.method == 'DELETE':
                return httpx.Response(204)
            return httpx.Response(200, json={'id': 'event1', 'summary': 'Meeting', 'etag': '"v1"',
                'start': {'dateTime': '2026-10-27T15:00:00+09:00'}, 'end': {'dateTime': '2026-10-27T16:00:00+09:00'}})
        calendar = GoogleCalendar(httpx.Client(transport=httpx.MockTransport(transport)))
        calendar.credentials = {'client_id': 'test.apps.googleusercontent.com', 'refresh_token': 'test-refresh'}
        self.service = AssistantActions(self.db, lambda: self.settings, calendar)
        self.service.save_preferences('primary', 'Asia/Tokyo')
        app = FastAPI()
        app.include_router(assistant_router(lambda: self.service))
        self.client = TestClient(app)

    def handle(self, text, source='request', language='ja'):
        return self.service.handle(action_intent(text), 'chat', source, language)

    def test_calendar_natural_dates_kanji_and_ranges(self):
        now=datetime.fromisoformat('2026-10-29T22:22:40+09:00')
        cases=[('明日朝八時に「学校に行く」という予定を入れて','2026-10-30T08:00:00+09:00','2026-10-30T09:00:00+09:00'),('明日の午後三時半から五時まで「会議」を追加','2026-10-30T15:30:00+09:00','2026-10-30T17:00:00+09:00'),('十月三十一日の夜八時から二時間「映画」を追加','2026-10-31T20:00:00+09:00','2026-10-31T22:00:00+09:00'),('tomorrow at 8 am "School"','2026-10-30T08:00:00+09:00','2026-10-30T09:00:00+09:00')]
        for text,start,end in cases:
            with self.subTest(text=text):
                proposal=event_proposal(text,'Asia/Tokyo',now)
                self.assertEqual(start,proposal['start']);self.assertEqual(end,proposal['end'])
        proposal=event_proposal('明日終日「八時の映画」を追加','Asia/Tokyo',now)
        self.assertEqual('2026-10-30',proposal['start']);self.assertEqual('2026-10-31',proposal['end']);self.assertTrue(proposal['all_day'])

    def test_calendar_metadata_survives_validation_without_affecting_times(self):
        from olcr_api.assistant_actions import event_proposal
        text='明日8時に「面談」を登録、場所は会議室A、メモ: 議題は10時の報告、通話URL: https://meet.example.com/room、カレンダー: 職場'
        value=event_proposal(text,'Asia/Tokyo',datetime(2026,10,30,19,tzinfo=ZoneInfo('Asia/Tokyo')))
        self.assertEqual('2026-10-31T08:00:00+09:00',value['start'])
        self.assertEqual('2026-10-31T09:00:00+09:00',value['end'])
        self.assertEqual('職場',value.pop('calendar_name'))
        validated=self.service.validated_event(value)
        self.assertEqual('会議室A',validated['location'])
        self.assertEqual('議題は10時の報告',validated['description'])
        self.assertEqual('https://meet.example.com/room',validated['url'])
        with self.assertRaises(ActionError):self.service.validated_event({**value,'url':'javascript:alert(1)'})
        self.assertEqual(['招待者'],event_proposal('明日8時に「会議」を登録、招待者: person@example.com','Asia/Tokyo')['unsupported_fields'])

    def test_youtube_query_normalization_and_date_sort(self):
        from olcr_api.assistant_actions import youtube_request
        cases=[('youtubeで、最新のpewdiepieの動画を出して',('pewdiepie','date')),('ようつべで東海オンエアの動画を再生して',('東海オンエア','relevance')),('Show me the latest videos by PewDiePie',('PewDiePie','date'))]
        for text,expected in cases:
            self.assertEqual('youtube',action_intent(text)['service'])
            query,order=youtube_request(text)
            self.assertEqual(expected,(query,order))
        response=type('Response',(),{'status_code':200,'content':b'fixture','text':'fixture'})()
        client=type('Client',(),{})()
        from unittest.mock import Mock
        client.get=Mock(return_value=response)
        with patch('olcr_api.assistant_integrations.youtube_search_results',return_value=[{'id':'dQw4w9WgXcQ','title':'Video'}]):
            search_youtube('PewDiePie',client=client,order='date')
        self.assertEqual('CAI=',client.get.call_args.kwargs['params']['sp'])
        self.assertEqual('PewDiePie',client.get.call_args.kwargs['params']['search_query'])

    def test_recommended_video_requests_search_without_autoplay(self):
        for request in ('piewdiepieのおすすめの動画を出して。','おすすめの動画を見せて','Recommend videos by PewDiePie'):
            with self.subTest(request=request):
                self.assertEqual('youtube',action_intent(request)['service'])
                with patch('olcr_api.assistant_actions.search_youtube',return_value=[{'id':'dQw4w9WgXcQ','title':'Video'}]) as search:
                    result=self.handle(request,source=request)
                search.assert_called_once()
                self.assertEqual('media',result['blocks'][0]['kind'])
                self.assertFalse(result['blocks'][0]['autoplay'])

    def test_bare_music_playback_requests_use_youtube(self):
        requests=['Vaundyの まぶた をかけて','Vaundyのまぶたかけて',
            '米津玄師のLemonを流して','「まぶた」を聴かせて','Vaundyをかけて',
            'YouTube MusicでVaundyのまぶたを再生して',
            'ミュージックビデオを見せて','VaundyのMVが見たい','play Mabuta by Vaundy']
        for request in requests:
            with self.subTest(request=request), patch('olcr_api.assistant_actions.search_youtube',
                    return_value=[{'id':'dQw4w9WgXcQ','title':'Music video'}]) as search:
                self.assertEqual('youtube',action_intent(request)['service'])
                block=self.handle(request,source=request)['blocks'][0]
                self.assertEqual('media',block['kind'])
                search.assert_called_once()
                self.assertNotRegex(block['query'],r'かけて|流して|聴かせて|再生して')
        for request in ['母に電話をかけて','鍵をかけて','処理に負荷をかけて',
                '5分のタイマーをかけて','明日6時にアラームをかけて',
                'Vaundyのまぶたについて教えて','音楽を再生する機能を実装して',
                'ローカルファイルを再生して']:
            with self.subTest(non_music=request):
                intent=action_intent(request)
                self.assertNotEqual('youtube',intent['service'] if intent else None)

    def test_intent_and_durations_do_not_hijack_coding_or_calendar_questions(self):
        for text in ('タイマー機能を実装して', 'implement a timer widget', 'YouTube APIの設計を教えて'):
            self.assertIsNone(action_intent(text))
        self.assertEqual('calendar', action_intent('今日の予定は？')['service'])
        self.assertEqual('youtube', action_intent('音楽を流して')['service'])
        for text, expected in [('三分半', 210), ('1時間30分', 5400), ('one hour and thirty minutes', 5400), ('５分', 300), ('15 seconds', 15)]:
            self.assertEqual(expected, duration_seconds(text))
        for text in ('-5 minutes', '0秒', '8 days'):
            with self.assertRaises(ActionError):
                duration_seconds(text)

    def test_timer_survives_reload_is_idempotent_and_cancel_does_not_affect_another(self):
        first = self.handle('5分のタイマーをかけて')['blocks'][0]['timers'][0]
        self.assertEqual(first, self.handle('5分のタイマーをかけて')['blocks'][0]['timers'][0])
        second = self.handle('10分のタイマーをかけて', 'second')['blocks'][0]['timers'][0]
        self.assertEqual(2, len(self.service.timers(active=True)))
        self.handle('タイマーを止めて', 'ambiguous')
        self.assertEqual(2, len(self.service.timers(active=True)))
        paused = self.service.update_timer(first['id'], 'pause')
        self.assertEqual('paused', paused['status'])
        self.assertIsNone(paused['deadline'])
        reloaded = AssistantActions(self.db, lambda: self.settings)
        self.assertEqual('paused', reloaded.timer(first['id'])['status'])
        self.assertEqual('running', reloaded.update_timer(first['id'], 'resume')['status'])
        reloaded.update_timer(first['id'], 'cancel')
        self.assertEqual('running', reloaded.timer(second['id'])['status'])
        with self.db.connect() as con:
            con.execute('UPDATE assistant_timers SET deadline=? WHERE id=?', (time.time() - 1, second['id']))
        self.assertEqual('finished', reloaded.timer(second['id'])['status'])
        self.assertEqual({}, self.db.load_application_settings())

    def test_calendar_dates_and_preview_require_real_confirmation(self):
        proposal = event_proposal('明日の15時に「歯医者」の予定を追加して', 'Asia/Tokyo', datetime.fromisoformat('2026-10-26T12:00:00+09:00'))
        self.assertEqual('歯医者', proposal['summary'])
        self.assertEqual('2026-10-27T15:00:00+09:00', proposal['start'])
        prepared = self.client.post('/api/assistant/calendar/actions', json={'conversation_id': 'chat', 'payload': proposal})
        self.assertEqual(200, prepared.status_code, prepared.text)
        item = prepared.json()['action']
        self.assertFalse(self.calls)
        bad = self.client.post(f"/api/assistant/calendar/actions/{item['id']}/confirm", json={'conversation_id':'other','revision':item['revision'],'payload':proposal})
        self.assertEqual(422, bad.status_code)
        self.assertFalse(self.calls)
        confirmed = self.client.post(f"/api/assistant/calendar/actions/{item['id']}/confirm", json={'conversation_id':'chat','revision':item['revision'],'payload':proposal})
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        self.assertEqual('completed', confirmed.json()['action']['status'])
        self.client.post(f"/api/assistant/calendar/actions/{item['id']}/confirm", json={'conversation_id':'chat','revision':item['revision'],'payload':proposal})
        self.assertEqual(1, sum(r.method == 'POST' and '/events' in str(r.url) for r in self.calls))
        history = self.db.conversation('chat')['messages']
        self.assertEqual(2, len(history))
        self.assertFalse('test-refresh' in Path(self.db.path).read_bytes().decode(errors='ignore'))

    def test_long_running_timer_remains_addressable_after_many_completed_timers(self):
        active = self.service.start_timer(86400, 'Long timer', 'chat')
        now = time.time()
        with self.db.connect() as con:
            con.executemany("INSERT INTO assistant_timers(id,conversation_id,name,duration,deadline,remaining,status,notification_status,created_at,updated_at) VALUES(?,?,?,?,?,?,'finished','scheduled',?,?)",
                [(f'finished-{i}','chat','Done',1,now-1,0,now+i,now) for i in range(320)])
        self.assertEqual([active['id']], [t['id'] for t in self.service.timers(active=True)])
        self.assertEqual('cancelled', self.service.update_timer(active['id'],'cancel')['status'])

    def test_calendar_uses_etag_and_does_not_retry_stale_changes(self):
        item = self.service.prepare_calendar('chat', 'delete', 'event1')
        self.service.confirm_calendar(item['id'], 'chat', item['revision'])
        delete = next(r for r in self.calls if r.method == 'DELETE')
        self.assertEqual('"v1"', delete.headers['If-Match'])
        item = self.service.prepare_calendar('chat', 'update', 'event1')
        with patch.object(self.service.calendar, 'request', side_effect=ActionError('CALENDAR_EVENT_CHANGED')):
            with self.assertRaisesRegex(ActionError, 'CALENDAR_EVENT_CHANGED'):
                self.service.confirm_calendar(item['id'], 'chat', item['revision'])
        self.assertEqual('failed', self.service.calendar_action(item['id'], 'chat')['status'])

    def test_calendar_network_uncertainty_prevents_duplicate_write(self):
        proposal = {'summary':'Test','start':'2026-10-27T15:00','end':'2026-10-27T16:00','timeZone':'Asia/Tokyo'}
        item = self.service.prepare_calendar('chat', payload=proposal)
        with patch.object(self.service.calendar, 'request', side_effect=ActionError('CALENDAR_NETWORK_FAILED')) as call:
            with self.assertRaises(ActionError):
                self.service.confirm_calendar(item['id'], 'chat', item['revision'])
            with self.assertRaisesRegex(ActionError, 'CALENDAR_ACTION_BUSY'):
                self.service.confirm_calendar(item['id'], 'chat', item['revision'])
            self.assertEqual(1, call.call_count)
        self.assertEqual('uncertain', self.service.calendar_action(item['id'], 'chat')['status'])

    def test_spoken_calendar_move_preserves_duration_and_can_be_cancelled(self):
        event = {'id':'event1','summary':'会議','etag':'"v1"','start':{'dateTime':'2026-10-27T15:00:00+09:00'},'end':{'dateTime':'2026-10-27T16:00:00+09:00'}}
        with patch.object(self.service.calendar, 'events', return_value={'events':[event],'more':False}), patch.object(self.service.calendar, 'request', return_value=event) as request:
            result = self.handle('明日の会議を16時に変更して', 'move')
            item = result['blocks'][0]['action']
            self.assertEqual('update', item['action'])
            start, end = (datetime.fromisoformat(item['payload'][key]) for key in ('start','end'))
            self.assertEqual(16, start.hour)
            self.assertEqual(3600, (end-start).total_seconds())
            self.assertTrue(all(call.args[0] == 'GET' for call in request.call_args_list))
            self.settings.external_access_enabled = False
            self.assertEqual('cancelled', self.service.cancel_calendar(item['id'],'chat',item['revision'])['status'])
            with self.assertRaises(ActionError):
                self.service.confirm_calendar(item['id'],'chat',item['revision'])

    def test_nonexistent_daylight_saving_time_is_not_silently_shifted(self):
        with self.assertRaisesRegex(ActionError,'CALENDAR_TIME_REQUIRED'):
            self.service.validated_event({'summary':'Clock gap','start':'2026-03-08T02:30','end':'2026-03-08T04:30','timeZone':'America/New_York'})

    def test_revoked_google_refresh_token_reports_reconnect_not_network_failure(self):
        self.service.calendar.http = httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(400,json={'error':'invalid_grant'})))
        with self.assertRaisesRegex(ActionError,'CALENDAR_RECONNECT_REQUIRED'):
            self.service.calendar._access()
        self.assertTrue(self.service.calendar.status()['reauth_required'])

    def test_disabled_network_blocks_media_and_calendar_but_not_local_timers(self):
        self.settings.external_access_enabled = False
        for text in ('YouTubeで音楽を再生して', '今日の予定を教えて'):
            self.assertEqual('EXTERNAL_ACCESS_DISABLED', self.handle(text, text)['blocks'][0]['error'])
        self.assertFalse(self.calls)
        self.assertEqual('running', self.handle('5分タイマー', 'local')['blocks'][0]['timers'][0]['status'])

    def test_player_tickets_are_bounded_and_do_not_expose_gui_or_oauth_credentials(self):
        self.assertEqual('dQw4w9WgXcQ', video_id('https://youtu.be/dQw4w9WgXcQ'))
        self.assertIsNone(video_id('https://youtube.com.attacker.test/watch?v=dQw4w9WgXcQ'))
        self.assertEqual('dQw4w9WgXcQ', search_youtube('https://www.youtube.com/watch?v=dQw4w9WgXcQ')[0]['id'])
        response = self.client.post('/api/assistant/media/player', json={'video_id':'dQw4w9WgXcQ'})
        ticket = response.json()['ticket']
        page = self.client.get(response.json()['path'])
        self.assertEqual(200, page.status_code)
        self.assertIn('www.youtube.com/iframe_api', page.text)
        self.assertIn('controls:1,fs:1', page.text)
        self.assertIn("embed.setAttribute('allowfullscreen','')", page.text)
        self.assertIn("picture-in-picture; fullscreen", page.text)
        self.assertIn('fullscreen=(self "https://www.youtube.com" "https://www.youtube-nocookie.com")', page.headers['permissions-policy'])
        self.assertIn('onAutoplayBlocked', page.text)
        self.assertNotIn('test-refresh', page.text)
        self.assertIn('no-store', page.headers['cache-control'])
        self.assertEqual(404, self.client.get('/assistant-player/' + ticket + 'wrong').status_code)
        self.settings.external_access_enabled = False
        self.assertEqual(404, self.client.get(response.json()['path']).status_code)

    def test_oauth_state_pkce_loopback_and_versioned_keychain_acknowledgement(self):
        calendar = self.service.calendar
        started = calendar.start('en')
        self.addCleanup(calendar.disconnect)
        query = parse_qs(urlsplit(started['attempt']['authorization_url']).query)
        self.assertEqual('S256', query['code_challenge_method'][0])
        redirect = query['redirect_uri'][0]
        wrong = httpx.get(redirect, params={'state':'invalid', 'code':'not-accepted'}, trust_env=False)
        self.assertEqual(400, wrong.status_code)
        self.assertFalse(self.calls)
        success = httpx.get(redirect, params={'state':query['state'][0], 'code':'accepted'}, trust_env=False)
        self.assertEqual(200, success.status_code)
        self.assertTrue(calendar.status()['connected'])
        secret = calendar.export()
        self.assertTrue(calendar.status()['credential_pending'])
        calendar.acknowledge(secret['generation'] - 1)
        self.assertTrue(calendar.status()['credential_pending'])
        calendar.acknowledge(secret['generation'])
        self.assertFalse(calendar.status()['credential_pending'])
        calendar.disconnect()
        self.assertNotIn('refresh_token', json.loads(calendar.export()['value']))

    def test_disconnect_during_oauth_exchange_cannot_reconnect(self):
        calendar = self.service.calendar
        attempt = {'status':'pending','deadline':time.monotonic()+30,'redirect_uri':'http://127.0.0.1:123/oauth/callback','verifier':'test','stop':threading.Event()}
        calendar.attempt = attempt
        def revoke(request):
            calendar.disconnect()
            return httpx.Response(200, json={'refresh_token':'late','access_token':'late'})
        calendar.http = httpx.Client(transport=httpx.MockTransport(revoke))
        self.assertFalse(calendar.complete(attempt, 'code'))
        self.assertNotIn('refresh_token', calendar.credentials)


if __name__ == '__main__':
    unittest.main()
