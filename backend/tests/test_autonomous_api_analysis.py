import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
os.environ["OLCR_DB_PATH"] = str(Path(_tmp.name) / "autonomous-analysis.sqlite")
os.environ["OLCR_ALLOWED_ROOTS"] = _tmp.name

import olcr_api.app as app
from fastapi.testclient import TestClient
from olcr_api.config import Settings
from olcr_api.db import Database


def _recipe(tool_ids, *, request="Analyze the requested subject", quality_checks=None):
    return {"objective": "Analyze the requested subject", "analysis_method": "Compare supported observations",
            "sources": [{"tool_id": tool_id, "purpose": "Retrieve relevant evidence", "request": request,
                         "expected_evidence": "Relevant observations and provenance"} for tool_id in tool_ids],
            "quality_checks": quality_checks or ["Check scope, units, periods, missingness and provenance"]}


class AutonomousApiAnalysisTests(unittest.TestCase):
    def setUp(self):
        app._API_ROUTER_EMBEDDING_CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        document_fetch = patch.object(app, "fetch", side_effect=ValueError("offline_fixture"))
        document_fetch.start()
        self.addCleanup(document_fetch.stop)
        self.old_db, self.old_settings = app.db, app.settings
        db_path = str(Path(self.tmp.name) / "chat.sqlite")
        app.db = Database(db_path)
        app.db.initialize()
        app.rebuild(Settings(db_path=db_path, allowed_roots=(self.tmp.name,), external_access_enabled=True))
        app.db.create_project("project", self.tmp.name, time.time(), "project")
        app.db.create_conversation("conversation", time.time(), "conversation", "project")
        self.client = TestClient(app.app)

    def tearDown(self):
        app.db, app.settings = self.old_db, self.old_settings
        app.rebuild(self.old_settings)
        self.tmp.cleanup()

    def test_analysis_intent_is_narrow_and_supports_japanese_and_english(self):
        self.assertTrue(app._autonomous_analysis_requested("ウクライナ情勢を分析して"))
        self.assertTrue(app._autonomous_analysis_requested("数値を分析し、限界も説明して"))
        self.assertTrue(app._autonomous_analysis_requested("Brief me on Japan's energy outlook"))
        self.assertFalse(app._autonomous_analysis_requested("日本の人口を教えて"))
        self.assertFalse(app._explicit_knowledge_provider_named("ウクライナ情勢を分析して"))
        self.assertTrue(app._explicit_knowledge_provider_named("UCDP APIを使って分析して"))
        self.assertTrue(app._autonomous_analysis_requested("日本と韓国の経済成長を比較して傾向を説明して"))
        self.assertTrue(app._autonomous_analysis_requested("賃金と消費者物価の推移を比較して、どう違うか教えて"))
        self.assertTrue(app._autonomous_analysis_requested("日本の再生可能電力の割合は、ここ10年でどんな流れだった？"))
        self.assertTrue(app._autonomous_analysis_requested("QiitaでAIエージェント実装記事を探して、使われる構成や共通点を整理して"))

    def test_llm_resource_analysis_uses_only_returned_abstracts(self):
        evidence = [{
            "tool_id": "research.openalex", "provider": "OpenAlex",
            "data": {"works": [{"title": "Measuring LLM Inference Energy",
                                 "publication_year": 2025,
                                 "abstract": "We measured electricity per inference.",
                                 "doi": "https://doi.org/10.1234/example"}]},
            "sources": [{"title": "OpenAlex", "canonical_url": "https://openalex.org/"}],
        }]
        with patch.object(app.runtime, "_generate_brain", side_effect=AssertionError("must not invent details")):
            response, _sources, synthesis_ok = app._compose_autonomous_analysis(
                "研究では大規模言語モデルの電力や水の使用量をどう測定・比較している？近年の論文を複数参照して、共通する知見と限界をまとめて。",
                evidence,
            )
        self.assertFalse(synthesis_ok)
        self.assertIn("We measured electricity per inference", response)
        self.assertIn("測定境界", response)
        self.assertNotIn("直接エネルギーを計測するE3", response)
        self.assertTrue(app._autonomous_analysis_requested("2022年以降の研究で何が分かってきたか、流れをまとめて"))
        self.assertTrue(app._autonomous_analysis_requested("最近の日本語技術記事から、AIエージェント実装の傾向を複数記事をもとに整理して"))
        self.assertTrue(app._autonomous_analysis_requested("近年の論文を複数参照して、LLMの環境影響をまとめて"))

    def test_qiita_analysis_rejects_internal_draft_and_uses_excerpt_grounded_fallback(self):
        articles = [
            {"title": "MCPで参照を渡すエージェント設計", "tags": ["AIエージェント", "MCP"],
             "body_excerpt": "必要なデータだけを参照で渡し、コンテキストを小さく保つ。",
             "url": "https://qiita.com/example/items/abcdef0123456789"},
            {"title": "Claude Codeの実行範囲を制御する", "tags": ["AIエージェント"],
             "body_excerpt": "skillとCLAUDE.mdで変更範囲と確認を定義する。",
             "url": "https://qiita.com/example/items/abcdef0123456790"},
            {"title": "複数エージェントの黒板モデル", "tags": ["AIエージェント"],
             "body_excerpt": "共有状態を使う黒板モデルとメッセージパッシングを比較する。",
             "url": "https://qiita.com/example/items/abcdef0123456792"},
            {"title": "AIコンパニオンのチャット設計", "tags": ["AIエージェント"],
             "body_excerpt": "回答を公開する前に、設定を確認して許可された内容だけを使う。",
             "url": "https://qiita.com/example/items/abcdef0123456793"},
            {"title": "AI Agent 1 Billion Free Tokens referral", "tags": ["AIエージェント"],
             "body_excerpt": "Use this referral code for free tokens.",
             "url": "https://qiita.com/example/items/abcdef0123456791"},
        ]
        result = {"tool_id": "knowledge.qiita", "provider": "Qiita",
                  "data": {"items": articles},
                  "sources": [{"title": "Qiita API v2", "canonical_url": "https://qiita.com/api/v2/docs"}]}
        with patch.object(app.runtime, "_generate_brain", return_value={
            "text": "Let me write the briefing now. [S1] Draft: likely common patterns are..."
        }) as generate:
            response, _sources, synthesis_ok = app._compose_autonomous_analysis(
                "最近の日本語技術記事からAIエージェントの実装構成と課題を複数記事で整理して", [result]
            )
        self.assertTrue(generate.called)
        self.assertFalse(synthesis_ok)
        self.assertIn("データ・コンテキストの受け渡し", response)
        self.assertIn("権限・実行範囲の制御", response)
        self.assertIn("エージェント間の協調構成", response)
        self.assertNotIn("abcdef0123456793", response.split("## 分析", 1)[1])
        self.assertNotIn("Let me write", response)
        self.assertNotIn("referral", response.split("## 分析", 1)[1])
        self.assertIn("採用頻度や代表性を示すものではありません", response)

    def test_world_bank_compiler_distinguishes_gdp_growth_from_gdp_level(self):
        prompt = "日本とドイツの実質GDP成長率はコロナ前後でどう違って見える？ World Bankの公開年次データで2019年以降を比べて分析して"
        compiled = app.compile_provider_arguments("econ.worldbank", prompt, {
            "countries": ["JP", "DE"], "indicator": "NY.GDP.MKTP.CD",
        })
        self.assertEqual(["JP", "DE"], compiled["countries"])
        self.assertEqual("NY.GDP.MKTP.KD.ZG", compiled["indicator"])
        self.assertEqual(2019, compiled["start_year"])
        self.assertIsNone(compiled["end_year"])
        summary = app.external_tool_summary({
            "tool_id": "econ.worldbank", "provider": "World Bank",
            "data": {"indicator": compiled["indicator"], "items": [
                {"date": "2025", "country": {"value": "Japan"}, "value": 1.1},
            ]},
        })
        self.assertIn("GDP growth (annual %)", summary)
        self.assertIn("NY.GDP.MKTP.KD.ZG", summary)
        level = app.compile_provider_arguments("econ.worldbank", "日本の実質GDPを年次比較", {
            "countries": ["JP"], "indicator": "NY.GDP.MKTP.CD",
        })
        self.assertEqual("NY.GDP.MKTP.KD", level["indicator"])

    def test_world_bank_compiler_requests_both_demographic_indicators_for_vague_decade_prompt(self):
        compiled = app.compile_provider_arguments(
            "econ.worldbank", "日本の少子高齢化はこの10年ほどでどう進んだ？出生率と高齢人口を分析して",
            {"query": "日本の少子高齢化"},
        )
        self.assertEqual(["JP"], compiled["countries"])
        self.assertEqual(["SP.DYN.TFRT.IN", "SP.POP.65UP.TO.ZS"], compiled["indicators"])
        self.assertEqual(2016, compiled["start_year"])
        self.assertIsNone(compiled["end_year"])

    def test_world_bank_compiler_requests_electricity_and_total_energy_renewable_shares(self):
        compiled = app.compile_provider_arguments(
            "econ.worldbank", "日本の電源構成で再生可能エネルギーはどう変化？直近10年の電力とエネルギー消費全体の比率を分析し、区別して",
            {"indicator": "EG.ELC.RNEW.ZS"},
        )
        self.assertEqual(["EG.ELC.RNEW.ZS", "EG.FEC.RNEW.ZS"], compiled["indicators"])
        self.assertEqual(2016, compiled["start_year"])
        self.assertIsNone(compiled["end_year"])

    def test_total_electricity_production_is_added_only_when_explicitly_requested(self):
        compiled = app.compile_provider_arguments(
            "econ.worldbank", "日本の再生可能エネルギー比率と電力生産量を比較して",
            {"indicator": "EG.ELC.RNEW.ZS"},
        )
        self.assertIn("EG.ELC.PROD.KH", compiled["indicators"])

    def test_south_asia_electricity_access_compiles_to_world_bank_series_and_countries(self):
        compiled = app.compile_provider_arguments(
            "statistics.world_bank",
            "南アジアでは、電力を利用できる人の割合は国ごとにどう違う？",
            {"query": "南アジアの電力アクセス"},
        )
        self.assertEqual(["AF", "BD", "BT", "IN", "MV", "NP", "PK", "LK"], compiled["countries"])
        self.assertEqual(["EG.ELC.ACCS.ZS"], compiled["indicators"])

    def test_laya_expands_related_candidates_then_stops_within_candidate_pool(self):
        routing = {"candidate_ids": ["conflict.ucdp", "statistics.world_bank", "knowledge.wikimedia"],
                   "selected": "conflict.ucdp"}
        with patch.object(app.laya_provider.local, "loaded", True), \
             patch.object(app.laya_provider, "choice", side_effect=[
                 {"tool_id": "statistics.world_bank"},
                 {"tool_id": "__analysis_selection_complete__"},
             ]) as choose:
            selected = app._laya_select_analysis_apis("世界の動向を分析して", routing)
        self.assertEqual(["conflict.ucdp", "statistics.world_bank"], selected)
        self.assertEqual(2, choose.call_count)
        self.assertLessEqual(len(choose.call_args_list[0].kwargs["candidates"]), 8)

    def test_laya_selects_first_analysis_api_when_router_abstains(self):
        routing = {"candidate_ids": ["statistics.world_bank", "econ.eurostat", "statistics.oecd"],
                   "selected": None}
        with patch.object(app.laya_provider.local, "loaded", True), \
             patch.object(app.laya_provider, "choice", side_effect=[
                 {"tool_id": "statistics.world_bank"},
                 {"tool_id": "__analysis_selection_complete__"},
             ]) as choose:
            selected = app._laya_select_analysis_apis("世界の動向を分析して", routing)
        self.assertEqual(["statistics.world_bank"], selected)

        self.assertEqual(2, choose.call_count)
        initial_ids = [item["tool_id"] for item in choose.call_args_list[0].kwargs["candidates"]]
        self.assertEqual(["statistics.world_bank", "econ.eurostat", "statistics.oecd",
                          "__analysis_selection_complete__"], initial_ids)
        self.assertLessEqual(len(choose.call_args_list[0].kwargs["candidates"]), 8)

    def test_laya_abstention_uses_domain_matched_retrieved_candidate(self):
        routing = {"candidate_ids": ["statistics.world_bank", "statistics.oecd"], "selected": None}
        with patch.object(app.laya_provider.local, "loaded", True), \
             patch.object(app.laya_provider, "choice", return_value={
                 "tool_id": "__analysis_selection_complete__",
             }):
            selected = app._laya_select_analysis_apis(
                "日本と韓国の経済成長を比較分析して", routing
            )
        self.assertEqual(["statistics.world_bank"], selected)

    def test_laya_analysis_shortlist_excludes_adjacent_but_wrong_demographic_apis(self):
        routing = {"candidate_ids": ["finance.bis", "econ.jp_estat", "statistics.oecd",
                                     "statistics.world_bank", "development.un_sdg"],
                   "selected": "econ.jp_estat"}
        with patch.object(app.laya_provider.local, "loaded", True), \
            patch.object(app.laya_provider, "choice", side_effect=[
                 {"tool_id": "statistics.world_bank"},
             ]) as choose:
            selected = app._laya_select_analysis_apis(
                "日本の少子高齢化を出生率と高齢人口から分析して", routing
            )
        self.assertEqual(["econ.worldbank"], selected)
        offered = {row["tool_id"] for row in choose.call_args_list[0].kwargs["candidates"]}
        self.assertNotIn("finance.bis", offered)
        self.assertNotIn("econ.jp_estat", offered)
        self.assertNotIn("statistics.oecd", offered)
        self.assertNotIn("development.un_sdg", offered)

    def test_mixed_numeric_and_non_numeric_provider_evidence_keeps_both_in_synthesis(self):
        evidence = [
            {"tool_id": "econ.worldbank", "provider": "World Bank",
             "data": {"indicator": "SP.DYN.TFRT.IN", "items": [
                 {"date": "2016", "country": {"value": "Japan"}, "value": 1.44,
                  "indicator": {"id": "SP.DYN.TFRT.IN"}},
                 {"date": "2024", "country": {"value": "Japan"}, "value": 1.15,
                  "indicator": {"id": "SP.DYN.TFRT.IN"}},
             ]},
             "sources": [{"title": "World Bank", "canonical_url": "https://data.worldbank.org/"}]},
            {"tool_id": "econ.oecd", "provider": "OECD",
             "data": {"items": [{"label": "population definition", "value": "population"}]},
             "sources": [{"title": "OECD", "canonical_url": "https://data.oecd.org/"}]},
        ]
        with patch.object(app.runtime, "_generate_brain", return_value={"text": "出生率の観測値は減少しています [S1]。OECDは定義情報のみで、別の観測値ではありません [S2]。"}) as generate, \
             patch.object(app, "external_tool_summary", return_value="provider evidence"):
            response, _sources, _ok = app._compose_autonomous_analysis("出生率を分析", evidence)
        generate.assert_called_once()
        packet = str(generate.call_args.args[0])
        self.assertIn("1.44", packet)
        self.assertIn("population definition", packet)
        self.assertIn("### OECD", response)
        self.assertNotIn("Generating", response)

    def test_brain_plan_is_constrained_to_laya_candidate_ids(self):
        with patch.object(app.runtime, "_generate_brain", return_value={"text": json.dumps(
                _recipe(["conflict.ucdp", "econ.worldbank"]) )}) as generate:
            planned, result = app._brain_plan_analysis_apis(
                "ウクライナ情勢を分析して", ["conflict.ucdp", "econ.worldbank"])
        self.assertEqual(["conflict.ucdp", "econ.worldbank"], planned)
        self.assertIn("quality_checks", result)
        schema = generate.call_args.kwargs["structured_schema"]
        self.assertEqual({"conflict.ucdp", "econ.worldbank"},
                         set(schema["properties"]["sources"]["items"]["properties"]["tool_id"]["enum"]))
        system_prompt = generate.call_args.args[0][0]["content"]
        for heuristic in ("time window", "commensurable units", "Never infer causation", "candidate set"):
            self.assertIn(heuristic, system_prompt)

    def test_single_laya_candidate_still_gets_a_brain_analysis_recipe(self):
        with patch.object(app.runtime, "_generate_brain", return_value={
                "text": json.dumps(_recipe(["conflict.ucdp"], request="Ukraine events within the requested dates"))
        }) as generate:
            planned, recipe = app._brain_plan_analysis_apis("ウクライナの暴力イベントを分析", ["conflict.ucdp"])
        self.assertEqual(["conflict.ucdp"], planned)
        self.assertEqual("Ukraine events within the requested dates", recipe["sources"][0]["request"])
        self.assertEqual(1, generate.call_count)

    def test_brain_plan_retries_once_for_malformed_json_and_accepts_fenced_json(self):
        with patch.object(app.runtime, "_generate_brain", side_effect=[
                {"text": "not-json"},
                {"text": '```json\n' + json.dumps(_recipe(["conflict.ucdp"])) + '\n```'},
        ]) as generate:
            planned, _ = app._brain_plan_analysis_apis("Analyze", ["conflict.ucdp", "econ.worldbank"])
        self.assertEqual(["conflict.ucdp"], planned)
        self.assertEqual(2, generate.call_count)
        self.assertIn("failed validation", generate.call_args.args[0][-1]["content"])
        self.assertFalse(generate.call_args.kwargs["thinking_override"])

    def test_laya_does_not_expand_a_production_api_into_its_legacy_adapter_alias(self):
        with patch.object(app.laya_provider.local, "loaded", True), \
             patch.object(app.laya_provider, "choice", side_effect=AssertionError("duplicate API offered")):
            selected = app._laya_select_analysis_apis("日本の最近の地震を分析して", {
                "candidate_ids": ["earth.earthquakes"], "selected": "earth.earthquakes"})
        self.assertEqual(["earth.earthquakes"], selected)

    def test_brain_plan_recovers_from_runtime_error_after_format_retry(self):
        with patch.object(app.runtime, "_generate_brain", side_effect=[
                {"text": "not-json"}, RuntimeError("transient model runtime failure"),
                {"text": json.dumps(_recipe(["energy.us_eia"]))},
        ]) as generate:
            planned, result = app._brain_plan_analysis_apis(
                "米国のエネルギー動向を分析", ["energy.us_eia", "econ.worldbank"]
            )
        self.assertEqual(["energy.us_eia"], planned)
        self.assertNotIn("planner_fallback", result)
        self.assertEqual(3, generate.call_count)

    def test_map_plan_has_bounded_time_for_two_places_and_failure_attempt_is_visible(self):
        with patch.object(app.runtime, '_generate_brain', return_value={'text':json.dumps(_recipe(['knowledge.wikimedia']))}), \
             patch.object(app, 'inference_timeout') as timeout:
            app._brain_plan_analysis_apis('歴史を比較して\n[USER_MAP_LOCATIONS]', ['knowledge.wikimedia'])
        timeout.assert_called_once_with(60)
        with patch.object(app.runtime, '_generate_brain', side_effect=RuntimeError('timeout')), patch('builtins.print'):
            _, result=app._brain_plan_analysis_apis('地震を比較して\n[USER_MAP_LOCATIONS]', ['earth.earthquakes'])
        self.assertEqual('runtime_failure_fallback',result['_model_call']['status'])
        self.assertEqual('UNKNOWN',result['_model_call']['actual_model'])
        self.assertEqual(3,result['_model_call']['attempts'])

    def test_qiita_is_available_to_laya_and_brain_for_analysis_planning(self):
        candidates = ["knowledge.qiita", "dev.github"]
        with patch.object(app.laya_provider.local, "loaded", True), \
             patch.object(app.laya_provider, "choice", side_effect=[
                 {"tool_id": "knowledge.qiita"},
                 {"tool_id": "__analysis_selection_complete__"},
             ]):
            selected = app._laya_select_analysis_apis("Pythonの実装パターンを分析して", {
                "candidate_ids": candidates, "selected": "knowledge.qiita",
            })
        self.assertEqual(["knowledge.qiita"], selected)
        with patch.object(app.runtime, "_generate_brain", return_value={
                "text": json.dumps(_recipe(["knowledge.qiita"]))}) as generate:
            planned, _ = app._brain_plan_analysis_apis(
                "Pythonの実装パターンを分析して", selected + ["dev.github"]
            )
        self.assertEqual(["knowledge.qiita"], planned)
        request = json.loads(generate.call_args.args[0][1]["content"])
        self.assertEqual("Qiita API v2", request["candidate_apis"][0]["provider"])

    def test_brain_cannot_expand_plan_outside_laya_selection(self):
        with patch.object(app.runtime, "_generate_brain", return_value={"text": json.dumps(
                _recipe(["knowledge.wikimedia"]) )}):
            planned, result = app._brain_plan_analysis_apis(
                "Analyze", ["conflict.ucdp", "econ.worldbank"]
            )
        self.assertEqual([], planned)
        self.assertEqual({}, result)

    def test_malformed_brain_plan_falls_back_to_domain_matched_laya_approved_id(self):
        candidates = ["econ.worldbank", "energy.us_eia"]
        with patch.object(app.runtime, "_generate_brain", side_effect=[
                {"text": "not-json"}, {"text": "still-not-json"}, {"text": "also-not-json"}]), \
             patch("builtins.print"):
            planned, result = app._brain_plan_analysis_apis("Analyze GDP growth", candidates)
        self.assertEqual(["econ.worldbank"], planned)
        self.assertEqual("domain_match", result["planner_fallback"])
        self.assertEqual(["econ.worldbank"], [source["tool_id"] for source in result["sources"]])

    def test_malformed_brain_plan_uses_two_sources_when_cross_check_is_requested(self):
        candidates = ["econ.worldbank", "econ.oecd"]
        with patch.object(app.runtime, "_generate_brain", side_effect=[
                {"text": "not-json"}, {"text": "still-not-json"}, {"text": "also-not-json"}]), \
             patch("builtins.print"):
            planned, result = app._brain_plan_analysis_apis(
                "日本の出生率を複数の情報源で照合して分析", candidates
            )
        self.assertEqual(["econ.worldbank", "econ.oecd"], planned)
        self.assertEqual("domain_match", result["planner_fallback"])

    def test_population_candidates_exclude_unverified_adjacent_adapters(self):
        self.assertEqual(
            ["econ.worldbank"],
            app._analysis_candidates_for_domain(
                "日本の出生率と高齢人口を複数ソースで分析",
                ["econ.oecd", "econ.worldbank", "development.un_sdg"],
            ),
        )

    def test_clinical_and_earthquake_evidence_render_with_requested_fields(self):
        clinical = app.external_tool_summary({
            "tool_id": "health.clinical_trials", "provider": "ClinicalTrials.gov",
            "data": {"query": "Alzheimer disease", "items": [{
                "nct_id": "NCT-123", "title": "Alzheimer intervention study",
                "overall_status": "RECRUITING", "phases": ["PHASE2"],
                "enrollment": 90, "enrollment_type": "ESTIMATED",
                "conditions": ["Alzheimer Disease"],
            }]},
        })
        self.assertIn("NCT-123", clinical)
        self.assertIn("状況：RECRUITING", clinical)
        self.assertIn("段階：PHASE2", clinical)

        earthquake = app.external_tool_summary({
            "tool_id": "earth.earthquakes", "provider": "USGS",
            "data": {"items": [{"id": "us-test", "properties": {
                "place": "10 km east of Tokyo", "time": 1791207045560, "mag": 4.6,
            }, "geometry": {"coordinates": [140.0, 35.5, 10.0]}}]},
        })
        self.assertIn("USGS · 地震", earthquake)
        self.assertIn("関東・伊豆：1件", earthquake)
        self.assertIn("10 km east of Tokyo", earthquake)
        self.assertIn("UTC · M4.6", earthquake)
        self.assertNotIn("1791207045560", earthquake)

    def test_earthquake_analysis_groups_global_coordinates_and_summarizes_magnitudes(self):
        events = [
            {"properties": {"mag": 6.6}, "geometry": {"coordinates": [168.1, -21.5, 10]}},
            {"properties": {"mag": 6.4}, "geometry": {"coordinates": [145.0, -6.1, 10]}},
            {"properties": {"mag": 6.5}, "geometry": {"coordinates": [107.3, -6.1, 10]}},
            {"properties": {"mag": 6.5}, "geometry": {"coordinates": [-169.8, 52.8, 10]}},
        ]
        provider_results = [{"tool_id": "earth.earthquake", "provider": "USGS", "data": {"items": events},
                             "sources": [{"title": "USGS", "canonical_url": "https://earthquake.usgs.gov/"}]}]
        with patch.object(app.runtime, "_generate_brain", side_effect=AssertionError("must be deterministic")):
            response, _sources, synthesis_ok = app._compose_autonomous_analysis(
                "今年9月に世界で起きたM6以上の地震はどの地域に集中した？発生数と規模を分析して", provider_results)
        self.assertFalse(synthesis_ok)
        self.assertIn("4件", response)
        self.assertIn("インド太平洋（東南アジア〜メラネシア）: 3件", response)
        self.assertIn("アラスカ・アリューシャン列島: 1件", response)
        self.assertIn("6.4〜6.6（平均6.50）", response)

    def test_global_earthquake_evidence_assigns_regions_from_coordinates(self):
        summary = app.external_tool_summary({
            "tool_id": "earth.earthquake", "provider": "USGS",
            "data": {"items": [
                {"properties": {"mag": 6.6, "place": "New Caledonia"}, "geometry": {"coordinates": [168.1, -21.5, 10]}},
                {"properties": {"mag": 6.4, "place": "Papua New Guinea"}, "geometry": {"coordinates": [145.0, -6.1, 10]}},
                {"properties": {"mag": 6.5, "place": "Indonesia"}, "geometry": {"coordinates": [107.3, -6.1, 10]}},
                {"properties": {"mag": 6.5, "place": "Alaska"}, "geometry": {"coordinates": [-169.8, 52.8, 10]}},
            ]},
        })
        self.assertIn("インド太平洋（東南アジア〜メラネシア）：3件", summary)
        self.assertIn("アラスカ・アリューシャン列島：1件", summary)
        self.assertNotIn("地域特定不可：4件", summary)

    def test_long_term_pm25_analysis_routes_to_annual_world_bank_series(self):
        prompt = "日本の大気汚染は近年改善している？PM2.5濃度の年次推移から傾向を説明して"
        self.assertEqual(["econ.worldbank"], app._analysis_candidates_for_domain(prompt, ["environment.air_quality"]))

    def test_world_bank_worldwide_emissions_renders_latest_country_rankings_and_global_series(self):
        summary = app.external_tool_summary({
            "tool_id": "econ.worldbank", "provider": "World Bank",
            "data": {"countries": ["ALL"], "indicators": ["EN.ATM.CO2E.KT"],
                     "request_complete": True, "items": [
                {"date": "2000", "value": 100, "countryiso3code": "WLD",
                 "country": {"value": "World"}, "indicator": {"id": "EN.ATM.CO2E.KT"}},
                {"date": "2023", "value": 200, "countryiso3code": "WLD",
                 "country": {"value": "World"}, "indicator": {"id": "EN.ATM.CO2E.KT"}},
                {"date": "2023", "value": 50, "countryiso3code": "CHN",
                 "country": {"value": "China"}, "region": {"value": "Aggregates"},
                 "indicator": {"id": "EN.ATM.CO2E.KT"}},
                {"date": "2023", "value": 40, "countryiso3code": "USA",
                 "country": {"value": "United States"}, "region": {"value": "North America"},
                 "indicator": {"id": "EN.ATM.CO2E.KT"}},
            ]},
        })
        self.assertIn("2023年", summary)
        self.assertIn("世界合計の年次推移", summary)
        self.assertIn("United States：40", summary)
        self.assertNotIn("China：50", summary)

    def test_analysis_domain_recall_recovers_candidates_below_embedding_floor(self):
        cases = [
            ("日本と韓国、少子高齢化の進み方ってこの20年でどう違う？出生率と高齢人口の数字で比較", ["econ.worldbank"]),
            ("コロナ後、日本に来る旅行者はどれくらい戻った？2019年以降の年次推移", ["econ.worldbank"]),
            ("日本と韓国のエネルギー輸入依存を長期で比較", ["econ.worldbank"]),
            ("世界のCO2排出量を一人あたりでも比較", ["econ.worldbank"]),
            ("Qiitaで最近のAIエージェント実装記事を探して共通点を整理", ["knowledge.qiita"]),
            ("最近の日本語技術記事から、AIエージェントの実装でよく使われる構成と課題を複数記事をもとに整理して", ["knowledge.qiita"]),
            ("2022年以降の大規模言語モデルの電力と水の消費研究をまとめて", ["research.openalex"]),
            ("研究では大規模言語モデルの電力や水の使用量をどう測定・比較している？近年の論文を複数参照して、共通する知見と限界をまとめて。", ["research.openalex"]),
            ("日本の輸出入と貿易収支を分析して", ["trade.un_comtrade"]),
            ("東京のPM2.5とNO2を季節ごとに分析して", ["environment.air_quality"]),
            ("日本の地震を分析して", ["earth.earthquakes"]),
            ("GLP-1肥満治療の臨床試験を分析して", ["health.clinical_trials"]),
            ("リチウムイオン電池のリサイクル研究を分析して", ["research.openalex"]),
            ("系外惑星の発見年と公転周期を分析して", ["space.exoplanets"]),
            ("OECD諸国で高齢者の就業率がどう違うか、直近年で見たい。", ["statistics.oecd"]),
            ("Qiitaで最近のAIエージェント実装記事を探して共通点を整理", ["knowledge.qiita"]),
        ]
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                found = app._analysis_candidates_for_domain(prompt, [])
                self.assertEqual(expected, found)
        tourism = "2019年以降、日本を訪れる旅行者の回復はどの程度進んだ？観光客数の年次データからコロナ前との違いを説明して。"
        self.assertTrue(app._autonomous_analysis_requested(tourism))
        self.assertEqual(["econ.worldbank"], app._analysis_candidates_for_domain(tourism, []))
        gui_tourism = "日本を訪れる外国人旅行者は2019年以降どう戻った？年ごとの変化から特徴をまとめて"
        self.assertTrue(app._autonomous_analysis_requested(gui_tourism))
        self.assertEqual(["econ.worldbank"], app._analysis_candidates_for_domain(gui_tourism, []))
        combined = app._analysis_candidates_for_domain(
            "日本の賃金と消費者物価の推移を比較分析して",
            ["econ.jp_estat", "econ.oecd", "econ.worldbank", "econ.us_labor", "econ.fred"],
        )
        self.assertEqual(["econ.jp_estat", "econ.worldbank"], combined)

    def test_cross_region_gdp_comparison_excludes_regional_development_banks(self):
        candidates = app._analysis_candidates_for_domain(
            "日本とドイツの実質GDP成長率を2010年以降で比較し分析",
            ["econ.worldbank", "econ.oecd", "econ.adb", "econ.afdb"],
        )
        self.assertEqual(["econ.worldbank"], candidates)
        lexical = [(10, "econ.adb", 8), (7, "econ.worldbank", 5)]
        self.assertEqual((7, "econ.worldbank", 5),
                         app._best_shortlisted_lexical_candidate(lexical, candidates))

    def test_external_tool_error_exposes_safe_category_for_router_abstention(self):
        error = app.ExternalToolError("ARGUMENT_COMPILATION_FAILED")
        self.assertEqual("ARGUMENT_COMPILATION_FAILED", error.category)

    def test_analysis_degraded_fallback_matches_requested_indicator_and_domain(self):
        cases = [
            ("日本と韓国のエネルギー輸入依存を長期で比較", ["trade.un_comtrade", "econ.worldbank"], "econ.worldbank"),
            ("実質GDP成長率を日本とドイツで分析して", ["finance.bis", "econ.worldbank"], "econ.worldbank"),
            ("日本の消費者物価指数を分析して", ["econ.fred", "econ.jp_estat"], "econ.jp_estat"),
            ("日本の少子高齢化と人口を分析して", ["finance.bis", "econ.worldbank"], "econ.worldbank"),
            ("日本の出生率と高齢人口を分析して", ["econ.jp_estat", "econ.worldbank"], "econ.worldbank"),
            ("ウクライナの紛争を分析して", ["econ.worldbank", "conflict.ucdp"], "conflict.ucdp"),
        ]
        for prompt, candidates, expected in cases:
            with self.subTest(prompt=prompt):
                self.assertEqual(expected, app._analysis_fallback_source(prompt, candidates))

    def test_cpi_analysis_uses_only_calculable_changes_and_discloses_missing_yoy_baseline(self):
        evidence = [{
            "tool_id": "econ.jp_estat",
            "data": {"items": [
                {"period": "2026年8月", "value": 114.3},
                {"period": "2026年7月", "value": 114.2},
                {"period": "2025年10月", "value": 112.8},
            ]},
            "sources": [{"source_id": "S1", "title": "e-Stat 消費者物価指数",
                          "canonical_url": "https://www.e-stat.go.jp/stat-search/files?page=1"}],
        }]
        with patch.object(app.runtime, "_generate_brain", side_effect=AssertionError("CPI synthesis must be deterministic")):
            response, sources, synthesis_ok = app._compose_autonomous_analysis("日本の物価を分析して", evidence)
        self.assertFalse(synthesis_ok)
        self.assertEqual("S1", sources[0]["source_id"])
        self.assertIn("+1.5指数ポイント", response)
        self.assertIn("前月比 +0.09%", response)
        self.assertIn("前年同月比のインフレ率はここでは算出できません", response)
        self.assertIn("## 取得したAPI出力（証拠）", response)
        self.assertIn("[e-Stat 消費者物価指数](https://", response)
        self.assertNotIn("[S1]", response)
        self.assertNotIn("出现", response)
        self.assertNotIn("1990", response)

    def test_ucdp_grouped_analysis_is_deterministic_and_uses_aggregates_not_raw_events(self):
        evidence = [{
            "tool_id": "conflict.ucdp", "data": {
                "dataset_version": "26.1", "aggregated_row_count": 2,
                "analysis_summary": {"monthly": [
                    {"month": "2025-01", "events": 2, "best_deaths": 3},
                ], "top_regions": [
                    {"region": "Kyiv", "events": 2, "best_deaths": 3},
                ]},
            }, "sources": [{"source_id": "S1", "canonical_url": "https://ucdpapi.pcr.uu.se/api/gedevents/26.1"}],
        }]
        with patch.object(app.runtime, "_generate_brain", side_effect=AssertionError("aggregate is deterministic")):
            response, sources, synthesis_ok = app._compose_autonomous_analysis("月別・地域別に分析して", evidence)
        self.assertFalse(synthesis_ok)
        self.assertEqual("S1", sources[0]["source_id"])
        self.assertIn("2025-01: 2件", response)
        self.assertIn("Kyiv: 2件", response)
        self.assertIn("死者数の完全な実数や因果関係を意味しません", response)

    def test_chat_dispatches_generic_analysis_to_autonomous_api_pipeline(self):
        prompt = "東アジアのエネルギー転換を分析して"
        routing = {"candidate_ids": ["energy.us_eia", "econ.worldbank"],
                   "selected": "energy.us_eia", "retrieval": "embedding"}
        task = app.Task(prompt); task.transition(app.TaskState.ROUTING)
        task.transition(app.TaskState.GENERATING); task.transition(app.TaskState.COMPLETED)

        def decide(_message):
            app._API_ROUTING_TRACE.set(routing)
            return "energy.us_eia", {"query": prompt}

        with patch.object(app, "route_external_tool", return_value=None), \
             patch.object(app, "router_decision", side_effect=decide), \
             patch.object(app, "_run_autonomous_analysis", return_value={
                 "task": task, "response": "取得証拠と統合分析", "sources": [], "blocks": [],
                 "tool": {"kind": "autonomous_analysis"},
             }) as run_analysis:
            response = self.client.post("/api/chat", json={
                "message": prompt, "project_id": "project", "conversation_id": "conversation",
            })
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("取得証拠と統合分析", response.json()["response"])
        run_analysis.assert_called_once_with(prompt, routing)

    def test_chat_dispatches_analysis_with_retrieved_candidates_when_router_abstains(self):
        prompt = "日本とドイツの景気回復を比べると、コロナ前後で何が違って見える？ 実質GDP成長率を公開データから取得して分析して。"
        routing = {"candidate_ids": ["econ.worldbank", "econ.eurostat", "econ.oecd"],
                   "selected": None, "retrieval": "embedding", "laya_called": True}
        task = app.Task(prompt)
        task.transition(app.TaskState.ROUTING); task.transition(app.TaskState.GENERATING)
        task.transition(app.TaskState.COMPLETED)

        def abstain(_message):
            app._API_ROUTING_TRACE.set(routing)
            return None

        with patch.object(app, "route_external_tool", return_value=None), \
             patch.object(app, "router_decision", side_effect=abstain), \
             patch.object(app, "_run_autonomous_analysis", return_value={
                 "task": task, "response": "証拠付き比較分析", "sources": [], "blocks": [],
                 "tool": {"kind": "autonomous_analysis"},
             }) as run_analysis:
            response = self.client.post("/api/chat", json={
                "message": prompt, "project_id": "project", "conversation_id": "conversation",
            })
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("証拠付き比較分析", response.json()["response"])
        run_analysis.assert_called_once_with(prompt, routing)

    def test_explicit_api_name_does_not_bypass_autonomous_analysis_pipeline(self):
        prompt = "UCDPの記録からウクライナ紛争の月別・地域別傾向を分析して"
        task = app.Task(prompt)
        task.transition(app.TaskState.ROUTING); task.transition(app.TaskState.GENERATING)
        task.transition(app.TaskState.COMPLETED)
        with patch.object(app, "route_external_tool", return_value=("conflict.ucdp", {"country_id": "369"})), \
             patch.object(app, "router_decision", side_effect=AssertionError("deterministic API seed must be preserved")), \
             patch.object(app, "_run_autonomous_analysis", return_value={
                 "task": task, "response": "月別・地域別の集計", "sources": [], "blocks": [],
                 "tool": {"kind": "autonomous_analysis"},
             }) as run_analysis:
            response = self.client.post("/api/chat", json={
                "message": prompt, "project_id": "project", "conversation_id": "conversation",
            })
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("月別・地域別の集計", response.json()["response"])
        routing = run_analysis.call_args.args[1]
        self.assertEqual(["conflict.ucdp"], routing["candidate_ids"])
        self.assertEqual("conflict.ucdp", routing["seed_tool_id"])

    def test_analysis_preserves_deterministic_domain_api_when_semantic_router_abstains(self):
        prompt = "日本の再生可能エネルギーの普及状況をブリーフィングして。最近の進展と課題を、関連する公開データを複数探して突き合わせて。"
        task = app.Task(prompt); task.transition(app.TaskState.ROUTING)
        task.transition(app.TaskState.GENERATING); task.transition(app.TaskState.COMPLETED)
        with patch.object(app, "route_external_tool", return_value=("econ.worldbank", {"indicator": "EG.FEC.RNEW.ZS"})), \
             patch.object(app, "router_decision", return_value=None), \
             patch.object(app, "_run_autonomous_analysis", return_value={
                 "task": task, "response": "証拠に基づく統合分析", "sources": [], "blocks": [],
                 "tool": {"kind": "autonomous_analysis"},
             }) as run_analysis:
            response = self.client.post("/api/chat", json={
                "message": prompt, "project_id": "project", "conversation_id": "conversation",
            })
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("証拠に基づく統合分析", response.json()["response"])
        self.assertEqual(prompt, run_analysis.call_args.args[0])
        self.assertEqual(["econ.worldbank", "development.un_sdg"],
                         run_analysis.call_args.args[1]["candidate_ids"])
        self.assertEqual("deterministic_analysis_seed", run_analysis.call_args.args[1]["retrieval"])
        self.assertEqual({"indicator": "EG.FEC.RNEW.ZS"},
                         run_analysis.call_args.args[1]["seed_arguments"])
        self.assertEqual("econ.worldbank", run_analysis.call_args.args[1]["seed_tool_id"])

    def test_streaming_analysis_delegates_before_direct_provider_execution(self):
        prompt = "日本の再生可能エネルギーの普及状況をブリーフィングして。最近の進展と課題を、関連する公開データを複数探して突き合わせて。"
        task = app.Task(prompt); task.transition(app.TaskState.ROUTING)
        task.transition(app.TaskState.GENERATING); task.transition(app.TaskState.COMPLETED)
        with patch.object(app, "route_external_tool", return_value=("econ.worldbank", {"indicator": "EG.FEC.RNEW.ZS"})), \
             patch.object(app, "router_decision", return_value=None), \
             patch.object(app, "_run_autonomous_analysis", return_value={
                 "task": task, "response": "複数API候補に基づく分析", "sources": [], "blocks": [],
                 "tool": {"kind": "autonomous_analysis"},
             }) as run_analysis, \
             patch.object(app, "execute_external_tool", side_effect=AssertionError("stream bypassed shared analysis")):
            response = self.client.post("/api/chat/stream", json={
                "message": prompt, "project_id": "project", "conversation_id": "conversation",
            })
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn("複数API候補に基づく分析", response.text)
        self.assertEqual("deterministic_analysis_seed", run_analysis.call_args.args[1]["retrieval"])

    def test_streaming_qiita_failure_uses_brave_then_wikipedia_runtime_fallback(self):
        prompt = "Reactで状態管理の技術記事を3つ探して"
        task = app.Task(prompt)
        task.transition(app.TaskState.ROUTING); task.transition(app.TaskState.GENERATING)
        task.transition(app.TaskState.COMPLETED)
        with patch.object(app, "route_external_tool", return_value=("knowledge.qiita", {"query": "React", "limit": 3})), \
             patch.object(app, "compile_provider_arguments", return_value={"query": "React", "limit": 3}), \
             patch.object(app, "external_tool_credential_setup", return_value=None), \
             patch.object(app, "execute_external_tool", side_effect=app.ExternalToolError("PROVIDER_UNAVAILABLE")), \
             patch.object(app, "_qiita_provider_failure_fallback", return_value=(task, "Brave/Wikipedia fallback answer")) as fallback:
            response = self.client.post("/api/chat/stream", json={
                "message": prompt, "project_id": "project", "conversation_id": "conversation",
            })
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn("Brave/Wikipedia fallback answer", response.text)
        fallback.assert_called_once_with(prompt)

    def test_chat_qiita_failure_uses_brave_then_wikipedia_runtime_fallback(self):
        prompt = "Reactで状態管理の技術記事を3つ探して"
        task = app.Task(prompt)
        task.transition(app.TaskState.ROUTING); task.transition(app.TaskState.GENERATING)
        task.transition(app.TaskState.COMPLETED)
        with patch.object(app, "route_external_tool", return_value=("knowledge.qiita", {"query": "React", "limit": 3})), \
             patch.object(app, "router_decision", return_value=None), \
             patch.object(app, "external_tool_credential_setup", return_value=None), \
             patch.object(app, "compile_provider_arguments", return_value={"query": "React", "limit": 3}), \
             patch.object(app, "execute_external_tool", side_effect=app.ExternalToolError("PROVIDER_UNAVAILABLE")), \
             patch.object(app, "_qiita_provider_failure_fallback", return_value=(task, "Brave/Wikipedia fallback answer")) as fallback:
            response = self.client.post("/api/chat", json={
                "message": prompt, "project_id": "project", "conversation_id": "conversation",
            })
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("Brave/Wikipedia fallback answer", response.json()["response"])
        fallback.assert_called_once_with(prompt)

    def test_english_community_failures_preserve_provider_and_fallback_in_both_routes(self):
        prompt = "Find React state management articles"
        for tool_id in ("knowledge.devto", "news.hackernews"):
            for endpoint in ("/api/chat", "/api/chat/stream"):
                with self.subTest(tool_id=tool_id, endpoint=endpoint):
                    task = app.Task(prompt)
                    task.transition(app.TaskState.ROUTING)
                    task.transition(app.TaskState.GENERATING)
                    task.transition(app.TaskState.COMPLETED)
                    with patch.object(app, "route_external_tool", return_value=(tool_id, {"query": "React"})), \
                         patch.object(app, "router_decision", return_value=None), \
                         patch.object(app, "compile_provider_arguments", return_value={"query": "React"}), \
                         patch.object(app, "external_tool_credential_setup", return_value=None), \
                         patch.object(app, "execute_external_tool", side_effect=app.ExternalToolError("PROVIDER_TIMEOUT")), \
                         patch.object(app, "_community_provider_failure_fallback", return_value=(task, "Fallback evidence")) as fallback:
                        response = self.client.post(endpoint, json={"message": prompt,
                            "project_id": "project", "conversation_id": "conversation"})
                    self.assertEqual(200, response.status_code)
                    self.assertIn("Fallback evidence", response.text)
                    fallback.assert_called_once_with(prompt, tool_id)
                    self.assertTrue(any(entry.get("tool") == tool_id and entry.get("error") == "PROVIDER_TIMEOUT"
                                        for entry in task.tool_executions))

    def test_qiita_failure_uses_shared_recovery_owner_when_search_is_disabled(self):
        task = app.Task("q"); task.transition(app.TaskState.ROUTING)
        task.transition(app.TaskState.GENERATING); task.transition(app.TaskState.COMPLETED)
        task.selected_context.append({"inference_fallback": {"external_evidence_verified": False}})
        with patch.object(app.settings, "web_mode", "off"), \
             patch.object(app.runtime, "execute", return_value=(task, "留保付き推論")) as recover:
            _, response = app._qiita_provider_failure_fallback("Reactの状態管理の記事を探して")
        self.assertEqual("留保付き推論", response)
        self.assertTrue(recover.call_args.kwargs["api_miss_fallback"])
        self.assertTrue(recover.call_args.kwargs["wikipedia_fallback_allowed"])

    def test_failed_source_recovery_reaches_disclosed_inference(self):
        failed = app.Task("q"); failed.transition(app.TaskState.ROUTING)
        failed.transition(app.TaskState.GENERATING); failed.transition(app.TaskState.FAILED)
        failed.error = "WIKIMEDIA_NOT_FOUND"
        complete = app.Task("q"); complete.transition(app.TaskState.ROUTING)
        complete.transition(app.TaskState.GENERATING); complete.transition(app.TaskState.COMPLETED)
        complete.selected_context.append({"inference_fallback": {"external_evidence_verified": False}})
        with patch.object(app.runtime, "execute", return_value=(failed, "")), \
             patch.object(app.runtime, "infer_without_sources", return_value=(complete, "外部情報未確認の一般回答")) as infer:
            task, response = app._qiita_provider_failure_fallback("Reactの状態管理の記事を探して")
        self.assertEqual("completed", task.state.value)
        self.assertIn("外部情報未確認", response)
        self.assertEqual("WIKIMEDIA_NOT_FOUND", infer.call_args.kwargs["failure_reason"])

    def test_aggregate_brain_analysis_displays_provider_evidence_and_resolves_citations(self):
        provider_results = [
            {"tool_id": "conflict.ucdp", "provider": "UCDP", "fetched_at": "2026-10-04T00:00:00Z",
             "data": {"semantic_status": "DATA", "summary": "one reported event"},
             "sources": [{"title": "UCDP GED", "canonical_url": "https://ucdp.uu.se/api"}]},
            {"tool_id": "econ.worldbank", "provider": "World Bank", "fetched_at": "2026-10-04T00:00:00Z",
             "data": {"semantic_status": "DATA", "summary": "GDP series returned: 100"},
             "sources": [{"title": "World Bank GDP", "canonical_url": "https://api.worldbank.org/v2"}]},
        ]
        with patch.object(app.runtime, "_generate_brain", return_value={
                "text": json.dumps({"fact_ids": ["E1R1", "E2R1"]})}), \
             patch.object(app, "external_tool_summary", side_effect=["one event", "GDP 100"]):
            response, sources, synthesis_ok = app._compose_autonomous_analysis("Analyze", provider_results)
        self.assertIn("## 取得したAPI出力（証拠）", response)
        self.assertIn("## 分析", response)
        self.assertIn("[UCDP GED](https://ucdp.uu.se/api)", response)
        self.assertIn("[World Bank GDP](https://api.worldbank.org/v2)", response)
        self.assertNotIn("[S1]", response)
        self.assertEqual(["S1", "S2"], [source["source_id"] for source in sources])
        self.assertTrue(synthesis_ok)

    def test_failed_model_citations_fall_back_to_bounded_tabular_comparison(self):
        provider_results = [
            {"tool_id": "development.un_sdg", "provider": "UN SDG API",
             "data": {"indicator": "NY.GDP.PCAP.KD.ZG", "items": [
                 {"year": "2023", "geo_area": "Japan", "value": 9.68, "unit": "%"},
                 {"year": "2021", "geo_area": "Japan", "value": 8.82, "unit": "%"},
             ]},
             "sources": [{"title": "UN SDG", "canonical_url": "https://unstats.un.org/sdgs/"}]},
            {"tool_id": "econ.worldbank", "provider": "World Bank",
             "data": {"indicator": "NY.GDP.PCAP.KD.ZG", "items": [
                 {"date": "2021", "country": {"value": "Japan"}, "countryiso3code": "JPN",
                  "value": 8.8, "indicator": {"id": "NY.GDP.PCAP.KD.ZG"}},
             ]},
             "sources": [{"title": "World Bank", "canonical_url": "https://data.worldbank.org/"}]},
        ]
        with patch.object(app.runtime, "_generate_brain", return_value={"text": "Ungrounded answer."}), \
             patch.object(app, "external_tool_summary", return_value="tabular evidence"):
            response, _sources, synthesis_ok = app._compose_autonomous_analysis("brief", provider_results)
        self.assertFalse(synthesis_ok)
        self.assertIn("2021年の8.82%から2023年の9.68%へ上昇", response)
        self.assertIn("[UN SDG](https://unstats.un.org/sdgs/)", response)
        self.assertNotIn("課題や直近年の状況を断定できるだけ", response)
        self.assertNotIn("Ungrounded answer", response)

    def test_tabular_synthesis_rejects_wrong_delta_and_false_disagreement_claims(self):
        provider_results = [
            {"tool_id": "development.un_sdg", "provider": "UN SDG API",
             "data": {"indicator": "NY.GDP.PCAP.KD.ZG", "items": [
                 {"year": "2023.0", "geo_area": "Japan", "value": 9.68, "unit": "%"},
                 {"year": "2021.0", "geo_area": "Japan", "value": 8.82, "unit": "%"},
                 {"year": "2019.0", "geo_area": "Japan", "value": 7.78, "unit": "%"},
             ]},
             "sources": [{"title": "UN SDG", "canonical_url": "https://unstats.un.org/sdgs/"}]},
            {"tool_id": "econ.worldbank", "provider": "World Bank",
             "data": {"indicator": "NY.GDP.PCAP.KD.ZG", "items": [
                 {"date": "2021", "country": {"value": "Japan"}, "value": 8.8, "unit": "%",
                  "indicator": {"id": "NY.GDP.PCAP.KD.ZG"}},
                 {"date": "2022", "country": {"value": "Japan"}, "value": None,
                  "indicator": {"id": "NY.GDP.PCAP.KD.ZG"}},
             ]},
             "sources": [{"title": "World Bank", "canonical_url": "https://data.worldbank.org/"}]},
        ]
        hallucinated = (
            "両者の値が一致しない [S1]。2019年7.78%から2023年9.68%に約2.9ポイント上昇 [S1]。"
            "World Bankの値は2022年以降未公表 [S2]。"
        )
        with patch.object(app.runtime, "_generate_brain", return_value={"text": hallucinated}), \
             patch.object(app, "external_tool_summary", return_value="tabular evidence"):
            response, _sources, synthesis_ok = app._compose_autonomous_analysis("brief", provider_results)
        self.assertFalse(synthesis_ok)
        self.assertIn("2019年の7.78%から2023年の9.68%へ上昇しています（+1.9ポイント）", response)
        self.assertIn("共通する2021年の値", response)
        self.assertIn("差は0.02ポイント（ほぼ一致）", response)
        self.assertIn("### World Bank", response)
        self.assertNotIn("### Econ", response)
        self.assertIn("2022年はAPI応答に数値がありません", response)
        self.assertIn("理由（未発表・未更新など）は、この応答だけでは判断できません", response)
        self.assertNotIn("2.9ポイント", response)
        self.assertNotIn("値が一致しない", response)
        self.assertNotIn("未公表", response)
        self.assertIn("共通する2021年の値", response)

    def test_tabular_synthesis_keeps_country_series_separate(self):
        provider_results = [{
            "tool_id": "econ.worldbank", "provider": "World Bank",
            "data": {"indicator": "NY.GDP.MKTP.KD.ZG", "items": [
                {"date": "2021", "country": {"value": "Japan"}, "value": 2.2, "unit": "%"},
                {"date": "2025", "country": {"value": "Japan"}, "value": 1.1, "unit": "%"},
                {"date": "2021", "country": {"value": "Germany"}, "value": 3.7, "unit": "%"},
                {"date": "2025", "country": {"value": "Germany"}, "value": 0.2, "unit": "%"},
            ]},
            "sources": [{"title": "World Bank", "canonical_url": "https://data.worldbank.org/"}],
        }]
        hallucinated = "Japan rose from 2.2 to 3.7 and Germany fell from 1.1 to 0.2 [S1]."
        with patch.object(app.runtime, "_generate_brain", return_value={"text": hallucinated}), \
             patch.object(app, "external_tool_summary", return_value="GDP growth table"):
            response, _sources, synthesis_ok = app._compose_autonomous_analysis("日独の実質GDP成長率を比較", provider_results)
        self.assertFalse(synthesis_ok)
        self.assertIn("World Bankの実質GDP成長率では日本の値が2021年の2.2%から2025年の1.1%へ低下しています（−1.1ポイント）", response)
        self.assertIn("World Bankの実質GDP成長率ではドイツの値が2021年の3.7%から2025年の0.2%へ低下しています（−3.5ポイント）", response)
        self.assertIn("共通年の国別比較", response)
        self.assertIn("2025年はドイツ 0.2%、日本 1.1%", response)
        self.assertNotIn("Japan rose", response)
        self.assertNotIn("Germany rose", response)

    def test_tabular_analysis_skips_second_brain_call_and_names_population_metrics(self):
        provider_results = [{
            "tool_id": "econ.worldbank", "provider": "World Bank",
            "data": {"indicators": ["SP.DYN.TFRT.IN", "SP.POP.65UP.TO.ZS"], "items": [
                {"date": "2015", "country": {"value": "Japan"}, "value": 1.45,
                 "unit": "", "indicator": {"id": "SP.DYN.TFRT.IN"}},
                {"date": "2024", "country": {"value": "Japan"}, "value": 1.15,
                 "unit": "", "indicator": {"id": "SP.DYN.TFRT.IN"}},
                {"date": "2015", "country": {"value": "Japan"}, "value": 26.0,
                 "unit": "%", "indicator": {"id": "SP.POP.65UP.TO.ZS"}},
                {"date": "2024", "country": {"value": "Japan"}, "value": 29.6,
                 "unit": "%", "indicator": {"id": "SP.POP.65UP.TO.ZS"}},
            ]},
            "sources": [{"title": "World Bank", "canonical_url": "https://data.worldbank.org/"}],
        }]
        with patch.object(app.runtime, "_generate_brain", side_effect=AssertionError("must not block on synthesis")), \
             patch.object(app, "external_tool_summary", return_value="World Bank demographic evidence"):
            response, _sources, synthesis_ok = app._compose_autonomous_analysis("日本の少子高齢化を分析", provider_results)
        self.assertFalse(synthesis_ok)
        self.assertIn("取得できた公開データの範囲", response)
        self.assertIn("合計特殊出生率（女性1人あたりの出生数）", response)
        self.assertIn("総人口に占める65歳以上の割合", response)
        self.assertIn("2015年", response)
        self.assertIn("2024年", response)
        self.assertNotIn("複数の公開データを照合", response)
        self.assertNotIn("課題や直近年の状況を断定できるだけ", response)

    def test_renewables_analysis_names_both_distinct_metrics(self):
        evidence = [{
            "tool_id": "econ.worldbank", "provider": "World Bank",
            "data": {"items": [
                {"date": "2016", "country": {"value": "Japan"}, "value": 14.7,
                 "indicator": {"id": "EG.ELC.RNEW.ZS"}},
                {"date": "2021", "country": {"value": "Japan"}, "value": 21.1,
                 "indicator": {"id": "EG.ELC.RNEW.ZS"}},
                {"date": "2016", "country": {"value": "Japan"}, "value": 6.4,
                 "indicator": {"id": "EG.FEC.RNEW.ZS"}},
                {"date": "2021", "country": {"value": "Japan"}, "value": 8.8,
                 "indicator": {"id": "EG.FEC.RNEW.ZS"}},
            ]},
            "sources": [{"source_id": "S1", "title": "World Bank",
                         "canonical_url": "https://data.worldbank.org/"}],
        }]
        with patch.object(app.runtime, "_generate_brain", side_effect=AssertionError("deterministic")), \
             patch.object(app, "external_tool_summary", return_value="renewables evidence"):
            response, _sources, _synthesis_ok = app._compose_autonomous_analysis(
                "日本の再生可能エネルギーを分析", evidence
            )
        self.assertIn("電力発電量に占める再生可能電力の割合", response)
        self.assertIn("最終エネルギー消費に占める再生可能エネルギーの割合", response)
        self.assertIn("+6.4ポイント", response)
        self.assertIn("+2.4ポイント", response)

    def test_tourism_and_resource_outputs_include_metric_meaning_and_coverage_limits(self):
        evidence = [{
            "tool_id": "econ.worldbank", "provider": "World Bank",
            "data": {"items": [
                {"date": "2019", "country": {"value": "Japan"}, "value": 31882000,
                 "unit": "people", "indicator": {"id": "ST.INT.ARVL"}},
                {"date": "2020", "country": {"value": "Japan"}, "value": 4115811,
                 "unit": "people", "indicator": {"id": "ST.INT.ARVL"}},
                *[{"date": str(year), "country": {"value": "Japan"}, "value": None,
                   "unit": "people", "indicator": {"id": "ST.INT.ARVL"}}
                  for year in range(2021, 2026)],
            ]},
            "sources": [{"source_id": "S1", "title": "World Bank",
                         "canonical_url": "https://data.worldbank.org/"}],
        }]
        with patch.object(app.runtime, "_generate_brain", side_effect=AssertionError("deterministic")), \
             patch.object(app, "external_tool_summary", return_value="tourism evidence"):
            response, _sources, _synthesis_ok = app._compose_autonomous_analysis(
                "日本の訪日観光はコロナ後どれくらい戻った？ここ数年の流れを見たい", evidence
            )
        self.assertIn("国際観光客到着数（到着先国別）", response)
        self.assertIn("日本では訪日旅行者数", response)
        self.assertIn("回復状況は今回のWorld Bank系列からは判断できません", response)

        resource_evidence = [{
            "tool_id": "econ.worldbank", "provider": "World Bank",
            "data": {"items": [{"date": "2023", "country": {"value": "Japan"},
                                 "value": 92.5, "unit": "%",
                                 "indicator": {"id": "EG.IMP.CONS.ZS"}}]},
            "sources": [{"source_id": "S2", "title": "World Bank",
                         "canonical_url": "https://data.worldbank.org/"}],
        }]
        with patch.object(app.runtime, "_generate_brain", side_effect=AssertionError("deterministic")), \
             patch.object(app, "external_tool_summary", return_value="energy evidence"):
            response, _sources, _synthesis_ok = app._compose_autonomous_analysis(
                "日本のエネルギー自給はどんな傾向？", resource_evidence
            )
        self.assertIn("自給率そのものではない", response)

    def test_tabular_analysis_does_not_cross_check_unrelated_indicators(self):
        evidence = [
            {"tool_id": "econ.worldbank", "provider": "World Bank",
             "data": {"indicator": "SP.DYN.TFRT.IN", "items": [
                 {"date": "2023", "country": {"value": "Japan"}, "value": 1.2,
                  "unit": "births per woman", "indicator": {"id": "SP.DYN.TFRT.IN"}},
             ]}, "sources": [{"source_id": "S1", "title": "World Bank",
                              "canonical_url": "https://data.worldbank.org/"}]},
            {"tool_id": "development.un_sdg", "provider": "UN SDG API",
             "data": {"indicator": "8.1.1", "items": [
                 {"year": "2023", "geo_area": "Japan", "value": 1.2, "unit": "%"},
             ]}, "sources": [{"source_id": "S2", "title": "UN SDG",
                              "canonical_url": "https://unstats.un.org/sdgs/"}]},
        ]
        with patch.object(app.runtime, "_generate_brain", return_value={"text": "1.2 agrees across sources"}), \
             patch.object(app, "external_tool_summary", return_value="unrelated indicators"):
            response, _sources, synthesis_ok = app._compose_autonomous_analysis(
                "日本の出生率を複数の情報源で照合して分析", evidence
            )
        self.assertFalse(synthesis_ok)
        self.assertIn("同一指標・地域・年を別APIで確認できず", response)
        self.assertNotIn("複数の公開データを照合すると", response)
        self.assertNotIn("共通する2023年の値", response)
        self.assertNotIn("で、差は", response)

    def test_population_indicator_uses_scalar_birth_rate_units_and_rounded_values(self):
        evidence = [{
            "tool_id": "econ.worldbank", "provider": "World Bank",
            "data": {"indicator": "SP.DYN.TFRT.IN", "items": [
                {"date": "2016", "country": {"value": "Japan"}, "value": 1.4448123,
                 "indicator": {"id": "SP.DYN.TFRT.IN"}},
                {"date": "2024", "country": {"value": "Japan"}, "value": 1.1534,
                 "indicator": {"id": "SP.DYN.TFRT.IN"}},
            ]},
            "sources": [{"title": "World Bank", "canonical_url": "https://data.worldbank.org/"}],
        }]
        with patch.object(app.runtime, "_generate_brain", side_effect=AssertionError("deterministic tabular path")), \
             patch.object(app, "external_tool_summary", return_value="fertility evidence"):
            response, _sources, _ok = app._compose_autonomous_analysis("出生率を分析", evidence)
        self.assertIn("1.44から2024年の1.15へ低下しています（−0.29）", response)
        self.assertNotIn("1.44%", response)
        self.assertNotIn("ポイント", response)

    def test_null_values_reject_unpublished_claims_and_use_evidence_only_fallback(self):
        provider_results = [
            {"tool_id": "econ.worldbank", "provider": "World Bank",
             "data": {"items": [
                 {"date": "2021", "value": 8.8, "countryiso3code": "JPN"},
                 {"date": "2022", "value": None, "countryiso3code": "JPN"},
                 {"date": "2023", "value": None, "countryiso3code": "JPN"},
             ]},
             "sources": [{"title": "World Bank", "canonical_url": "https://data.worldbank.org/"}]},
        ]
        with patch.object(app.runtime, "_generate_brain", return_value={
                "text": "2022年以降の値は未発表です [S1]"}), \
             patch.object(app, "external_tool_summary", return_value="tabular evidence"):
            response, _sources, synthesis_ok = app._compose_autonomous_analysis("brief", provider_results)
        self.assertFalse(synthesis_ok)
        self.assertIn("2021年の8.8", response)
        self.assertIn("2022, 2023年はAPI応答に数値がありません", response)
        self.assertIn("理由（未発表・未更新など）は、この応答だけでは判断できません", response)
        self.assertNotIn("2022年以降の値は未発表です", response)

    def test_research_year_counts_are_deterministic_and_labeled_as_search_sample(self):
        provider_results = [{"tool_id": "research.openalex", "provider": "OpenAlex",
            "data": {"works": [
                {"publication_year": 2021, "title": "Hydrometallurgy of lithium-ion battery recycling"},
                {"publication_year": 2021, "title": "Direct recycling of lithium-ion batteries"},
                {"publication_year": 2023, "title": "Circular recovery of battery materials"},
            ]},
            "sources": [{"title": "OpenAlex", "canonical_url": "https://openalex.org/"}]}]
        with patch.object(app.runtime, "_generate_brain", side_effect=AssertionError("must be deterministic")), \
             patch.object(app, "external_tool_summary", return_value="OpenAlex evidence"):
            response, _sources, synthesis_ok = app._compose_autonomous_analysis(
                "年別件数と主なテーマを分析", provider_results)
        self.assertFalse(synthesis_ok)
        self.assertIn("2021年: 2件", response)
        self.assertIn("2023年: 1件", response)
        self.assertIn("検索結果内の件数", response)

    def test_exoplanet_grouped_catalog_rows_are_synthesized_without_brain(self):
        provider_results = [{"tool_id": "space.exoplanets", "provider": "NASA Exoplanet Archive",
            "data": {"items": [{"disc_year": 2022, "planet_count": 3, "mean_orbital_period_days": 18.5},
                                {"disc_year": 2023, "planet_count": 4, "mean_orbital_period_days": 21.0}]},
            "sources": [{"title": "NASA Exoplanet Archive", "canonical_url": "https://exoplanetarchive.ipac.caltech.edu/"}]}]
        with patch.object(app.runtime, "_generate_brain", side_effect=AssertionError("must be deterministic")), \
             patch.object(app, "external_tool_summary", return_value="NASA catalog evidence"):
            response, _sources, synthesis_ok = app._compose_autonomous_analysis(
                "発見年別の件数と公転周期の傾向", provider_results)
        self.assertFalse(synthesis_ok)
        self.assertIn("2022年: 3件", response)
        self.assertIn("2023年: 4件", response)
        self.assertIn("平均公転周期 21.00日", response)

    def test_multi_api_runner_uses_only_brain_planned_laya_candidates(self):
        routing = {"candidate_ids": ["conflict.ucdp", "statistics.world_bank"],
                   "selected": "conflict.ucdp", "retrieval": "embedding",
                   "seed_tool_id": "statistics.world_bank", "seed_arguments": {"indicator": "EG.FEC.RNEW.ZS"}}
        provider_result = lambda tool_id, provider: {
            "tool_id": tool_id, "provider": provider, "fetched_at": "2026-10-04T00:00:00Z",
            "data": {"semantic_status": "DATA", "items": [{"value": 1}]},
            "sources": [{"title": provider, "canonical_url": "https://example.org/source"}],
        }
        with patch.object(app.laya_provider.local, "loaded", True), \
             patch.object(app.laya_provider, "choice", side_effect=[
                 {"tool_id": "statistics.world_bank"},
                 {"tool_id": "__analysis_selection_complete__"},
             ]), \
             patch.object(app.runtime, "_generate_brain", side_effect=[
                {"text": json.dumps(_recipe(["statistics.world_bank"], request="Retrieve worldwide renewable energy share"))},
                 {"text": "World Bank's returned value is 1 [S1]."},
             ]), \
             patch.object(app, "external_tool_credential_setup", return_value=None), \
             patch.object(app, "compile_provider_arguments", return_value={"query": "brief"}) as compile_args, \
             patch.object(app, "execute_external_tool", return_value=provider_result("statistics.world_bank", "World Bank")) as execute, \
             patch.object(app, "external_tool_summary", return_value="World Bank evidence"), \
             patch.object(app, "external_tool_display_blocks", return_value=[]):
            outcome = app._run_autonomous_analysis("世界の動向を分析して", routing)
        self.assertNotIn("blocked", outcome)
        self.assertEqual(1, execute.call_count)
        self.assertEqual("statistics.world_bank", execute.call_args.args[0])
        self.assertNotIn("indicator", compile_args.call_args.args[2], "an unrequested router default cannot seed scope")
        self.assertIn("世界の動向を分析して", compile_args.call_args.args[1])
        self.assertIn("Retrieve worldwide renewable energy share", compile_args.call_args.args[1])
        self.assertEqual("EXTERNAL_API", outcome["task"].route.value)
        self.assertEqual("completed", outcome["task"].state.value)
        self.assertIn("World Bank evidence", outcome["response"])
        self.assertEqual(1, len(outcome["sources"]))

    def test_missing_provider_key_skips_only_that_api_and_shows_registration_card(self):
        missing = {"type": "credential_setup", "tool_id": "econ.us_census",
                   "provider": "U.S. Census", "fields": []}
        provider_result = {
            "tool_id": "statistics.world_bank", "provider": "World Bank",
            "data": {"semantic_status": "DATA", "items": [{"value": 1}]},
            "sources": [{"title": "World Bank", "canonical_url": "https://example.org/source"}],
        }
        with patch.object(app, "_laya_select_analysis_apis", return_value=["econ.us_census", "statistics.world_bank"]), \
             patch.object(app, "_brain_plan_analysis_apis", return_value=(
                 ["econ.us_census", "statistics.world_bank"],
                 _recipe(["econ.us_census", "statistics.world_bank"]),
             )), \
             patch.object(app, "external_tool_credential_setup", side_effect=lambda tool: missing if tool == "econ.us_census" else None), \
             patch.object(app, "compile_provider_arguments", return_value={"query": "analysis"}), \
             patch.object(app, "execute_external_tool", return_value=provider_result) as execute, \
             patch.object(app, "_compose_autonomous_analysis", return_value=("Partial analysis", [], True)), \
             patch.object(app, "external_tool_display_blocks", return_value=[]):
            outcome = app._run_autonomous_analysis("Analyze the economic trend", {})
        self.assertNotIn("blocked", outcome)
        self.assertEqual(["statistics.world_bank"], [call.args[0] for call in execute.call_args_list])
        self.assertIn("U.S. Censusを利用しようとしました", outcome["response"])
        self.assertIn("キー／認証情報が未登録", outcome["response"])
        self.assertIn(missing, outcome["blocks"])

    def test_all_missing_analysis_credentials_fall_back_and_offer_brave_key_setup(self):
        missing = {"type": "credential_setup", "tool_id": "econ.us_census",
                   "provider": "U.S. Census", "fields": []}
        fallback_task = app.Task("Analyze this subject")
        fallback_task.transition(app.TaskState.ROUTING)
        fallback_task.transition(app.TaskState.GENERATING)
        fallback_task.transition(app.TaskState.COMPLETED)
        with patch.object(app, "_laya_select_analysis_apis", return_value=["econ.us_census"]), \
             patch.object(app, "_brain_plan_analysis_apis", return_value=(
                 ["econ.us_census"], _recipe(["econ.us_census"]),
             )), \
             patch.object(app, "external_tool_credential_setup", return_value=missing), \
             patch.object(app.runtime, "execute", return_value=(fallback_task, "Wikipedia fallback")) as fallback, \
             patch.object(app, "provider_key", return_value=""), \
             patch.object(app.settings, "web_mode", "auto"):
            outcome = app._run_autonomous_analysis("Analyze this subject", {})
        self.assertIn("Wikipedia fallback", outcome["response"])
        self.assertIn("この提供元のデータは今回の根拠に含まれていません", outcome["response"])
        self.assertEqual(["econ.us_census", "web.search.brave"], [block["tool_id"] for block in outcome["blocks"]])
        self.assertEqual("https://api-dashboard.search.brave.com/", outcome["blocks"][1]["setup_url"])
        fallback.assert_called_once_with("Analyze this subject", suppress_web=False,
                                         api_miss_fallback=True, wikipedia_fallback_allowed=True, evidence_only=True,
                                         external_search_query="Analyze this subject")

    def test_all_provider_failures_fall_back_to_web_then_wikipedia_with_failure_trace(self):
        fallback_task = app.Task("Analyze this subject")
        fallback_task.transition(app.TaskState.ROUTING)
        fallback_task.transition(app.TaskState.GENERATING)
        fallback_task.transition(app.TaskState.COMPLETED)
        with patch.object(app, "_laya_select_analysis_apis", return_value=["econ.worldbank"]), \
             patch.object(app, "_brain_plan_analysis_apis", return_value=(
                 ["econ.worldbank"], _recipe(["econ.worldbank"]),
             )), \
             patch.object(app, "external_tool_credential_setup", return_value=None), \
             patch.object(app, "compile_provider_arguments", return_value={"query": "analysis"}), \
             patch.object(app, "execute_external_tool", side_effect=app.ExternalToolError("PROVIDER_TIMEOUT")), \
             patch.object(app.runtime, "execute", return_value=(fallback_task, "Brave/Wikipedia evidence")) as fallback, \
             patch.object(app, "provider_key", return_value="configured"), \
             patch.object(app.settings, "web_mode", "auto"):
            outcome = app._run_autonomous_analysis("Analyze this subject", {})
        self.assertNotIn("blocked", outcome)
        self.assertIn("Brave/Wikipedia evidence", outcome["response"])
        self.assertIn("Econ", outcome["response"])
        self.assertIn("Web検索、またはWikipedia APIへ切り替えました", outcome["response"])
        self.assertEqual("PROVIDER_TIMEOUT", fallback_task.selected_context[-1][
            "analysis_provider_failure_fallback"]["failed_tools"][0]["error"])
        fallback.assert_called_once_with("Analyze this subject", suppress_web=False,
                                         api_miss_fallback=True, wikipedia_fallback_allowed=True, evidence_only=True,
                                         external_search_query="Analyze this subject")

    def test_empty_api_envelope_is_not_counted_as_evidence_and_falls_back(self):
        fallback_task = app.Task("Analyze this subject")
        fallback_task.transition(app.TaskState.ROUTING)
        fallback_task.transition(app.TaskState.GENERATING)
        fallback_task.transition(app.TaskState.COMPLETED)
        empty_result = {
            "tool_id": "statistics.world_bank", "provider": "World Bank",
            "data": {"semantic_status": "EMPTY", "items": [], "has_payload": False},
            "sources": [{"title": "World Bank", "canonical_url": "https://example.org/source"}],
        }
        with patch.object(app, "_laya_select_analysis_apis", return_value=["statistics.world_bank"]), \
             patch.object(app, "_brain_plan_analysis_apis", return_value=(
                 ["statistics.world_bank"], _recipe(["statistics.world_bank"]),
             )), \
             patch.object(app, "external_tool_credential_setup", return_value=None), \
             patch.object(app, "compile_provider_arguments", return_value={"query": "analysis"}), \
             patch.object(app, "execute_external_tool", return_value=empty_result), \
             patch.object(app.runtime, "execute", return_value=(fallback_task, "Wikipedia evidence")) as fallback:
            outcome = app._run_autonomous_analysis("Analyze this subject", {})
        self.assertEqual("analysis_provider_fallback", outcome["tool"]["kind"])
        self.assertIn("Wikipedia evidence", outcome["response"])
        self.assertEqual("PROVIDER_EMPTY", fallback_task.selected_context[-1][
            "analysis_provider_failure_fallback"]["failed_tools"][0]["error"])
        fallback.assert_called_once()

    def test_partial_api_failure_uses_existing_web_fallback_alongside_api_evidence(self):
        good_result = {
            "tool_id": "statistics.world_bank", "provider": "World Bank",
            "data": {"semantic_status": "DATA", "items": [{"value": 1}]},
            "sources": [{"title": "World Bank", "canonical_url": "https://example.org/source"}],
        }
        fallback_task = app.Task("Analyze this subject")
        calls = {"statistics.world_bank": good_result, "econ.us_labor": app.ExternalToolError("PROVIDER_TIMEOUT")}
        def execute(tool_id, _arguments):
            result = calls[tool_id]
            if isinstance(result, Exception):
                raise result
            return result
        with patch.object(app, "_laya_select_analysis_apis", return_value=list(calls)), \
             patch.object(app, "_brain_plan_analysis_apis", return_value=(
                 list(calls), _recipe(list(calls)),
             )), \
             patch.object(app, "external_tool_credential_setup", return_value=None), \
             patch.object(app, "compile_provider_arguments", return_value={"query": "analysis"}), \
             patch.object(app, "execute_external_tool", side_effect=execute), \
             patch.object(app, "_compose_autonomous_analysis", return_value=("World Bank evidence", [], True)), \
             patch.object(app, "external_tool_display_blocks", return_value=[]), \
             patch.object(app.runtime, "execute", return_value=(fallback_task, "Wikipedia gap evidence")) as fallback:
            outcome = app._run_autonomous_analysis("Analyze this subject", {})
        self.assertIn("World Bank evidence", outcome["response"])
        self.assertIn("Wikipedia gap evidence", outcome["response"])
        recovery_context = next(context for context in outcome["task"].selected_context
                                if "provider_evidence_count" in context)
        self.assertEqual(1, recovery_context["provider_evidence_count"])
        self.assertTrue(recovery_context["partial_fallback_used"])
        fallback.assert_called_once_with("Analyze this subject", suppress_web=False,
                                         api_miss_fallback=True, wikipedia_fallback_allowed=True, evidence_only=True,
                                         external_search_query="Analyze this subject")


if __name__ == "__main__":
    unittest.main()
