import copy
import unittest
from unittest.mock import patch

from olcr_api.config import Settings
from olcr_api.context_extension import compress_messages, estimated_tokens
from olcr_api.model_provider import RoleAwareModelProvider
from olcr_api.ollama import ModelFailure


class ContextExtensionTests(unittest.TestCase):
    def test_settings_roundtrip_and_bounds(self):
        value = Settings().with_overrides({'yarn_enabled': True, 'yarn_target_tokens': 1048576,
                                           'context_auto_compress': True, 'local_context_tokens': 65536})
        self.assertEqual(value, Settings().with_overrides(value.public_dict()))
        for patch_value in ({'yarn_target_tokens': 0}, {'local_context_tokens': True},
                            {'yarn_enabled': 'false'}, {'yarn_enabled': True, 'yarn_target_tokens': 2048}):
            with self.assertRaises(ValueError):
                Settings().with_overrides(patch_value)

    def test_short_messages_remain_identical(self):
        messages = [{'role': 'user', 'content': '続けてください'}]
        result, report = compress_messages(messages, 32768)
        self.assertIs(result, messages)
        self.assertFalse(report['compressed'])
        self.assertGreater(estimated_tokens('日本語'), estimated_tokens('abc'))

    def test_compression_preserves_controls_current_input_and_originals(self):
        messages = [{'role': 'system', 'content': 'Required exact JSON schema'},
                    {'role': 'user', 'content': 'Old question ' * 3000},
                    {'role': 'assistant', 'content': 'Earlier data ' * 3000},
                    {'role': 'system', 'content': 'Required execution contract'},
                    {'role': 'user', 'content': 'Current exact request'}]
        original = copy.deepcopy(messages)
        result, report = compress_messages(messages, 4096, 1024)
        self.assertTrue(report['compressed'])
        self.assertLess(report['estimated_tokens_after'], report['estimated_tokens_before'])
        self.assertEqual(messages, original)
        for index in (0, 3, 4):
            self.assertEqual(result[index], messages[index])
        self.assertEqual([m['role'] for m in messages], [m['role'] for m in result])
        self.assertIn('omitted', result[2]['content'])

    def test_ollama_rejects_unsupported_yarn_honestly(self):
        provider = RoleAwareModelProvider(Settings(yarn_enabled=True))
        with patch.object(provider.ollama, 'generate') as generate:
            with self.assertRaises(ModelFailure) as error:
                provider.generate([{'role': 'user', 'content': 'hello'}], 'custom-test')
            self.assertEqual('yarn_unsupported', error.exception.category)
            generate.assert_not_called()

    def test_ollama_compression_window_sent_only_when_enabled(self):
        provider = RoleAwareModelProvider(Settings(context_auto_compress=True, local_context_tokens=4096))
        with patch.object(provider.ollama, 'generate', return_value={'text': 'ok', 'model': 'custom-test'}) as generate:
            result = provider.generate([{'role': 'user', 'content': 'hello'}], 'custom-test')
        self.assertEqual({'num_ctx': 4096, 'num_predict': 2048}, generate.call_args.kwargs['options'])
        self.assertFalse(result['context_compression']['compressed'])
        provider.settings.context_auto_compress = False
        with patch.object(provider.ollama, 'generate', return_value={'text': 'ok', 'model': 'custom-test'}) as generate:
            result = provider.generate([{'role': 'user', 'content': 'hello'}], 'custom-test')
        self.assertNotIn('options', generate.call_args.kwargs)
        self.assertNotIn('context_compression', result)

    def test_json_evidence_stays_valid_and_tool_sequences_stay_intact(self):
        import json
        evidence = {'items': [{'value': x, 'text': 'evidence ' * 500} for x in range(30)]}
        messages = [{'role': 'system', 'content': '[PROVIDER_EVIDENCE_JSON]\n' + json.dumps(evidence)},
                    {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'abc'}]},
                    {'role': 'tool', 'content': 'result', 'tool_call_id': 'abc'},
                    {'role': 'user', 'content': 'answer'}]
        result, report = compress_messages(messages, 4096)
        self.assertTrue(report['compressed'])
        self.assertTrue(json.loads(result[0]['content'].split('\n', 1)[1])['evidence_truncated'])
        self.assertEqual(messages[1:], result[1:])

    def test_exact_worker_limit_never_silently_truncates_the_current_prompt(self):
        from olcr_api.mlx_worker import _check_context_length
        context = {'context_auto_compress': True, 'local_context_tokens': 4096}
        self.assertEqual(2048, _check_context_length(2048, 2048, context))
        self.assertEqual(2047, _check_context_length(2049, 2048, context))
        with self.assertRaisesRegex(ValueError, 'CONTEXT_WINDOW_EXCEEDED'):
            _check_context_length(4096, 2048, context)
        self.assertEqual(2048, _check_context_length(9000, 2048, None))
        self.assertEqual(2048, _check_context_length(9000, 2048, {'yarn_enabled': True, 'yarn_target_tokens': 16384}))
        minimum = {'context_auto_compress': True, 'local_context_tokens': 2048}
        self.assertEqual(1948, _check_context_length(100, 2048, minimum))
        self.assertEqual(1, _check_context_length(2047, 2048, minimum))
        with self.assertRaisesRegex(ValueError, 'CONTEXT_WINDOW_EXCEEDED'):
            _check_context_length(2049, 2048, minimum)

    def test_narrow_api_patch_persists_and_reloads_without_resetting_other_settings(self):
        import tempfile
        from pathlib import Path
        from olcr_api import app
        from olcr_api.db import Database
        with tempfile.TemporaryDirectory() as folder:
            db = Database(str(Path(folder) / 'context.sqlite'))
            db.initialize()
            current = Settings(context_budget=12345, rag_min_relevance=.7)
            def rebuild(candidate):
                app.settings = candidate
            with patch.object(app, 'db', db), patch.object(app, 'settings', current), patch.object(app, 'rebuild', side_effect=rebuild):
                result = app.patch_retrieval_settings(app.RetrievalSettingsPatch(
                    yarn_enabled=True, yarn_target_tokens=1048576, context_auto_compress=True, local_context_tokens=65536))
                self.assertEqual(12345, result['context_budget'])
                restored = Settings().with_overrides(db.load_application_settings())
                self.assertTrue(restored.yarn_enabled)
                self.assertTrue(restored.context_auto_compress)
                self.assertEqual(1048576, restored.yarn_target_tokens)
                app.patch_retrieval_settings(app.RetrievalSettingsPatch(yarn_enabled=False, context_auto_compress=False))
                restored = Settings().with_overrides(db.load_application_settings())
                self.assertFalse(restored.yarn_enabled)
                self.assertFalse(restored.context_auto_compress)
                self.assertEqual(1048576, restored.yarn_target_tokens)

    def test_selected_evidence_is_shortened_without_cutting_instructions(self):
        messages = [{'role': 'system', 'content': 'Required policy'},
                    {'role': 'system', 'content': 'Selected evidence:\n' + 'source finding ' * 10000},
                    {'role': 'user', 'content': 'Compare sources'}]
        result, report = compress_messages(messages, 4096)
        self.assertTrue(report['compressed'])
        self.assertEqual(messages[0], result[0])
        self.assertEqual(messages[2], result[2])
        self.assertIn('remainder omitted', result[1]['content'])

    def test_mlx_receives_the_saved_yarn_controls(self):
        provider = RoleAwareModelProvider(Settings(yarn_enabled=True, yarn_target_tokens=1048576))
        with patch.object(provider, '_run_mlx', return_value={'text': 'ok'}) as worker:
            provider.generate([{'role': 'user', 'content': 'hello'}], 'role:planner_replanner')
        self.assertEqual('bonsai', worker.call_args.args[0])
        controls = worker.call_args.args[1]['context']
        self.assertTrue(controls['yarn_enabled'])
        self.assertEqual(1048576, controls['yarn_target_tokens'])
        self.assertFalse(controls['context_auto_compress'])

    def test_external_api_is_not_compressed_or_given_yarn_options(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from unittest.mock import Mock
        provider = RoleAwareModelProvider(Settings(yarn_enabled=True, context_auto_compress=True))
        binding = replace(provider.registry.binding('normal_chat'), model_id='api:openai:demo',
                          deployment_model='demo', runtime='OpenAI')
        generate = Mock(return_value={'text': 'ok', 'model': 'demo'})
        provider.brain_models = SimpleNamespace(configuration=lambda: {'mode': 'unified', 'fallback_models': []},
            selected_model_for_role=lambda role: 'api:openai:demo', resolve=lambda b, r: binding,
            effort_for_role=lambda role: None, preflight=lambda *_args: {'state': 'available'},
            quick_for_model=lambda *_args: False, note_local_result=lambda *_args: None,
            providers=SimpleNamespace(generate=generate, model_metadata=lambda *_args: None))
        with patch.object(provider, '_binding', return_value=binding), patch('olcr_api.context_extension.compress_messages') as compress:
            result = provider.generate([{'role': 'user', 'content': 'hello'}], 'role:normal_chat')
        compress.assert_not_called()
        self.assertNotIn('context_compression', result)
        self.assertNotIn('options', generate.call_args.kwargs)

    def test_latest_input_with_reserved_prefixes_survives_all_context_filters(self):
        import json
        from olcr_api.context_policy import fit_messages
        inputs = [
            'Selected evidence:\n' + 'user supplied text ' * 1000,
            '[PROVIDER_EVIDENCE_JSON]\n' + json.dumps({'text': 'user data ' * 1000}),
            '[PROVIDER_EVIDENCE_JSON]\nthis is user text, not valid JSON',
            'Workspace core context\n[ACTIVE_CONVERSATION]\n' + 'user text ' * 1000,
            '[RAG_CONTEXT]\n[ACTIVE_CONVERSATION]\n' + 'user text ' * 1000,
        ]
        for content in inputs:
            with self.subTest(prefix=content[:40]):
                current = {'role': 'user', 'content': content}
                messages = [{'role': 'system', 'content': 'Required instructions'}, current]
                fitted = fit_messages(messages, content, 256)
                self.assertEqual(current, fitted[-1])
                compressed, _ = compress_messages(fitted, 2048, 1024)
                self.assertEqual(current, compressed[-1])
                self.assertEqual(content, current['content'])

    def test_history_excerpt_never_increases_characters_or_estimated_tokens(self):
        for content in ('a' * 161, 'a' * 202, '日本語' * 60, 'a' * 1000):
            messages = [{'role': 'user', 'content': content},
                        {'role': 'assistant', 'content': 'b' * 170},
                        {'role': 'user', 'content': 'current'}]
            result, report = compress_messages(messages, 2048, 2048)
            for before, after in zip(messages, result):
                self.assertLessEqual(len(after['content']), len(before['content']))
                self.assertLessEqual(estimated_tokens(after['content']), estimated_tokens(before['content']))
            self.assertLessEqual(report['estimated_tokens_after'], report['estimated_tokens_before'])

    def test_ollama_overflow_is_rejected_before_provider_request(self):
        provider = RoleAwareModelProvider(Settings(context_auto_compress=True, local_context_tokens=4096))
        current = {'role': 'user', 'content': 'current request ' * 2000}
        with patch.object(provider.ollama, 'generate') as generate:
            with self.assertRaises(ModelFailure) as error:
                provider.generate([current], 'custom-test')
        self.assertEqual('context_window_exceeded', error.exception.category)
        self.assertIn('CONTEXT_WINDOW_EXCEEDED', str(error.exception))
        generate.assert_not_called()
        self.assertEqual('current request ' * 2000, current['content'])

    def test_minimum_ollama_window_leaves_room_for_prompt_and_caps_response(self):
        provider = RoleAwareModelProvider(Settings(context_auto_compress=True, local_context_tokens=2048))
        with patch.object(provider.ollama, 'generate', return_value={'text': 'ok', 'model': 'custom-test'}) as generate:
            result = provider.generate([{'role': 'user', 'content': 'hello'}], 'custom-test')
        options = generate.call_args.kwargs['options']
        self.assertEqual(2048, options['num_ctx'])
        self.assertGreater(options['num_predict'], 0)
        self.assertLess(options['num_predict'], options['num_ctx'])
        self.assertFalse(result['context_compression']['over_budget'])

    def test_ornith_generation_uses_the_remaining_exact_token_budget(self):
        import sys
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        from olcr_api.mlx_worker import _ornith
        prompt = 'complete prompt including all required instructions'
        class Tokenizer:
            def apply_chat_template(self, messages, **kwargs):
                return prompt
            def encode(self, text):
                assert text == prompt
                return list(range(1800))
        generate = Mock(return_value='ok')
        modules = {'mlx_lm': SimpleNamespace(load=lambda path: (object(), Tokenizer()), generate=generate,
                                             stream_generate=Mock()),
                   'mlx_lm.sample_utils': SimpleNamespace(make_sampler=lambda **kwargs: None)}
        with tempfile.TemporaryDirectory() as folder:
            pack = Path(folder)
            (pack / 'model.safetensors.index.json').write_text('{}')
            (pack / 'config.json').write_text('{}')
            with patch.dict(sys.modules, modules), patch('olcr_api.mlx_worker._asset', return_value=pack):
                result = _ornith([{'role': 'user', 'content': 'hello'}], 2048, False,
                    context={'context_auto_compress': True, 'local_context_tokens': 2048})
        self.assertEqual('ok', result)
        self.assertEqual(prompt, generate.call_args.kwargs['prompt'])
        self.assertEqual(248, generate.call_args.kwargs['max_tokens'])

    def test_global_capability_toggles_and_length_settings_remain_independent(self):
        import tempfile
        from pathlib import Path
        from olcr_api import app
        from olcr_api.db import Database
        with tempfile.TemporaryDirectory() as folder:
            db = Database(str(Path(folder) / 'context.sqlite'))
            db.initialize()
            current = Settings(yarn_target_tokens=1048576, local_context_tokens=65536)
            def rebuild(candidate):
                app.settings = candidate
            with patch.object(app, 'db', db), patch.object(app, 'settings', current), patch.object(app, 'rebuild', side_effect=rebuild):
                app.patch_capability_settings(app.CapabilitySettingsPatch(yarn_enabled=True))
                app.patch_capability_settings(app.CapabilitySettingsPatch(context_auto_compress=True))
                restored = Settings().with_overrides(db.load_application_settings())
                self.assertTrue(restored.yarn_enabled)
                self.assertTrue(restored.context_auto_compress)
                self.assertEqual(1048576, app.settings.yarn_target_tokens)
                self.assertEqual(65536, app.settings.local_context_tokens)
                app.patch_retrieval_settings(app.RetrievalSettingsPatch(yarn_target_tokens=524288, local_context_tokens=32768))
                self.assertTrue(app.settings.yarn_enabled)
                self.assertTrue(app.settings.context_auto_compress)
                app.patch_capability_settings(app.CapabilitySettingsPatch(yarn_enabled=False))
                app.patch_capability_settings(app.CapabilitySettingsPatch(context_auto_compress=False))
                restored = Settings().with_overrides(db.load_application_settings())
                self.assertFalse(restored.yarn_enabled)
                self.assertFalse(restored.context_auto_compress)
                self.assertEqual(524288, restored.yarn_target_tokens)
                self.assertEqual(32768, restored.local_context_tokens)
