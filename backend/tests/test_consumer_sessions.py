import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from olcr_api.consumer_sessions import register_consumer_session_routes

class ConsumerSessionTests(unittest.TestCase):
 def setUp(self):
  self.service=Mock();self.browser=Mock();self.browser.list.return_value=[]
  self.browser.open.return_value={'status':'READY','session_id':'session'}
  self.browser.navigate.return_value={'status':'AVAILABLE','clean_markdown':'PRIVATE SNAPSHOT'}
  self.browser.profiles.return_value=[{'profile_id':'market-amazon','in_use':False}]
  self.app=FastAPI();register_consumer_session_routes(self.app,lambda:{'service_connectors':lambda:self.service,'browser_sessions':self.browser})
  self.client=TestClient(self.app)
 def test_opens_fixed_persistent_profile_without_claiming_login(self):
  r=self.client.post('/api/commerce-sessions/amazon/open');self.assertEqual(r.status_code,200,r.text)
  self.assertEqual(r.json()['account_status'],'sign_in_unverified');self.assertNotIn('PRIVATE',r.text)
  self.browser.open.assert_called_once_with(profile_id='market-amazon',interactive_login=True)
  self.browser.navigate.assert_called_once_with('session','https://www.amazon.co.jp/')
  self.assertEqual(self.client.get('/api/commerce-sessions').json()['sessions'][0]['profile_saved'],True)
 def test_revocation_and_unknown_provider_never_open_browser(self):
  self.service.authorize.side_effect=ValueError('OFF')
  self.assertEqual(self.client.post('/api/commerce-sessions/amazon/open').status_code,403)
  self.assertEqual(self.client.post('/api/commerce-sessions/evil/open').status_code,404)
  self.browser.open.assert_not_called()
 def test_forget_closes_profile_and_removes_local_session(self):
  self.browser.list.return_value=[{'profile_id':'market-amazon','session_id':'session'}]
  self.assertEqual(self.client.delete('/api/commerce-sessions/amazon/session/forget').status_code,200)
  self.browser.close.assert_called_once_with('session');self.browser.forget_profile.assert_called_once_with('market-amazon')
 def test_browser_manager_enforces_revocation_on_existing_profile(self):
  import tempfile
  from olcr_api.browser_network import BrowserSessionManager
  permitted=[True];sessions=[]
  class Session:
   def __init__(self,*args,**kwargs):self.closed=False;sessions.append(self)
   def start(self):return {'status':'READY','tools':[]}
   def close(self):self.closed=True
  with tempfile.TemporaryDirectory() as directory:
   manager=BrowserSessionManager(lambda:['fake'],session_factory=Session,profile_root=directory,session_policy=lambda profile:permitted[0])
   try:
    opened=manager.open('market-amazon',interactive_login=True)
    self.assertEqual(opened['status'],'READY')
    permitted[0]=False
    self.assertEqual(manager.snapshot(opened['session_id'])['status'],'NOT_FOUND')
    self.assertTrue(sessions[0].closed)
    self.assertTrue(manager.profiles()[0]['profile_id']=='market-amazon')
   finally:manager.close_all()
