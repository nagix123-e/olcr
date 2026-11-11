import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace
from olcr_api.db import Database
from olcr_api.github_connection import GitHubConnection
from olcr_api.git_api import GitIntegration

class GitHubConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir='/private/tmp');self.addCleanup(self.tmp.cleanup)
        self.db=Database(str(Path(self.tmp.name)/'test.sqlite'));self.db.initialize()
        self.connection=GitHubConnection(lambda:self.db)

    def test_browser_login_sets_up_git_and_never_returns_credentials(self):
        process=Mock(stdout=io.StringIO('! First copy your one-time code: ABCD-1234\nLogged in\n'))
        process.wait.return_value=0
        with patch('olcr_api.github_connection.github_cli',return_value='/fixture/gh'), \
             patch('olcr_api.github_connection.subprocess.Popen',return_value=process) as spawn, \
             patch.object(self.connection,'run',side_effect=lambda args:json.dumps({'login':'fixture','id':42,'token':'must-never-return'}) if args[0]=='api' else '') as run:
            attempt=self.connection.start()
            deadline=time.monotonic()+2
            while self.connection.poll(attempt['id'])['status']=='pending' and time.monotonic()<deadline:time.sleep(.01)
            result=self.connection.poll(attempt['id'])
            self.assertEqual('connected',result['status'])
            self.assertEqual('ABCD-1234',result['code'])
            self.assertEqual('https://github.com/login/device',result['url'])
            self.assertTrue(self.connection.automatic())
            account=self.connection.account()
            self.assertEqual('fixture',account['account'])
            self.assertEqual('42+fixture@users.noreply.github.com',self.connection.identity()['email'])
            self.assertNotIn('must-never-return',json.dumps([account,result]))
            self.assertIn(['auth','setup-git','--hostname','github.com'],[call.args[0] for call in run.call_args_list])
            self.assertIn('--web',spawn.call_args.args[0])
            self.assertNotIn('GH_TOKEN',spawn.call_args.kwargs['env'])

    def test_cancel_terminates_only_its_owned_login_process(self):
        process=Mock();process.poll.return_value=None
        value={'id':'owned','status':'pending','code':'ABCD-1234','url':'https://github.com/login/device','deadline':time.monotonic()+60,'process':process}
        self.connection.attempts={'owned':value}
        self.connection.expire('other',cancel=True);process.terminate.assert_not_called()
        self.connection.expire('owned',cancel=True);process.terminate.assert_called_once()
        self.assertEqual('cancelled',self.connection.poll('owned')['status'])

    def test_scoped_github_transport_uses_connected_account_and_keeps_preferences(self):
        original='git@github.com:fixture/repo.git'
        self.assertEqual(original,self.connection.transport(original))
        self.connection.preference('account','fixture')
        self.assertEqual('https://github.com/fixture/repo.git',self.connection.transport(original))
        self.connection.policy('review');self.assertFalse(self.connection.automatic())
        self.connection.policy('autonomous');self.assertTrue(GitHubConnection(lambda:self.db).automatic())

    def test_api_connection_endpoints_use_same_flow_without_project(self):
        integration=GitIntegration(lambda:self.db,lambda:SimpleNamespace(external_access_enabled=False))
        app=FastAPI();app.include_router(integration.router);client=TestClient(app)
        with patch.object(integration.github,'account',return_value={'installed':True,'connected':False}), \
             patch.object(integration.github,'start',return_value={'id':'attempt','status':'pending','code':'ABCD-1234','url':'https://github.com/login/device'}), \
             patch.object(integration.github,'poll',return_value={'id':'attempt','status':'connected'}):
            self.assertFalse(client.get('/api/github/connection').json()['connected'])
            self.assertEqual(200,client.post('/api/github/connect').status_code)
            self.assertEqual('connected',client.get('/api/github/connect/attempt').json()['status'])
            self.assertEqual(200,client.put('/api/github/policy',json={'execution_mode':'autonomous'}).status_code)
            self.assertTrue(integration.github.automatic())

if __name__=='__main__':unittest.main()
