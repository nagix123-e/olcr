"""Provider accounts are the inner retry loop; configured models are the outer loop."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

from olcr_api.brain_models import BrainModels, INITIAL, ROLE_SLOTS, brain_scope
from olcr_api.brain_providers import BrainProviders, account_key, credential_handle
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.model_provider import RoleAwareModelProvider
from olcr_api.ollama import ModelFailure


def reply(text='answer'):
    return {'choices':[{'message':{'content':text}}]}


class ProviderAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.tmp.name)/'accounts.db');self.db.initialize()
        self.settings=Settings(external_access_enabled=True)
        self.models=BrainModels(self.db,self.settings)
        self.providers=self.models.providers
        self.providers.credentials.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, provider='openai', slots=(1,2,3), model='current'):
        for slot in slots:
            self.providers.register(provider,'api_key',{'base_url':'https://fixture.example/v1',
                'model_ids':model,'api_key':f'fixture-{provider}-{slot}','account_name':f'Work {slot}'},slot)

    def test_legacy_handle_and_metadata_survive_reordering_and_restore(self):
        self.register()
        self.assertEqual('OLCR_BRAIN_OPENAI',credential_handle('openai',1))
        self.providers.save_account_order('openai',[3,1,2])
        restored=BrainProviders(self.db,self.settings)
        restored.restore_credentials({credential_handle('openai',slot):f'fixture-openai-{slot}' for slot in (1,2,3)})
        self.assertEqual([3,1,2],restored.account_order('openai'))
        for slot in (1,2,3):
            self.assertEqual(f'Work {slot}',restored.metadata('openai',slot)['account_name'])
            self.assertEqual(f'fixture-openai-{slot}',restored.token('openai',restored.metadata('openai',slot)))
        with self.db.connect() as con:
            metadata=str([row[0] for row in con.execute('SELECT metadata_json FROM brain_providers')])
        self.assertNotIn('fixture-openai-',metadata)

    def test_only_three_stable_slots_and_an_exact_order_are_accepted(self):
        for slot in (0,4,True,'2'):
            with self.subTest(slot=slot),self.assertRaises(ValueError):account_key('openai',slot)
        for order in ([1,1,2],[1,2,4],[1,2],[True,2,3]):
            with self.subTest(order=order),self.assertRaises(ValueError):self.providers.save_account_order('openai',order)
        with self.assertRaises(ValueError):self.providers.save_account_order('unknown',[1,2,3])

    def test_model_catalog_unions_accounts_without_duplicate_model_choices_or_secrets(self):
        self.register(slots=(1,),model='shared, first-only')
        self.register(slots=(2,),model='shared, second-only')
        models,providers=self.providers.catalog()
        self.assertEqual({'api:openai:shared','api:openai:first-only','api:openai:second-only'},
                         {row['id'] for row in models})
        self.assertEqual(3,len(models))
        provider=next(row for row in providers if row['id']=='openai')
        self.assertEqual([1,2,3],[row['account_id'] for row in provider['accounts']])
        self.assertNotIn('fixture-openai-',json.dumps(provider))

    def test_quota_and_empty_output_retry_same_model_with_each_account(self):
        self.register()
        messages=[{'role':'user','content':'keep this request'}]
        with patch('olcr_api.brain_providers.call_json',side_effect=[
            ModelFailure('provider_rejected','HTTP 429'),reply(''),reply('recovered')]) as call:
            result=self.providers.generate('openai','current',messages,None,True)
        self.assertEqual('recovered',result['text'])
        self.assertEqual(['Bearer fixture-openai-1','Bearer fixture-openai-2','Bearer fixture-openai-3'],
                         [invocation.args[2]['Authorization'] for invocation in call.call_args_list])
        self.assertTrue(all(invocation.args[1]['model']=='current' for invocation in call.call_args_list))
        self.assertTrue(all(invocation.args[1]['messages']==messages for invocation in call.call_args_list))
        self.assertEqual([1,2,3],[attempt['account_id'] for attempt in result['provider_account_attempts']])
        self.assertEqual(3,result['provider_account_id'])
        self.assertEqual('api:openai:current',result['fallback_from'])

    def test_exhausted_accounts_preserve_a_shared_failure_reason_for_model_fallback(self):
        self.register()
        failure=ModelFailure('rate_limited','HTTP 429')
        failure.http_status=429
        with patch('olcr_api.brain_providers.call_json',side_effect=[failure,failure,failure]):
            with self.assertRaises(ModelFailure) as raised:
                self.providers.generate('openai','current',[],None,True)
        self.assertEqual('provider_accounts_exhausted',raised.exception.category)
        self.assertEqual('rate_limited',getattr(raised.exception,'fallback_reason',None))

    def test_malformed_response_retries_without_exposing_provider_payload(self):
        self.register()
        with patch('olcr_api.brain_providers.call_json',side_effect=[{'choices':[None]},reply('recovered')]):
            result=self.providers.generate('openai','current',[],None,True)
        self.assertEqual(2,result['provider_account_id'])
        self.assertEqual('invalid_response',result['provider_account_attempts'][0]['category'])
        self.assertEqual(0,self.providers.restore_credentials({'OLCR_BRAIN_OPENAI_OAUTH_2':json.dumps({
            'access_token':7,'client_id':'fixture','id_token':'fixture','scope':'chatgpt.tokens.use.direct'})}))
        self.assertFalse(self.providers.configured('openai_oauth',2))

    def test_configured_priority_is_used_and_first_success_stops_retries(self):
        self.register();self.providers.save_account_order('openai',[3,1,2])
        with patch('olcr_api.brain_providers.call_json',side_effect=[ModelFailure('quota','quota'),reply()]) as call:
            result=self.providers.generate('openai','current',[],None,True)
        self.assertEqual(['Bearer fixture-openai-3','Bearer fixture-openai-1'],[c.args[2]['Authorization'] for c in call.call_args_list])
        self.assertEqual(1,result['provider_account_id'])
        with patch('olcr_api.brain_providers.call_json',return_value=reply()) as call:
            result=self.providers.generate('openai','current',[],None,True)
        self.assertEqual(1,call.call_count)
        self.assertNotIn('fallback_from',result)

    def test_model_discovery_caches_are_isolated_between_accounts(self):
        for slot in (1,2):
            self.providers.register('openai','api_key',{'base_url':'https://api.openai.com/v1',
                'model_ids':'seed','api_key':f'fixture-{slot}'},slot)
        with patch('olcr_api.brain_providers.get_json',side_effect=[{'data':[{'id':'first-model'}]}, {'data':[{'id':'second-model'}]}]) as fetch:
            models,_=self.providers.catalog()
        self.assertEqual({'api:openai:first-model','api:openai:second-model'},{row['id'] for row in models})
        self.assertEqual(['Bearer fixture-1','Bearer fixture-2'],[call.args[1]['Authorization'] for call in fetch.call_args_list])
        self.assertEqual(['first-model'],self.providers.metadata('openai',1)['model_ids'])
        self.assertEqual(['second-model'],self.providers.metadata('openai',2)['model_ids'])
        with patch('olcr_api.brain_providers.get_json') as fetch:self.providers.catalog()
        fetch.assert_not_called()

    def test_the_next_model_restarts_the_same_providers_account_priority(self):
        self.register(model='current,next')
        config=copy.deepcopy(INITIAL)
        config['selections']={slot:'api:openai:current' for slot in config['selections']}
        config['fallback_models']=['api:openai:next']
        provider=RoleAwareModelProvider(self.settings);provider.brain_models=self.models
        try:
            with brain_scope(config),patch('olcr_api.brain_providers.call_json',side_effect=[
                ModelFailure('quota','quota'),ModelFailure('quota','quota'),ModelFailure('quota','quota'),reply()]) as call:
                result=provider.generate([], 'role:planner_replanner')
            self.assertEqual(['current','current','current','next'],[row.args[1]['model'] for row in call.call_args_list])
            self.assertEqual(['Bearer fixture-openai-1','Bearer fixture-openai-2','Bearer fixture-openai-3','Bearer fixture-openai-1'],
                [row.args[2]['Authorization'] for row in call.call_args_list])
            self.assertEqual('api:openai:next',result['fallback_model_id'])
        finally:provider.close()

    def test_accounts_without_the_selected_model_are_not_used(self):
        self.register(slots=(1,),model='other')
        self.register(slots=(2,3))
        with patch('olcr_api.brain_providers.call_json',return_value=reply()) as call:
            result=self.providers.generate('openai','current',[],None,True)
        self.assertEqual('Bearer fixture-openai-2',call.call_args.args[2]['Authorization'])
        self.assertEqual(2,result['provider_account_id'])
        self.assertEqual(1,call.call_count)

    def test_external_access_permission_cannot_be_bypassed_by_account_retry(self):
        self.register()
        with patch('olcr_api.brain_providers.call_json') as call,self.assertRaises(ModelFailure) as error:
            self.providers.generate('openai','current',[],None,False)
        self.assertEqual('authorization',error.exception.category)
        call.assert_not_called()

    def test_role_resolution_accepts_a_model_available_only_in_account_two(self):
        self.register(slots=(2,))
        config=copy.deepcopy(INITIAL)
        config['selections']={slot:'api:openai:current' for slot in config['selections']}
        provider=RoleAwareModelProvider(self.settings);provider.brain_models=self.models
        try:
            with brain_scope(config),patch('olcr_api.brain_providers.call_json',return_value=reply()) as call:
                result=provider.generate([{'role':'user','content':'request'}],'role:planner_replanner')
            self.assertEqual(2,result['provider_account_id'])
            self.assertEqual('Bearer fixture-openai-2',call.call_args.args[2]['Authorization'])
        finally:provider.close()

    def test_multimodal_requests_retry_accounts_without_stringifying_the_image(self):
        from olcr_api.purpose_models import PurposeModels
        self.register()
        manager=PurposeModels(self.db,self.settings,self.models)
        payload={'model':'current','messages':[{'role':'user','content':[
            {'type':'text','text':'describe'}, {'type':'image_url','image_url':{'url':'data:image/png;base64,UE5H'}}]}]}
        with patch('olcr_api.brain_providers.call_json',side_effect=[ModelFailure('quota','quota'),reply('image description')]) as call:
            result=manager._brain_vision('vision',{'provider':'openai','model':'current'},
                                         b'PNG','image/png','describe',payload)
        self.assertEqual(2,result['provider_account_id'])
        self.assertEqual('image description',result['text'])
        self.assertTrue(all(invocation.args[1]==payload for invocation in call.call_args_list))

    def test_all_roles_exhaust_accounts_before_the_next_model_in_user_order(self):
        self.register();self.register('google',slots=(1,2),model='next')
        self.providers.save_account_order('google',[2,1,3])
        config=copy.deepcopy(INITIAL)
        config['selections']={slot:'api:openai:current' for slot in config['selections']}
        config['fallback_models']=['api:google:next']
        provider=RoleAwareModelProvider(self.settings);provider.brain_models=self.models
        try:
            for role in ROLE_SLOTS:
                calls=[]
                def transport(url,payload,headers,**kwargs):
                    token=headers.get('Authorization') or headers.get('x-goog-api-key')
                    calls.append(token)
                    if token!='fixture-google-1':raise ModelFailure('quota','quota')
                    return {'candidates':[{'content':{'parts':[{'text':'answer'}]}}]}
                with self.subTest(role=role),brain_scope(config),patch('olcr_api.brain_providers.call_json',side_effect=transport):
                    result=provider.generate([{'role':'user','content':'request'}],'role:'+role)
                self.assertEqual(['Bearer fixture-openai-1','Bearer fixture-openai-2','Bearer fixture-openai-3',
                                  'fixture-google-2','fixture-google-1'],calls)
                self.assertEqual('api:google:next',result['fallback_model_id'])
                self.assertEqual('api:openai:current',result['fallback_from'])
        finally:provider.close()

    def test_changed_endpoint_never_receives_a_new_credential_with_old_metadata(self):
        self.register(slots=(1,))
        snapshot=self.providers.metadata('openai')
        self.providers.register('openai','api_key',{'base_url':'https://new.fixture.example/v1',
            'model_ids':'current','api_key':'fixture-new'})
        with self.assertRaises(ModelFailure) as error:self.providers.token('openai',snapshot)
        self.assertEqual('configuration',error.exception.category)

    def test_oauth_quota_retries_with_the_second_account_token(self):
        for slot in (1,2,3):
            self.providers.credentials[account_key('openai_oauth',slot)]={
                'access_token':f'fixture-oauth-{slot}','id_token':'fixture-id','client_id':f'client-{slot}',
                'scope':'chatgpt.tokens.use.direct','expires_at':time.time()+3600}
            metadata=self.providers.metadata('openai_oauth',slot)
            metadata['model_ids']=['current']
            with self.db.connect() as con:
                con.execute('INSERT OR REPLACE INTO brain_providers VALUES(?,?)',
                    (account_key('openai_oauth',slot),json.dumps(metadata)))
        with patch.object(self.providers,'_responses_stream',side_effect=[ModelFailure('quota','quota'),('oauth answer',{})]) as call:
            result=self.providers.generate('openai_oauth','current',[{'role':'user','content':'request'}],None,True)
        self.assertEqual(['fixture-oauth-1','fixture-oauth-2'],[row.args[2] for row in call.call_args_list])
        self.assertEqual(2,result['provider_account_id'])
        self.assertEqual('oauth answer',result['text'])
        self.assertEqual('api:openai_oauth:current',result['fallback_model_id'])

    def test_oauth_rotations_and_acknowledgements_are_isolated_by_slot(self):
        for slot in (1,2,3):
            self.providers.credentials[account_key('openai_oauth',slot)]={
                'access_token':f'fixture-old-{slot}','client_id':f'client-{slot}','id_token':'fixture-id',
                'scope':'chatgpt.tokens.use.direct','refresh_token':f'fixture-refresh-{slot}','expires_at':0}
        with patch('olcr_api.brain_providers.call_json',return_value={'access_token':'fixture-new-2','expires_in':3600}):
            self.assertEqual('fixture-new-2',self.providers.token('openai_oauth',self.providers.metadata('openai_oauth',2)))
        pending=self.providers.pending_credential()
        self.assertEqual('OLCR_BRAIN_OPENAI_OAUTH_2',pending['handle'])
        self.assertEqual(2,pending['account_id'])
        self.assertEqual('fixture-old-1',self.providers.credentials['openai_oauth']['access_token'])
        self.providers.restore_credentials({'OLCR_BRAIN_OPENAI_OAUTH_2':json.dumps({
            'access_token':'stale','client_id':'client-2','id_token':'fixture-id','scope':'chatgpt.tokens.use.direct'})})
        self.assertEqual('fixture-new-2',self.providers.credentials[account_key('openai_oauth',2)]['access_token'])
        self.assertFalse(self.providers.acknowledge_credential('0'*64))
        self.assertTrue(self.providers.acknowledge_credential(pending['version']))
        self.assertEqual({'credential':None},self.providers.pending_credential())

    def test_oauth_second_account_signin_keeps_first_account_and_persists_correct_handle(self):
        from olcr_api.brain_providers import CHATGPT_RESOURCE
        self.providers.availability_store.save('openai_oauth',2,CHATGPT_RESOURCE,'*','unavailable','reauth_required',time.time()+900)
        self.providers.credentials['openai_oauth']={'subject':'first-account','client_id':'first-client'}
        started=self.providers.start_oauth(2)
        query=parse_qs(urlsplit(started['authorization_url']).query)
        self.assertEqual(['dynamic_agent_client'],query['client_id'])
        callback=query['redirect_uri'][0]+'?'+urlencode({'state':query['state'][0],'code':'fixture-code',
            'client_id':'fixture-client-2','scope':query['scope'][0]})
        with patch('olcr_api.brain_providers.call_json',return_value={'access_token':'fixture-access-2',
             'id_token':'fixture-id-2','scope':query['scope'][0],'expires_in':3600}), \
             patch('olcr_api.brain_providers.decode_chatgpt_id_token',return_value={'sub':'second-account'}):
            with urlopen(callback) as response:self.assertIn(b'complete',response.read())
        self.assertEqual({'status':'complete'},self.providers.poll_oauth(started['attempt_id']))
        self.assertEqual('first-account',self.providers.credentials['openai_oauth']['subject'])
        self.assertTrue(self.providers.configured('openai_oauth',2))
        self.assertEqual('unknown',self.providers.availability_store.get('openai_oauth',2,CHATGPT_RESOURCE,'model')['state'])
        self.assertEqual('OLCR_BRAIN_OPENAI_OAUTH_2',self.providers.pending_credential()['handle'])

    def test_all_three_oauth_slots_are_saved_without_acknowledging_a_different_slot(self):
        for slot in (1,2,3):
            self.providers.pending_credentials[account_key('openai_oauth',slot)]={'access_token':f'fixture-{slot}'}
        pending=self.providers.pending_credential()
        self.providers.save_account_order('openai_oauth',[2,3,1])
        self.assertTrue(self.providers.acknowledge_credential(pending['version']))
        self.assertEqual(2,self.providers.pending_credential()['account_id'])
        self.assertEqual({account_key('openai_oauth',2),account_key('openai_oauth',3)},set(self.providers.pending_credentials))
