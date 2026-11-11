"""Evidence regressions observed in the long, single-conversation GUI run."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from olcr_api.analysis_coverage import missing_coverage
from olcr_api.analysis_scope import filter_scope
from olcr_api.evidence_quality import (
    filter_provider_evidence, has_observations, recovery_query, web_source_matches,
)
from olcr_api import external_tools as tools
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.models import TaskState
from olcr_api.retrieval import RetrievalRouter, FileRetriever, FTSRetriever, DisabledVectorStore
from olcr_api.runtime import Runtime


class EvidenceQualityTests(unittest.TestCase):
    def test_subject_must_appear_in_article_content_not_only_tags(self):
        rows = [{"title": "はとがの使い分け", "tags": ["AIエージェント"]},
                {"title": "LLM agent tool execution", "body_excerpt": "An autonomous AI agent invokes tools."}]
        original = {"items": rows, "raw_metadata": {"items": rows}, "results": rows}
        before = copy.deepcopy(original)
        filtered, rejected = filter_provider_evidence(original, "AIエージェント開発記事を比較して", "knowledge.qiita")
        self.assertEqual(1, rejected)
        self.assertEqual([rows[1]], filtered["items"])
        self.assertNotIn("raw_metadata", filtered)
        self.assertNotIn("results", filtered)
        self.assertEqual(before, original)

    def test_broad_research_words_do_not_satisfy_the_current_subject(self):
        data = {"works": [{"title": "AIと質的研究の協働"},
                          {"title": "Protein research in swine production"}]}
        filtered, _ = filter_provider_evidence(data, "生物多様性とタンパク質研究をつないで", "research.openalex")
        self.assertEqual("NO_RELEVANT_RESULT", filtered["semantic_status"])
        self.assertFalse(has_observations(filtered))

    def test_research_compiler_removes_sibling_provider_tasks(self):
        args = tools.compile_provider_arguments("research.openalex", "感染症研究の論文動向と臨床試験、国別の健康指標を整理して")
        self.assertEqual("infectious disease", args["query"])
        args = tools.compile_provider_arguments("research.openalex", "免疫療法の臨床試験・FDA有害事象報告と標的タンパク質の研究情報を照合して")
        self.assertEqual("immunotherapy", args["query"])

    def test_explicit_taxa_are_preserved_and_provider_name_is_not_a_taxon(self):
        args = tools.compile_provider_arguments("biology.phylogeny", "Open TreeでHomo sapiensとPan troglodytesの系統関係を確認して")
        self.assertEqual(["Homo sapiens", "Pan troglodytes"], args["taxa"])
        with self.assertRaises(tools.ExternalToolError):
            tools.compile_provider_arguments("biology.phylogeny", "Open Treeの系統樹")

    def test_zero_observation_and_verified_negative_evidence_remain_usable(self):
        result = tools._external_result("policy.us_federal_register", "events", {"count": 0, "value": 0, "unit": "events"}, "https://example.invalid")
        self.assertTrue(result["data"]["has_payload"])
        self.assertTrue(has_observations({"items": [], "has_payload": True,
                                          "absence_description": "No matching listed entity"}))

    def test_japanese_language_is_not_confused_with_jurisdiction(self):
        request = "米国の連邦規則制定と議会審議、裁判例を照合して"
        self.assertFalse(web_source_matches(request, {"title": "日本の議院規則", "text": "議会審議と裁判例"}))
        self.assertTrue(web_source_matches(request, {"title": "米国の連邦議会と規則制定", "text": "連邦規則制定と議会審議、裁判例"}))

    def test_web_rank_is_not_evidence_of_subject_relevance(self):
        self.assertFalse(web_source_matches("気候関連法案を調べて", {"title": "AI agent tools", "text": "Artificial intelligence implementation", "rank": 1}))
        self.assertTrue(web_source_matches("気候関連法案を調べて", {"title": "Climate change legislation", "text": "Carbon emissions policy", "rank": 5}))

    def test_recovery_query_keeps_scope_but_omits_satisfied_axes(self):
        query = recovery_query("2024年の米国の気候法案と電力価格を比較して", [{"label": "電力価格"}])
        self.assertIn("US", query)
        self.assertIn("2024", query)
        self.assertIn("climate", query)
        self.assertIn("電力価格", query)
        self.assertNotIn("法案", query)

    def test_empty_envelope_or_catalog_cannot_complete_an_observation_need(self):
        data = {"items": [], "raw_metadata": {"description": "matching documents", "count": 0}}
        self.assertFalse(has_observations(data))
        result = tools._external_result("policy.us_federal_register", "climate", {"count": 0, "description": "matching climate documents"}, "https://example.invalid")
        self.assertFalse(result["data"]["has_payload"])
        catalog, _ = filter_provider_evidence({"items": [{"title": "Humanitarian data catalog"}]}, "人道ニーズを比較して", "humanitarian.hdx")
        missing = missing_coverage("人道ニーズを比較して", {"humanitarian.hdx"}, lambda x: x,
                                   [{"tool_id": "humanitarian.hdx", "data": catalog}])
        self.assertIn("humanitarian", {item["key"] for item in missing})

    def test_complete_measure_survives_a_missing_sibling_measure(self):
        data = {"request_complete": False, "missing_fields": ["inflation"], "items": [
            {"indicator": "NY.GDP.MKTP.KD.ZG", "country": "Japan", "date": "2024", "value": 1.1}]}
        missing = missing_coverage("2024年の日本の成長と物価を比較して", {"statistics.world_bank"},
            lambda x: tools.CANONICAL_ALIASES.get(x, x), [{"tool_id": "econ.worldbank", "data": data}])
        self.assertNotIn("growth", {item["key"] for item in missing})
        self.assertIn("inflation", {item["key"] for item in missing})

    def test_one_country_or_one_year_cannot_satisfy_a_comparison(self):
        data = {"items": [{"indicator": "NY.GDP.MKTP.KD.ZG", "country": "Japan", "date": "2024", "value": 1.1}]}
        for request in ("2024年の日本と韓国の成長を比較して", "日本の成長の推移を比較して"):
            missing = missing_coverage(request, {"statistics.world_bank"},
                lambda x: tools.CANONICAL_ALIASES.get(x, x), [{"tool_id": "econ.worldbank", "data": data}])
            self.assertIn("growth", {item["key"] for item in missing})

    def test_one_chamber_or_sanctions_list_cannot_satisfy_both_sides(self):
        for request, key, succeeded in (
            ("ブラジルの上下両院の審議を比較して", "parliament", "politics.br_senate"),
            ("国連と米国の制裁リストを比較して", "sanctions", "sanctions.unsc"),
        ):
            data = {"items": [{"title": "Verified source record"}]}
            missing = missing_coverage(request, {succeeded}, lambda x: x,
                                       [{"tool_id": succeeded, "data": data}])
            self.assertIn(key, {item["key"] for item in missing})

    def test_observation_time_and_region_are_filtered_without_changing_input(self):
        data = {"items": [{"time": "2020-01", "geo": "AT", "value": 2.2},
                          {"time": "2025-01", "geo": "DE", "value": 3.1}]}
        filtered, count = filter_scope(data, "EUの2025年の物価")
        self.assertEqual(1, count)
        self.assertEqual([3.1], [row["value"] for row in filtered["items"]])
        wrong, _ = filter_scope({"items": [{"region": "Africa", "value": 1}]}, "中南米の紛争")
        self.assertFalse(has_observations(wrong))

    def test_rejected_observations_cannot_survive_in_precomputed_aggregates(self):
        data = {"items": [{"country": "France", "year": 2025, "value": 3},
                          {"country": "Japan", "year": 2025, "value": 4}],
                "analysis_summary": {"total": 7}, "aggregated_row_count": 2}
        filtered, _ = filter_provider_evidence(data, "日本の2025年の紛争", "conflict.ucdp")
        self.assertEqual([4], [row["value"] for row in filtered["items"]])
        self.assertNotIn("analysis_summary", filtered)
        self.assertEqual({"total": 7}, data["analysis_summary"])

    def test_samples_do_not_establish_complete_coverage(self):
        data = {"items": [{"country": "Japan", "value": 3}], "sample_only": True}
        missing = missing_coverage("日本の紛争を分析", {"conflict.ucdp"}, lambda x: x,
                                   [{"tool_id": "conflict.ucdp", "data": data}])
        self.assertIn("conflict", {item["key"] for item in missing})
        data = {"items": [{"indicator": "NY.GDP.MKTP.KD.ZG", "country": "Japan", "date": "2025", "value": 1}], "sample_only": True}
        missing = missing_coverage("日本の成長を分析", {"statistics.world_bank"},
            lambda x: tools.CANONICAL_ALIASES.get(x, x), [{"tool_id": "econ.worldbank", "data": data}])
        self.assertIn("growth", {item["key"] for item in missing})
        args = tools.compile_provider_arguments("development.un_sdg", "各地域の水資源ストレスを比較して")
        with patch.object(tools, "_request", return_value={"totalPages": 3, "data": [
                {"geoAreaName": "Afghanistan", "timePeriodStart": 2000, "value": 1}]}):
            result = tools.execute("development.un_sdg", args)
        self.assertTrue(result["data"]["sample_only"])
        self.assertFalse(result["data"]["request_complete"])

    def test_eurostat_compiler_keeps_all_countries_and_recent_window(self):
        args = tools.compile_provider_arguments("econ.eurostat", "EU各国の消費者物価を比較して")
        self.assertEqual(27, len(args["countries"]))
        self.assertIn("FR", args["countries"])
        self.assertGreaterEqual(int(args["since"]), 2021)

    def test_unknown_entity_does_not_turn_into_an_example_country_or_protein(self):
        self.assertTrue(tools.compile_provider_arguments("econ.afdb", "アフリカの成長を比較して")["unsupported_capability"])
        self.assertTrue(tools.compile_provider_arguments("development.idb", "中南米の地域開発を比較して")["unsupported_capability"])
        for tool in ("biology.protein", "biology.occurrence"):
            with self.subTest(tool=tool), self.assertRaises(tools.ExternalToolError):
                tools.compile_provider_arguments(tool, "生物多様性の観察記録と系統関係、タンパク質研究をつないで説明して")

    def test_contract_search_submits_topic_filter_and_rejects_unrelated_purpose(self):
        args = tools.compile_provider_arguments("gov.us_spending", "米国の気候関連法案と政府契約支出を比較して")
        self.assertEqual(["climate change"], args["keywords"])
        with patch.object(tools, "_post_request", return_value={"results": []}) as post:
            tools.execute("gov.us_spending", args)
        self.assertEqual(["climate change"], post.call_args.args[2]["filters"]["keywords"])
        filtered, _ = filter_provider_evidence({"items": [{"description": "Missile production"}]},
            "気候関連の政府契約支出を比較して", "gov.us_spending")
        self.assertFalse(has_observations(filtered))

    def test_legal_metadata_query_filters_current_subject_instead_of_first_labels(self):
        args = tools.compile_provider_arguments("law.eu_cellar", "EUの環境政策と法令文書を比較して")
        with patch.object(tools, "_request", return_value={"results": {"bindings": [
                {"title": {"type": "literal", "value": "Environmental policy"},
                 "celex": {"type": "literal", "value": "32020R1234"},
                 "work": {"type": "uri", "value": "http://publications.europa.eu/resource/cellar/1234-abcd"}}]}}) as request:
            result = tools.execute("law.eu_cellar", args)
        query = request.call_args.args[2]["query"]
        self.assertIn("FILTER(CONTAINS", query)
        self.assertIn("environment", query)
        self.assertEqual(1, len(result["data"]["items"]))
        data, _ = filter_provider_evidence(result["data"], "EUの環境政策と法令文書を比較して", "law.eu_cellar")
        self.assertEqual("document_catalog", data["evidence_kind"])
        self.assertFalse(data["request_complete"])

    def test_web_enrichment_returns_current_evidence_without_a_second_model_call(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(str(Path(root) / "evidence.db")); db.initialize()
            settings = Settings(db_path=db.path, allowed_roots=(root,), main_model="mock",
                                web_mode="auto", web_provider="brave").validated()
            retrieval = RetrievalRouter(FileRetriever((root,)), FTSRetriever(db), DisabledVectorStore(), False)
            runtime = Runtime(settings, db, retrieval, None)
            candidates = [{"title": "US climate legislation", "url": "https://example.invalid/climate"}]
            with patch("olcr_api.runtime.brave_search", return_value=candidates), \
                 patch("olcr_api.runtime.provider_key", return_value="configured"), \
                 patch("olcr_api.runtime.web_fetch", return_value={"text": "Climate change legislation and carbon emissions.", "final_url": candidates[0]["url"]}), \
                 patch.object(runtime, "_generate_brain", side_effect=AssertionError("duplicate composition")):
                task, answer = runtime.execute("米国の気候法案を比較して", api_miss_fallback=True, evidence_only=True)
            self.assertEqual(TaskState.COMPLETED, task.state)
            self.assertIn("Climate", answer)
            self.assertEqual([], task.model_calls)


import test_autonomous_api_analysis as support
import olcr_api.app as app
from olcr_api.models import Task


class AnalysisRecoveryTests(unittest.TestCase):
    setUp = support.AutonomousApiAnalysisTests.setUp
    tearDown = support.AutonomousApiAnalysisTests.tearDown

    def test_policy_renderers_cannot_relabel_unrelated_documents_as_ai(self):
        for tool in ("politics.us_congress", "politics.eu_parliament", "politics.uk_parliament",
                     "government.us_federal_register", "policy.us_rulemaking"):
            with self.subTest(tool=tool):
                response = app.external_tool_summary({"tool_id": tool, "provider": tool,
                    "data": {"items": [{"title": "Environmental monitoring rule"}]}, "sources": []})
                self.assertIn("Environmental monitoring rule", response)
                self.assertNotIn("AI", response)
                self.assertNotIn("人工知能", response)

    def test_exhausted_model_chain_is_not_restarted_by_the_optional_planner(self):
        with patch.object(app.runtime, "_generate_brain", side_effect=app.ModelFailure("timeout", "chain exhausted")) as generate:
            ids, recipe = app._brain_plan_analysis_apis("米国の成長を分析", ["econ.worldbank"])
        self.assertEqual(1, generate.call_count)
        self.assertEqual(["econ.worldbank"], ids)
        self.assertIn("planner_fallback", recipe)

    def test_malformed_provider_does_not_discard_successful_sibling(self):
        ids = ["econ.worldbank", "trade.un_comtrade"]
        task = Task("recovery"); task.transition(TaskState.ROUTING)
        task.transition(TaskState.SEARCHING); task.transition(TaskState.COMPLETED)
        result = {"tool_id": ids[1], "data": {"items": [{"value": 1}]},
                  "sources": [{"canonical_url": "https://example.invalid/trade"}]}
        with patch.object(app, "_laya_select_analysis_apis", return_value=ids), \
             patch.object(app, "_brain_plan_analysis_apis", return_value=(ids, support._recipe(ids))), \
             patch.object(app, "external_tool_credential_setup", return_value=None), \
             patch.object(app, "compile_provider_arguments", return_value={"query": "current"}), \
             patch.object(app, "execute_external_tool", side_effect=[ValueError("bad record"), result]) as execute, \
             patch.object(app, "_compose_autonomous_analysis", return_value=("Trade evidence", [], False)), \
             patch.object(app.runtime, "execute", return_value=(task, "")), \
             patch.object(app, "external_tool_display_blocks", return_value=[]):
            outcome = app._run_autonomous_analysis("所得と貿易を比較して", {})
        self.assertEqual(2, execute.call_count)
        self.assertIn("Trade evidence", outcome["response"])
        self.assertIn("PROVIDER_BAD_RESPONSE", outcome["response"])

    def test_exhausted_evidence_uses_one_caveated_inference_answer(self):
        task = Task("empty"); task.transition(TaskState.ROUTING)
        task.transition(TaskState.SEARCHING); task.transition(TaskState.COMPLETED)
        with patch.object(app, "_laya_select_analysis_apis", return_value=["econ.worldbank"]), \
             patch.object(app, "_brain_plan_analysis_apis", return_value=(["econ.worldbank"], support._recipe(["econ.worldbank"]))), \
             patch.object(app, "external_tool_credential_setup", return_value=None), \
             patch.object(app, "compile_provider_arguments", return_value={"query": "current"}), \
             patch.object(app, "execute_external_tool", side_effect=tools.ExternalToolError("PROVIDER_TIMEOUT")), \
             patch.object(app.runtime, "execute", return_value=(task, "")), \
             patch.object(app.runtime, "infer_without_sources", return_value=(task, "外部情報未確認の一般的な説明")) as infer:
            outcome = app._run_autonomous_analysis("成長を分析して", {})
        infer.assert_called_once_with("成長を分析して", failure_reason="NO_RELEVANT_API_OR_WEB_EVIDENCE",
                                      evidence_requirements=app.evidence_needs("成長を分析して"))
        self.assertIn("外部情報未確認", outcome["response"])

    def test_analysis_synthesis_packet_obeys_the_selected_model_budget(self):
        result = {"tool_id": "policy.us_federal_register", "provider": "Federal Register",
                  "data": {"items": [{"title": "Climate rule", "description": "climate " * 3000}],
                           "raw_metadata": {"duplicate": "climate " * 3000}},
                  "sources": [{"title": "Federal Register", "canonical_url": "https://example.invalid/rule"}]}
        with patch.object(app.runtime, "context_budget", return_value=1500), \
             patch.object(app.runtime, "_generate_brain", return_value={"text": "Climate evidence [S1]"}) as generate:
            app._compose_autonomous_analysis("気候法案を比較して", [result])
        content = generate.call_args.args[0][-1]["content"]
        packet = content.split("PROVIDER_EVIDENCE_JSON:\n", 1)[1]
        self.assertLessEqual(len(packet), 1500)
        self.assertNotIn("raw_metadata", packet)
        json.loads(packet)
        self.assertFalse(generate.call_args.kwargs["thinking_override"])
