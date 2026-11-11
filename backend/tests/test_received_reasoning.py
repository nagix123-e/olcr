import asyncio
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from olcr_api.brain_models import BrainModels
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.models import Task, TaskState
from olcr_api.ollama import OllamaProvider
from olcr_api.runtime import _model_call_metadata
from olcr_api.thinking import received_reasoning


class Response(io.BytesIO):
    headers = {}


class ReceivedReasoningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / 'test.sqlite'))
        self.db.initialize()
        self.manager = BrainModels(self.db, Settings())

    def tearDown(self):
        self.tmp.cleanup()

    def test_ollama_keeps_native_channels_without_an_extra_call_or_request_field(self):
        raw = {'message': {'content': 'answer', 'thinking': 'received work'}, 'eval_count': 12}
        with patch('olcr_api.ollama.open_provider_response', return_value=Response(json.dumps(raw).encode())) as call:
            result = OllamaProvider('http://localhost').generate([], 'local', think=True)
        self.assertEqual('answer', result['text'])
        self.assertEqual('received work', result['reasoning']['text'])
        self.assertEqual(12, result['completion_tokens'])
        call.assert_called_once()
        self.assertEqual({'model':'local','messages':[],'stream':False,'think':True}, json.loads(call.call_args.args[0].data))

    def test_ollama_stream_preserves_reasoning_deltas_and_answer_independently(self):
        frames = [{'message': {'thinking': 'one '}}, {'message': {'thinking': 'two'}},
                  {'message': {'content': 'answer'}, 'done': True, 'eval_count': 8}]
        response = Response(b'\n'.join(json.dumps(frame).encode() for frame in frames))
        with patch('olcr_api.ollama.open_provider_response', return_value=response) as call:
            result = list(OllamaProvider('http://localhost').generate([], 'local', stream=True, think=True))
        self.assertEqual('one two', ''.join(frame['reasoning']['text'] for frame in result if frame['reasoning']))
        self.assertEqual('answer', ''.join(frame['text'] for frame in result))
        self.assertEqual(8, result[-1]['completion_tokens'])
        call.assert_called_once()
        self.assertTrue(response.closed)

    def test_external_channels_are_passive_and_opaque_data_is_ignored(self):
        cases = [
            ('openai', {'choices':[{'message':{'content':'answer','reasoning_content':'work'}}]}, 'trace'),
            ('google', {'candidates':[{'content':{'parts':[{'text':'work','thought':True},
                {'text':'answer'}, {'thoughtSignature':'opaque'}]}}]}, 'summary'),
            ('anthropic', {'content':[{'type':'thinking','thinking':'work','signature':'opaque'},
                {'type':'redacted_thinking','data':'opaque'}, {'type':'text','text':'answer'}]}, 'summary')]
        for provider, response, kind in cases:
            with self.subTest(provider=provider):
                self.manager.providers.register(provider, 'api_key', {'base_url':'http://127.0.0.1:9/v1',
                    'model_ids':'test-model','api_key':'fixture-only'})
                with patch('olcr_api.brain_providers.call_json', return_value=response) as call:
                    result = self.manager.providers.generate(provider, 'test-model', [], None, False)
                call.assert_called_once()
                body = json.dumps(call.call_args.args[1])
                self.assertNotIn('includeThoughts', body)
                self.assertNotIn('summary', body)
                self.assertNotIn('thinking', body)
                self.assertEqual('answer', result['text'])
                self.assertEqual('work', result['reasoning']['text'])
                self.assertEqual(kind, result['reasoning']['kind'])
                self.assertNotIn('opaque', json.dumps(result))

    def test_responses_already_delivered_summary_is_deduplicated(self):
        events = [
            {'type':'response.reasoning_summary_text.delta','item_id':'r','summary_index':0,'delta':'work'},
            {'type':'response.reasoning_summary_text.done','item_id':'r','summary_index':0,'text':'work'},
            {'type':'response.output_text.delta','delta':'answer'},
            {'type':'response.completed','response':{'usage':{'output_tokens':12},'output':[{'encrypted_content':'opaque'}]}}]
        response = Response(b'\n'.join(b'data: ' + json.dumps(event).encode() for event in events))
        payload = {'model':'test','stream':True}
        with patch('olcr_api.brain_providers.open_provider_response', return_value=response) as call:
            answer, usage = self.manager.providers._responses_stream('http://localhost', payload, 'fixture-only')
        call.assert_called_once()
        self.assertEqual(payload, json.loads(call.call_args.args[0].data))
        self.assertEqual('answer', answer)
        self.assertEqual('work', usage['_olcr_reasoning']['text'])
        self.assertEqual('summary', usage['_olcr_reasoning']['kind'])
        self.assertEqual(12, usage['output_tokens'])
        self.assertNotIn('opaque', json.dumps(usage))

    def test_persistence_retains_logs_outside_answer_and_repeated_save_is_idempotent(self):
        self.db.create_conversation('one', time.time(), 'one')
        task = Task('hello')
        result = {'text':'answer','reasoning':received_reasoning('private received work')}
        task.model_calls.append(_model_call_metadata(result, 'local'))
        self.db.save_task(task, 'one')
        self.db.save_task(task, 'one')
        self.db.add_message('one', 'assistant', 'answer', time.time(), 'message', task.id)
        message = Database(self.db.path).conversation('one')['messages'][0]
        self.assertEqual('answer', message['content'])
        self.assertEqual('private received work', message['blocks'][0]['logs'][0]['text'])
        import olcr_api.app as api
        with patch.object(api, 'db', self.db):
            self.assertNotIn('private received work', api.active_conversation_context('one'))
            self.assertNotIn('private received work', api.active_conversation_context('one', request='answer'))
        with self.db.connect() as conn:
            self.assertEqual(1, conn.execute('SELECT COUNT(*) FROM task_reasoning').fetchone()[0])
        self.db.create_conversation('two', time.time(), 'two')
        self.assertEqual([], self.db.conversation('two')['messages'])

    def test_chat_stream_separates_persists_and_completes_received_channel(self):
        import olcr_api.app as api
        self.db.create_conversation('one', time.time(), 'one')
        class Model:
            calls = 0
            def generate(self, *args, **kwargs):
                self.calls += 1
                return iter([{'text':'','reasoning':received_reasoning('work ', complete=False)},
                             {'text':'answer','reasoning':received_reasoning('done', complete=False)}])
        model = Model()
        async def read(response):
            return [json.loads(str(chunk).removeprefix('data: ').strip()) async for chunk in response.body_iterator]
        with patch.object(api,'db',self.db), patch.object(api,'brain_models',self.manager), patch.object(api.runtime,'model',model):
            events = asyncio.run(read(api.stream_chat(api.ChatInput(message='hello', conversation_id='one'))))
        self.assertEqual(1, model.calls)
        self.assertEqual(2, len([event for event in events if event['type']=='reasoning_delta']))
        self.assertEqual('answer', ''.join(event.get('text','') for event in events if event['type']=='chunk'))
        logs = [event for event in events if event['type']=='reasoning'][-1]['block']['logs']
        self.assertEqual('work done', logs[0]['text'])
        self.assertTrue(logs[0]['complete'])
        assistant = self.db.conversation('one')['messages'][-1]
        self.assertEqual('answer', assistant['content'])
        self.assertEqual(logs, assistant['blocks'][0]['logs'])

    def test_failed_stream_keeps_an_incomplete_log_without_an_answer(self):
        import olcr_api.app as api
        from olcr_api.ollama import ModelFailure
        self.db.create_conversation('one', time.time(), 'one')
        class Model:
            def generate(self, *args, **kwargs):
                yield {'text':'','reasoning':received_reasoning('unfinished work', complete=False)}
                raise ModelFailure('timeout', 'fixture timeout')
        async def read(response):
            return [json.loads(str(chunk).removeprefix('data: ').strip()) async for chunk in response.body_iterator]
        with patch.object(api,'db',self.db), patch.object(api,'brain_models',self.manager), patch.object(api.runtime,'model',Model()):
            events = asyncio.run(read(api.stream_chat(api.ChatInput(message='hello', conversation_id='one'))))
        self.assertEqual('error', events[-1]['type'])
        assistant = self.db.conversation('one')['messages'][-1]
        self.assertEqual('', assistant['content'])
        self.assertFalse(assistant['blocks'][0]['logs'][0]['complete'])

    def test_responses_completed_summary_is_authoritative_and_bounded(self):
        events = [{'type':'response.reasoning_summary_text.delta','item_id':'r','delta':'partial'},
            {'type':'response.output_text.delta','delta':'answer'},
            {'type':'response.completed','response':{'output':[{'type':'reasoning','encrypted_content':'opaque',
                'summary':[{'type':'summary_text','text':'x'*64001}]}]}}]
        response = Response(b'\n'.join(b'data: ' + json.dumps(event).encode() for event in events))
        with patch('olcr_api.brain_providers.open_provider_response', return_value=response):
            _, usage = self.manager.providers._responses_stream('http://localhost', {'model':'test'}, 'fixture-only')
        log = usage['_olcr_reasoning']
        self.assertEqual(64000, len(log['text']))
        self.assertTrue(log['truncated'])
        self.assertNotIn('opaque', json.dumps(usage))
