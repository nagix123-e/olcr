import unittest
import math

from olcr_api.vision import DOMAXObservationProvider, VisionRouter
from olcr_api.model_registry import ModelRegistry


class VisionRouterTests(unittest.TestCase):
    def test_ax_has_priority_over_dom_and_image(self):
        state = DOMAXObservationProvider().observe(
            dom={"elements": [{"id": "dom"}], "confidence": 0.9},
            ax={"elements": [{"id": "ax"}], "confidence": 1.0,
                "coordinateSpace": "ax_global_screen_coordinates"},
            image={"elements": [{"id": "image"}], "confidence": 0.2},
        )
        self.assertEqual("AX", state.source)
        self.assertEqual("ax", state.elements[0]["id"])
        self.assertEqual("ax_global_screen_coordinates", state.coordinate_space)
        self.assertEqual("ax_global_screen_coordinates", state.public_dict()["coordinate_space"])
        self.assertEqual("role:gui_grounding", VisionRouter().select_role("inspect this" , screen_state=state))

    def test_low_confidence_visual_routes_to_qwen_fallback(self):
        router = VisionRouter()
        state = router.screen_state_from_visual({
            "elements": [{"id": "button", "confidence": 0.4}],
            "confidence": 0.4,
            "uncertainty": ["label ambiguous"],
        })
        self.assertLess(state.confidence, 0.65)
        self.assertTrue(router.fallback_required(state))
        self.assertEqual("AMBIGUOUS_TARGET", router.escalation_reason(state, "Click the save button"))

    def test_escalation_reason_names_missing_region_and_insufficient_text(self):
        router = VisionRouter()
        self.assertEqual("UNKNOWN_REGION", router.escalation_reason(None))
        empty = router.screen_state_from_visual({"elements": [], "text": [], "confidence": 0.9})
        self.assertEqual("OCR_INSUFFICIENT", router.escalation_reason(empty, "Read the text in this image"))
        self.assertIsNone(router.escalation_reason(router.screen_state_from_visual({
            "elements": [{"id": "save", "label": "Save", "confidence": 0.95}],
            "text": ["Save"], "confidence": 0.95, "uncertainty": [],
        })))

    def test_general_image_selects_fastvlm_role(self):
        self.assertEqual("role:general_image", VisionRouter().select_role("describe this photo"))
        self.assertEqual("role:general_image", VisionRouter().select_role("describe this fruit"))

    def test_gui_action_selects_manifest_bound_ui_venus_role(self):
        self.assertEqual("role:gui_grounding", VisionRouter().select_role("Click the Save button on screen"))
        binding = ModelRegistry().binding("gui_grounding")
        self.assertEqual("ui_venus_mlx_6bit", binding.model_id)

    def test_common_screenshot_and_dialog_requests_select_ui_venus(self):
        router = VisionRouter()
        for request in (
            "Find the close control in this screenshot",
            "Find the dialogs in these screenshots",
            "Read the warning in the dialog",
            "このスクリーンショットのチェックボックスを探して",
        ):
            with self.subTest(request=request):
                self.assertEqual("role:gui_grounding", router.select_role(request))

    def test_dom_observation_preserves_freshness_and_session_provenance(self):
        state = DOMAXObservationProvider().observe(dom={
            "elements": [{"id": "save"}], "observed_at": 1234.5,
            "observation_id": "obs-1", "session_id": "window-1",
        })
        self.assertEqual("obs-1", state.observation_id)
        self.assertEqual(1234.5, state.observed_at)
        self.assertEqual("window-1", state.session_id)

    def test_native_ax_camel_case_provenance_survives_even_with_an_empty_tree(self):
        state = DOMAXObservationProvider().observe(ax={
            "source": "AX", "elements": [], "observationId": "ax-123-456",
            "observedAt": 1234.5, "sessionId": "macos-ax-123",
            "coordinateSpace": "ax_global_screen_coordinates", "uncertainty": ["tree empty"],
        })
        self.assertEqual("AX", state.source)
        self.assertEqual("ax-123-456", state.observation_id)
        self.assertEqual(1234.5, state.observed_at)
        self.assertEqual("macos-ax-123", state.session_id)
        self.assertEqual("ax_global_screen_coordinates", state.coordinate_space)
        self.assertIn("tree empty", state.uncertainty)

    def test_nonfinite_dom_confidence_cannot_become_high_trust(self):
        for confidence in (math.nan, math.inf, -math.inf, "not-a-number", None):
            with self.subTest(confidence=confidence):
                state = DOMAXObservationProvider().observe(dom={
                    "elements": [{"id": "save"}], "confidence": confidence,
                    "observation_id": "dom-observation", "session_id": "dom-session",
                })
                self.assertEqual(0.0, state.confidence)
                self.assertIn("INVALID_CONFIDENCE", state.uncertainty)
                self.assertTrue(VisionRouter().fallback_required(state))

    def test_malformed_visual_confidence_and_collections_fail_closed(self):
        state = VisionRouter.screen_state_from_visual({
            "elements": None,
            "text": None,
            "confidence": math.nan,
            "uncertainty": "malformed",
        })
        self.assertEqual(0.0, state.confidence)
        self.assertEqual((), state.elements)
        self.assertEqual((), state.text)
        self.assertIn("INVALID_CONFIDENCE", state.uncertainty)
        self.assertIn("INVALID_ELEMENTS", state.uncertainty)
        self.assertTrue(VisionRouter().fallback_required(state))

    def test_invalid_element_confidence_is_not_replaced_by_default_high_confidence(self):
        state = VisionRouter.screen_state_from_visual({
            "elements": [{"id": "save", "confidence": math.nan}],
        })
        self.assertEqual(0.0, state.confidence)
        self.assertIn("INVALID_CONFIDENCE", state.uncertainty)
        self.assertTrue(VisionRouter().fallback_required(state))
