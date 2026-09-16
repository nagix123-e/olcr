import unittest
from unittest.mock import patch

import olcr_api.app as app


class FakeRouter:
    def __init__(self, outputs): self.outputs=list(outputs); self.calls=0
    def generate(self, *args, **kwargs):
        self.calls += 1
        return {"text": self.outputs.pop(0)}


class RouterTests(unittest.TestCase):
    def test_current_provider_summaries_preserve_world_bank_entities_and_event_data(self):
        world_bank = {
            "tool_id": "statistics.world_bank", "provider": "World Bank",
            "data": {"items": [
                {"country": {"value": "Japan"}, "date": "2023", "value": 4200},
                {"country": {"value": "Germany"}, "date": "2023", "value": 4500},
            ]},
        }
        summary = app.external_tool_summary(world_bank)
        self.assertIn("Japan", summary)
        self.assertIn("Germany", summary)
        self.assertIn("provider native units (unscaled)", summary)

        table_data = {"items": [
            {"country": {"value": country}, "date": str(year), "value": year * 1000}
            for country in ("Japan", "Germany") for year in range(2015, 2024)
        ], "request_complete": True}
        table = app.external_tool_summary({"tool_id": "statistics.world_bank", "provider": "World Bank", "data": table_data})
        self.assertTrue(all(str(year) in table for year in range(2015, 2024)))
        self.assertEqual(9, sum(1 for line in table.splitlines() if line[:4].isdigit()))
        self.assertEqual(2, table.splitlines()[1].count("|"))
        self.assertTrue(table_data["render_complete"])

        block = app.external_tool_display_blocks({"tool_id": "statistics.world_bank", "provider": "World Bank", "data": table_data})[0]
        self.assertEqual("table", block["type"])
        self.assertEqual(3, len(block["columns"]))
        self.assertEqual(9, len(block["rows"]))
        self.assertEqual({"year", "country_0", "country_1"}, set(block["rows"][0]))
        self.assertEqual("right", block["columns"][0]["align"])
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
        self.assertIn("現在EONETに登録されている自然災害の例です。", rendered)
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
        self.assertIn("Federal Registerで人工知能に関係する最近の文書が見つかりました。", rendered)
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

    def test_provider_success_contradiction_retries_once_then_uses_typed_fallback(self):
        result = {
            "tool_id": "geo.poi_search", "provider": "Overpass", "fetched_at": "now",
            "sources": [{"provider": "Overpass"}],
            "data": {"items": [{"name": "Tokyo Station"}], "semantic_status": "DATA"},
        }
        fake_task = app.Task("駅を探して")
        with patch.object(app.runtime, "compose_tool_result", return_value=(fake_task, "情報を取得できませんでした")) as compose:
            _, response = app.compose_external_result("駅を探して", result)
        self.assertIn("Tokyo Station", response)
        self.assertEqual(2, compose.call_count)

    def test_osrm_summary_is_natural_japanese_with_presentation_only_conversion(self):
        result = {
            "tool_id": "geo.routing", "provider": "OSRM",
            "data": {"origin": "東京駅", "destination": "東京タワー", "profile": "driving",
                     "items": [{"distance_m": 3913.7, "duration_s": 349.5}]},
        }
        response = app.external_tool_summary(result)
        self.assertIn("東京駅から東京タワーまでの道路距離は約3.9kmで、推定所要時間は車で約5分50秒です。", response)
        self.assertIn("経路データ：OSRM", response)
        self.assertNotIn("Profile:", response)
        self.assertNotIn("Distance:", response)
        self.assertNotIn("Estimated duration:", response)

        long_result = {"tool_id": "geo.routing", "provider": "OSRM", "data": {"origin": "A", "destination": "B", "profile": "driving", "items": [{"distance_m": 500, "duration_s": 3720}]}}
        self.assertIn("約500m", app.external_tool_summary(long_result))
        self.assertIn("約1時間2分", app.external_tool_summary(long_result))

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

    def test_valid_decision_is_allowlisted(self):
        fake=FakeRouter(['{"decision":"tool","tool_id":"currency.frankfurter","arguments":{"base":"USD","quote":"JPY","amount":100}}'])
        with patch.object(app.settings, "router_model", "gemma3:1b"), patch.object(app.runtime, "model", fake):
            decision=app.router_decision("100 USD in JPY")
        self.assertEqual("currency.frankfurter", decision[0]); self.assertEqual(1, fake.calls)

    def test_invalid_output_retries_once_then_does_not_guess(self):
        fake=FakeRouter(["not json", "still not json"])
        with patch.object(app.settings, "router_model", "gemma3:1b"), patch.object(app.runtime, "model", fake):
            decision=app.router_decision("today's weather")
        self.assertIsNone(decision); self.assertEqual(2, fake.calls)

    def test_embedding_model_is_rejected(self):
        with self.assertRaises(ValueError): app.settings.with_overrides({"router_model":"embeddinggemma:latest"})
