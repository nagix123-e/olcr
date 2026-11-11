"""Every Brain role starts at its selection and follows the shared ordered chain."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from olcr_api.brain_models import BrainModels, INITIAL, ROLE_SLOTS, brain_scope
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.model_provider import RoleAwareModelProvider
from olcr_api.ollama import ModelFailure


class BrainFallbackOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = Database(Path(self.tmp.name) / 'test.db')
        db.initialize()
        self.models = BrainModels(db, Settings())
        self.provider = RoleAwareModelProvider(Settings())
        self.provider.brain_models = self.models
        self.config = copy.deepcopy(INITIAL)
        self.config['selections'] = {slot: 'ollama:' + slot for slot in INITIAL['selections']}
        self.config['fallback_models'] = ['ollama:first', 'ollama:second']
        ids = [*self.config['selections'].values(), *self.config['fallback_models']]
        self.catalog = patch.object(self.models, 'catalog', return_value={
            'models': [{'id': item, 'available': True} for item in ids]})
        self.catalog.start()
        # These route/provider tests use mocked installed-model inventories.
        actual_preflight=self.models.preflight
        self.preflight_fixture=patch.object(self.models,'preflight',side_effect=lambda model,external_enabled=None: {'state':'available','reason':'fixture_installed','retry_at':None} if model and model.startswith(('ollama:','mlx:')) else actual_preflight(model,external_enabled))
        self.preflight_fixture.start()
        self.addCleanup(self.preflight_fixture.stop)

    def tearDown(self):
        self.catalog.stop()
        self.provider.close()
        self.tmp.cleanup()

    def test_user_cancellation_never_starts_a_fallback_model(self):
        from olcr_api.generation_control import Generation, GenerationCancelled, generation_scope
        generation = Generation()
        def cancelled(*args, **kwargs):
            generation.cancel()
            raise ModelFailure('unavailable', 'interrupted connection')
        with brain_scope(self.config), generation_scope(generation), \
             patch.object(self.provider.ollama, 'generate', side_effect=cancelled) as call:
            with self.assertRaises(GenerationCancelled):
                self.provider.generate([{'role':'user','content':'Hello'}], 'role:normal_chat')
        self.assertEqual(1, call.call_count)

    def test_each_fallback_uses_its_own_context_allowance(self):
        self.provider.settings.model_context_budgets = {'ollama:general_inference': 5000, 'ollama:first': 1024}
        packet = {'items': [{'value': n, 'text': 'observation ' * 200} for n in range(30)]}
        messages = [{'role': 'user', 'content': 'Compare observations\n[TOOL_RESULT_UNTRUSTED]\n' + json.dumps(packet)}]
        with brain_scope(self.config), patch.object(self.provider.ollama, 'generate', side_effect=[
                ModelFailure('quota', 'fixture'), {'text': 'Recovered'}]) as call:
            self.provider.generate(messages, 'role:normal_chat')
        self.assertEqual(2, call.call_count)
        for invocation, limit in zip(call.call_args_list, (5000, 1024)):
            self.assertEqual('Compare observations', next(item['content'] for item in invocation.args[0] if item['role'] == 'user'))
            context = next(item['content'].split('\n', 1)[1] for item in invocation.args[0]
                           if item['content'].startswith('[PROVIDER_EVIDENCE_JSON]'))
            self.assertLessEqual(len(context), limit)
            self.assertTrue(json.loads(context)['evidence_truncated'])

    def test_all_roles_start_at_current_selection_in_split_and_unified_modes(self):
        for mode in ('split', 'unified'):
            for role, slot in ROLE_SLOTS.items():
                with self.subTest(mode=mode, role=role):
                    config = copy.deepcopy(self.config)
                    config['mode'] = mode
                    start = 'brain' if mode == 'unified' else slot
                    # Include the current model and another role's model in the
                    # stored order: only this request's starting model is skipped.
                    config['fallback_models'] = ['ollama:first', 'ollama:' + start,
                                                'ollama:short_coding', 'ollama:second']
                    calls = []
                    def respond(messages, model, **kwargs):
                        calls.append(model)
                        if model != 'second':
                            raise ModelFailure('unavailable', 'fixture')
                        return {'text': 'answer', 'model': model}
                    with brain_scope(config), patch.object(self.provider.ollama, 'generate', side_effect=respond):
                        result = self.provider.generate([], 'role:' + role)
                    expected = list(dict.fromkeys([start, 'first', 'short_coding', 'second']))
                    self.assertEqual(expected, calls)
                    self.assertEqual('ollama:' + start, result['fallback_from'])
                    self.assertEqual('ollama:second', result['fallback_model_id'])

    def test_successful_primary_never_calls_fallback(self):
        with brain_scope(self.config), patch.object(self.provider.ollama, 'generate', return_value={'text': 'answer'}) as call:
            result = self.provider.generate([], 'role:planner_replanner')
        self.assertEqual('general_inference', call.call_args.args[1])
        self.assertEqual(1, call.call_count)
        self.assertNotIn('fallback_from', result)

    def test_response_failures_exhaust_candidates_in_order(self):
        failures = [ModelFailure('quota', 'fixture'), TimeoutError(), {'text': ''},
                    {'text': 'wrong model', 'model': 'unexpected'}, None]
        for failure in failures:
            for stream in (False, True):
                with self.subTest(failure=failure, stream=stream):
                    def respond(messages, model, **kwargs):
                        if model == 'second':
                            return {'text': 'answer', 'model': model}
                        if isinstance(failure, Exception):
                            raise failure
                        return copy.deepcopy(failure)
                    with brain_scope(self.config), patch.object(self.provider.ollama, 'generate', side_effect=respond) as call:
                        result = self.provider.generate([], 'role:short_coding_fix_debug', stream=stream)
                        if stream:
                            result = list(result)[0]
                    self.assertEqual(['short_coding', 'first', 'second'], [c.args[1] for c in call.call_args_list])
                    self.assertEqual('answer', result['text'])

    def test_partial_stream_failure_discards_failed_text_and_keeps_order(self):
        def respond(messages, model, **kwargs):
            def frames():
                yield {'text': model, 'model': model, 'reasoning': {'text': model+' work', 'kind':'trace', 'complete':True, 'truncated':False}}
                if model != 'second':
                    raise ModelFailure('unavailable', 'interrupted')
            return frames()
        with brain_scope(self.config), patch.object(self.provider.ollama, 'generate', side_effect=respond) as call:
            frames = list(self.provider.generate([], 'role:long_implementation', stream=True))
        self.assertEqual(['coder', 'first', 'second'], [c.args[1] for c in call.call_args_list])
        self.assertEqual(['second'], [f['text'] for f in frames])
        self.assertEqual(['second work'], [f['reasoning']['text'] for f in frames])
        self.assertEqual('ollama:coder', frames[0]['fallback_from'])

    def test_all_failed_returns_error_only_after_last_candidate(self):
        with brain_scope(self.config), patch.object(self.provider.ollama, 'generate', side_effect=ModelFailure('quota', 'fixture')) as call:
            with self.assertRaisesRegex(ModelFailure, 'All configured fallback'):
                self.provider.generate([], 'role:normal_chat')
        self.assertEqual(['general_inference', 'first', 'second'], [c.args[1] for c in call.call_args_list])

    def test_fallback_log_reports_the_immediately_previous_failure_and_model(self):
        from olcr_api.model_progress import observe_model_progress
        events=[]
        def respond(messages, model, **kwargs):
            if model=='general_inference':
                raise ModelFailure('credential_expired','primary authentication failed')
            if model=='first':
                raise ModelFailure('provider_unavailable','first fallback unavailable')
            return {'text':'answer','model':model}
        with observe_model_progress(events.append), brain_scope(self.config), \
             patch.object(self.provider.ollama,'generate',side_effect=respond):
            result=self.provider.generate([], 'role:normal_chat')
        fallbacks=[event for event in events if event.get('state')=='fallback']
        self.assertEqual('answer',result['text'])
        self.assertEqual([('ollama:general_inference','credential_expired'),('ollama:first','provider_unavailable')],
            [(event.get('previous_model'),event.get('reason')) for event in fallbacks])

    def test_other_roles_selected_models_can_be_saved_as_fallbacks(self):
        self.config['fallback_models'] = ['ollama:short_coding', 'ollama:general_inference']
        saved = self.models.save(self.config)
        self.assertEqual(self.config['fallback_models'], saved['fallback_models'])

    def test_legacy_custom_chat_starts_at_explicit_model_then_shared_chain(self):
        def respond(messages, model, **kwargs):
            if model != 'second':
                raise ModelFailure('unavailable', 'fixture')
            return {'text': 'answer'}
        with brain_scope(self.config), patch.object(self.provider.ollama, 'generate', side_effect=respond) as call:
            result = self.provider.generate([], 'custom-chat')
        self.assertEqual(['custom-chat', 'first', 'second'], [c.args[1] for c in call.call_args_list])
        self.assertEqual('ollama:custom-chat', result['fallback_from'])

    def test_unavailable_selected_mlx_uses_order_without_calling_another_primary(self):
        from olcr_api.brain_models import ORNITH_MLX
        config = copy.deepcopy(self.config)
        config['selections']['general_inference'] = ORNITH_MLX
        with brain_scope(config), patch.object(self.provider.ollama, 'generate', return_value={'text': 'answer'}) as call:
            result = self.provider.generate([], 'role:planner_replanner')
        self.assertEqual(['first'], [c.args[1] for c in call.call_args_list])
        self.assertEqual(ORNITH_MLX, result['fallback_from'])

    def test_remote_selected_model_is_called_before_local_fallback(self):
        config = copy.deepcopy(self.config)
        config['selections']['coder'] = 'api:google:selected-remote'
        with brain_scope(config), \
             patch.object(self.models.providers, 'metadata', return_value={'model_ids': ['selected-remote'], 'base_url': 'https://fixture'}), \
             patch.object(self.models.providers, 'configured', return_value=True), \
             patch.object(self.models.providers, 'generate', side_effect=ModelFailure('quota', 'fixture')) as remote, \
             patch.object(self.provider.ollama, 'generate', return_value={'text': 'answer'}) as local:
            result = self.provider.generate([], 'role:long_implementation')
        self.assertEqual(('google', 'selected-remote'), remote.call_args.args[:2])
        self.assertEqual('first', local.call_args.args[1])
        self.assertEqual('api:google:selected-remote', result['fallback_from'])
