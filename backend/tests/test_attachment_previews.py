import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from olcr_api.db import Database
from olcr_api.attachment_previews import image_preview, register_preview_routes

IMAGE='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='
class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'preview.db');self.db.initialize()
        self.db.create_conversation('Test',1,'chat',None)
        app=FastAPI();register_preview_routes(app,lambda:self.db);self.client=TestClient(app)
    def test_preview_survives_reload_and_expires_exactly_after_one_day(self):
        with patch('olcr_api.attachment_previews.time.time',return_value=100):
            block=image_preview(self.db,'chat',{'data_url':IMAGE})
        path='/api/message-image-previews/'+block['preview_id']
        with patch('olcr_api.attachment_previews.time.time',return_value=86499):
            result=self.client.get(path);self.assertEqual(200,result.status_code)
            self.assertEqual(IMAGE,result.json()['data_url']);self.assertEqual('no-store',result.headers['cache-control'])
        with patch('olcr_api.attachment_previews.time.time',return_value=86500):
            self.assertEqual(404,self.client.get(path).status_code)
        with self.db.connect() as con: self.assertEqual(0,con.execute('SELECT count(*) FROM message_image_previews').fetchone()[0])
    def test_invalid_or_active_content_is_never_previewed(self):
        for data in ['data:image/svg+xml;base64,PHN2Zz4=', 'data:image/png;base64,aGVsbG8=', 'data:image/png;base64,!!!']:
            self.assertEqual({},image_preview(self.db,'chat',{'data_url':data}))
