import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from olcr_api.db import Database,SCHEMA_VERSION

class SidebarPinDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=Database(Path(self.temp.name)/'pins.db');self.db.initialize()
        self.db.create_project('Older',None,1,'older');self.db.create_project('Newer',None,2,'newer')
        for scope in ['older',None]:
            for index in [1,2]:self.db.create_conversation(str(index),index,f'{scope}-{index}',scope)
    def tearDown(self):self.temp.cleanup()
    def test_project_and_each_conversation_scope_pin_sort_unpin_and_restart(self):
        self.db.pin_project('older',True)
        for scope in ['older',None]:self.db.pin_conversation(f'{scope}-1',True,scope)
        restarted=Database(self.db.path);restarted.initialize()
        self.assertEqual(['older','newer'],[x['id'] for x in restarted.projects()])
        for scope in ['older',None]:
            values=restarted.conversations(scope) if scope else restarted.projectless_conversations()
            self.assertEqual([f'{scope}-1',f'{scope}-2'],[x['id'] for x in values])
            self.assertEqual(1,values[0]['updated_at'])
            restarted.pin_conversation(f'{scope}-1',False,scope)
            values=restarted.conversations(scope) if scope else restarted.projectless_conversations()
            self.assertEqual([f'{scope}-2',f'{scope}-1'],[x['id'] for x in values])
        restarted.pin_project('older',False)
        self.assertEqual(['newer','older'],[x['id'] for x in restarted.projects()])
        self.assertEqual(1,restarted.project('older')['updated_at'])
    def test_legacy_migration_and_multiple_pin_recency_and_normal_writes(self):
        # Reproduce the immediately preceding schema without pin columns.
        with self.db.connect() as con:
            con.execute('ALTER TABLE projects DROP COLUMN pinned');con.execute('ALTER TABLE conversations DROP COLUMN pinned')
            con.execute('UPDATE schema_version SET version=35')
        self.db.initialize()
        self.assertFalse(self.db.project('older')['pinned'])
        with self.db.connect() as con:self.assertEqual(SCHEMA_VERSION,con.execute('SELECT version FROM schema_version').fetchone()[0])
        self.db.pin_project('older',True);self.db.pin_project('newer',True)
        self.assertEqual(['newer','older'],[x['id'] for x in self.db.projects()])
        self.db.pin_conversation('older-1',True,'older')
        self.db.rename_conversation('older-1','Renamed',3);self.db.add_message('older-1','user','preserved',4,'message')
        self.assertTrue(self.db.conversation('older-1')['pinned'])
        self.assertEqual('preserved',self.db.conversation('older-1')['messages'][0]['content'])
        self.db.create_project('New',None,5,'new');self.db.create_conversation('New',5,'new-chat',None)
        self.assertFalse(self.db.project('new')['pinned']);self.assertFalse(self.db.conversation('new-chat')['pinned'])

class SidebarPinApiTests(unittest.TestCase):
    setUp=SidebarPinDatabaseTests.setUp
    tearDown=SidebarPinDatabaseTests.tearDown
    def test_api_pins_and_enforces_conversation_scope_and_boolean_schema(self):
        import olcr_api.app as api
        with patch.object(api,'db',self.db),patch.object(api,'_gui_session_token','pin-test'):
            client=TestClient(api.app);client.headers['X-OLCR-Session']='pin-test'
            for path in ['/api/projects/older/pin','/api/projects/older/conversations/older-1/pin','/api/conversations/None-1/pin']:
                self.assertEqual(200,client.put(path,json={'pinned':True}).status_code)
            self.assertTrue(client.get('/api/projects').json()['projects'][0]['pinned'])
            self.assertTrue(client.get('/api/conversations').json()['conversations'][0]['pinned'])
            self.assertTrue(client.get('/api/projects/older/conversations').json()['conversations'][0]['pinned'])
            self.assertEqual(404,client.put('/api/projects/newer/conversations/older-1/pin',json={'pinned':False}).status_code)
            self.assertEqual(404,client.put('/api/conversations/older-1/pin',json={'pinned':False}).status_code)
            self.assertEqual(404,client.put('/api/projects/older/conversations/None-1/pin',json={'pinned':False}).status_code)
            self.assertEqual(404,client.put('/api/projects/missing/pin',json={'pinned':True}).status_code)
            self.assertEqual(422,client.put('/api/projects/older/pin',json={'pinned':'false'}).status_code)
            self.assertEqual(401,client.put('/api/projects/older/pin',json={'pinned':False},headers={'X-OLCR-Session':'wrong'}).status_code)
