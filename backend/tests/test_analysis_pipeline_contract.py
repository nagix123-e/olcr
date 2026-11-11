"""Regression tests from the same-chat GUI run; no live providers or model calls."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_autonomous_api_analysis as support
import olcr_api.app as app
from olcr_api import external_tools as tools
from olcr_api.analysis_scope import explicit_countries, country_scope, filter_scope
from olcr_api.analysis_coverage import missing_coverage, required_sources
from olcr_api.models import Task, TaskState, Route


class AnalysisScopeTests(unittest.TestCase):
    def test_all_curated_analysis_prompts_route_to_analysis_but_single_lookup_does_not(self):
        cases = json.loads((Path(__file__).parent / "fixtures/multi_api_analysis_cases.json").read_text())["cases"]
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertTrue(app._autonomous_analysis_requested(case["prompt"]))
        self.assertFalse(app._autonomous_analysis_requested("Rosneftが現在の制裁リストに掲載されているか、利用可能なAPIで確認して。"))
    def test_region_names_are_not_substring_countries(self):
        self.assertEqual([], explicit_countries("ラテンアメリカのインフレを比較"))
        self.assertEqual(["ID"], explicit_countries("インドネシアの人口"))
        self.assertEqual([], explicit_countries("indicator ID and US$"))
        self.assertEqual({"KR", "JP"}, country_scope("日本と韓国"))
        self.assertEqual(["JP", "DE"], explicit_countries("Japanese and German GDP growth"))

    def test_filters_each_observation_and_nested_request_without_accepting_query_echoes(self):
        data = {"query": "EU inflation", "items": [
            {"country": "Albania", "year": "2024", "value": 1},
            {"country": "France", "year": "2024", "value": 2},
            {"country": "Germany", "year": "1951", "value": 3}]}
        kept, count = filter_scope(data, "EU各国の2024年のインフレ")
        self.assertEqual(2, count)
        self.assertEqual([2], [row["value"] for row in kept["items"]])
        nested, count = filter_scope({"items": [{"data": data}]}, "EU各国の2024年のインフレ")
        self.assertEqual(2, count)
        self.assertEqual([2], [row["value"] for row in nested["items"][0]["data"]["items"]])
        self.assertEqual(3, len(data["items"]), "do not mutate stored evidence")

    def test_since_year_is_a_range_through_present_not_one_exact_year(self):
        data = {"items": [{"date": "2019-01-01", "value": 1}, {"date": "2024-01-01", "value": 2}]}
        kept, rejected = filter_scope(data, "2020年以降の推移を分析")
        self.assertEqual(1, rejected)
        self.assertEqual([2], [row["value"] for row in kept["items"]])
        self.assertTrue(app._analysis_result_matches_scope("2020年以降の推移を分析", kept))

    def test_partial_provider_response_cannot_satisfy_an_evidence_need(self):
        message = "米国の雇用・物価の推移を比較して"
        result = {"tool_id": "econ.us_labor", "data": {
            "items": [{"year": 2026, "value": 4.1}], "request_complete": False,
            "missing_fields": ["inflation"],
        }}
        missing = missing_coverage(
            message, {"econ.us_labor"}, lambda tool: tool, [result],
        )
        self.assertIn("employment", {item["key"] for item in missing})

    def test_no_wrong_default_model_scope_for_typed_adapters(self):
        with self.assertRaises(tools.ExternalToolError):
            tools.compile_provider_arguments("aviation.operational_weather", "紛争地域周辺の民間航空気象")
        self.assertEqual("electricity_generation", tools.compile_provider_arguments("energy.us_eia", "米国の電力構成")["dataset"])
        self.assertTrue(tools.compile_provider_arguments("econ.oecd", "東南アジアの所得成長")["unsupported_capability"])
        self.assertTrue(tools.compile_provider_arguments("development.un_sdg", "人道危機とSDGsを比較")["unsupported_capability"])
        args = tools.compile_provider_arguments("gov.us_spending", "米国の政府契約支出")
        self.assertFalse(args["recipient"])
        self.assertFalse(args["agency"])

    def test_worldbank_preserves_independent_measures_and_regions(self):
        args = tools.compile_provider_arguments("econ.worldbank", "日本と韓国の成長、物価、雇用、人口を比較して")
        self.assertEqual(["JP", "KR"], args["countries"])
        self.assertEqual({"NY.GDP.MKTP.KD.ZG", "FP.CPI.TOTL.ZG", "SL.UEM.TOTL.ZS", "SP.POP.TOTL"}, set(args["indicators"]))
        args = tools.compile_provider_arguments("econ.worldbank", "各国の再生可能電力と温室効果ガス排出")
        self.assertEqual(["all"], args["countries"])
        self.assertIn("EN.GHG.ALL.MT.CE.AR5", args["indicators"])
        self.assertIn("EG.ELC.RNEW.ZS", args["indicators"])
        args = tools.compile_provider_arguments("econ.worldbank", "東南アジア各国のGDP成長率")
        self.assertNotIn("JP", args["countries"])
        self.assertIn("VN", args["countries"])

    def test_unhcr_compiles_documented_filters_instead_of_ignored_free_text(self):
        args = tools.compile_provider_arguments("humanitarian.unhcr", "2021年から2024年のウクライナの避難民を比較")
        self.assertEqual(["UKR"], args["country_codes"])
        self.assertEqual((2021, 2024), (args["year_from"], args["year_to"]))
        with patch.object(tools, "_request", return_value={"items": [
            {"year": 2024, "coo_name": "Ukraine", "refugees": 100, "idps": 20}], "maxPages": 1}) as request:
            result = tools.execute("humanitarian.unhcr", args)
        params = request.call_args.args[2]
        self.assertEqual("UKR", params["coo"])
        self.assertEqual("ISO", params["cf_type"])
        self.assertNotIn("query", params)
        self.assertEqual({"refugees", "idps"}, {row["indicator"] for row in result["data"]["items"]})
        with patch.object(tools, "_request", return_value={"items": [
            {"year": 1951, "coo_name": "Ukraine", "refugees": 100}], "maxPages": 1}):
            stale = tools.execute("humanitarian.unhcr", args)
        self.assertEqual("EMPTY", stale["data"]["semantic_status"])

    def test_bls_wire_format_keeps_units_and_identity(self):
        rows = tools._provider_records({"Results": {"series": [{"seriesID": "LNS14000000", "data": [
            {"year": "2026", "period": "M09", "value": "4.2"}]}]}})
        self.assertEqual("%", rows[0]["unit"])
        self.assertEqual("LNS14000000", rows[0]["series_id"])

    def test_all_curated_requests_compile_real_arguments_or_request_valid_clarification(self):
        cases = json.loads((Path(__file__).parent / "fixtures/multi_api_analysis_cases.json").read_text())["cases"]
        for case in cases:
            for tool in dict.fromkeys([*required_sources(case["prompt"]), *case["tool_ids"]]):
                with self.subTest(case=case["id"], tool=tool):
                    try:
                        arguments = tools.compile_provider_arguments(tool, case["prompt"])
                        self.assertIsInstance(arguments, dict)
                        self.assertTrue(arguments)
                    except tools.ExternalToolError as exc:
                        self.assertIn(exc.category, {"ARGUMENT_COMPILATION_FAILED", "ARGUMENT_SEMANTIC_VALIDATION_FAILED", "INVALID_TOOL_ARGUMENTS"})
        courts = tools.compile_provider_arguments("law.us_courts", "連邦規則制定と議会審議、裁判例の動きを照合して")
        self.assertTrue(courts["federal_only"])
        self.assertTrue(courts["query"].startswith("(court_id:"))

    def test_point_weather_does_not_stand_for_a_continent_or_sibling_metric(self):
        self.assertTrue(tools.compile_provider_arguments("weather.open_meteo", "Europe weather", {"location": "Europe"})["unsupported_capability"])
        self.assertTrue(tools.compile_provider_arguments("environment.air_quality", "Europe air quality", {"location": "Europe"})["unsupported_capability"])
        with self.assertRaises(tools.ExternalToolError):
            tools.compile_provider_arguments("weather.open_meteo", "米国の電力構成と気温を比較", {"location": "米国の電力構成と"})
        self.assertNotIn("unsupported_capability", tools.compile_provider_arguments("weather.open_meteo", "Tokyo weather", {"location": "Tokyo"}))

    def test_imf_keeps_country_indicator_and_actual_observation_period(self):
        args = tools.compile_provider_arguments("econ.imf", "2020年から2024年の韓国の政府債務")
        self.assertEqual(["KOR"], args["countries"])
        self.assertEqual("GGXWDG_NGDP", args["indicator"])
        self.assertTrue(tools.compile_provider_arguments("econ.imf", "金融環境を分析")["unsupported_capability"])
        self.assertEqual([], tools.compile_provider_arguments("econ.imf", "債務を比較")["countries"])
        with patch.object(tools, "_request", return_value={"values": {"GGXWDG_NGDP": {
                "KOR": {"2010": 5, "2024": 45, "2030": 60}, "JPN": {"2024": 200}}}}):
            result = tools.execute("econ.imf", args)
        self.assertEqual([{"country": "KOR", "year": "2024", "value": 45, "indicator": "GGXWDG_NGDP", "unit": "% of GDP"}], result["data"]["items"])


class AnalysisPipelineContractTests(unittest.TestCase):
    setUp = support.AutonomousApiAnalysisTests.setUp
    tearDown = support.AutonomousApiAnalysisTests.tearDown

    def test_curated_requests_keep_required_sources_when_both_models_choose_only_one(self):
        cases = json.loads((Path(__file__).parent / "fixtures/multi_api_analysis_cases.json").read_text())["cases"]
        # Semantic expectations, not the fixture's original forced API triplets
        # (which include e.g. ECB for a US employment request).
        essentials = {
            1: ["econ.worldbank"], 2: ["econ.fred", "econ.us_labor", "econ.worldbank"],
            3: ["trade.un_comtrade", "econ.worldbank"], 4: ["econ.imf", "development.un_sdg"],
            5: ["econ.worldbank"], 6: ["econ.eurostat", "econ.worldbank"], 7: ["trade.un_comtrade", "currency.frankfurter"],
            8: ["econ.worldbank"], 9: ["econ.us_labor", "econ.fred", "gov.us_spending"],
            11: ["econ.fred"], 12: ["econ.jp_estat"], 13: ["econ.worldbank"],
            14: ["energy.us_eia", "environment.air_quality"], 15: ["earth.earthquakes", "humanitarian.hdx"],
            18: ["econ.worldbank"],
            19: ["weather.open_meteo", "environment.air_quality"],
            21: ["development.un_sdg"], 22: ["space.space_weather"], 23: ["earth.earthquakes", "earth.disasters"],
            24: ["econ.worldbank", "trade.un_comtrade"], 25: ["conflict.ucdp", "humanitarian.unhcr", "humanitarian.hdx"],
            26: ["conflict.ucdp", "humanitarian.unhcr", "humanitarian.hdx"],
            28: ["sanctions.unsc", "sanctions.us_ofac", "conflict.ucdp"], 29: ["humanitarian.unhcr", "econ.worldbank"],
            34: ["conflict.ucdp", "humanitarian.unhcr"],
            37: ["politics.us_congress", "policy.us_federal_register", "gov.us_spending"],
            38: ["japan.diet_transcript", "japan.law"], 39: ["politics.eu_parliament", "law.eu_cellar", "econ.worldbank"],
            40: ["politics.uk_parliament", "health.clinical_trials"], 41: ["politics.ca_openparliament", "econ.worldbank"],
            42: ["politics.br_chamber", "politics.br_senate"],
            48: ["knowledge.qiita", "software.github"],
        }
        def generate(_messages, _text, **kwargs):
            schema = kwargs.get("structured_schema")
            if schema and "fact_ids" in schema["properties"]:
                return {"text": json.dumps({"fact_ids": schema["properties"]["fact_ids"]["items"]["enum"][:1]})}
            if schema:
                first = schema["properties"]["sources"]["items"]["properties"]["tool_id"]["enum"][0]
                return {"text": json.dumps(support._recipe([first]))}
            return {"text": "現在のAPI根拠の範囲です [S1]。"}
        def execute(tool, arguments):
            return {"tool_id": tool, "provider": tool, "data": {"items": [{"current_request": arguments["query"]}]},
                    "sources": [{"title": tool, "canonical_url": "https://fixture.invalid/" + tool}]}
        fallback = Task("fallback"); fallback.transition(TaskState.ROUTING); fallback.transition(TaskState.GENERATING); fallback.transition(TaskState.COMPLETED)
        for case in cases:
            case_number = int(case["id"].rsplit("-", 1)[1])
            with self.subTest(case=case["id"]), patch.object(app.laya_provider.local, "loaded", True), \
                 patch.object(app.laya_provider, "choice", return_value={"tool_id": "__analysis_selection_complete__"}), \
                 patch.object(app.runtime, "_generate_brain", side_effect=generate), \
                 patch.object(app.runtime, "execute", return_value=(fallback, "不足部分の代替根拠")), \
                 patch.object(app, "compile_provider_arguments", side_effect=lambda tool, prompt, seed: {"query": prompt}), \
                 patch.object(app, "execute_external_tool", side_effect=execute) as calls, \
                 patch.object(app, "external_tool_summary", return_value="current provider packet"), \
                 patch.object(app, "external_tool_credential_setup", return_value=None), \
                 patch.object(app, "external_tool_display_blocks", return_value=[]):
                outcome = app._run_autonomous_analysis(case["prompt"], {"candidate_ids": ["finance.bis", *case["tool_ids"]]})
                self.assertNotIn("blocked", outcome)
                actual = {tools.CANONICAL_ALIASES.get(call.args[0], call.args[0]) for call in calls.call_args_list}
                expected = {tools.CANONICAL_ALIASES.get(tool, tool) for tool in essentials[case_number]}
                self.assertTrue(expected <= actual, (expected - actual, actual))
                self.assertTrue(all(item["analysis_request_id"] == outcome["tool"]["analysis_request_id"]
                                    for item in outcome["tool"]["results"]))

    def test_fanout_keeps_series_dates_units_and_each_source(self):
        def child(series, values, unit):
            return {"data": {"series_id": series, "items": [
                {"date": day, "year": day[:4], "value": value, "unit": unit, "series_id": series}
                for day, value in values]}, "sources": [{"title": series, "canonical_url": "https://fred.stlouisfed.org/series/" + series}]}
        data = {"render_nested_evidence": True, "items": [
            child("FEDFUNDS", [("2026-01-01", 4.5), ("2026-09-01", 4.0)], "%"),
            child("CPIAUCSL", [("2026-01-01", 320), ("2026-09-01", 325)], "index")]}
        with patch.object(app.runtime, "_generate_brain", side_effect=AssertionError("numeric composition is deterministic")):
            response, sources, _ = app._compose_autonomous_analysis("金利とCPIを比較", [{"tool_id": "econ.fred", "provider": "FRED", "data": data}])
        self.assertEqual(2, len(sources))
        for text in ("2026-01-01", "2026-09-01", "4.5%", "4%", "320index", "325index", "米国政策金利", "米国消費者物価指数"):
            self.assertIn(text, response)
        self.assertNotIn("320%", response)

    def test_mixed_packet_cannot_publish_previous_turn_statistic(self):
        evidence = [
            {"tool_id": "econ.fred", "provider": "FRED", "data": {"series_id": "UNRATE", "items": [
                {"date": "2026-09-01", "value": 4.2, "unit": "%", "country": "United States"}]},
             "sources": [{"title": "FRED", "canonical_url": "https://fred.stlouisfed.org/series/UNRATE"}]},
            {"tool_id": "research.openalex", "provider": "OpenAlex", "data": {"works": [{"title": "Current paper"}]},
             "sources": [{"title": "OpenAlex", "canonical_url": "https://openalex.org/"}]}]
        with patch.object(app.runtime, "_generate_brain", return_value={"text": "米国の失業率は4.3%でした [S1]。 [S2]"}):
            response, _, ok = app._compose_autonomous_analysis("論文と失業率を照合", evidence)
        self.assertFalse(ok)
        self.assertNotIn("4.3%", response)
        self.assertIn("4.2%", response)

    def test_latest_bls_month_is_not_replaced_by_older_month_in_same_year(self):
        data = {"items": [{"year": "2026", "period": period, "value": value,
                           "series_id": "LNS14000000", "unit": "%"}
                          for period, value in [("M09", 4.2), ("M01", 4.3)]]}
        response, _, _ = app._compose_autonomous_analysis("米国の失業率の変化", [{
            "tool_id": "econ.us_labor", "provider": "BLS", "data": data,
            "sources": [{"title": "BLS", "canonical_url": "https://www.bls.gov/"}]}])
        self.assertIn("2026-09", response)
        self.assertRegex(response, r"2026-09[^\n]*4\.2%")
        self.assertNotRegex(response, r"2026-09[^\n]*4\.3%")

    def test_country_comparison_does_not_silently_drop_every_country_after_second(self):
        evidence = [{"tool_id": "econ.worldbank", "provider": "World Bank", "data": {"items": [
            {"country": country, "date": "2024", "value": value, "indicator": "EG.ELC.ACCS.ZS", "unit": "%"}
            for country, value in [("Japan", 100), ("Germany", 99), ("Korea", 98)]]},
            "sources": [{"title": "World Bank", "canonical_url": "https://data.worldbank.org/"}]}]
        response, _, _ = app._compose_autonomous_analysis("国別に電力アクセスを比較", evidence)
        for value in ("100%", "99%", "98%"):
            self.assertIn(value, response)
        self.assertIn("電力アクセス率", response)

    def test_temporal_comparison_never_subtracts_different_units(self):
        result = {"tool_id": "econ.imf", "provider": "IMF", "data": {"items": [
            {"country": "KOR", "year": "2023", "value": 10, "indicator": "debt", "unit": "USD billions"},
            {"country": "KOR", "year": "2024", "value": 50, "indicator": "debt", "unit": "% of GDP"}]},
            "sources": [{"title": "IMF", "canonical_url": "https://www.imf.org/"}]}
        response, _, _ = app._compose_autonomous_analysis("債務を比較", [result])
        self.assertIn("10USD billions", response)
        self.assertIn("50% of GDP", response)
        self.assertNotIn("へ上昇", response)

    def test_model_failure_keeps_every_mixed_provider_evidence_visible(self):
        results = [{"tool_id": tool, "provider": label, "data": {"items": [{"title": label}]},
                    "sources": [{"title": label, "canonical_url": url}]} for tool, label, url in [
                    ("knowledge.qiita", "Qiita", "https://qiita.com/"),
                    ("software.github", "GitHub", "https://github.com/")]]
        with patch.object(app.runtime, "_generate_brain", side_effect=RuntimeError("unavailable")):
            response, sources, ok = app._compose_autonomous_analysis("記事と実装を照合", results)
        self.assertFalse(ok)
        self.assertEqual(2, len(sources))
        self.assertIn("### Qiita", response)
        self.assertIn("### GitHub", response)
        self.assertIn("https://github.com/", response)

    def test_invalid_arguments_return_actionable_waiting_not_generic_error(self):
        with patch.object(app, "_laya_select_analysis_apis", return_value=["security.cve"]), \
             patch.object(app, "_brain_plan_analysis_apis", return_value=(["security.cve"], support._recipe(["security.cve"]))):
            outcome = app._run_autonomous_analysis("脆弱性の深刻度を分析", {})
        self.assertEqual(TaskState.WAITING, outcome["task"].state)
        self.assertNotIn("APIの応答状態を確認して再試行", outcome["response"])
        self.assertIn("CVE", outcome["response"])

    def test_one_provider_success_does_not_cover_omitted_second_indicator(self):
        message = "南アジアの電力アクセスと再生可能電力を比較して"
        result = {"tool_id": "econ.worldbank", "provider": "World Bank", "data": {"items": [
            {"country": {"id": "AF", "value": "Afghanistan"}, "date": "2024", "value": 80,
             "indicator": {"id": "EG.ELC.RNEW.ZS"}}]},
            "sources": [{"title": "World Bank", "canonical_url": "https://data.worldbank.org/"}]}
        recovery = Task(message); recovery.transition(TaskState.ROUTING); recovery.transition(TaskState.GENERATING); recovery.transition(TaskState.COMPLETED)
        with patch.object(app, "_laya_select_analysis_apis", return_value=["econ.worldbank"]), \
             patch.object(app, "_brain_plan_analysis_apis", return_value=(["econ.worldbank"], support._recipe(["econ.worldbank"]))), \
             patch.object(app, "external_tool_credential_setup", return_value=None), \
             patch.object(app, "execute_external_tool", return_value=result), \
             patch.object(app.runtime, "execute", return_value=(recovery, "不足指標を補完できませんでした")) as recover, \
             patch.object(app, "external_tool_display_blocks", return_value=[]):
            outcome = app._run_autonomous_analysis(message, {})
        recover.assert_called_once()
        self.assertIn("electricity access rate", recover.call_args.kwargs["external_search_query"])
        self.assertIn("電力アクセス率", outcome["response"])
        self.assertTrue(outcome["tool"]["missing_coverage"])

    def test_unsupported_adapter_capability_is_never_sent_to_provider(self):
        fallback = Task("fallback"); fallback.transition(TaskState.ROUTING)
        fallback.transition(TaskState.GENERATING); fallback.transition(TaskState.COMPLETED)
        with patch.object(app, "_laya_select_analysis_apis", return_value=["weather.open_meteo"]), \
             patch.object(app, "_brain_plan_analysis_apis", return_value=(
                 ["weather.open_meteo"], support._recipe(["weather.open_meteo"]))), \
             patch.object(app, "compile_provider_arguments", return_value={
                 "query": "Europe temperature", "unsupported_capability": True}), \
             patch.object(app, "external_tool_credential_setup", return_value=None), \
             patch.object(app, "execute_external_tool") as execute, \
             patch.object(app.runtime, "execute", return_value=(fallback, "No relevant source.")), \
             patch.object(app, "external_tool_display_blocks", return_value=[]):
            outcome = app._run_autonomous_analysis("欧州の気温と大気汚染を照合して", {})
        execute.assert_not_called()
        self.assertEqual("PROVIDER_CAPABILITY_UNSUPPORTED",
                         outcome["tool"]["failures"][0]["error"])


if __name__ == "__main__":
    unittest.main()
