import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory()
os.environ['OLCR_DB_PATH'] = str(Path(_tmp.name) / 'host.db')
os.environ['OLCR_ALLOWED_ROOTS'] = _tmp.name
from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api.db import Database


class MessageEditAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / 'edit.db'))
        self.db.initialize()
        self.db.create_conversation('Chat', 1, 'c')
        self.db.add_message('c', 'user', 'Original', 2, 'u')
        self.db.add_message('c', 'assistant', 'Discard this answer', 3, 'a')
        self.patcher = patch.object(api, 'db', self.db)
        self.patcher.start()
        self.client = TestClient(api.app)

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_rewind_returns_persisted_prefix(self):
        response = self.client.post('/api/conversations/c/rewind', json={'message_id':'u', 'expected_last_message_id':'a', 'project_id':None})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([], response.json()['messages'])
        self.assertEqual([], self.client.get('/api/conversations/c').json()['messages'])

    def test_stale_history_is_conflict_and_preserves_history(self):
        response = self.client.post('/api/conversations/c/rewind', json={'message_id':'u', 'expected_last_message_id':'wrong'})
        self.assertEqual(409, response.status_code)
        self.assertEqual('CONVERSATION_CHANGED', response.json()['detail'])
        self.assertEqual(2, len(self.db.conversation('c')['messages']))

    def test_scope_mismatch_and_missing_conversation(self):
        payload = {'message_id':'u', 'expected_last_message_id':'a', 'project_id':'other'}
        self.assertEqual(403, self.client.post('/api/conversations/c/rewind', json=payload).status_code)
        self.assertEqual(404, self.client.post('/api/conversations/missing/rewind', json=payload).status_code)
        self.assertEqual(2, len(self.db.conversation('c')['messages']))
