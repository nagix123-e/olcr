import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory()
os.environ["OLCR_DB_PATH"] = str(Path(_tmp.name) / "api.db")
os.environ["OLCR_ALLOWED_ROOTS"] = _tmp.name

try:
    from fastapi.testclient import TestClient
    import olcr_api.app as api
    from olcr_api.app import app, conversation_memory_constraint, db, settings
    from olcr_cli.main import VERSION as CLI_VERSION
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "FastAPI test dependencies not installed")
class APITests(unittest.TestCase):
    def test_handsfree_transcription_stays_local_even_with_remote_audio_configuration(self):
        import time
        from olcr_api.handsfree import HandsFreeManager
        with tempfile.TemporaryDirectory() as root:
            manager = HandsFreeManager(Path(root)); manager.session = 'human'; manager.last_seen = time.monotonic()
            events = [{'type': 'text', 'text': 'テスト'}, {'type': 'done', 'text': 'テスト'}]
            with patch.object(api, 'handsfree', manager), patch.object(api.purpose_models, 'choice', side_effect=AssertionError('remote configuration must not be consulted')), patch('olcr_api.audio_transcription.stream_transcribe_audio', return_value=(event for event in events)) as local:
                response = self.client.post('/api/audio/transcribe/stream', json={
                    'audio_base64': 'fixture', 'language': 'ja', 'handsfree_session': 'human'})
                self.assertEqual(200, response.status_code, response.text)
                self.assertIn('テスト', response.text)
                self.assertEqual(('fixture', 'ja'), local.call_args.args)
                self.assertIsInstance(local.call_args.kwargs['cancelled'], __import__('threading').Event)
            manager.stop()

    def test_handsfree_off_blocks_input_and_local_transcription_without_model_calls(self):
        from olcr_api.handsfree import HandsFreeManager
        with tempfile.TemporaryDirectory() as root:
            manager = HandsFreeManager(Path(root))
            with patch.object(api, 'handsfree', manager), patch.object(api.purpose_models, 'choice') as choice:
                state = self.client.get('/api/handsfree').json()
                self.assertFalse(state['enabled'])
                self.assertFalse(manager.root.exists())
                rejected = self.client.post('/api/handsfree/input', json={'session': 'stale', 'op': 'wake'})
                self.assertEqual(422, rejected.status_code)
                audio = self.client.post('/api/audio/transcribe/stream', json={
                    'audio_base64': '', 'language': 'ja', 'handsfree_session': 'stale'})
                self.assertEqual(409, audio.status_code)
                choice.assert_not_called()
                invalid = self.client.post('/api/handsfree/enable', json={'language': 'es'})
                self.assertEqual(409, invalid.status_code)

    def test_chat_list_field_definitions_cover_requested_directory_columns(self):
        fields = api._chat_list_field_definitions(
            "List each commercial facility name, address, and category")
        self.assertEqual({"facility_name", "address", "category"}, set(fields))
        self.assertTrue(all(item == {"selector": "*", "extract": "text"}
                            for item in fields.values()))
        self.assertEqual({"address": {"selector": "*", "extract": "text"},
                          "category": {"selector": "*", "extract": "text"}},
                         api._chat_list_field_definitions("住所とカテゴリを抽出"))

    def test_native_goal_state_persists_and_cancel_blocks_late_model_actions(self):
        import time
        import uuid
        import json
        conversation_id = str(uuid.uuid4())
        api.db.create_conversation("Native state", time.time(), conversation_id, None)
        original = api.settings
        self.addCleanup(setattr, api, "settings", original)
        api.settings = original.with_overrides({"computer_use_enabled": True,
            "computer_use_allowed_bundle_ids": ["example.target"]})
        ax = {"permissionGranted": True, "applicationPid": 42, "applicationBundleId": "example.target",
              "observedAt": time.time(), "observationId": "state-1", "sessionId": "state-session",
              "text": [], "elements": []}
        payload = {"question": "Open settings", "conversation_id": conversation_id,
                   "capture_session_id": "capture-1", "observation": {"ax": ax}}
        created = self.client.post("/api/computer/goals", json=payload)
        self.assertEqual(200, created.status_code, created.text)
        task_id = created.json()["task_id"]
        state = self.client.get(f"/api/computer/goals/{task_id}", params={"conversation_id": conversation_id})
        self.assertEqual("executing", state.json()["state"])
        cancelled = self.client.patch(f"/api/computer/goals/{task_id}", json={
            "conversation_id": conversation_id, "state": "cancelled"})
        self.assertEqual("cancelled", cancelled.json()["state"])
        with patch.object(api.runtime.model, "generate") as generate:
            late = self.client.post("/api/computer/actions/goal/next", json={
                **payload, "goal_task_id": task_id})
        self.assertEqual(409, late.status_code)
        generate.assert_not_called()
        wrong = self.client.get(f"/api/computer/goals/{task_id}", params={"conversation_id": "other"})
        self.assertEqual(404, wrong.status_code)

    def test_slow_goal_inference_revalidates_once_against_fresh_screen(self):
        import time
        import uuid
        import json
        conversation_id = str(uuid.uuid4())
        api.db.create_conversation("Slow goal", time.time(), conversation_id, None)
        original = api.settings
        self.addCleanup(setattr, api, "settings", original)
        api.settings = original.with_overrides({"computer_use_enabled": True,
            "computer_use_allowed_bundle_ids": ["example.target"]})
        clock = [time.time()]
        ax = {"permissionGranted": True, "applicationPid": 42, "applicationBundleId": "example.target",
              "observedAt": clock[0], "observationId": "before", "sessionId": "session",
              "text": ["Waiting"], "elements": []}
        payload = {"question": 'Wait for "Done"', "conversation_id": conversation_id,
                   "observation": {"ax": ax}}
        def slow_generate(*args, **kwargs):
            clock[0] += 31
            return {"text": json.dumps({"decision": "complete", "completion_text": "Done"})}
        with patch.object(api.time, "time", side_effect=lambda: clock[0]), \
                patch.object(api.runtime.model, "generate", side_effect=slow_generate) as generate:
            proposed = self.client.post("/api/computer/actions/goal/next", json=payload)
            self.assertEqual("REOBSERVE", proposed.json()["status"], proposed.text)
            token = proposed.json()["proposal_id"]
            fresh = {**payload, "proposal_id": token, "observation": {"ax": {
                **ax, "observedAt": clock[0], "observationId": "after", "text": ["Done"]}}}
            # The proposal cannot be rebound to another native observation session.
            wrong = {**fresh, "observation": {"ax": {**fresh["observation"]["ax"], "sessionId": "other"}}}
            self.assertEqual(403, self.client.post("/api/computer/actions/goal/validate", json=wrong).status_code)
            validated = self.client.post("/api/computer/actions/goal/validate", json=fresh)
            self.assertEqual("COMPLETE", validated.json()["status"], validated.text)
            self.assertTrue(validated.json()["verified"])
            self.assertEqual(409, self.client.post("/api/computer/actions/goal/validate", json=fresh).status_code)
            generate.assert_called_once()

    def test_projectless_mixed_goal_uses_existing_text_plan_and_receipt_verification(self):
        import time
        import uuid
        import json
        conversation_id = str(uuid.uuid4())
        api.db.create_conversation("Native goal", time.time(), conversation_id, None)
        original = api.settings
        self.addCleanup(setattr, api, "settings", original)
        api.settings = original.with_overrides({"computer_use_enabled": True,
            "computer_use_allowed_bundle_ids": ["example.target"]})
        ax = {"elements": [{"id": "field", "role": "AXTextField", "label": "Search",
              "visible": True, "enabled": True, "focused": True}],
              "text": ["Search"], "confidence": 1.0, "observedAt": time.time(),
              "observationId": "goal-before", "sessionId": "goal-session",
              "applicationPid": 123, "applicationBundleId": "example.target", "permissionGranted": True}
        payload = {"question": 'Search for "bird" and verify results', "project_id": None,
                   "conversation_id": conversation_id, "observation": {"ax": ax}}
        generated = {"decision": "execute", "kind": "type_text", "target_label": "Search", "input_text": "bird"}
        with patch.object(api.runtime.model, "generate", return_value={"text": "```json\n" + json.dumps(generated) + "\n```"}) as generate:
            next_step = self.client.post("/api/computer/actions/goal/next", json=payload)
        self.assertEqual("role:planner_replanner", generate.call_args.args[1])
        self.assertEqual(256, generate.call_args.kwargs["max_tokens"])
        self.assertEqual(200, next_step.status_code, next_step.text)
        self.assertEqual("type_text", next_step.json()["action"]["kind"])
        self.assertIn("Output JSON schema", generate.call_args.args[0][0]["content"])
        plan = self.client.post("/api/computer/actions/text/plan", json={**payload,
            "target_label": "Search", "input_text": "bird", "record_user_message": False})
        self.assertEqual(200, plan.status_code, plan.text)
        after = {**ax, "observedAt": time.time() + .01, "observationId": "goal-after"}
        verified = self.client.post("/api/computer/actions/text/verify", json={
            "plan_id": plan.json()["plan_id"], "observation": {"ax": after},
            "execution_receipt": {"source": "native_accessibility", "action_kind": "type_text",
                "action": "AXSetValue", "status": "PASS", "execution_attempted": True,
                "element_id": "field", "value_matched": True, "verified": True}})
        self.assertEqual(200, verified.status_code, verified.text)
        self.assertTrue(verified.json()["verified"])
        self.assertNotIn("bird", json.dumps(api._pending_semantic_action_plans.get(plan.json()["plan_id"], {})))
        payload["observation"]["ax"]["applicationBundleId"] = "local.olcr.desktop"
        with patch.object(api.runtime.model, "generate") as generate:
            rejected = self.client.post("/api/computer/actions/goal/next", json=payload)
        self.assertEqual(403, rejected.status_code)
        generate.assert_not_called()

    def test_computer_use_launch_authorization_requires_global_enable_and_rejects_olcr(self):
        original = api.settings
        self.addCleanup(setattr, api, "settings", original)
        api.settings = original.with_overrides({
            "computer_use_enabled": True,
            "computer_use_allowed_bundle_ids": ["com.apple.Calculator"],
        })
        allowed = self.client.post("/api/computer-use/authorize-launch", json={
            "application_bundle_id": "com.apple.Calculator",
        })
        self.assertEqual(200, allowed.status_code, allowed.text)
        self.assertEqual({"status": "AUTHORIZED", "application_bundle_id": "com.apple.Calculator"}, allowed.json())
        self.assertEqual(200, self.client.post("/api/computer-use/authorize-launch", json={
            "application_bundle_id": "com.example.Unlisted",
        }).status_code)
        api.settings = original.with_overrides({"computer_use_enabled": False,
            "computer_use_allowed_bundle_ids": ["com.apple.Calculator"]})
        self.assertEqual(403, self.client.post("/api/computer-use/authorize-launch", json={
            "application_bundle_id": "com.apple.Calculator",
        }).status_code)
        api.settings = original.with_overrides({"computer_use_enabled": True,
            "computer_use_allowed_bundle_ids": ["local.olcr.desktop"]})
        self.assertEqual(422, self.client.post("/api/computer-use/authorize-launch", json={
            "application_bundle_id": "local.olcr.desktop",
        }).status_code)

    def test_computer_use_launch_authorization_binds_project_app_path_and_bundle_id(self):
        import plistlib
        import time
        import uuid

        original = api.settings
        self.addCleanup(setattr, api, "settings", original)
        api.settings = original.with_overrides({
            "computer_use_enabled": True,
            "computer_use_allowed_bundle_ids": ["local.example.game"],
        })
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as outside:
            app_bundle = Path(workspace) / "build" / "Game.app"
            app_bundle.joinpath("Contents").mkdir(parents=True)
            with app_bundle.joinpath("Contents", "Info.plist").open("wb") as stream:
                plistlib.dump({"CFBundlePackageType": "APPL",
                    "CFBundleIdentifier": "local.example.game"}, stream)
            project_id = str(uuid.uuid4())
            api.db.create_project("Scoped app fixture", workspace, time.time(), project_id)
            authorized = self.client.post("/api/computer-use/authorize-launch", json={
                "application_path": str(app_bundle), "project_id": project_id,
            })
            self.assertEqual(200, authorized.status_code, authorized.text)
            self.assertEqual({"status": "AUTHORIZED",
                "application_bundle_id": "local.example.game",
                "application_path": str(app_bundle.resolve())}, authorized.json())

            outside_app = Path(outside) / "Other.app"
            outside_app.joinpath("Contents").mkdir(parents=True)
            with outside_app.joinpath("Contents", "Info.plist").open("wb") as stream:
                plistlib.dump({"CFBundlePackageType": "APPL",
                    "CFBundleIdentifier": "local.example.game"}, stream)
            rejected_path = self.client.post("/api/computer-use/authorize-launch", json={
                "application_path": str(outside_app), "project_id": project_id,
            })
            self.assertEqual(403, rejected_path.status_code)
            self.assertEqual("COMPUTER_USE_APP_PATH_OUTSIDE_PROJECT", rejected_path.json()["detail"])

            mismatch = self.client.post("/api/computer-use/authorize-launch", json={
                "application_path": str(app_bundle), "project_id": project_id,
                "application_bundle_id": "local.example.other",
            })
            self.assertEqual(422, mismatch.status_code)

    def test_computer_use_scope_update_persists_without_replacing_toggle(self):
        original = api.settings
        self.addCleanup(setattr, api, "settings", original)
        api.settings = original.with_overrides({"computer_use_enabled": True,
            "computer_use_allowed_bundle_ids": []})
        response = self.client.put("/api/computer-use/scope", json={
            "allowed_bundle_ids": ["com.apple.TextEdit"],
        })
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(["com.apple.TextEdit"], response.json()["computer_use_allowed_bundle_ids"])
        self.assertEqual(["com.apple.TextEdit"],
            api.db.load_application_settings()["computer_use_allowed_bundle_ids"])
        self.assertTrue(api.settings.computer_use_enabled)
        capability = self.client.patch("/api/settings/capabilities", json={
            "web_scraping_enabled": True,
        })
        self.assertEqual(200, capability.status_code, capability.text)
        self.assertTrue(capability.json()["web_scraping_enabled"])
        self.assertEqual(["com.apple.TextEdit"], capability.json()["computer_use_allowed_bundle_ids"])
        self.assertEqual(["com.apple.TextEdit"],
            api.db.load_application_settings()["computer_use_allowed_bundle_ids"])
        rejected = self.client.put("/api/computer-use/scope", json={
            "allowed_bundle_ids": ["local.olcr.desktop"],
        })
        self.assertEqual(422, rejected.status_code)

    def test_computer_use_scope_applies_to_authorization_without_model_reload(self):
        original = api.settings
        self.addCleanup(setattr, api, "settings", original)
        api.settings = original.with_overrides({"computer_use_enabled": True,
            "computer_use_allowed_bundle_ids": []})
        with patch.object(api, "rebuild", side_effect=AssertionError("scope must not reload models")):
            response = self.client.put("/api/computer-use/scope", json={
                "allowed_bundle_ids": ["com.apple.TextEdit"],
            })
            self.assertEqual(200, response.status_code, response.text)
            allowed = self.client.post("/api/computer-use/authorize-launch", json={
                "application_bundle_id": "com.apple.TextEdit",
            })
            self.assertEqual(200, allowed.status_code, allowed.text)
            self.assertEqual(200, self.client.post("/api/computer-use/authorize-launch", json={
                "application_bundle_id": "com.apple.Calculator",
            }).status_code)

    def test_chat_native_button_plan_requires_fresh_effect_and_consumes_plan(self):
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Button action fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)
        api.db.add_message(conversation_id, "user",
            "Start Computer Use, then Start the game and verify Started", now, "native-start-source")

        class DecisionProvider:
            loaded = True
            @staticmethod
            def choice(*, question, candidates, language="multilingual"):
                return {"tool_id": candidates[0]["tool_id"]}

        original_laya = api.laya_provider
        self.addCleanup(setattr, api, "laya_provider", original_laya)
        api.laya_provider = DecisionProvider()
        before = {"elements": [
            {"id": "start", "role": "AXButton", "label": "Start", "visible": True,
             "enabled": True, "supportedActions": ["AXPress"]},
            {"id": "disabled", "role": "AXButton", "label": "Start", "visible": True,
             "enabled": False, "supportedActions": ["AXPress"]},
        ], "text": ["Idle"], "confidence": 1.0, "observationId": "button-before",
            "observedAt": now, "sessionId": "button-session", "applicationPid": 123,
            "applicationBundleId": "example.target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        payload = {"question": 'Start the game and verify "Started"',
            "target_label": "Start", "expected_text": "Started", "project_id": project_id,
            "conversation_id": conversation_id, "record_user_message": False,
            "observation": {"ax": before}}
        planned = self.client.post("/api/computer/actions/button/plan", json=payload)
        self.assertEqual(200, planned.status_code, planned.text)
        self.assertEqual(["Start Computer Use, then Start the game and verify Started"],
            [message["content"] for message in api.db.conversation(conversation_id)["messages"]
             if message["role"] == "user"])
        plan = planned.json()
        self.assertEqual("start", plan["selected_element"]["id"])
        self.assertEqual("accessibility", plan["action_route"]["mechanism"])
        after = {**before, "observationId": "button-after", "observedAt": now + 0.1,
                 "text": ["Started"]}
        receipt = {"source": "native_accessibility", "action_kind": "press_button",
            "status": "PASS", "execution_attempted": True}
        verified = self.client.post("/api/computer/actions/button/verify", json={
            "plan_id": plan["plan_id"], "observation": {"ax": after}, "execution_receipt": receipt})
        self.assertEqual(200, verified.status_code, verified.text)
        self.assertTrue(verified.json()["verified"])
        self.assertIn("Started", verified.json()["message"])
        replay = self.client.post("/api/computer/actions/button/verify", json={
            "plan_id": plan["plan_id"], "observation": {"ax": after}, "execution_receipt": receipt})
        self.assertEqual(404, replay.status_code)

    def test_chat_native_text_entry_is_exact_focused_non_secure_and_does_not_echo_value(self):
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Text entry fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)
        before = {"elements": [{"id": "search", "role": "AXTextField", "label": "Search",
            "visible": True, "enabled": True, "focused": True}], "text": [], "confidence": 1.0,
            "observationId": "text-entry-before", "observedAt": now, "sessionId": "text-entry-session",
            "applicationPid": 321, "applicationBundleId": "example.text-entry",
            "permissionGranted": True, "coordinateSpace": "ax_global_screen_coordinates"}
        payload = {"question": 'Type "red fox" into the "Search" field.',
            "target_label": "Search", "input_text": "red fox", "project_id": project_id,
            "conversation_id": conversation_id, "record_user_message": False,
            "observation": {"ax": before}}
        planned = self.client.post("/api/computer/actions/text/plan", json=payload)
        self.assertEqual(200, planned.status_code, planned.text)
        plan = planned.json()
        self.assertEqual("search", plan["selected_element"]["id"])
        self.assertNotIn("red fox", planned.text)
        self.assertNotIn("input_text", api._pending_semantic_action_plans[plan["plan_id"]])
        after = {**before, "observationId": "text-entry-after", "observedAt": now + 0.1}
        receipt = {"source": "native_accessibility", "action_kind": "type_text",
            "action": "AXSetValue", "element_id": "search", "value_matched": True,
            "status": "PASS", "execution_attempted": True, "verified": True}
        verified = self.client.post("/api/computer/actions/text/verify", json={
            "plan_id": plan["plan_id"], "observation": {"ax": after}, "execution_receipt": receipt})
        self.assertEqual(200, verified.status_code, verified.text)
        self.assertTrue(verified.json()["verified"])
        self.assertNotIn("red fox", verified.text)
        replay = self.client.post("/api/computer/actions/text/verify", json={
            "plan_id": plan["plan_id"], "observation": {"ax": after}, "execution_receipt": receipt})
        self.assertEqual(404, replay.status_code)

    def test_chat_native_text_entry_plan_accepts_unfocused_but_rejects_secure_and_ambiguous_controls(self):
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Text entry rejection fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)
        base = {"elements": [], "confidence": 1.0, "observationId": "text-reject-before",
            "observedAt": now, "sessionId": "text-reject-session", "applicationPid": 654,
            "applicationBundleId": "example.text-reject", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        payload = {"question": "type value", "target_label": "Search", "input_text": "value",
            "project_id": project_id, "conversation_id": conversation_id,
            "observation": {"ax": base}}
        unfocused = self.client.post("/api/computer/actions/text/plan", json={**payload,
            "observation": {"ax": {**base, "elements": [{"id": "search", "role": "AXTextField",
                "label": "Search", "visible": True, "enabled": True, "focused": False}]}}})
        self.assertEqual(200, unfocused.status_code, unfocused.text)
        self.assertEqual("search", unfocused.json()["selected_element"]["id"])
        for elements, expected_reason in (
            ([{"id": "password", "role": "AXSecureTextField", "label": "Search",
              "visible": True, "enabled": True, "focused": True}], "SECURE_TEXT_ENTRY_UNAVAILABLE"),
            ([{"id": "search-a", "role": "AXTextField", "label": "Search",
              "visible": True, "enabled": True, "focused": True},
              {"id": "search-b", "role": "AXTextArea", "label": "Search",
              "visible": True, "enabled": True, "focused": True}], "TEXT_TARGET_NOT_UNIQUE"),
        ):
            response = self.client.post("/api/computer/actions/text/plan", json={**payload,
                "observation": {"ax": {**base, "elements": elements}}})
            self.assertEqual(422, response.status_code, response.text)
            self.assertEqual(expected_reason, response.json()["detail"])

    def test_chat_native_keypress_plan_binds_allowlisted_key_and_verifies_new_ax_text(self):
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Keypress fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)
        window = {"id": "game-window", "role": "AXWindow", "label": "Game",
                  "visible": True, "enabled": True, "focused": True}
        base = {"elements": [window], "text": [], "confidence": 1.0,
            "observationId": "key-before", "observedAt": now, "sessionId": "key-session",
            "applicationPid": 654, "applicationBundleId": "example.keypress",
            "permissionGranted": True, "coordinateSpace": "ax_global_screen_coordinates"}
        planned = self.client.post("/api/computer/actions/keyboard/key/plan", json={
            "question": "Press Space and verify Started", "key": "Space",
            "expected_text": "Started", "project_id": project_id,
            "conversation_id": conversation_id, "observation": {"ax": base}})
        self.assertEqual(200, planned.status_code, planned.text)
        self.assertEqual("deterministic_input", planned.json()["action_route"]["mechanism"])
        self.assertEqual("game-window", planned.json()["selected_element"]["id"])
        after = {**base, "observationId": "key-after", "observedAt": now + 0.1,
            "text": ["Started"],
            "elements": [window, {"id": "started", "role": "AXStaticText",
                "label": "Started", "visible": True, "enabled": True}]}
        verify_payload = {"plan_id": planned.json()["plan_id"], "observation": {"ax": after},
            "execution_receipt": {"source": "native_accessibility", "action_kind": "press_key",
                "status": "PASS", "execution_attempted": True,
                "action": "CGEventKeyboard", "key": "Space", "element_id": "game-window"}}
        verified = self.client.post("/api/computer/actions/keyboard/key/verify", json=verify_payload)
        self.assertEqual(200, verified.status_code, verified.text)
        self.assertTrue(verified.json()["verified"], verified.text)
        self.assertEqual("PASS", verified.json()["status"])

    def test_chat_native_tab_focus_plan_requires_bound_dispatch_and_fresh_focus_transition(self):
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Tab focus fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)
        current = {"id": "name", "role": "AXTextField", "label": "Name",
            "visible": True, "enabled": True, "focused": True}
        target = {"id": "continue", "role": "AXButton", "label": "Continue",
            "visible": True, "enabled": True, "focused": False}
        before = {"elements": [current, target], "text": [], "confidence": 1.0,
            "observationId": "tab-api-before", "observedAt": now,
            "sessionId": "tab-api-session", "applicationPid": 789,
            "applicationBundleId": "example.tab-fixture", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        payload = {"question": 'Press Tab to move focus to "Continue" and verify it receives focus.',
            "target_label": "Continue", "project_id": project_id,
            "conversation_id": conversation_id, "record_user_message": False,
            "observation": {"ax": before}}
        planned = self.client.post("/api/computer/actions/keyboard/tab/plan", json=payload)
        self.assertEqual(200, planned.status_code, planned.text)
        plan = planned.json()
        self.assertEqual("continue", plan["selected_element"]["id"])
        self.assertEqual("name", plan["current_focus_element"]["id"])
        self.assertEqual("keyboard_shortcut", plan["action_route"]["mechanism"])

        after = {**before, "observationId": "tab-api-after", "observedAt": now + 0.1,
            "elements": [{**current, "focused": False}, {**target, "focused": True}]}
        receipt = {"source": "native_accessibility", "action_kind": "press_tab",
            "status": "PASS", "execution_attempted": True}
        verified = self.client.post("/api/computer/actions/keyboard/tab/verify", json={
            "plan_id": plan["plan_id"], "observation": {"ax": after},
            "execution_receipt": receipt})
        self.assertEqual(200, verified.status_code, verified.text)
        self.assertTrue(verified.json()["verified"])
        self.assertEqual("press_tab", verified.json()["action"])
        replay = self.client.post("/api/computer/actions/keyboard/tab/verify", json={
            "plan_id": plan["plan_id"], "observation": {"ax": after},
            "execution_receipt": receipt})
        self.assertEqual(404, replay.status_code)

    def test_chat_native_wheel_scroll_requires_bound_dispatch_and_fresh_position_change(self):
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Wheel scroll fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)
        area = {"id": "results", "role": "AXScrollArea", "label": "Results",
            "visible": True, "depth": 1, "frame": {"x": 10, "y": 20, "width": 400, "height": 300}}
        peer = {"id": "toolbar", "role": "AXGroup", "label": "Toolbar", "visible": True,
            "depth": 1, "frame": {"x": 10, "y": 0, "width": 400, "height": 20}}
        bar = {"id": "results-vbar", "role": "AXScrollBar", "label": "Scrollbar",
            "visible": True, "depth": 2, "scrollValue": 0.4,
            "frame": {"x": 398, "y": 30, "width": 12, "height": 280}}
        before = {"elements": [area, peer, bar], "observationId": "scroll-api-before",
            "observedAt": now, "sessionId": "scroll-api-session", "applicationPid": 879,
            "applicationBundleId": "example.scroll-api", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates",
            "windowFrame": {"x": 0, "y": 0, "width": 800, "height": 600}}
        planned = self.client.post("/api/computer/actions/scroll/plan", json={
            "question": 'Scroll down in the "Results" area.', "target_label": "Results",
            "direction": "down", "project_id": project_id, "conversation_id": conversation_id,
            "observation": {"ax": before, "pointer_event_permission_granted": True},
            "record_user_message": False})
        self.assertEqual(200, planned.status_code, planned.text)
        plan = planned.json()
        self.assertEqual("results", plan["selected_element"]["id"])
        self.assertEqual("results-vbar", plan["selected_element"]["scrollbar_id"])
        self.assertEqual(0.4, plan["scroll_value"])

        after = {**before, "observationId": "scroll-api-after", "observedAt": now + 0.2,
            "elements": [area, peer, {**bar, "scrollValue": 0.6}]}
        receipt = {"source": "native_accessibility", "action_kind": "scroll_wheel",
            "action": "CGEventScrollWheel", "direction": "down", "element_id": "results",
            "scrollbar_element_id": "results-vbar", "status": "PASS", "execution_attempted": True}
        verified = self.client.post("/api/computer/actions/scroll/verify", json={
            "plan_id": plan["plan_id"], "observation": {"ax": after}, "execution_receipt": receipt})
        self.assertEqual(200, verified.status_code, verified.text)
        self.assertTrue(verified.json()["verified"])
        self.assertIn("下にスクロール", verified.json()["message"])
        replay = self.client.post("/api/computer/actions/scroll/verify", json={
            "plan_id": plan["plan_id"], "observation": {"ax": after}, "execution_receipt": receipt})
        self.assertEqual(404, replay.status_code)

    def test_chat_native_button_without_axpress_requires_pointer_receipt_and_verifies_effect(self):
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Vision button fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)

        class DecisionProvider:
            loaded = True
            @staticmethod
            def choice(*, question, candidates, language="multilingual"):
                return {"tool_id": candidates[0]["tool_id"]}

        original_laya = api.laya_provider
        self.addCleanup(setattr, api, "laya_provider", original_laya)
        api.laya_provider = DecisionProvider()
        target = {"applicationPid": 123, "applicationBundleId": "example.target"}
        before = {"elements": [{"id": "start", "role": "AXButton", "label": "Start",
            "visible": True, "enabled": True, "supportedActions": []}], "text": ["Idle"],
            "confidence": 1.0, "observationId": "visual-button-before", "observedAt": now,
            "sessionId": "visual-button-session", **target, "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        geometry = {"displayId": 1, "originX": 0, "originY": 0,
            "widthPoints": 1440, "heightPoints": 900}
        vision_pointer = {"candidate": {"id": "start-frame-box", "label": "Start",
                "bbox": [0.05, 0.05, 0.15, 0.15], "confidence": 0.9},
            "provenance": {"capture_session_id": "visual-capture", "frame_sequence": 8,
                "tracked_frame_sequence": 9, "observed_at": now, "image_sha256": "b" * 64,
                # Native Retina capture is in pixels while AX/display geometry is in points.
                "width": 2880, "height": 1800, "target_application": target,
                "display_geometry": geometry},
            "alignment": {"status": "READY", "captureSessionId": "visual-capture",
                "targetApplication": target, "seedSequence": 8, "verifiedSequence": 9,
                "verifiedAt": now, "displayId": 1, "trackingSource": "OPENCV_LUCAS_KANADE",
                "trackingConfidence": 0.9, "targetWindowContainsPoint": True,
                "mappedGlobalPoint": [120, 80]}}
        base_payload = {"question": 'Click "Start" and verify "Started"', "target_label": "Start",
            "expected_text": "Started", "project_id": project_id,
            "conversation_id": conversation_id}
        requested_grounding = self.client.post("/api/computer/actions/button/plan", json={
            **base_payload, "observation": {"ax": before, "pointer_event_permission_granted": True}})
        self.assertEqual(200, requested_grounding.status_code, requested_grounding.text)
        self.assertEqual("VISION_GROUNDING_REQUIRED", requested_grounding.json()["status"])
        planned = self.client.post("/api/computer/actions/button/plan", json={
            **base_payload, "observation": {"ax": before, "pointer_event_permission_granted": True,
                "vision_pointer": vision_pointer}})
        self.assertEqual(200, planned.status_code, planned.text)
        plan = planned.json()
        self.assertEqual("vision_pointer", plan["action_route"]["mechanism"])

        after = {**before, "observationId": "visual-button-after", "observedAt": now + 0.1,
            "text": ["Started"]}
        receipt = {"source": "native_accessibility", "action_kind": "press_button",
            "status": "PASS", "execution_attempted": True,
            "action": "VISION_GROUNDED_POINTER", "alignment": vision_pointer["alignment"]}
        before_pointer_actions = api.model_telemetry.snapshot()["counters"].get("pointer_actions", 0)
        verified = self.client.post("/api/computer/actions/button/verify", json={
            "plan_id": plan["plan_id"], "observation": {"ax": after}, "execution_receipt": receipt})
        self.assertEqual(200, verified.status_code, verified.text)
        self.assertTrue(verified.json()["verified"])
        self.assertEqual(before_pointer_actions + 1,
            api.model_telemetry.snapshot()["counters"]["pointer_actions"])

    def test_chat_native_plain_goal_infers_grounded_button_and_visible_postcondition(self):
        import json
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Native goal fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)

        class DecisionProvider:
            loaded = True
            @staticmethod
            def choice(*, question, candidates, language="multilingual"):
                return {"tool_id": candidates[0]["tool_id"]}

        original_laya = api.laya_provider
        self.addCleanup(setattr, api, "laya_provider", original_laya)
        api.laya_provider = DecisionProvider()
        before = {"elements": [
            {"id": "start", "role": "AXButton", "label": "Start game", "visible": True,
             "enabled": True, "supportedActions": ["AXPress"]},
        ], "text": ["Ready"], "confidence": 1.0, "observationId": "native-goal-before",
            "observedAt": now, "sessionId": "native-goal-session", "applicationPid": 123,
            "applicationBundleId": "example.target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        inferred = {"decision": "execute", "target_label": "Start game"}
        with patch.object(api.runtime.model, "generate", return_value={"text": json.dumps(inferred)}) as generate:
            planned = self.client.post("/api/computer/actions/button/plan", json={
                "question": "Start the game and verify 'Game started'", "expected_text": "Game started",
                "project_id": project_id,
                "conversation_id": conversation_id, "observation": {"ax": before},
            })
        self.assertEqual(200, planned.status_code, planned.text)
        plan = planned.json()
        self.assertTrue(plan["goal_postcondition_inferred"])
        self.assertEqual("Start game", plan["selected_element"]["label"])
        self.assertEqual("Game started", plan["expected_text_contains"])
        generate.assert_called_once()
        self.assertEqual(api.settings.main_model, generate.call_args.args[1])
        self.assertIn('User-supplied expected visible result (untrusted): "Game started"',
                      generate.call_args.args[0][1]["content"])

        after = {**before, "observationId": "native-goal-after", "observedAt": now + 0.1,
                 "text": ["Game started"]}
        verified = self.client.post("/api/computer/actions/button/verify", json={
            "plan_id": plan["plan_id"], "observation": {"ax": after}, "execution_receipt": {
                "source": "native_accessibility", "action_kind": "press_button",
                "status": "PASS", "execution_attempted": True,
            },
        })
        self.assertEqual(200, verified.status_code, verified.text)
        self.assertTrue(verified.json()["verified"])
        self.assertIn("Game started", verified.json()["message"])

    def test_chat_native_goal_planner_persists_chat_clarification_without_action_plan(self):
        from contextlib import redirect_stderr
        from io import StringIO
        import json
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Native clarify fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)

        class DecisionProvider:
            loaded = True
            @staticmethod
            def choice(*, question, candidates, language="multilingual"):
                return {"tool_id": candidates[0]["tool_id"]}

        original_laya = api.laya_provider
        self.addCleanup(setattr, api, "laya_provider", original_laya)
        api.laya_provider = DecisionProvider()
        question = "Open the project and verify Project opened"
        clarification = "Which project should I open?"
        diagnostics = StringIO()
        with redirect_stderr(diagnostics), patch.object(api.runtime.model, "generate", return_value={"text": json.dumps({
                "decision": "clarify", "clarification": clarification,
            })}):
            response = self.client.post("/api/computer/actions/button/plan", json={
                "question": question, "expected_text": "Project opened",
                "project_id": project_id, "conversation_id": conversation_id,
                "observation": {"ax": {"elements": [{"id": "open", "role": "AXButton",
                    "label": "Open", "visible": True, "enabled": True,
                    "supportedActions": ["AXPress"]}], "text": [], "confidence": 1.0,
                    "observationId": "native-clarify", "observedAt": now,
                    "sessionId": "native-goal-session", "applicationPid": 123,
                    "applicationBundleId": "example.target", "permissionGranted": True,
                    "coordinateSpace": "ax_global_screen_coordinates"}},
            })
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("CLARIFY", response.json()["status"])
        self.assertEqual(clarification, response.json()["clarification"])
        self.assertNotIn("plan_id", response.json())
        self.assertIn("NATIVE_CHAT_GOAL_PLAN=CLARIFY REASON=MODEL_CLARIFY", diagnostics.getvalue())
        self.assertNotIn(question, diagnostics.getvalue())
        stored = api.db.conversation(conversation_id)["messages"]
        self.assertEqual(["user", "assistant"], [message["role"] for message in stored])
        self.assertEqual(clarification, stored[-1]["content"])

    def test_chat_native_goal_completion_requires_exact_fresh_ax_evidence(self):
        import json
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Native goal completion fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)

        class DecisionProvider:
            loaded = True
            @staticmethod
            def choice(*, question, candidates, language="multilingual"):
                return {"tool_id": candidates[0]["tool_id"]}

        original_laya = api.laya_provider
        self.addCleanup(setattr, api, "laya_provider", original_laya)
        api.laya_provider = DecisionProvider()

        ax = {"elements": [], "text": ["Game started", "Score: 0"], "confidence": 1.0,
            "observationId": "native-goal-complete", "observedAt": now,
            "sessionId": "native-goal-session", "applicationPid": 123,
            "applicationBundleId": "example.target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        for completion_text, expected_status in (("Game started", "COMPLETE"),
                                                  ("Game finished", "CLARIFY")):
            with self.subTest(expected_status=expected_status), patch.object(
                api.runtime.model, "generate", return_value={"text": json.dumps({
                    "decision": "complete", "completion_text": completion_text,
                })}) as generate:
                response = self.client.post("/api/computer/actions/button/plan", json={
                    "question": "Start the game and play until it starts",
                    "project_id": project_id, "conversation_id": conversation_id,
                    "goal_step_index": 1,
                    "verified_effects": ["Pressed Start; the menu opened."],
                    "record_user_message": False,
                    "observation": {"ax": ax},
                })
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(expected_status, response.json()["status"])
            if expected_status == "COMPLETE":
                self.assertTrue(response.json()["verified"])
                self.assertEqual("Game started", response.json()["completion_text"])
                self.assertNotIn("plan_id", response.json())
            else:
                self.assertNotIn("plan_id", response.json())
            prompt = generate.call_args.args[0][1]["content"]
            self.assertIn("Already verified effects", prompt)
            self.assertIn("Pressed Start; the menu opened.", prompt)
        stored = api.db.conversation(conversation_id)["messages"]
        self.assertNotIn("user", [message["role"] for message in stored])
        self.assertTrue(any("完了を検証しました" in message["content"] for message in stored))

    def test_chat_native_goal_completes_from_exact_visible_user_postcondition_without_model_call(self):
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Visible native goal fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)

        class DecisionProvider:
            loaded = True

        original_laya = api.laya_provider
        self.addCleanup(setattr, api, "laya_provider", original_laya)
        api.laya_provider = DecisionProvider()
        ax = {"elements": [], "text": ["Game started", "Score: 0"], "confidence": 1.0,
              "observationId": "native-goal-visible-result", "observedAt": now,
              "sessionId": "native-goal-session", "applicationPid": 123,
              "applicationBundleId": "example.target", "permissionGranted": True,
              "coordinateSpace": "ax_global_screen_coordinates"}

        with patch.object(api.runtime.model, "generate") as generate:
            response = self.client.post("/api/computer/actions/button/plan", json={
                "question": "Start the game and verify 'Game started'",
                "expected_text": "Game started", "project_id": project_id,
                "conversation_id": conversation_id, "record_user_message": False,
                "observation": {"ax": ax},
            })

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("COMPLETE", response.json()["status"])
        self.assertTrue(response.json()["verified"])
        self.assertEqual("Game started", response.json()["completion_text"])
        self.assertNotIn("plan_id", response.json())
        generate.assert_not_called()

    def test_chat_native_goal_does_not_complete_from_an_unmentioned_expected_hint(self):
        import json
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Unrelated native goal fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)

        class DecisionProvider:
            loaded = True

        original_laya = api.laya_provider
        self.addCleanup(setattr, api, "laya_provider", original_laya)
        api.laya_provider = DecisionProvider()
        ax = {"elements": [], "text": ["Game started"], "confidence": 1.0,
              "observationId": "native-goal-unrelated-hint", "observedAt": now,
              "sessionId": "native-goal-session", "applicationPid": 123,
              "applicationBundleId": "example.target", "permissionGranted": True,
              "coordinateSpace": "ax_global_screen_coordinates"}

        with patch.object(api.runtime.model, "generate", return_value={"text": json.dumps({
            "decision": "clarify", "clarification": "What should I close?",
        })}) as generate:
            response = self.client.post("/api/computer/actions/button/plan", json={
                "question": "Close the window and verify it is closed",
                "expected_text": "Game started", "project_id": project_id,
                "conversation_id": conversation_id, "record_user_message": False,
                "observation": {"ax": ax},
            })

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("CLARIFY", response.json()["status"])
        generate.assert_called_once()

    def test_chat_native_goal_does_not_complete_from_stale_ax_text(self):
        from contextlib import redirect_stderr
        from io import StringIO
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Stale native goal fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)

        class DecisionProvider:
            loaded = True

        original_laya = api.laya_provider
        self.addCleanup(setattr, api, "laya_provider", original_laya)
        api.laya_provider = DecisionProvider()
        ax = {"elements": [], "text": ["Game started"], "confidence": 1.0,
              "observationId": "native-goal-stale-result", "observedAt": now - 31,
              "sessionId": "native-goal-session", "applicationPid": 123,
              "applicationBundleId": "example.target", "permissionGranted": True,
              "coordinateSpace": "ax_global_screen_coordinates"}

        diagnostics = StringIO()
        with redirect_stderr(diagnostics), patch.object(api.runtime.model, "generate") as generate:
            response = self.client.post("/api/computer/actions/button/plan", json={
                "question": "Start the game and verify 'Game started'",
                "expected_text": "Game started", "project_id": project_id,
                "conversation_id": conversation_id, "record_user_message": False,
                "observation": {"ax": ax},
            })

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("CLARIFY", response.json()["status"])
        self.assertIn("NATIVE_CHAT_GOAL_PLAN=CLARIFY REASON=NATIVE_CHAT_COMPLETION_OBSERVATION_STALE",
                      diagnostics.getvalue())
        generate.assert_not_called()

    def test_gui_preflight_and_auth(self):
        import secrets
        token=secrets.token_urlsafe(32)
        origin="http://127.0.0.1:5173"
        with patch("olcr_api.app._gui_session_token", token):
            preflight=self.client.options("/api/projects",headers={"Origin":origin,"Access-Control-Request-Method":"GET","Access-Control-Request-Headers":"x-olcr-session,content-type"})
            self.assertEqual(200,preflight.status_code)
            self.assertEqual(origin,preflight.headers.get("access-control-allow-origin"))
            self.assertEqual(401,self.client.get("/api/projects").status_code)
            self.assertEqual(401,self.client.get("/api/projects",headers={"X-OLCR-Session":"invalid"}).status_code)
            self.assertEqual(200,self.client.get("/api/projects",headers={"X-OLCR-Session":token}).status_code)
            denied=self.client.options("/api/projects",headers={"Origin":"https://example.com","Access-Control-Request-Method":"GET"})
            self.assertEqual(400,denied.status_code)
    def test_desktop_world_state_uses_gui_auth_and_restores_persisted_state(self):
        import secrets
        # Another test module may import the app before this module sets
        # OLCR_DB_PATH. Isolate this persistent-state fixture from a developer DB.
        isolated_profile = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(isolated_profile.cleanup)
        original_db = api.db
        self.addCleanup(setattr, api, "db", original_db)
        api.db = api.Database(str(Path(isolated_profile.name) / "world-state.sqlite"))
        api.db.initialize()
        token = secrets.token_urlsafe(32)
        payload = {"observation": {
            "source": "AX", "permissionGranted": True, "observationId": "ax-123-456",
            "observedAt": 1234.5, "sessionId": "macos-ax-123",
            "coordinateSpace": "ax_global_screen_coordinates", "confidence": 0.95,
            "windowTitle": "Demo", "applicationPid": 123, "applicationBundleId": "com.example.demo",
            "elements": [{"id": "ax-0", "role": "AXButton", "label": "Continue", "depth": 1}],
        }}
        with patch("olcr_api.app._gui_session_token", token):
            self.assertEqual(401, self.client.put("/api/desktop/world-state", json=payload).status_code)
            headers = {"X-OLCR-Session": token}
            saved = self.client.put("/api/desktop/world-state", headers=headers, json=payload)
            self.assertEqual(200, saved.status_code)
            self.assertEqual("macos-ax-123", saved.json()["session_id"])
            self.assertEqual(1, saved.json()["scene_epoch"])
            unchanged = self.client.put("/api/desktop/world-state", headers=headers, json=payload)
            self.assertEqual(1, unchanged.json()["scene_epoch"])
            changed_payload = {"observation": {**payload["observation"], "elements": [{**payload["observation"]["elements"][0], "label": "Cancel"}]}}
            changed = self.client.put("/api/desktop/world-state", headers=headers, json=changed_payload)
            self.assertEqual(2, changed.json()["scene_epoch"])
            restored = self.client.get("/api/desktop/world-state", headers=headers)
            self.assertEqual(200, restored.status_code)
            self.assertEqual("ax-123-456", restored.json()["state"]["observation_id"])
            self.assertEqual("ax_global_screen_coordinates", restored.json()["state"]["coordinate_space"])
            self.assertEqual("Cancel", restored.json()["state"]["ui_elements"][0]["label"])
            self.assertEqual("com.example.demo", restored.json()["state"]["active_window"]["application_bundle_id"])
            self.assertEqual("Demo", self.client.get("/api/desktop/world-state/macos-ax-123", headers=headers).json()["state"]["active_window"]["title"])
            policy = {"controller_id": "a1b2c3d4e5f60718", "revision": 1, "state": "ACTIVE", "reason": "accessibility_change",
                      "target_fps": 8, "capture_fps_ceiling": 2, "achievable_fps": 2,
                      "compute_level_target": 1, "burst_available": True,
                      "state_durations_ms": {"idle_ms": 0, "active_ms": 123, "high_ms": 0, "burst_ms": 0},
                      "event_wakeups": 4, "pixel_change_wakeups": 9}
            update = {"session_id": "macos-ax-123", "observation_id": "ax-123-456", "policy": policy}
            self.assertEqual(401, self.client.put("/api/desktop/world-state/perception", json=update).status_code)
            merged = self.client.put("/api/desktop/world-state/perception", headers=headers, json=update)
            self.assertEqual(200, merged.status_code)
            self.assertEqual("ACTIVE", merged.json()["state"]["perception_state"])
            self.assertEqual(policy, merged.json()["state"]["perception_policy"])
            self.assertEqual(409, self.client.put("/api/desktop/world-state/perception", headers=headers,
                                                  json={**update, "observation_id": "old-observation"}).status_code)
            inconsistent = {**update, "policy": {**policy, "achievable_fps": 8}}
            self.assertEqual(422, self.client.put("/api/desktop/world-state/perception", headers=headers, json=inconsistent).status_code)
            negative_wakeups = {**update, "policy": {**policy, "event_wakeups": -1}}
            self.assertEqual(422, self.client.put("/api/desktop/world-state/perception", headers=headers, json=negative_wakeups).status_code)

    def test_vision_pointer_disclosure_plan_is_narrow_and_fresh_ax_verifies(self):
        import time
        import uuid

        isolated = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(isolated.cleanup)
        original_db = api.db
        self.addCleanup(setattr, api, "db", original_db)
        api.db = api.Database(str(Path(isolated.name) / "vision-pointer.sqlite"))
        api.db.initialize()
        project_id = str(uuid.uuid4())
        conversation_id = str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Vision pointer fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)

        class DecisionProvider:
            loaded = True

            @staticmethod
            def choice(*, question, candidates, language="multilingual"):
                return {"tool_id": candidates[0]["tool_id"]}

        original_laya = api.laya_provider
        self.addCleanup(setattr, api, "laya_provider", original_laya)
        api.laya_provider = DecisionProvider()
        ax = {"elements": [{"id": "context", "role": "AXDisclosureTriangle",
            "label": "Project Core Context", "enabled": True, "expanded": False,
            "frame": None, "supportedActions": []}],
            "observationId": "ax-vision-before", "observedAt": now, "sessionId": "macos-ax-vision",
            "applicationPid": 123, "applicationBundleId": "example.target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        vision_pointer = {"candidate": {"id": "candidate-1", "label": "Project Core Context",
                "bbox": [0.1, 0.2, 0.2, 0.3], "confidence": 0.9},
            "provenance": {"capture_session_id": "capture-1", "frame_sequence": 12,
                "tracked_frame_sequence": 13, "observed_at": now, "image_sha256": "a" * 64,
                "width": 640, "height": 480,
                "target_application": {"applicationPid": 123, "applicationBundleId": "example.target"},
                "display_geometry": {"displayId": 2, "originX": 0, "originY": 0,
                    "widthPoints": 1440, "heightPoints": 900}},
            "alignment": {"status": "READY", "captureSessionId": "capture-1",
                "targetApplication": {"applicationPid": 123, "applicationBundleId": "example.target"},
                "seedSequence": 12, "verifiedSequence": 13, "verifiedAt": now,
                "displayId": 2, "trackingSource": "OPENCV_LUCAS_KANADE",
                "trackingConfidence": 0.88, "targetWindowContainsPoint": True,
                "mappedGlobalPoint": [240, 180]}}
        payload = {"question": "Open Project Core Context", "project_id": project_id,
            "conversation_id": conversation_id,
            "observation": {"ax": ax, "pointer_event_permission_granted": True,
                "vision_pointer": vision_pointer}}
        response = self.client.post("/api/computer/actions/open-disclosure/plan", json=payload)
        self.assertEqual(200, response.status_code, response.text)
        plan = response.json()
        self.assertEqual("vision_pointer", plan["action_route"]["mechanism"])
        self.assertTrue(plan["action_route"]["verification_required"])

        after = {**ax, "observationId": "ax-vision-after", "observedAt": now + 0.2,
            "elements": [{**ax["elements"][0], "expanded": True}]}
        before_dispatches = api.model_telemetry.snapshot()["counters"].get("vision_grounded_actions", 0)
        verified = self.client.post("/api/computer/actions/open-disclosure/verify",
            json={"plan_id": plan["plan_id"], "observation": {"ax": after},
                  "execution_receipt": {"source": "native_accessibility", "action_kind": "open_disclosure",
                      "status": "PASS", "execution_attempted": True}})
        self.assertEqual(200, verified.status_code, verified.text)
        self.assertTrue(verified.json()["verified"])
        self.assertIn("AX観測", verified.json()["message"])
        self.assertEqual(before_dispatches + 1,
            api.model_telemetry.snapshot()["counters"]["vision_grounded_actions"])

    def test_failed_disclosure_attempt_offers_one_shot_replan_only_for_confirmed_missing_effect(self):
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Recovery fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)
        class DecisionProvider:
            loaded = True
            @staticmethod
            def choice(*, question, candidates, language="multilingual"):
                return {"tool_id": candidates[0]["tool_id"]}
        original_laya = api.laya_provider
        self.addCleanup(setattr, api, "laya_provider", original_laya)
        api.laya_provider = DecisionProvider()
        before = {"elements": [{"id": "details", "role": "AXDisclosureTriangle", "label": "Details",
            "enabled": True, "expanded": False, "frame": {"x": 10, "y": 10, "width": 20, "height": 20},
            "supportedActions": ["AXPress"]}], "observationId": "recovery-before", "observedAt": now,
            "sessionId": "recovery-session", "applicationPid": 321, "applicationBundleId": "example.target",
            "permissionGranted": True, "coordinateSpace": "ax_global_screen_coordinates"}
        payload = {"question": "Open Details", "project_id": project_id, "conversation_id": conversation_id,
            "observation": {"ax": before}}
        planned = self.client.post("/api/computer/actions/open-disclosure/plan", json=payload)
        self.assertEqual(200, planned.status_code, planned.text)
        plan_id = planned.json()["plan_id"]
        after = {**before, "observationId": "recovery-after", "observedAt": now + 0.1}
        receipt = {"source": "native_accessibility", "action_kind": "open_disclosure",
            "status": "FAILED", "execution_attempted": True}
        with patch.object(api.model_telemetry, "record_action_verification") as record_verification:
            failed = self.client.post("/api/computer/actions/open-disclosure/verify", json={
                "plan_id": plan_id, "observation": {"ax": after}, "execution_receipt": receipt})
        record_verification.assert_called_once()
        self.assertEqual("NOT_CONFIRMED", record_verification.call_args.args[0]["result"])
        self.assertEqual(200, failed.status_code, failed.text)
        self.assertEqual("NOT_CONFIRMED", failed.json()["verification_state"])
        self.assertTrue(failed.json()["recovery_offer"])
        self.assertEqual("open_disclosure", failed.json()["action_kind"])
        self.assertEqual(404, self.client.post("/api/computer/actions/open-disclosure/verify", json={
            "plan_id": plan_id, "observation": {"ax": after}, "execution_receipt": receipt}).status_code)

        # Both recovery endpoints return to Laya instead of matching the same
        # approved routine that produced the failed plan.
        with patch.object(api, "_approved_routine_action_plan",
                          side_effect=AssertionError("routine cache must be bypassed for recovery replan")):
            replanned = self.client.post("/api/computer/actions/open-disclosure/plan", json={
                **payload, "observation": {"ax": after}, "bypass_routine_cache": True})
        self.assertEqual(200, replanned.status_code, replanned.text)
        self.assertEqual("recovery-after", replanned.json()["action_route"]["observation_id"])

        inconclusive_before = {**before, "observationId": "recovery-before-inconclusive", "observedAt": time.time()}
        payload["observation"] = {"ax": inconclusive_before}
        inconclusive_plan = self.client.post("/api/computer/actions/open-disclosure/plan", json=payload).json()
        stale = {**inconclusive_before, "observationId": "recovery-stale", "observedAt": time.time() - 60}
        inconclusive = self.client.post("/api/computer/actions/open-disclosure/verify", json={
            "plan_id": inconclusive_plan["plan_id"], "observation": {"ax": stale}, "execution_receipt": receipt})
        self.assertEqual(200, inconclusive.status_code, inconclusive.text)
        self.assertEqual("INCONCLUSIVE", inconclusive.json()["verification_state"])
        self.assertFalse(inconclusive.json()["recovery_offer"])

        mismatch_before = {**before, "observationId": "recovery-before-mismatch", "observedAt": time.time()}
        payload["observation"] = {"ax": mismatch_before}
        mismatch_plan = self.client.post("/api/computer/actions/open-disclosure/plan", json=payload).json()
        foreign_after = {**mismatch_before, "observationId": "recovery-foreign-after",
            "observedAt": time.time() + 0.1, "applicationPid": 999}
        mismatch = self.client.post("/api/computer/actions/open-disclosure/verify", json={
            "plan_id": mismatch_plan["plan_id"], "observation": {"ax": foreign_after}, "execution_receipt": receipt})
        self.assertEqual(200, mismatch.status_code, mismatch.text)
        self.assertEqual("INCONCLUSIVE", mismatch.json()["verification_state"])
        self.assertFalse(mismatch.json()["recovery_offer"])
        self.assertEqual(404, self.client.post("/api/computer/actions/open-disclosure/verify", json={
            "plan_id": mismatch_plan["plan_id"], "observation": {"ax": foreign_after}, "execution_receipt": receipt}).status_code)

    def test_failed_find_shortcut_attempt_offers_replan_and_consumes_the_previous_plan_once(self):
        import time
        import uuid

        project_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        api.db.create_project("Find recovery fixture", None, now, project_id)
        api.db.create_conversation("Fixture", now, conversation_id, project_id)

        class DecisionProvider:
            loaded = True
            @staticmethod
            def choice(*, question, candidates, language="multilingual"):
                return {"tool_id": candidates[0]["tool_id"]}

        original_laya = api.laya_provider
        self.addCleanup(setattr, api, "laya_provider", original_laya)
        api.laya_provider = DecisionProvider()
        before = {"elements": [{"id": "find-menu", "role": "AXMenuItem", "label": "Find…",
            "enabled": True, "keyboardShortcut": {"keyCode": 3, "modifiers": 0, "key": "f",
                "provenance": "application_menu"}}],
            "observationId": "find-recovery-before", "observedAt": now,
            "sessionId": "find-recovery-session", "applicationPid": 456,
            "applicationBundleId": "example.find-target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        payload = {"question": "Open the search field", "project_id": project_id,
            "conversation_id": conversation_id, "observation": {"ax": before}}
        planned = self.client.post("/api/computer/actions/shortcut/open-find/plan", json=payload)
        self.assertEqual(200, planned.status_code, planned.text)
        self.assertEqual("keyboard_shortcut", planned.json()["action_route"]["mechanism"])

        after = {**before, "observationId": "find-recovery-after", "observedAt": now + 0.1}
        receipt = {"source": "native_accessibility", "action_kind": "open_find",
            "status": "FAILED", "execution_attempted": True}
        failed = self.client.post("/api/computer/actions/shortcut/open-find/verify", json={
            "plan_id": planned.json()["plan_id"], "observation": {"ax": after},
            "execution_receipt": receipt})
        self.assertEqual(200, failed.status_code, failed.text)
        self.assertEqual("NOT_CONFIRMED", failed.json()["verification_state"])
        self.assertEqual("SEARCH_FIELD_FOCUS_NOT_OBSERVED", failed.json()["reason"])
        self.assertTrue(failed.json()["recovery_offer"])
        self.assertEqual("open_find", failed.json()["action_kind"])
        replay = self.client.post("/api/computer/actions/shortcut/open-find/verify", json={
            "plan_id": planned.json()["plan_id"], "observation": {"ax": after},
            "execution_receipt": receipt})
        self.assertEqual(404, replay.status_code, replay.text)

        # A recovery replan must not immediately select the same cached routine
        # that just failed. It should return to the existing Laya planner.
        with patch.object(api, "_approved_routine_action_plan",
                          side_effect=AssertionError("routine cache must be bypassed for recovery replan")):
            replanned = self.client.post("/api/computer/actions/shortcut/open-find/plan", json={
                **payload, "observation": {"ax": after}, "bypass_routine_cache": True})
        self.assertEqual(200, replanned.status_code, replanned.text)
        self.assertEqual("find-recovery-after", replanned.json()["action_route"]["observation_id"])

    def test_unified_world_state_composes_desktop_and_browser_refs_without_query_strings(self):
        class Sessions:
            def list(self):
                return [{"session_id":"browser-1","url":"https://example.test/list?session=secret#row","last_activity":123.0}]
        original=api.browser_sessions
        api.browser_sessions=Sessions()
        desktop={"session_id":"desktop-1","state":{"observation_id":"obs-1","source":"AX"}}
        try:
            with patch("olcr_api.app._gui_session_token","world-state-token"), \
                 patch.object(api.db,"load_latest_desktop_world_state",return_value=desktop):
                self.assertEqual(401,self.client.get("/api/world-state").status_code)
                response=self.client.get("/api/world-state",headers={"X-OLCR-Session":"world-state-token"})
                self.assertEqual(200,response.status_code)
                state=response.json()
                self.assertEqual("AVAILABLE",state["desktop"]["status"])
                self.assertEqual("obs-1",state["desktop"]["record"]["state"]["observation_id"])
                self.assertEqual("https://example.test/list",state["browser_sessions"][0]["url"])
                self.assertNotIn("secret",response.text)
        finally:
            api.browser_sessions=original

    @classmethod
    def setUpClass(cls): cls.client = TestClient(app)
    def test_health(self):
        health=self.client.get("/api/health").json()
        self.assertEqual("ok", health["status"])
        self.assertEqual(api._api_repair_build_id, health["build_id"])
        self.assertEqual("0.6.0", CLI_VERSION)
        self.assertEqual(CLI_VERSION, health["version"])
        self.assertEqual("ready", health["model_configuration"])
        # Test discovery can import the app through another module first.  In
        # that case its temporary path is not the active configuration.  The
        # endpoint must report the database the already-running app uses.
        self.assertEqual(settings.db_path, health["db_path"])
        self.assertEqual(db.path, health["db_path"])
    def test_direct_chat(self):
        body = self.client.post("/api/chat", json={"message":"lowercase: HELLO"}).json()
        self.assertEqual("DIRECT", body["task"]["route"]); self.assertEqual(0, len(body["task"]["model_calls"]))
    def test_projectless_conversation_create_list_chat_and_rename(self):
        created = self.client.post("/api/conversations")
        self.assertEqual(200, created.status_code, created.text)
        conversation = created.json()
        self.assertIsNone(conversation["project_id"])
        self.assertIn(conversation["id"], [row["id"] for row in self.client.get("/api/conversations").json()["conversations"]])
        chat = self.client.post("/api/chat", json={
            "message": "lowercase: HELLO", "project_id": None,
            "conversation_id": conversation["id"],
        })
        self.assertEqual(200, chat.status_code, chat.text)
        self.assertEqual(conversation["id"], chat.json()["conversation_id"])
        history = self.client.get(f"/api/conversations/{conversation['id']}").json()
        self.assertIsNone(history["project_id"])
        self.assertGreaterEqual(len(history["messages"]), 2)
        renamed = self.client.patch(f"/api/conversations/{conversation['id']}", json={"title": "Quick note"})
        self.assertEqual(200, renamed.status_code, renamed.text)
        self.assertEqual("Quick note", renamed.json()["title"])
    def test_unresolved_natural_question_uses_wikipedia_before_search_fallback(self):
        message = "Ukraine conflict casualties in 2025?"
        result = {"tool_id": "knowledge.wikimedia", "provider": "Wikimedia",
                  "fetched_at": "2026-10-04T00:00:00Z",
                  "data": {"title": "Ukraine", "description": "Country", "extract": "Ukraine is a country in Europe.",
                           "page_url": "https://en.wikipedia.org/wiki/Ukraine", "language": "en"},
                  "sources": [{"title": "Ukraine", "provider": "Wikipedia",
                               "canonical_url": "https://en.wikipedia.org/wiki/Ukraine"}]}
        composed_task = api.Task(message)
        composed_task.transition(api.TaskState.ROUTING)
        composed_task.transition(api.TaskState.GENERATING)
        composed_task.transition(api.TaskState.COMPLETED)
        with patch.object(api, "route_external_tool", return_value=None), \
             patch.object(api, "router_decision", return_value=None), \
             patch.object(api, "execute_external_tool", return_value=result) as execute, \
             patch.object(api, "compose_external_result", return_value=(composed_task, "Wikipedia answer")) as compose, \
             patch.object(api.runtime, "execute", side_effect=AssertionError("no-search miss should use Wikipedia")):
            response = self.client.post("/api/chat", json={"message": message,
                "project_id": api.db.default_project_id()})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("knowledge.wikimedia", execute.call_args.args[0])
        self.assertEqual("Ukraine conflict casualties in 2025", execute.call_args.args[1]["query"])
        self.assertEqual("Wikipedia answer", response.json()["response"])
        compose.assert_called_once()
    def test_index_path_traversal(self):
        response = self.client.post("/api/files/index", json={"path":"/etc/passwd"})
        self.assertEqual(403, response.status_code)

    def test_memory_off_runtime_constraint_guards_unavailable_history(self):
        constraint = conversation_memory_constraint(False)
        self.assertIn("Do not invent replacement facts", constraint)
        self.assertIn("Current user message", constraint)
        self.assertEqual("", conversation_memory_constraint(True))

    def test_browser_session_api_is_gui_session_protected_and_uses_the_scoped_manager(self):
        class Sessions:
            def list(self): return [{"session_id":"s1","url":"https://example.com/"}]
            def open(self, profile_id=None, *, interactive_login=False): return {"status":"READY","session_id":"s1","profile_id":profile_id,"tools":["browser_navigate","browser_snapshot"]}
            def profiles(self): return []
            def forget_profile(self, profile_id): return True
            def navigate(self, session_id, url): return {"status":"AVAILABLE","session_id":session_id,"url":url,"clean_markdown":"page"}
            def navigate_back(self, session_id): return {"status":"AVAILABLE","session_id":session_id,"url":"https://example.com/previous","clean_markdown":"previous"}
            def reload(self, session_id): return {"status":"AVAILABLE","session_id":session_id,"url":"https://example.com/","clean_markdown":"page"}
            def network_requests(self, session_id): return {"status":"AVAILABLE","url":"https://example.com/","requests":[{"method":"GET","url":"https://example.com/api/items","status_code":200}]}
            def network_json_evidence(self, session_id, request_index): return {"status":"AVAILABLE","request_index":request_index,"url":"https://example.com/api/items","status_code":200,"content_type":"application/json","preview":{"items":[{"name":"Fixture"}]},"truncated":False,"persisted":False}
            def snapshot(self, session_id): return {"status":"AVAILABLE","session_id":session_id,"clean_markdown":"page"}
            def picker_evidence(self, session_id, kind): return {"status":"AVAILABLE","candidates":[{"kind":kind,"text":"row"}]}
            def close(self, session_id): return session_id=="s1"
        original=api.browser_sessions
        api.browser_sessions=Sessions()
        try:
            token="browser-session-api-token"
            with patch("olcr_api.app._gui_session_token",token):
                self.assertEqual(401,self.client.get("/api/web/browser/sessions").status_code)
                headers={"X-OLCR-Session":token}
                self.assertEqual("s1",self.client.get("/api/web/browser/sessions",headers=headers).json()["sessions"][0]["session_id"])
                opened=self.client.post("/api/web/browser/sessions",headers=headers)
                self.assertEqual("s1",opened.json()["session_id"])
                nav=self.client.post("/api/web/browser/sessions/s1/navigate",headers=headers,json={"url":"https://example.com/"})
                self.assertEqual("page",nav.json()["clean_markdown"])
                back=self.client.post("/api/web/browser/sessions/s1/back",headers=headers)
                self.assertEqual("https://example.com/previous",back.json()["url"])
                reload_response=self.client.post("/api/web/browser/sessions/s1/reload",headers=headers)
                self.assertEqual("page",reload_response.json()["clean_markdown"])
                self.assertEqual(200,self.client.get("/api/web/browser/sessions/s1/network",headers=headers).status_code)
                self.assertEqual(401,self.client.get("/api/web/browser/sessions/s1/network").status_code)
                json_evidence=self.client.get("/api/web/browser/sessions/s1/network/2/json",headers=headers)
                self.assertEqual(200,json_evidence.status_code)
                self.assertEqual({"name":"Fixture"},json_evidence.json()["preview"]["items"][0])
                self.assertFalse(json_evidence.json()["persisted"])
                self.assertEqual(401,self.client.get("/api/web/browser/sessions/s1/network/2/json").status_code)
                self.assertEqual(200,self.client.get("/api/web/browser/sessions/s1/snapshot",headers=headers).status_code)
                picker=self.client.post("/api/web/browser/sessions/s1/picker",headers=headers,json={"kind":"row"})
                self.assertEqual("row",picker.json()["candidates"][0]["kind"])
                self.assertEqual(200,self.client.delete("/api/web/browser/sessions/s1",headers=headers).status_code)
        finally:
            api.browser_sessions=original

    def test_network_json_recipe_uses_existing_preview_and_validate_gate(self):
        import secrets
        isolated = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(isolated.cleanup)
        original_db = api.db
        self.addCleanup(setattr, api, "db", original_db)
        api.db = api.Database(str(Path(isolated.name) / "json-recipes.sqlite"))
        api.db.initialize()

        class Sessions:
            profile_id = "account-one"
            def list(self): return [{"session_id":"json-session", "profile_id":self.profile_id}]
            def navigate(self, session_id, url):
                if session_id != "json-session": raise AssertionError(session_id)
                return {"status":"AVAILABLE", "url":url}
            def extract_network_json_recipe(self, session_id, source, *, limit=100):
                if source["endpoint_path"] != "/api/items" or source["fields"]["name"]["pointer"] != "/name":
                    raise AssertionError(source)
                return {"status":"AVAILABLE", "rows":[{"name":"Fixture"}],
                        "record_count":1, "field_match_counts":{"name":1}, "truncated":False}

        original_sessions = api.browser_sessions
        api.browser_sessions = Sessions()
        try:
            token = secrets.token_urlsafe(24)
            headers = {"X-OLCR-Session":token}
            with patch("olcr_api.app._gui_session_token",token):
                payload = {"name":"JSON fixture", "start_url":"https://example.test/list",
                    "record_selector":"body", "fields":{}, "source":"network_json",
                    "network_json":{"endpoint_path":"/api/items", "records_pointer":"/items",
                        "fields":{"name":{"pointer":"/name"}}},
                    "browser_profile_id":"account-one",
                    "navigation":{"type":"none"}, "max_pages":1, "max_records":5,
                    "max_duration_seconds":60, "output_format":"json"}
                self.assertEqual(401,self.client.post("/api/web/recipes",json=payload).status_code)
                created = self.client.post("/api/web/recipes",headers=headers,json=payload)
                self.assertEqual(200,created.status_code,created.text)
                recipe = created.json()
                self.assertEqual("DRAFT",recipe["status"])
                self.assertEqual("network_json",recipe["source"])
                self.assertNotIn("response_body",str(recipe))
                preview = self.client.post(f"/api/web/recipes/{recipe['id']}/preview",headers=headers,
                    json={"session_id":"json-session", "limit":5})
                self.assertEqual(200,preview.status_code,preview.text)
                self.assertEqual([{"name":"Fixture"}],preview.json()["rows"])
                api.browser_sessions.profile_id = "another-account"
                mismatch = self.client.post(f"/api/web/recipes/{recipe['id']}/preview",headers=headers,
                    json={"session_id":"json-session", "limit":5})
                self.assertEqual(409,mismatch.status_code,mismatch.text)
                self.assertIn("RECIPE_BROWSER_PROFILE_MISMATCH",mismatch.text)
                api.browser_sessions.profile_id = "account-one"
                validated = self.client.post(f"/api/web/recipes/{recipe['id']}/validate",headers=headers,
                    json={"session_id":"json-session", "limit":5})
                self.assertEqual(200,validated.status_code,validated.text)
                self.assertEqual("VALIDATED",validated.json()["recipe"]["status"])
                self.assertEqual("account-one",api.db.web_recipe(recipe["id"])["browser_profile_id"])
                self.assertEqual("/api/items",api.db.web_recipe(recipe["id"])["network_json"]["endpoint_path"])
        finally:
            api.browser_sessions=original_sessions

    def test_natural_language_recipe_proposal_is_grounded_and_remains_a_draft(self):
        class Sessions:
            def list(self): return [{"session_id":"s-nl","url":"https://example.test/list?campaign=private",
                                    "profile_id":"text-profile"}]
            def extract_recipe(self, session_id, record_selector, fields, **kwargs):
                return {"status":"UNAVAILABLE", "error":"AUTOMATIC_EXTRACTION_NOT_APPLICABLE"}
            def picker_evidence(self, session_id, kind):
                self.assert_session(session_id, kind)
                return {"status":"AVAILABLE","candidates":[
                    {"kind":"row","tag":"tr","role":"row","text":"Acme","dom_path":"table > tbody > tr:nth-of-type(1)",
                     "selector_candidates":["table > tbody > tr:nth-of-type(1)"],"attributes":{}},
                    {"kind":"field","tag":"td","role":"cell","text":"Acme","dom_path":"table > tbody > tr:nth-of-type(1) > td:nth-of-type(1)",
                     "selector_candidates":["td:nth-of-type(1)"],"relative_selector":"td:nth-of-type(1)","attributes":{}},
                    {"kind":"field","tag":"a","role":"link","text":"Profile","dom_path":"table > tbody > tr:nth-of-type(1) > td:nth-of-type(2) > a",
                     "selector_candidates":["a.profile"],"relative_selector":"td:nth-of-type(2) > a","attributes":{"href":"https://example.test/profile/1"}},
                ]}
            def assert_session(self, session_id, kind):
                if session_id != "s-nl" or kind != "all": raise AssertionError("unexpected picker request")
        proposal={"name":"Organization directory","record_candidate_index":0,
                  "fields":[{"name":"organization","candidate_index":1,"extract":"text"},
                            {"name":"profile","candidate_index":2,"extract":"href"}],
                  "max_pages":1,"max_records":100}
        original=api.browser_sessions
        api.browser_sessions=Sessions()
        with patch("olcr_api.app._gui_session_token","nl-recipe-token"), \
             patch.object(api.runtime.model,"generate",side_effect=[
                 {"text":__import__("json").dumps(proposal)},
                 {"text":__import__("json").dumps({**proposal,"record_candidate_index":999})},
             ]) as generate:
            try:
                self.assertEqual(401,self.client.post("/api/web/recipes/draft-from-text",json={"session_id":"s-nl","instruction":"Collect names and profile links"}).status_code)
                response=self.client.post("/api/web/recipes/draft-from-text",headers={"X-OLCR-Session":"nl-recipe-token"},
                                          json={"session_id":"s-nl","instruction":"Collect names and profile links"})
                self.assertEqual(200,response.status_code,response.text)
                record=response.json()
                self.assertEqual("DRAFT",record["status"])
                self.assertEqual("https://example.test/list?campaign=private",record["start"]["url"])
                self.assertEqual("table > tbody > tr",record["record_selector"])
                self.assertEqual("td:nth-of-type(1)",record["fields"]["organization"]["selector"])
                self.assertEqual("text-profile",record["browser_profile_id"])
                self.assertEqual("href",record["fields"]["profile"]["extract"])
                self.assertNotIn("campaign",generate.call_args.args[0][1]["content"])
                self.assertIsNotNone(api.db.web_recipe(record["id"]))
                api.db.delete_web_recipe(record["id"])
                rejected=self.client.post("/api/web/recipes/draft-from-text",headers={"X-OLCR-Session":"nl-recipe-token"},
                                          json={"session_id":"s-nl","instruction":"Collect names and profile links"})
                self.assertEqual(422,rejected.status_code)
                self.assertEqual("RECIPE_DRAFT_NOT_GROUNDED_IN_VISIBLE_EVIDENCE",rejected.json()["detail"])
            finally:
                api.browser_sessions=original

    def test_web_recipe_routes_require_gui_session_and_persist_only_valid_drafts(self):
        class PreviewSession:
            def list(self): return [{"session_id":"s-preview","url":"https://example.com/directory"}]
            def navigate(self, session_id, url): return {"status":"AVAILABLE","session_id":session_id,"url":url,"clean_markdown":"fixture"}
            def snapshot(self, session_id): return self.navigate(session_id, "https://example.com/directory")
            def picker_evidence(self, session_id, kind):
                return {"status":"AVAILABLE","candidates":[{"kind":"row","tag":"tr","text":"Acme",
                    "selector_candidates":["table.people tr"],"dom_path":"table.people > tbody > tr:nth-of-type(1)"}]}
            def probe_recipe_selectors(self, session_id, candidates, fields):
                return {"status":"AVAILABLE","url":"https://example.com/directory",
                    "results":[{"candidate_id":item["candidate_id"],"selector":item["selector"],
                        "record_count":2,"field_match_counts":{"name":2}} for item in candidates]}
            def extract_recipe(self, session_id, record_selector, fields, *, limit=100, tool=None):
                return {"status":"AVAILABLE","rows":[{"name":"Acme"}],"record_count":2,
                        "field_match_counts":{"name":2},"truncated":True}
        token="recipe-api-token"
        payload={
            "name":"Public directory", "start_url":"https://example.com/directory",
            "record_selector":"table.people > tbody > tr",
            "fields":{"name":{"selector":"td.name","extract":"text"}},
            "navigation":{"type":"none"}, "max_pages":5,
        }
        original=api.browser_sessions
        api.browser_sessions=PreviewSession()
        try:
            with patch("olcr_api.app._gui_session_token",token):
                self.assertEqual(401,self.client.get("/api/web/recipes").status_code)
                headers={"X-OLCR-Session":token}
                created=self.client.post("/api/web/recipes",headers=headers,json=payload)
                self.assertEqual(200,created.status_code)
                recipe=created.json()
                self.assertEqual("DRAFT",recipe["status"])
                self.assertEqual("table.people > tbody > tr",recipe["record_selector"])
                self.assertEqual(recipe["id"],self.client.get("/api/web/recipes",headers=headers).json()["recipes"][0]["id"])
                self.assertEqual("Public directory",self.client.get(f"/api/web/recipes/{recipe['id']}",headers=headers).json()["name"])
                updated=self.client.put(f"/api/web/recipes/{recipe['id']}",headers=headers,json={**payload,"name":"Updated directory"})
                self.assertEqual("Updated directory",updated.json()["name"])
                preview=self.client.post(f"/api/web/recipes/{recipe['id']}/preview",headers=headers,json={"session_id":"s-preview","limit":3})
                self.assertEqual("PREVIEW",preview.json()["status"])
                self.assertEqual("Acme",preview.json()["rows"][0]["name"])
                validated=self.client.post(f"/api/web/recipes/{recipe['id']}/validate",headers=headers,json={"session_id":"s-preview"})
                self.assertEqual("VALIDATED",validated.json()["status"])
                self.assertEqual("VALIDATED",self.client.get(f"/api/web/recipes/{recipe['id']}",headers=headers).json()["status"])
                manually_updated=self.client.put(f"/api/web/recipes/{recipe['id']}",headers=headers,json={
                    **payload,"record_selector":"table.people tr",
                    "fields":{"name":{"selector":"td.organization","extract":"text"}},
                })
                self.assertEqual(200,manually_updated.status_code,manually_updated.text)
                self.assertEqual("DRAFT",manually_updated.json()["status"])
                self.assertEqual("table.people tr",manually_updated.json()["record_selector"])
                self.assertEqual("td.organization",manually_updated.json()["fields"]["name"]["selector"])
                revalidated=self.client.post(f"/api/web/recipes/{recipe['id']}/validate",headers=headers,json={"session_id":"s-preview"})
                self.assertEqual("VALIDATED",revalidated.json()["status"])
                alternatives=self.client.post(f"/api/web/recipes/{recipe['id']}/recovery-alternatives",headers=headers,json={"session_id":"s-preview"})
                self.assertEqual(200,alternatives.status_code,alternatives.text)
                self.assertFalse(alternatives.json()["saved_recipe_changed"])
                self.assertGreater(alternatives.json()["candidates"][0]["record_count"],0)
                self.assertEqual("VALIDATED",self.client.get(f"/api/web/recipes/{recipe['id']}",headers=headers).json()["status"])
                repaired=self.client.patch(f"/api/web/recipes/{recipe['id']}/record-selector",headers=headers,
                    json={"record_selector":"table.people tr"})
                self.assertEqual(200,repaired.status_code,repaired.text)
                self.assertEqual("DRAFT",repaired.json()["status"])
                self.assertEqual("table.people tr",repaired.json()["record_selector"])
                invalid=self.client.post("/api/web/recipes",headers=headers,json={**payload,"start_url":"https://example.com/?session_token=secret"})
                self.assertEqual(422,invalid.status_code)
                self.assertEqual(200,self.client.delete(f"/api/web/recipes/{recipe['id']}",headers=headers).status_code)
                self.assertEqual(404,self.client.get(f"/api/web/recipes/{recipe['id']}",headers=headers).status_code)
        finally:
            api.browser_sessions=original

    def test_scrape_run_status_and_streaming_csv_export_are_authenticated_and_bounded(self):
        import uuid
        recipe_id=f"export-recipe-{uuid.uuid4().hex}"
        run_id=f"export-run-{uuid.uuid4().hex}"
        recipe={"id":recipe_id,"version":1,"name":"Export fixture","start":{"url":"https://example.com/"},
                "record_selector":"tr","fields":{"name":{"selector":"td","extract":"text"},
                "email":{"selector":"a","extract":"href"}},"navigation":{"type":"none"},
                "detail":{"link_field":"email","record_selector":"body","fields":{"city":{"selector":".city","extract":"text"}}},
                "max_pages":1,"max_records":10,"max_duration_seconds":60,"output_format":"csv","status":"VALIDATED"}
        api.db.save_web_recipe(recipe)
        api.db.set_web_recipe_status(recipe_id,"VALIDATED")
        api.db.create_web_scrape_run(run_id,api.db.web_recipe(recipe_id))
        api.db.update_web_scrape_run(run_id,"RUNNING")
        api.db.checkpoint_web_scrape_page(run_id,1,[(None,{"name":"=unsafe","email":"https://example.com/person","city":"Tokyo"})],"fixture-hash")
        api.db.update_web_scrape_run(run_id,"COMPLETED",stop_reason="MAX_PAGES")
        token="scrape-export-token"
        with patch("olcr_api.app._gui_session_token",token):
            self.assertEqual(401,self.client.get(f"/api/web/scrape/runs/{run_id}").status_code)
            headers={"X-OLCR-Session":token}
            status=self.client.get(f"/api/web/scrape/runs/{run_id}",headers=headers)
            self.assertEqual(200,status.status_code)
            self.assertEqual(1,status.json()["record_count"])
            self.assertNotIn("recipe",status.json())
            export=self.client.get(f"/api/web/scrape/runs/{run_id}/export",headers=headers)
            self.assertEqual(200,export.status_code)
            self.assertEqual("text/csv; charset=utf-8",export.headers["content-type"])
            self.assertEqual("name,email,city\r\n'=unsafe,https://example.com/person,Tokyo\r\n",export.text)
            delete=self.client.delete(f"/api/web/recipes/{recipe_id}",headers=headers)
            self.assertEqual(409,delete.status_code)

    def test_failed_scrape_page_review_route_is_gui_authenticated_and_transient(self):
        token = "scrape-recovery-preview-token"
        snapshot = {"status": "AVAILABLE", "page_number": 2,
                    "url": "https://example.test/people", "clean_markdown": "untrusted fixture",
                    "persisted": False}
        with patch("olcr_api.app._gui_session_token", token), \
             patch.object(api.scrape_runs, "recovery_snapshot", return_value=snapshot) as preview:
            self.assertEqual(401, self.client.post(
                "/api/web/scrape/runs/run-1/recovery-snapshot").status_code)
            response = self.client.post(
                "/api/web/scrape/runs/run-1/recovery-snapshot",
                headers={"X-OLCR-Session": token})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(snapshot, response.json())
            self.assertEqual("run-1", preview.call_args.args[0])

    def test_streaming_json_and_jsonl_exports_keep_bounded_row_order(self):
        import uuid
        token="scrape-json-export-token"
        with patch("olcr_api.app._gui_session_token",token):
            headers={"X-OLCR-Session":token}
            for output_format,media,expected in (
                ("json","application/json",'[{"name":"Acme"},{"name":"Beta"}]'),
                ("jsonl","application/x-ndjson",'{"name":"Acme"}\n{"name":"Beta"}\n'),
            ):
                recipe_id=f"export-recipe-{uuid.uuid4().hex}"
                run_id=f"export-run-{uuid.uuid4().hex}"
                recipe={"id":recipe_id,"version":1,"name":"Export fixture","start":{"url":"https://example.com/"},
                        "record_selector":"tr","fields":{"name":{"selector":"td","extract":"text"}},
                        "navigation":{"type":"none"},"max_pages":1,"max_records":10,
                        "max_duration_seconds":60,"output_format":output_format,"status":"VALIDATED"}
                api.db.save_web_recipe(recipe); api.db.set_web_recipe_status(recipe_id,"VALIDATED")
                api.db.create_web_scrape_run(run_id,api.db.web_recipe(recipe_id)); api.db.update_web_scrape_run(run_id,"RUNNING")
                api.db.checkpoint_web_scrape_page(run_id,1,[(None,{"name":"Acme"}),(None,{"name":"Beta"})],"fixture-hash")
                api.db.update_web_scrape_run(run_id,"COMPLETED",stop_reason="MAX_PAGES")
                export=self.client.get(f"/api/web/scrape/runs/{run_id}/export",headers=headers)
                self.assertEqual(200,export.status_code)
                self.assertEqual(media,export.headers["content-type"])
                self.assertEqual(expected,export.text)

    def test_current_eonet_empty_falls_back_to_search_wikipedia_on_chat_path(self):
        import olcr_api.app as api_module
        task = api_module.Task("現在進行中の自然災害")
        task.transition(api_module.TaskState.ROUTING); task.transition(api_module.TaskState.GENERATING); task.transition(api_module.TaskState.COMPLETED)
        result = {"tool_id":"earth.natural_event", "provider":"NASA EONET",
                  "sources":[{"provider":"NASA EONET"}],
                  "data":{"items":[], "semantic_status":"EMPTY", "current_turn_evidence":True}}
        with patch.object(api_module.settings, "external_access_enabled", True), \
             patch.object(api_module, "route_external_tool", return_value=("earth.natural_event", {"status":"open"})), \
             patch.object(api_module, "execute_external_tool", return_value=result), \
             patch.object(api_module.runtime, "execute", return_value=(task, "Wikipedia fallback evidence")) as fallback:
            response = self.client.post("/api/chat", json={"message":"現在進行中の自然災害", "project_id":api_module.db.default_project_id()})
        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertIn("Wikipedia fallback evidence", body["response"])
        self.assertIn("NASA EONETから有効なデータを取得できなかった", body["response"])
        fallback.assert_called_once()

    def test_current_eonet_data_is_terminal_on_chat_path(self):
        import olcr_api.app as api_module
        result = {"tool_id":"earth.natural_event", "provider":"NASA EONET",
                  "sources":[{"provider":"NASA EONET"}],
                  "data":{"items":[{"title":"Current wildfire"}], "semantic_status":"DATA", "current_turn_evidence":True}}
        with patch.object(api_module.settings, "external_access_enabled", True), \
             patch.object(api_module, "route_external_tool", return_value=("earth.natural_event", {"status":"open"})), \
             patch.object(api_module, "execute_external_tool", return_value=result), \
             patch.object(api_module.runtime, "compose_tool_result", side_effect=AssertionError("Brain must not rewrite EONET")):
            response = self.client.post("/api/chat", json={"message":"現在進行中として登録されている山火事や火山などの自然災害をいくつか教えてください。", "project_id":api_module.db.default_project_id()})
        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertIn("Current wildfire", body["response"])
        self.assertNotIn("2023", body["response"])

    def test_eonet_failure_falls_back_to_search_wikipedia_on_chat_path(self):
        import olcr_api.app as api_module
        task = api_module.Task("現在進行中の自然災害")
        task.transition(api_module.TaskState.ROUTING); task.transition(api_module.TaskState.GENERATING); task.transition(api_module.TaskState.COMPLETED)
        with patch.object(api_module.settings, "external_access_enabled", True), \
             patch.object(api_module, "route_external_tool", return_value=("earth.natural_event", {"status":"open"})), \
             patch.object(api_module, "execute_external_tool", side_effect=api_module.ExternalToolError("PROVIDER_UNAVAILABLE")), \
             patch.object(api_module.runtime, "execute", return_value=(task, "Wikipedia fallback evidence")) as fallback:
            response = self.client.post("/api/chat", json={"message":"現在進行中の自然災害", "project_id":api_module.db.default_project_id()})
        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertIn("Wikipedia fallback evidence", body["response"])
        self.assertIn("NASA EONETから有効なデータを取得できなかった", body["response"])
        fallback.assert_called_once()

    def test_federal_empty_metadata_falls_back_to_search_wikipedia_on_chat_path(self):
        import olcr_api.app as api_module
        task = api_module.Task("Federal Registerから人工知能を検索")
        task.transition(api_module.TaskState.ROUTING); task.transition(api_module.TaskState.GENERATING); task.transition(api_module.TaskState.COMPLETED)
        result = {"tool_id":"government.us_federal_register", "provider":"Federal Register",
                  "sources":[{"provider":"Federal Register"}],
                  "data":{"items":[], "semantic_status":"EMPTY", "item_count":0}}
        with patch.object(api_module.settings, "external_access_enabled", True), \
             patch.object(api_module, "route_external_tool", return_value=("government.us_federal_register", {"query":"artificial intelligence"})), \
             patch.object(api_module, "execute_external_tool", return_value=result), \
             patch.object(api_module.runtime, "execute", return_value=(task, "Search fallback evidence")) as fallback:
            response = self.client.post("/api/chat", json={"message":"Federal Registerから人工知能を検索", "project_id":api_module.db.default_project_id()})
        self.assertEqual(200, response.status_code)
        self.assertIn("Search fallback evidence", response.json()["response"])
        self.assertIn("Federal Registerから有効なデータを取得できなかった", response.json()["response"])
        fallback.assert_called_once()

    def test_federal_failure_falls_back_to_search_wikipedia_on_chat_path(self):
        import olcr_api.app as api_module
        task = api_module.Task("Federal RegisterからAI ruleを検索")
        task.transition(api_module.TaskState.ROUTING); task.transition(api_module.TaskState.GENERATING); task.transition(api_module.TaskState.COMPLETED)
        with patch.object(api_module.settings, "external_access_enabled", True), \
             patch.object(api_module, "route_external_tool", return_value=("government.us_federal_register", {"query":"artificial intelligence"})), \
             patch.object(api_module, "execute_external_tool", side_effect=api_module.ExternalToolError("PROVIDER_UNAVAILABLE")), \
             patch.object(api_module.runtime, "execute", return_value=(task, "Search fallback evidence")) as fallback:
            response = self.client.post("/api/chat", json={"message":"Federal Registerから人工知能に関係する最近のruleまたはnoticeを5件検索してください。", "project_id":api_module.db.default_project_id()})
        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertIn("Search fallback evidence", body["response"])
        self.assertIn("Federal Registerから有効なデータを取得できなかった", body["response"])
        fallback.assert_called_once()

    def test_federal_data_uses_authoritative_terminal_renderer(self):
        import olcr_api.app as api_module
        result = {"tool_id":"government.us_federal_register", "provider":"Federal Register",
                  "sources":[{"provider":"Federal Register"}],
                  "data":{"items":[{"title":"AI notice", "document_number":"2026-001"}], "semantic_status":"DATA"}}
        with patch.object(api_module.settings, "external_access_enabled", True), \
             patch.object(api_module, "route_external_tool", return_value=("government.us_federal_register", {"query":"artificial intelligence"})), \
             patch.object(api_module, "execute_external_tool", return_value=result), \
             patch.object(api_module.runtime, "compose_tool_result", side_effect=AssertionError("Brain must not rewrite Federal Register")):
            response = self.client.post("/api/chat", json={"message":"Federal Registerから人工知能に関係する最近のruleまたはnoticeを検索してください。", "project_id":api_module.db.default_project_id()})
        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertIn("AI notice", body["response"])
        self.assertIn("2026-001", body["response"])

    def test_sympy_success_uses_concise_renderer_on_chat_path(self):
        import olcr_api.app as api_module
        result = {"tool_id":"math.symbolic", "provider":"SymPy",
                  "sources":[{"provider":"SymPy"}],
                  "data":{"operation":"factor", "expression":"x^4 - 1", "result":"(x - 1)*(x + 1)*(x^2 + 1)"}}
        with patch.object(api_module, "route_external_tool", return_value=("math.symbolic", {"operation":"factor", "expression":"x^4 - 1"})), \
             patch.object(api_module, "execute_external_tool", return_value=result), \
             patch.object(api_module.runtime, "compose_tool_result", side_effect=AssertionError("Brain must not rewrite SymPy")):
            response = self.client.post("/api/chat", json={"message":"x^4 - 1 を因数分解してください。可能なら記号計算ツールを使ってください。", "project_id":api_module.db.default_project_id()})
        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertIn("因数分解結果", body["response"])
        self.assertNotIn("from sympy import", body["response"])

    def test_currency_chat_uses_current_provider_value_without_brain_rewrite(self):
        import olcr_api.app as api_module
        result = {"tool_id": "currency.frankfurter", "provider": "Frankfurter",
                  "fetched_at": "2026-09-18T00:00:00+00:00",
                  "sources": [{"provider": "Frankfurter"}],
                  "data": {"base": "USD", "quote": "EUR", "amount": "100", "rate": "0.8721",
                           "converted_amount": "87.21", "rate_date": "2026-09-18"}}
        with patch.object(api_module.settings, "external_access_enabled", True), \
             patch.object(api_module, "route_external_tool", return_value=("currency.frankfurter", {"base": "USD", "quote": "EUR", "amount": 100})), \
             patch.object(api_module, "execute_external_tool", return_value=result), \
             patch.object(api_module.runtime, "compose_tool_result", side_effect=AssertionError("currency must not use Brain composition")):
            response = self.client.post("/api/chat", json={"message": "100ドルはユーロでいくら？", "project_id": api_module.db.default_project_id()})
        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertIn("87.21", body["response"])
        self.assertNotIn("92.50", body["response"])


if __name__ == "__main__": unittest.main()
