import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from olcr_api.brain_models import BrainModels,INITIAL,ORNITH,ORNITH_MLX,brain_scope
from olcr_api.brain_providers import BrainProviders
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.model_availability import retry_time,attach_http_failure
from olcr_api.model_progress import observe_model_progress
from olcr_api.model_provider import RoleAwareModelProvider
from olcr_api.ollama import ModelFailure

class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'state.db');self.db.initialize()
        self.settings=Settings(external_access_enabled=True)
        self.models=BrainModels(self.db,self.settings);self.providers=self.models.providers
        self.register(1)
    def register(self,slot):
        self.providers.register('openai','api_key',{'base_url':'https://api.example/v1','api_key':'fixture-secret','model_ids':'m,other'},slot)
    def failure(self,status,headers=None):
        return attach_http_failure(ModelFailure('provider_rejected','Safe status'),HTTPError('https://api.example/v1',status,'status',headers or {},None))
    def attempt(self,model='m',operation=None):
        return self.providers.with_model_accounts('openai',model,operation or (lambda metadata:{'text':'ok'}))
    def test_unknown_then_success_and_external_access_state_have_no_probe_completion(self):
        with patch('olcr_api.brain_providers.call_json') as call:
            self.assertEqual('unknown',self.models.preflight('api:openai:m')['state']);call.assert_not_called()
            self.assertEqual('unavailable',self.models.preflight('api:openai:m',False)['state'])
        self.attempt();self.assertEqual('available',self.models.preflight('api:openai:m')['state'])
    def test_rate_limited_account_is_skipped_until_retry_time_then_becomes_unknown(self):
        with patch('olcr_api.model_availability.time.time',return_value=100):
            with self.assertRaises(ModelFailure):self.attempt(operation=lambda _:(_ for _ in ()).throw(self.failure(429,{'Retry-After':'60'})))
            self.assertEqual(160,self.models.preflight('api:openai:m')['retry_at'])
        operation=unittest.mock.Mock(return_value={'text':'recovered'})
        with patch('olcr_api.model_availability.time.time',return_value=159):
            with self.assertRaises(ModelFailure):self.attempt(operation=operation)
            operation.assert_not_called()
        with patch('olcr_api.model_availability.time.time',return_value=161):
            self.assertEqual('unknown',self.models.preflight('api:openai:m')['state']);self.attempt(operation=operation)
        operation.assert_called_once()
    def test_second_account_remains_usable_and_skips_known_unavailable_first(self):
        self.register(2)
        self.providers.availability_store.failure('openai',1,'https://api.example/v1','m',self.failure(403))
        calls=[];events=[]
        with observe_model_progress(events.append):self.attempt(operation=lambda meta:calls.append(meta['account_id']) or {'text':'ok'})
        self.assertEqual([2],calls);self.assertIn('account_skipped',[e['state'] for e in events])
        self.assertNotEqual('unavailable',self.models.preflight('api:openai:m')['state'])
    def test_access_denial_is_model_scoped_but_auth_failure_is_account_scoped(self):
        self.providers.availability_store.failure('openai',1,'https://api.example/v1','m',self.failure(403))
        self.assertEqual('unknown',self.models.preflight('api:openai:other')['state'])
        self.providers.availability_store.failure('openai',1,'https://api.example/v1','m',self.failure(401))
        self.assertEqual('unavailable',self.models.preflight('api:openai:other')['state'])
        self.register(1);self.assertEqual('unknown',self.models.preflight('api:openai:m')['state'])
    def test_bad_request_or_bad_image_does_not_poison_model_availability(self):
        for status in (400,422):
            with self.assertRaises(ModelFailure):self.attempt(operation=lambda _:(_ for _ in ()).throw(self.failure(status)))
            self.assertEqual('unknown',self.models.preflight('api:openai:m')['state'])
    def test_cooldown_survives_reload_and_does_not_persist_credentials(self):
        self.providers.availability_store.failure('openai',1,'https://api.example/v1','m',self.failure(429,{'Retry-After':'120'}))
        restored=BrainProviders(self.db,self.settings)
        from olcr_api.brain_providers import credential_handle
        restored.restore_credentials({credential_handle('openai'):'fixture-secret'})
        self.assertEqual('unavailable',restored.model_availability('openai','m')['state'])
        with self.db.connect() as con:
            stored=str([tuple(row) for row in con.execute('SELECT * FROM model_availability')])
        self.assertNotIn('fixture-secret',stored)
    def test_transient_network_failure_is_not_a_permanent_disable(self):
        with patch('olcr_api.model_availability.time.time',return_value=100):
            self.providers.availability_store.failure('openai',1,'https://api.example/v1','m',ModelFailure('provider_unavailable','safe'))
            self.assertEqual('unavailable',self.models.preflight('api:openai:m')['state'])
        with patch('olcr_api.model_availability.time.time',return_value=116):self.assertEqual('unknown',self.models.preflight('api:openai:m')['state'])
    def test_zero_remaining_on_a_successful_call_prevents_next_call_until_reset(self):
        self.attempt(operation=lambda _: {'text':'last available request','provider_rate_limits':{'x-ratelimit-remaining-requests':'0','x-ratelimit-reset-requests':'2m'}})
        self.assertEqual('rate_limited',self.models.preflight('api:openai:m')['reason'])
    def test_role_fallback_skips_unavailable_model_without_calling_its_generator(self):
        provider=RoleAwareModelProvider(self.settings);provider.brain_models=self.models;self.addCleanup(provider.close)
        self.providers.availability_store.failure('openai',1,'https://api.example/v1','m',self.failure(429))
        config=copy.deepcopy(INITIAL);config['selections']['general_inference']='api:openai:m';config['fallback_models']=['api:openai:other']
        events=[]
        with brain_scope(config),observe_model_progress(events.append),patch.object(self.providers,'generate',return_value={'text':'fallback answer'}) as call:
            result=provider.generate([{'role':'user','content':'Plan it'}],'role:planner_replanner')
        self.assertEqual('other',call.call_args.args[1]);call.assert_called_once();self.assertEqual('fallback answer',result['text'])
        self.assertTrue(any(e['state']=='model_skipped' and e['reason']=='rate_limited' for e in events))
        self.assertFalse(any(e['state']=='request_started' and e['model']=='m' for e in events))
    def test_missing_local_runtime_and_ollama_inventory_are_checked_without_generation(self):
        with patch.dict('os.environ',{'OLCR_MLX_PYTHON':str(Path(self.tmp.name)/'missing-python')}):
            self.assertEqual('local_runtime_missing',self.models.preflight(ORNITH_MLX)['reason'])
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return json.dumps({'models':[]}).encode()
        with patch('olcr_api.brain_models.request.urlopen',return_value=Response()) as call:
            self.assertEqual('local_model_missing',self.models.preflight(ORNITH)['reason'])
            self.assertEqual('local_model_missing',self.models.preflight('ollama:other')['reason']);call.assert_called_once()
            self.assertTrue(call.call_args.args[0].endswith('/api/tags'))
    def test_recoverable_oauth_expiry_is_unknown_but_expiry_without_refresh_is_unavailable(self):
        from olcr_api.brain_providers import account_key,CHATGPT_RESOURCE,CHATGPT_AUTHORIZATION_URL,CHATGPT_TOKEN_URL,CHATGPT_SCOPE
        with self.db.connect() as con:
            con.execute('INSERT INTO brain_providers VALUES(?,?)',(account_key('openai_oauth',1),json.dumps({'id':'openai_oauth','base_url':CHATGPT_RESOURCE,'authorization_url':CHATGPT_AUTHORIZATION_URL,'token_url':CHATGPT_TOKEN_URL,'model_ids':['gpt']})))
        self.providers.credentials[account_key('openai_oauth',1)]={'access_token':'fixture-token','client_id':'fixture-client','id_token':'fixture-id','scope':CHATGPT_SCOPE,'expires_at':1,'refresh_token':'fixture-refresh'}
        self.assertEqual('unknown',self.models.preflight('api:openai_oauth:gpt')['state'])
        self.providers.credentials[account_key('openai_oauth',1)].pop('refresh_token')
        self.assertEqual('reauth_required',self.models.preflight('api:openai_oauth:gpt')['reason'])
    def test_recent_local_transport_failure_skips_inventory_until_cooldown_expires(self):
        with patch('olcr_api.model_availability.time.time',return_value=100):
            self.models.note_local_result(ORNITH,ModelFailure('timeout','safe'))
            with patch('olcr_api.brain_models.request.urlopen') as call:
                self.assertEqual('unavailable',self.models.preflight(ORNITH)['state']);call.assert_not_called()

    def test_skipped_requests_do_not_increment_provider_usage_or_send_another_completion(self):
        with patch('olcr_api.brain_providers.call_json',side_effect=self.failure(429,{'Retry-After':'60'})) as call:
            for _ in range(2):
                with self.assertRaises(ModelFailure):self.providers.generate('openai','m',[{'role':'user','content':'test'}],None,True)
            call.assert_called_once()
        with self.db.connect() as con:self.assertEqual(1,con.execute('SELECT count(*) FROM brain_usage_events').fetchone()[0])

    def test_retry_header_formats(self):
        self.assertEqual(160,retry_time('60',100));self.assertEqual(3723,retry_time('1h2m3s',0))
        self.assertEqual(1793264400,retry_time('Thu, 29 Oct 2026 09:00:00 GMT',1793264300))
