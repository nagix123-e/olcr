import time
import unittest

from olcr_api.native_goal import validate_goal_action


def observation():
    return {"ax": {"permissionGranted": True, "applicationPid": 42,
        "applicationBundleId": "example.app", "observedAt": time.time(), "confidence": 1,
        "observationId": "ax-1", "sessionId": "session-1", "text": ["Search"],
        "elements": [{"id": "field", "role": "AXTextField", "label": "Search", "visible": True, "enabled": True}]}}


class NativeGoalTests(unittest.TestCase):
    def test_goal_input_is_grounded_and_must_come_from_user_literal(self):
        plan = {"decision": "execute", "kind": "type_text", "target_label": "Search", "input_text": "青い鳥"}
        result = validate_goal_action(plan, '「青い鳥」を入力して検索し結果を確認して', observation())
        self.assertEqual("青い鳥", result["action"]["inputText"])
        with self.assertRaisesRegex(ValueError, "INPUT_NOT_USER_LITERAL"):
            validate_goal_action({**plan, "input_text": "invented"}, '「青い鳥」を検索して', observation())

    def test_goal_rejects_ambiguous_secure_and_stale_fields(self):
        plan = {"decision": "execute", "kind": "type_text", "target_label": "Search", "input_text": "bird"}
        ax = observation()
        ax["ax"]["elements"] *= 2
        with self.assertRaisesRegex(ValueError, "TARGET_NOT_UNIQUE"):
            validate_goal_action(plan, 'Search for "bird"', ax)
        ax = observation()
        ax["ax"]["elements"][0]["role"] = "AXSecureTextField"
        with self.assertRaisesRegex(ValueError, "SECURE"):
            validate_goal_action(plan, 'Search for "bird"', ax)
        ax["ax"]["observedAt"] -= 31
        with self.assertRaisesRegex(ValueError, "STALE"):
            validate_goal_action({"decision": "wait"}, "Search", ax)

    def test_goal_completion_requires_visible_evidence_and_key_requires_new_effect(self):
        with self.assertRaisesRegex(ValueError, "EVIDENCE_NOT_VISIBLE"):
            validate_goal_action({"decision": "complete", "completion_text": "Results"}, "Search", observation())
        with self.assertRaisesRegex(ValueError, "POSTCONDITION_INVALID"):
            validate_goal_action({"decision": "execute", "kind": "press_key", "key": "Enter", "expected_text": "Search"}, "Search", observation())

    def test_goal_can_propose_existing_visual_button_route_without_weakening_legacy_validation(self):
        from olcr_api.computer_use import validate_chat_native_button_plan
        ax = observation()
        button = {"id": "visual-button", "role": "AXButton", "label": "Start", "visible": None,
                  "enabled": True, "supportedActions": [], "frame": {"x": 0, "y": 0, "width": 0, "height": 0}}
        ax["ax"]["elements"] = [button]
        plan = {"decision": "execute", "kind": "press_button", "target_label": "Start", "expected_text": "Started"}
        self.assertEqual("press_button", validate_goal_action(plan, "Start the demo", ax)["kind"])
        with self.assertRaisesRegex(ValueError, "TARGET_NOT_UNIQUELY_GROUNDED"):
            validate_chat_native_button_plan(plan, "Start the demo", ax)
        button["visible"] = False
        with self.assertRaisesRegex(ValueError, "TARGET_NOT_UNIQUELY_GROUNDED"):
            validate_goal_action(plan, "Start the demo", ax)


if __name__ == "__main__":
    unittest.main()
