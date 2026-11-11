import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api.brain_models import BrainModels, INITIAL, BONSAI, ORNITH, ORNITH_MLX
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.model_provider import RoleAwareModelProvider
from olcr_api.ollama import ModelFailure

class BrainApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        db=Database(Path(self.tmp.name)/'api.db');db.initialize()
        settings=Settings(task_manager_enabled=False)
        manager=BrainModels(db,settings)
        # Keep API tests independent of the developer machine's installed
        # Ollama/MLX inventory and local model-availability cache.
        self.local_models_fixture=patch.object(manager,'_local_models',return_value=[
            {'id': ORNITH, 'available': True, 'local': True},
            {'id': ORNITH_MLX, 'available': True, 'local': True},
            {'id': BONSAI, 'available': True, 'local': True},
        ])
        self.local_models_fixture.start()
        actual_preflight=manager.preflight
        self.local_preflight_fixture=patch.object(manager,'preflight',
            side_effect=lambda model,external_enabled=None: {'state':'available','reason':'fixture_installed','retry_at':None}
            if isinstance(model,str) and model.startswith(('ollama:','mlx:'))
            else actual_preflight(model,external_enabled))
        self.local_preflight_fixture.start()
        provider=RoleAwareModelProvider(settings);provider.brain_models=manager
        self.provider=provider;self.db=db
        self.patches=[patch.object(api,'db',db),patch.object(api,'brain_models',manager),patch.object(api,'settings',settings),patch.object(api.runtime,'model',provider),patch.object(api,'_gui_session_token','test-brain-session')]
        for p in self.patches:p.start()
        self.client=TestClient(api.app);self.client.headers['X-OLCR-Session']='test-brain-session'
        self.project=db.default_project_id()
        for cid in ('one','two'):db.create_conversation(cid,time.time(),cid,self.project)
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.local_preflight_fixture.stop();self.local_models_fixture.stop()
        self.provider.close();self.tmp.cleanup()
    def test_local_effort_slider_modes_reach_the_selected_worker(self):
        for scope in ('split', 'unified'):
            for effort, enabled in (('off', False), ('auto', False), ('on', True)):
                configuration=copy.deepcopy(INITIAL)
                configuration['mode']=scope
                slot='brain' if scope=='unified' else 'general_inference'
                configuration['efforts'][slot]=effort
                with patch.object(api.brain_models, 'catalog', return_value={'models':[], 'providers':[]}):
                    saved=self.client.put('/api/brain/configuration?conversation_id=one',json=configuration)
                self.assertEqual(200,saved.status_code,saved.text)
                with patch.object(self.provider,'_run_mlx',return_value={'text':'4'}) as call:
                    response=self.client.post('/api/chat/stream',json={'message':'Say hello briefly.','conversation_id':'one'})
                self.assertEqual(200,response.status_code,response.text)
                self.assertTrue(call.called,response.text)
                self.assertEqual(1,call.call_count)
                self.assertIs(enabled,call.call_args.args[1]['think'],(scope,effort))
    def test_api_configuration_validates_scope_and_rejects_unregistered_models(self):
        current=self.client.get('/api/brain/configuration').json()
        self.assertEqual(INITIAL,current)
        changed=copy.deepcopy(current);changed['selections']['coder']=BONSAI
        response=self.client.put('/api/brain/configuration?conversation_id=one',json=changed)
        self.assertEqual(200,response.status_code)
        self.assertEqual(INITIAL,self.client.get('/api/brain/configuration?conversation_id=two').json())
        changed['selections']['coder']='default'
        self.assertEqual(422,self.client.put('/api/brain/configuration',json=changed).status_code)
        self.assertEqual(404,self.client.get('/api/brain/configuration?conversation_id=missing').status_code)
        self.assertEqual(401,self.client.get('/api/brain/configuration',headers={'X-OLCR-Session':'bad'}).status_code)

    def test_local_model_switch_preserves_offline_fallbacks_without_a_422(self):
        remote = 'api:openai:temporarily-offline'
        catalog = {'models': [{'id': model, 'available': True} for model in
                              (ORNITH, ORNITH_MLX, BONSAI, remote)], 'providers': []}
        original = copy.deepcopy(INITIAL)
        original['fallback_models'] = [remote, BONSAI]
        with patch.object(api.brain_models, 'catalog', return_value=catalog):
            api.brain_models.save(original, 'one')
            catalog['models'][-1]['available'] = False
            for model in (ORNITH, ORNITH_MLX, BONSAI):
                with self.subTest(model=model):
                    value = copy.deepcopy(original)
                    value['selections']['coder'] = model
                    result = self.client.put('/api/brain/configuration?conversation_id=one', json=value)
                    self.assertEqual(200, result.status_code, result.text)
                    self.assertEqual(model, result.json()['selections']['coder'])
                    self.assertEqual([remote, BONSAI], result.json()['fallback_models'])
                    self.assertEqual(INITIAL, api.brain_models.configuration('two'))
    def test_stream_uses_conversation_selection_and_retains_neural_route(self):
        value=copy.deepcopy(INITIAL);value['selections']['general_inference']=BONSAI
        other=copy.deepcopy(INITIAL);other['selections']['general_inference']=ORNITH
        catalog={'models':[{'id':model,'available':True} for model in (BONSAI,ORNITH,ORNITH_MLX)],'providers':[]}
        with patch.object(api.brain_models,'catalog',return_value=catalog):
            self.assertEqual(200,self.client.put('/api/brain/configuration?conversation_id=one',json=value).status_code)
            self.assertEqual(200,self.client.put('/api/brain/configuration?conversation_id=two',json=other).status_code)
        def ollama(messages,model,stream=False,**kwargs):
            result={'text':'ollama response','model':model,'done':True}
            return iter([result]) if stream else result
        with patch.object(api.brain_models,'preflight',return_value={'state':'available'}), \
             patch.object(self.provider,'_run_mlx',side_effect=lambda operation,payload:{'text':f'mlx {operation} response'}) as mlx, \
             patch.object(self.provider.ollama,'generate',side_effect=ollama):
            # An explicit Ollama selection remains distinct from the MLX deployment.
            for cid,expected in [('one','mlx bonsai response'),('two','ollama response')]:
                response=self.client.post('/api/chat/stream',json={'message':'Say hello briefly.','conversation_id':cid,'project_id':self.project})
                self.assertEqual(200,response.status_code)
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                chunks=''.join(event.get('text','') for event in events)
                self.assertEqual(expected,chunks,response.text)
                done=next(e for e in events if e['type']=='done')
                self.assertEqual('NEURAL',done['task']['route'])
            self.assertEqual(1,mlx.call_count)

    def test_stream_records_selected_model_token_usage_and_usage_can_be_reset(self):
        with patch.object(self.provider,'generate',return_value=iter([{
            'text':'usage checked','model':'ternary_bonsai_2_27b','model_role':'normal_chat',
            'prompt_tokens':9,'completion_tokens':4}])):
            response=self.client.post('/api/chat/stream',json={'message':'Say hello briefly.','conversation_id':'one','project_id':self.project})
        self.assertEqual(200,response.status_code)
        usage=self.client.get('/api/brain/usage').json()['models'][BONSAI]
        self.assertEqual((9,4,13),(usage['input_tokens'],usage['output_tokens'],usage['total_tokens']))
        self.assertIsNone(usage['limit_tokens']);self.assertIsNone(usage['cost_usd'])
        reset=self.client.post('/api/brain/usage/reset',json={'model_id':BONSAI})
        self.assertEqual(200,reset.status_code)
        self.assertEqual(0,self.client.get('/api/brain/usage').json()['models'][BONSAI]['total_tokens'])

    def test_external_requests_with_and_without_tokens_are_not_counted_twice(self):
        manager=self.provider.brain_models;model_id='api:openai:usage-fixture'
        manager.providers.register('openai','api_key',{'base_url':'http://127.0.0.1:9/v1','api_key':'fixture-only','model_ids':'usage-fixture'})
        configuration=copy.deepcopy(INITIAL);configuration['mode']='unified';configuration['selections']['brain']=model_id
        models=[{'id':identifier,'available':True} for identifier in set(INITIAL['selections'].values())|{model_id}]
        with patch.object(manager,'catalog',return_value={'models':models,'providers':[]}):manager.save(configuration,'one')
        calls=reported=0
        for endpoint in ['/api/chat','/api/chat/stream']:
            for tokens in [None,{'prompt_tokens':8,'completion_tokens':4,'total_tokens':12}]:
                with self.subTest(endpoint=endpoint,tokens=bool(tokens)),patch('olcr_api.brain_providers.call_json',return_value={'choices':[{'message':{'content':'Hello.'}}],'usage':tokens}) as call:
                    response=self.client.post(endpoint,json={'message':'Say hello briefly.','conversation_id':'one','project_id':self.project})
                    self.assertEqual(200,response.status_code,response.text)
                    self.assertGreaterEqual(call.call_count,1)
                    calls+=call.call_count;reported+=call.call_count if tokens is not None else 0
                    usage=self.client.get('/api/brain/usage').json()['models'][model_id]
                    self.assertEqual(calls,usage['request_count'])
                    self.assertEqual(reported,usage['token_reported_requests'])
                    self.assertEqual(calls-reported,usage['unreported_requests'])
                    self.assertEqual(reported*12,usage['total_tokens'])

    def test_two_conversations_can_stream_the_same_model_concurrently(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        barrier=threading.Barrier(2)
        def generate(operation,payload):
            text=next(message['content'] for message in reversed(payload['messages']) if message['role']=='user')
            barrier.wait(timeout=3)
            return {'text':text}
        with patch.object(self.provider,'_run_mlx',side_effect=generate), ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.client.post,'/api/chat/stream',json={'message':'Reply with privateAlpha.','conversation_id':'one','project_id':self.project})
            second=pool.submit(self.client.post,'/api/chat/stream',json={'message':'Reply with privateBeta.','conversation_id':'two','project_id':self.project})
            a,b=first.result(timeout=5),second.result(timeout=5)
        self.assertEqual(200,a.status_code);self.assertEqual(200,b.status_code)
        self.assertIn('privateAlpha',a.text);self.assertNotIn('privateBeta',a.text)
        self.assertIn('privateBeta',b.text);self.assertNotIn('privateAlpha',b.text)
        self.assertNotIn('privateBeta',json.dumps(self.db.conversation('one')['messages']))
        self.assertNotIn('privateAlpha',json.dumps(self.db.conversation('two')['messages']))

    def test_model_concurrency_limit_is_an_explicit_stream_failure(self):
        with patch.object(self.provider,'_run_mlx',side_effect=ModelFailure('model_concurrency_limit','MODEL_CONCURRENCY_LIMIT: fixture capacity rejection')):
            response=self.client.post('/api/chat/stream',json={'message':'Reply concisely.','conversation_id':'one','project_id':self.project})
        events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        failure=next(event for event in events if event['type']=='error')
        self.assertIn('MODEL_CONCURRENCY_LIMIT',failure['message'])
        self.assertEqual('failed',failure['task']['state'])
        self.assertEqual([],self.db.conversation('two')['messages'])

    def test_stream_fallback_notice_uses_the_request_language_without_crossing_conversations(self):
        with patch.object(self.provider,'generate',return_value=iter([{'text':'fallback reply','fallback_from':'busy-primary','fallback_model_id':ORNITH_MLX}])):
            response=self.client.post('/api/chat/stream',headers={'X-OLCR-Language':'en'},json={'message':'Reply briefly.','conversation_id':'one','project_id':self.project})
        self.assertIn('fallback reply',response.text)
        self.assertIn('The selected model was unavailable',response.text)
        self.assertNotIn('"type": "error"',response.text)
        self.assertEqual([],self.db.conversation('two')['messages'])

    def test_provider_account_registration_priority_and_live_keychain_restore(self):
        manager=self.provider.brain_models
        for slot in (1,2,3):
            response=self.client.put('/api/brain/providers',json={'id':'openai','account_id':slot,'auth_type':'api_key',
                'values':{'base_url':'https://fixture.example/v1','model_ids':'account-fixture','api_key':f'fixture-secret-{slot}'}})
            self.assertEqual(200,response.status_code)
            self.assertNotIn('fixture-secret-',response.text)
        response=self.client.put('/api/brain/providers/openai/accounts/order',json={'account_order':[3,1,2]})
        self.assertEqual(200,response.status_code)
        with patch.object(manager,'_local_models',return_value=[]):
            catalogue=self.client.get('/api/brain/models').json()
        accounts=next(row for row in catalogue['providers'] if row['id']=='openai')['accounts']
        self.assertEqual([3,1,2],[row['account_id'] for row in accounts])
        self.assertEqual(1,len([row for row in catalogue['models'] if row['id']=='api:openai:account-fixture']))
        self.assertNotIn('fixture-secret-',json.dumps(catalogue))
        manager.providers.credentials.clear()
        with patch.dict('os.environ', {}):
            restored=self.client.put('/api/external-tools/keychain-restore',json={'credentials':{
                'OLCR_BRAIN_OPENAI_2':'restored-fixture-2','OLCR_BRAIN_OPENAI_3':'restored-fixture-3'}})
        self.assertEqual(200,restored.status_code)
        self.assertTrue(manager.providers.configured('openai',2))
        self.assertTrue(manager.providers.configured('openai',3))
        self.assertNotIn('restored-fixture-',restored.text)
        for slot in (0,4,True):
            response=self.client.put('/api/brain/providers',json={'id':'openai','account_id':slot,'auth_type':'api_key','values':{}})
            self.assertEqual(422,response.status_code)
        for order in ([1,1,2],[True,2,3],[1,2,4]):
            self.assertEqual(422,self.client.put('/api/brain/providers/openai/accounts/order',json={'account_order':order}).status_code)
        self.assertEqual(401,self.client.put('/api/brain/providers/openai/accounts/order',json={'account_order':[1,2,3]},headers={'X-OLCR-Session':'bad'}).status_code)

    def test_oauth_start_routes_the_selected_slot_and_rejects_a_fourth(self):
        providers=self.provider.brain_models.providers
        with patch.object(providers,'start_oauth',return_value={'attempt_id':'fixture','authorization_url':'https://fixture.example'}) as start:
            self.assertEqual(200,self.client.post('/api/brain/oauth/start?account_id=3').status_code)
            start.assert_called_once_with(3)
            self.assertEqual(422,self.client.post('/api/brain/oauth/start?account_id=4').status_code)
