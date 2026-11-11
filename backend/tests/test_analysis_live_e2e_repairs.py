"""Regressions for real Brain timeouts and misleading analysis completion records."""
import unittest
from unittest.mock import patch

import test_autonomous_api_analysis as support
from olcr_api import app, external_tools
from olcr_api.analysis_coverage import missing_coverage
from olcr_api.models import Task, TaskState
from olcr_api.ollama import ModelFailure, model_timeout_seconds, inference_timeout


class AnalysisLiveRepairTests(unittest.TestCase):
    setUp = support.AutonomousApiAnalysisTests.setUp
    tearDown = support.AutonomousApiAnalysisTests.tearDown

    @staticmethod
    def evidence():
        return [{"tool_id": "politics.ca_openparliament", "provider": "Parliament",
                 "data": {"items": [{"title": "Verified debate", "text": "A debate record."}]},
                 "sources": [{"title": "Parliament", "canonical_url": "https://example.test/debate"}]}]

    def test_synthesis_uses_provider_budget_and_preserves_an_outer_deadline(self):
        budgets = []
        def generate(*args, **kwargs):
            budgets.append(model_timeout_seconds(750))
            return {"text": "審議記録を取得しました [S1]。"}
        with patch.object(app.runtime, "_generate_brain", side_effect=generate):
            app._compose_autonomous_analysis("審議を整理して", self.evidence(), {})
            with inference_timeout(120):
                app._compose_autonomous_analysis("審議を整理して", self.evidence(), {})
        self.assertEqual(750, budgets[0])
        self.assertGreater(budgets[1], 100)
        self.assertLessEqual(budgets[1], 120)

    def test_failed_synthesis_keeps_model_identity_and_safe_failure_category(self):
        recipe = {}
        with patch.object(app.runtime, "_generate_brain", side_effect=ModelFailure("timeout", "private detail")), \
             patch.object(app.brain_models, "selected_model_for_role", return_value="configured-local-model"):
            response, _, accepted = app._compose_autonomous_analysis("審議を整理して", self.evidence(), recipe)
        call = recipe["_model_calls"][0]
        self.assertFalse(accepted)
        self.assertTrue(response)
        self.assertEqual("generation_failed", call["status"])
        self.assertEqual("configured-local-model", call["model"])
        self.assertEqual("UNKNOWN", call["actual_model"])
        self.assertEqual("timeout", call["error"])
        self.assertNotIn("private detail", str(call))

    def test_partial_result_is_one_execution_with_retained_evidence(self):
        task = Task("compare")
        result = {**self.evidence()[0], "data": {"items": [{"title": "Sample"}], "request_complete": False}}
        app._record_analysis_attempts(task, [result], [
            {"tool_id": result["tool_id"], "error": "PROVIDER_PARTIAL_COVERAGE"},
            {"tool_id": "econ.worldbank", "error": "PROVIDER_TIMEOUT"}])
        self.assertEqual(2, len(task.tool_executions))
        self.assertEqual(["partial", "failed"], [row["status"] for row in task.tool_executions])
        self.assertEqual(1, task.tool_executions[0]["output"]["item_count"])

    def test_same_provider_complete_and_partial_calls_remain_distinct(self):
        task = Task("compare places")
        complete = {**self.evidence()[0], "data": {"items": [{"title": "Complete"}], "request_complete": True}}
        partial = {**complete, "data": {"items": [{"title": "Partial"}], "request_complete": False}}
        app._record_analysis_attempts(task, [complete, partial], [
            {"tool_id": partial["tool_id"], "error": "PROVIDER_PARTIAL_COVERAGE"}])
        self.assertEqual(["success", "partial"], [row["status"] for row in task.tool_executions])

    def test_discarding_unrequested_records_is_not_a_failed_provider_call(self):
        evidence = {"tool_id": "econ.worldbank", "provider": "World Bank",
                    "data": {"items": [
                        {"country": "Japan", "date": "2024", "indicator": "NY.GDP.MKTP.KD.ZG", "value": 1},
                        {"country": "France", "date": "2024", "indicator": "NY.GDP.MKTP.KD.ZG", "value": 2}]},
                    "sources": [{"canonical_url": "https://data.worldbank.org/"}]}
        with patch.object(app, "_laya_select_analysis_apis", return_value=["econ.worldbank"]), \
             patch.object(app, "_brain_plan_analysis_apis", return_value=(["econ.worldbank"], support._recipe(["econ.worldbank"]))), \
             patch.object(app, "external_tool_credential_setup", return_value=None), \
             patch.object(app, "execute_external_tool", return_value=evidence), \
             patch.object(app, "_recover_analysis_evidence", side_effect=AssertionError("coverage is complete")):
            outcome = app._run_autonomous_analysis("2024年の日本の成長を比較して", {})
        self.assertEqual([], outcome["tool"]["failures"])
        self.assertEqual("complete", outcome["tool"]["analysis_status"])
        self.assertEqual(["success"], [row["status"] for row in outcome["task"].tool_executions])

    def test_model_only_recovery_is_explicitly_unverified(self):
        def completed_task():
            task = Task("観光の推移を比較して")
            task.transition(TaskState.ROUTING)
            task.transition(TaskState.GENERATING)
            task.transition(TaskState.COMPLETED)
            return task
        with patch.object(app, "_laya_select_analysis_apis", return_value=["aviation.opensky"]), \
             patch.object(app, "_brain_plan_analysis_apis", return_value=(["aviation.opensky"], support._recipe(["aviation.opensky"]))), \
             patch.object(app, "external_tool_credential_setup", return_value=None), \
             patch.object(app, "compile_provider_arguments", return_value={"unsupported_capability": True}), \
             patch.object(app, "_recover_analysis_evidence", return_value=(completed_task(), "")), \
             patch.object(app.runtime, "infer_without_sources", return_value=(completed_task(), "未検証の説明")):
            outcome = app._run_autonomous_analysis("観光の推移を比較して", {})
        self.assertEqual("unverified", outcome["tool"]["analysis_status"])
        self.assertTrue(any(row.get("analysis_status") == "unverified" for row in outcome["task"].selected_context))

    def test_typed_numeric_providers_share_arithmetic_without_optional_generation(self):
        evidence = [
            {"tool_id": "trade.un_comtrade", "provider": "UN Comtrade", "data": {"items": [
                {"year": 2022, "trade_value_usd": 100, "reporter": "Japan", "partner": "World", "flow": "Export", "commodity": "TOTAL"},
                {"year": 2024, "trade_value_usd": 120, "reporter": "Japan", "partner": "World", "flow": "Export", "commodity": "TOTAL"}]},
             "sources": [{"canonical_url": "https://comtradeapi.un.org/"}]},
            {"tool_id": "currency.frankfurter", "provider": "Frankfurter", "data": {"items": [
                {"date": "2024-01-01", "value": 0.006, "indicator": "JPY/USD", "unit": "USD/JPY"}]},
             "sources": [{"canonical_url": "https://frankfurter.dev/"}]},
        ]
        with patch.object(app.runtime, "_generate_brain", side_effect=AssertionError("validated numeric tables use arithmetic")):
            response, sources, generated = app._compose_autonomous_analysis("輸出額と為替の変化を比較して", evidence, {})
        self.assertFalse(generated)
        self.assertEqual(2, len(sources))
        self.assertIn("120 US$", response)
        self.assertIn("0.006USD/JPY", response)

    def test_nonnumeric_values_are_not_misclassified_as_numeric_tables(self):
        from olcr_api.analysis_summary import has_numeric_observations
        for value in ("unavailable", None, True, float("inf")):
            self.assertFalse(has_numeric_observations({"data": {"items": [{"date": "2024", "value": value}]}}))


class AnalysisCoverageRepairTests(unittest.TestCase):
    def test_incomplete_country_comparison_reports_partial_not_absent(self):
        result = {"tool_id": "econ.worldbank", "data": {"items": [
            {"indicator": "FP.CPI.TOTL.ZG", "country": "Japan", "date": "2024", "value": 2.5}]}}
        missing = missing_coverage("2024年の日本と韓国の成長と物価を比較して",
            {"statistics.world_bank"}, lambda tool: external_tools.CANONICAL_ALIASES.get(tool, tool), [result])
        by_key = {row["key"]: row for row in missing}
        self.assertEqual("partial", by_key["inflation"]["coverage_status"])
        self.assertEqual("missing", by_key["growth"]["coverage_status"])

    def test_sample_keeps_obligations_incomplete_without_erasing_observations(self):
        result = {"tool_id": "politics.ca_openparliament", "data": {
            "items": [{"text": "Debate excerpt"}], "request_complete": False}}
        missing = missing_coverage("カナダ議会の審議を比較して", {"politics.ca_openparliament"}, lambda tool: tool, [result])
        self.assertEqual("partial", missing[0]["coverage_status"])


class LiveDriverOutcomeTests(unittest.TestCase):
    def test_http_completion_never_promotes_failed_synthesis_to_semantic_pass(self):
        from scripts.analysis_chat_live_e2e import case_outcome
        task = {"state": "completed", "selected_context": [{"analysis_status": "partial", "synthesis_status": "generation_failed"}],
                "model_calls": [{"status": "generation_failed", "error": "timeout"}]}
        outcome = case_outcome(task, "done", 200, "fallback evidence")
        self.assertTrue(outcome["pipeline_completed"])
        self.assertEqual(1, outcome["model_failure_count"])
        self.assertEqual("partial", outcome["analysis_status"])
        self.assertIsNone(outcome["semantic_pass"])


if __name__ == "__main__":
    unittest.main()
