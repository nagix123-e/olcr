import unittest
import time

from olcr_api.computer_use import (plan_button_press, plan_disclosure_open, plan_open_find_shortcut,
                                  resolve_domax_question, verify_button_press,
                                  verify_disclosure_open, verify_find_shortcut,
                                  validate_chat_native_button_plan, plan_text_entry, verify_text_entry,
                                  plan_tab_focus, verify_tab_focus, plan_keypress, verify_keypress,
                                  plan_wheel_scroll, verify_wheel_scroll)


class FakeDecisionProvider:
    def __init__(self, selection=0):
        self.selection = selection
        self.calls = []

    def choice(self, *, question, candidates, language="multilingual"):
        self.calls.append((question, candidates, language))
        if self.selection is None:
            return {"tool_id": "not-grounded"}
        return {"tool_id": candidates[self.selection]["tool_id"]}


class ComputerUseObservationTests(unittest.TestCase):
    def test_tab_focus_requires_unique_visible_target_and_fresh_same_session_transition(self):
        now = time.time()
        current = {"id": "current", "role": "AXTextField", "label": "Name", "visible": True,
            "enabled": True, "focused": True}
        target = {"id": "continue", "role": "AXButton", "label": "Continue", "visible": True,
            "enabled": True, "focused": False}
        before_ax = {"elements": [current, target], "observationId": "tab-before", "observedAt": now,
            "sessionId": "macos-ax-tab", "applicationPid": 123,
            "applicationBundleId": "example.target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        plan = plan_tab_focus("Press Tab and verify Continue receives focus", "Continue", {"ax": before_ax})
        self.assertEqual("PASS", plan["status"])
        self.assertEqual("keyboard_shortcut", plan["action_route"]["mechanism"])
        self.assertEqual("current", plan["current_focus_element"]["id"])
        after_ax = {**before_ax, "observationId": "tab-after", "observedAt": now + 0.2,
            "elements": [{**current, "focused": False}, {**target, "focused": True}]}
        result = verify_tab_focus(plan["before_state"], {"ax": after_ax}, "current", "continue", "Continue")
        self.assertTrue(result["verified"])
        self.assertEqual("CONFIRMED", result["result"])

    def test_tab_focus_blocks_ambiguous_or_disabled_targets_and_wrong_identity(self):
        now = time.time()
        current = {"id": "current", "role": "AXTextField", "label": "Name", "visible": True,
            "enabled": True, "focused": True}
        target = {"id": "continue", "role": "AXButton", "label": "Continue", "visible": True,
            "enabled": True, "focused": False}
        ax = {"elements": [current, target], "observationId": "tab-before", "observedAt": now,
            "sessionId": "macos-ax-tab", "applicationPid": 123, "applicationBundleId": "example.target",
            "permissionGranted": True, "coordinateSpace": "ax_global_screen_coordinates"}
        duplicate = {**ax, "elements": [current, target, {**target, "id": "continue-2"}]}
        self.assertEqual("TAB_FOCUS_TARGET_NOT_UNIQUE",
            plan_tab_focus("Tab to Continue", "Continue", {"ax": duplicate})["reason"])
        disabled = {**ax, "elements": [current, {**target, "enabled": False}]}
        self.assertEqual("TAB_FOCUS_TARGET_NOT_ACTIONABLE",
            plan_tab_focus("Tab to Continue", "Continue", {"ax": disabled})["reason"])
        plan = plan_tab_focus("Tab to Continue", "Continue", {"ax": ax})
        after = {"ax": {**ax, "observedAt": now + 0.2, "applicationBundleId": "other.target",
            "elements": [current, {**target, "focused": True}]}}
        result = verify_tab_focus(plan["before_state"], after, "current", "continue", "Continue")
        self.assertFalse(result["verified"])
        self.assertEqual("AX_APPLICATION_IDENTITY_MISMATCH", result["reason"])

    def test_wheel_scroll_matches_vertical_bar_by_geometry_not_ax_traversal_adjacency(self):
        now = time.time()
        area = {"id": "results", "role": "AXScrollArea", "label": "Results",
            "visible": True, "depth": 1, "frame": {"x": 10, "y": 20, "width": 400, "height": 300}}
        # AX observation is breadth-first: another depth-1 child appears between
        # the area and its depth-2 scrollbar, so adjacency/take-until is invalid.
        peer = {"id": "toolbar", "role": "AXGroup", "label": "Toolbar", "visible": True,
            "depth": 1, "frame": {"x": 10, "y": 0, "width": 400, "height": 20}}
        bar = {"id": "results-vbar", "role": "AXScrollBar", "label": "Scrollbar",
            "visible": True, "depth": 2, "scrollValue": 0.4,
            "frame": {"x": 398, "y": 30, "width": 12, "height": 280}}
        before = {"elements": [area, peer, bar], "observationId": "scroll-before",
            "observedAt": now, "sessionId": "scroll-session", "applicationPid": 123,
            "applicationBundleId": "example.target", "permissionGranted": True,
            "windowFrame": {"x": 0, "y": 0, "width": 800, "height": 600}}
        observation = {"ax": before, "pointer_event_permission_granted": True}
        plan = plan_wheel_scroll("Scroll down in Results", "Results", "down", observation)
        self.assertEqual("PASS", plan["status"])
        self.assertEqual("results-vbar", plan["selected_element"]["scrollbar_id"])
        self.assertEqual(0.4, plan["selected_element"]["scroll_value"])
        after = {**before, "observationId": "scroll-after", "observedAt": now + 0.2,
            "elements": [area, peer, {**bar, "scrollValue": 0.55}]}
        verified = verify_wheel_scroll(plan["before_state"], {"ax": after},
            "results", "results-vbar", "Results", "down", 0.4)
        self.assertTrue(verified["verified"])

    def test_wheel_scroll_rejects_ambiguous_or_unrelated_bars_and_wrong_direction(self):
        now = time.time()
        area = {"id": "results", "role": "AXScrollArea", "label": "Results",
            "visible": True, "depth": 1, "frame": {"x": 10, "y": 20, "width": 400, "height": 300}}
        bar = {"id": "results-vbar", "role": "AXScrollBar", "label": "Scrollbar",
            "visible": True, "depth": 2, "scrollValue": 0.4,
            "frame": {"x": 398, "y": 30, "width": 12, "height": 280}}
        before = {"elements": [area, bar], "observationId": "scroll-guard-before",
            "observedAt": now, "sessionId": "scroll-guard-session", "applicationPid": 123,
            "applicationBundleId": "example.target", "permissionGranted": True,
            "windowFrame": {"x": 0, "y": 0, "width": 800, "height": 600}}
        base = {"ax": before, "pointer_event_permission_granted": True}
        self.assertEqual("AX_IDENTITY_OR_POINTER_PERMISSION_REQUIRED", plan_wheel_scroll(
            "Scroll down in Results", "Results", "down", {"ax": before})["reason"])
        far_bar = {**bar, "id": "other-vbar", "frame": {"x": 700, "y": 30, "width": 12, "height": 280}}
        self.assertEqual("VERTICAL_SCROLL_VALUE_UNAVAILABLE_OR_AMBIGUOUS", plan_wheel_scroll(
            "Scroll down in Results", "Results", "down", {**base,
                "ax": {**before, "elements": [area, far_bar]}})["reason"])
        plan = plan_wheel_scroll("Scroll down in Results", "Results", "down", base)
        unchanged = {**before, "observationId": "scroll-unchanged", "observedAt": now + 0.2}
        result = verify_wheel_scroll(plan["before_state"], {"ax": unchanged},
            "results", "results-vbar", "Results", "down", 0.4)
        self.assertFalse(result["verified"])
        reverse = {**unchanged, "observationId": "scroll-reverse", "elements": [area,
            {**bar, "scrollValue": 0.3}]}
        result = verify_wheel_scroll(plan["before_state"], {"ax": reverse},
            "results", "results-vbar", "Results", "down", 0.4)
        self.assertFalse(result["verified"])
        moved_area = {**area, "frame": {"x": 700, "y": 20, "width": 80, "height": 300}}
        moved = {**unchanged, "observationId": "scroll-area-moved",
            "elements": [moved_area, bar]}
        result = verify_wheel_scroll(plan["before_state"], {"ax": moved},
            "results", "results-vbar", "Results", "down", 0.4)
        self.assertFalse(result["verified"])
        future = {**before, "observationId": "scroll-future", "observedAt": now + 10,
            "elements": [area, bar]}
        result = verify_wheel_scroll(plan["before_state"], {"ax": future},
            "results", "results-vbar", "Results", "down", 0.4)
        self.assertFalse(result["verified"])

    def test_text_entry_focuses_one_visible_enabled_non_secure_ax_field(self):
        now = time.time()
        field = {"id": "search", "role": "AXTextField", "label": "Search", "visible": True,
                 "enabled": True, "focused": True}
        ax = {"elements": [field], "text": [], "confidence": 1.0,
              "observationId": "text-before", "observedAt": now, "sessionId": "text-session",
              "applicationPid": 123, "applicationBundleId": "example.target",
              "permissionGranted": True, "coordinateSpace": "ax_global_screen_coordinates"}
        plan = plan_text_entry('Type "red fox" into "Search"', "Search", "red fox", {"ax": ax})
        self.assertEqual("PASS", plan["status"])
        self.assertEqual("search", plan["selected_element"]["id"])
        self.assertEqual("accessibility", plan["action_route"]["mechanism"])
        self.assertNotIn("red fox", str(plan))
        unfocused = {**ax, "elements": [{**field, "focused": False}]}
        focus_plan = plan_text_entry('Type "red fox" into "Search"', "Search", "red fox", {"ax": unfocused})
        self.assertEqual("PASS", focus_plan["status"])
        self.assertEqual("search", focus_plan["selected_element"]["id"])

        secure = {**ax, "elements": [{**field, "role": "AXSecureTextField", "id": "password",
                                       "label": "Password"}]}
        self.assertEqual("SECURE_TEXT_ENTRY_UNAVAILABLE", plan_text_entry(
            "type password", "Password", "not-a-real-secret", {"ax": secure})["reason"])
        for changes, reason in (
            ({"visible": False}, "TEXT_TARGET_NOT_VISIBLE_AND_ENABLED"),
            ({"enabled": False}, "TEXT_TARGET_NOT_VISIBLE_AND_ENABLED"),
        ):
            candidate = {**field, **changes}
            blocked = plan_text_entry("type query", "Search", "query", {"ax": {**ax,
                "elements": [candidate]}})
            self.assertEqual(reason, blocked["reason"])
        duplicate = plan_text_entry("type query", "Search", "query", {"ax": {**ax,
            "elements": [field, {**field, "id": "search-2"}]}})
        self.assertEqual("TEXT_TARGET_NOT_UNIQUE", duplicate["reason"])
        self.assertEqual("MULTILINE_TEXT_REQUIRES_TEXT_AREA", plan_text_entry(
            "type two lines", "Search", "one\ntwo", {"ax": ax})["reason"])

    def test_text_entry_verification_uses_native_private_readback_and_fresh_ax_focus(self):
        now = time.time()
        field = {"id": "search", "role": "AXTextField", "label": "Search", "visible": True,
                 "enabled": True, "focused": True}
        before = {"elements": [field], "text": [], "confidence": 1.0,
                  "observationId": "text-before-verify", "observedAt": now,
                  "sessionId": "text-session", "applicationPid": 123,
                  "applicationBundleId": "example.target", "permissionGranted": True,
                  "coordinateSpace": "ax_global_screen_coordinates"}
        plan = plan_text_entry("type query", "Search", "private query", {"ax": before})
        after = {**before, "observationId": "text-after-verify", "observedAt": now + 0.1}
        receipt = {"status": "PASS", "execution_attempted": True,
                   "verified": True, "value_matched": True, "element_id": "search"}
        verified = verify_text_entry(plan["before_state"], {"ax": after}, "search", receipt)
        self.assertTrue(verified["verified"])
        self.assertNotIn("private query", str(verified))
        self.assertFalse(verify_text_entry(plan["before_state"], {"ax": after}, "search",
            {**receipt, "value_matched": False})["verified"])
        self.assertFalse(verify_text_entry(plan["before_state"], {"ax": after}, "search",
            {**receipt, "verified": False})["verified"])
        unfocused = {**after, "observationId": "text-after-unfocused", "observedAt": now + 0.2,
                     "elements": [{**field, "focused": False}]}
        self.assertFalse(verify_text_entry(plan["before_state"], {"ax": unfocused}, "search",
            receipt)["verified"])
        stale = {**after, "observationId": "text-after-stale", "observedAt": now - 31}
        self.assertFalse(verify_text_entry(plan["before_state"], {"ax": stale}, "search",
            receipt)["verified"])

    def test_bounded_keypress_requires_unique_focus_allowlisted_key_and_new_text(self):
        now = time.time()
        window = {"id": "game-window", "role": "AXWindow", "label": "Game",
                  "visible": True, "enabled": True, "focused": True}
        ax = {"elements": [window], "text": [], "confidence": 1.0,
              "observationId": "key-before", "observedAt": now, "sessionId": "key-session",
              "applicationPid": 123, "applicationBundleId": "example.game",
              "permissionGranted": True, "coordinateSpace": "ax_global_screen_coordinates"}
        plan = plan_keypress('Press Space and verify "Started".', "Space", "Started", {"ax": ax})
        self.assertEqual("PASS", plan["status"])
        self.assertEqual("deterministic_input", plan["action_route"]["mechanism"])
        self.assertEqual("game-window", plan["selected_element"]["id"])
        self.assertEqual("KEY_NOT_ALLOWLISTED", plan_keypress(
            "press Delete", "Delete", "Deleted", {"ax": ax})["reason"])
        already_visible = {**ax, "elements": [window,
            {"id": "status", "role": "AXStaticText", "label": "Started", "visible": True}]}
        self.assertEqual("KEY_POSTCONDITION_ALREADY_VISIBLE", plan_keypress(
            "press Space", "Space", "Started", {"ax": already_visible})["reason"])
        ambiguous = {**ax, "elements": [window,
            {**window, "id": "other-focused", "label": "Another focused element"}]}
        self.assertEqual("CURRENT_AX_FOCUS_NOT_UNIQUE", plan_keypress(
            "press Space", "Space", "Started", {"ax": ambiguous})["reason"])
        container_without_enabled = {**ax, "elements": [{**window, "enabled": None}]}
        self.assertEqual("PASS", plan_keypress(
            "press Space", "Space", "Started", {"ax": container_without_enabled})["status"])
        for secure in (
            {**window, "role": "AXSecureTextField"},
            {**window, "subrole": "AXSecureTextField"},
        ):
            self.assertEqual("SECURE_KEY_TARGET_UNAVAILABLE", plan_keypress(
                "press Space", "Space", "Started", {"ax": {**ax, "elements": [secure]}})["reason"])

    def test_native_chat_goal_plan_is_grounded_and_requires_a_new_visible_postcondition(self):
        now = time.time()
        observation = {"ax": {"elements": [
            {"id": "start", "role": "AXButton", "label": "Start game", "visible": True,
             "enabled": True, "supportedActions": ["AXPress"]},
            {"id": "delete", "role": "AXButton", "label": "Delete save", "visible": True,
             "enabled": True, "supportedActions": ["AXPress"]},
            {"id": "disabled", "role": "AXButton", "label": "Continue", "visible": True,
             "enabled": False, "supportedActions": ["AXPress"]},
        ], "text": ["Ready"], "confidence": 1.0, "observationId": "goal-plan-before",
            "observedAt": now, "sessionId": "goal-plan-session", "applicationPid": 123,
            "applicationBundleId": "example.target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}}
        plan = validate_chat_native_button_plan({"decision": "execute", "target_label": "Start game",
            "expected_text": "Game started"}, "Start the game", observation)
        self.assertEqual("execute", plan["decision"])
        self.assertEqual("Start game", plan["target_label"])
        self.assertEqual("Game started", plan["expected_text"])

        with self.assertRaisesRegex(ValueError, "NATIVE_CHAT_TARGET_NOT_UNIQUELY_GROUNDED"):
            validate_chat_native_button_plan({"decision": "execute", "target_label": "Ghost button",
                "expected_text": "Game started"}, "Start the game", observation)
        with self.assertRaisesRegex(ValueError, "NATIVE_CHAT_POSTCONDITION_ALREADY_VISIBLE"):
            validate_chat_native_button_plan({"decision": "execute", "target_label": "Start game",
                "expected_text": "Ready"}, "Start the game", observation)
        with self.assertRaisesRegex(ValueError, "NATIVE_CHAT_ACTION_CONFIRMATION_REQUIRED"):
            validate_chat_native_button_plan({"decision": "execute", "target_label": "Delete save",
                "expected_text": "Save deleted"}, "Delete the save", observation)

    def test_native_chat_goal_plan_can_request_a_chat_clarification(self):
        result = validate_chat_native_button_plan({"decision": "clarify",
            "clarification": "Which mode should I start?"}, "Start the game", {"ax": {}})
        self.assertEqual({"decision": "clarify", "clarification": "Which mode should I start?"}, result)

    def test_button_press_requires_exact_ax_button_and_verifies_requested_new_text(self):
        now = time.time()
        before_ax = {"elements": [
            {"id": "start", "role": "AXButton", "label": "Start", "visible": True,
             "enabled": True, "supportedActions": ["AXPress"]},
            {"id": "field", "role": "AXTextField", "label": "Start", "enabled": True,
             "supportedActions": ["AXPress"]},
        ], "text": ["Start"], "confidence": 1.0, "observationId": "button-before",
            "observedAt": now, "sessionId": "button-session", "applicationPid": 123,
            "applicationBundleId": "example.target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        decision = FakeDecisionProvider()
        planned = plan_button_press("Click the Start button and verify Started", "Start", "Started",
                                    {"ax": before_ax}, decision)
        self.assertEqual("PASS", planned["status"])
        self.assertEqual("start", planned["selected_element"]["id"])
        self.assertEqual("accessibility", planned["action_route"]["mechanism"])
        self.assertEqual([], decision.calls)

        after_ax = {**before_ax, "observationId": "button-after", "observedAt": now + 0.1,
                    "text": ["Started"]}
        verified = verify_button_press(planned["before_state"], {"ax": after_ax}, "start", "Started")
        self.assertTrue(verified["verified"])

        noop = verify_button_press(planned["before_state"], {"ax": {**after_ax,
            "observationId": "button-noop", "observedAt": now + 0.2, "text": ["Start"]}},
            "start", "Started")
        self.assertFalse(noop["verified"])
        self.assertEqual("EXPECTED_POSTCONDITION_NOT_OBSERVED", noop["reason"])

    def test_button_press_fails_closed_without_axpress_or_unique_exact_label(self):
        now = time.time()
        base = {"elements": [{"id": "start", "role": "AXButton", "label": "Start",
            "visible": True, "enabled": True, "supportedActions": []}], "confidence": 1.0,
            "observationId": "button-no-press", "observedAt": now, "sessionId": "button-session",
            "applicationPid": 123, "applicationBundleId": "example.target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        blocked = plan_button_press("Click Start and verify Started", "Start", "Started",
                                    {"ax": base}, FakeDecisionProvider())
        self.assertEqual("NO_PRESSABLE_GROUNDED_AX_BUTTON", blocked["reason"])
        self.assertEqual("NO_PRESSABLE_GROUNDED_AX_BUTTON", plan_button_press(
            "Start the game and verify Started", None, "Started", {"ax": base}, FakeDecisionProvider())["reason"])

    def test_button_without_axpress_requests_and_accepts_bound_vision_pointer_evidence(self):
        now = time.time()
        ax = {"elements": [{"id": "start", "role": "AXButton", "label": "Start",
            "frame": {"x": 0, "y": 0, "width": 0, "height": 0},
            "visible": None, "enabled": True, "supportedActions": []}], "text": ["Ready"],
            "confidence": 1.0, "observationId": "button-visual-before", "observedAt": now,
            "sessionId": "button-visual-session", "applicationPid": 123,
            "applicationBundleId": "example.target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        observation = {"ax": ax, "pointer_event_permission_granted": True}
        requested = plan_button_press("Click Start and verify Started", "Start", "Started",
                                      observation, FakeDecisionProvider())
        self.assertEqual("VISION_GROUNDING_REQUIRED", requested["status"])
        self.assertEqual({"id": "start", "label": "Start"}, requested["selected_element"])

        target = {"applicationPid": 123, "applicationBundleId": "example.target"}
        geometry = {"displayId": 1, "originX": 0, "originY": 0, "widthPoints": 1440, "heightPoints": 900}
        alignment = {"status": "READY", "captureSessionId": "capture-1", "targetApplication": target,
            "seedSequence": 8, "verifiedSequence": 9, "verifiedAt": now, "displayId": 1,
            "trackingSource": "OPENCV_LUCAS_KANADE", "trackingConfidence": 0.91,
            "targetWindowContainsPoint": True, "mappedGlobalPoint": [120, 80]}
        provenance = {"capture_session_id": "capture-1", "frame_sequence": 8,
            "tracked_frame_sequence": 9, "observed_at": now, "image_sha256": "a" * 64,
            "width": 1440, "height": 900, "target_application": target,
            "display_geometry": geometry}
        evidence = {"candidate": {"label": "Start", "bbox": [0.05, 0.05, 0.15, 0.15],
                                   "confidence": 0.88},
            "provenance": provenance, "alignment": alignment}
        planned = plan_button_press("Click Start and verify Started", "Start", "Started",
                                    {**observation, "vision_pointer": evidence}, FakeDecisionProvider())
        self.assertEqual("PASS", planned["status"])
        self.assertEqual("vision_pointer", planned["action_route"]["mechanism"])

        # The pinned image may age during local OCR and optical-flow grounding,
        # while its tracked frame and final alignment remain fresh.
        delayed_seed = {**evidence, "provenance": {**provenance,
            "observed_at": now - 9.0}}
        delayed_seed_plan = plan_button_press(
            "Click Start and verify Started", "Start", "Started",
            {**observation, "vision_pointer": delayed_seed}, FakeDecisionProvider())
        self.assertEqual("PASS", delayed_seed_plan["status"])

        expired_seed = {**evidence, "provenance": {**provenance,
            "observed_at": now - 10.1}}
        expired_seed_plan = plan_button_press(
            "Click Start and verify Started", "Start", "Started",
            {**observation, "vision_pointer": expired_seed}, FakeDecisionProvider())
        self.assertEqual("VISION_GROUNDING_REQUIRED", expired_seed_plan["status"])

        stale = {**evidence, "alignment": {**alignment, "verifiedAt": now - 3}}
        blocked = plan_button_press("Click Start and verify Started", "Start", "Started",
                                    {**observation, "vision_pointer": stale}, FakeDecisionProvider())
        self.assertEqual("VISION_GROUNDING_REQUIRED", blocked["status"])

    def test_vision_candidate_rejects_explicitly_hidden_button_even_with_pointer_permission(self):
        now = time.time()
        ax = {"elements": [{"id": "start", "role": "AXButton", "label": "Start",
            "frame": {"x": 0, "y": 0, "width": 0, "height": 0},
            "visible": False, "enabled": True, "supportedActions": []}], "text": ["Ready"],
            "confidence": 1.0, "observationId": "button-hidden", "observedAt": now,
            "sessionId": "button-hidden-session", "applicationPid": 123,
            "applicationBundleId": "example.target", "permissionGranted": True}
        result = plan_button_press("Click Start and verify Started", "Start", "Started",
            {"ax": ax, "pointer_event_permission_granted": True}, FakeDecisionProvider())
        self.assertEqual("NO_PRESSABLE_GROUNDED_AX_BUTTON", result["reason"])

    def test_button_press_semantically_selects_from_small_grounded_set_without_exact_label(self):
        now = time.time()
        before_ax = {"elements": [
            {"id": "cancel", "role": "AXButton", "label": "Cancel", "visible": True,
             "enabled": True, "supportedActions": ["AXPress"]},
            {"id": "start", "role": "AXButton", "label": "Start game", "visible": True,
             "enabled": True, "supportedActions": ["AXPress"]},
        ], "text": ["Game setup"], "confidence": 1.0, "observationId": "goal-before",
            "observedAt": now, "sessionId": "button-session", "applicationPid": 123,
            "applicationBundleId": "example.target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        decision = FakeDecisionProvider(selection=1)
        planned = plan_button_press("Start the game and verify ‘Game started’", None, "Game started",
                                    {"ax": before_ax}, decision)
        self.assertEqual("PASS", planned["status"])
        self.assertEqual("start", planned["selected_element"]["id"])
        self.assertEqual(2, len(decision.calls[0][1]))
        self.assertEqual("Start the game and verify ‘Game started’", decision.calls[0][0])

        after_ax = {**before_ax, "observationId": "goal-after", "observedAt": now + 0.1,
                    "text": ["Game started"]}
        verified = verify_button_press(planned["before_state"], {"ax": after_ax}, "start", "Game started")
        self.assertTrue(verified["verified"])

    def test_button_press_refuses_unbounded_semantic_candidate_set(self):
        now = time.time()
        before_ax = {"elements": [
            {"id": f"button-{index}", "role": "AXButton", "label": f"Action {index}", "visible": True,
             "enabled": True, "supportedActions": ["AXPress"]}
            for index in range(9)
        ], "text": ["Idle"], "confidence": 1.0, "observationId": "many-buttons",
            "observedAt": now, "sessionId": "button-session", "applicationPid": 123,
            "applicationBundleId": "example.target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        decision = FakeDecisionProvider()
        result = plan_button_press("Start and verify Started", None, "Started", {"ax": before_ax}, decision)
        self.assertEqual("TOO_MANY_GROUNDED_AX_BUTTONS", result["reason"])
        self.assertEqual([], decision.calls)

    def test_button_press_refuses_duplicate_labels_without_chat_disambiguation(self):
        now = time.time()
        before_ax = {"elements": [
            {"id": element_id, "role": "AXButton", "label": "Continue", "visible": True,
             "enabled": True, "supportedActions": ["AXPress"]}
            for element_id in ("continue-left", "continue-right")
        ], "text": ["Choose a mode"], "confidence": 1.0, "observationId": "duplicate-buttons",
            "observedAt": now, "sessionId": "button-session", "applicationPid": 123,
            "applicationBundleId": "example.target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        decision = FakeDecisionProvider()
        result = plan_button_press("Continue and verify Game ready", None, "Game ready",
                                   {"ax": before_ax}, decision)
        self.assertEqual("AMBIGUOUS_GROUNDED_AX_BUTTON_LABELS", result["reason"])
        self.assertEqual([], decision.calls)

    def test_heading_question_prefers_heading_over_ready_status(self):
        decision = FakeDecisionProvider(selection=0)
        result = resolve_domax_question("What is the current project heading?", {
            "dom": {"elements": [
                {"id": "ready", "role": "status", "label": "READY", "visible": True},
                {"id": "project", "role": "heading", "label": "test", "level": 1, "visible": True},
                {"id": "prompt", "role": "heading", "label": "Ask OLCR anything", "level": 2, "visible": True},
            ], "confidence": 1.0}
        }, decision)
        self.assertEqual("PASS", result["status"])
        self.assertEqual("test", result["answer"])
        self.assertEqual([], decision.calls)

    def test_dom_question_uses_grounded_semantic_candidate_and_no_vision(self):
        decision = FakeDecisionProvider(selection=1)
        result = resolve_domax_question("What is the page status?", {
            "dom": {"elements": [
                {"id": "title", "role": "heading", "label": "Workspace", "visible": True},
                {"id": "status", "role": "status", "label": "READY", "visible": True},
            ], "text": ["Workspace", "READY"], "confidence": 1.0}
        }, decision)
        self.assertEqual("PASS", result["status"])
        self.assertEqual("READY", result["answer"])
        self.assertEqual("DOM", result["observation_source"])
        self.assertEqual(0, result["vision_model_calls"])
        self.assertFalse(result["action_executed"])
        self.assertEqual([], decision.calls)

    def test_ax_is_preferred_over_dom_and_coordinates_are_not_returned(self):
        result = resolve_domax_question("Read the status", {
            "dom": {"elements": [{"id": "dom", "role": "status", "label": "DOM"}]},
            "ax": {"elements": [{"id": "ax", "role": "status", "label": "AX", "bbox": [1, 2, 3, 4]}]},
        }, FakeDecisionProvider())
        self.assertEqual("AX", result["observation_source"])
        self.assertEqual("AX", result["answer"])
        self.assertNotIn("bbox", result["selected_element"])

    def test_image_only_or_missing_domax_does_not_fall_back_to_vision(self):
        result = resolve_domax_question("Read the status", {"image": {"elements": [{"id": "x", "label": "X"}]}}, FakeDecisionProvider())
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual("DOMAX_STATE_REQUIRED", result["reason"])
        self.assertNotIn("vision_model_calls", result)

    def test_unresolved_choice_fails_closed(self):
        result = resolve_domax_question("Read status", {
            "dom": {"elements": [{"id": "a", "label": "READY"}, {"id": "b", "label": "BUSY"}]}
        }, FakeDecisionProvider(selection=None))
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual("INVALID_SEMANTIC_DECISION", result["reason"])

    def test_disclosure_plan_is_grounded_and_fresh_expanded_state_verifies(self):
        before={"dom":{"elements":[{"id":"context","role":"disclosure","label":"Project Core Context",
                                      "visible":True,"enabled":True,"expanded":False}],
                        "text":["Project Core Context"],"confidence":1.0,
                        "observation_id":"before-1","observed_at":time.time(),"session_id":"webview-1"}}
        plan=plan_disclosure_open("Open Project Core Context",before,FakeDecisionProvider())
        self.assertEqual("PASS",plan["status"])
        self.assertEqual("open_disclosure:context",plan["semantic_action"]["tool_id"])
        self.assertEqual("DOM",plan["observation_source"])
        self.assertEqual("native_structured",plan["action_route"]["mechanism"])
        self.assertTrue(plan["action_route"]["verification_required"])
        after={"dom":{"elements":[{"id":"context","role":"disclosure","label":"Project Core Context",
                                     "visible":True,"enabled":True,"expanded":True}],
                       "text":["Project Core Context"],"confidence":1.0,
                       "observation_id":"after-1","observed_at":time.time()+1,"session_id":"webview-1"}}
        result=verify_disclosure_open(plan["before_state"],after,"context")
        self.assertTrue(result["verified"])

    def test_disclosure_noop_is_rejected_by_postcondition_verifier(self):
        before={"dom":{"elements":[{"id":"context","role":"disclosure","label":"Project Core Context",
                                      "visible":True,"enabled":True,"expanded":False}],
                        "observation_id":"before-noop","observed_at":time.time(),"session_id":"webview-1"}}
        plan=plan_disclosure_open("Open Project Core Context",before,FakeDecisionProvider())
        noop={"dom":{"elements":[{"id":"context","role":"disclosure","label":"Project Core Context",
                                    "visible":True,"enabled":True,"expanded":False}],
                      "observation_id":"after-noop","observed_at":time.time()+0.1,"session_id":"webview-1"}}
        result=verify_disclosure_open(plan["before_state"],noop,"context")
        self.assertFalse(result["verified"])
        self.assertEqual("EXPECTED_POSTCONDITION_NOT_OBSERVED",result["reason"])

    def test_action_router_blocks_stale_dom_and_ax_without_supported_press(self):
        stale={"dom":{"elements":[{"id":"context","role":"disclosure","label":"Project Core Context",
                                      "visible":True,"enabled":True,"expanded":False}],
                        "observation_id":"old-dom","observed_at":time.time()-60,"session_id":"webview-1"}}
        stale_plan=plan_disclosure_open("Open Project Core Context",stale,FakeDecisionProvider())
        self.assertEqual("BLOCKED",stale_plan["status"])
        self.assertEqual("NO_RELIABLE_SAFE_VERIFIABLE_MECHANISM",stale_plan["reason"])
        ax={"ax":{"elements":[{"id":"context","role":"AXDisclosureTriangle","label":"Project Core Context",
                                  "enabled":True,"expanded":False}],
                   "observationId":"ax-1","observedAt":time.time(),"sessionId":"macos-ax-1"}}
        ax_plan=plan_disclosure_open("Open Project Core Context",ax,FakeDecisionProvider())
        self.assertEqual("BLOCKED",ax_plan["status"])
        self.assertEqual("NO_CLOSED_GROUNDED_DISCLOSURE",ax_plan["reason"])

    def test_ax_disclosure_without_axpress_uses_identity_bound_geometry_fallback(self):
        observation={"pointer_event_permission_granted":True,"ax":{"elements":[{"id":"context","role":"AXDisclosureTriangle",
            "label":"Project Core Context","enabled":True,"expanded":False,
            "frame":{"x":120,"y":80,"width":14,"height":14},"supportedActions":[]}],
            "observationId":"ax-no-press","observedAt":time.time(),"sessionId":"macos-ax-123",
            "applicationPid":123,"applicationBundleId":"example.target","permissionGranted":True,
            "coordinateSpace":"ax_global_screen_coordinates"}}

        plan=plan_disclosure_open("Open Project Core Context",observation,FakeDecisionProvider())

        self.assertEqual("PASS",plan["status"])
        self.assertEqual("deterministic_input",plan["action_route"]["mechanism"])
        self.assertEqual("ax-no-press",plan["action_route"]["observation_id"])
        self.assertEqual("macos-ax-123",plan["action_route"]["session_id"])

    def test_ax_disclosure_uses_vision_pointer_only_when_ax_geometry_is_missing(self):
        now = time.time()
        ax = {"elements": [{"id": "context", "role": "AXDisclosureTriangle",
            "label": "Project Core Context", "enabled": True, "expanded": False,
            "frame": None, "supportedActions": []}],
            "observationId": "ax-vision-before", "observedAt": now, "sessionId": "macos-ax-vision",
            "applicationPid": 123, "applicationBundleId": "example.target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        evidence = {"candidate": {"id": "candidate-1", "label": "Project Core Context",
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
        observation = {"pointer_event_permission_granted": True, "ax": ax, "vision_pointer": evidence}

        plan = plan_disclosure_open("Open Project Core Context", observation, FakeDecisionProvider())

        self.assertEqual("PASS", plan["status"])
        self.assertEqual("vision_pointer", plan["action_route"]["mechanism"])
        self.assertTrue(plan["action_route"]["verification_required"])
        after = {"ax": {**ax, "observationId": "ax-vision-after", "observedAt": now + 0.2,
            "elements": [{**ax["elements"][0], "expanded": True}]}}
        verified = verify_disclosure_open(plan["before_state"], after, "context")
        self.assertTrue(verified["verified"])

        stale = {**evidence, "alignment": {**evidence["alignment"], "verifiedAt": now - 3}}
        self.assertEqual("BLOCKED", plan_disclosure_open("Open Project Core Context",
            {"pointer_event_permission_granted": True, "ax": ax, "vision_pointer": stale},
            FakeDecisionProvider())["status"])

        duplicate = {**ax, "elements": [*ax["elements"],
            {**ax["elements"][0], "id": "context-duplicate", "role": "AXButton"}]}
        ambiguous_plan = plan_disclosure_open("Open Project Core Context", {
            "pointer_event_permission_granted": True, "ax": duplicate, "vision_pointer": evidence,
        }, FakeDecisionProvider())
        self.assertEqual("BLOCKED", ambiguous_plan["status"])
        self.assertEqual("NO_CLOSED_GROUNDED_DISCLOSURE", ambiguous_plan["reason"])

    def test_ax_geometry_route_precedes_valid_vision_pointer_evidence(self):
        now = time.time()
        ax = {"elements": [{"id": "context", "role": "AXDisclosureTriangle",
            "label": "Project Core Context", "enabled": True, "expanded": False,
            "frame": {"x": 10, "y": 20, "width": 18, "height": 18}, "supportedActions": []}],
            "observationId": "ax-prefer-a4", "observedAt": now, "sessionId": "macos-ax-prefer-a4",
            "applicationPid": 123, "applicationBundleId": "example.target", "permissionGranted": True,
            "coordinateSpace": "ax_global_screen_coordinates"}
        evidence = {"candidate": {"id": "candidate-1", "label": "Project Core Context",
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

        plan = plan_disclosure_open("Open Project Core Context", {
            "pointer_event_permission_granted": True, "ax": ax, "vision_pointer": evidence,
        }, FakeDecisionProvider())

        self.assertEqual("PASS", plan["status"])
        self.assertEqual("deterministic_input", plan["action_route"]["mechanism"])

    def test_ax_disclosure_pointer_fallback_requires_bundle_identity_and_valid_geometry(self):
        base={"elements":[{"id":"context","role":"AXDisclosureTriangle",
            "label":"Project Core Context","enabled":True,"expanded":False,
            "frame":{"x":120,"y":80,"width":14,"height":14},"supportedActions":[]}],
            "observationId":"ax-no-press","observedAt":time.time(),"sessionId":"macos-ax-123",
            "applicationPid":123,"applicationBundleId":"example.target","permissionGranted":True,
            "coordinateSpace":"ax_global_screen_coordinates"}
        missing_bundle={"pointer_event_permission_granted":True,"ax":{**base,"applicationBundleId":""}}
        malformed_frame={"pointer_event_permission_granted":True,"ax":{**base,"elements":[{**base["elements"][0],
            "frame":{"x":float("nan"),"y":80,"width":14,"height":14}}]}}
        self.assertEqual("BLOCKED",plan_disclosure_open("Open Project Core Context",missing_bundle,
            FakeDecisionProvider())["status"])
        self.assertEqual("BLOCKED",plan_disclosure_open("Open Project Core Context",malformed_frame,
            FakeDecisionProvider())["status"])
        no_pointer_permission={"ax":base}
        self.assertEqual("BLOCKED",plan_disclosure_open("Open Project Core Context",no_pointer_permission,
            FakeDecisionProvider())["status"])

    def test_find_shortcut_uses_menu_provenance_and_verifies_fresh_search_focus(self):
        before={"ax":{"elements":[{"id":"find-1","role":"AXMenuItem","label":"Find…","enabled":True,
            "keyboardShortcut":{"keyCode":3,"modifiers":0,"key":"f","provenance":"application_menu"}}],
            "observationId":"menu-before","observedAt":time.time(),"sessionId":"macos-ax-123",
            "applicationPid":123,"permissionGranted":True,
            "coordinateSpace":"ax_global_screen_coordinates"}}
        plan=plan_open_find_shortcut("Open the search field",before,FakeDecisionProvider())
        self.assertEqual("PASS",plan["status"])
        self.assertEqual("keyboard_shortcut",plan["action_route"]["mechanism"])
        self.assertEqual("application_menu",plan["keyboard_shortcut"]["provenance"])
        after={"ax":{"elements":[{"id":"search-1","role":"AXSearchField","label":"Search","focused":True}],
            "observationId":"menu-after","observedAt":time.time()+1,"sessionId":"macos-ax-123",
            "applicationPid":123,"permissionGranted":True,
            "coordinateSpace":"ax_global_screen_coordinates"}}
        self.assertTrue(verify_find_shortcut(plan["before_state"],after)["verified"])

    def test_find_shortcut_blocks_stale_or_convention_only_evidence(self):
        item={"id":"find-1","role":"AXMenuItem","label":"Search","enabled":True,
              "keyboardShortcut":{"keyCode":4,"modifiers":0,"key":"h","provenance":"application_menu"}}
        stale={"ax":{"elements":[item],"observationId":"old-menu","observedAt":time.time()-60,
            "sessionId":"macos-ax-123","applicationPid":123,"permissionGranted":True}}
        self.assertEqual("BLOCKED",plan_open_find_shortcut("Open search",stale,FakeDecisionProvider())["status"])
        convention={"ax":{"elements":[{**item,"keyboardShortcut":{**item["keyboardShortcut"],"provenance":"platform_convention"}}],
            "observationId":"menu-1","observedAt":time.time(),"sessionId":"macos-ax-123",
            "applicationPid":123,"permissionGranted":True}}
        self.assertEqual("NO_MENU_DISCOVERED_FIND_SHORTCUT",
            plan_open_find_shortcut("Open search",convention,FakeDecisionProvider())["reason"])

    def test_ax_disclosure_uses_accessibility_route_and_verifies_fresh_expansion(self):
        before={"ax":{"elements":[{"id":"ax-2","role":"AXDisclosureTriangle","label":"Project Core Context",
                                      "frame":{"x":5,"y":10,"width":20,"height":20},
                                      "enabled":True,"expanded":False,"supportedActions":["AXPress"]}],
                        "text":["Project Core Context"],"observationId":"ax-before","observedAt":time.time(),
                        "sessionId":"macos-ax-123","applicationPid":123,"permissionGranted":True,
                        "applicationBundleId":"example.target",
                        "coordinateSpace":"ax_global_screen_coordinates"}}
        plan=plan_disclosure_open("Open Project Core Context",before,FakeDecisionProvider())
        self.assertEqual("PASS",plan["status"])
        self.assertEqual("accessibility",plan["action_route"]["mechanism"])
        self.assertEqual("ax-before",plan["action_route"]["observation_id"])
        after={"ax":{"elements":[{"id":"ax-2","role":"AXDisclosureTriangle","label":"Project Core Context",
                                     "frame":{"x":5,"y":10,"width":20,"height":20},
                                     "enabled":True,"expanded":True,"supportedActions":["AXPress"]}],
                       "text":["Project Core Context"],"observationId":"ax-after","observedAt":time.time()+1,
                       "sessionId":"macos-ax-123","applicationPid":123,"applicationBundleId":"example.target",
                       "permissionGranted":True,
                       "coordinateSpace":"ax_global_screen_coordinates"}}
        result=verify_disclosure_open(plan["before_state"],after,"ax-2")
        self.assertTrue(result["verified"])

    def test_ax_disclosure_verification_requires_same_application_identity(self):
        before={"ax":{"elements":[{"id":"ax-2","role":"AXDisclosureTriangle","label":"Details",
            "frame":{"x":5,"y":10,"width":20,"height":20},"enabled":True,"expanded":False}],
            "observationId":"before","observedAt":time.time(),"sessionId":"macos-ax-123",
            "applicationPid":123,"applicationBundleId":"example.target","permissionGranted":True,
            "coordinateSpace":"ax_global_screen_coordinates"}}
        plan=plan_disclosure_open("Open Details",{"ax":before["ax"],
            "pointer_event_permission_granted":True},FakeDecisionProvider())
        after={"ax":{"elements":[{"id":"ax-2","role":"AXDisclosureTriangle","label":"Details",
            "frame":{"x":5,"y":10,"width":20,"height":20},"enabled":True,"expanded":True}],
            "observationId":"after","observedAt":time.time()+1,"sessionId":"macos-ax-123",
            "applicationPid":123,"applicationBundleId":"other.target","permissionGranted":True,
            "coordinateSpace":"ax_global_screen_coordinates"}}
        result=verify_disclosure_open(plan["before_state"],after,"ax-2")
        self.assertFalse(result["verified"])
        self.assertEqual("AX_APPLICATION_IDENTITY_MISMATCH",result["reason"])


if __name__ == "__main__":
    unittest.main()
