import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,urlencode
from urllib.request import urlopen
from urllib.error import HTTPError
from unittest.mock import patch
from olcr_api.db import Database
from olcr_api.brain_models import BrainModels, brain_scope, INITIAL
from olcr_api.brain_providers import endpoint, decode_chatgpt_id_token, reasoning_profile, rate_limit_snapshot
from olcr_api.config import Settings
from olcr_api.model_provider import RoleAwareModelProvider
from olcr_api.ollama import ModelFailure
from olcr_api.structured_output import OutputMode

class BrainProviderTests(unittest.TestCase):
    def test_ollama_cloud_discovery_and_chat_are_separate_from_local_ollama(self):
        self.providers.register('ollama_cloud','api_key',{
            'base_url':'https://ollama.com/v1','api_key':'cloud-fixture','model_ids':''})
        self.providers.settings.external_access_enabled=True
        with patch('olcr_api.brain_providers.get_json',return_value={'data':[
            {'id':'gemini-cloud:latest'}, {'id':'embed-model'}]}) as fetch:
            models,providers=self.providers.catalog()
        cloud=[model for model in models if model['provider']=='Ollama Cloud']
        self.assertEqual(['api:ollama_cloud:gemini-cloud:latest'],[model['id'] for model in cloud])
        self.assertFalse(cloud[0]['local'])
        self.assertEqual('https://ollama.com/settings/usage',cloud[0]['usage_url'])
        self.assertTrue(next(row for row in providers if row['id']=='ollama_cloud')['configured'])
        fetch.assert_called_once_with('https://ollama.com/v1/models',{'Authorization':'Bearer cloud-fixture'},provider_label='Ollama Cloud')
        with patch('olcr_api.brain_providers.call_json',return_value={
            'choices':[{'message':{'content':'answer','reasoning_content':'received reasoning'}}],
            'usage':{'prompt_tokens':4,'completion_tokens':2}}) as call:
            result=self.providers.generate('ollama_cloud','gemini-cloud:latest',[{'role':'user','content':'hello'}],None,True)
        self.assertEqual('answer',result['text'])
        self.assertEqual('https://ollama.com/v1/chat/completions',call.call_args.args[0])
        self.assertEqual({'Authorization':'Bearer cloud-fixture'},call.call_args.args[2])
        self.assertEqual('gemini-cloud:latest',call.call_args.args[1]['model'])
        self.assertNotIn('reasoning_effort',call.call_args.args[1])
        with self.db.connect() as con:
            self.assertNotIn('cloud-fixture',con.execute('SELECT metadata_json FROM brain_providers WHERE id=?',('ollama_cloud',)).fetchone()[0])

    def test_ollama_cloud_restores_each_account_and_respects_external_access(self):
        restored=self.providers.restore_credentials({'OLCR_BRAIN_OLLAMA_CLOUD':'cloud-one',
            'OLCR_BRAIN_OLLAMA_CLOUD_2':'cloud-two','OLCR_BRAIN_OLLAMA_CLOUD_3':'cloud-three'})
        self.assertEqual(3,restored)
        self.providers.settings.external_access_enabled=False
        with patch('olcr_api.brain_providers.get_json') as fetch,patch('olcr_api.brain_providers.call_json') as call:
            _,providers=self.providers.catalog()
            row=next(row for row in providers if row['id']=='ollama_cloud')
            self.assertEqual(3,sum(bool(account['configured']) for account in row['accounts']))
            with self.assertRaises(ModelFailure):
                self.providers.generate('ollama_cloud','test',[{'role':'user','content':'hi'}],None,False)
        fetch.assert_not_called();call.assert_not_called()

    def test_native_binary_thinking_controls_and_automatic_choice(self):
        self.providers.settings.external_access_enabled=True
        cases=[('google','gemini-2.5-flash',{'candidates':[{'content':{'parts':[{'text':'ok'}]}}]}),
               ('anthropic','claude-sonnet-4-20250514',{'content':[{'type':'text','text':'ok'}]})]
        for provider,model,response in cases:
            self.providers.register(provider,'api_key',{'base_url':'http://127.0.0.1:9/v1','api_key':'synthetic','model_ids':model})
            for mode,text,enabled in [('off','比較して',False),('on','hello',True),('auto','hello',False),('auto','検算して',True)]:
                with self.subTest(provider=provider,mode=mode,text=text),patch('olcr_api.brain_providers.call_json',return_value=response) as call:
                    self.providers.generate(provider,model,[{'role':'user','content':text}],None,True,effort=mode)
                    payload=call.call_args.args[1]
                    if provider=='google':
                        config=payload['generationConfig']['thinkingConfig']
                        self.assertEqual(1024 if enabled else 0,config['thinkingBudget'])
                        self.assertNotIn('thinkingLevel',config)
                    else:
                        self.assertEqual('enabled' if enabled else 'disabled',payload['thinking']['type'])
                        self.assertNotIn('effort',payload.get('output_config',{}))
                        if enabled:self.assertLess(payload['thinking']['budget_tokens'],payload['max_tokens'])
        self.assertEqual([],reasoning_profile('google','gemini-2.5-pro')['effort_levels'])
        self.assertEqual([],reasoning_profile('google','unknown')['effort_levels'])

    def test_binary_api_mode_uses_task_role_and_original_request_even_for_json(self):
        model='gemini-2.5-flash'
        self.providers.register('google','api_key',{'base_url':'http://127.0.0.1:9/v1beta','api_key':'synthetic','model_ids':model})
        provider=RoleAwareModelProvider(Settings());provider.brain_models=self.manager
        configuration=json.loads(json.dumps(INITIAL))
        configuration['selections']['coder']='api:google:'+model
        response={'candidates':[{'content':{'parts':[{'text':'{}'}]}}]}
        try:
            for mode,expected in [('off',False),('auto',True),('on',True)]:
                configuration['efforts']['coder']=mode
                with self.subTest(mode=mode),brain_scope(configuration),patch('olcr_api.brain_providers.call_json',return_value=response) as call:
                    result=provider.generate([{'role':'user','content':'internal JSON instruction'}],'role:long_implementation',
                        format={'type':'object'},think=False,thinking_text='作成して')
                    self.assertEqual(expected,result['thinking_decision']['enabled'])
                    self.assertEqual(1024 if expected else 0,call.call_args.args[1]['generationConfig']['thinkingConfig']['thinkingBudget'])
        finally:provider.close()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();db=Database(Path(self.tmp.name)/'providers.db');db.initialize()
        self.manager=BrainModels(db,Settings());self.db=db;self.providers=self.manager.providers
    def tearDown(self):self.tmp.cleanup()
    def register(self,name,base='https://provider.example/v1'):
        return self.providers.register(name,'api_key',{'base_url':base,'api_key':'synthetic-key','model_ids':'text-brain'})
    def test_openai_reasoning_profile_is_disabled_for_custom_compatible_endpoints(self):
        self.assertEqual([],reasoning_profile('openai','gpt-6-luna','https://provider.example/v1')['effort_levels'])
        self.assertEqual(['minimal','low','medium','high','xhigh'],
                         reasoning_profile('openai','gpt-6-luna','https://api.openai.com/v1')['effort_levels'])
        self.providers.register('openai','api_key',{'base_url':'https://provider.example/v1',
            'api_key':'synthetic-key','model_ids':'gpt-6-luna'})
        with patch('olcr_api.brain_providers.call_json',return_value={'choices':[{'message':{'content':'answer'}}]}) as call:
            self.providers.generate('openai','gpt-6-luna',[{'role':'user','content':'hello'}],None,True,effort='medium')
        self.assertNotIn('reasoning_effort',call.call_args.args[1])
    def test_unsupported_openai_schema_uses_one_accurate_compatibility_notice_without_retrying(self):
        from olcr_api.model_progress import observe_model_progress
        self.providers.register('openai','api_key',{'base_url':'https://api.openai.com/v1',
            'api_key':'synthetic-key','model_ids':'gpt-6-luna'})
        self.providers.settings.external_access_enabled=True
        schemas=[{'type':'object','properties':{'optional':{'type':'string'}},'required':[],
                  'additionalProperties':False},
                 {'type':'object','properties':{'other':{'type':'number'}},'required':[],
                  'additionalProperties':False}]
        response={'choices':[{'message':{'content':'{}'}}]}
        events=[]
        with patch('olcr_api.brain_providers.call_json',return_value=response) as call, \
             observe_model_progress(events.append):
            for schema in schemas:
                self.providers.generate('openai','gpt-6-luna',
                    [{'role':'user','content':'return a result'}],schema,True)

        self.assertEqual(2,call.call_count)
        self.assertTrue(all('response_format' not in item.args[1] for item in call.call_args_list))
        notices=[event for event in events if event.get('state')=='schema_compatibility']
        self.assertEqual(1,len(notices))
        self.assertEqual('schema_unsupported_by_native_mode',notices[0].get('reason'))
        self.providers.register('openai','api_key',{'base_url':'https://provider.example/v1',
            'api_key':'synthetic-key','model_ids':'gpt-6-luna'})
        with patch('olcr_api.brain_providers.call_json',return_value={'choices':[{'message':{'content':'answer'}}]}) as call:
            self.providers.generate('openai','gpt-6-luna',[{'role':'user','content':'hello'}],None,True,effort='medium')
        self.assertNotIn('reasoning_effort',call.call_args.args[1])
    def test_reasoning_parameter_rejection_retries_without_changing_schema(self):
        self.providers.register('openai','api_key',{'base_url':'https://api.openai.com/v1',
            'api_key':'synthetic-key','model_ids':'gpt-6-luna'})
        self.providers.settings.external_access_enabled=True
        failure=ModelFailure('provider_rejected','OpenAI request failed (HTTP 400) · invalid_request_error · reasoning.effort')
        failure.http_status=400;failure.provider_code='invalid_request_error';failure.parameter='reasoning.effort'
        failure.schema_format_rejection=False
        schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
        with patch('olcr_api.brain_providers.resolve_output_mode',return_value=OutputMode.JSON_SCHEMA),\
             patch('olcr_api.brain_providers.call_json',side_effect=[failure,{'choices':[{'message':{'content':'{"ok":true}'}}]}]) as call:
            result=self.providers.generate('openai','gpt-6-luna',[{'role':'user','content':'hello'}],schema,True,effort='medium')
            self.assertEqual('{"ok":true}',result['text'])
        self.assertEqual(2,call.call_count)
        first,second=(call.call_args_list[0].args[1],call.call_args_list[1].args[1])
        self.assertIn('reasoning_effort',first)
        self.assertNotIn('reasoning_effort',second)
        self.assertEqual(first['response_format'],second['response_format'])
        with patch('olcr_api.brain_providers.call_json',return_value={'choices':[{'message':{'content':'{"ok":true}'}}]}) as cached:
            self.providers.generate('openai','gpt-6-luna',[{'role':'user','content':'hello'}],schema,True,effort='medium')
        self.assertEqual(1,cached.call_count)
        self.assertNotIn('reasoning_effort',cached.call_args.args[1])
    def test_google_and_anthropic_map_developer_messages_to_system_fields(self):
        messages=[{'role':'developer','content':'Follow the house rules.'},
                  {'role':'user','content':'Answer this.'}]
        responses={
            'google':{'candidates':[{'content':{'parts':[{'text':'answer'}]}}]},
            'anthropic':{'content':[{'type':'text','text':'answer'}]},
        }
        for provider in ('google','anthropic'):
            with self.subTest(provider=provider):
                self.providers.register(provider,'api_key',{'base_url':f'https://{provider}.example/v1',
                    'api_key':'synthetic-key','model_ids':'text-brain'})
                with patch('olcr_api.brain_providers.call_json',return_value=responses[provider]) as call:
                    result=self.providers.generate(provider,'text-brain',messages,None,True)
                self.assertEqual('answer',result['text'])
                body=call.call_args.args[1]
                if provider=='google':
                    self.assertEqual('Follow the house rules.',body['systemInstruction']['parts'][0]['text'])
                    self.assertEqual([{'role':'user','parts':[{'text':'Answer this.'}]}],body['contents'])
                else:
                    self.assertEqual('Follow the house rules.',body['system'])
                    self.assertEqual([{'role':'user','content':'Answer this.'}],body['messages'])
    def test_openai_compatible_response_accepts_text_content_parts(self):
        self.register('huggingface')
        response={'choices':[{'message':{'content':[{'type':'text','text':'answer'},
                                                    {'type':'image_url','image_url':{'url':'ignored'}}]}}]}
        with patch('olcr_api.brain_providers.call_json',return_value=response):
            result=self.providers.generate('huggingface','text-brain',[{'role':'user','content':'hello'}],None,True)
        self.assertEqual('answer',result['text'])
    def test_responses_api_drops_rejected_effort_and_remembers_provider_capability(self):
        self.providers.credentials['openai_oauth']={'access_token':'synthetic','id_token':'synthetic',
            'client_id':'oaiapp_test','scope':'chatgpt.tokens.use.direct','expires_at':time.time()+3600}
        self.providers.settings.external_access_enabled=True
        with patch('olcr_api.brain_providers.get_json',return_value={'models':[]}):self.providers.catalog()
        failure=ModelFailure('provider_rejected','HTTP 400 · invalid_request_error · reasoning.effort')
        failure.http_status=400;failure.provider_code='invalid_request_error';failure.parameter='reasoning.effort'
        failure.schema_format_rejection=False
        with patch('olcr_api.brain_providers.BrainProviders._responses_stream',
                   side_effect=[failure,('answer',{})]) as stream:
            result=self.providers.generate('openai_oauth','gpt-6-luna',
                [{'role':'user','content':'hello'}],None,True,effort='medium')
        self.assertEqual('answer',result['text']);self.assertEqual(2,stream.call_count)
        self.assertEqual({'effort':'medium'},stream.call_args_list[0].args[1]['reasoning'])
        self.assertNotIn('reasoning',stream.call_args_list[1].args[1])
        with patch('olcr_api.brain_providers.BrainProviders._responses_stream',return_value=('answer',{})) as cached:
            self.providers.generate('openai_oauth','gpt-6-luna',
                [{'role':'user','content':'hello'}],None,True,effort='medium')
        self.assertNotIn('reasoning',cached.call_args.args[1])
    def test_responses_schema_fallback_is_reused_for_a_different_contract(self):
        self.providers.credentials['openai_oauth']={'access_token':'synthetic','id_token':'synthetic',
            'client_id':'oaiapp_test','scope':'chatgpt.tokens.use.direct','expires_at':time.time()+3600}
        self.providers.settings.external_access_enabled=True
        with patch('olcr_api.brain_providers.get_json',return_value={'models':[]}):self.providers.catalog()
        failure=ModelFailure('provider_rejected','HTTP 400 · invalid_request_error · text.format')
        failure.http_status=400;failure.provider_code='invalid_request_error'
        failure.parameter='text.format';failure.schema_format_rejection=True
        schemas=[{'type':'object','properties':{'first':{'type':'string'}},'required':['first'],'additionalProperties':False},
                 {'type':'object','properties':{'second':{'type':'boolean'}},'required':['second'],'additionalProperties':False}]
        with patch('olcr_api.brain_providers.BrainProviders._responses_stream',
                   side_effect=[failure,('answer',{}),('answer',{})]) as stream:
            for schema in schemas:
                self.providers.generate('openai_oauth','gpt-6-luna',
                    [{'role':'user','content':'return structured data'}],schema,True)

        self.assertEqual(3,stream.call_count)
        self.assertIn('text',stream.call_args_list[0].args[1])
        self.assertNotIn('text',stream.call_args_list[2].args[1])
    def test_rate_limit_snapshot_keeps_only_allowlisted_rate_fields(self):
        values=rate_limit_snapshot({'X-RateLimit-Limit-Tokens':'9000',
                                    'X-RateLimit-Remaining-Tokens':'8123',
                                    'Retry-After':'2',
                                    'Authorization':'Bearer secret',
                                    'Set-Cookie':'session=private'})
        self.assertEqual({'x-ratelimit-limit-tokens':'9000',
                          'x-ratelimit-remaining-tokens':'8123','retry-after':'2'},values)
    def test_registration_has_no_network_no_secret_in_database_and_auxiliary_models_rejected(self):
        with patch('olcr_api.brain_providers.call_json') as call:
            self.register('openai');call.assert_not_called()
        with self.db.connect() as db:
            saved=str(db.execute('SELECT * FROM brain_providers').fetchall()[0]['metadata_json'])
        self.assertNotIn('synthetic-key',saved)
        self.assertNotIn('synthetic-key',json.dumps(self.manager.catalog()))
        with self.assertRaises(ValueError):self.providers.register('google','api_key',{'base_url':'https://provider.example','api_key':'x','model_ids':'embedding-model'})
        for url in ['http://remote.example','https://user:pass@remote.example','file:///tmp/key','https://remote.example/?secret=1']:
            with self.assertRaises(ValueError):endpoint(url)

    def test_effort_stages_follow_model_profiles_and_use_provider_request_fields(self):
        self.assertEqual(['minimal','low','medium','high','xhigh'],
                         reasoning_profile('openai_oauth','gpt-6.1-sol')['effort_levels'])
        self.assertEqual(['off','auto','on'],reasoning_profile('google','gemini-2.5-flash')['effort_levels'])
        self.assertIsNone(reasoning_profile('unknown','model')['effort_default'])
        self.providers.settings.external_access_enabled=True
        messages=[{'role':'user','content':'hello'}]
        self.providers.register('openai','api_key',{'base_url':'https://api.openai.com/v1','api_key':'openai-key','model_ids':'gpt-6.1-sol'})
        with patch('olcr_api.brain_providers.call_json',return_value={'choices':[{'message':{'content':'ok'}}]}) as call:
            self.providers.generate('openai','gpt-6.1-sol',messages,None,True,effort='xhigh')
        self.assertEqual('xhigh',call.call_args.args[1]['reasoning_effort'])

        self.providers.register('google','api_key',{'base_url':'http://127.0.0.1:9/v1beta','api_key':'google-key','model_ids':'gemini-3.7-flash'})
        with patch('olcr_api.brain_providers.call_json',return_value={'candidates':[{'content':{'parts':[{'text':'ok'}]}}]}) as call:
            self.providers.generate('google','gemini-3.7-flash',messages,None,True,effort='high')
        self.assertEqual('high',call.call_args.args[1]['generationConfig']['thinkingConfig']['thinkingLevel'])

        self.providers.register('anthropic','api_key',{'base_url':'http://127.0.0.1:9/v1','api_key':'anthropic-key','model_ids':'claude-opus-4-6'})
        with patch('olcr_api.brain_providers.call_json',return_value={'content':[{'type':'text','text':'ok'}]}) as call:
            self.providers.generate('anthropic','claude-opus-4-6',messages,None,True,effort='high')
        self.assertEqual('high',call.call_args.args[1]['output_config']['effort'])
        self.assertEqual({'type':'adaptive'},call.call_args.args[1]['thinking'])
    def test_remote_calls_require_external_permission_and_keep_requested_model(self):
        self.register('openai')
        with patch('olcr_api.brain_providers.call_json') as call:
            with self.assertRaises(ModelFailure):self.providers.generate('openai','text-brain',[],None,False)
            call.assert_not_called()
        self.register('openai','http://127.0.0.1:9222/v1')
        provider=RoleAwareModelProvider(Settings());provider.brain_models=self.manager
        configuration=json.loads(json.dumps(INITIAL));configuration['selections']['short_coding']='api:openai:text-brain'
        schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
        with brain_scope(configuration),patch('olcr_api.brain_providers.resolve_output_mode',return_value=OutputMode.JSON_SCHEMA),patch('olcr_api.brain_providers.call_json',return_value={'choices':[{'message':{'content':'{"ok":true}'}}],'usage':{'prompt_tokens':4,'completion_tokens':3}}) as call:
            value=provider.generate([],'role:short_coding_fix_debug',format=schema,stream=True)
            result=list(value)[0]
            self.assertEqual('text-brain',result['model']);self.assertEqual('short_coding_fix_debug',result['model_role'])
            self.assertEqual(schema,call.call_args.args[1]['response_format']['json_schema']['schema'])
            self.assertEqual(4,result['prompt_tokens']);self.assertTrue(result['done'])
        provider.close()
    def test_coding_output_budget_reaches_each_api_wire_format(self):
        cases=[('openai','gpt-6-luna','https://api.openai.com/v1','max_completion_tokens'),
               ('openai','gpt-4-turbo','https://api.openai.com/v1','max_tokens'),
               ('openai','gpt-6-luna','http://127.0.0.1:9/v1','max_tokens'),
               ('huggingface','fixture-code','http://127.0.0.1:9/v1','max_tokens'),
               ('google','fixture-code','http://127.0.0.1:9/v1beta','maxOutputTokens'),
               ('anthropic','fixture-code','http://127.0.0.1:9/v1','max_tokens')]
        settings=Settings();settings.external_access_enabled=True
        provider=RoleAwareModelProvider(settings);provider.brain_models=self.manager
        self.addCleanup(provider.close)
        self.providers.settings.external_access_enabled=True
        for name,model,base,field in cases:
            with self.subTest(provider=name,base=base,model=model):
                self.providers.register(name,'api_key',{'base_url':base,'api_key':'synthetic',
                    'model_ids':model})
                configuration=json.loads(json.dumps(INITIAL))
                configuration['selections']['coder']=f'api:{name}:{model}'
                responses={'google':{'candidates':[{'content':{'parts':[{'text':'{}'}]},'finishReason':'STOP'}]},
                           'anthropic':{'content':[{'type':'text','text':'{}'}],'stop_reason':'end_turn'}}
                response=responses.get(name,{'choices':[{'message':{'content':'{}'},'finish_reason':'stop'}]})
                with brain_scope(configuration),patch('olcr_api.brain_providers.call_json',return_value=response) as call:
                    result=provider.generate([{'role':'user','content':'implement'}],
                        'role:long_implementation',format={'type':'object'},max_tokens=12288)
                payload=call.call_args.args[1]
                target=payload.get('generationConfig',{}) if name=='google' else payload
                self.assertEqual(12288,target.get(field))
                self.assertTrue(result['done'])

    def test_chatgpt_responses_receives_explicit_output_budget(self):
        self.providers.credentials['openai_oauth']={'access_token':'synthetic','id_token':'synthetic',
            'client_id':'oaiapp_test','scope':'chatgpt.tokens.use.direct','expires_at':time.time()+3600}
        self.providers.settings.external_access_enabled=True
        with patch('olcr_api.brain_providers.get_json',return_value={'models':[]}):self.providers.catalog()
        settings=Settings();settings.external_access_enabled=True
        provider=RoleAwareModelProvider(settings);provider.brain_models=self.manager
        self.addCleanup(provider.close)
        configuration=json.loads(json.dumps(INITIAL))
        configuration['selections']['coder']='api:openai_oauth:gpt-6-luna'
        with brain_scope(configuration),patch.object(self.providers,'_responses_stream',return_value=('{}',{})) as stream:
            provider.generate([{'role':'user','content':'implement'}],'role:long_implementation',
                format={'type':'object'},max_tokens=12288)
        self.assertEqual(12288,stream.call_args.args[1].get('max_output_tokens'))

    def test_anthropic_thinking_fits_the_callers_small_output_budget(self):
        model='claude-sonnet-4-20250514'
        self.providers.register('anthropic','api_key',{'base_url':'http://127.0.0.1:9/v1',
            'api_key':'synthetic','model_ids':model})
        configuration=json.loads(json.dumps(INITIAL))
        configuration['selections']['coder']='api:anthropic:'+model
        configuration['efforts']['coder']='on'
        provider=RoleAwareModelProvider(Settings());provider.brain_models=self.manager
        self.addCleanup(provider.close)
        response={'content':[{'type':'text','text':'{}'}],'stop_reason':'end_turn'}
        for budget,wire_budget,enabled in ((384,384,False),(1024,1024,False),
                                            (1025,1025,True),(None,4096,True)):
            with self.subTest(budget=budget),brain_scope(configuration), \
                 patch('olcr_api.brain_providers.call_json',return_value=response) as call:
                result=provider.generate([{'role':'user','content':'implement'}],'role:long_implementation',
                    format={'type':'object'},max_tokens=budget)
                payload=call.call_args.args[1]
                self.assertEqual(wire_budget,payload['max_tokens'])
                self.assertEqual('enabled' if enabled else 'disabled',payload['thinking']['type'])
                self.assertIs(enabled,result['thinking_decision']['enabled'])
                self.assertEqual('on',result['thinking_decision']['mode'])
                if enabled:self.assertLess(payload['thinking']['budget_tokens'],wire_budget)

    def test_rejected_optional_output_budget_preserves_schema_and_is_remembered(self):
        from olcr_api.provider_errors import provider_http_failure
        self.providers.register('openai','api_key',{'base_url':'https://api.openai.com/v1',
            'api_key':'synthetic','model_ids':'gpt-6-luna'})
        self.providers.settings.external_access_enabled=True
        settings=Settings();settings.external_access_enabled=True
        provider=RoleAwareModelProvider(settings);provider.brain_models=self.manager
        self.addCleanup(provider.close)
        configuration=json.loads(json.dumps(INITIAL))
        configuration['selections']['coder']='api:openai:gpt-6-luna'
        failure=provider_http_failure(HTTPError('https://example.test',400,'bad',{},io.BytesIO(
            b'{"error":{"code":"unsupported_parameter","param":"max_completion_tokens","message":"Unsupported parameter"}}')))
        schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
        response={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]}
        with brain_scope(configuration),patch('olcr_api.brain_providers.call_json',side_effect=[failure,response,response]) as call:
            for _ in range(2):
                result=provider.generate([{'role':'user','content':'implement'}],'role:long_implementation',
                    format=schema,max_tokens=12288)
                self.assertEqual('{"ok":true}',result['text'])
        self.assertEqual(3,call.call_count)
        self.assertEqual(12288,call.call_args_list[0].args[1].get('max_completion_tokens'))
        for attempt in call.call_args_list[1:]:
            self.assertNotIn('max_completion_tokens',attempt.args[1])
            self.assertEqual(schema,attempt.args[1]['response_format']['json_schema']['schema'])

    def test_truncated_api_responses_raise_output_limit_instead_of_done(self):
        partial='{"operations":['
        cases=[('openai',{'choices':[{'message':{'content':partial},'finish_reason':'length'}]}),
               ('google',{'candidates':[{'content':{'parts':[{'text':partial}]},'finishReason':'MAX_TOKENS'}]}),
               ('anthropic',{'content':[{'type':'text','text':partial}],'stop_reason':'max_tokens'}),
               ('anthropic',{'content':[{'type':'text','text':partial}],'stop_reason':'model_context_window_exceeded'})]
        for name,response in cases:
            with self.subTest(provider=name,response=response):
                self.providers.register(name,'api_key',{'base_url':'http://127.0.0.1:9/v1',
                    'api_key':'synthetic','model_ids':'fixture-code'})
                with patch('olcr_api.brain_providers.call_json',return_value=response),self.assertRaises(ModelFailure) as raised:
                    self.providers.generate(name,'fixture-code',[{'role':'user','content':'implement'}],
                        {'type':'object'},False)
                self.assertEqual('output_limit',raised.exception.category)

    def test_truncated_coding_response_uses_the_next_configured_model(self):
        self.providers.register('anthropic','api_key',{'base_url':'http://127.0.0.1:9/v1',
            'api_key':'synthetic','model_ids':'fixture-code'})
        self.providers.register('google','api_key',{'base_url':'http://127.0.0.1:9/v1',
            'api_key':'synthetic','model_ids':'fixture-next'})
        configuration=json.loads(json.dumps(INITIAL))
        configuration['selections']['coder']='api:anthropic:fixture-code'
        configuration['fallback_models']=['api:google:fixture-next']
        provider=RoleAwareModelProvider(Settings());provider.brain_models=self.manager
        self.addCleanup(provider.close)
        responses=[{'content':[{'type':'text','text':'{"operations":['}],'stop_reason':'max_tokens'},
                   {'candidates':[{'content':{'parts':[{'text':'{"operations":[]}' }]},'finishReason':'STOP'}]}]
        with brain_scope(configuration),patch('olcr_api.brain_providers.call_json',side_effect=responses) as call:
            result=provider.generate([{'role':'user','content':'implement'}],'role:long_implementation',
                format={'type':'object'},max_tokens=12288)
        self.assertEqual('{"operations":[]}',result['text'])
        self.assertEqual('api:google:fixture-next',result['fallback_model_id'])
        self.assertEqual(12288,call.call_args_list[1].args[1]['generationConfig']['maxOutputTokens'])

    def test_chatgpt_incomplete_token_limit_is_output_limit(self):
        events=[{'type':'response.output_text.delta','delta':'{"operations":['},
                {'type':'response.incomplete','response':{'status':'incomplete',
                    'incomplete_details':{'reason':'max_output_tokens'},'usage':{'output_tokens':42}}}]
        response=io.BytesIO(b''.join(b'data: '+json.dumps(event).encode()+b'\n\n' for event in events))
        response.headers={}
        with patch('olcr_api.brain_providers.open_provider_response',return_value=response),self.assertRaises(ModelFailure) as raised:
            self.providers._responses_stream('https://example.test/responses',{},'synthetic')
        self.assertEqual('output_limit',raised.exception.category)
        self.assertEqual('{"operations":[',raised.exception.model_traffic_partial['text'])
    def test_google_and_anthropic_keep_system_messages_and_structured_contract(self):
        messages=[{'role':'system','content':'instruction'},{'role':'user','content':'question'}];schema={'type':'object'}
        for name,response in [('google',{'candidates':[{'content':{'parts':[{'text':'{"ok":true}'}]}}]}),('anthropic',{'content':[{'type':'text','text':'{"ok":true}'}]})]:
            self.register(name)
            with patch('olcr_api.brain_providers.resolve_output_mode',return_value=OutputMode.JSON_SCHEMA),patch('olcr_api.brain_providers.call_json',return_value=response) as call:
                self.assertEqual('{"ok":true}',self.providers.generate(name,'text-brain',messages,schema,True)['text'])
                body=call.call_args.args[1]
                if name=='google':
                    self.assertEqual(schema,body['generationConfig']['responseJsonSchema'])
                    self.assertEqual('instruction',body['systemInstruction']['parts'][0]['text'])
                else:
                    self.assertEqual(schema,body['output_config']['format']['schema'])
                    self.assertEqual('instruction',body['system'])

    def test_nvidia_nim_discovers_models_and_uses_openai_compatible_chat_api(self):
        self.providers.register('nvidia_nim','api_key',{
            'base_url':'https://integrate.api.nvidia.com/v1',
            'api_key':'nim-key-fixture','model_ids':''})
        self.providers.settings.external_access_enabled=True
        response={'data':[
            {'id':'nvidia/llama-3.3-nemotron-super-49b-v1','object':'model'},
            {'id':'nvidia/nv-embedqa-e5-v5','object':'model'},
        ]}
        schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
        with patch('olcr_api.brain_providers.get_json',return_value=response) as fetch, \
             patch('olcr_api.brain_providers.call_json',return_value={
                 'choices':[{'message':{'content':'{"ok":true}'}}],
                 'usage':{'prompt_tokens':4,'completion_tokens':2}}) as call:
            models,providers=self.providers.catalog()
            nim_models=[model for model in models if model['provider']=='NVIDIA NIM']
            self.assertEqual(['api:nvidia_nim:nvidia/llama-3.3-nemotron-super-49b-v1'],
                             [model['id'] for model in nim_models])
            self.assertTrue(nim_models[0]['available'])
            provider=next(row for row in providers if row['id']=='nvidia_nim')
            self.assertTrue(provider['configured'])
            self.assertEqual('https://integrate.api.nvidia.com/v1',provider['base_url'])
            fetch.assert_called_once_with('https://integrate.api.nvidia.com/v1/models',
                                          {'Authorization':'Bearer nim-key-fixture'},
                                          provider_label='NVIDIA NIM')
            result=self.providers.generate('nvidia_nim','nvidia/llama-3.3-nemotron-super-49b-v1',
                [{'role':'system','content':'follow rules'},{'role':'user','content':'hello'}],schema,True)
        self.assertEqual('{"ok":true}',result['text'])
        self.assertEqual('https://integrate.api.nvidia.com/v1/chat/completions',call.call_args.args[0])
        self.assertEqual({'Authorization':'Bearer nim-key-fixture'},call.call_args.args[2])
        self.assertEqual('nvidia/llama-3.3-nemotron-super-49b-v1',call.call_args.args[1]['model'])
        self.assertNotIn('response_format',call.call_args.args[1])
        self.assertIn('JSON Schema',call.call_args.args[1]['messages'][-1]['content'])

    def test_nvidia_nim_restored_key_stays_discoverable_but_never_calls_when_external_access_is_off(self):
        self.providers.credentials['nvidia_nim']='nim-key-fixture'
        self.providers.settings.external_access_enabled=False
        with patch('olcr_api.brain_providers.get_json') as fetch:
            models,providers=self.providers.catalog()
        fetch.assert_not_called()
        self.assertTrue(next(row for row in providers if row['id']=='nvidia_nim')['configured'])
        self.assertFalse(any(model['provider']=='NVIDIA NIM' for model in models))
    def test_chatgpt_oauth_dynamic_registration_pkce_and_keychain_handoff(self):
        started=self.providers.start_oauth();query=parse_qs(urlsplit(started['authorization_url']).query)
        self.assertEqual('https://auth.openai.com/api/accounts/authorize',urlsplit(started['authorization_url'])._replace(query='',fragment='').geturl())
        self.assertEqual(['dynamic_agent_client'],query['client_id'])
        self.assertEqual(['OLCR'],query['agent_name_hint'])
        self.assertEqual(['https://api.openai.com/v1'],query['resource'])
        self.assertEqual(['openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'],query['scope'])
        self.assertTrue(query['ext_agent_host_id'][0].startswith('urn:uuid:'))
        self.assertEqual(['S256'],query['code_challenge_method'])
        self.assertNotIn('code_verifier',query)
        self.assertTrue(urlsplit(query['redirect_uri'][0]).path.endswith('/auth/callback'))
        callback=query['redirect_uri'][0]+'?'+urlencode({'state':query['state'][0],'code':'synthetic-code','client_id':'oaiapp_test-client',
                      'scope':query['scope'][0]})
        with patch('olcr_api.brain_providers.call_json',return_value={'access_token':'synthetic-access','refresh_token':'synthetic-refresh',
                 'id_token':'synthetic-id','scope':query['scope'][0],'expires_in':3600}) as call, \
             patch('olcr_api.brain_providers.decode_chatgpt_id_token',return_value={'sub':'synthetic-sub','email':'synthetic@example.test'}) as verify:
            with urlopen(callback) as result:self.assertIn(b'complete',result.read())
            payload=call.call_args.args[1]
            self.assertTrue(payload['code_verifier']);self.assertEqual(query['redirect_uri'][0],payload['redirect_uri'])
            self.assertEqual('oaiapp_test-client',payload['client_id']);self.assertEqual('https://api.openai.com/v1',payload['resource'])
            verify.assert_called_once_with('synthetic-id','oaiapp_test-client',query['nonce'][0])
        polled=self.providers.poll_oauth(started['attempt_id'])
        self.assertEqual({'status':'complete'},polled)
        self.assertTrue(self.providers.configured('openai_oauth'))
        self.assertEqual('expired',self.providers.poll_oauth(started['attempt_id'])['status'])
        pending=self.providers.pending_credential()
        self.assertNotIn('synthetic-access',str(polled))
        self.assertEqual('oaiapp_test-client',json.loads(pending['credential'])['client_id'])
        self.providers.acknowledge_credential('0'*64)
        self.assertEqual(pending,self.providers.pending_credential())
        self.providers.acknowledge_credential(pending['version'])
        self.assertEqual({'credential':None},self.providers.pending_credential())
        with self.db.connect() as db:
            self.assertNotIn('synthetic-access',str(db.execute('SELECT metadata_json FROM brain_providers').fetchall()[0][0]))

    def test_chatgpt_id_token_verifies_signature_audience_issuer_and_nonce(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        public_jwk=jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(),as_dict=True)
        public_jwk.update(kid='test-key',alg='RS256',use='sig')
        token=jwt.encode({'iss':'https://auth.openai.com','aud':'oaiapp_test','sub':'subject',
                          'iat':int(time.time()),'exp':int(time.time())+300,'nonce':'expected-nonce'},private,
                         algorithm='RS256',headers={'kid':'test-key'})
        with patch('olcr_api.brain_providers.get_json',return_value={'keys':[public_jwk]}):
            claims=decode_chatgpt_id_token(token,'oaiapp_test','expected-nonce')
            with self.assertRaisesRegex(ValueError,'could not be verified'):
                decode_chatgpt_id_token(token,'oaiapp_test','wrong-nonce')
        self.assertEqual('subject',claims['sub'])

    def test_generic_openai_oauth_settings_are_not_reused_as_chatgpt_plan_settings(self):
        with self.db.connect() as db:
            db.execute('INSERT INTO brain_providers VALUES(?,?)',('openai_oauth',json.dumps({
                'base_url':'https://custom.example/v1','authorization_url':'https://custom.example/auth',
                'token_url':'https://custom.example/token','client_id':'static-client','model_ids':['private-model']})))
        metadata=self.providers.metadata('openai_oauth')
        self.assertEqual('https://api.openai.com/v1',metadata['base_url'])
        self.assertEqual([],metadata['model_ids'])
        with self.assertRaisesRegex(ValueError,'Continue with ChatGPT'):
            self.providers.register('openai_oauth','oauth',{'base_url':'https://api.openai.com/v1','client_id':'static-client',
                'authorization_url':'https://auth.openai.com/api/accounts/authorize','token_url':'https://auth.openai.com/api/accounts/oauth/token',
                'model_ids':'manual-model'})

    def test_refreshed_oauth_tokens_wait_for_keychain_acknowledgement(self):
        self.providers.credentials['openai_oauth']={'access_token':'old','refresh_token':'old-refresh','id_token':'id','client_id':'oaiapp_test',
            'scope':'chatgpt.tokens.use.direct','expires_at':0}
        metadata={'client_id':'oaiapp_test','token_url':'https://auth.openai.com/api/accounts/oauth/token'}
        with patch('olcr_api.brain_providers.call_json',return_value={'access_token':'new','refresh_token':'rotated-refresh','expires_in':3600}):
            self.assertEqual('new',self.providers.token('openai_oauth',metadata))
        pending=self.providers.pending_credential()
        self.assertEqual('rotated-refresh',json.loads(pending['credential'])['refresh_token'])
        self.providers.acknowledge_credential(pending['version'])
        self.assertEqual({'credential':None},self.providers.pending_credential())

    def test_chatgpt_oauth_does_not_refresh_just_to_discover_an_account_id(self):
        self.providers.credentials['openai_oauth']={'access_token':'old-access','refresh_token':'old-refresh',
            'id_token':'id','client_id':'oaiapp_test','scope':'chatgpt.tokens.use.direct',
            'expires_at':time.time()+3600}
        metadata={'client_id':'oaiapp_test','token_url':'https://auth.openai.com/api/accounts/oauth/token'}
        with patch('olcr_api.brain_providers.call_json') as refresh:
            self.assertEqual('old-access',self.providers.token('openai_oauth',metadata))
            self.assertEqual('old-access',self.providers.token('openai_oauth',metadata))
        refresh.assert_not_called()

    def test_chatgpt_uses_public_account_model_catalog_and_streaming_responses_api(self):
        self.providers.credentials['openai_oauth']={'access_token':'oauth-access','refresh_token':'refresh','id_token':'id',
            'client_id':'oaiapp_test',
            'scope':'chatgpt.tokens.use.direct','expires_at':time.time()+3600}
        self.providers.settings.external_access_enabled=True
        with patch('olcr_api.brain_providers.get_json',return_value={'models':[
                {'slug':'gpt-visible','display_name':'GPT Visible','visibility':'list'},
                {'slug':'gpt-hidden','display_name':'GPT Hidden','visibility':'hide'}]}) as fetch:
            models,providers=self.providers.catalog()
        self.assertEqual(['gpt-visible','gpt-6-luna','gpt-6.1-sol'],
                         [m['id'].rsplit(':',1)[-1] for m in models if m['provider']=='OpenAI Codex · ChatGPT plan'])
        self.assertEqual('GPT Visible',next(m['label'] for m in models if m['id'].endswith(':gpt-visible')))
        self.assertEqual('GPT-6 Luna',next(m['label'] for m in models if m['id'].endswith(':gpt-6-luna')))
        self.assertEqual('GPT-6.1 Sol',next(m['label'] for m in models if m['id'].endswith(':gpt-6.1-sol')))
        fetch.assert_called_once()
        self.assertEqual('https://api.openai.com/v1/models',fetch.call_args.args[0])
        self.assertEqual({'Authorization':'Bearer oauth-access'},fetch.call_args.args[1])
        schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
        with patch('olcr_api.brain_providers.BrainProviders._responses_stream',return_value=('answer',{'input_tokens':5,'output_tokens':2})) as stream:
            result=self.providers.generate('openai_oauth','gpt-6-luna',[{'role':'system','content':'follow rules'},
                    {'role':'user','content':'hello'}],schema,True)
        self.assertEqual('answer',result['text']);self.assertEqual(5,result['prompt_tokens'])
        usage=self.manager.usage()['models']['api:openai_oauth:gpt-6-luna']
        self.assertEqual((1,7),(usage['request_count'],usage['total_tokens']))
        self.assertEqual('https://api.openai.com/v1/responses',stream.call_args.args[0])
        payload=stream.call_args.args[1]
        self.assertEqual({'Authorization':'Bearer oauth-access'},stream.call_args.args[3])
        self.assertEqual('gpt-6-luna',payload['model'])
        self.assertTrue(payload['stream']);self.assertFalse(payload['store'])
        self.assertEqual('follow rules',payload['instructions'])
        self.assertEqual('json_schema',payload['text']['format']['type'])
        self.assertNotIn('/chat/completions',stream.call_args.args[0])

    def test_chatgpt_plan_schema_is_not_sent_as_an_invalid_wire_schema(self):
        from olcr_api.coding_tasks import plan_schema
        self.providers.credentials['openai_oauth']={'access_token':'fixture','id_token':'fixture','client_id':'oaiapp_test','scope':'chatgpt.tokens.use.direct','expires_at':time.time()+3600}
        self.providers.settings.external_access_enabled=True
        with patch('olcr_api.brain_providers.get_json',return_value={'models':[]}):self.providers.catalog()
        with patch('olcr_api.brain_providers.BrainProviders._responses_stream',return_value=('{}',{})) as stream:
            self.providers.generate('openai_oauth','gpt-6-luna',[{'role':'user','content':'make a plan'}],plan_schema(),True)
        stream.assert_called_once()
        payload=stream.call_args.args[1]
        self.assertNotIn('text',payload)
        self.assertIn('JSON Schema',payload['input'][-1]['content'])

    def test_chatgpt_model_discovery_respects_external_access_setting(self):
        self.providers.credentials['openai_oauth']={'access_token':'oauth-access','refresh_token':'refresh','id_token':'id',
            'client_id':'oaiapp_test','scope':'chatgpt.tokens.use.direct','expires_at':time.time()+3600}
        self.providers.settings.external_access_enabled=False
        with patch('olcr_api.brain_providers.get_json') as fetch:
            models,providers=self.providers.catalog()
        fetch.assert_not_called()
        self.assertFalse(any(m['provider']=='OpenAI Codex · ChatGPT plan' for m in models))

    def test_chatgpt_catalog_preserves_safe_http_status_for_diagnosis(self):
        self.providers.credentials['openai_oauth']={'access_token':'oauth-access','refresh_token':'refresh','id_token':'id',
            'client_id':'oaiapp_test','account_id':'chatgpt-account-fixture',
            'scope':'chatgpt.tokens.use.direct','expires_at':time.time()+3600}
        self.providers.settings.external_access_enabled=True
        from olcr_api.ollama import ModelFailure
        with patch('olcr_api.brain_providers.get_json',side_effect=ModelFailure(
                'provider_rejected','ChatGPT rejected model discovery (HTTP 401)')):
            _,providers=self.providers.catalog()
        row=next(item for item in providers if item['id']=='openai_oauth')
        self.assertEqual('ChatGPT rejected model discovery (HTTP 401)',row['availability_message'])
        self.assertNotIn('oauth-access',row['availability_message'])

    def test_restored_google_key_reconstructs_provider_and_discovers_generative_models(self):
        self.providers.credentials['google']='restored-key-fixture'
        self.providers.settings.external_access_enabled=True
        response={'models':[
            {'name':'models/gemini-2.5-flash','displayName':'Gemini Flash',
             'supportedGenerationMethods':['generateContent']},
            {'name':'models/text-embedding-004','supportedGenerationMethods':['embedContent']},
            {'name':'models/gemini-audio','supportedGenerationMethods':['generateContent']},
        ]}
        with patch('olcr_api.brain_providers.get_json',return_value=response) as fetch, \
             patch('olcr_api.brain_providers.call_json',return_value={
                 'candidates':[{'content':{'parts':[{'text':'hello'}]}}],
                 'usageMetadata':{'promptTokenCount':3,'candidatesTokenCount':1}}) as call:
            models,providers=self.providers.catalog()
            gemini=next(model for model in models if model['provider']=='Google compatible API')
            self.assertEqual('api:google:gemini-2.5-flash',gemini['id'])
            self.assertTrue(gemini['available'])
            self.assertNotIn('text-embedding-004',str(models))
            self.assertNotIn('gemini-audio',str(models))
            self.assertTrue(next(row for row in providers if row['id']=='google')['configured'])
            fetch.assert_called_once_with('https://generativelanguage.googleapis.com/v1beta/models',
                                          {'x-goog-api-key':'restored-key-fixture'},provider_label='Gemini')
            result=self.providers.generate('google','gemini-2.5-flash',
                [{'role':'user','content':'hello'}],None,True)
        self.assertEqual('hello',result['text'])
        self.assertEqual('https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent',
                         call.call_args.args[0])

    def test_restored_google_key_is_visible_but_never_used_while_external_access_is_off(self):
        self.providers.credentials['google']='restored-key-fixture'
        self.providers.settings.external_access_enabled=False
        with patch('olcr_api.brain_providers.get_json') as fetch:
            models,providers=self.providers.catalog()
        fetch.assert_not_called()
        self.assertTrue(next(row for row in providers if row['id']=='google')['configured'])
        self.assertFalse(any(model['provider']=='Google compatible API' for model in models))

    def test_provider_catalog_uses_external_access_after_settings_rebuild(self):
        self.providers.credentials['google']='restored-key-fixture'
        self.manager.settings=Settings(external_access_enabled=True)
        with patch('olcr_api.brain_providers.get_json',return_value={'models':[
                {'name':'models/gemini-2.5-flash','supportedGenerationMethods':['generateContent']}]}) as fetch:
            models=self.manager.catalog()['models']
        self.assertIn('api:google:gemini-2.5-flash',[model['id'] for model in models])
        fetch.assert_called_once()

    def test_first_party_google_catalog_repairs_stale_saved_model_ids(self):
        self.providers.credentials['google']='restored-key-fixture'
        self.providers.settings.external_access_enabled=True
        with self.db.connect() as db:
            db.execute('insert into brain_providers values(?,?)',('google',json.dumps({
                'id':'google','label':'Google compatible API','auth_type':'api_key',
                'base_url':'https://generativelanguage.googleapis.com/v1beta',
                'model_ids':['retired-gemini-model']})))
        with patch('olcr_api.brain_providers.get_json',return_value={'models':[
                {'name':'models/gemini-2.5-flash','supportedGenerationMethods':['generateContent']}]}) as fetch:
            models,_=self.providers.catalog()
        ids=[model['id'] for model in models if model['provider']=='Google compatible API']
        self.assertEqual(['api:google:gemini-2.5-flash'],ids)
        fetch.assert_called_once()

    def test_restored_provider_catalog_failure_is_explained_without_losing_credential(self):
        self.providers.credentials['google']='restored-key-fixture'
        self.providers.settings.external_access_enabled=True
        with patch('olcr_api.brain_providers.get_json',side_effect=ModelFailure('provider_rejected','HTTP 403')):
            _,providers=self.providers.catalog()
        google=next(row for row in providers if row['id']=='google')
        self.assertTrue(google['configured'])
        self.assertIn('APIキー',google['availability_message'])
        self.assertEqual('restored-key-fixture',self.providers.credentials['google'])

    def test_restored_chatgpt_oauth_credential_discovers_models_without_prior_provider_row(self):
        credential={'access_token':'restored-access-fixture','refresh_token':'refresh-fixture','id_token':'id-fixture',
            'client_id':'oaiapp_test','account_id':'restored-account-fixture',
            'scope':'chatgpt.tokens.use.direct','expires_at':time.time()+3600}
        with patch.dict('os.environ',{'OLCR_BRAIN_OPENAI_OAUTH':json.dumps(credential)}):
            restored=BrainModels(self.db,self.providers.settings).providers
        with self.db.connect() as db:
            self.assertEqual(0,db.execute('select count(*) from brain_providers').fetchone()[0])
        restored.settings.external_access_enabled=True
        with patch('olcr_api.brain_providers.get_json',return_value={'models':[
                 {'slug':'gpt-restored','visibility':'list','display_name':'GPT Restored'}]}) as fetch:
            models,providers=restored.catalog()
        self.assertTrue(next(row for row in providers if row['id']=='openai_oauth')['configured'])
        self.assertIn('api:openai_oauth:gpt-restored',[model['id'] for model in models])
        fetch.assert_called_once()

class ProviderUsageTelemetryTests(unittest.TestCase):
    def setUp(self):
        from olcr_api.db import Database
        from olcr_api.brain_models import BrainModels
        self.temp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.temp.name)/'usage.db');self.db.initialize()
        self.manager=BrainModels(self.db,Settings());self.providers=self.manager.providers
        self.providers.register('openai','api_key',{'base_url':'http://127.0.0.1:9/v1','api_key':'fixture-only','model_ids':'fixture'})
    def tearDown(self):self.temp.cleanup()
    def usage(self):return self.manager.usage()['models']['api:openai:fixture']
    def test_truncated_response_records_failure_and_provider_reported_usage(self):
        response={'choices':[{'message':{'content':'{"operations":['},'finish_reason':'length'}],
                  'usage':{'prompt_tokens':4,'completion_tokens':4096,'total_tokens':4100}}
        with patch('olcr_api.brain_providers.call_json',return_value=response),self.assertRaises(ModelFailure) as raised:
            self.providers.generate('openai','fixture',[],{'type':'object'},False)
        self.assertEqual('output_limit',raised.exception.category)
        usage=self.usage()
        self.assertEqual((1,1,4,4096,4100),(usage['request_count'],usage['failed_requests'],
            usage['input_tokens'],usage['output_tokens'],usage['total_tokens']))
    def test_missing_usage_is_a_request_and_schema_retries_count_each_attempt(self):
        schema_failure=ModelFailure('provider_rejected','HTTP 400 · invalid_json_schema · response_format')
        schema_failure.http_status=400;schema_failure.provider_code='invalid_json_schema'
        schema_failure.parameter='response_format';schema_failure.schema_format_rejection=True
        with patch('olcr_api.brain_providers.resolve_output_mode',return_value=OutputMode.JSON_SCHEMA),patch('olcr_api.brain_providers.call_json',side_effect=[schema_failure,{'choices':[{'message':{'content':'{}'}}],'usage':None}]) as call:
            result=self.providers.generate('openai','fixture',[{'role':'user','content':'hello'}],{'type':'object','properties':{},'additionalProperties':False},False)
            with patch('olcr_api.brain_providers.call_json',return_value={'choices':[{'message':{'content':'{}'}}],'usage':None}) as subsequent:
                self.providers.generate('openai','fixture',[{'role':'user','content':'hello'}],{'type':'object','properties':{},'additionalProperties':False},False)
        self.assertTrue(result['usage_recorded']);self.assertIsNone(result['prompt_tokens']);self.assertEqual(2,call.call_count)
        # Once prompted-JSON compatibility succeeds, the same model/account/schema
        # must not repeat the known-rejected structured-output request.
        self.assertNotIn('response_format',subsequent.call_args.args[1])
        self.assertEqual(1,subsequent.call_count)
        usage=self.usage();self.assertEqual((3,1,3,0),(usage['request_count'],usage['failed_requests'],usage['unreported_requests'],usage['total_tokens']))

    def test_schema_format_fallback_is_reused_for_new_contracts_on_the_same_model_account(self):
        failure=ModelFailure('provider_rejected','OpenAI request failed (HTTP 400) · invalid_request_error · response_format')
        failure.http_status=400;failure.provider_code='invalid_request_error'
        failure.parameter='response_format';failure.schema_format_rejection=True
        schema_one={'type':'object','properties':{'first':{'type':'string'}},
                    'required':['first'],'additionalProperties':False}
        schema_two={'type':'object','properties':{'second':{'type':'boolean'}},
                    'required':['second'],'additionalProperties':False}
        response={'choices':[{'message':{'content':'{}'}}]}
        with patch('olcr_api.brain_providers.resolve_output_mode',return_value=OutputMode.JSON_SCHEMA), \
             patch('olcr_api.brain_providers.call_json',side_effect=[failure,response,response]) as call:
            self.providers.generate('openai','fixture',[{'role':'user','content':'first'}],schema_one,False)
            self.providers.generate('openai','fixture',[{'role':'user','content':'second'}],schema_two,False)

        self.assertEqual(3,call.call_count)
        self.assertIn('response_format',call.call_args_list[0].args[1])
        self.assertNotIn('response_format',call.call_args_list[2].args[1])

        self.providers.register('openai','api_key',{'base_url':'http://127.0.0.1:10/v1',
            'api_key':'fixture-only','model_ids':'fixture'})
        with patch('olcr_api.brain_providers.resolve_output_mode',return_value=OutputMode.JSON_SCHEMA), \
             patch('olcr_api.brain_providers.call_json',return_value=response) as changed_endpoint:
            self.providers.generate('openai','fixture',[{'role':'user','content':'third'}],schema_two,False)
        self.assertIn('response_format',changed_endpoint.call_args.args[1])
    def test_provider_failure_counts_but_denied_external_access_does_not(self):
        with patch('olcr_api.brain_providers.call_json',side_effect=ModelFailure('provider_unavailable','offline')):
            with self.assertRaises(ModelFailure):self.providers.generate('openai','fixture',[],None,False)
        self.assertEqual(1,self.usage()['failed_requests'])
        self.providers.register('openai','api_key',{'base_url':'https://example.invalid/v1','api_key':'fixture-only','model_ids':'fixture'})
        with patch('olcr_api.brain_providers.call_json') as call:
            with self.assertRaises(ModelFailure):self.providers.generate('openai','fixture',[],None,False)
            call.assert_not_called()
        self.assertEqual(1,self.usage()['request_count'])
    def test_token_normalization_keeps_provider_totals_cache_and_reasoning(self):
        from olcr_api.brain_providers import reported_tokens
        self.assertEqual((5,3,8),reported_tokens('openai',{'usage':{'prompt_tokens':5,'completion_tokens':3,'total_tokens':8,'completion_tokens_details':{'reasoning_tokens':2}}}))
        self.assertEqual((5,7,12),reported_tokens('google',{'usageMetadata':{'promptTokenCount':4,'toolUsePromptTokenCount':1,'candidatesTokenCount':2,'thoughtsTokenCount':5,'totalTokenCount':12}}))
        self.assertEqual((15,2,17),reported_tokens('anthropic',{'usage':{'input_tokens':3,'cache_read_input_tokens':10,'cache_creation_input_tokens':2,'output_tokens':2}}))
        self.assertEqual((None,None,9),reported_tokens('openai',{'usage':{'total_tokens':9}}))
        self.assertEqual((None,None,None),reported_tokens('openai',{'usage':None}))
        self.assertEqual((None,None,None),reported_tokens('openai',{'usage':{'prompt_tokens':True,'completion_tokens':-1}}))
