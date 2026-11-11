import unittest
import json
from unittest.mock import patch

import olcr_api.app as app


class RouterTests(unittest.TestCase):
    def setUp(self):
        # Mocked embedding fixtures must not inherit descriptor vectors cached
        # by a preceding test that used a different mock implementation.
        app._API_ROUTER_EMBEDDING_CACHE.clear()

    def test_embedding_digest_failure_abstains_to_existing_knowledge_policy(self):
        from types import SimpleNamespace
        from olcr_api.semantic import EmbeddingFailure
        with patch.object(app, 'vectors', SimpleNamespace(state='ready')), \
             patch.object(app._api_embedding_provider, 'embed', side_effect=EmbeddingFailure('digest_mismatch', 'mismatch')) as embedding, \
             patch.object(app.laya_provider.local, 'loaded', False):
            decision = app.router_decision('Ukraine conflict casualties in 2025?')
        embedding.assert_called()
        self.assertIsNone(decision)
        self.assertEqual('DEGRADED_EmbeddingFailure_digest_mismatch', app._API_ROUTING_TRACE.get()['embedding_status'])

    def test_general_research_never_enters_closed_set_specialist_selection(self):
        # Include unrelated products, people, concepts and English requests.
        for message in ("ママレモンについて調べて", "任天堂について詳しく教えて", "猫について説明してください",
                        "シルクロードについて調べてください", "量子力学について解説して",
                        "Tell me about dishwashing liquid", "Research Nintendo", "ママレモンを調べて",
                        "ママレモンとは？", "ママレモンって何？", "What is dish soap?", "Who is Vaundy?"):
            with self.subTest(message=message), \
                 patch.object(app, 'route_external_tool', return_value=None), \
                 patch.object(app._api_embedding_provider, 'embed') as embed, \
                 patch.object(app.laya_provider, 'choice') as choose:
                self.assertIsNone(app.router_decision(message))
                self.assertEqual('GENERAL_RESEARCH', app._API_ROUTING_TRACE.get()['selection_guard'])
                embed.assert_not_called()
                choose.assert_not_called()

    def test_wrong_model_selection_cannot_demand_unrequested_specialist_fields(self):
        from types import SimpleNamespace
        from olcr_api.external_tools import ExternalToolError
        with patch.object(app, 'vectors', SimpleNamespace(state='ready')), \
             patch.object(app, 'route_external_tool', return_value=None), \
             patch.object(app, '_api_embedding_provider') as embedding, \
             patch.object(app.laya_provider.local, 'loaded', True), \
             patch.object(app.laya_provider, 'choice', return_value={'tool_id':'chemistry.pubchem','confidence':0.99}), \
             patch.object(app, 'compile_provider_arguments', side_effect=ExternalToolError('ARGUMENT_COMPILATION_FAILED')):
            embedding.embed.side_effect = lambda texts, model: ([[1.,0.]] if len(texts)==1 else
                [[1.,0.] if 'chemistry pubchem' in t else [0.,1.] for t in texts])
            self.assertIsNone(app.router_decision('ママレモンを詳しく調査して'))
        trace = app._API_ROUTING_TRACE.get()
        self.assertEqual('UNGROUNDED_API_SELECTION', trace['selection_guard'])
        self.assertFalse(trace['clarification_required'])
        self.assertEqual('KNOWLEDGE_POLICY', trace['fallback'])

    def test_semantic_router_finds_api_for_topic_paraphrase_without_api_name(self):
        class FakeVectors:
            state = "ready"

        prompt = "Ukraine conflict casualties in 2025?"

        self.assertTrue(app._external_router_candidate(prompt))

        def embed(texts, _model):
            if len(texts) == 1:
                return [[1.0, 0.0]]
            return [[1.0, 0.0] if "conflict ucdp" in text else [0.0, 1.0] for text in texts]

        with patch.object(app, "vectors", FakeVectors()), \
             patch.object(app, "_api_embedding_provider") as embedding, \
             patch.object(app.laya_provider.local, "loaded", False):
            embedding.embed.side_effect = embed
            decision = app.router_decision(prompt)

        self.assertIsNotNone(decision)
        self.assertEqual("conflict.ucdp", decision[0])
        self.assertEqual("369", decision[1]["country_id"])
        self.assertEqual("2025-01-01", decision[1]["start_date"])
        self.assertEqual("2025-12-31", decision[1]["end_date"])

    def test_weak_semantic_neighbor_is_not_executed_when_laya_is_unavailable(self):
        class FakeVectors:
            state = "ready"

        prompt = "地震のこと、東京近辺で最近どれくらいあった？"
        with patch.object(app, "vectors", FakeVectors()), \
             patch.object(app, "_api_embedding_provider") as embedding, \
             patch.object(app.laya_provider.local, "loaded", False):
            embedding.embed.side_effect = lambda texts, _model: ([[1.0, 0.0]] if len(texts) == 1
                                                                  else [[0.0, 1.0] for _ in texts])
            decision = app.router_decision(prompt)

        self.assertIsNone(decision)
        self.assertIsNone(app._API_ROUTING_TRACE.get()["selected"])

    def test_laya_no_match_does_not_promote_nearest_embedding_neighbor(self):
        class FakeVectors:
            state = "ready"

        prompt = "シルクロードの歴史を要約して、主な時代と交易への影響を教えて。"

        def embed(texts, _model):
            if len(texts) == 1:
                return [[1.0, 0.0]]
            return [[0.5, 0.866025] if "humanitarian unhcr" in text else [0.0, 1.0] for text in texts]

        choice_candidates = []

        def choose(*, question, language, candidates):
            choice_candidates.extend(candidates)
            return {"tool_id": "__no_matching_api__", "confidence": 0.0}

        with patch.object(app, "vectors", FakeVectors()), \
             patch.object(app, "_api_embedding_provider") as embedding, \
             patch.object(app.laya_provider.local, "loaded", True), \
             patch.object(app.laya_provider, "choice", side_effect=choose):
            embedding.embed.side_effect = embed
            decision = app.router_decision(prompt)

        self.assertIsNone(decision)
        self.assertLessEqual(len(choice_candidates), 8)
        self.assertEqual("__no_matching_api__", choice_candidates[-1]["tool_id"])
        trace = app._API_ROUTING_TRACE.get()
        self.assertTrue(trace["laya_called"])
        self.assertIn("humanitarian.unhcr", trace["candidate_ids"])
        self.assertIsNone(trace["selected"])
        self.assertEqual("LAYA_NO_MATCH", trace["selection_guard"])

    def test_laya_failure_does_not_execute_a_weak_embedding_neighbor(self):
        class FakeVectors:
            state = "ready"

        prompt = "シルクロードの歴史を要約して、主な時代と交易への影響を教えて。"

        def embed(texts, _model):
            return [[1.0, 0.0]] if len(texts) == 1 else [[0.1, 0.994987] for _text in texts]

        with patch.object(app, "vectors", FakeVectors()), \
             patch.object(app, "_api_embedding_provider") as embedding, \
             patch.object(app.laya_provider.local, "loaded", True), \
             patch.object(app.laya_provider, "choice", side_effect=app.LayaRuntimeError("LAYA_RUNTIME_FAILURE", "test failure")):
            embedding.embed.side_effect = embed
            decision = app.router_decision(prompt)

        self.assertIsNone(decision)
        trace = app._API_ROUTING_TRACE.get()
        self.assertIsNone(trace["selected"])
        self.assertEqual("EMBEDDING_BELOW_FLOOR", trace["selection_guard"])

    def test_laya_explicit_low_confidence_choice_abstains(self):
        class FakeVectors:
            state = "ready"

        prompt = "Ukraine conflict casualties in 2025?"

        def embed(texts, _model):
            if len(texts) == 1:
                return [[1.0, 0.0]]
            return [[0.5, 0.866025] if "conflict ucdp" in text else [0.0, 1.0] for text in texts]

        with patch.object(app, "vectors", FakeVectors()), \
             patch.object(app, "_api_embedding_provider") as embedding, \
             patch.object(app.laya_provider.local, "loaded", True), \
             patch.object(app.laya_provider, "choice", return_value={
                 "tool_id": "conflict.ucdp", "confidence": 0.1377,
             }):
            embedding.embed.side_effect = embed
            decision = app.router_decision(prompt)

        self.assertIsNone(decision)
        trace = app._API_ROUTING_TRACE.get()
        self.assertEqual("conflict.ucdp", trace["laya_selected"])
        self.assertIsNone(trace["selected"])
        self.assertEqual("LAYA_LOW_CONFIDENCE", trace["selection_guard"])

    def test_laya_cannot_promote_a_weak_semantic_neighbor_for_uncovered_domain(self):
        class FakeVectors:
            state = "ready"

        prompt = "日本の再生可能エネルギーの普及状況をブリーフィングして。最近の進展と課題を、関連する公開データを複数探して突き合わせて。"

        def embed(texts, _model):
            return [[1.0, 0.0]] if len(texts) == 1 else [[0.1, 0.994987] for _text in texts]

        def choose(*, candidates, **_kwargs):
            return {"tool_id": candidates[0]["tool_id"], "confidence": 0.62}

        with patch.object(app, "vectors", FakeVectors()), \
             patch.object(app, "_api_embedding_provider") as embedding, \
             patch.object(app.laya_provider.local, "loaded", True), \
             patch.object(app.laya_provider, "choice", side_effect=choose):
            embedding.embed.side_effect = embed
            decision = app.router_decision(prompt)

        self.assertIsNone(decision)
        trace = app._API_ROUTING_TRACE.get()
        self.assertIsNone(trace["selected"])
        self.assertEqual("EMBEDDING_BELOW_FLOOR", trace["selection_guard"])

    def test_public_holidays_render_each_item_date_not_envelope_date(self):
        result = {
            "tool_id": "calendar.public_holidays", "provider": "Nager.Date",
            "data": {
                "date": "2026-01-01",
                "items": [
                    {"date": "2026-01-01", "localName": "元日"},
                    {"date": "2026-01-12", "localName": "成人の日"},
                ],
            },
        }

        rendered = app.external_tool_summary(result)

        self.assertIn("2026-01-01：元日", rendered)
        self.assertIn("2026-01-12：成人の日", rendered)
        self.assertNotIn("2026-01-01：成人の日", rendered)

    def test_open_library_summary_preserves_authors_and_bibliographic_fields(self):
        result = {
            "tool_id": "books.openlibrary", "provider": "Open Library",
            "data": {"items": [{
                "title": "Fantastic Mr Fox", "authors": ["Roald Dahl"],
                "publish_year": 1970, "isbn": ["9780140328721"],
            }]},
        }
        rendered = app.external_tool_summary(result)
        self.assertIn("Fantastic Mr Fox", rendered)
        self.assertIn("著者：Roald Dahl", rendered)
        self.assertIn("初版年：1970", rendered)
        self.assertIn("ISBN：9780140328721", rendered)

    def test_external_api_task_persists_provider_execution_evidence(self):
        routing_evidence = {
            "retrieval": "embedding", "embedding_status": "PASS",
            "candidate_ids": ["books.openlibrary"], "laya_called": True,
            "laya_selected": "books.openlibrary", "selected": "books.openlibrary",
            "selection_guard": None,
        }
        result = {
            "tool_id": "books.openlibrary", "provider": "Open Library",
            "response_kind": "record",
            "routing_evidence": routing_evidence,
            "sources": [{"provider": "Open Library", "url": "https://openlibrary.org/"}],
            "data": {"semantic_status": "DATA", "has_payload": True,
                     "item_count": 1, "items": [{"title": "Fantastic Mr Fox"}]},
        }
        task, response = app.compose_external_result("look up a book", result)
        self.assertEqual(app.Route.EXTERNAL_API, task.route)
        self.assertEqual(app.TaskState.COMPLETED, task.state)
        self.assertEqual([{"api_routing": routing_evidence}], task.selected_context)
        self.assertIn("Fantastic Mr Fox", response)
        self.assertEqual([{
            "tool": "external_api", "version": "external_api_v1", "risk": "SAFE",
            "input": {"tool_id": "books.openlibrary", "provider": "Open Library"},
            "output": {"response_kind": "record", "source_count": 1,
                       "semantic_status": "DATA", "item_count": 1},
            "status": "success", "error": None,
        }], task.tool_executions)
        app.db.save_task(task)
        with app.db.connect() as conn:
            stored_task = conn.execute("SELECT route,selected_context_json FROM tasks WHERE id=?", (task.id,)).fetchone()
            stored_execution = conn.execute(
                "SELECT tool_name,status,input_json,output_json FROM tool_executions WHERE task_id=?",
                (task.id,),
            ).fetchone()
        self.assertEqual("EXTERNAL_API", stored_task["route"])
        self.assertEqual([{"api_routing": routing_evidence}], json.loads(stored_task["selected_context_json"]))
        self.assertEqual("external_api", stored_execution["tool_name"])
        self.assertEqual("success", stored_execution["status"])

    def test_external_api_failure_persists_route_selection_and_provider_error(self):
        routing_evidence = {
            "retrieval": "embedding", "embedding_status": "PASS",
            "candidate_ids": ["news.global_search"], "laya_called": True,
            "laya_selected": "news.global_search", "selected": "news.global_search",
        }
        task = app.Task("最新の海外報道で、日本の半導体政策について調べてください。")
        task.transition(app.TaskState.ROUTING)
        task.error = "PROVIDER_RATE_LIMITED"
        task.transition(app.TaskState.FAILED)
        app.record_external_api_execution(
            task, "news.global_search", "GDELT", routing_evidence,
            "failed", task.error,
        )
        app.db.save_task(task)

        with app.db.connect() as conn:
            stored_task = conn.execute(
                "SELECT route,error,selected_context_json FROM tasks WHERE id=?", (task.id,)
            ).fetchone()
            stored_execution = conn.execute(
                "SELECT tool_name,status,input_json,error FROM tool_executions WHERE task_id=?",
                (task.id,),
            ).fetchone()
        self.assertEqual("EXTERNAL_API", stored_task["route"])
        self.assertEqual("PROVIDER_RATE_LIMITED", stored_task["error"])
        self.assertEqual([{"api_routing": routing_evidence}], json.loads(stored_task["selected_context_json"]))
        self.assertEqual("external_api", stored_execution["tool_name"])
        self.assertEqual("failed", stored_execution["status"])
        self.assertEqual("news.global_search", json.loads(stored_execution["input_json"])["tool_id"])
        self.assertEqual("PROVIDER_RATE_LIMITED", stored_execution["error"])

    def test_laya_choice_with_weak_prompt_alignment_is_guarded(self):
        class FakeVectors:
            state = "ready"

        def embed(texts, model):
            if len(texts) == 1:
                return [[1.0, 0.0]]
            return [[1.0, 0.0] if "holiday" in text.casefold() else [0.0, 1.0] for text in texts]

        prompt = "最新の海外報道で、日本の半導体政策について調べてください。要点を3件。"
        with patch.object(app, "vectors", FakeVectors()), \
             patch.object(app, "_api_embedding_provider") as embedding, \
             patch.object(app.laya_provider.local, "loaded", True), \
             patch.object(app.laya_provider, "choice", return_value={"tool_id": "calendar.public_holidays", "confidence": 0.93}):
            embedding.embed.side_effect = embed
            decision = app.router_decision(prompt)

        self.assertIsNone(decision)
        trace = app._API_ROUTING_TRACE.get()
        self.assertEqual("PASS", trace["embedding_status"])
        self.assertTrue(trace["laya_called"])
        self.assertEqual("calendar.public_holidays", trace["laya_selected"])
        self.assertIsNone(trace["selected"])
        self.assertEqual("BRAVE_SEARCH", trace["fallback"])

    def test_api_candidates_exclude_explicitly_conflicting_country(self):
        class FakeVectors:
            state = "ready"

        prompt = "日本の最新の失業率を信頼できるデータソースから示してください"
        selected_candidates = []

        def embed(texts, model):
            if len(texts) == 1:
                return [[1.0, 0.0]]
            return [[1.0, 0.0] if "econ oecd" in text else [0.5, 0.5] for text in texts]

        def choose(*, question, language, candidates):
            selected_candidates.extend(item["tool_id"] for item in candidates)
            self.assertIn("econ.oecd", selected_candidates)
            self.assertNotIn("econ.us_labor", selected_candidates)
            return {"tool_id": "econ.oecd", "confidence": 0.8}

        with patch.object(app, "vectors", FakeVectors()), \
             patch.object(app, "_api_embedding_provider") as embedding, \
             patch.object(app.laya_provider.local, "loaded", True), \
             patch.object(app.laya_provider, "choice", side_effect=choose):
            embedding.embed.side_effect = embed
            decision = app.router_decision(prompt)

        self.assertEqual("econ.oecd", decision[0])
        self.assertIn("econ.oecd", selected_candidates)
        self.assertNotIn("econ.us_labor", selected_candidates)

    def test_unqualified_japan_gdp_uses_embedding_and_laya_then_compiles_jp(self):
        class FakeVectors:
            state = "ready"

        prompt = "Show Japan's GDP year by year from 2015 through the latest available year."
        with patch.object(app, "vectors", FakeVectors()), \
             patch.object(app, "_api_embedding_provider") as embedding, \
             patch.object(app.laya_provider.local, "loaded", True), \
             patch.object(app.laya_provider, "choice", return_value={"tool_id": "econ.worldbank", "confidence": 0.9}):
            embedding.embed.side_effect = lambda texts, _model: (
                [[1.0, 0.0]] if len(texts) == 1 else
                [[1.0, 0.0] if "econ worldbank" in text else [0.0, 1.0] for text in texts]
            )
            decision = app.router_decision(prompt)

        self.assertEqual("econ.worldbank", decision[0])
        trace = app._API_ROUTING_TRACE.get()
        self.assertEqual("PASS", trace["embedding_status"])
        self.assertTrue(trace["laya_called"])
        self.assertEqual("econ.worldbank", trace["laya_selected"])
        self.assertEqual("JP", decision[1]["country"])

    def test_canonical_j_gdp_prompt_avoids_ocean_route_and_reaches_world_bank_choice(self):
        class FakeVectors:
            state = "ready"

        prompt = (
            "This is not a coding task. Do not modify files. What is Japan's latest reported annual GDP "
            "in current US dollars? Return the value, year, and source name. Use an authoritative dataset. "
            "Do not guess if a verified source is unavailable."
        )
        selected_candidates = []

        def choose(*, question, language, candidates):
            selected_candidates.extend(item["tool_id"] for item in candidates)
            self.assertIn("econ.worldbank", selected_candidates)
            self.assertNotIn("ocean.tides", selected_candidates)
            return {"tool_id": "econ.worldbank", "confidence": 0.9}

        self.assertIsNone(app.route_external_tool(prompt))
        self.assertTrue(app._external_router_candidate(prompt))
        with patch.object(app, "vectors", FakeVectors()), \
             patch.object(app, "_api_embedding_provider") as embedding, \
             patch.object(app.laya_provider.local, "loaded", True), \
             patch.object(app.laya_provider, "choice", side_effect=choose):
            embedding.embed.side_effect = lambda texts, _model: (
                [[1.0, 0.0]] if len(texts) == 1 else
                [[1.0, 0.0] if "econ worldbank" in text else [0.0, 1.0] for text in texts]
            )
            decision = app.router_decision(prompt)

        self.assertEqual("econ.worldbank", decision[0])
        self.assertEqual("JP", decision[1]["country"])
        trace = app._API_ROUTING_TRACE.get()
        self.assertEqual("PASS", trace["embedding_status"])
        self.assertTrue(trace["laya_called"])
        self.assertEqual("econ.worldbank", trace["selected"])

    def test_typed_data_always_becomes_a_terminal_user_response(self):
        cases = [
            ("earth.earthquakes", "USGS", "event_list", {"title": "M 4.2", "place": "Tokyo", "magnitude": 4.2}),
            ("earth.disasters", "NASA EONET", "event_list", {"title": "Wildfire", "location": "California", "category": "Wildfires"}),
            ("astronomy.sunrise", "Sunrise-Sunset.org", "status", {"results": {"sunrise": "06:01", "sunset": "18:10"}}),
            ("food.product", "Open Food Facts", "record", {"product_name": "Chocolate", "brands": "Brand", "code": "123"}),
        ]
        for tool_id, provider, response_kind, payload in cases:
            data = {**payload, "semantic_status": "DATA", "has_payload": True}
            result = {"tool_id": tool_id, "provider": provider, "response_kind": response_kind,
                      "fetched_at": "2026-09-22T00:00:00Z", "data": data,
                      "sources": [{"provider": provider, "url": "https://example.com"}]}
            with patch.object(app.runtime, "compose_tool_result", side_effect=AssertionError("DATA must not fall through to Brain")):
                task, response = app.compose_external_result("現在のデータを確認", result)
            self.assertEqual(app.TaskState.COMPLETED, task.state, tool_id)
            self.assertTrue(response.strip(), tool_id)
            self.assertNotIn("取得できません", response, tool_id)

    def test_current_provider_summaries_preserve_world_bank_entities_and_event_data(self):
        world_bank = {
            "tool_id": "statistics.world_bank", "provider": "World Bank",
            "data": {"indicator": "NY.GDP.MKTP.CD", "items": [
                {"country": {"value": "Japan"}, "date": "2023", "value": 4200},
                {"country": {"value": "Germany"}, "date": "2023", "value": 4500},
            ]},
        }
        summary = app.external_tool_summary(world_bank)
        self.assertIn("Japan", summary)
        self.assertIn("Germany", summary)
        self.assertIn("provider native units (indicator labels specify units where available)", summary)
        self.assertIn("NY.GDP.MKTP.CD", summary)

        table_data = {"indicator": "NY.GDP.MKTP.CD", "items": [
            {"country": {"value": country}, "date": str(year), "value": year * 1000}
            for country in ("Japan", "Germany") for year in range(2015, 2024)
        ], "request_complete": True}
        table = app.external_tool_summary({"tool_id": "statistics.world_bank", "provider": "World Bank", "data": table_data})
        self.assertTrue(all(str(year) in table for year in range(2015, 2024)))
        self.assertEqual(9, sum(1 for line in table.splitlines() if line[:4].isdigit()))
        self.assertIn("Year | Japan | Germany", table)
        self.assertTrue(table_data["render_complete"])

        block = app.external_tool_display_blocks({"tool_id": "statistics.world_bank", "provider": "World Bank", "data": table_data})[0]
        self.assertEqual("table", block["type"])
        self.assertEqual(3, len(block["columns"]))
        self.assertEqual(9, len(block["rows"]))
        self.assertEqual({"year", "country_0", "country_1"}, set(block["rows"][0]))
        self.assertEqual("right", block["columns"][0]["align"])
        self.assertEqual("World Bank · 指標ID NY.GDP.MKTP.CD", block["source_label"])
        partial_block = app.external_tool_display_blocks({"tool_id": "statistics.world_bank", "data": {"items": [{"country": "Japan", "date": "2023", "value": None}]}})[0]
        self.assertIsNone(partial_block["rows"][0]["country_0"])

        eonet = {
            "tool_id": "earth.natural_event", "provider": "EONET",
            "sources": [{"provider": "NASA EONET"}],
            "data": {"items": [
                {"title": "Test wildfire", "location": "Broward, Florida", "category": {"title": "Wildfires"}, "date": "2026-09-10"},
                {"title": "Second event", "location": None, "category": None},
            ]},
        }
        rendered = app.external_tool_summary(eonet)
        self.assertIn("EONETに登録されている自然災害の例です。", rendered)
        self.assertIn("1. Test wildfire", rendered)
        self.assertIn("場所：Broward, Florida", rendered)
        self.assertIn("種別：Wildfires", rendered)
        self.assertIn("2. Second event", rendered)
        self.assertIn("出典：NASA EONET", rendered)
        self.assertNotIn("None", rendered)

    def test_current_provider_results_bypass_brain_composition(self):
        result = {
            "tool_id": "earth.natural_event", "provider": "EONET",
            "sources": [{"provider": "EONET"}],
            "data": {"items": [{"title": "Current wildfire"}]},
        }
        with patch.object(app.runtime, "compose_tool_result", side_effect=AssertionError("Brain must not compose current event data")):
            _, response = app.compose_external_result("現在の自然災害", result)
        self.assertIn("Current wildfire", response)

    def test_federal_register_renderer_has_data_and_empty_states(self):
        data_result = {
            "tool_id": "government.us_federal_register", "provider": "Federal Register",
            "sources": [{"provider": "Federal Register"}],
            "data": {"items": [
                {"title": "AI rule", "document_type": "Rule", "publication_date": "2026-09-10", "agency": ["Example agency"], "url": "https://example.gov/doc"},
                {"title": "AI notice", "document_type": "Notice", "publication_date": "2026-09-09"},
            ]},
        }
        rendered = app.external_tool_summary(data_result)
        self.assertIn("Federal Registerで検索条件に一致する文書が見つかりました。", rendered)
        self.assertIn("1. AI rule", rendered)
        self.assertIn("2. AI notice", rendered)
        self.assertIn("種別：Rule", rendered)
        self.assertIn("公開日：2026-09-10", rendered)
        self.assertIn("機関：Example agency", rendered)
        self.assertIn("出典：Federal Register", rendered)
        self.assertNotEqual("Federal Register", rendered)
        empty = dict(data_result, data={"items": []})
        empty_text = app.external_tool_summary(empty)
        self.assertIn("今回のFederal Register検索では", empty_text)
        self.assertNotEqual("Federal Register", empty_text)

    def test_geo_success_uses_deterministic_typed_renderer(self):
        result = {
            "tool_id": "geo.poi_search", "provider": "Overpass", "fetched_at": "now",
            "sources": [{"provider": "Overpass"}],
            "data": {"items": [{"type": "node", "id": 123,
                                 "tags": {"name": "Tokyo Station", "amenity": "hospital"}}],
                     "semantic_status": "DATA"},
        }
        with patch.object(app.runtime, "compose_tool_result",
                          side_effect=AssertionError("Geo results must use the typed renderer")):
            _, response = app.compose_external_result("駅を探して", result)
        self.assertIn("Tokyo Station", response)

    def test_empty_geo_result_uses_deterministic_japanese_renderer(self):
        result = {
            "tool_id": "geo.poi_search", "provider": "Overpass", "fetched_at": "now",
            "sources": [{"provider": "Overpass"}],
            "data": {"items": [], "semantic_status": "EMPTY"},
        }
        with patch.object(app.runtime, "compose_tool_result",
                          side_effect=AssertionError("empty Geo results must use the typed renderer")):
            task, response = app.compose_external_result("東京駅から1km以内の病院を5件探して", result)
        self.assertEqual("completed", task.state.value)
        self.assertEqual("OpenStreetMapで指定範囲内の施設は見つかりませんでした。", response)
        self.assertNotIn("标签", response)
        self.assertNotIn("诊疗所", response)

    def test_osrm_summary_is_natural_japanese_with_presentation_only_conversion(self):
        result = {
            "tool_id": "geo.routing", "provider": "OSRM",
            "data": {"origin": "東京駅", "destination": "東京タワー", "profile": "driving",
                     "items": [{"distance_m": 3913.7, "duration_s": 349.5}]},
        }
        response = app.external_tool_summary(result)
        self.assertIn("東京駅 → 東京タワー：車で3.914 km、推定5.8分。", response)
        self.assertIn("OSRM", response)
        self.assertNotIn("Profile:", response)
        self.assertNotIn("Distance:", response)
        self.assertNotIn("Estimated duration:", response)

        long_result = {"tool_id": "geo.routing", "provider": "OSRM", "data": {"origin": "A", "destination": "B", "profile": "driving", "items": [{"distance_m": 500, "duration_s": 3720}]}}
        self.assertRegex(app.external_tool_summary(long_result), r"0\.50* km")
        self.assertIn("62.0分", app.external_tool_summary(long_result))

    def test_sympy_summary_is_natural_japanese_and_attributes_only_with_evidence(self):
        result = {
            "tool_id": "math.symbolic", "provider": "SymPy",
            "sources": [{"provider": "SymPy"}],
            "data": {"operation": "factor", "expression": "x**4 - 1", "result": "(x - 1)*(x + 1)*(x**2 + 1)"},
        }
        response = app.external_tool_summary(result)
        self.assertIn("$x^{4} - 1$ の因数分解結果は、", response)
        self.assertIn("(x - 1)\\,(x + 1)\\,(x^{2} + 1)", response)
        self.assertIn("$$", response)
        self.assertNotIn("⁴", response)
        self.assertIn("使用ツール：SymPy", response)
        self.assertNotIn("factor:", response)
        self.assertNotIn("Profile:", response)

        brain_only = dict(result)
        brain_only["provider"] = "Brain"
        brain_only["sources"] = []
        self.assertNotIn("使用ツール：SymPy", app.external_tool_summary(brain_only))

    def test_sympy_compose_path_uses_deterministic_renderer_without_brain(self):
        result = {
            "tool_id": "math.symbolic", "provider": "SymPy",
            "sources": [{"provider": "SymPy"}],
            "data": {"operation": "factor", "expression": "x^4 - 1", "result": "(x - 1)*(x + 1)*(x^2 + 1)"},
        }
        with patch.object(app.runtime, "compose_tool_result", side_effect=AssertionError("Brain must not rewrite SymPy output")):
            _, response = app.compose_external_result("x^4 - 1 を因数分解してください。可能なら記号計算ツールを使ってください。", result)
        self.assertIn("$x^{4} - 1$ の因数分解結果は、", response)
        self.assertNotIn("from sympy import", response)

    def test_sympy_renderer_emits_latex_delimiters_for_frontend(self):
        result = {
            "tool_id": "math.symbolic", "provider": "SymPy",
            "sources": [{"provider": "SymPy"}],
            "data": {"operation": "factor", "expression": "$$x^4 - 1$$", "result": r"\(x^2 + 1\)"},
        }
        response = app.external_tool_summary(result)
        self.assertIn("$$", response)
        self.assertIn("$x^{4} - 1$", response)
        self.assertIn("x^{2} + 1", response)

    def test_currency_renderer_uses_provider_amount_without_brain_replacement(self):
        result = {
            "tool_id": "currency.frankfurter", "provider": "Frankfurter",
            "fetched_at": "2026-09-18T00:00:00+00:00",
            "sources": [{"provider": "Frankfurter"}],
            "data": {"base": "USD", "quote": "EUR", "amount": "100", "rate": "0.8721",
                     "converted_amount": "87.21", "rate_date": "2026-09-18"},
        }
        with patch.object(app.runtime, "compose_tool_result", side_effect=AssertionError("currency must use deterministic rendering")):
            _, response = app.compose_external_result("100ドルはユーロでいくら？", result)
        self.assertIn("87.21", response)
        self.assertNotIn("92.50", response)

    def test_currency_renderer_rejects_inconsistent_provider_amount(self):
        result = {
            "tool_id": "currency.frankfurter", "provider": "Frankfurter",
            "fetched_at": "2026-09-18T00:00:00+00:00",
            "sources": [{"provider": "Frankfurter"}],
            "data": {"base": "USD", "quote": "EUR", "amount": "100", "rate": "0.8721",
                     "converted_amount": "92.50", "rate_date": "2026-09-18"},
        }
        _, response = app.compose_external_result("100ドルはユーロでいくら？", result)
        self.assertIn("整合性のある通貨換算結果", response)
        self.assertNotIn("92.50", response)

    def test_known_currency_decision_is_allowlisted(self):
        decision = app.route_external_tool("100 USD in JPY")
        self.assertEqual("currency.frankfurter", decision[0])

    def test_unresolved_router_input_does_not_guess(self):
        self.assertIsNone(app.router_decision("today's weather"))

    def test_embedding_model_is_rejected(self):
        with self.assertRaises(ValueError): app.settings.with_overrides({"router_model":"embeddinggemma:latest"})
