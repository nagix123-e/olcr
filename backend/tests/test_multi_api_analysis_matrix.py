"""Curated deterministic, distinct multi-provider analysis tasks.

All outbound providers are stubbed. These cases exercise the complete API
planning/execution/evidence handoff for different combinations without live
network access or provider credentials.
"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_autonomous_api_analysis as _analysis_test_support
from olcr_api import external_tools
import olcr_api.app as app
from olcr_api.models import Task, TaskState


FIXTURE = Path(__file__).parent / "fixtures" / "multi_api_analysis_cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


class MultiApiAnalysisMatrixTests(unittest.TestCase):
    setUp = _analysis_test_support.AutonomousApiAnalysisTests.setUp
    tearDown = _analysis_test_support.AutonomousApiAnalysisTests.tearDown

    def test_curated_distinct_tasks_execute_each_planned_api_and_aggregate_evidence(self):
        self.assertEqual(32, len(CASES))
        combinations = [tuple(sorted(case["tool_ids"])) for case in CASES]
        self.assertEqual(len(CASES), len(set(combinations)), "each task must use a distinct API combination")

        for case in CASES:
            with self.subTest(case=case["id"]):
                tool_ids = case["tool_ids"]
                self.assertGreaterEqual(len(tool_ids), 2)
                self.assertTrue(all(tool_id in external_tools.RUNTIME_REGISTRY
                                    and external_tools.RUNTIME_REGISTRY[tool_id].implemented
                                    for tool_id in tool_ids))
                provider_results = {
                    tool_id: {
                        "tool_id": tool_id,
                        "provider": external_tools.RUNTIME_REGISTRY[tool_id].provider,
                        "fetched_at": "2026-10-20T00:00:00Z",
                        "data": {"items": [{"case_id": case["id"], "value": index + 1}]},
                        "sources": [{"title": tool_id,
                                     "canonical_url": f"https://fixture.invalid/{tool_id}"}],
                    }
                    for index, tool_id in enumerate(tool_ids)
                }
                recipe = _analysis_test_support._recipe(tool_ids, request=case["prompt"])
                recovery = Task(case["prompt"])
                recovery.transition(TaskState.ROUTING)
                recovery.transition(TaskState.GENERATING)
                recovery.transition(TaskState.COMPLETED)

                with patch.object(app, "_laya_select_analysis_apis", return_value=tool_ids), \
                     patch("olcr_api.analysis_recovery.recovery_requests", return_value=[]), \
                     patch.object(app, "_brain_plan_analysis_apis",
                                  return_value=(tool_ids, recipe)), \
                     patch.object(app, "external_tool_credential_setup", return_value=None), \
                     patch.object(app, "compile_provider_arguments",
                                  side_effect=lambda tool, prompt, seed: {"query": prompt}) as compile_args, \
                     patch.object(app, "execute_external_tool",
                                  side_effect=lambda tool, _args: provider_results[tool]) as execute, \
                     patch.object(app.runtime, "execute", return_value=(recovery, "Fixture has no typed indicator coverage")), \
                     patch.object(app, "_compose_autonomous_analysis",
                                  side_effect=lambda prompt, evidence, plan: (
                                      f"Combined {len(evidence)} provider records for {case['id']}",
                                      [source for result in evidence for source in result["sources"]], True,
                                  )) as compose, \
                     patch.object(app, "external_tool_display_blocks", return_value=[]):
                    outcome = app._run_autonomous_analysis(case["prompt"], {})

                self.assertNotIn("blocked", outcome)
                self.assertEqual(tool_ids, [item.args[0] for item in execute.call_args_list])
                self.assertEqual(tool_ids, [item.args[0] for item in compile_args.call_args_list])
                self.assertEqual(case["prompt"], compile_args.call_args.args[1])
                self.assertEqual(len(tool_ids), len(compose.call_args.args[1]))
                self.assertEqual({case["id"]}, {
                    item["data"]["items"][0]["case_id"] for item in compose.call_args.args[1]
                })
                self.assertEqual(len(tool_ids), len(outcome["sources"]))
                self.assertEqual(len(tool_ids), len(outcome["tool"]["results"]))
                self.assertIn(case["id"], outcome["response"])


if __name__ == "__main__":
    unittest.main()
