"""Semantic counterexamples from live reviews, independent of benchmark wording."""
import json
import unittest
from unittest.mock import patch

import test_autonomous_api_analysis as support
from olcr_api import app
from olcr_api.analysis_semantics import analytical_gaps, evidence_inventory, record_facts


class SemanticContractTests(unittest.TestCase):
    setUp = support.AutonomousApiAnalysisTests.setUp
    tearDown = support.AutonomousApiAnalysisTests.tearDown

    def evidence(self, rows, tool="test.series", **data):
        return {"tool_id": tool, "provider": tool, "data": {"items": rows, **data},
                "sources": [{"source_id": "S1", "canonical_url": "https://example.test/source"}]}

    def test_subpopulations_are_not_a_single_trend(self):
        rows = [{"year": "2020", "value": 1, "indicator": "r", "dimensions": {"sex": "female"}},
                {"year": "2021", "value": 90, "indicator": "r", "dimensions": {"sex": "male"}}]
        response = app._deterministic_tabular_analysis([self.evidence(rows)], "変化を比較")
        self.assertNotIn("+89", response)
        self.assertIn("female", response)
        self.assertIn("male", response)

    def test_sdg_wire_classifications_survive_normalization_and_arithmetic(self):
        from olcr_api import external_tools as tools
        payload = {"totalPages": 1, "data": [
            {"geoAreaName": "Area", "timePeriodStart": "2000.0", "value": "0.42", "indicator": ["6.4.2"], "series": "ER_H2O_STRESS", "dimensions": {"Activity": "INDUSTRY"}},
            {"geoAreaName": "Area", "timePeriodStart": "2000.0", "value": "54.76", "indicator": ["6.4.2"], "series": "ER_H2O_STRESS", "dimensions": {"Activity": "TOTAL"}},
            {"geoAreaName": "Area", "timePeriodStart": "2001.0", "value": "54.76", "indicator": ["6.4.2"], "series": "ER_H2O_STRESS", "dimensions": {"Activity": "TOTAL"}}]}
        with patch.object(tools, "_request", return_value=payload):
            result = tools.execute("development.un_sdg", {"indicator": "6.4.2", "limit": 5})
        self.assertEqual({"Activity": "INDUSTRY"}, next(row for row in result["data"]["items"] if row["value"] == "0.42")["dimensions"])
        answer = app._deterministic_tabular_analysis([result])
        self.assertNotIn("+54.34", answer)
        self.assertIn("横ばい", answer)

    def test_monthly_and_annual_average_periods_do_not_form_one_trend(self):
        rows = [{"year": "2020", "period": "M13", "indicator": "x", "value": 90},
                {"year": "2021", "period": "M01", "indicator": "x", "value": 1}]
        answer = app._deterministic_tabular_analysis([self.evidence(rows)])
        self.assertNotIn("−89", answer)
        self.assertIn("annual_average", answer)
        self.assertIn("monthly", answer)

    def test_shared_measure_head_preserves_both_requested_dimensions(self):
        from olcr_api.analysis_coverage import evidence_needs
        from olcr_api import external_tools as tools
        request = "各国の経済・人口指標を比較して"
        self.assertTrue({"macro", "population"}.issubset({need["key"] for need in evidence_needs(request)}))
        args = tools.compile_provider_arguments("econ.worldbank", request)
        self.assertTrue({"NY.GDP.PCAP.CD", "SP.POP.TOTL"}.issubset(set(args["indicators"])))

    def test_country_identity_is_not_an_extra_measure_dimension(self):
        from olcr_api import external_tools as tools
        payload = {"maxPages": 1, "items": [
            {"year": 2024, "refugees": 100, "coa_name": "France", "coa_iso": "FRA", "coo_iso": "-"},
            {"year": 2024, "refugees": 200, "coa_name": "Germany", "coa_iso": "DEU", "coo_iso": "-"}]}
        with patch.object(tools, "_request", return_value=payload):
            result = tools.execute("humanitarian.unhcr", {"country_dimension": "asylum", "country_codes": ["FRA", "DEU"], "year_from": 2024, "year_to": 2024, "limit": 20})
        self.assertNotIn("coa_iso", result["data"]["items"][0]["dimensions"])
        answer = app._deterministic_tabular_analysis([result])
        self.assertIn("共通年の国別比較", answer)
        self.assertIn("難民数", answer)

    def test_observation_names_do_not_use_country_substring_matches(self):
        from olcr_api.analysis_scope import observation_country
        self.assertEqual(set(), observation_country("St. Martin (French part)"))
        self.assertEqual(set(), observation_country("Virgin Islands (U.S.)"))
        self.assertEqual({"SS"}, observation_country("South Sudan"))
        self.assertEqual({"SD"}, observation_country("Sudan"))

    def test_geographic_join_cannot_replace_a_country_with_a_territory(self):
        evidence = [self.evidence([
            {"country": "France", "countryiso3code": "FRA", "year": "2024", "value": 70000000, "indicator": "population"},
            {"country": "St. Martin (French part)", "countryiso3code": "MAF", "year": "2024", "value": 24941, "indicator": "population"},
            {"country": "United States", "countryiso3code": "USA", "year": "2024", "value": 335000000, "indicator": "population"},
            {"country": "Virgin Islands (U.S.)", "countryiso3code": "VIR", "year": "2024", "value": 103792, "indicator": "population"}]),
            self.evidence([
            {"country": "France", "country_code": "FRA", "year": "2024", "value": 3000, "indicator": "refugees"},
            {"country": "United States of America", "country_code": "USA", "year": "2024", "value": 5000, "indicator": "refugees"}], "other")]
        table = app._deterministic_tabular_analysis(evidence).split("比較可能な対象", 1)[0]
        self.assertIn("| France | 70000000 | 3000 |", table)
        self.assertIn("| United States | 335000000 | 5000 |", table)
        self.assertNotIn("24941", table)
        self.assertNotIn("103792", table)

    def test_conflicting_same_period_cannot_choose_an_arbitrary_endpoint(self):
        rows = [{"year": "2020", "value": 1, "indicator": "r"},
                {"year": "2020", "value": 30, "indicator": "r"},
                {"year": "2021", "value": 50, "indicator": "r"}]
        response = app._deterministic_tabular_analysis([self.evidence(rows)], "推移を比較")
        self.assertIn("異なる値", response)
        self.assertNotIn("+49", response)

    def test_metric_sampling_does_not_starve_a_requested_second_measure(self):
        rows = [{"country": f"Area {i}", "year": "2020", "value": i, "indicator": metric}
                for metric in ("renewable", "emissions") for i in range(30)]
        response = app._deterministic_tabular_analysis([self.evidence(rows)])
        self.assertIn("のemissions", response.split("共通年の", 1)[0])

    def test_named_comparison_axes_survive_a_large_regional_sample(self):
        rows = [{"country": f"Area {i:02}", "year": "2024", "value": i, "indicator": "x"} for i in range(30)]
        rows += [{"country": "Japan", "year": "2024", "value": 70, "indicator": "x"},
                 {"country": "United States", "year": "2024", "value": 80, "indicator": "x"}]
        answer = app._deterministic_tabular_analysis([self.evidence(rows)], "日米欧で比較して")
        self.assertIn("観測は日本", answer.split("地域別の詳細", 1)[0])
        self.assertIn("観測は米国", answer.split("地域別の詳細", 1)[0])
        self.assertIn("日本 70", answer.split("共通年の国別比較", 1)[1])
        self.assertIn("米国 80", answer.split("共通年の国別比較", 1)[1])

    def test_comparison_joins_actual_common_period_not_each_latest(self):
        rows = [{"country": "Japan", "year": str(year), "value": value, "indicator": metric}
                for metric, year, value in (("a", 2023, 1), ("a", 2024, 2), ("b", 2023, 3))]
        response = app._deterministic_tabular_analysis([self.evidence(rows)])
        self.assertIn("同一期間で揃えた比較（2023", response)
        self.assertIn("| Japan | 1 | 3 |", response)

    def test_fiscal_and_calendar_years_are_not_the_same_observation_period(self):
        evidence = [self.evidence([{"country": "Japan", "year": "2024", "value": 1, "indicator": "a"}]),
                    self.evidence([{"country": "Japan", "year": "2024", "value": 2, "indicator": "b"}], "other", fiscal_year_basis=True)]
        answer = app._deterministic_tabular_analysis(evidence)
        self.assertIn("直接比較は未完了", answer)
        self.assertNotIn("同一期間で揃えた比較（2024", answer)

    def test_header_tables_keep_index_and_observation_time(self):
        evidence = self.evidence([["time_tag", "Kp"], ["2026-01-01 00:00", "2"],
                                  ["2026-01-01 03:00", "3"]], "space.space_weather")
        response, _, _ = app._compose_autonomous_analysis("観測値の変化", [evidence])
        self.assertIn("地磁気Kp指数", response)
        self.assertIn("2026-01-01 03:00", response)
        self.assertIn("+1 Kp", response)

    def test_model_cannot_publish_new_facts_even_with_valid_source_ids(self):
        evidence = [self.evidence([{"title": "Actual record", "text": "Actual excerpt"}])]
        recipe = {}
        with patch.object(app.runtime, "_generate_brain", return_value={"text": "There are only 3 countries [S1]"}):
            response, _, accepted = app._compose_autonomous_analysis("記録を照合", evidence, recipe)
        self.assertFalse(accepted)
        self.assertNotIn("only 3 countries", response)
        self.assertIn("Actual excerpt", response)
        self.assertFalse(recipe["_semantic_review"]["free_form_claims_published"])

    def test_equal_provider_labels_do_not_hide_a_distinct_evidence_packet(self):
        evidence = [self.evidence([{"title": "Source one"}]), self.evidence([{"title": "Source two"}])]
        with patch.object(app.runtime, "_generate_brain", return_value={"text": json.dumps({"fact_ids": ["E1R1"]})}):
            response, _, _ = app._compose_autonomous_analysis("資料を比較", evidence, {})
        body = response.split("## 分析", 1)[1]
        self.assertIn("Source one", body)
        self.assertIn("Source two", body)

    def test_valid_brain_selection_preserves_full_inventory_not_sample_count(self):
        evidence = [self.evidence([{"title": f"Record {i}", "country": f"Area {i}"} for i in range(27)])]
        with patch.object(app.runtime, "_generate_brain", return_value={"text": json.dumps({"fact_ids": ["E1R1"]})}):
            response, _, accepted = app._compose_autonomous_analysis("記録を整理", evidence, {})
        self.assertTrue(accepted)
        self.assertIn("取得記録27件", response)
        self.assertIn("対象国・地域27", response)

    def test_brain_can_choose_a_relevant_record_beyond_the_first_three(self):
        evidence = [self.evidence([{"title": f"Record {i}"} for i in range(8)])]
        with patch.object(app.runtime, "_generate_brain", return_value={"text": json.dumps({"fact_ids": ["E1R7"]})}) as model:
            response, _, accepted = app._compose_autonomous_analysis("記録を整理", evidence, {})
        self.assertTrue(accepted)
        self.assertIn("E1R7", model.call_args.kwargs["structured_schema"]["properties"]["fact_ids"]["items"]["enum"])
        self.assertIn("Record 6", response.split("## 分析", 1)[1])

    def test_proxy_provider_observations_are_partial_not_absent(self):
        from olcr_api.analysis_coverage import missing_coverage
        from olcr_api.external_tools import CANONICAL_ALIASES
        result = self.evidence([{"indicator": "EN.ATM.PM25.MC.M3", "country": "France", "date": "2024", "value": 7}], "econ.worldbank")
        gaps = missing_coverage("欧州の大気汚染の推移を比較", {"statistics.world_bank"}, lambda tool: CANONICAL_ALIASES.get(tool, tool), [result])
        self.assertEqual("partial", next(item for item in gaps if item["key"] == "air")["coverage_status"])

    def test_unknown_event_records_and_sanctions_attributes_are_not_omitted(self):
        evidence = [self.evidence([{"place": "Observed place", "mag": 6, "time": 12345}]),
                    self.evidence([{"name": "Observed person", "programs": ["P"], "aliases": ["A"]}], "list")]
        facts = record_facts(evidence)
        self.assertIn("mag: 6", facts[0]["text"])
        self.assertIn("programs:", facts[1]["text"])
        self.assertIn("aliases:", facts[1]["text"])

    def test_aggregate_trade_cannot_fulfill_composition(self):
        gaps = analytical_gaps("貿易の構成を比較", [self.evidence([{"commodity": "TOTAL", "year": 2024}], "trade.un_comtrade")])
        self.assertEqual("composition", gaps[0]["key"])

    def test_source_free_analysis_cannot_publish_remembered_current_trends(self):
        with patch.object(app.runtime, "_generate_brain", return_value={"text": "Current employment increased 35%"}):
            task, response = app.runtime.infer_without_sources("地域別の雇用を比較", failure_reason="NO_EVIDENCE",
                evidence_requirements=[{"key": "employment", "label": "雇用"}])
        self.assertEqual("completed", task.state.value)
        self.assertIn("分析は未完了", response)
        self.assertNotIn("35%", response)
        self.assertNotIn("increased", response)


if __name__ == "__main__":
    unittest.main()
