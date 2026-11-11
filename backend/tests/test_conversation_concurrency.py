"""Concurrent invocations keep independent configuration and capacity diagnostics."""
import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from olcr_api.brain_models import active_configuration, brain_scope
from olcr_api.brain_providers import call_json
from olcr_api.config import Settings
from olcr_api.model_provider import RoleAwareModelProvider
from olcr_api.ollama import ModelFailure, OllamaProvider, model_concurrency_failure


class ConversationConcurrencyTests(unittest.TestCase):
    def test_same_model_accepts_independent_overlapping_requests(self):
        barrier = threading.Barrier(2)
        provider = OllamaProvider('http://fixture')
        def respond(req, **_):
            payload = json.loads(req.data)
            barrier.wait(timeout=2)
            return io.BytesIO(json.dumps({'model': 'shared', 'message': {'content': payload['messages'][0]['content']}}).encode())
        with patch('olcr_api.ollama.request.urlopen', side_effect=respond), ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(provider.generate, [{'role': 'user', 'content': 'private A'}], 'shared')
            b = pool.submit(provider.generate, [{'role': 'user', 'content': 'private B'}], 'shared')
            self.assertEqual('private A', a.result(timeout=3)['text'])
            self.assertEqual('private B', b.result(timeout=3)['text'])

    def test_stream_fallback_keeps_its_original_conversation_configuration(self):
        provider = RoleAwareModelProvider(Settings())
        base = provider._binding('shared-fixture')
        def config(primary, fallback):
            return {'mode': 'unified', 'selections': {'brain': primary}, 'fallback_models': [fallback], 'efforts': {'brain': None}}
        global_config = config('global-primary', 'global-fallback')
        provider.brain_models = SimpleNamespace(
            configuration=lambda: global_config,
            selected_model_for_role=lambda _: active_configuration(global_config)['selections']['brain'],
            preflight=lambda *_args: {'state': 'available'},
            quick_for_model=lambda *_args: False,
            note_local_result=lambda *_args: None,
            resolve=lambda binding, _: replace(binding, role='normal_chat', deployment_model=active_configuration(global_config)['selections']['brain']),
        )
        calls = []
        def generate(messages, model, **kwargs):
            calls.append(model)
            if model == 'A-primary':
                def rejected():
                    raise ModelFailure('provider_unavailable', 'fixture interruption')
                    yield
                return rejected()
            return {'text': model, 'model': model}
        with patch.object(provider, '_binding', return_value=replace(base, role='normal_chat')), patch.object(provider.ollama, 'generate', side_effect=generate):
            with brain_scope(config('A-primary', 'A-fallback')):
                stream = provider.generate([], 'shared-fixture', stream=True)
            with brain_scope(config('B-primary', 'B-fallback')):
                frames = list(stream)
        self.assertEqual(['A-primary', 'A-fallback'], calls)
        self.assertEqual('A-fallback', frames[0]['text'])

    def test_capacity_is_distinguished_from_quota_and_private_provider_body(self):
        def rejected(body, status=429):
            return HTTPError('http://fixture', status, 'rejected', {}, io.BytesIO(body.encode()))
        failure = model_concurrency_failure(rejected('maximum concurrent requests exceeded; private prompt/key'))
        self.assertEqual('model_concurrency_limit', failure.category)
        self.assertIn('MODEL_CONCURRENCY_LIMIT', str(failure))
        self.assertNotIn('private prompt/key', str(failure))
        self.assertIsNone(model_concurrency_failure(rejected('weekly token quota exceeded')))
        with patch('olcr_api.brain_providers.request.build_opener') as opener:
            opener.return_value.open.side_effect = rejected('model is busy; secret credential', 503)
            with self.assertRaises(ModelFailure) as raised:
                call_json('http://fixture', {'model': 'shared'}, {})
        self.assertEqual('model_concurrency_limit', raised.exception.category)
        self.assertNotIn('secret credential', str(raised.exception))
