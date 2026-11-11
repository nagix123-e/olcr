import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from olcr_api.brain_models import BrainModels
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.ollama import ModelFailure


def credential(expiry=0,token='old',refresh='old-refresh'):
    return {'access_token':token,'refresh_token':refresh,'id_token':'id-fixture','client_id':'client-fixture',
            'scope':'chatgpt.tokens.use.direct','expires_at':expiry}


class OAuthRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Database(str(Path(self.tmp.name)/'test.db'));self.db.initialize()
        self.providers=BrainModels(self.db,Settings()).providers

    def tearDown(self):self.tmp.cleanup()

    def test_concurrent_catalog_and_inference_rotate_refresh_token_only_once(self):
        self.providers.credentials['openai_oauth']=credential()
        metadata=self.providers.metadata('openai_oauth')
        started=threading.Event();release=threading.Event()
        def refresh(*args,**kwargs):
            started.set();self.assertTrue(release.wait(2))
            return {'access_token':'new','refresh_token':'rotated','expires_in':3600}
        with patch('olcr_api.brain_providers.call_json',side_effect=refresh) as call,ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.providers.token,'openai_oauth',metadata)
            self.assertTrue(started.wait(2))
            second=pool.submit(self.providers.token,'openai_oauth',metadata)
            release.set()
            self.assertEqual('new',first.result(timeout=2));self.assertEqual('new',second.result(timeout=2))
            call.assert_called_once()
        self.assertEqual('rotated',json.loads(self.providers.pending_credential()['credential'])['refresh_token'])

    def test_live_keychain_restore_populates_existing_provider_instance(self):
        self.providers.credentials.pop('openai_oauth',None)
        self.assertEqual(1,self.providers.restore_credentials({'OLCR_BRAIN_OPENAI_OAUTH':json.dumps(credential(time.time()+3600))}))
        self.assertTrue(self.providers.configured('openai_oauth'))

    def test_stale_restore_cannot_overwrite_unpersisted_rotation_or_newer_token(self):
        current=credential(time.time()+3600,'new','new-refresh')
        self.providers.credentials['openai_oauth']=current
        self.providers.pending_credentials['openai_oauth']=current
        self.providers.restore_credentials({'OLCR_BRAIN_OPENAI_OAUTH':json.dumps(credential())})
        self.assertEqual('new-refresh',self.providers.credentials['openai_oauth']['refresh_token'])
        self.providers.acknowledge_credential(self.providers.pending_credential()['version'])
        self.providers.restore_credentials({'OLCR_BRAIN_OPENAI_OAUTH':json.dumps(credential())})
        self.assertEqual('new-refresh',self.providers.credentials['openai_oauth']['refresh_token'])

    def test_invalid_restore_does_not_erase_live_credential(self):
        self.providers.credentials['openai_oauth']=credential()
        self.assertEqual(0,self.providers.restore_credentials({'OLCR_BRAIN_OPENAI_OAUTH':'invalid-json'}))
        self.assertTrue(self.providers.configured('openai_oauth'))

    def test_revoked_refresh_is_reported_as_reconnect_not_empty_disconnected_provider(self):
        self.providers.credentials['openai_oauth']=credential()
        with patch('olcr_api.brain_providers.call_json',side_effect=ModelFailure('provider_rejected','HTTP 400')):
            _,providers=self.providers.catalog()
        row=next(p for p in providers if p['id']=='openai_oauth')
        self.assertTrue(row['configured']);self.assertEqual('reauth_required',row['connection_state'])
        self.assertIn('再接続',row['availability_message'])
        self.assertNotIn('old-refresh',json.dumps(row))

    def test_temporary_catalog_error_does_not_require_reauthentication(self):
        self.providers.credentials['openai_oauth']=credential(time.time()+3600)
        with patch('olcr_api.brain_providers.get_json',side_effect=ModelFailure('network','temporary')):
            _,providers=self.providers.catalog()
        row=next(p for p in providers if p['id']=='openai_oauth')
        self.assertEqual('connected',row['connection_state']);self.assertTrue(row['configured'])

    def test_reconnect_in_new_profile_keeps_the_host_identity_of_restored_registration(self):
        saved=credential();saved['host_id']='urn:uuid:15176362-936f-4dde-8c01-e1f25c57ffcc'
        self.providers.credentials['openai_oauth']=saved
        metadata=self.providers.metadata('openai_oauth')
        self.assertEqual(saved['host_id'],metadata['host_id'])
        self.providers.credentials['openai_oauth']['host_id']='invalid-host'
        self.assertIsNone(self.providers.metadata('openai_oauth')['host_id'])

    def test_live_restore_api_updates_brain_without_rebuilding_runtime(self):
        import olcr_api.app as api
        from unittest.mock import patch
        original=api.runtime
        with patch.object(api,'brain_models',type('Manager',(),{'providers':self.providers})()),patch.dict('os.environ',{},clear=False):
            response=api.restore_external_tool_credentials(api.KeychainCredentialRestoreInput(credentials={
                'OLCR_BRAIN_OPENAI_OAUTH':json.dumps(credential(time.time()+3600))}))
        self.assertEqual('RESTORED',response['status'])
        self.assertTrue(self.providers.configured('openai_oauth'))
        self.assertIs(original,api.runtime)
        self.assertNotIn('old-refresh',json.dumps(response))

    def test_old_acknowledgement_cannot_clear_a_newer_pending_token(self):
        first=credential(time.time()+3600,'first','first-refresh')
        self.providers.pending_credentials['openai_oauth']=first
        old=self.providers.pending_credential()['version']
        self.providers.pending_credentials['openai_oauth']=credential(time.time()+3600,'second','second-refresh')
        self.assertFalse(self.providers.acknowledge_credential(old))
        self.assertIn('second-refresh',self.providers.pending_credential()['credential'])
