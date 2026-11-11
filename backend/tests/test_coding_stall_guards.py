"""Regressions for coding that spent its whole output budget thinking invisibly."""
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from olcr_api.coding_tasks import model_slot
from olcr_api.db import Database
from olcr_api.generation_control import Generation, GenerationCancelled, generation_scope
from olcr_api.model_progress import model_progress, observe_model_progress
from olcr_api.ollama import ModelFailure, OllamaProvider, unbounded_inference


class CodingStallGuardsTests(unittest.TestCase):
    def test_stop_interrupts_slot_wait_without_waiting_for_the_owner(self):
        entered = threading.Event()
        stopped = threading.Event()
        generation = Generation()
        errors = []

        def waiter():
            try:
                with generation_scope(generation):
                    entered.set()
                    with model_slot():
                        errors.append('cancelled waiter acquired the slot')
            except GenerationCancelled:
                stopped.set()

        with model_slot():
            worker = threading.Thread(target=waiter)
            worker.start()
            self.assertTrue(entered.wait(1))
            generation.cancel()
            interrupted = stopped.wait(.5)
        worker.join(2)
        self.assertTrue(interrupted, 'Stop was blocked behind another model invocation')
        self.assertFalse(errors)
        # Cancellation must never release another request's lock or leak its own.
        with model_slot():
            pass

    def test_account_retries_preserve_separate_received_reasoning_logs(self):
        from olcr_api.live_model_logs import record_model_log
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / 'test.db'); db.initialize()
            db.create_conversation('test', 1, 'chat', None)
            with observe_model_progress(lambda event: record_model_log(db, 'chat', event)):
                model_progress(state='request_started', model='coder', runtime='api')
                model_progress(state='account_attempt', model='coder', account=1)
                model_progress(state='reasoning', text='first attempt')
                model_progress(state='account_failed', model='coder', account=1)
                model_progress(state='account_attempt', model='coder', account=2)
                model_progress(state='reasoning', text='second delta')
                model_progress(state='reasoning', text='second final', snapshot=True)
            with db.connect() as connection:
                logs=connection.execute("SELECT data_json FROM live_model_logs WHERE kind='reasoning' ORDER BY id").fetchall()
            self.assertEqual(['first attempt','second final'], [json.loads(row[0])['text'] for row in logs])

    def test_task_polling_exposes_worker_progress_without_prompts(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / 'test.db')
            db.initialize()
            db.create_conversation('test', 1, 'chat', None)
            db.create_coding_task('task', 'chat', 'Build app', 'RUNNING', 'IMPLEMENTATION', None, 1)
            db.record_coding_event('task', 'MODEL_INVOCATION', {
                'invocation_id': 'first', 'state': 'request_started', 'model': 'coder', 'runtime': 'mlx'})
            db.record_coding_event('task', 'MODEL_PROGRESS', {
                'invocation_id': 'first', 'state': 'worker_progress',
                'event': 'bonsai_generation_progress', 'generated_tokens': 128, 'max_tokens': 8192})
            value = db.coding_task('task')
            self.assertEqual(128, value.get('model_progress', {}).get('generated_tokens'))
            db.record_coding_event('task', 'MODEL_INVOCATION', {
                'invocation_id': 'first', 'state': 'request_completed', 'model': 'coder', 'runtime': 'mlx'})
            self.assertIsNone(db.coding_task('task').get('model_progress'),
                              'Finished inference must not still be shown as generating')
            db.record_coding_event('task', 'MODEL_INVOCATION', {
                'invocation_id': 'second', 'state': 'request_started', 'model': 'api-coder', 'runtime': 'google'})
            self.assertIsNone(db.coding_task('task').get('model_progress'))

    def test_structured_ollama_stops_unfinished_reasoning_before_the_entire_output_budget(self):
        # Distinct text avoids depending on the separate repetition detector.
        reasoning = ''.join(f'Consider requirement {i:04d} carefully. ' for i in range(1000))
        frames = [json.dumps({'message': {'thinking': reasoning[i:i+128]}, 'done': False})
                  for i in range(0, len(reasoning), 128)]
        response = io.BytesIO(('\n'.join(frames)).encode())
        with unbounded_inference(), self.assertRaisesRegex(ModelFailure, 'THINKING_BUDGET'):
            list(OllamaProvider('http://fixture')._stream(response, 0, {'format': {'type': 'object'}}))
        self.assertTrue(response.closed)

    def test_structured_ollama_progress_reports_counts_without_source_content(self):
        response = io.BytesIO((json.dumps({'message': {'content': 'private source'}, 'done': True,
                                           'eval_count': 7}) + '\n').encode())
        events = []
        with observe_model_progress(events.append):
            list(OllamaProvider('http://fixture')._stream(response, 0, {'format': {'type': 'object'}}))
        progress = [event for event in events if event['state'] == 'worker_progress']
        self.assertTrue(progress)
        self.assertEqual(7, progress[-1]['generated_tokens'])
        self.assertNotIn('private source', json.dumps(events))

    def test_headless_structured_coding_uses_the_same_reasoning_guard(self):
        reasoning = ''.join(f'Consider requirement {i:04d} carefully. ' for i in range(1000))
        response = io.BytesIO((json.dumps({'message': {'thinking': reasoning}, 'done': False}) + '\n').encode())
        with unbounded_inference(), patch('olcr_api.ollama.open_provider_response', return_value=response):
            with self.assertRaisesRegex(ModelFailure, 'THINKING_BUDGET'):
                OllamaProvider('http://fixture').generate([], 'coder', format={'type': 'object'})
        self.assertTrue(response.closed)

    def test_whitespace_does_not_disable_the_structured_reasoning_guard(self):
        frames=[{'message': {'content': ' \n'}, 'done': False},
                {'message': {'thinking': ''.join(f'Consider requirement {i:04d} carefully. ' for i in range(1000))}, 'done': False}]
        response=io.BytesIO(('\n'.join(map(json.dumps,frames))).encode())
        with unbounded_inference(), self.assertRaisesRegex(ModelFailure, 'THINKING_BUDGET'):
            list(OllamaProvider('http://fixture')._stream(response,0,{'format':'EXPLICIT_JSON_SCHEMA'}))
        self.assertTrue(response.closed)

    def test_normal_structured_mlx_does_not_receive_the_coding_reasoning_limit(self):
        from olcr_api.mlx_worker import _thinking_constraint
        self.assertIsNone(_thinking_constraint(object(),object(),'prompt',True).max_reasoning_tokens)
        from olcr_api.config import Settings
        from olcr_api.model_provider import RoleAwareModelProvider
        provider=RoleAwareModelProvider(Settings())
        try:
            for coding in (False,True):
                from contextlib import nullcontext
                with (unbounded_inference() if coding else nullcontext()), patch.object(provider,'_run_mlx',return_value={'text':'{}'}) as worker:
                    provider.generate([], 'role:short_coding_fix_debug', format={'type':'object'}, think=True)
                self.assertEqual(coding,worker.call_args.args[1].get('bounded_reasoning',False))
        finally:
            provider.close()


if __name__ == '__main__':
    unittest.main()
