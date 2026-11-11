import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from olcr_api.ollama import unbounded_inference, inference_timeout, provider_timeout_seconds, model_timeout_seconds
from olcr_api.brain_providers import call_json
from olcr_api.model_progress import observe_model_progress, model_progress
from olcr_api.package_manager import allow_bootstrap_default, package_manager_from_project
from olcr_api.db import Database

class CodingWaitTests(unittest.TestCase):
    def test_remote_requests_keep_a_socket_timeout_during_unbounded_local_coding(self):
        with unbounded_inference():
            self.assertIsNone(model_timeout_seconds(120))
            with patch('olcr_api.brain_providers.open_provider_response',side_effect=OSError('offline')) as opened:
                with self.assertRaises(Exception): call_json('https://example.org',{}, {})
                self.assertEqual(120,opened.call_args.kwargs['timeout'])
            with inference_timeout(10):
                self.assertLessEqual(provider_timeout_seconds(120),10)
                self.assertLessEqual(model_timeout_seconds(750),10)
    def test_bootstrap_defaults_apply_to_general_apps_and_preserve_existing_project_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            required={'new_project_intent':True,'task_profile':'GENERAL_CODING'}
            self.assertTrue(allow_bootstrap_default(required))
            self.assertEqual('npm',package_manager_from_project(root,allow_explicit_default=allow_bootstrap_default(required))['package_manager'])
            (root/'pnpm-lock.yaml').write_text('lockfileVersion: 9')
            self.assertEqual('pnpm',package_manager_from_project(root,allow_explicit_default=True)['package_manager'])
            self.assertFalse(allow_bootstrap_default({'new_project_intent':False}))
    def test_invocation_progress_is_persisted_and_restored_without_prompt_data(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Database(Path(directory)/'test.db');db.initialize();db.create_conversation('test',1,'chat',None)
            db.create_coding_task('work','chat','Build app','PLANNING','QWEN_PLANNING',None,1)
            with observe_model_progress(lambda data:db.record_coding_event('work','MODEL_INVOCATION',data)):
                model_progress(state='request_started',role='planner_replanner',model='planner-api',runtime='google')
                model_progress(state='request_failed',role='planner_replanner',model='planner-api',runtime='google',failure='provider_unavailable')
            result=db.coding_task('work')['model_invocation']
            self.assertEqual('request_failed',result['state']);self.assertEqual('planner-api',result['model'])
            self.assertEqual(2,len(db.coding_task_events('work')))

    def test_cancelled_coding_invocation_is_persisted_as_terminal_status(self):
        from olcr_api import app
        with tempfile.TemporaryDirectory() as directory:
            db=Database(Path(directory)/'test.db');db.initialize();db.create_conversation('test',1,'chat',None)
            db.create_coding_task('work','chat','Build app','RUNNING','IMPLEMENTATION',None,1)
            with patch.object(app,'db',db):
                app._record_coding_model_progress('work',{
                    'state':'request_cancelled','invocation_id':'cancelled-call','model':'coder',
                    'runtime':'mlx','failure':'cancelled'})
            result=db.coding_task('work')['model_invocation']
            self.assertEqual('request_cancelled',result['state'])
            self.assertEqual('cancelled-call',result['invocation_id'])
            self.assertEqual(1,len(db.coding_task_events('work')))
