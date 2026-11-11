import tempfile
import unittest
import os
import json
import base64
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api.db import Database
from olcr_api.procedures import ProcedureStore, routine_goal_signature
from olcr_api.config import Settings
from olcr_api.skills import SkillManager
from olcr_api.runtime import ContextManager

class GuiFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.old_db, self.old_settings, self.old_skills = api.db, api.settings, api.skills
        api.db = Database(str(Path(self.tmp.name)/'test.sqlite'))
        api.skills = SkillManager(Path(self.tmp.name) / 'skills')
        api.db.initialize()
        api.rebuild(self.old_settings.with_overrides({
            'db_path': str(Path(self.tmp.name) / 'test.sqlite'),
            'task_manager_enabled': False,
        }))
        self.client = TestClient(api.app)
        self.project = self.client.post('/api/projects', json={'name':'A','workspace_path':self.tmp.name}).json()['id']
    def tearDown(self):
        api.db = self.old_db
        api.skills = self.old_skills
        api.rebuild(self.old_settings)
        self.tmp.cleanup()
    def test_catalog_and_dispatch(self):
        commands=self.client.get('/api/commands').json()['commands']
        self.assertEqual(len(commands),len({x['id'] for x in commands}))
        self.assertIn('/screen', {row['command'] for row in commands})
        self.assertIn('/computer use on', {row['command'] for row in commands})
        self.assertIn('/computer use off', {row['command'] for row in commands})
        self.assertEqual(422,self.client.post('/api/commands',json={'text':'/arbitrary'}).status_code)
        self.assertEqual(422,self.client.post('/api/commands',json={'text':'/context show'}).status_code)
        self.assertEqual(200,self.client.post('/api/commands',json={'text':'/memory off'}).status_code)
        self.assertFalse(api.settings.conversation_memory_enabled)
        self.assertEqual(200,self.client.post('/api/commands',json={'text':'/help'}).status_code)

    def test_agent_video_search_uses_youtube_in_both_chat_transports(self):
        from olcr_api.assistant_actions import AssistantActions
        service = AssistantActions(api.db, lambda: api.settings.with_overrides({'external_access_enabled': True}))
        videos = [{'id':'dQw4w9WgXcQ','title':'Python tutorial','url':'https://www.youtube.com/watch?v=dQw4w9WgXcQ'}]
        for endpoint in ('/api/chat', '/api/chat/stream'):
            with self.subTest(endpoint=endpoint), \
                 patch.object(api, 'get_assistant_service', return_value=service), \
                 patch('olcr_api.assistant_actions.search_youtube', return_value=videos) as search, \
                 patch.object(api.runtime, 'execute', side_effect=AssertionError('must not use generic Web search')):
                response = self.client.post(endpoint, json={'message':'YouTubeでPythonの使い方動画を検索して', 'project_id':self.project})
            self.assertEqual(200, response.status_code, response.text)
            search.assert_called_once()
            if endpoint.endswith('/stream'):
                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                block = next(event['block'] for event in events if event['type'] == 'block')
            else:
                block = response.json()['blocks'][0]
            self.assertEqual(videos, block['candidates'])
            self.assertFalse(block['autoplay'])

    def test_stop_interrupts_stream_before_another_token_and_preserves_partial_answer(self):
        import threading
        from olcr_api.generation_control import current_generation, check_cancelled
        identifier = 'generation-stop-fixture-0001'
        waiting, responses = threading.Event(), []
        def generate(*args, **kwargs):
            def frames():
                yield {'text': 'Partial answer', 'model': api.settings.main_model}
                generation = current_generation()
                self.assertIsNotNone(generation)
                waiting.set()
                generation.event.wait(3)
                check_cancelled()
                yield {'text': 'must never appear'}
            return frames()
        def request():
            responses.append(self.client.post('/api/chat/stream', json={
                'message': 'こんにちは', 'project_id': self.project},
                headers={'X-OLCR-Generation': identifier}))
        with patch.object(api.runtime.model, 'generate', side_effect=generate):
            worker = threading.Thread(target=request, daemon=True)
            worker.start()
            self.assertTrue(waiting.wait(3))
            stopped = self.client.post(f'/api/generations/{identifier}/cancel')
            self.assertEqual(200, stopped.status_code, stopped.text)
            worker.join(2)
            self.assertFalse(worker.is_alive(), 'Stop waited for another token')
        response = responses[0]
        self.assertEqual(200, response.status_code, response.text)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        self.assertEqual('cancelled', events[-1]['type'])
        self.assertEqual('cancelled', events[-1]['task']['state'])
        self.assertNotIn('must never appear', response.text)
        saved = api.db.conversation(events[0]['conversation_id'])
        self.assertEqual('Partial answer', saved['messages'][-1]['content'])

    def test_generation_header_is_allowed_by_desktop_cors_and_stop_needs_no_confirmation(self):
        response = self.client.options('/api/chat/stream', headers={
            'Origin': 'tauri://localhost', 'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'x-olcr-generation,x-olcr-session,x-olcr-language,content-type'})
        self.assertEqual(200, response.status_code, response.text)
        from olcr_api.permissions import requires_request_approval
        self.assertFalse(requires_request_approval('POST', '/api/generations/request-id/cancel'))

    def test_completed_chat_schedules_memory_after_saving_in_both_routes(self):
        prompt = "こんにちは"
        answer = "こんにちは。"
        task = api.Task(prompt)
        task.transition(api.TaskState.ROUTING)
        task.transition(api.TaskState.GENERATING)
        task.transition(api.TaskState.COMPLETED)

        def check_saved(conversation_id):
            self.assertEqual(answer, api.db.conversation(conversation_id)["messages"][-1]["content"])

        for endpoint in ("/api/chat", "/api/chat/stream"):
            with self.subTest(endpoint=endpoint), \
                 patch.object(api, "_automatic_skill_selection", return_value=None), \
                 patch.object(api.conversation_memory, "search", return_value=[]), \
                 patch.object(api.runtime, "execute", return_value=(task, answer)), \
                 patch.object(api.runtime.model, "generate",
                              return_value=iter([{"text": answer, "model": api.settings.main_model}])), \
                 patch.object(api.conversation_memory, "schedule_maintenance", side_effect=check_saved) as schedule:
                response = self.client.post(endpoint, json={"message": prompt, "project_id": self.project})
                self.assertEqual(200, response.status_code, response.text)
                schedule.assert_called_once()

    def test_chat_management_cards_survive_conversation_reload_without_model_or_network_execution(self):
        with patch.object(api.runtime, 'execute', side_effect=AssertionError('model execution forbidden')):
            for text, kind in [('/settings', 'settings_control'),
                               ('/scrape manage', 'scrape_control'), ('/routines', 'routine_control')]:
                response = self.client.post('/api/commands', json={'text': text, 'project_id': self.project})
                self.assertEqual(200, response.status_code, response.text)
                result = response.json()
                self.assertEqual([{'type': kind}], result['blocks'])
                restored = self.client.get(f"/api/projects/{self.project}/conversations/{result['conversation_id']}")
                self.assertEqual(200, restored.status_code)
                self.assertEqual(result['blocks'], restored.json()['messages'][-1]['blocks'])
                self.assertNotIn('credentials', result['data'])
            natural = self.client.post('/api/chat', json={'message': '設定を開いて', 'project_id': self.project})
            self.assertEqual(200, natural.status_code, natural.text)
            self.assertEqual([{'type': 'settings_control'}], natural.json()['blocks'])

    def test_chat_capability_commands_preserve_other_permissions_and_scope(self):
        api.settings = api.settings.with_overrides({'computer_use_enabled': True,
            'computer_use_allowed_bundle_ids': ['com.example.allowed'], 'web_scraping_enabled': False,
            'local_e2e_enabled': False})
        for command, field in [('/scrape on', 'web_scraping_enabled'), ('/e2e on', 'local_e2e_enabled')]:
            response = self.client.post('/api/commands', json={'text': command, 'project_id': self.project})
            self.assertEqual(200, response.status_code, response.text)
            self.assertTrue(response.json()['data'][field])
        self.assertTrue(api.settings.computer_use_enabled)
        self.assertEqual(['com.example.allowed'], list(api.settings.computer_use_allowed_bundle_ids))
        response = self.client.post('/api/chat', json={'message': 'スクレイピングをオフにして', 'project_id': self.project})
        self.assertEqual(200, response.status_code, response.text)
        self.assertFalse(api.settings.web_scraping_enabled)
        self.assertTrue(api.settings.local_e2e_enabled)

    def test_computer_use_settings_do_not_expose_per_app_grant_commands(self):
        commands = self.client.get('/api/commands').json()['commands']
        names = {command['command'] for command in commands}
        self.assertIn('/computer use on', names)
        self.assertIn('/computer use off', names)
        self.assertNotIn('/computer use scope', names)
        self.assertNotIn('/computer use allow', names)
        self.assertNotIn('/computer use remove', names)

    def test_routine_chat_commands_reuse_compilation_and_review_without_execution(self):
        target = {'role': 'AXMenuItem', 'label': 'Find…', 'enabled': True}
        trace = {'trace_version': 1, 'goal_signature_version': 1,
            'goal_signature': routine_goal_signature('Open Find'),
            'context': {'source': 'AX', 'session_id': 'old', 'application_pid': 123,
                        'application_bundle_id': 'com.example.demo', 'coordinate_space': 'ax_global_screen_coordinates'},
            'preconditions': [target], 'action': {'kind': 'open_find', 'mechanism': 'keyboard_shortcut'},
            'actions': [{'kind': 'open_find', 'mechanism': 'keyboard_shortcut', 'target': target,
                         'shortcut': {'key_code': 3, 'modifiers': 0, 'key': 'f', 'provenance': 'application_menu'}}],
            'expected_effects': [{'role': 'AXSearchField|AXTextField', 'focused': True}],
            'verification': {'result': 'CONFIRMED'}}
        store = ProcedureStore(api.db)
        first = store.capture_verified_trace(trace)
        second = store.capture_verified_trace({**trace, 'context': {**trace['context'], 'session_id': 'second'}})
        response = self.client.post('/api/commands', json={
            'text': f'/routines compile {first.id} {second.id}', 'project_id': self.project})
        self.assertEqual(200, response.status_code, response.text)
        candidate_id = response.json()['data']['procedure']['id']
        approved = self.client.post('/api/commands', json={
            'text': f'/routines approve {candidate_id}', 'project_id': self.project})
        self.assertEqual(200, approved.status_code, approved.text)
        self.assertFalse(approved.json()['data']['validated'])
        self.assertEqual('APPROVED', approved.json()['data']['review']['decision'])
        for text in ['/routines compile single', '/routines approve', '/routines match']:
            invalid = self.client.post('/api/commands', json={'text': text, 'project_id': self.project})
            self.assertEqual(422, invalid.status_code, invalid.text)

    def test_computer_use_slash_commands_toggle_global_setting_without_changing_scope(self):
        allowed_bundle_ids = ['com.example.allowed-game']
        api.settings = api.settings.with_overrides({
            'computer_use_enabled': False,
            'computer_use_allowed_bundle_ids': allowed_bundle_ids,
        })
        enabled = self.client.post('/api/commands', json={
            'text': '/computer use on', 'project_id': self.project,
        })
        self.assertEqual(200, enabled.status_code, enabled.text)
        self.assertTrue(api.settings.computer_use_enabled)
        self.assertEqual(allowed_bundle_ids, list(api.settings.computer_use_allowed_bundle_ids))
        self.assertEqual({'scope': 'global', 'computer_use_enabled': True}, enabled.json()['data'])
        self.assertEqual('Computer Use: ON', enabled.json()['message'])

        disabled = self.client.post('/api/commands', json={
            'text': '/computer use off', 'project_id': self.project,
        })
        self.assertEqual(200, disabled.status_code, disabled.text)
        self.assertFalse(api.settings.computer_use_enabled)
        self.assertEqual(allowed_bundle_ids, list(api.settings.computer_use_allowed_bundle_ids))
        self.assertEqual({'scope': 'global', 'computer_use_enabled': False}, disabled.json()['data'])
        self.assertEqual('Computer Use: OFF', disabled.json()['message'])

    def test_laya_language_selection_uses_english_checkpoint_only_for_ascii_english(self):
        self.assertEqual('english', api._laya_request_language('Use calculator for 17 times 19.'))
        self.assertEqual('multilingual', api._laya_request_language('23×19はいくつですか？'))

    def test_screen_command_uses_structured_dom_and_bypasses_vision(self):
        class Decision:
            loaded = True
            def choice(self, *, question, candidates, language='multilingual'):
                self.question = question
                return {'tool_id': next(row['tool_id'] for row in candidates if row['tool_id'] == 'read:status')}
        decision=Decision()
        observation={'dom':{'elements':[
            {'id':'heading','role':'heading','label':'Workspace','visible':True},
            {'id':'status','role':'status','label':'READY','visible':True},
        ],'text':['Workspace','READY'],'confidence':1.0}}
        with patch.object(api,'laya_provider',decision):
            response=self.client.post('/api/commands',json={
                'text':'/screen What is the current status?',
                'project_id':self.project,
                'screen_observation':observation,
            })
        self.assertEqual(200,response.status_code,response.text)
        body=response.json()
        self.assertIn('READY',body['message'])
        self.assertEqual('DOM',body['data']['observation_source'])
        self.assertEqual(0,body['data']['vision_model_calls'])
        self.assertFalse(body['data']['action_executed'])

    def test_screen_command_requires_domax_and_rejects_oversized_observation(self):
        missing=self.client.post('/api/commands',json={'text':'/screen What is visible?','project_id':self.project})
        self.assertEqual(422,missing.status_code)
        large={'dom':{'elements':[{'id':'x','role':'text','label':'x'*25_000}]}}
        oversized=self.client.post('/api/commands',json={
            'text':'/screen What is visible?','project_id':self.project,'screen_observation':large,
        })
        self.assertEqual(413,oversized.status_code)

    def test_gui_grounding_binds_model_result_to_fresh_selected_capture_metadata(self):
        raw = bytearray(24)
        raw[:8] = b"\x89PNG\r\n\x1a\n"
        raw[16:20] = (1280).to_bytes(4, "big")
        raw[20:24] = (720).to_bytes(4, "big")
        raw = bytes(raw)
        result = {"status": "GROUNDED", "candidate": {
            "id": "save-1", "label": "Save", "bbox": [0.1, 0.2, 0.3, 0.4], "confidence": 0.9},
            "confidence": 0.9, "reason": None, "visual_context": {"elements": []},
            "model_call": {"model": "ui_venus_mlx_6bit"}}
        frame = {"question": "Find the Save button", "data_url": "data:image/png;base64," +
                 base64.b64encode(raw).decode(), "image_sha256": hashlib.sha256(raw).hexdigest(),
                 "capture_session_id": "capture-a9", "frame_sequence": 17,
                 "observed_at": time.time(), "width": 1280, "height": 720,
                 "display_geometry": {"displayId": 7, "originX": -640.0, "originY": 0.0,
                                      "widthPoints": 640.0, "heightPoints": 360.0},
                 "target_application": {"applicationPid": 445,
                                         "applicationBundleId": "com.apple.TextEdit"},
                 "ocr_observation": {"source": "APPLE_VISION_OCR", "observationId": "ocr-a9",
                                     "imageSha256": hashlib.sha256(raw).hexdigest()},
                 "ocr_duration_ms": 4.5,
                 "ocr_roi": {"source": "APPLE_VISION_OCR", "observation_id": "ocr-a9",
                             "image_sha256": hashlib.sha256(raw).hexdigest(),
                             "target_text": "Save", "target_confidence": 0.98}}
        with patch.object(api.runtime, "ground_gui_target", return_value=result) as ground:
            response = self.client.post("/api/computer/perception/gui-ground", json=frame)
            stale = self.client.post("/api/computer/perception/gui-ground", json={
                **frame, "observed_at": time.time() - 5.0})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(result["candidate"], response.json()["candidate"])
        self.assertIsNone(response.json()["reason"])
        self.assertEqual("capture-a9", response.json()["provenance"]["capture_session_id"])
        self.assertEqual(17, response.json()["provenance"]["frame_sequence"])
        self.assertEqual("com.apple.TextEdit",
                         response.json()["provenance"]["target_application"]["applicationBundleId"])
        ground.assert_called_once()
        self.assertEqual(frame["ocr_observation"], ground.call_args.args[1]["ocr_observation"])
        self.assertEqual(4.5, ground.call_args.args[1]["ocr_duration_ms"])
        self.assertEqual(frame["ocr_roi"], ground.call_args.args[1]["ocr_roi"])
        failed_ocr_attempt = {"source": "APPLE_VISION_OCR", "status": "error",
                              "duration_ms": 8.0, "image_sha256": hashlib.sha256(raw).hexdigest()}
        failure_frame = {key: value for key, value in frame.items()
                         if key not in {"ocr_observation", "ocr_duration_ms", "ocr_roi"}}
        failure_frame["ocr_attempt"] = failed_ocr_attempt
        with patch.object(api.runtime, "ground_gui_target", return_value=result) as failed_ground:
            failed_ocr_response = self.client.post("/api/computer/perception/gui-ground", json=failure_frame)
        self.assertEqual(200, failed_ocr_response.status_code, failed_ocr_response.text)
        self.assertEqual(failed_ocr_attempt, failed_ground.call_args.args[1]["ocr_attempt"])
        self.assertEqual(409, stale.status_code)
        late_value = api.GuiGroundingInput(**{**frame, "observed_at": 100.0})
        with patch.object(api.runtime, "ground_gui_target", return_value=result), \
             patch.object(api.time, "time", side_effect=[100.0, 103.0]):
            late = api.ground_gui_frame(late_value)
        self.assertEqual("GROUNDED", late["status"])
        expired_value = api.GuiGroundingInput(**{**frame, "observed_at": 100.0})
        with patch.object(api.runtime, "ground_gui_target", return_value=result), \
             patch.object(api.time, "time", side_effect=[100.0, 111.0]):
            late = api.ground_gui_frame(expired_value)
        self.assertEqual("INCONCLUSIVE", late["status"])
        self.assertIsNone(late["candidate"])
        self.assertEqual("GUI_GROUNDING_FRAME_STALE_AFTER_INFERENCE", late["reason"])

    def test_gui_grounding_uses_unique_hash_bound_ocr_label_before_slow_ui_venus(self):
        raw = bytearray(24)
        raw[:8] = b"\x89PNG\r\n\x1a\n"
        raw[16:20] = (1280).to_bytes(4, "big")
        raw[20:24] = (720).to_bytes(4, "big")
        raw = bytes(raw)
        image_hash = hashlib.sha256(raw).hexdigest()
        frame = {"question": "Start tracking demo", "data_url": "data:image/png;base64," +
                 base64.b64encode(raw).decode(), "image_sha256": image_hash,
                 "capture_session_id": "capture-ocr-a9", "frame_sequence": 21,
                 "observed_at": time.time(), "width": 1280, "height": 720,
                 "display_geometry": {"displayId": 7, "originX": 0.0, "originY": 0.0,
                                      "widthPoints": 1280.0, "heightPoints": 720.0},
                 "target_application": {"applicationPid": 445,
                                         "applicationBundleId": "local.olcr.fixture"},
                 "ocr_observation": {"source": "APPLE_VISION_OCR",
                     "observationId": "ocr-a9", "coordinateSpace": "normalized_image_top_left",
                     "imageSha256": image_hash, "width": 1280, "height": 720,
                 "elements": [{"text": "Start tracking demo",
                                   "bboxNormalized": [0.2, 0.3, 0.4, 0.4], "confidence": 0.7}]},
                 "ocr_duration_ms": 14.0}
        with patch.object(api.runtime, "ground_gui_target",
                          side_effect=AssertionError("UI-Venus should not run for an exact OCR match")):
            response = self.client.post("/api/computer/perception/gui-ground", json=frame)
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual("GROUNDED", body["status"])
        self.assertEqual("APPLE_VISION_OCR", body["grounding_source"])
        self.assertEqual("Start tracking demo", body["candidate"]["label"])
        self.assertEqual([0.2, 0.3, 0.4, 0.4], body["candidate"]["bbox"])
        self.assertEqual(21, body["provenance"]["frame_sequence"])

        low_confidence_frame = {**frame, "frame_sequence": 22,
                                "ocr_observation": {**frame["ocr_observation"],
                                    "elements": [{**frame["ocr_observation"]["elements"][0],
                                                  "confidence": 0.64}]}}
        fallback = {"status": "INCONCLUSIVE", "candidate": None,
                    "confidence": 0.0, "reason": "GUI_GROUNDING_UNCERTAIN",
                    "model_call": {"model": "ui_venus_mlx_6bit"}}
        with patch.object(api.runtime, "ground_gui_target", return_value=fallback) as ground:
            fallback_response = self.client.post(
                "/api/computer/perception/gui-ground", json=low_confidence_frame)
        self.assertEqual(200, fallback_response.status_code, fallback_response.text)
        self.assertEqual("GUI_GROUNDING_UNCERTAIN", fallback_response.json()["reason"])
        ground.assert_called_once()

    def test_gui_grounding_warmup_uses_authenticated_model_readiness_path(self):
        result = {"status": "READY", "model_call": {"stage": "GUI_GROUNDING_WARMUP"}}
        with patch.object(api.runtime, "warm_gui_grounding", return_value=result) as warm:
            response = self.client.post("/api/computer/perception/gui-warmup")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(result, response.json())
        warm.assert_called_once_with()

    def test_gui_grounding_release_endpoint_releases_the_worker(self):
        with patch.object(api.runtime, "release_gui_grounding_worker",
                          return_value={"status": "RELEASED"}) as release:
            response = self.client.post("/api/computer/perception/gui-release")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("RELEASED", response.json()["status"])
        release.assert_called_once_with()

    def test_semantic_disclosure_route_plans_executes_only_allowlisted_target_then_verifies(self):
        class Decision:
            loaded=True
            def choice(self, *, question, candidates, language='multilingual'):
                return {'tool_id':candidates[0]['tool_id']}
        conversation=self.client.post(f'/api/projects/{self.project}/conversations',json={}).json()['id']
        before={'dom':{'elements':[{'id':'dom-4','role':'disclosure','label':'Project Core Context',
                                    'visible':True,'enabled':True,'expanded':False}],
                       'text':['Project Core Context'],'confidence':1.0,'observation_id':'gui-before-1',
                       'observed_at':time.time(),'session_id':'webview-test-1'}}
        with patch.object(api,'laya_provider',Decision()):
            planned=self.client.post('/api/computer/actions/open-disclosure/plan',json={
                'question':'Open Project Core Context','project_id':self.project,
                'conversation_id':conversation,'observation':before,
            })
            self.assertEqual(200,planned.status_code,planned.text)
            plan=planned.json()
            self.assertEqual('open_disclosure:dom-4',plan['semantic_action']['tool_id'])
            self.assertEqual('DOM',plan['observation_source'])
            self.assertEqual(0,plan['vision_model_calls'])
            self.assertEqual('native_structured',plan['action_route']['mechanism'])
            self.assertEqual('ACTIVE_CONVERSATION_REQUIRED',self.client.post(
                '/api/computer/actions/open-disclosure/plan',json={
                    'question':'Open Project Core Context','project_id':self.project,'observation':before,
                }).json()['detail'])
            after={'dom':{'elements':[{'id':'dom-4','role':'disclosure','label':'Project Core Context',
                                       'visible':True,'enabled':True,'expanded':True}],
                          'text':['Project Core Context'],'confidence':1.0,'observation_id':'gui-after-1',
                          'observed_at':time.time()+0.1,'session_id':'webview-test-1'}}
            executions_before=api.model_telemetry.snapshot()['counters'].get(
                'action_native_structured_executions',0)
            verified=self.client.post('/api/computer/actions/open-disclosure/verify',json={
                'plan_id':plan['plan_id'],'observation':after,
                'execution_receipt':{'source':'native_structured','action_kind':'open_disclosure',
                    'status':'PASS','execution_attempted':True,
                    'before_observation_id':'gui-before-1','before_session_id':'webview-test-1',
                    'after_observation_id':'gui-after-1','after_session_id':'webview-test-1'},
            })
        self.assertEqual(200,verified.status_code,verified.text)
        self.assertTrue(verified.json()['verified'])
        self.assertEqual('open_disclosure',verified.json()['action'])
        self.assertEqual(executions_before+1,api.model_telemetry.snapshot()['counters'][
            'action_native_structured_executions'])

    def test_structured_action_execution_telemetry_rejects_receipts_for_another_dom_observation(self):
        class Decision:
            loaded=True
            def choice(self, *, question, candidates, language='multilingual'):
                return {'tool_id':candidates[0]['tool_id']}
        conversation=self.client.post(f'/api/projects/{self.project}/conversations',json={}).json()['id']
        now=time.time()
        before={'dom':{'elements':[{'id':'dom-4','role':'disclosure','label':'Project Core Context',
                                    'visible':True,'enabled':True,'expanded':False}],
                       'text':['Project Core Context'],'confidence':1.0,'observation_id':'wrong-receipt-before',
                       'observed_at':now,'session_id':'webview-test-1'}}
        after={'dom':{'elements':[{'id':'dom-4','role':'disclosure','label':'Project Core Context',
                                   'visible':True,'enabled':True,'expanded':True}],
                      'text':['Project Core Context'],'confidence':1.0,'observation_id':'wrong-receipt-after',
                      'observed_at':now+0.1,'session_id':'webview-test-1'}}
        with patch.object(api,'laya_provider',Decision()):
            plan=self.client.post('/api/computer/actions/open-disclosure/plan',json={
                'question':'Open Project Core Context','project_id':self.project,
                'conversation_id':conversation,'observation':before}).json()
            executions_before=api.model_telemetry.snapshot()['counters'].get(
                'action_native_structured_executions',0)
            verified=self.client.post('/api/computer/actions/open-disclosure/verify',json={
                'plan_id':plan['plan_id'],'observation':after,
                'execution_receipt':{'source':'native_structured','action_kind':'open_disclosure',
                    'status':'PASS','execution_attempted':True,
                    'before_observation_id':'foreign-observation','before_session_id':'webview-test-1',
                    'after_observation_id':'wrong-receipt-after','after_session_id':'webview-test-1'},
            })
        self.assertEqual(200,verified.status_code,verified.text)
        self.assertTrue(verified.json()['verified'])
        self.assertEqual(executions_before,api.model_telemetry.snapshot()['counters'].get(
            'action_native_structured_executions',0))

    def test_semantic_disclosure_route_rejects_noop_observation(self):
        class Decision:
            loaded=True
            def choice(self, *, question, candidates, language='multilingual'):
                return {'tool_id':candidates[0]['tool_id']}
        conversation=self.client.post(f'/api/projects/{self.project}/conversations',json={}).json()['id']
        before={'dom':{'elements':[{'id':'dom-4','role':'disclosure','label':'Project Core Context',
                                    'visible':True,'enabled':True,'expanded':False}],
                       'observation_id':'gui-before-noop','observed_at':time.time(),'session_id':'webview-test-1'}}
        with patch.object(api,'laya_provider',Decision()):
            plan=self.client.post('/api/computer/actions/open-disclosure/plan',json={
                'question':'Open Project Core Context','project_id':self.project,
                'conversation_id':conversation,'observation':before,
            }).json()
            verify=self.client.post('/api/computer/actions/open-disclosure/verify',json={
                'plan_id':plan['plan_id'],'observation':before,
            })
        self.assertEqual(200,verify.status_code)
        self.assertEqual('FAILED',verify.json()['status'])
        self.assertFalse(verify.json()['verified'])

    def test_accessibility_disclosure_route_binds_permission_and_pid_through_verification(self):
        class Decision:
            loaded=True
            def choice(self, *, question, candidates, language='multilingual'):
                return {'tool_id':candidates[0]['tool_id']}
        conversation=self.client.post(f'/api/projects/{self.project}/conversations',json={}).json()['id']
        before={'ax':{'elements':[{'id':'ax-2','role':'AXDisclosureTriangle','label':'Details',
                                   'frame':{'x':5,'y':10,'width':20,'height':20},'enabled':True,
                                   'expanded':False,'supportedActions':['AXPress']}],
                       'text':['Details'],'observationId':'ax-before','observedAt':time.time(),
                       'sessionId':'macos-ax-123','applicationPid':123,'applicationBundleId':'com.example.demo','permissionGranted':True,
                       'coordinateSpace':'ax_global_screen_coordinates'}}
        with patch.object(api,'laya_provider',Decision()):
            planned=self.client.post('/api/computer/actions/open-disclosure/plan',json={
                'question':'Open Details','project_id':self.project,'conversation_id':conversation,'observation':before,
            })
            self.assertEqual(200,planned.status_code,planned.text)
            plan=planned.json()
            self.assertEqual('accessibility',plan['action_route']['mechanism'])
            wrong_app={'ax':{'elements':[{'id':'ax-2','role':'AXDisclosureTriangle','label':'Details',
                                          'frame':{'x':5,'y':10,'width':20,'height':20},'enabled':True,
                                          'expanded':True,'supportedActions':['AXPress']}],
                             'observationId':'ax-after-wrong','observedAt':time.time()+0.1,
                             'sessionId':'macos-ax-999','applicationPid':999,'applicationBundleId':'com.example.demo','permissionGranted':True,
                             'coordinateSpace':'ax_global_screen_coordinates'}}
            rejected=self.client.post('/api/computer/actions/open-disclosure/verify',json={
                'plan_id':plan['plan_id'],'observation':wrong_app,
            })
            self.assertEqual('AX_APPLICATION_OR_PERMISSION_MISMATCH',rejected.json()['reason'])
            self.assertFalse(any(item.get('status')=='CAPTURED'
                                 for item in api.db.procedures(active_only=False)))
            replay=self.client.post('/api/computer/actions/open-disclosure/verify',json={
                'plan_id':plan['plan_id'],'observation':before,
            })
            self.assertEqual(404,replay.status_code,replay.text)
            replanned=self.client.post('/api/computer/actions/open-disclosure/plan',json={
                'question':'Open Details','project_id':self.project,
                'conversation_id':conversation,'observation':before,
            })
            self.assertEqual(200,replanned.status_code,replanned.text)
            plan=replanned.json()
            after={'ax':{'elements':[{'id':'ax-2','role':'AXDisclosureTriangle','label':'Details',
                                      'frame':{'x':5,'y':10,'width':20,'height':20},'enabled':True,
                                      'expanded':True,'supportedActions':['AXPress']}],
                         'text':['Details'],'observationId':'ax-after','observedAt':time.time()+0.2,
                         'sessionId':'macos-ax-123','applicationPid':123,'applicationBundleId':'com.example.demo','permissionGranted':True,
                         'coordinateSpace':'ax_global_screen_coordinates'}}
            verified=self.client.post('/api/computer/actions/open-disclosure/verify',json={
                'plan_id':plan['plan_id'],'observation':after,
                'execution_receipt':{'source':'native_accessibility','action_kind':'open_disclosure',
                    'status':'PASS','execution_attempted':True},
            })
        self.assertEqual(200,verified.status_code,verified.text)
        self.assertTrue(verified.json()['verified'])
        traces=[item for item in api.db.procedures(active_only=False) if item.get('status')=='CAPTURED']
        self.assertEqual(1,len(traces))
        trace=traces[0]['captured_trace']
        self.assertEqual('CONFIRMED',trace['verification']['result'])
        self.assertEqual('open_disclosure',trace['actions'][0]['kind'])
        self.assertNotIn('text',trace)
        self.assertFalse(traces[0]['validated'])
        duplicate=ProcedureStore(api.db).capture_verified_trace(trace)
        routine_counters_before=api.model_telemetry.snapshot()['counters']
        compiled=self.client.post('/api/routines/candidates/compile',json={
            'trace_ids':[traces[0]['id'],duplicate.id],
        })
        self.assertEqual(200,compiled.status_code,compiled.text)
        self.assertEqual('CANDIDATE',compiled.json()['status'])
        self.assertFalse(compiled.json()['validated'])
        routine_counters_after=api.model_telemetry.snapshot()['counters']
        self.assertEqual(routine_counters_before.get('routine_candidates',0)+1,
                         routine_counters_after['routine_candidates'])
        self.assertEqual(routine_counters_before.get('routines_compiled',0)+1,
                         routine_counters_after['routines_compiled'])
        inventory=self.client.get('/api/routines/candidates').json()
        self.assertEqual(1,len(inventory['candidates']))
        self.assertEqual('open_disclosure',inventory['candidates'][0]['action']['kind'])
        self.assertEqual('accessibility',inventory['candidates'][0]['actions'][0]['mechanism'])
        token='routine-review-token'
        with patch('olcr_api.app._gui_session_token',token):
            self.assertEqual(401,self.client.post(
                f"/api/routines/candidates/{compiled.json()['procedure']['id']}/review",
                json={'decision':'APPROVED'}).status_code)
            reviewed=self.client.post(
                f"/api/routines/candidates/{compiled.json()['procedure']['id']}/review",
                headers={'X-OLCR-Session':token},json={'decision':'APPROVED'})
        self.assertEqual(200,reviewed.status_code,reviewed.text)
        self.assertEqual('APPROVED',reviewed.json()['review']['decision'])
        self.assertFalse(reviewed.json()['validated'])
        self.assertEqual('CANDIDATE',reviewed.json()['status'])
        self.assertEqual(422,self.client.post(
            f"/api/routines/candidates/{compiled.json()['procedure']['id']}/review",
            json={'decision':'ACTIVE'}).status_code)

    def test_routine_match_uses_approved_candidate_and_latest_fresh_world_state(self):
        target={'role':'AXDisclosureTriangle','label':'Details','enabled':True,'expanded':False}
        goal='Open the Details section'
        trace={'trace_version':1,'goal_signature_version':1,
            'goal_signature':routine_goal_signature(goal),
            'context':{'source':'AX','session_id':'old-session','application_pid':123,
                       'application_bundle_id':'com.example.demo',
                       'coordinate_space':'ax_global_screen_coordinates'},
            'preconditions':[target],
            'action':{'kind':'open_disclosure','mechanism':'accessibility'},
            'actions':[{'kind':'open_disclosure','mechanism':'accessibility','target':target}],
            'expected_effects':[{'role':'disclosure','expanded':True}],
            'verification':{'result':'CONFIRMED'}}
        store=ProcedureStore(api.db)
        first=store.capture_verified_trace(trace)
        second=store.capture_verified_trace({**trace,'context':{**trace['context'],
            'session_id':'second-session','application_pid':456}})
        candidate=store.compile_candidate([first.id,second.id])
        observation={'source':'AX','permissionGranted':True,'observationId':'match-observation',
            'observedAt':time.time(),'sessionId':'live-session','coordinateSpace':'ax_global_screen_coordinates',
            'applicationPid':999,'applicationBundleId':'com.example.demo',
            'elements':[{**target,'id':'live-element','frame':{'x':5,'y':10,'width':20,'height':20},
                         'supportedActions':['AXPress']}]}
        saved=self.client.put('/api/desktop/world-state',json={'observation':observation})
        self.assertEqual(200,saved.status_code,saved.text)
        routine_counters_before=api.model_telemetry.snapshot()['counters']
        self.assertEqual('FALLBACK',self.client.post('/api/routines/match',json={'question':goal}).json()['status'])
        fallback_counters=api.model_telemetry.snapshot()['counters']
        self.assertEqual(routine_counters_before.get('routine_match_attempts',0)+1,
                         fallback_counters['routine_match_attempts'])
        self.assertEqual(routine_counters_before.get('routine_fallbacks',0)+1,
                         fallback_counters['routine_fallbacks'])
        store.review_candidate(candidate.id,'APPROVED')
        token='routine-match-token'
        with patch('olcr_api.app._gui_session_token',token):
            self.assertEqual(401,self.client.post('/api/routines/match',json={'question':goal}).status_code)
            response=self.client.post('/api/routines/match',headers={'X-OLCR-Session':token},
                                      json={'question':' OPEN   THE Details section!? '})
        self.assertEqual(200,response.status_code,response.text)
        body=response.json()
        self.assertEqual('CANDIDATE_MATCHED',body['status'])
        self.assertTrue(body['planner_fallback_required'])
        self.assertFalse(body['execution_available'])
        self.assertEqual(candidate.id,body['matches'][0]['procedure_id'])
        self.assertEqual('live-element',body['matches'][0]['element_id'])
        matched_counters=api.model_telemetry.snapshot()['counters']
        self.assertEqual(fallback_counters['routine_match_attempts']+1,
                         matched_counters['routine_match_attempts'])
        self.assertEqual(fallback_counters.get('routine_matches',0)+1,
                         matched_counters['routine_matches'])
        conversation=self.client.post(f'/api/projects/{self.project}/conversations',json={}).json()['id']
        action_observation=observation
        with patch.object(api,'laya_provider',SimpleNamespace(loaded=False)):
            wrong_route=self.client.post('/api/computer/actions/shortcut/open-find/plan',json={
                'question':goal,'project_id':self.project,'conversation_id':conversation,
                'observation':{'ax':action_observation},
            })
            self.assertEqual(503,wrong_route.status_code,wrong_route.text)
            stale=self.client.post('/api/computer/actions/open-disclosure/plan',json={
                'question':goal,'project_id':self.project,'conversation_id':conversation,
                'observation':{'ax':{**action_observation,
                    'elements':[{**action_observation['elements'][0],'label':'Changed'}]}},
            })
            self.assertEqual(503,stale.status_code,stale.text)
            planned=self.client.post('/api/computer/actions/open-disclosure/plan',json={
                'question':goal,'project_id':self.project,'conversation_id':conversation,
                'observation':{'ax':action_observation},
            })
        self.assertEqual(200,planned.status_code,planned.text)
        plan=planned.json()
        self.assertEqual('APPROVED_VERIFIED_ROUTINE',plan['decision']['selection_source'])
        self.assertEqual(candidate.id,plan['routine_candidate_id'])
        self.assertEqual('live-element',plan['selected_element']['id'])
        self.assertEqual('accessibility',plan['action_route']['mechanism'])
        self.assertNotIn('tool_id',plan['semantic_action'])
        after={**action_observation,'observationId':'match-after','observedAt':time.time()+0.1,
               'elements':[{**action_observation['elements'][0],'expanded':True}]}
        rejected=self.client.post('/api/computer/actions/open-disclosure/verify',json={
            'plan_id':plan['plan_id'],'observation':{'ax':{
                key:value for key,value in after.items() if key!='applicationBundleId'
            }},
        })
        self.assertEqual('AX_APPLICATION_OR_PERMISSION_MISMATCH',rejected.json()['reason'])
        replay=self.client.post('/api/computer/actions/open-disclosure/verify',json={
            'plan_id':plan['plan_id'],'observation':{'ax':after},
        })
        self.assertEqual(404,replay.status_code,replay.text)
        replanned=self.client.post('/api/computer/actions/open-disclosure/plan',json={
            'question':goal,'project_id':self.project,'conversation_id':conversation,
            'observation':{'ax':action_observation},
        })
        self.assertEqual(200,replanned.status_code,replanned.text)
        plan=replanned.json()
        verified=self.client.post('/api/computer/actions/open-disclosure/verify',json={
            'plan_id':plan['plan_id'],'observation':{'ax':after},
        })
        self.assertEqual(200,verified.status_code,verified.text)
        self.assertTrue(verified.json()['verified'])
        self.assertTrue(any(item.get('status')=='CAPTURED'
                            for item in api.db.procedures(active_only=False)))

    def test_observed_find_shortcut_routes_and_verifies_same_application_focus(self):
        class Decision:
            loaded=True
            def choice(self, *, question, candidates, language='multilingual'):
                return {'tool_id':candidates[0]['tool_id']}
        conversation=self.client.post(f'/api/projects/{self.project}/conversations',json={}).json()['id']
        before={'ax':{'elements':[{'id':'find-1','role':'AXMenuItem','label':'Find…','enabled':True,
                                   'keyboardShortcut':{'keyCode':3,'modifiers':0,'key':'f','provenance':'application_menu'}}],
                       'observationId':'menu-before','observedAt':time.time(),'sessionId':'macos-ax-123',
                       'applicationPid':123,'applicationBundleId':'com.example.demo','permissionGranted':True,
                       'coordinateSpace':'ax_global_screen_coordinates'}}
        with patch.object(api,'laya_provider',Decision()):
            planned=self.client.post('/api/computer/actions/shortcut/open-find/plan',json={
                'question':'Open the search field','project_id':self.project,
                'conversation_id':conversation,'observation':before,
            })
            self.assertEqual(200,planned.status_code,planned.text)
            plan=planned.json()
            self.assertEqual('keyboard_shortcut',plan['action_route']['mechanism'])
            self.assertEqual('application_menu',plan['keyboard_shortcut']['provenance'])
            wrong_app={'ax':{'elements':[{'id':'search-1','role':'AXTextField','subrole':'AXSearchField',
                                          'label':'Search','focused':True}],
                             'observationId':'menu-after-wrong','observedAt':time.time()+0.1,
                             'sessionId':'macos-ax-999','applicationPid':999,'applicationBundleId':'com.example.demo','permissionGranted':True,
                             'coordinateSpace':'ax_global_screen_coordinates'}}
            rejected=self.client.post('/api/computer/actions/shortcut/open-find/verify',json={
                'plan_id':plan['plan_id'],'observation':wrong_app,
            })
            self.assertEqual('SHORTCUT_APPLICATION_OR_PERMISSION_MISMATCH',rejected.json()['reason'])
            self.assertFalse(any(item.get('status')=='CAPTURED'
                                 for item in api.db.procedures(active_only=False)))
            replay=self.client.post('/api/computer/actions/shortcut/open-find/verify',json={
                'plan_id':plan['plan_id'],'observation':before,
            })
            self.assertEqual(404,replay.status_code,replay.text)
            replanned=self.client.post('/api/computer/actions/shortcut/open-find/plan',json={
                'question':'Open the search field','project_id':self.project,
                'conversation_id':conversation,'observation':before,
            })
            self.assertEqual(200,replanned.status_code,replanned.text)
            plan=replanned.json()
            after={'ax':{'elements':[{'id':'search-1','role':'AXTextField','subrole':'AXSearchField',
                                      'label':'Search','focused':True}],
                         'observationId':'menu-after','observedAt':time.time()+0.2,
                         'sessionId':'macos-ax-123','applicationPid':123,'applicationBundleId':'com.example.demo','permissionGranted':True,
                         'coordinateSpace':'ax_global_screen_coordinates'}}
            verified=self.client.post('/api/computer/actions/shortcut/open-find/verify',json={
                'plan_id':plan['plan_id'],'observation':after,
                'execution_receipt':{'source':'native_accessibility','action_kind':'open_find',
                    'status':'PASS','execution_attempted':True},
            })
        self.assertEqual(200,verified.status_code,verified.text)
        self.assertTrue(verified.json()['verified'])
        traces=[item for item in api.db.procedures(active_only=False) if item.get('status')=='CAPTURED']
        self.assertEqual(1,len(traces))
        self.assertEqual('open_find',traces[0]['captured_trace']['action']['kind'])
        self.assertEqual('application_menu',
                         traces[0]['captured_trace']['actions'][0]['shortcut']['provenance'])

    def test_approved_find_routine_reuses_existing_shortcut_plan_and_verifier(self):
        goal='Open the Find search field'
        target={'role':'AXMenuItem','label':'Find…','enabled':True}
        shortcut={'key_code':3,'modifiers':0,'key':'f','provenance':'application_menu'}
        trace={'trace_version':1,'goal_signature_version':1,
            'goal_signature':routine_goal_signature(goal),
            'context':{'source':'AX','session_id':'old-session','application_pid':123,
                       'application_bundle_id':'com.example.demo',
                       'coordinate_space':'ax_global_screen_coordinates'},
            'preconditions':[target],
            'action':{'kind':'open_find','mechanism':'keyboard_shortcut'},
            'actions':[{'kind':'open_find','mechanism':'keyboard_shortcut','target':target,
                        'shortcut':shortcut}],
            'expected_effects':[{'role':'AXSearchField|AXTextField','focused':True}],
            'verification':{'result':'CONFIRMED'}}
        store=ProcedureStore(api.db)
        first=store.capture_verified_trace(trace)
        second=store.capture_verified_trace({**trace,'context':{**trace['context'],
            'session_id':'second-session','application_pid':456}})
        candidate=store.compile_candidate([first.id,second.id])
        store.review_candidate(candidate.id,'APPROVED')
        observation={'source':'AX','permissionGranted':True,'observationId':'find-match',
            'observedAt':time.time(),'sessionId':'live-find-session',
            'coordinateSpace':'ax_global_screen_coordinates','applicationPid':999,
            'applicationBundleId':'com.example.demo','elements':[{**target,'id':'find-menu',
                'keyboardShortcut':{'keyCode':3,'modifiers':0,'key':'f','provenance':'application_menu'}}]}
        saved=self.client.put('/api/desktop/world-state',json={'observation':observation})
        self.assertEqual(200,saved.status_code,saved.text)
        conversation=self.client.post(f'/api/projects/{self.project}/conversations',json={}).json()['id']
        with patch.object(api,'laya_provider',SimpleNamespace(loaded=False)):
            wrong_route=self.client.post('/api/computer/actions/open-disclosure/plan',json={
                'question':goal,'project_id':self.project,'conversation_id':conversation,
                'observation':{'ax':observation},
            })
            self.assertEqual(503,wrong_route.status_code,wrong_route.text)
            for mismatched_ax in (
                {**observation,'applicationPid':1000},
                {**observation,'applicationBundleId':'com.example.other'},
            ):
                mismatched=self.client.post('/api/computer/actions/shortcut/open-find/plan',json={
                    'question':goal,'project_id':self.project,'conversation_id':conversation,
                    'observation':{'ax':mismatched_ax},
                })
                self.assertEqual(503,mismatched.status_code,mismatched.text)
            planned=self.client.post('/api/computer/actions/shortcut/open-find/plan',json={
                'question':goal,'project_id':self.project,'conversation_id':conversation,
                'observation':{'ax':observation},
            })
        self.assertEqual(200,planned.status_code,planned.text)
        plan=planned.json()
        self.assertEqual('APPROVED_VERIFIED_ROUTINE',plan['decision']['selection_source'])
        self.assertEqual(candidate.id,plan['routine_candidate_id'])
        self.assertEqual('find-menu',plan['selected_element']['id'])
        self.assertEqual('application_menu',plan['keyboard_shortcut']['provenance'])
        before_run=self.client.get('/api/routines/candidates').json()['candidates'][0]['run_stats']
        self.assertEqual(0,before_run['runs'])
        after={'ax':{'elements':[{'id':'find-field','role':'AXTextField','subrole':'AXSearchField',
                                  'label':'Find','focused':True}],
                     'observationId':'find-after','observedAt':time.time()+0.1,
                     'sessionId':'live-find-session','applicationPid':999,
                     'applicationBundleId':'com.example.demo','permissionGranted':True,
                     'coordinateSpace':'ax_global_screen_coordinates'}}
        rejected=self.client.post('/api/computer/actions/shortcut/open-find/verify',json={
            'plan_id':plan['plan_id'],'observation':{'ax':{
                key:value for key,value in after['ax'].items() if key!='applicationBundleId'
            }},
        })
        self.assertEqual('SHORTCUT_APPLICATION_OR_PERMISSION_MISMATCH',rejected.json()['reason'])
        replay=self.client.post('/api/computer/actions/shortcut/open-find/verify',json={
            'plan_id':plan['plan_id'],'observation':after,
        })
        self.assertEqual(404,replay.status_code,replay.text)
        replanned=self.client.post('/api/computer/actions/shortcut/open-find/plan',json={
            'question':goal,'project_id':self.project,'conversation_id':conversation,
            'observation':{'ax':observation},
        })
        self.assertEqual(200,replanned.status_code,replanned.text)
        plan=replanned.json()
        routine_counters_before=api.model_telemetry.snapshot()['counters']
        verified=self.client.post('/api/computer/actions/shortcut/open-find/verify',json={
            'plan_id':plan['plan_id'],'observation':after,
            'execution_receipt':{'source':'native_accessibility','action_kind':'open_find',
                'status':'PASS','execution_attempted':True},
        })
        self.assertEqual(200,verified.status_code,verified.text)
        self.assertTrue(verified.json()['verified'])
        saved_candidate=api.db.procedure(candidate.id)
        self.assertEqual('CANDIDATE',saved_candidate['status'])
        self.assertFalse(saved_candidate['validated'])
        self.assertEqual(1,saved_candidate['run_stats']['runs'])
        self.assertEqual(1,saved_candidate['run_stats']['verified_successes'])
        routine_counters_after=api.model_telemetry.snapshot()['counters']
        self.assertEqual(routine_counters_before.get('routine_runs',0)+1,
                         routine_counters_after['routine_runs'])
        self.assertEqual(routine_counters_before.get('routine_successes',0)+1,
                         routine_counters_after['routine_successes'])
        after_run=self.client.get('/api/routines/candidates').json()['candidates'][0]['run_stats']
        self.assertEqual(1,after_run['verified_successes'])

        second_plan=self.client.post('/api/computer/actions/shortcut/open-find/plan',json={
            'question':goal,'project_id':self.project,'conversation_id':conversation,
            'observation':{'ax':observation},
        })
        self.assertEqual(200,second_plan.status_code,second_plan.text)
        not_confirmed={'ax':{'elements':observation['elements'],'observationId':'find-no-effect',
            'observedAt':time.time()+0.2,'sessionId':'live-find-session','applicationPid':999,
            'applicationBundleId':'com.example.demo','permissionGranted':True,
            'coordinateSpace':'ax_global_screen_coordinates'}}
        no_receipt=self.client.post('/api/computer/actions/shortcut/open-find/verify',json={
            'plan_id':second_plan.json()['plan_id'],'observation':not_confirmed,
        })
        self.assertFalse(no_receipt.json()['routine_failure_recorded'])
        self.assertEqual(1,api.db.procedure(candidate.id)['run_stats']['runs'])
        failure_counters_before=api.model_telemetry.snapshot()['counters']
        attempted=self.client.post('/api/computer/actions/shortcut/open-find/verify',json={
            'plan_id':second_plan.json()['plan_id'],'observation':not_confirmed,
            'execution_receipt':{'source':'native_accessibility','action_kind':'open_find',
                'status':'FAILED','execution_attempted':True},
        })
        self.assertTrue(attempted.json()['routine_failure_recorded'])
        failed_candidate=api.db.procedure(candidate.id)
        self.assertEqual('CANDIDATE',failed_candidate['status'])
        self.assertFalse(failed_candidate['validated'])
        self.assertEqual(2,failed_candidate['run_stats']['runs'])
        self.assertEqual(1,failed_candidate['run_stats']['failures'])
        failure_counters_after=api.model_telemetry.snapshot()['counters']
        self.assertEqual(failure_counters_before['routine_runs']+1,
                         failure_counters_after['routine_runs'])
        self.assertEqual(failure_counters_before.get('routine_failures',0)+1,
                         failure_counters_after['routine_failures'])

        third_plan=self.client.post('/api/computer/actions/shortcut/open-find/plan',json={
            'question':goal,'project_id':self.project,'conversation_id':conversation,
            'observation':{'ax':observation},
        })
        self.assertEqual(200,third_plan.status_code,third_plan.text)
        second_failure=self.client.post('/api/computer/actions/shortcut/open-find/verify',json={
            'plan_id':third_plan.json()['plan_id'],'observation':not_confirmed,
            'execution_receipt':{'source':'native_accessibility','action_kind':'open_find',
                'status':'FAILED','execution_attempted':True},
        })
        self.assertTrue(second_failure.json()['routine_failure_recorded'])
        suspect=api.db.procedure(candidate.id)
        self.assertEqual('SUSPECT',suspect['status'])
        self.assertFalse(suspect['validated'])
        self.assertEqual(2,suspect['run_stats']['consecutive_failures'])
        listed=self.client.get('/api/routines/candidates').json()['candidates']
        self.assertEqual('SUSPECT',next(item for item in listed if item['id']==candidate.id)['status'])
        match=self.client.post('/api/routines/match',json={'question':goal})
        self.assertEqual('FALLBACK',match.json()['status'])
        self.assertEqual([],match.json()['matches'])
        with self.assertRaisesRegex(ValueError,'source traces already compiled'):
            store.compile_candidate([first.id,second.id])

    def test_unconfirmed_routine_receipt_must_match_target_identity(self):
        goal='Open the Find search field'
        target={'role':'AXMenuItem','label':'Find…','enabled':True}
        shortcut={'key_code':3,'modifiers':0,'key':'f','provenance':'application_menu'}
        trace={'trace_version':1,'goal_signature_version':1,'goal_signature':routine_goal_signature(goal),
            'context':{'source':'AX','session_id':'old','application_pid':123,
                'application_bundle_id':'com.example.demo','coordinate_space':'ax_global_screen_coordinates'},
            'preconditions':[target], 'action':{'kind':'open_find','mechanism':'keyboard_shortcut'},
            'actions':[{'kind':'open_find','mechanism':'keyboard_shortcut','target':target,'shortcut':shortcut}],
            'expected_effects':[{'role':'AXSearchField|AXTextField','focused':True}],
            'verification':{'result':'CONFIRMED'}}
        store=ProcedureStore(api.db)
        first=store.capture_verified_trace(trace)
        second=store.capture_verified_trace({**trace,'context':{**trace['context'],'session_id':'old-2'}})
        candidate=store.compile_candidate([first.id,second.id])
        store.review_candidate(candidate.id,'APPROVED')
        before={'source':'AX','permissionGranted':True,'observationId':'id','observedAt':time.time(),
            'sessionId':'session','coordinateSpace':'ax_global_screen_coordinates','applicationPid':999,
            'applicationBundleId':'com.example.demo','elements':[{**target,'id':'menu',
                'keyboardShortcut':{'keyCode':3,'modifiers':0,'key':'f','provenance':'application_menu'}}]}
        self.client.put('/api/desktop/world-state',json={'observation':before})
        conversation=self.client.post(f'/api/projects/{self.project}/conversations',json={}).json()['id']
        planned=self.client.post('/api/computer/actions/shortcut/open-find/plan',json={
            'question':goal,'project_id':self.project,'conversation_id':conversation,
            'observation':{'ax':before}})
        self.assertEqual(200,planned.status_code,planned.text)
        after={'ax':{**before,'observationId':'after','observedAt':time.time()+0.1,
            'elements':[{**target,'id':'menu','keyboardShortcut':{'keyCode':3,'modifiers':0,
                'key':'f','provenance':'application_menu'}}]}}
        after['ax']['applicationPid']=1000
        receipt={'source':'native_accessibility','action_kind':'open_find','status':'FAILED',
            'execution_attempted':True}
        for wrong_target in ({**after['ax'],'applicationPid':1000},
                             {**after['ax'],'applicationBundleId':'com.example.other'}):
            rejected=self.client.post('/api/computer/actions/shortcut/open-find/verify',json={
                'plan_id':planned.json()['plan_id'],'observation':{'ax':wrong_target},
                'execution_receipt':receipt})
            self.assertFalse(rejected.json().get('routine_failure_recorded', False))
        self.assertEqual(0,api.db.procedure(candidate.id)['run_stats']['runs'])

    def test_explicit_skill_loads_lazily_and_executes_pinned_prompt(self):
        api.skills.intake(name='calculator', version='1', source='local',
                          content='Use the deterministic calculator when arithmetic is requested.',
                          trusted=True, description='Calculate arithmetic')
        api.skills.activate('calculator')
        response = self.client.post('/api/commands', json={
            'text': '/skill calculator calculate 2+2', 'project_id': self.project,
        })
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual('skill_execution', payload['kind'])
        self.assertIn('"result": "4"', payload['message'])
        self.assertTrue(payload['data']['skill_loaded'])
        self.assertTrue(payload['data']['execution']['primary_skill_pinned'])

    def test_underspecified_software_request_is_clarified_without_search(self):
        api.rebuild(api.settings.with_overrides({'task_manager_enabled':True}))
        for endpoint in ('/api/chat','/api/chat/stream'):
            conversation=self.client.post(f'/api/projects/{self.project}/conversations').json()['id']
            with patch.object(api.runtime,'execute',side_effect=AssertionError('ambiguous software must not search')), \
                 patch.object(api,'route_external_tool',return_value=('knowledge.wikimedia',{})):
                response=self.client.post(endpoint,json={'message':'献立プランナーをお願い','project_id':self.project,'conversation_id':conversation})
            self.assertEqual(200,response.status_code,response.text)
            self.assertIn('実装を行うか',response.text)
            self.assertEqual([],api.db.coding_tasks(conversation))

    def test_shared_intent_routes_domain_named_software_to_coding_in_chat_and_stream(self):
        api.rebuild(api.settings.with_overrides({'task_manager_enabled':True}))
        requests=[
            'YouTube動画検索アプリを作ってください。',
            'カレンダー管理画面を作ってください。明日の予定を一覧表示します。',
            'CSVに変換する機能を実装して。',
            '最新のドキュメントを検索してから、検索APIを実装してください。',
            'https://example.com から商品情報を収集するスクレイピングアプリを作ってください。',
            'Build a weekly meal planner from scratch, with a searchable recipe list.',
        ]
        for text in requests:
            for endpoint in ('/api/chat','/api/chat/stream'):
                with self.subTest(text=text,endpoint=endpoint):
                    conversation=self.client.post(f'/api/projects/{self.project}/conversations').json()['id']
                    with patch.object(api._coding_scheduler_wake,'set'), \
                         patch.object(api,'_assistant_intent',side_effect=AssertionError('domain router stole coding')) as domain, \
                         patch.object(api,'_chat_file_conversion',side_effect=AssertionError('conversion stole coding')), \
                         patch.object(api,'agent_chat',side_effect=AssertionError('agent stole coding')), \
                         patch.object(api,'chat_scrape_request',side_effect=AssertionError('scrape router stole coding')), \
                         patch.object(api.runtime,'execute',side_effect=AssertionError('normal/search stole coding')) as normal:
                        response=self.client.post(endpoint,json={'message':text,'project_id':self.project,'conversation_id':conversation})
                    self.assertEqual(200,response.status_code,response.text)
                    tasks=api.db.coding_tasks(conversation)
                    self.assertEqual(1,len(tasks));self.assertEqual(text,tasks[0]['original_goal'])
                    normal.assert_not_called();domain.assert_not_called()

    def test_explicit_skill_implementation_reaches_orchestrator_in_all_transports(self):
        api.rebuild(api.settings.with_overrides({'task_manager_enabled':True}))
        api.skills.intake(name='apple-design',version='1',source='local',trusted=True,
                          content='Keep the interface accessible and use clear navigation.')
        api.skills.activate('apple-design')
        goal=('一週間の献立プランナーを、独立したアプリとして一から作ってください。'
              '料理を登録・編集・削除でき、ジャンル、調理時間、予算、アレルゲン、使う食材を設定します。'
              '曜日ごとの朝食・昼食・夕食に割り当て、条件に合う献立を自動作成してください。'
              '同じ料理の連続を避け、予算とアレルギー条件を守り、買い物リストを作成します。'
              '保存して再起動しても復元し、空データ、候補なし、保存エラーを扱ってください。')
        for endpoint in ('/api/commands','/api/chat','/api/chat/stream'):
            with self.subTest(endpoint=endpoint):
                conversation=self.client.post(f'/api/projects/{self.project}/conversations').json()['id']
                text='/skill apple-design\n'+goal
                payload={'project_id':self.project,'conversation_id':conversation,
                         ('text' if endpoint=='/api/commands' else 'message'):text}
                with patch.object(api._coding_scheduler_wake,'set') as wake, \
                     patch.object(api.runtime,'execute',side_effect=AssertionError('normal Brain/search must not own implementation')) as execute, \
                     patch.object(api,'_candidate_retrieval_only_response',side_effect=AssertionError('search must not own implementation')):
                    response=self.client.post(endpoint,json=payload)
                self.assertEqual(200,response.status_code,response.text)
                if endpoint.endswith('/stream'):
                    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                    final=next(e for e in events if e['type']=='done')
                    self.assertEqual('CODING_ORCHESTRATOR',final['response_owner'])
                    task_id=final['task']['task_id']
                else:
                    task_id=response.json()['coding_task_id']
                managed=api.db.coding_task(task_id)
                self.assertEqual(goal,managed['original_goal'])
                self.assertEqual('apple-design',managed['requirements']['explicit_skill']['name'])
                context=api._coding_skill_context(managed)
                self.assertIn('[ACTIVE_SKILL name=apple-design ',context)
                self.assertIn('Keep the interface accessible',context)
                self.assertEqual(text,api.db.conversation(conversation)['messages'][0]['content'])
                self.assertIsNone(api._coding_skill_invocation.get())
                wake.assert_called();execute.assert_not_called()

    def test_explicit_skill_does_not_substitute_an_unavailable_skill(self):
        response = self.client.post('/api/commands', json={
            'text': '/skill unavailable calculate 2+2', 'project_id': self.project,
        })
        self.assertEqual(422, response.status_code)
        self.assertIn('SKILL_NOT_INSTALLED_OR_ENABLED', response.text)

    def test_bare_skill_executes_in_current_project_with_conversation_and_source_evidence(self):
        api.skills.intake(name='project-helper', version='1', source='local', trusted=True,
                          content='Review the current project and give one concrete recommendation.')
        api.skills.activate('project-helper')
        (Path(self.tmp.name) / 'README.md').write_text('Project fixture: meal planner with offline storage.')
        cid = api.db.create_conversation('Review', time.time(), 'skill-project-chat', self.project)
        api.db.add_message(cid, 'user', '保存処理の改善点を教えて', time.time(), 'prior-skill-request')
        with patch.object(api.runtime, 'execute', return_value=(api.Task('skill task'), '保存処理の改善点です。')) as execute:
            response = self.client.post('/api/commands', json={'text': '/skill project-helper', 'conversation_id': cid})
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual('skill_execution', result['kind'])
        self.assertEqual('保存処理の改善点です。', result['message'])
        request = execute.call_args.args[0]
        context = execute.call_args.kwargs['core_context']
        self.assertIn('Use the selected skill project-helper', request)
        self.assertIn('Review the current project', context)
        self.assertIn('保存処理の改善点を教えて', context)
        self.assertIn('meal planner with offline storage', context)
        self.assertEqual(self.tmp.name, execute.call_args.kwargs['workspace_root'])
        self.assertEqual(['user', 'user', 'assistant'], [m['role'] for m in api.db.conversation(cid)['messages']])

    def test_explicit_skill_uses_attached_projectless_folder_and_preserves_prompt(self):
        from olcr_api.working_folders import WorkingFolders
        api.skills.intake(name='editor', version='1', source='local', trusted=True, content='Edit requested files.')
        api.skills.activate('editor')
        cid = api.db.create_conversation('Folder', time.time(), 'skill-folder-chat', None)
        folder = Path(self.tmp.name) / 'actual workspace'
        folder.mkdir()
        (folder / 'README.md').write_text('Use this attached folder only.')
        WorkingFolders(api.db).attach(cid, {'directoryPath': str(folder)})
        prompt = 'README.md を修正して。  内容は保持する。'
        with patch.object(api.runtime, 'execute', return_value=(api.Task('edit'), 'Edited.')) as execute:
            result = self.client.post('/api/commands', json={'text': '/skill editor ' + prompt, 'conversation_id': cid})
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual(prompt, execute.call_args.args[0])
        self.assertEqual(str(folder), execute.call_args.kwargs['workspace_root'])
        self.assertIn('Use this attached folder only.', execute.call_args.kwargs['core_context'])
        self.assertIsNone(api.db.conversation(cid)['project_id'])

    def test_skill_chat_ingress_uses_pinned_execution_before_unrelated_intent_routes(self):
        api.skills.intake(name='helper', version='1', source='local', trusted=True, content='Use the chosen skill.')
        api.skills.activate('helper')
        for endpoint in ('/api/chat', '/api/chat/stream'):
            with self.subTest(endpoint=endpoint), \
                 patch.object(api, '_assistant_intent', side_effect=AssertionError('Skill owns this request')), \
                 patch.object(api.runtime, 'execute', return_value=(api.Task('answer'), 'Skill answer.')) as execute:
                response = self.client.post(endpoint, json={'message': '/skill helper Explain timer code', 'project_id': self.project})
            self.assertEqual(200, response.status_code, response.text)
            execute.assert_called_once()
            self.assertIn('[ACTIVE_SKILL name=helper ', execute.call_args.kwargs['core_context'])
            self.assertEqual('Explain timer code', execute.call_args.args[0])
            if endpoint.endswith('/stream'):
                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                self.assertEqual(['meta', 'chunk', 'done'], [e['type'] for e in events])
                self.assertEqual('Skill answer.', events[1]['text'])
                self.assertEqual('EXPLICIT_SKILL', events[-1]['response_owner'])
                cid = events[0]['conversation_id']
            else:
                self.assertEqual('Skill answer.', response.json()['response'])
                cid = response.json()['conversation_id']
            self.assertEqual(['user', 'assistant'], [m['role'] for m in api.db.conversation(cid)['messages']])

    def test_bare_skill_without_project_executes_instead_of_returning_registration_data(self):
        api.skills.intake(name='helper', version='1', source='local', trusted=True, content='Answer user questions.')
        api.skills.activate('helper')
        with patch.object(api.runtime, 'execute', return_value=(api.Task('answer'), '何を検討しますか？')) as execute:
            response = self.client.post('/api/commands', json={'text': '/skill helper', 'project_id': None})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('何を検討しますか？', response.json()['message'])
        self.assertIsNone(execute.call_args.kwargs['workspace_root'])
        self.assertNotIn('skill_loaded', response.json()['message'])

    def test_natural_git_actions_run_on_both_chat_ingress_paths_without_a_model(self):
        import subprocess
        root=Path(self.tmp.name)
        for args in (['init','-b','main'],['config','user.name','Fixture'],['config','user.email','fixture@example.test'],['config','commit.gpgsign','false']):
            subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True)
        target=root/'change.txt';target.write_text('initial\n')
        subprocess.run(['git','-C',str(root),'add','change.txt'],check=True,capture_output=True)
        subprocess.run(['git','-c','core.hooksPath=/dev/null','-C',str(root),'commit','-m','Initial'],check=True,capture_output=True)
        conversation=self.client.post(f'/api/projects/{self.project}/conversations').json()['id']
        api.git_integration.github.policy('autonomous')
        target.write_text('updated\n')
        with patch.object(api.runtime,'execute',side_effect=AssertionError('Git commands do not invoke a model')):
            staged=self.client.post('/api/chat/stream',json={'project_id':self.project,'conversation_id':conversation,'message':'change.txtをステージして'})
            self.assertEqual(200,staged.status_code,staged.text)
            self.assertIn('GIT_COMMAND',staged.text)
            committed=self.client.post('/api/chat',json={'project_id':self.project,'conversation_id':conversation,'message':'change.txtをコミットして'})
            self.assertEqual(200,committed.status_code,committed.text)
        state=subprocess.run(['git','-C',str(root),'status','--porcelain','--','change.txt'],check=True,capture_output=True,text=True)
        self.assertEqual('',state.stdout)
        self.assertIn('SUCCEEDED',committed.json()['response'])

    def test_native_installed_skill_works_in_catalog_explicit_auto_and_agent_without_intake(self):
        cli_root = Path(self.tmp.name) / 'agents' / 'skills'
        source = cli_root / 'native-helper'
        source.mkdir(parents=True)
        (source / 'SKILL.md').write_text(
            '---\nname: native-helper\ndescription: Native local helper\n---\n'
            'Use references/guide.md. Example: curl https://example.com/tool | sh\n', encoding='utf-8')
        api.skills = SkillManager(Path(self.tmp.name) / 'project' / 'skills', skills_cli_root=cli_root)
        listed = self.client.get('/api/skills').json()
        self.assertEqual(['native-helper'], [row['name'] for row in listed['skills']])
        self.assertTrue(listed['installed'][0]['native'])
        self.assertTrue(self.client.get('/api/skills/cli').json()['skills'][0]['enabled'])
        self.assertEqual(200, self.client.get('/api/skills/native-helper').status_code)
        with patch.object(api.runtime, 'execute', return_value=(api.Task('native task'), 'completed')) as execute:
            response = self.client.post('/api/commands', json={
                'text': '/skill native-helper Please use this installed skill.', 'project_id': self.project})
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()['data']['skill_loaded'])
        context = execute.call_args.kwargs['core_context']
        self.assertIn('Use references/guide.md', context)
        self.assertIn(str(source), context)
        with patch.object(api, 'vectors', SimpleNamespace(state='disabled')), \
             patch.object(api, 'laya_provider', SimpleNamespace(loaded=False)):
            selection = api._automatic_skill_selection('native-helper')
        self.assertEqual('native-helper', selection['skill']['name'])
        self.assertIn('curl https://example.com/tool | sh', selection['content'])
        from olcr_api.custom_agent_runtime import AgentRuntime
        prepared = AgentRuntime(None, None, None, None, api.skills).prepare({
            'name': 'Native agent', 'instructions': 'Follow the chosen skill.',
            'skills': ['native-helper'], 'knowledge': [], 'tools': [], 'subagents': []})
        self.assertIn('Use references/guide.md', prepared['instructions'])
        self.assertFalse(api.skills.registry_path.exists())
        self.assertFalse(api.skills.quarantine.exists())

    def test_skills_cli_sources_are_discoverable_and_imported_only_to_quarantine(self):
        cli_root = Path(self.tmp.name) / 'agents' / 'skills'
        source_dir = cli_root / 'read-only-helper'
        source_dir.mkdir(parents=True)
        (source_dir / 'SKILL.md').write_text(
            '---\nname: read-only-helper\ndescription: Local helper\n---\nUse local evidence only.\n',
            encoding='utf-8',
        )
        global_root = Path(self.tmp.name) / 'global' / '.olcr' / 'skills'
        quarantine = Path(self.tmp.name) / 'global' / '.olcr' / 'quarantine' / 'skills'
        api.skills = SkillManager(Path(self.tmp.name) / 'project' / '.olcr' / 'skills',
                                  global_root=global_root, quarantine_root=quarantine,
                                  skills_cli_root=cli_root)

        inventory = self.client.get('/api/skills/cli').json()['skills']
        self.assertEqual(['read-only-helper'], [row['name'] for row in inventory])
        reviewed = self.client.get('/api/skills/cli/read-only-helper/review')
        self.assertEqual(200, reviewed.status_code)
        review = reviewed.json()
        self.assertEqual('PASS', review['static_scan']['status'])
        self.assertEqual('read-only-helper', review['name'])
        self.assertIn('Use local evidence only', review['content'])
        self.assertFalse((global_root / 'registry.json').exists())
        self.assertFalse(quarantine.exists())
        intake = self.client.post('/api/skills/cli/read-only-helper/intake', json={
            'trusted': False, 'expected_hash': review['content_hash'],
        })
        self.assertEqual(200, intake.status_code)
        self.assertEqual('QUARANTINED', intake.json()['status'])
        self.assertFalse(intake.json()['skill']['enabled'])
        self.assertFalse(intake.json()['skill']['trusted'])
        self.assertTrue((global_root / 'registry.json').is_file())
        self.assertFalse((global_root / 'read-only-helper.skill').exists())
        self.assertEqual(422, self.client.post('/api/skills/read-only-helper/activate',
                                               json={'permission': 'explicit'}).status_code)

        trusted_intake = self.client.post('/api/skills/cli/read-only-helper/intake', json={
            'trusted': True, 'expected_hash': review['content_hash'],
        })
        self.assertEqual(200, trusted_intake.status_code)
        activated = self.client.post('/api/skills/read-only-helper/activate',
                                     json={'permission': 'explicit'})
        self.assertEqual(200, activated.status_code)
        self.assertEqual('ACTIVE', activated.json()['status'])
        self.assertTrue((global_root / 'read-only-helper.skill').is_file())

    def test_cli_skill_review_and_intake_fail_closed_when_source_changes(self):
        cli_root = Path(self.tmp.name) / 'agents' / 'skills'
        source_dir = cli_root / 'read-only-helper'
        source_dir.mkdir(parents=True)
        skill_file = source_dir / 'SKILL.md'
        skill_file.write_text('---\nname: read-only-helper\n---\nOriginal body.\n', encoding='utf-8')
        api.skills = SkillManager(Path(self.tmp.name) / 'skills', skills_cli_root=cli_root)
        review = self.client.get('/api/skills/cli/read-only-helper/review').json()
        skill_file.write_text('---\nname: read-only-helper\n---\nChanged body.\n', encoding='utf-8')
        intake = self.client.post('/api/skills/cli/read-only-helper/intake', json={
            'trusted': True, 'expected_hash': review['content_hash'],
        })
        self.assertEqual(422, intake.status_code)
        self.assertIn('SKILL_CLI_SOURCE_CHANGED', intake.text)
        self.assertEqual([], api.skills.registry())

    def test_automatic_skill_keeps_embedding_top_k_for_laya_and_persists_route_evidence(self):
        for name, description, content in (
            ('calculator', 'Deterministic arithmetic Skill', 'Use the calculator for arithmetic.'),
            ('find-skills', 'Installed Skills CLI discovery workflow', 'Find an installed Skill for a requested task.'),
        ):
            api.skills.intake(name=name, version='1', source='local', content=content,
                              trusted=True, description=description)
            api.skills.activate(name)

        embedding = SimpleNamespace(embed=lambda _texts, _model: [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
        choices = []
        def laya_choice(**kwargs):
            choices.append(kwargs)
            return {'tool_id': 'calculator'}
        laya = SimpleNamespace(loaded=True, choice=laya_choice)
        normal_task = api.Task('Please use the calculator skill automatically to compute 23*19.')
        conversation = self.client.post(f'/api/projects/{self.project}/conversations').json()['id']

        with patch.object(api, 'vectors', SimpleNamespace(state='ready')), \
             patch.object(api, '_api_embedding_provider', embedding), \
             patch.object(api, 'laya_provider', laya), \
             patch.object(api.runtime, 'execute', return_value=(normal_task, '437')), \
             patch.object(api.conversation_memory, 'index_completed_turn'), \
             patch.object(api.conversation_memory, 'backfill'):
            response = self.client.post('/api/chat', json={
                'project_id': self.project,
                'conversation_id': conversation,
                'message': 'Please use the calculator skill automatically to compute 23*19.',
            })

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(choices))
        self.assertEqual({'calculator', 'find-skills'}, {item['tool_id'] for item in choices[0]['candidates']})
        self.assertEqual('english', choices[0]['language'])
        task_id = response.json()['task']['id']
        with api.db.connect() as database:
            row = database.execute('SELECT selected_context_json FROM tasks WHERE id=?', (task_id,)).fetchone()
        evidence = json.loads(row['selected_context_json'])[-1]['skill_routing']
        self.assertEqual('embedding', evidence['retrieval'])
        self.assertEqual({'calculator', 'find-skills'}, {item['name'] for item in evidence['candidates']})
        self.assertTrue(evidence['laya_called'])
        self.assertEqual('PASS', evidence['laya_status'])
        self.assertEqual('calculator', evidence['laya_selected'])
        self.assertEqual('calculator', evidence['selected_skill'])

    def test_local_skill_source_code_research_does_not_auto_load_a_skill(self):
        prompt = (
            'This is not a coding task. Search the local repository for the function that retrieves '
            'Skill candidates and summarize its ranking stages. Do not modify files or use external services.'
        )
        conversation = self.client.post(f'/api/projects/{self.project}/conversations').json()['id']
        with patch.object(api, '_automatic_skill_selection', side_effect=AssertionError('source research is not Skill use')), \
             patch.object(api.runtime, 'execute', return_value=(api.Task(prompt), 'Local source summary.')) as run, \
             patch.object(api.conversation_memory, 'index_completed_turn'), \
             patch.object(api.conversation_memory, 'backfill'):
            response = self.client.post('/api/chat', json={
                'project_id': self.project,
                'conversation_id': conversation,
                'message': prompt,
            })

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('Local source summary.', response.json()['response'])
        self.assertEqual(prompt, run.call_args.args[0])

    def test_image_chat_does_not_inject_cross_conversation_memory(self):
        prompt='Describe the landscape and visible objects in the attached image.'
        conversation=self.client.post(f'/api/projects/{self.project}/conversations').json()['id']
        contexts=[]
        def execute_image(text,image,core_context=''):
            contexts.append(core_context)
            return api.Task(text),'A grounded image description.'
        with patch.object(api.conversation_memory,'search',side_effect=AssertionError('image turns must not load cross-conversation memory')), \
             patch.object(api.runtime,'execute_image',side_effect=execute_image), \
             patch.object(api.conversation_memory,'index_completed_turn'), \
             patch.object(api.conversation_memory,'backfill'):
            response=self.client.post('/api/chat',json={
                'project_id':self.project,
                'conversation_id':conversation,
                'message':prompt,
                'image':{'name':'scene.png','mime_type':'image/png','data_url':'data:image/png;base64,aW1hZ2U='},
            })
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual('A grounded image description.',response.json()['response'])
        self.assertEqual(1,len(contexts))
        self.assertNotIn('[CONVERSATION_MEMORY]',contexts[0])

    def test_automatic_skill_chat_excludes_unrelated_project_core_context(self):
        api.skills.intake(name='calculator', version='1', source='local',
                          content='Use arithmetic only and explain the calculation.',
                          trusted=True, description='Deterministic arithmetic Skill')
        api.skills.activate('calculator')
        prompt = ('これはコーディングタスクではありません。次の依頼に適した、'
                  'インストール済みのローカル計算スキルを使って回答してください。'
                  'コマンド・ネットワーク・ファイル変更は行わないでください。'
                  '依頼: 23×19はいくつですか？')
        core = self.client.put(f'/api/projects/{self.project}/core-context', json={
            'content': 'Tetris reference: the drop interval is 800 ms. Keep this unrelated fact out of calculator answers.',
        })
        self.assertEqual(200, core.status_code, core.text)
        conversation = self.client.post(f'/api/projects/{self.project}/conversations').json()['id']
        embedding = SimpleNamespace(embed=lambda _texts, _model: [[1.0, 0.0], [1.0, 0.0]])
        laya = SimpleNamespace(loaded=True, choice=lambda **_kwargs: {'tool_id':'calculator'})
        runtime_contexts = []
        def execute_with_context(text, approved=False, core_context='', **_kwargs):
            runtime_contexts.append(ContextManager(api.settings.context_budget)._select_core(text, core_context))
            return api.Task(text), 'calculator'

        with patch.object(api, 'vectors', SimpleNamespace(state='ready')), \
             patch.object(api, '_api_embedding_provider', embedding), \
             patch.object(api, 'laya_provider', laya), \
             patch.object(api.runtime, 'execute', side_effect=execute_with_context), \
             patch.object(api.conversation_memory, 'index_completed_turn'), \
             patch.object(api.conversation_memory, 'backfill'):
            response = self.client.post('/api/chat', json={
                'project_id': self.project,
                'conversation_id': conversation,
                'message': prompt,
            })

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, len(runtime_contexts))
        self.assertIn('[ACTIVE_SKILL name=calculator', runtime_contexts[0])
        self.assertNotIn('Tetris', runtime_contexts[0])
        self.assertNotIn('800 ms', runtime_contexts[0])

    def test_automatic_skill_skips_unrelated_low_similarity_request(self):
        rows = [
            {'name':'calculator','description':'Deterministic arithmetic Skill','source':'local'},
            {'name':'find-skills','description':'Installed Skills CLI discovery workflow','source':'local'},
        ]
        # The observed live ambiguity prompt scored 0.255 and 0.214 for these
        # Skills. Keep both below the empirically established 0.40 route floor.
        embedding = SimpleNamespace(embed=lambda _texts, _model: [
            [1.0, 0.0],
            [0.25, (1.0 - 0.25**2) ** 0.5],
            [0.20, (1.0 - 0.20**2) ** 0.5],
        ])
        choices = []
        laya = SimpleNamespace(loaded=True, choice=lambda **kwargs: choices.append(kwargs))
        with patch.object(api.skills, 'search', return_value=rows), \
             patch.object(api.skills, 'load') as load, \
             patch.object(api, 'vectors', SimpleNamespace(state='ready')), \
             patch.object(api, '_api_embedding_provider', embedding), \
             patch.object(api, 'laya_provider', laya):
            result = api._automatic_skill_selection(
                'The image shows two similar green circles. Is either one visibly darker?')
        self.assertIsNone(result)
        self.assertEqual([], choices)
        load.assert_not_called()

    def test_selection_only_skill_requests_load_one_ranked_candidate_without_executing_or_reranking(self):
        rows = [
            {'name':'find-skills','description':'Installed Skills CLI discovery workflow','source':'local'},
            {'name':'calculator','description':'Deterministic arithmetic Skill','source':'local'},
        ]
        prompts = [
            ('/api/chat',
             'これはコーディングタスクではありません。日本語の仕様要約に最も適するローカルSkill候補を、登録済み候補の中から一つ選び、Skill名と短い理由だけを日本語で答えてください。Skillは実行しないでください。',
             'find-skills — 仕様を直接要約するSkillではありませんが、要約向けSkillを探せるため、登録候補では最も近い選択です。'),
            ('/api/chat/stream',
             'This is not a coding task. Choose exactly one currently registered local Skill candidate for producing a concise summary of an English technical specification. Reply with only its name and one-sentence reason. Do not execute the Skill.',
             'find-skills — It can help locate a summarization Skill, but does not summarize technical specifications itself.'),
        ]
        embedding = SimpleNamespace(embed=lambda _texts, _model: [
            [1.0, 0.0], [1.0, 0.0], [0.6, 0.8],
        ])
        choices = []
        laya = SimpleNamespace(
            loaded=True,
            choice=lambda **kwargs: (choices.append(kwargs) or {'tool_id':'find-skills'}),
        )

        for endpoint, prompt, expected in prompts:
            with self.subTest(endpoint=endpoint):
                conversation = self.client.post(
                    f'/api/projects/{self.project}/conversations'
                ).json()['id']
                with patch.object(api.skills, 'search', return_value=rows), \
                 patch.object(api.skills, 'load', return_value={
                     'content': 'Use find-skills instructions as read-only context.'
                     if endpoint.endswith('/stream') else 'Japanese find-skills instructions.'
                 }) as load, \
                     patch.object(api, 'vectors', SimpleNamespace(state='ready')), \
                     patch.object(api, '_api_embedding_provider', embedding), \
                     patch.object(api, 'laya_provider', laya), \
                     patch.object(api.runtime, 'execute', side_effect=AssertionError(
                         'selection-only Skill requests must not invoke Normal Brain')) as execute, \
                     patch.object(api.conversation_memory, 'index_completed_turn'), \
                     patch.object(api.conversation_memory, 'backfill'):
                    response = self.client.post(endpoint, json={
                        'project_id': self.project,
                        'conversation_id': conversation,
                        'message': prompt,
                    })

                self.assertEqual(200, response.status_code, response.text)
                if endpoint.endswith('/stream'):
                    chunks = [
                        json.loads(line[6:])['text']
                        for line in response.text.splitlines()
                        if line.startswith('data: ') and json.loads(line[6:]).get('type') == 'chunk'
                    ]
                    visible = ''.join(chunks)
                else:
                    visible = response.json()['response']
                self.assertEqual(expected, visible)
                load.assert_called_once_with('find-skills')
                self.assertEqual(
                    {'find-skills', 'calculator'},
                    {item['tool_id'] for item in choices[-1]['candidates']},
                )
                execute.assert_not_called()

    def test_streaming_automatic_skill_uses_shared_embedding_laya_pipeline(self):
        for name, description, content in (
            ('calculator', 'Deterministic arithmetic Skill', 'Use the calculator for arithmetic.'),
            ('find-skills', 'Installed Skills CLI discovery workflow', 'Find an installed Skill for a requested task.'),
        ):
            api.skills.intake(name=name, version='1', source='local', content=content,
                              trusted=True, description=description)
            api.skills.activate(name)

        embedding = SimpleNamespace(embed=lambda _texts, _model: [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
        choices = []
        def laya_choice(**kwargs):
            choices.append(kwargs)
            return {'tool_id': 'calculator'}
        laya = SimpleNamespace(loaded=True, choice=laya_choice)
        normal_task = api.Task('Please use the calculator skill automatically to compute 23*19.')
        conversation = self.client.post(f'/api/projects/{self.project}/conversations').json()['id']

        with patch.object(api, 'vectors', SimpleNamespace(state='ready')), \
             patch.object(api, '_api_embedding_provider', embedding), \
             patch.object(api, 'laya_provider', laya), \
             patch.object(api.runtime, 'execute', return_value=(normal_task, '437')), \
             patch.object(api.conversation_memory, 'index_completed_turn'), \
             patch.object(api.conversation_memory, 'backfill'):
            response = self.client.post('/api/chat/stream', json={
                'project_id': self.project,
                'conversation_id': conversation,
                'message': 'Please use the calculator skill automatically to compute 23*19.',
            })

        self.assertEqual(200, response.status_code)
        self.assertIn('SHARED_SKILL_ROUTER', response.text)
        self.assertIn('437', response.text)
        self.assertEqual(1, len(choices))
        self.assertEqual({'calculator', 'find-skills'}, {item['tool_id'] for item in choices[0]['candidates']})
        self.assertEqual('english', choices[0]['language'])
        with api.db.connect() as database:
            row = database.execute(
                "SELECT t.id,t.selected_context_json FROM tasks t "
                "JOIN messages m ON m.task_id=t.id WHERE m.conversation_id=? AND m.role='assistant'",
                (conversation,),
            ).fetchone()
        self.assertIsNotNone(row)
        evidence = json.loads(row['selected_context_json'])[-1]['skill_routing']
        self.assertEqual('embedding', evidence['retrieval'])
        self.assertTrue(evidence['laya_called'])
        self.assertEqual('PASS', evidence['laya_status'])
        self.assertEqual('calculator', evidence['laya_selected'])
        self.assertEqual('calculator', evidence['selected_skill'])

    def test_streaming_source_summary_expands_full_workspace_function(self):
        source_path = Path(self.tmp.name) / 'ranking.py'
        source_path.write_text(
            'def _automatic_skill_selection(message):\n'
            '    """Rank Skill candidates for a request."""\n'
            '    candidates = retrieve_candidates(message)\n'
            '    lexical = score_token_overlap(message, candidates)\n'
            '    embedded = compare_query_to_each_candidate(lexical)\n'
            '    chosen = laya_choose(embedded)\n'
            "    padding = '" + 'x' * 1100 + "'\n"
            '    return chosen\n',
            encoding='utf-8',
        )
        evidence = SimpleNamespace(
            source=str(source_path), line=1,
            snippet='1: def _automatic_skill_selection(message):\n'
                    '2:     candidates = retrieve_candidates(message)\n',
            method='ranked_source', score=4.25,
        )
        conversation = self.client.post(
            f'/api/projects/{self.project}/conversations'
        ).json()['id']
        prompt = (
            'This is not a coding task. Search for the function that retrieves Skill candidates '
            'and summarize its ranking stages. Use only this local repository; no external services.'
        )
        with patch.object(api.retrieval, 'retrieve', return_value=([evidence], 'ranked_source')), \
             patch.object(api, '_requests_automatic_skill_selection', return_value=False), \
             patch.object(api.runtime.model, 'generate',
                          return_value=iter([{'text': 'It retrieves, scores, compares, and selects candidates.'}])):
            response = self.client.post('/api/chat/stream', json={
                'project_id': self.project,
                'conversation_id': conversation,
                'message': prompt,
            })

        self.assertEqual(200, response.status_code, response.text)
        with api.db.connect() as database:
            row = database.execute(
                'SELECT selected_context_json FROM tasks WHERE conversation_id=? '
                'ORDER BY created_at DESC LIMIT 1', (conversation,),
            ).fetchone()
        selected = json.loads(row['selected_context_json'])
        self.assertEqual('ranked_source_complete_function', selected[0]['method'])
        self.assertIn('    return chosen', selected[0]['text'])
        self.assertGreater(len(selected[0]['text']), 1000)

    def test_coding_task_manager_setting_is_global_and_persistent(self):
        # A new installation with no persisted preference enables chat routing.
        with patch.dict(os.environ, {}, clear=True):
            fresh = Settings.from_env()
            self.assertTrue(fresh.task_manager_enabled)
            self.assertTrue(fresh.with_overrides({}).task_manager_enabled)

        # The explicit preference is global and survives a normal reload.
        before = self.client.get('/api/settings').json()
        self.assertIn('task_manager_enabled', before)
        self.assertFalse(before['task_manager_enabled'])
        on_response = self.client.put('/api/settings', json={**before, 'task_manager_enabled': True})
        self.assertEqual(200, on_response.status_code)
        self.assertTrue(on_response.json()['task_manager_enabled'])
        reopened = Database(api.db.path)
        reopened.initialize()
        self.assertTrue(reopened.load_settings()['task_manager_enabled'])
        api.reload_settings()
        self.assertTrue(api.settings.task_manager_enabled)

        off_response = self.client.put('/api/settings', json={**on_response.json(), 'task_manager_enabled': False})
        response = off_response
        self.assertEqual(200, response.status_code)
        self.assertFalse(response.json()['task_manager_enabled'])
        self.assertFalse(reopened.load_settings()['task_manager_enabled'])
        api.reload_settings()
        self.assertFalse(api.settings.task_manager_enabled)

    def test_task_manager_off_bypasses_managed_creation_and_keeps_safe_non_manager_path(self):
        api.rebuild(api.settings.with_overrides({'task_manager_enabled': False}))
        conversation = self.client.post(f'/api/projects/{self.project}/conversations').json()['id']
        request = 'このリポジトリに認証機能を実装してください'
        normal_task = api.Task(request)
        with patch.object(api._coding_scheduler_wake, 'set') as wake, \
             patch.object(api.runtime, 'execute', return_value=(normal_task, '通常のBrain経路で回答しました。')) as execute:
            response = self.client.post('/api/chat', json={'project_id': self.project, 'conversation_id': conversation, 'message': request})
        self.assertEqual(200, response.status_code)
        self.assertFalse(api.settings.task_manager_enabled)
        self.assertEqual([], api.db.coding_tasks(conversation))
        self.assertFalse(wake.called)
        execute.assert_called_once()
        self.assertEqual('通常のBrain経路で回答しました。', response.json()['response'])

    def test_orchestrator_off_coding_writes_files_with_long_implementation_model(self):
        calls=[]
        def generate(messages, model, stream=False, **kwargs):
            calls.append({'model':model,'format':kwargs.get('format')})
            prompt='\n'.join(str(message.get('content') or '') for message in messages)
            path='stream-app.html' if 'stream-app.html' in prompt else 'chat-app.html'
            if kwargs.get('format'):
                text=json.dumps({'change_required':True,'source_inspected':True,'condition_evaluated':True,
                    'reason_code':'direct_coding_test','operations':[{'op':'write','path':path,
                    'content':'<!doctype html><html lang="ja"><meta charset="utf-8"><title>動作確認</title><h1>作成できました</h1></html>'}],
                    'verification':'read the created HTML file'})
            else:
                text='この環境ではプロジェクトのファイルを直接作成・変更できません。'
            result={'text':text,'model':model,'done':True}
            return iter([result]) if stream else result

        for endpoint, path in (('/api/chat','chat-app.html'),('/api/chat/stream','stream-app.html')):
            with self.subTest(endpoint=endpoint):
                conversation=self.client.post(f'/api/projects/{self.project}/conversations').json()['id']
                request=f'独立したアプリとして {path} を新規作成してください。ファイルに実装してください。'
                with patch.object(api.runtime.model,'generate',side_effect=generate), \
                     patch.object(api._coding_scheduler_wake,'set') as wake:
                    response=self.client.post(endpoint,json={'project_id':self.project,
                        'conversation_id':conversation,'message':request})
                self.assertEqual(200,response.status_code,response.text)
                self.assertTrue((Path(self.tmp.name)/path).is_file(),response.text)
                self.assertEqual('role:long_implementation',calls[-1]['model'])
                self.assertIsNotNone(calls[-1]['format'])
                if endpoint.endswith('/stream'):
                    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                    meta=next(event for event in events if event['type']=='meta')
                    done=next(event for event in events if event['type']=='done')
                    self.assertTrue(meta.get('task_id'))
                    self.assertEqual('DIRECT_CODING_EXECUTION',done['response_owner'])
                    self.assertEqual('completed',done['task']['state'])
                wake.assert_not_called()
                self.assertEqual([],api.db.coding_tasks(conversation))

    def test_task_manager_on_uses_existing_managed_creation_path(self):
        api.rebuild(api.settings.with_overrides({'task_manager_enabled': True}))
        conversation = self.client.post(f'/api/projects/{self.project}/conversations').json()['id']
        request = 'このリポジトリに認証機能を実装してください'
        with patch.object(api._coding_scheduler_wake, 'set') as wake, \
             patch.object(api.runtime, 'execute', side_effect=AssertionError('managed route must not fall through')):
            response = self.client.post('/api/chat', json={'project_id': self.project, 'conversation_id': conversation, 'message': request})
        self.assertEqual(200, response.status_code)
        self.assertTrue(api.settings.task_manager_enabled)
        self.assertTrue(response.json().get('coding_task_id'))
        self.assertEqual('QUEUED', api.db.coding_task(response.json()['coding_task_id'])['status'])
        self.assertTrue(wake.called)
    def test_web_validation_and_secret(self):
        with patch.dict('os.environ',{'OLCR_WEB_BRAVE_API_KEY':'secret-test-value'}):
            response=self.client.get('/api/web/settings')
            self.assertNotIn('secret-test-value',response.text)
            self.assertEqual('Configured',response.json()['credentials']['brave'])
        before=api.settings.web_provider
        self.assertEqual(422,self.client.put('/api/web/settings',json={'web_mode':'auto','web_provider':'invalid'}).status_code)
        self.assertEqual(before,api.settings.web_provider)
    def test_context_isolation_reload_and_security(self):
        path=Path(self.tmp.name)/'core.txt';path.write_text('Project A context')
        base=f'/api/projects/{self.project}/core-context'
        self.assertEqual(200,self.client.post(base+'/load',json={'path':str(path)}).status_code)
        workspace_b=Path(self.tmp.name)/'workspace-B';workspace_b.mkdir()
        b=self.client.post('/api/projects',json={'name':'B','workspace_path':str(workspace_b)}).json()['id']
        self.assertEqual('',self.client.get(f'/api/projects/{b}/core-context').json()['content'])
        self.assertEqual('Project A context',self.client.get(base).json()['content'])
        path.write_text('reloaded')
        self.assertEqual('reloaded',self.client.post(base+'/reload').json()['content'])
        link=Path(self.tmp.name)/'link.txt';link.symlink_to(path)
        self.assertEqual(403,self.client.post(base+'/load',json={'path':str(link)}).status_code)
        self.assertEqual(422,self.client.post(base+'/load',json={'path':str(path.parent/'..'/'other')}).status_code)
        reopened=Database(api.db.path);reopened.initialize()
        self.assertEqual('reloaded',reopened.load_settings()['project_core_context:'+self.project])
