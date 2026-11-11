import copy
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from olcr_api.brain_models import BrainModels, INITIAL, BONSAI, ORNITH, ORNITH_MLX, brain_scope
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.model_provider import RoleAwareModelProvider
from olcr_api.ollama import ModelFailure

class BrainModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'brain.db'); self.db.initialize()
        self.models = BrainModels(self.db, Settings())
        self.provider = RoleAwareModelProvider(Settings())
        self.provider.brain_models = self.models
        self.available = [{'id': x, 'label': x, 'available': True} for x in (ORNITH, ORNITH_MLX, BONSAI, 'ollama:new-brain')]
        self.catalog = patch.object(self.models, 'catalog', return_value={'models': self.available, 'providers': []})
        self.catalog.start()
        # These route/provider tests use mocked installed-model inventories.
        actual_preflight=self.models.preflight
        self.preflight_fixture=patch.object(self.models,'preflight',side_effect=lambda model,external_enabled=None: {'state':'available','reason':'fixture_installed','retry_at':None} if model and model.startswith(('ollama:','mlx:')) else actual_preflight(model,external_enabled))
        self.preflight_fixture.start()
        self.addCleanup(self.preflight_fixture.stop)
    def tearDown(self):
        self.catalog.stop(); self.provider.close(); self.tmp.cleanup()

    def test_thinking_mode_is_scoped_and_legacy_saves_preserve_it(self):
        value = copy.deepcopy(INITIAL)
        value['efforts']['coder'] = 'on'
        self.models.save(value, 'one')
        self.assertIsNone(self.models.configuration('two')['efforts']['coder'])
        value.pop('efforts')
        self.assertEqual('on', self.models.save(value, 'one')['efforts']['coder'])
        for invalid in (True, 'invalid'):
            with self.assertRaises(ValueError):
                self.models.save({**value, 'efforts': {**INITIAL['efforts'], 'coder': invalid}}, 'one')
            self.assertEqual('on', self.models.configuration('one')['efforts']['coder'])

    def test_local_thinking_modes_reach_both_workers_including_json(self):
        for model in (ORNITH_MLX, BONSAI, ORNITH):
            for mode, expected in (('auto', False), ('on', True), ('off', False)):
                configuration = copy.deepcopy(INITIAL)
                configuration['selections']['general_inference'] = model
                configuration['efforts']['general_inference'] = mode
                with brain_scope(configuration), \
                     patch.object(self.provider, '_run_mlx', return_value={'text': 'answer'}) as mlx, \
                     patch.object(self.provider.ollama, 'generate', return_value={'text': 'answer'}) as ollama:
                    self.provider.generate([{'role': 'user', 'content': 'hello'}], 'role:normal_chat')
                    actual = ollama.call_args.kwargs['think'] if model == ORNITH else mlx.call_args.args[1]['think']
                    self.assertIs(expected, actual, (model, mode))
                    self.provider.generate([], 'role:normal_chat', format={'type': 'object'}, think=True)
                    actual = ollama.call_args.kwargs['think'] if model == ORNITH else mlx.call_args.args[1]['think']
                    self.assertEqual(mode!='off',actual)
                    self.assertEqual(2, ollama.call_count if model == ORNITH else mlx.call_count)

    def test_stream_uses_the_mode_snapshot_after_scope_exits(self):
        value = copy.deepcopy(INITIAL)
        value['selections']['general_inference'] = ORNITH
        value['efforts']['general_inference'] = 'on'
        with brain_scope(value), patch.object(self.provider.ollama, 'generate', return_value=iter([{'text':'answer'}])) as call:
            stream = self.provider.generate([], 'role:normal_chat', stream=True)
            self.assertTrue(call.call_args.kwargs['think'])
        value['efforts']['general_inference'] = 'off'
        self.assertEqual('on', list(stream)[0]['thinking_decision']['mode'])

    def test_local_selection_is_not_blocked_by_stale_provider_selections_or_fallbacks(self):
        offline, removed = 'api:openai:offline', 'api:google:removed'
        self.available.extend([{'id': offline, 'available': True}, {'id': removed, 'available': True}])
        original = copy.deepcopy(INITIAL)
        original['selections']['short_coding'] = removed
        original['fallback_models'] = [offline, removed, BONSAI]
        original = self.models.save(original, 'one')
        self.available[-2]['available'] = False
        self.available.pop()
        for model in (ORNITH, ORNITH_MLX, BONSAI):
            with self.subTest(local_model=model):
                next_config = copy.deepcopy(original)
                next_config['selections']['coder'] = model
                saved = self.models.save(next_config, 'one')
                self.assertEqual(model, saved['selections']['coder'])
                self.assertEqual(removed, saved['selections']['short_coding'])
                self.assertEqual(original['fallback_models'], saved['fallback_models'])
                self.assertEqual(INITIAL, self.models.configuration('other'))
                with brain_scope(saved), \
                     patch.object(self.provider.ollama, 'generate', return_value={'text': 'local answer'}), \
                     patch.object(self.provider, '_run_mlx', return_value={'text': 'local answer'}), \
                     patch.object(self.models.providers, 'generate', side_effect=AssertionError('must use local model')):
                    self.assertEqual('local answer', self.provider.generate([], 'role:normal_chat')['text'])

    def test_new_unavailable_fallbacks_are_rejected_without_changing_configuration(self):
        self.available.append({'id': 'api:openai:offline', 'available': False})
        for candidates in [['api:openai:offline'], ['api:google:missing'], [ORNITH, ORNITH], [ORNITH] * 17, [{}]]:
            with self.subTest(candidates=candidates):
                value = copy.deepcopy(INITIAL)
                value['fallback_models'] = candidates
                with self.assertRaisesRegex(ValueError, 'fallback sequence'):
                    self.models.save(value, 'one')
                self.assertEqual(INITIAL, self.models.configuration('one'))

    def test_unavailable_existing_fallback_can_be_removed(self):
        value = copy.deepcopy(INITIAL)
        value['fallback_models'] = [ORNITH, BONSAI]
        self.models.save(value)
        self.available[0]['available'] = False
        value['fallback_models'] = [BONSAI]
        self.assertEqual([BONSAI], self.models.save(value)['fallback_models'])

    def test_unchanged_effort_for_a_disconnected_model_does_not_block_local_switch(self):
        remote = {'id': 'api:openai:remote', 'available': True, 'effort_levels': ['high']}
        self.available.append(remote)
        value = copy.deepcopy(INITIAL)
        value['selections']['general_inference'] = remote['id']
        value['efforts']['general_inference'] = 'high'
        value = self.models.save(value)
        self.available.pop()
        value['selections']['coder'] = ORNITH
        self.assertEqual('high', self.models.save(value)['efforts']['general_inference'])
        value['efforts']['general_inference'] = 'invalid-new-effort'
        with self.assertRaisesRegex(ValueError, 'effort'):
            self.models.save(value)
    def test_configuration_initial_and_conversation_isolation(self):
        value = copy.deepcopy(INITIAL); value['selections']['coder'] = BONSAI
        self.models.save(value, 'one')
        self.assertEqual(value, self.models.configuration('one'))
        self.assertEqual(INITIAL, self.models.configuration('two'))
        restarted = BrainModels(self.db, Settings())
        self.assertEqual(value, restarted.configuration('one'))
        self.assertNotIn('brain_configurations', self.db.load_application_settings())
        with self.assertRaises(ValueError):
            self.models.save({'mode':'split', 'selections':dict(INITIAL['selections'], coder='vision:model')})
    def test_unavailable_selection_preserves_previous_configuration(self):
        value=copy.deepcopy(INITIAL);value['selections']['coder']='ollama:new-brain'
        self.available[-1]['available']=False
        with self.assertRaises(ValueError):self.models.save(value,'one')
        self.assertEqual(INITIAL,self.models.configuration('one'))

    def test_model_change_uses_supported_default_effort_and_rejects_unsupported_effort(self):
        remote={'id':'api:google:gemini-3.7-flash','label':'Gemini Flash','available':True,
                'effort_levels':['low','medium','high'],'effort_default':'medium'}
        self.available.append(remote)
        value=copy.deepcopy(INITIAL);value['selections']['coder']=remote['id']
        saved=self.models.save(value,'one')
        self.assertEqual('medium',saved['efforts']['coder'])
        with brain_scope(saved):
            self.assertEqual('medium',self.models.effort_for_role('long_implementation'))
        value['efforts']['coder']='xhigh'
        with self.assertRaisesRegex(ValueError,'effort is not supported'):
            self.models.save(value,'one')

    def test_normal_chat_follows_inference_and_never_the_coder_selection(self):
        from olcr_api.thinking import configured_thinking_mode
        value=copy.deepcopy(INITIAL)
        value['selections']['coder']=ORNITH
        value['selections']['general_inference']=BONSAI
        value['efforts']['coder']='on'
        value['efforts']['general_inference']='off'
        with brain_scope(value),patch.object(self.provider.ollama,'generate',return_value={'text':'code'}) as coder,patch.object(self.provider,'_run_mlx',return_value={'text':'chat'}) as inference:
            chat=self.provider.generate([{'role':'user','content':'おすすめの動画を教えて'}],'role:normal_chat')
            self.assertEqual('normal_chat',chat['model_role'])
            inference.assert_called_once();coder.assert_not_called()
            self.assertEqual('off',configured_thinking_mode(value,'normal_chat'))
            self.provider.generate([],'role:long_implementation')
            coder.assert_called_once()

    def test_swapping_providers_preserves_route_and_schema(self):
        value = copy.deepcopy(INITIAL)
        value['selections']['short_coding'] = ORNITH
        value['selections']['coder'] = BONSAI
        schema = {'type':'object', 'properties':{'ok':{'type':'boolean'}}}
        with brain_scope(value), patch.object(self.provider.ollama, 'generate', return_value={'text':'{"ok":true}'}) as ollama, patch.object(self.provider, '_run_mlx', return_value={'text':'test'}) as mlx:
            short = self.provider.generate([], 'role:short_coding_fix_debug', format=schema, think=False)
            coder = self.provider.generate([], 'role:long_implementation')
            self.assertEqual('short_coding_fix_debug', short['model_role'])
            self.assertEqual('ornith-1.5:9b', short['model'])
            self.assertEqual(schema, ollama.call_args.kwargs['format'])
            self.assertEqual('long_implementation', coder['model_role'])
            self.assertEqual('ternary_bonsai_2_27b', coder['model_provenance']['model_id'])
            mlx.assert_called_once()
    def test_unified_configuration_does_not_change_auxiliary_roles(self):
        value = copy.deepcopy(INITIAL); value['mode']='unified'; value['selections']['brain']=BONSAI
        registry = self.provider.registry
        with brain_scope(value):
            for role in ('long_implementation', 'normal_chat', 'planner_replanner', 'short_coding_fix_debug'):
                result = self.models.resolve(registry.binding(role), registry)
                self.assertEqual(role, result.role); self.assertEqual('ternary_bonsai_2_27b',result.model_id)
            for role in ('gui_grounding','general_image','semantic_embedding','typed_local_decisions'):
                binding = registry.binding(role)
                self.assertEqual(binding,self.models.resolve(binding,registry))
    def test_concurrent_configuration_contexts_and_missing_model_fail_closed(self):
        first=copy.deepcopy(INITIAL); first['selections']['general_inference']=BONSAI
        second=copy.deepcopy(INITIAL); second['selections']['general_inference']='ollama:new-brain'
        def resolved(value):
            with brain_scope(value):
                return self.models.resolve(self.provider.registry.binding('normal_chat'), self.provider.registry).deployment_model
        with ThreadPoolExecutor(2) as pool:
            values=list(pool.map(resolved,[first,second]*5))
        self.assertEqual([self.provider.registry.binding('planner_replanner').deployment_model, 'new-brain']*5,values)
        second['selections']['general_inference']='ollama:deleted'
        with self.assertRaises(ModelFailure): resolved(second)
    def test_catalog_filters_auxiliary_capabilities_and_accepts_text_model(self):
        self.catalog.stop()
        class Response:
            def __init__(self,value): self.value=value
            def __enter__(self): return self
            def __exit__(self,*a): pass
            def read(self): return json.dumps(self.value).encode()
        def fetch(req, **kwargs):
            if isinstance(req,str):return Response({'models':[{'name':x} for x in ['ornith-1.5:9b','embeddinggemma','venus','vision-only','text-model','audio']]})
            name=json.loads(req.data)['model']
            return Response({'capabilities':{'ornith-1.5:9b':['completion','vision'],'embeddinggemma':['embedding'],'venus':['completion','vision'],'vision-only':['vision'],'text-model':['completion'],'audio':['audio','completion']}[name]})
        with patch('olcr_api.brain_models.request.urlopen',side_effect=fetch), \
             patch('olcr_api.model_profiles._data',return_value={'models':{}}):
            ids={m['id'] for m in self.models.catalog()['models']}
        # A model advertising completion remains a Brain even with image input.
        self.assertEqual({ORNITH,ORNITH_MLX,BONSAI,'ollama:text-model','ollama:venus'},ids)

    def test_purpose_inventory_exposes_manifest_roles_separately_from_brain_candidates(self):
        purpose_models = self.models._purpose_models()
        by_role = {model['role']: model for model in purpose_models}
        self.assertEqual('EmbeddingGemma', by_role['semantic_embedding']['label'])
        self.assertEqual('FastVLM · light / fast', by_role['general_image']['label'])
        self.assertEqual('UI-Venus · heavy / UI', by_role['gui_grounding']['label'])
        self.assertEqual('mlx-community/FastVLM-0.5B-bf16', by_role['general_image']['deployment'])
        self.assertEqual('mlx-community/UI-Venus-1.5-2B-6bit', by_role['gui_grounding']['deployment'])

    def test_usage_is_per_model_weekly_resettable_and_does_not_invent_quota_or_cost(self):
        now=time.time()
        self.models.record_usage('old-task',ORNITH_MLX,500,200,now-8*24*60*60)
        self.models.record_usage('new-task',ORNITH_MLX,11,7,now,provider_rate_limits={
            'x-ratelimit-remaining-tokens':'1200','authorization':'must-not-persist'})
        self.models.record_usage('other-model',BONSAI,3,2,now)
        usage=self.models.usage()['models']
        self.assertEqual((11,7,18),(usage[ORNITH_MLX]['input_tokens'],usage[ORNITH_MLX]['output_tokens'],usage[ORNITH_MLX]['total_tokens']))
        self.assertIsNone(usage[ORNITH_MLX]['limit_tokens'])
        self.assertIsNone(usage[ORNITH_MLX]['cost_usd'])
        self.assertEqual({'x-ratelimit-remaining-tokens':'1200'},usage[ORNITH_MLX]['provider_rate_limits'])
        self.assertIsNotNone(usage[ORNITH_MLX]['rate_limits_observed_at'])
        self.models.record_usage('rate-only', 'api:google:gemini-3.5-flash', None, None,
                                 provider_rate_limits={'x-ratelimit-remaining-requests':'3'})
        rate_only=self.models.usage()['models']['api:google:gemini-3.5-flash']
        self.assertEqual(0,rate_only['total_tokens'])
        self.assertEqual({'x-ratelimit-remaining-requests':'3'},rate_only['provider_rate_limits'])
        with self.assertRaises(ValueError):self.models.reset_usage('not-a-model')
        self.models.reset_usage(ORNITH_MLX)
        self.assertEqual(0,self.models.usage()['models'][ORNITH_MLX]['total_tokens'])
        with self.db.connect() as con:
            con.execute('INSERT INTO brain_usage_events(task_id,model_id,input_tokens,output_tokens,created_at) VALUES(?,?,?,?,?)',
                        ('after-reset',ORNITH_MLX,4,2,time.time()+1))
        self.assertEqual(6,self.models.usage()['models'][ORNITH_MLX]['total_tokens'])

    def test_selected_model_for_role_uses_captured_conversation_configuration(self):
        selected=copy.deepcopy(INITIAL);selected['selections']['coder']=BONSAI
        self.assertEqual(BONSAI,self.models.selected_model_for_role('normal_chat',selected))
        self.assertEqual(BONSAI,self.models.selected_model_for_role('long_implementation',selected))
        self.assertIsNone(self.models.selected_model_for_role('semantic_embedding',selected))

class BrainUsageTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.temp.name)/'usage.db');self.db.initialize()
        self.models=BrainModels(self.db,Settings())
    def tearDown(self):self.temp.cleanup()
    def test_missing_partial_zero_and_total_only_usage_are_distinct_and_idempotent(self):
        m='api:openai:fixture'
        for task,inp,out,total in [('unknown',None,None,None),('partial',3,None,None),('zero',0,0,None),('total',None,None,17)]:
            self.models.record_usage(task,m,inp,out,total_tokens=total)
            self.models.record_usage(task,m,inp,out,total_tokens=total)
        usage=self.models.usage()['models'][m]
        self.assertEqual(4,usage['request_count']);self.assertEqual(2,usage['token_reported_requests'])
        self.assertEqual(2,usage['unreported_requests']);self.assertEqual(1,usage['total_only_requests'])
        self.assertEqual(17,usage['total_tokens'])
    def test_request_start_completion_error_reset_and_restart_preserve_measurements(self):
        m='api:openai:fixture'
        with patch('olcr_api.brain_models.time.time',return_value=time.time()+10):
            self.models.record_api_usage('one',m,'pending')
            self.models.record_api_usage('one',m,'success',4,3,7)
            self.models.record_api_usage('two',m,'error')
        usage=BrainModels(self.db,Settings()).usage()['models'][m]
        self.assertEqual((2,1,1,7),(usage['request_count'],usage['failed_requests'],usage['unreported_requests'],usage['total_tokens']))
        self.models.record_usage('upper-level',m,4,3,usage_recorded=True)
        self.assertEqual(2,self.models.usage()['models'][m]['request_count'])
        with patch.object(self.models,'catalog',return_value={'models':[{'id':m}]}),patch('olcr_api.brain_models.time.time',return_value=time.time()+20):
            self.models.reset_usage(m)
        self.assertEqual(0,self.models.usage()['models'][m]['request_count'])
    def test_old_schema_migrates_without_losing_reported_usage(self):
        with self.db.connect() as con:
            con.execute('DROP TABLE brain_usage_events')
            con.execute('CREATE TABLE brain_usage_events(id INTEGER PRIMARY KEY,task_id TEXT NOT NULL,model_id TEXT NOT NULL,input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,created_at REAL NOT NULL,call_index INTEGER NOT NULL DEFAULT 0,UNIQUE(task_id,model_id,call_index))')
            con.execute('INSERT INTO brain_usage_events(task_id,model_id,input_tokens,output_tokens,created_at) VALUES(?,?,?,?,?)',('legacy','api:openai:fixture',4,5,time.time()))
        restored=BrainModels(self.db,Settings()).usage()['models']['api:openai:fixture']
        self.assertEqual((9,1,1),(restored['total_tokens'],restored['request_count'],restored['token_reported_requests']))
