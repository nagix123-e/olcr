from __future__ import annotations

import unittest

from olcr_api.model_telemetry import ModelTelemetry, RECENT_CALL_LIMIT


class ModelTelemetryTests(unittest.TestCase):
    def test_recent_invocations_are_bounded_while_aggregate_counts_continue(self):
        telemetry = ModelTelemetry()
        total = RECENT_CALL_LIMIT + 7

        for index in range(total):
            telemetry.record(
                task_id=f"task-{index}",
                role="gui_grounding",
                model="ui_venus_mlx_6bit",
            )

        snapshot = telemetry.snapshot()
        self.assertEqual(total, snapshot["counters"]["model_invocations_total"])
        self.assertEqual(RECENT_CALL_LIMIT, len(snapshot["calls"]))
        self.assertEqual("task-7", snapshot["calls"][0]["task_id"])
        self.assertEqual(f"task-{total - 1}", snapshot["calls"][-1]["task_id"])

    def test_reset_clears_recent_invocations_and_aggregate_counts(self):
        telemetry = ModelTelemetry()
        telemetry.record(role="gui_grounding", model="ui_venus_mlx_6bit")

        telemetry.reset()

        snapshot = telemetry.snapshot()
        self.assertTrue(all(value == 0 for value in snapshot["counters"].values()))
        self.assertEqual([], snapshot["calls"])

    def test_action_router_decisions_are_aggregated_with_bounded_mechanism_labels(self):
        telemetry = ModelTelemetry()
        telemetry.record_action_router_decision({"status": "PASS", "mechanism": "playwright"})
        telemetry.record_action_router_decision({"status": "BLOCKED", "mechanism": "caller-controlled"})

        counters = telemetry.snapshot()["counters"]
        self.assertEqual(2, counters["action_router_decisions"])
        self.assertEqual(1, counters["action_router_playwright_decisions"])
        self.assertEqual(1, counters["action_router_blocked_decisions"])
        self.assertNotIn("action_router_caller-controlled_decisions", counters)

    def test_action_verification_uses_only_authoritative_tri_state_outcomes(self):
        telemetry = ModelTelemetry()
        telemetry.record_action_verification({"result": "CONFIRMED", "verified": True})
        telemetry.record_action_verification({"result": "NOT_CONFIRMED", "verified": False})
        telemetry.record_action_verification({"result": "INCONCLUSIVE", "verified": False})
        telemetry.record_action_verification({"verified": True})

        counters = telemetry.snapshot()["counters"]
        self.assertEqual(4, counters["action_verification_attempts"])
        self.assertEqual(1, counters["actions_verified"])
        self.assertEqual(1, counters["actions_not_confirmed"])
        self.assertEqual(2, counters["verification_inconclusive"])

    def test_native_action_execution_counters_use_a_closed_mechanism_set(self):
        telemetry = ModelTelemetry()
        telemetry.record_action_execution("accessibility")
        telemetry.record_action_execution("keyboard_shortcut", routine=True)
        telemetry.record_action_execution("vision_pointer")
        telemetry.record_action_execution("playwright")
        telemetry.record_action_execution("caller-controlled")

        counters = telemetry.snapshot()["counters"]
        self.assertEqual(3, counters["native_actions"])
        self.assertEqual(1, counters["accessibility_actions"])
        self.assertEqual(1, counters["shortcut_actions"])
        self.assertEqual(1, counters["routine_actions"])
        self.assertEqual(1, counters["pointer_actions"])
        self.assertEqual(1, counters["vision_grounded_actions"])
        self.assertNotIn("action_caller-controlled_executions", counters)

    def test_playwright_action_counts_are_bounded_by_operation_and_separate_from_native(self):
        telemetry = ModelTelemetry()
        telemetry.record_playwright_action("navigate", successful=True)
        telemetry.record_playwright_action("navigate_back", successful=False)
        telemetry.record_playwright_action("wait_for_text", successful=True)
        telemetry.record_playwright_action("assert_text", successful=False)
        telemetry.record_playwright_action("caller-controlled", successful=True)
        telemetry.record_action_execution("playwright")

        counters = telemetry.snapshot()["counters"]
        self.assertEqual(2, counters["playwright_actions"])
        self.assertEqual(2, counters["playwright_action_failures"])
        self.assertEqual(4, counters["playwright_action_attempts"])
        self.assertEqual(1, counters["playwright_navigate_successes"])
        self.assertEqual(1, counters["playwright_navigate_back_failures"])
        self.assertEqual(1, counters["playwright_wait_for_text_successes"])
        self.assertEqual(1, counters["playwright_assert_text_failures"])
        self.assertNotIn("playwright_caller-controlled_attempts", counters)
        self.assertEqual(0, counters.get("native_actions", 0))

    def test_routine_counters_separate_compilation_matching_fallback_and_verified_outcomes(self):
        telemetry = ModelTelemetry()
        telemetry.record_routine_compilation()
        telemetry.record_routine_match(1)
        telemetry.record_routine_match(0)
        telemetry.record_routine_match(2)
        telemetry.record_routine_outcome(success=True)
        telemetry.record_routine_outcome(success=False)

        counters = telemetry.snapshot()["counters"]
        self.assertEqual(1, counters["routine_candidates"])
        self.assertEqual(1, counters["routines_compiled"])
        self.assertEqual(3, counters["routine_match_attempts"])
        self.assertEqual(1, counters["routine_matches"])
        self.assertEqual(2, counters["routine_fallbacks"])
        self.assertEqual(2, counters["routine_runs"])
        self.assertEqual(1, counters["routine_successes"])
        self.assertEqual(1, counters["routine_failures"])

    def test_vision_and_ocr_cost_aggregates_are_bounded_and_keep_reason_labels_closed(self):
        telemetry = ModelTelemetry()
        telemetry.record_vision_call(reason="GUI_GROUNDING", scope="roi", pixel_count=1200,
                                     duration_ms=12.5)
        telemetry.record_vision_call(reason="caller-controlled", scope="other", pixel_count=10**9,
                                     duration_ms=float("inf"), status="error")
        telemetry.record_ocr_call(4.25)
        telemetry.record_ocr_call(-100)
        telemetry.record_ocr_call(7.5, status="error")

        snapshot = telemetry.snapshot()
        counters = snapshot["counters"]
        self.assertEqual(2, counters["vision_calls"])
        self.assertEqual(1, counters["vision_roi_calls"])
        self.assertEqual(1, counters["vision_call_errors"])
        self.assertEqual(50_001_200, counters["vision_pixels_total"])
        self.assertEqual(12.5, counters["vision_inference_ms"])
        self.assertEqual(1, snapshot["vision_calls_by_reason"]["GUI_GROUNDING"])
        self.assertEqual(1, snapshot["vision_calls_by_reason"]["OTHER"])
        self.assertEqual(3, counters["ocr_calls"])
        self.assertEqual(1, counters["ocr_call_errors"])
        self.assertEqual(11.75, counters["ocr_ms"])


if __name__ == "__main__":
    unittest.main()
