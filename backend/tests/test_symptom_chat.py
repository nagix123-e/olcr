import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from olcr_api.db import Database
from olcr_api.symptom_chat import SymptomChat, chat_events, respond_in_chat, symptom_request

_APP_PROFILES = []  # Keep an imported app's bootstrap DB alive for other test modules.


class SymptomChatTests(unittest.TestCase):
    def setUp(self):
        self.operations = SimpleNamespace(
            status=Mock(return_value={'available': True}),
            search=Mock(return_value={'items': [{'id': 's_1', 'label': '頭痛'}]}),
            assess=Mock(return_value={'status': 'questioning', 'urgency': 'consultation',
                'question': {'type': 'single', 'text': '吐き気はありますか？',
                             'items': [{'id': 's_2', 'name': '吐き気'}]},
                'possible_causes': [], 'advice': '追加で確認します。'}))
        self.chat = SymptomChat(self.operations)

    def begin(self):
        turn = self.chat.turn('今、頭痛があるのだけど何が疑われる？')
        self.assertIn('急', turn['response'])
        turn = self.chat.turn('ない', turn['state'])
        self.assertIn('年齢', turn['response'])
        turn = self.chat.turn('30歳、女性、日本、該当なし', turn['state'])
        self.assertIn('Infermedica', turn['response'])
        self.operations.search.assert_not_called()
        turn = self.chat.turn('同意します', turn['state'])
        self.assertIn('1. 頭痛', turn['response'])
        return turn

    def test_personal_symptom_routing_excludes_programming_and_general_knowledge(self):
        for text in ['今、頭痛があるのだけど何が疑われる？', '頭が痛い。何の病気？', '頭痛がする']:
            self.assertTrue(symptom_request(text), text)
        for text in ['頭痛の原因を教えて', '頭痛診断APIを実装したい', '小説の登場人物に頭痛があります']:
            self.assertFalse(symptom_request(text), text)

    def test_complete_interview_is_normal_text_and_preserves_unknown(self):
        turn = self.begin()
        turn = self.chat.turn('1', turn['state'])
        self.assertIn('吐き気はありますか？', turn['response'])
        self.operations.assess.return_value = {'status': 'complete', 'urgency': 'consultation',
            'question': None, 'possible_causes': [{'id': 'c_1', 'rank': 1, 'name': '片頭痛'}],
            'department': '脳神経内科', 'advice': '参考順です', 'self_care': []}
        turn = self.chat.turn('分からない', turn['state'])
        self.assertIn('1. 片頭痛', turn['response'])
        self.assertIn('脳神経内科', turn['response'])
        self.assertIn('OpenSymptom', turn['response'])
        self.assertIsNone(turn['state'])
        payload = self.operations.assess.call_args.args[0]
        self.assertEqual(payload.evidence[-1].choice_id, 'unknown')
        self.assertIsNone(payload.evidence[-1].source)

    def test_emergency_short_circuits_without_remote_calls(self):
        turn = self.chat.turn('今頭痛がある')
        turn = self.chat.turn('ある', turn['state'])
        self.assertIn('救急', turn['response'])
        self.assertIsNone(turn['state'])
        self.operations.search.assert_not_called()
        self.operations.assess.assert_not_called()

    def test_unconfigured_is_answered_in_chat_without_model_fallback(self):
        self.operations.status.return_value = {'available': False}
        turn = self.chat.turn('頭痛がする')
        self.assertIn('未設定', turn['response'])
        self.assertIsNone(turn['state'])
        self.operations.assess.assert_not_called()

    def test_declining_consent_cancels_without_transmission(self):
        turn = self.chat.turn('頭痛がする')
        turn = self.chat.turn('ない', turn['state'])
        turn = self.chat.turn('30歳、女性、日本、該当なし', turn['state'])
        turn = self.chat.turn('同意しない', turn['state'])
        self.assertIsNone(turn['state'])
        self.operations.search.assert_not_called()

    def test_retry_is_not_initial_consent_and_new_emergency_stops_questions(self):
        turn = self.chat.turn('頭痛がする')
        turn = self.chat.turn('ない', turn['state'])
        turn = self.chat.turn('30歳、女性、日本、該当なし', turn['state'])
        turn = self.chat.turn('再試行', turn['state'])
        self.operations.search.assert_not_called()
        self.assertIn('同意', turn['response'])
        turn = self.begin()
        turn = self.chat.turn('1', turn['state'])
        turn = self.chat.turn('緊急症状がある', turn['state'])
        self.assertIn('119', turn['response'])
        self.assertIsNone(turn['state'])

    def test_unrelated_followup_does_not_hijack_chat(self):
        turn = self.chat.turn('頭痛がする')
        self.assertIsNone(self.chat.turn('Pythonのコードを書いて', turn['state']))
        self.assertIsNone(self.chat.turn('/help', turn['state']))
        self.assertFalse(symptom_request('/skill doctor 頭痛がある'))
        turn = self.chat.turn('外部送信をやめて', turn['state'])
        self.assertIsNone(turn['state'])
        self.operations.search.assert_not_called()

    def test_scope_and_expiry_do_not_invent_data(self):
        turn = self.chat.turn('頭痛がする')
        turn = self.chat.turn('ない', turn['state'])
        turn = self.chat.turn('70歳、男性、日本、該当なし', turn['state'])
        self.assertIn('対象外', turn['response'])
        self.operations.search.assert_not_called()
        turn = self.chat.turn('頭痛がする')
        turn['state']['expires_at'] = time.time() - 1
        turn = self.chat.turn('ない', turn['state'])
        self.assertIn('期限', turn['response'])
        self.assertIsNone(turn['state'])

    def test_turn_limit_and_mutually_exclusive_answers(self):
        turn = self.begin()
        self.operations.assess.return_value['question'] = {
            'type': 'group_single', 'text': 'どの程度ですか？',
            'items': [{'id': 's_2', 'name': '軽い'}, {'id': 's_3', 'name': '強い'}]}
        turn = self.chat.turn('1', turn['state'])
        turn = self.chat.turn('2', turn['state'])
        payload = self.operations.assess.call_args.args[0]
        self.assertEqual([e.id for e in payload.evidence], ['s_1', 's_3'])
        self.assertEqual(payload.question_count, 1)

    def test_persistence_idempotency_and_stream_have_no_ui_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(str(Path(directory) / 'test.db'))
            db.initialize()
            def resolve(value):
                if not value.conversation_id:
                    db.create_conversation('相談', time.time(), 'conversation-a', None)
                return 'conversation-a', None, db.conversation('conversation-a')
            value = SimpleNamespace(conversation_id=None, message='頭痛がする', message_id='user-1')
            first = respond_in_chat(db, value, resolve, self.chat)
            value.conversation_id = 'conversation-a'
            retry = respond_in_chat(db, value, resolve, self.chat)
            self.assertEqual(first['response'], retry['response'])
            self.assertEqual(len(db.conversation('conversation-a')['messages']), 2)
            self.assertEqual(first['blocks'], [])
            events = [json.loads(line[6:]) for line in chat_events(first)]
            self.assertEqual([event['type'] for event in events], ['meta', 'chunk', 'done'])
            self.assertEqual(events[1]['text'], first['response'])

    def test_real_chat_and_stream_routes_use_normal_messages(self):
        from fastapi.testclient import TestClient
        bootstrap = tempfile.TemporaryDirectory()
        _APP_PROFILES.append(bootstrap)
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'OLCR_DB_PATH': str(Path(bootstrap.name) / 'bootstrap.db'),
                    'OLCR_APP_SUPPORT': bootstrap.name, 'OLCR_ALLOWED_ROOTS': bootstrap.name, 'OLCR_VECTOR_ENABLED': 'false'}):
                import olcr_api.app as api
            db = Database(str(Path(directory) / 'chat.db'))
            db.initialize()
            settings = SimpleNamespace(permission_level='automatic')
            with patch.object(api, 'db', db), patch.object(api, '_symptom_chat', self.chat), \
                    patch.object(api, 'settings', settings), patch.object(api, '_gui_session_token', ''), \
                    patch.object(api, '_chat_request_intent', side_effect=AssertionError('ordinary model routing used')):
                with TestClient(api.app) as client:
                    result = client.post('/api/chat', json={'message': '頭痛がする', 'project_id': None}).json()
                    self.assertIn('緊急性', result['response'])
                    self.assertEqual(result['blocks'], [])
                    cid = result['conversation_id']
                    streamed = client.post('/api/chat/stream', json={
                        'message': 'ない', 'conversation_id': cid, 'project_id': None})
                    self.assertEqual(streamed.status_code, 200)
                    events = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith('data: ')]
                    self.assertEqual([event['type'] for event in events], ['meta', 'chunk', 'done'])
                    self.assertIn('年齢', events[1]['text'])
                    self.assertEqual(len(db.conversation(cid)['messages']), 4)
                    # Switching projects cannot replay a stored answer through deduplication.
                    saved = db.conversation(cid)['messages'][0]
                    replay = client.post('/api/chat', json={'message': saved['content'],
                        'message_id': saved['id'], 'conversation_id': cid, 'project_id': 'wrong-project'})
                    self.assertEqual(replay.status_code, 403)


if __name__ == '__main__':
    unittest.main()
