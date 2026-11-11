import time
import unittest
from unittest.mock import patch

from olcr_api.providers import ActionRouter, DeterministicActionExecutor, KeyboardShortcutRegistry, SemanticActionRuntime
from olcr_api.vision import ScreenState


class FakeDecisionProvider:
    def choice(self, *, question, candidates, language="multilingual"):
        return {"tool_id": candidates[0]["tool_id"], "confidence": 1.0}


class SemanticActionTests(unittest.TestCase):
    def test_action_router_records_decision_once_without_changing_return_contract(self):
        candidate = self._route_candidate("native_structured")
        with patch("olcr_api.providers.model_telemetry") as telemetry:
            result = ActionRouter().route(
                task_semantics="open disclosure", now=100.0, candidates=[candidate]
            )

        self.assertEqual("PASS", result["status"])
        self.assertEqual("native_structured", result["mechanism"])
        telemetry.record_action_router_decision.assert_called_once_with(result)

    def test_shortcut_registry_accepts_only_discovered_find_menu_affordances(self):
        observed = [{"id":"find-1","role":"AXMenuItem","label":"Find…",
            "keyboardShortcut":{"keyCode":3,"modifiers":0,"key":"f","provenance":"application_menu"}},
            {"id":"delete-1","role":"AXMenuItem","label":"Delete",
             "keyboardShortcut":{"keyCode":51,"modifiers":0,"key":"delete","provenance":"application_menu"}},
            {"id":"guess-1","role":"AXMenuItem","label":"Search",
             "keyboardShortcut":{"keyCode":4,"modifiers":0,"key":"h","provenance":"platform_convention"}}]
        actions=KeyboardShortcutRegistry.find_actions(elements=observed,task_semantics="open_find")
        self.assertEqual(1,len(actions))
        self.assertEqual("find-1",actions[0]["element_id"])
        self.assertEqual("application_menu",actions[0]["shortcut"]["provenance"])
        self.assertEqual([],KeyboardShortcutRegistry.find_actions(elements=observed,task_semantics="delete"))

    def test_shortcut_registry_rejects_invalid_or_unattributed_key_data(self):
        observed=[{"id":"bad","role":"AXMenuItem","label":"Find",
            "keyboardShortcut":{"keyCode":256,"modifiers":0,"key":"f","provenance":"application_menu"}},
            {"id":"mixed","role":"AXMenuItem","label":"Search",
             "keyboardShortcut":{"keyCode":4,"modifiers":9,"key":"h","provenance":"application_menu"}},
            {"id":"unknown","role":"AXMenuItem","label":"Search",
             "keyboardShortcut":{"keyCode":4,"modifiers":0,"key":"h","provenance":"platform_convention"}}]
        self.assertEqual([],KeyboardShortcutRegistry.find_actions(elements=observed,task_semantics="open_find"))

    @staticmethod
    def _route_candidate(mechanism, **overrides):
        return {"mechanism":mechanism,"available":True,"semantics_compatible":True,
                "observed_at":99.0,"observation_id":"obs-1","session_id":"session-1",
                "reliability":0.9,"expected_cost":1.0,"verification_available":True,
                "security_allowed":True,**overrides}

    def test_action_router_chooses_cheapest_reliable_mechanism_with_bound_state(self):
        result=ActionRouter().route(task_semantics="open disclosure",now=100.0,candidates=[
            self._route_candidate("native_structured",expected_cost=0.8),
            self._route_candidate("playwright",expected_cost=0.3),
        ])
        self.assertEqual("PASS",result["status"])
        self.assertEqual("playwright",result["mechanism"])
        self.assertEqual("obs-1",result["observation_id"])
        self.assertEqual("session-1",result["session_id"])
        self.assertTrue(result["verification_required"])

    def test_action_router_rejects_semantic_security_and_verification_mismatches(self):
        unsafe=self._route_candidate("native_structured",expected_cost=0.1,security_allowed=False)
        mismatched=self._route_candidate("playwright",expected_cost=0.2,semantics_compatible=False)
        safe=self._route_candidate("accessibility",expected_cost=2.0)
        result=ActionRouter().route(task_semantics="open disclosure",now=100.0,
                                    candidates=[unsafe,mismatched,safe])
        self.assertEqual("accessibility",result["mechanism"])
        self.assertIn("SECURITY_POLICY_REJECTED",result["considered"][0]["rejected_reasons"])
        self.assertIn("TASK_SEMANTICS_MISMATCH",result["considered"][1]["rejected_reasons"])

    def test_action_router_fails_closed_without_fresh_verifiable_safe_route(self):
        stale=self._route_candidate("native_structured",observed_at=1.0)
        unverifiable=self._route_candidate("playwright",verification_available=False)
        result=ActionRouter().route(task_semantics="open disclosure",now=100.0,
                                    candidates=[stale,unverifiable])
        self.assertEqual("BLOCKED",result["status"])
        self.assertEqual("NO_RELIABLE_SAFE_VERIFIABLE_MECHANISM",result["reason"])

    def test_action_router_rejects_nonfinite_or_unrepresentable_cost_inputs(self):
        invalid=self._route_candidate("native_structured",reliability=10**1000,expected_cost=float("inf"))
        result=ActionRouter().route(task_semantics="open disclosure",now=100.0,candidates=[invalid])
        self.assertEqual("BLOCKED",result["status"])
        self.assertIn("RELIABILITY_INVALID",result["considered"][0]["rejected_reasons"])
        self.assertIn("EXPECTED_COST_INVALID",result["considered"][0]["rejected_reasons"])

    def test_vision_pointer_requires_bound_grounding_transform_identity_and_postcondition(self):
        candidate = self._route_candidate("vision_pointer")
        result = ActionRouter().route(task_semantics="open disclosure", now=100.0,
                                      candidates=[candidate])
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual({
            "VISION_GROUNDING_PROVENANCE_UNBOUND",
            "VISION_COORDINATE_TRANSFORM_UNVERIFIED",
            "VISION_TARGET_IDENTITY_UNBOUND",
            "VISION_POSTCONDITION_UNAVAILABLE",
        }, set(result["considered"][0]["rejected_reasons"]))

        candidate.update(grounding_provenance_bound=True,
                         coordinate_transform_verified=True,
                         target_identity_bound=True,
                         independent_postcondition=True)
        result = ActionRouter().route(task_semantics="open disclosure", now=100.0,
                                      candidates=[candidate])
        self.assertEqual("PASS", result["status"])
        self.assertEqual("vision_pointer", result["mechanism"])

    def test_vision_pointer_is_only_selected_when_no_preferred_route_is_viable(self):
        vision = self._route_candidate("vision_pointer", expected_cost=0.01,
            grounding_provenance_bound=True, coordinate_transform_verified=True,
            target_identity_bound=True, independent_postcondition=True)
        accessibility = self._route_candidate("accessibility", expected_cost=10.0)
        result = ActionRouter().route(task_semantics="open target", now=100.0,
                                      candidates=[vision, accessibility])
        self.assertEqual("PASS", result["status"])
        self.assertEqual("accessibility", result["mechanism"])
        self.assertIn("PREFERRED_ACTION_ROUTE_AVAILABLE",
                      result["considered"][0]["rejected_reasons"])

        fallback = ActionRouter().route(task_semantics="open target", now=100.0,
                                        candidates=[vision])
        self.assertEqual("PASS", fallback["status"])
        self.assertEqual("vision_pointer", fallback["mechanism"])

    def test_laya_selects_semantic_id_and_executor_owns_coordinates(self):
        observed = []
        runtime = SemanticActionRuntime(
            FakeDecisionProvider(),
            DeterministicActionExecutor({"click": lambda action: observed.append(action["element_id"]) or "clicked"}),
        )
        state = ScreenState("AX", 100, 100, ({"id": "save", "role": "button", "label": "Save", "bbox": [0, 0, 1, 1]},), (), 1.0, ())
        result = runtime.choose_and_execute(state, "save the document")
        self.assertEqual("PASS", result["status"])
        self.assertEqual(["save"], observed)
        self.assertNotIn("x", result["semantic_action"])
        self.assertEqual("save", result["semantic_action"]["element_id"])

    def test_post_action_verification_rejects_stale_element(self):
        now = time.time()
        before = ScreenState("AX", 100, 100, ({"id": "send"},), (), 1.0, (),
                             observation_id="before", observed_at=now, session_id="ax-1",
                             coordinate_space="ax_global_screen_coordinates", application_pid=42,
                             application_bundle_id="com.example.app", permission_granted=True)
        after = ScreenState("AX", 100, 100, (), (), 1.0, (),
                            observation_id="after", observed_at=now + 0.01, session_id="ax-1",
                            coordinate_space="ax_global_screen_coordinates", application_pid=42,
                            application_bundle_id="com.example.app", permission_granted=True)
        result = SemanticActionRuntime.verify_post_action(before, after, "send")
        self.assertFalse(result["verified"])
        self.assertTrue(result["stale_state_rejected"])
        self.assertEqual("EXPECTED_POSTCONDITION_REQUIRED", result["reason"])

    def test_post_action_requires_a_newly_observed_expected_transition(self):
        now = time.time()
        before = ScreenState("AX", 100, 100, ({"id": "save"},), ("Save",), 1.0, (),
                             observation_id="before", observed_at=now, session_id="ax-1",
                             coordinate_space="ax_global_screen_coordinates", application_pid=42,
                             application_bundle_id="com.example.app", permission_granted=True)
        after = ScreenState("AX", 100, 100, ({"id": "save"},), ("Saved",), 1.0, (),
                            observation_id="after", observed_at=now + 0.01, session_id="ax-1",
                            coordinate_space="ax_global_screen_coordinates", application_pid=42,
                            application_bundle_id="com.example.app", permission_granted=True)
        result = SemanticActionRuntime.verify_post_action(
            before, after, "save", expected_text_contains=("Saved",))
        self.assertTrue(result["verified"])
        self.assertEqual("EXPECTED_STATE_TRANSITION_VERIFIED", result["reason"])

    def test_post_action_noop_does_not_satisfy_transition_verification(self):
        now = time.time()
        before = ScreenState("AX", 100, 100, ({"id": "save"},), ("Save",), 1.0, (),
                             observation_id="before", observed_at=now, session_id="ax-1",
                             coordinate_space="ax_global_screen_coordinates", application_pid=42,
                             application_bundle_id="com.example.app", permission_granted=True)
        after = ScreenState("AX", 100, 100, ({"id": "save"},), ("Save",), 1.0, (),
                            observation_id="after", observed_at=now + 0.01, session_id="ax-1",
                            coordinate_space="ax_global_screen_coordinates", application_pid=42,
                            application_bundle_id="com.example.app", permission_granted=True)
        result = SemanticActionRuntime.verify_post_action(
            before, after, "save", expected_text_contains=("Saved",))
        self.assertFalse(result["verified"])
        self.assertEqual("EXPECTED_POSTCONDITION_NOT_OBSERVED", result["reason"])

    def test_post_action_ax_requires_same_permitted_application_identity(self):
        now = time.time()
        before = ScreenState("AX", 100, 100, ({"id": "save"},), ("Save",), 1.0, (),
                             observation_id="before", observed_at=now, session_id="ax-1",
                             coordinate_space="ax_global_screen_coordinates", application_pid=42,
                             application_bundle_id="com.example.app", permission_granted=True)
        cases = (
            ScreenState("AX", 100, 100, ({"id": "save"},), ("Saved",), 1.0, (),
                        observation_id="after-pid", observed_at=now + 0.01, session_id="ax-1",
                        coordinate_space="ax_global_screen_coordinates", application_pid=43,
                        application_bundle_id="com.example.app", permission_granted=True),
            ScreenState("AX", 100, 100, ({"id": "save"},), ("Saved",), 1.0, (),
                        observation_id="after-bundle", observed_at=now + 0.01, session_id="ax-1",
                        coordinate_space="ax_global_screen_coordinates", application_pid=42,
                        application_bundle_id="com.other.app", permission_granted=True),
            ScreenState("AX", 100, 100, ({"id": "save"},), ("Saved",), 1.0, (),
                        observation_id="after-permission", observed_at=now + 0.01, session_id="ax-1",
                        coordinate_space="ax_global_screen_coordinates", application_pid=42,
                        application_bundle_id="com.example.app", permission_granted=False),
        )
        for after in cases:
            with self.subTest(observation=after.observation_id):
                result = SemanticActionRuntime.verify_post_action(
                    before, after, "save", expected_text_contains=("Saved",))
                self.assertFalse(result["verified"])
                self.assertEqual("INCONCLUSIVE", result["result"])
                self.assertEqual("AX_APPLICATION_IDENTITY_MISMATCH", result["reason"])

    def test_post_action_ax_requires_identity_fields_even_when_session_id_matches(self):
        now = time.time()
        before = ScreenState("AX", 100, 100, ({"id": "save"},), ("Save",), 1.0, (),
                             observation_id="before", observed_at=now, session_id="ax-1",
                             coordinate_space="ax_global_screen_coordinates")
        after = ScreenState("AX", 100, 100, ({"id": "save"},), ("Saved",), 1.0, (),
                            observation_id="after", observed_at=now + 0.01, session_id="ax-1",
                            coordinate_space="ax_global_screen_coordinates")
        result = SemanticActionRuntime.verify_post_action(
            before, after, "save", expected_text_contains=("Saved",))
        self.assertFalse(result["verified"])
        self.assertEqual("INCONCLUSIVE", result["result"])
        self.assertEqual("AX_APPLICATION_IDENTITY_MISMATCH", result["reason"])

    def test_post_action_ax_rejects_boolean_pid_even_if_it_equals_integer_pid(self):
        now = time.time()
        before = ScreenState("AX", 100, 100, ({"id": "save"},), ("Save",), 1.0, (),
                             observation_id="before", observed_at=now, session_id="ax-1",
                             coordinate_space="ax_global_screen_coordinates", application_pid=1,
                             application_bundle_id="com.example.app", permission_granted=True)
        after = ScreenState("AX", 100, 100, ({"id": "save"},), ("Saved",), 1.0, (),
                            observation_id="after", observed_at=now + 0.01, session_id="ax-1",
                            coordinate_space="ax_global_screen_coordinates", application_pid=True,
                            application_bundle_id="com.example.app", permission_granted=True)
        result = SemanticActionRuntime.verify_post_action(
            before, after, "save", expected_text_contains=("Saved",))
        self.assertFalse(result["verified"])
        self.assertEqual("AX_APPLICATION_IDENTITY_MISMATCH", result["reason"])

    def test_post_action_incomplete_or_low_confidence_evidence_is_inconclusive(self):
        now = time.time()
        before = ScreenState("DOM", None, None, ({"id": "save"},), (), 1.0, (),
                             observation_id="before", observed_at=now, session_id="webview-1")
        incomplete = ScreenState("DOM", None, None, ({"id": "save"},), ("Saved",), 0.4,
                                 ("truncated",), observation_id="after", observed_at=now + 0.01,
                                 session_id="webview-1")
        result = SemanticActionRuntime.verify_post_action(
            before, incomplete, "save", expected_text_contains=("Saved",))
        self.assertFalse(result["verified"])
        self.assertEqual("INCONCLUSIVE", result["result"])
        self.assertEqual("OBSERVATION_COVERAGE_INSUFFICIENT", result["reason"])

    def test_post_action_missing_provenance_is_inconclusive(self):
        before = ScreenState("DOM", None, None, ({"id": "save"},), (), 1.0, ())
        after = ScreenState("DOM", None, None, ({"id": "save"},), ("Saved",), 1.0, ())
        result = SemanticActionRuntime.verify_post_action(
            before, after, "save", expected_text_contains=("Saved",))
        self.assertFalse(result["verified"])
        self.assertEqual("INCONCLUSIVE", result["result"])
        self.assertEqual("OBSERVATION_SESSION_REQUIRED", result["reason"])

    def test_post_action_stale_evidence_is_inconclusive(self):
        now = time.time()
        before = ScreenState("DOM", None, None, ({"id": "save"},), (), 1.0, (),
                             observation_id="before", observed_at=now - 40,
                             session_id="webview-1")
        after = ScreenState("DOM", None, None, ({"id": "save"},), ("Saved",), 1.0, (),
                            observation_id="after", observed_at=now - 39.99,
                            session_id="webview-1")
        result = SemanticActionRuntime.verify_post_action(
            before, after, "save", expected_text_contains=("Saved",))
        self.assertFalse(result["verified"])
        self.assertEqual("INCONCLUSIVE", result["result"])
        self.assertEqual("OBSERVATION_STALE", result["reason"])

    def test_post_action_rejects_replayed_observation(self):
        now = time.time()
        before = ScreenState("DOM", 100, 100, ({"id": "details", "expanded": False},), (), 1.0, (),
                             observation_id="before", observed_at=now, session_id="webview-1")
        replay = ScreenState("DOM", 100, 100, ({"id": "details", "expanded": True},), (), 1.0, (),
                             observation_id="before", observed_at=now, session_id="webview-1")
        result = SemanticActionRuntime.verify_post_action(
            before, replay, "details", expected_element_states=(("details", "expanded", True),))
        self.assertFalse(result["verified"])
        self.assertEqual("INCONCLUSIVE", result["result"])
        self.assertEqual("POST_ACTION_OBSERVATION_NOT_NEWER", result["reason"])

    def test_post_action_rejects_same_observation_id_even_with_later_timestamp(self):
        now = time.time()
        before = ScreenState("DOM", 100, 100, ({"id": "details", "expanded": False},), (), 1.0, (),
                             observation_id="same", observed_at=now, session_id="webview-1")
        after = ScreenState("DOM", 100, 100, ({"id": "details", "expanded": True},), (), 1.0, (),
                            observation_id="same", observed_at=now + 0.01, session_id="webview-1")
        result = SemanticActionRuntime.verify_post_action(
            before, after, "details", expected_element_states=(("details", "expanded", True),))
        self.assertFalse(result["verified"])
        self.assertEqual("INCONCLUSIVE", result["result"])
        self.assertEqual("POST_ACTION_OBSERVATION_REPLAYED", result["reason"])

    def test_post_action_rejects_observation_from_another_session(self):
        now = time.time()
        before = ScreenState("DOM", 100, 100, ({"id": "details", "expanded": False},), (), 1.0, (),
                             observation_id="before", observed_at=now, session_id="webview-1")
        after = ScreenState("DOM", 100, 100, ({"id": "details", "expanded": True},), (), 1.0, (),
                            observation_id="after", observed_at=now + 0.01, session_id="webview-2")
        result = SemanticActionRuntime.verify_post_action(
            before, after, "details", expected_element_states=(("details", "expanded", True),))
        self.assertFalse(result["verified"])
        self.assertEqual("INCONCLUSIVE", result["result"])
        self.assertEqual("OBSERVATION_SESSION_MISMATCH", result["reason"])

    def test_post_action_rejects_mixed_coordinate_spaces(self):
        now = time.time()
        before = ScreenState("AX", None, None, ({"id": "save"},), (), 1.0, (),
                             observation_id="before", observed_at=now, session_id="macos-ax-1",
                             coordinate_space="ax_global_screen_coordinates")
        after = ScreenState("IMAGE", 1920, 1080, ({"id": "save"},), (), 1.0, (),
                            observation_id="after", observed_at=now + 0.01, session_id="macos-ax-1",
                            coordinate_space="screen_capture_pixels")
        result = SemanticActionRuntime.verify_post_action(
            before, after, "save", expected_element_states=(("save", "enabled", True),))
        self.assertFalse(result["verified"])
        self.assertEqual("INCONCLUSIVE", result["result"])
        self.assertEqual("OBSERVATION_COORDINATE_SPACE_MISMATCH", result["reason"])

    def test_post_action_confirms_new_observation_and_effect(self):
        now = time.time()
        before = ScreenState("DOM", 100, 100, ({"id": "details", "expanded": False},), (), 1.0, (),
                             observation_id="before", observed_at=now, session_id="webview-1")
        after = ScreenState("DOM", 100, 100, ({"id": "details", "expanded": True},), (), 1.0, (),
                            observation_id="after", observed_at=now + 0.01, session_id="webview-1")
        result = SemanticActionRuntime.verify_post_action(
            before, after, "details", expected_element_states=(("details", "expanded", True),))
        self.assertTrue(result["verified"])
        self.assertEqual("CONFIRMED", result["result"])
