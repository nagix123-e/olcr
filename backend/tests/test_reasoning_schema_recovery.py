import tempfile
import unittest
from pathlib import Path
from olcr_api.db import Database, SCHEMA_VERSION
from olcr_api.models import Task
from olcr_api.runtime import _model_call_metadata
from olcr_api.thinking import received_reasoning
from olcr_api.assistant_actions import action_intent

class ReasoningSchemaRecoveryTests(unittest.TestCase):
    def test_upgrade_existing_profile_preserves_messages_and_saves_reasoning(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(Path(tmp)/'legacy.db');db.initialize()
            db.create_conversation('existing',1,'one')
            db.add_message('one','user','existing message',1,'message')
            with db.connect() as con:
                con.execute('DROP TABLE task_reasoning')
                con.execute('UPDATE schema_version SET version=37')
            db.initialize();db.initialize()
            task=Task('hello')
            task.model_calls.append(_model_call_metadata({'text':'answer','reasoning':received_reasoning('received reasoning')},'fixture'))
            db.save_task(task,'one')
            with db.connect() as con:
                self.assertEqual(SCHEMA_VERSION,con.execute('SELECT version FROM schema_version').fetchone()[0])
                self.assertEqual(1,con.execute('SELECT COUNT(*) FROM task_reasoning').fetchone()[0])
            self.assertEqual('existing message',db.conversation('one')['messages'][0]['content'])
    def test_show_latest_video_routes_to_youtube(self):
        for text in ['東海オンエアの最新の動画を見せて','東海オンエアの動画を見たい','Show the latest videos from this channel']:
            self.assertEqual('youtube',action_intent(text)['service'])
        self.assertIsNone(action_intent('動画を見せてという機能を実装して'))
    def test_unexpected_chat_failure_is_http_error_with_cors(self):
        from fastapi.testclient import TestClient
        from unittest.mock import patch
        import olcr_api.app as api
        with patch.object(api,'_gui_session_token','fixture-session'),patch.object(api,'_chat_request_intent',side_effect=RuntimeError('fixture-secret-must-not-leak')):
            client=TestClient(api.app,raise_server_exceptions=False)
            for path in ('/api/chat','/api/chat/stream'):
                response=client.post(path,headers={'X-OLCR-Session':'fixture-session','Origin':'tauri://localhost'},json={'message':'hello'})
                self.assertEqual(500,response.status_code,response.text)
                self.assertEqual('tauri://localhost',response.headers.get('access-control-allow-origin'))
                self.assertIn('CHAT_REQUEST_FAILED',response.json()['detail'])
                self.assertNotIn('fixture-secret',response.text)
