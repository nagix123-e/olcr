from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from fastapi.testclient import TestClient
from olcr_api.optional_runtimes import OptionalRuntimes,SetupError

class OptionalComponentApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.environment=patch.dict('os.environ',{'OLCR_DB_PATH':str(Path(cls.tmp.name)/'startup.db'),'OLCR_APP_SUPPORT':cls.tmp.name})
        cls.environment.start()
        import olcr_api.app as api
        cls.api=api;api.stop_coding_scheduler()
    @classmethod
    def tearDownClass(cls):
        cls.environment.stop();cls.tmp.cleanup()
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.profile=tempfile.TemporaryDirectory();self.addCleanup(self.profile.cleanup)
        self.components=OptionalRuntimes(Path(self.profile.name)/'components')
        self.provider=SimpleNamespace(loaded=False)
        self.provider.load=Mock(side_effect=lambda:setattr(self.provider,'loaded',True))
        self.stack.enter_context(patch.object(self.api,'optional_components',self.components))
        self.stack.enter_context(patch.object(self.api,'laya_provider',self.provider))
        self.stack.enter_context(patch.object(self.api,'_laya_setup_worker',None))
        self.stack.enter_context(patch.object(self.api,'laya_preflight',return_value={'ready':True}))
        self.stack.enter_context(patch.dict('os.environ',{'OLCR_LAYA_CACHE':self.profile.name}))
        self.client=TestClient(self.api.app)
    def test_inventory_is_read_only(self):
        with patch.object(self.components,'ensure',side_effect=AssertionError('inventory cannot download')):
            response=self.client.get('/api/components')
        self.assertEqual(200,response.status_code)
        self.assertEqual(['laya','agents','node-tools','browser','serena'],
                         [item['name'] for item in response.json()['components']])
        self.assertFalse(self.components.root.exists())
    def test_preparation_targets_only_selected_component(self):
        with patch.object(self.components,'prepare',return_value={'name':'agents','state':'installing'}) as prepare:
            response=self.client.post('/api/components/agents/prepare')
        self.assertEqual(200,response.status_code);prepare.assert_called_once_with('agents')
        self.provider.load.assert_not_called()
        self.assertEqual(422,self.client.post('/api/components/unknown/prepare').status_code)
    def test_laya_setup_loads_provider_and_failed_setup_can_retry(self):
        with patch.object(self.components,'ensure',side_effect=SetupError('unavailable')):
            self.client.post('/api/components/laya/prepare')
            self.api._laya_setup_worker.join(timeout=2)
        self.assertEqual('error',self.components.status('laya')['state'])
        self.provider.load.assert_not_called()
        with patch.object(self.components,'ensure',return_value=Path(self.profile.name)) as ensure:
            self.client.post('/api/components/laya/prepare')
            self.api._laya_setup_worker.join(timeout=2)
        ensure.assert_called_once_with('laya');self.provider.load.assert_called_once()
        self.assertTrue(self.provider.loaded)
        self.assertEqual('ready',self.client.get('/api/components').json()['components'][0]['state'])

if __name__=='__main__':unittest.main()
