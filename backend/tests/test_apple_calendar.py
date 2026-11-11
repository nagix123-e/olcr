from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from olcr_api.apple_calendar import AppleCalendar
from olcr_api.assistant_actions import AssistantActions, action_intent
from olcr_api.assistant_api import assistant_router
from olcr_api.assistant_integrations import ActionError, search_youtube
from olcr_api.config import Settings
from olcr_api.db import Database


class NativeCalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(str(Path(self.tmp.name) / 'test.sqlite')); self.db.initialize()
        with self.db.connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS service_connector_categories (id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0)')
            con.execute("INSERT INTO service_connector_categories (id,enabled) VALUES ('calendar',1) ON CONFLICT(id) DO UPDATE SET enabled=1")
        self.db.create_conversation('Fixture', 1, 'chat', None)
        self.calls = []
        self.event = {'id': 'NATIVE_EVENT', 'summary': 'Dentist', 'etag': 'version-one', 'provider': 'apple',
            'start': {'dateTime': '2026-10-29T15:00:00+09:00'}, 'end': {'dateTime': '2026-10-29T16:00:00+09:00'}}
        def runner(value):
            self.calls.append(value)
            operation = value['operation']
            if operation in {'status', 'connect'}:
                return {'available': True, 'authorized': True, 'permission': 'authorized'}
            if operation == 'calendars':
                return {'calendars': [{'id': 'native', 'title': 'Home', 'writable': True}]}
            if operation == 'events': return {'events': [self.event], 'more': False}
            if operation in {'get', 'create', 'update', 'delete'}: return self.event
            raise AssertionError(operation)
        self.runner = runner
        self.calendar = AppleCalendar(self.db, runner=runner)
        self.settings = Settings(external_access_enabled=False)
        self.service = AssistantActions(self.db, lambda: self.settings, self.calendar)
        self.service.save_preferences('primary', 'Asia/Tokyo')
        app = FastAPI(); app.include_router(assistant_router(lambda: self.service))
        self.client = TestClient(app)

    def test_kanji_morning_event_needs_only_confirmation_and_connects_natively(self):
        from olcr_api.assistant_actions import event_proposal
        self.calendar.start()
        self.calls.clear()
        now=datetime.fromisoformat('2026-10-29T22:22:40+09:00').timestamp()
        with patch('olcr_api.assistant_actions.time.time',return_value=now):
            proposal=event_proposal('カレンダーに、明日朝八時に"学校に行く"という予定を入れて。','Asia/Tokyo',datetime.fromisoformat('2026-10-29T22:22:40+09:00'))
            item=self.service.prepare_calendar('chat',payload=proposal)
            self.assertFalse(any(call['operation']=='connect' for call in self.calls))
            self.assertFalse(any(call['operation']=='create' for call in self.calls))
            confirmed=self.service.confirm_calendar(item['id'],'chat',item['revision'])
            self.assertEqual('completed',confirmed['status'])
            creates=[call for call in self.calls if call['operation']=='create']
            self.assertEqual(1,len(creates))
            self.assertEqual('native',creates[0]['calendar_id'])
            self.assertEqual('2026-10-30T08:00:00+09:00',creates[0]['payload']['start']['dateTime'])
            self.assertEqual('2026-10-30T09:00:00+09:00',creates[0]['payload']['end']['dateTime'])
            self.service.confirm_calendar(item['id'],'chat',item['revision'])
            self.assertEqual(1,len([call for call in self.calls if call['operation']=='create']))

    def test_read_only_default_calendar_uses_a_writable_destination(self):
        self.calendar.start()
        with patch.object(self.calendar,'calendars',return_value=[{'id':'read-only','writable':False,'primary':True},{'id':'home','writable':True}]):
            item=self.service.prepare_calendar('chat',payload={'summary':'School','start':'2026-10-30T08:00:00+09:00','end':'2026-10-30T09:00:00+09:00','timeZone':'Asia/Tokyo'})
        self.assertEqual('home',item['calendar_id'])

    def test_one_native_connection_needs_no_client_keys_or_external_network(self):
        state = self.client.get('/api/assistant/settings').json()
        self.assertEqual('apple', state['calendar']['provider'])
        self.assertFalse(state['calendar']['connected'])
        self.assertTrue(state['youtube_search_configured'])
        connected = self.client.post('/api/assistant/calendar/connect')
        self.assertEqual(200, connected.status_code, connected.text)
        self.assertTrue(connected.json()['connected'])
        self.assertFalse(connected.json()['credential_pending'])
        calendars = self.client.get('/api/assistant/calendar/calendars')
        self.assertEqual(200, calendars.status_code, calendars.text)
        self.assertEqual('Home', calendars.json()['calendars'][0]['title'])
        self.assertTrue(AppleCalendar(self.db, runner=self.runner).status()['connected'])
        self.client.post('/api/assistant/calendar/disconnect')
        self.assertFalse(self.calendar.status()['connected'])
        with self.assertRaisesRegex(ActionError, 'CALENDAR_NOT_CONNECTED'):
            self.calendar.events('primary', datetime.now(), datetime.now())

    def test_calendar_creation_still_requires_review_and_only_executes_once(self):
        self.calendar.start()
        payload = {'summary': 'Dentist', 'start': '2026-10-29T15:00:00+09:00',
                   'end': '2026-10-29T16:00:00+09:00', 'timeZone': 'Asia/Tokyo'}
        prepared = self.client.post('/api/assistant/calendar/actions', json={'conversation_id': 'chat', 'payload': payload})
        self.assertEqual(200, prepared.status_code, prepared.text)
        action = prepared.json()['action']
        self.assertEqual('apple', action['provider'])
        self.assertFalse(any(call['operation'] == 'create' for call in self.calls))
        path = f"/api/assistant/calendar/actions/{action['id']}/confirm"
        body = {'conversation_id': 'chat', 'revision': action['revision'], 'payload': payload}
        for _ in range(2):
            result = self.client.post(path, json=body)
            self.assertEqual(200, result.status_code, result.text)
            self.assertEqual('completed', result.json()['action']['status'])
        writes = [call for call in self.calls if call['operation'] == 'create']
        self.assertEqual(1, len(writes))
        self.assertEqual('Asia/Tokyo', writes[0]['time_zone'])
        self.assertEqual('Dentist', writes[0]['payload']['summary'])

    def test_chat_write_request_previews_then_writes_to_native_calendar(self):
        self.calendar.start()
        result=self.service.handle({**action_intent('カレンダーに明日15時に「歯医者」を書き込んで、場所: 新宿、メモ: 保険証、URL: https://example.com'),'time_zone':'Asia/Tokyo'},'chat','native-write')
        action=result['blocks'][0]['action']
        self.assertEqual('create',action['action'])
        self.assertEqual('歯医者',action['payload']['summary'])
        self.assertTrue(action['payload']['start'].endswith('15:00:00+09:00'))
        self.assertFalse(any(c['operation']=='create' for c in self.calls))
        confirmed=self.service.confirm_calendar(action['id'],'chat',action['revision'],action['payload'])
        self.assertEqual('completed',confirmed['status'])
        writes=[c for c in self.calls if c['operation']=='create']
        self.assertEqual(1,len(writes))
        self.assertEqual('歯医者',writes[0]['payload']['summary'])
        self.assertEqual('新宿',writes[0]['payload']['location'])
        self.assertEqual('保険証',writes[0]['payload']['description'])
        self.assertEqual('https://example.com',writes[0]['payload']['url'])

    def test_native_queries_work_with_external_access_disabled(self):
        self.calendar.start()
        result = self.service.handle(action_intent('明日の予定は？'), 'chat', 'native-query')
        self.assertEqual('calendar_events', result['blocks'][0]['kind'])
        self.assertEqual('Dentist', result['blocks'][0]['events'][0]['summary'])

    def test_youtube_search_blocking_offers_public_search_instead_of_key_setup(self):
        self.settings = Settings(external_access_enabled=True)
        with patch('olcr_api.assistant_actions.search_youtube', side_effect=ActionError('YOUTUBE_SEARCH_FAILED')):
            result = self.service.handle(action_intent('YouTubeでlofiを流して'), 'chat', 'public-search')
        block = result['blocks'][0]
        self.assertEqual('youtube', block['service'])
        self.assertTrue(block['search_url'].startswith('https://www.youtube.com/results?'))
        self.assertNotIn('API', result['response'])

    def test_video_search_runs_on_behalf_of_the_user_and_does_not_autoplay(self):
        self.settings = Settings(external_access_enabled=True)
        videos = [{'id': 'dQw4w9WgXcQ', 'title': 'Python tutorial', 'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'}]
        for index, text in enumerate(('YouTubeでPythonの使い方動画を検索して', 'Pythonの解説動画を探して', 'Find videos on YouTube about Python')):
            with self.subTest(text=text), patch('olcr_api.assistant_actions.search_youtube', return_value=videos) as search:
                intent = action_intent(text)
                self.assertEqual('youtube', intent['service'])
                result = self.service.handle(intent, 'chat', f'video-search-{index}')
            search.assert_called_once()
            self.assertIn('Python', search.call_args.args[0])
            block = result['blocks'][0]
            self.assertEqual('media', block['kind'])
            self.assertEqual(videos, block['candidates'])
            self.assertFalse(block['autoplay'])

    def test_explicit_search_and_play_still_requests_playback(self):
        self.settings = Settings(external_access_enabled=True)
        with patch('olcr_api.assistant_actions.search_youtube', return_value=[{'id': 'dQw4w9WgXcQ'}]):
            result = self.service.handle(action_intent('YouTubeでlofiを探して再生して'), 'chat', 'search-play')
        self.assertTrue(result['blocks'][0]['autoplay'])

    def test_permission_denial_is_actionable_and_does_not_mark_connected(self):
        def denied(value):
            if value['operation'] == 'connect': return {'error': 'CALENDAR_PERMISSION_DENIED'}
            return {'available': True, 'authorized': False, 'permission': 'denied'}
        self.calendar.runner = denied
        result = self.client.post('/api/assistant/calendar/connect')
        self.assertEqual(422, result.status_code)
        self.assertIn('CALENDAR_ACCESS_DENIED', result.text)
        self.assertFalse(self.calendar.status()['connected'])

    def test_google_preview_cannot_be_applied_to_native_calendar(self):
        self.calendar.start()
        item = self.service.prepare_calendar('chat', payload={'summary': '', 'start': '', 'end': ''})
        with self.db.connect() as con:
            con.execute("UPDATE assistant_calendar_actions SET provider='google' WHERE id=?", (item['id'],))
        with self.assertRaisesRegex(ActionError, 'CALENDAR_PROVIDER_CHANGED'):
            self.service.confirm_calendar(item['id'], 'chat', item['revision'])
        self.assertFalse(any(call['operation'] == 'create' for call in self.calls))


class YouTubeWithoutKeysTests(unittest.TestCase):
    def test_song_search_uses_public_results_without_keys_or_other_search_providers(self):
        data = {'contents': [{'videoRenderer': {'videoId': 'dQw4w9WgXcQ',
            'title': {'runs': [{'text': 'Music fixture'}]}, 'ownerText': {'runs': [{'text': 'Artist'}]}}}]}
        calls = []
        def serve(request):
            calls.append(request)
            return httpx.Response(200, text='<script>var ytInitialData = ' + json.dumps(data) + ';</script>')
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            found = search_youtube('music fixture', client=client)
            self.assertEqual(found, search_youtube('music fixture', key='obsolete-key', client=client))
        self.assertEqual('Music fixture', found[0]['title'])
        self.assertEqual('Artist', found[0]['channel'])
        self.assertEqual('www.youtube.com', calls[0].url.host)
        self.assertNotIn('key', calls[0].url.params)
        self.assertEqual('music fixture', calls[0].url.params['search_query'])
        self.assertTrue(all('key' not in call.url.params for call in calls))

    def test_unreadable_public_results_do_not_request_developer_credentials(self):
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text='Consent required'))) as client:
            with self.assertRaisesRegex(ActionError, 'YOUTUBE_SEARCH_FAILED'):
                search_youtube('song', client=client)
