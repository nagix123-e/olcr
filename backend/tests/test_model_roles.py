import os
import unittest
from unittest.mock import patch

import olcr_api.app as app
from olcr_api import external_tools
from olcr_api.config import DEFAULT_MAIN_MODEL, DEFAULT_ROUTER_MODEL, Settings


class ModelRoleTests(unittest.TestCase):
    def test_default_roles_use_one_primary_and_one_router(self):
        settings = Settings()
        self.assertEqual("qwen3.5:9b", DEFAULT_MAIN_MODEL)
        self.assertEqual("LiquidAI/lfm2.5-350m", DEFAULT_ROUTER_MODEL)
        self.assertEqual(DEFAULT_MAIN_MODEL, settings.main_model)
        self.assertEqual(DEFAULT_ROUTER_MODEL, settings.router_model)

    def test_environment_roles_are_independently_configurable(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "brain-test", "OLLAMA_ROUTER_MODEL": "router-test"}, clear=False):
            settings = Settings.from_env()
        self.assertEqual("brain-test", settings.main_model)
        self.assertEqual("router-test", settings.router_model)

    def test_legacy_coding_setting_and_primary_model_are_migrated(self):
        settings = Settings().with_overrides({"coding_model": "qwen3:14b", "main_model": "qwen3:14b"})
        self.assertEqual("qwen3.5:9b", settings.main_model)
        self.assertNotIn("coding_model", settings.public_dict())

    def test_legacy_primary_model_is_not_used_without_coding_key(self):
        self.assertEqual("qwen3.5:9b", Settings().with_overrides({"main_model": "qwen3:14b"}).main_model)

    def test_router_candidate_excludes_normal_chat(self):
        self.assertFalse(app._external_router_candidate("Explain recursion in simple terms"))
        self.assertTrue(app._external_router_candidate("find the latest exchange rate"))

    def test_router_uses_lfm_and_schema_for_registered_provider(self):
        class FakeRouter:
            def __init__(self): self.calls = []
            def generate(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return {"text": '{"decision":"tool","tool_id":"currency.frankfurter","arguments":{"base":"USD","quote":"JPY","amount":1}}'}
        fake = FakeRouter()
        with patch.object(app.settings, "router_model", "LiquidAI/lfm2.5-350m"), patch.object(app.runtime, "model", fake):
            decision = app.router_decision("find the latest exchange rate")
        self.assertEqual("currency.frankfurter", decision[0])
        self.assertEqual("LiquidAI/lfm2.5-350m", fake.calls[0][0][1])
        self.assertEqual(app.ROUTER_DECISION_SCHEMA, fake.calls[0][1]["format"])

    def test_router_rejects_unknown_provider(self):
        class FakeRouter:
            def generate(self, *args, **kwargs):
                return {"text": '{"decision":"tool","tool_id":"unknown.provider","arguments":{}}'}
        with patch.object(app.settings, "router_model", "LiquidAI/lfm2.5-350m"), patch.object(app.runtime, "model", FakeRouter()):
            self.assertIsNone(app.router_decision("find an external data source"))

    def test_registry_inventory_is_typed_and_explicit(self):
        rows = external_tools.status(True)
        self.assertEqual(set(external_tools.REGISTRY), {row["tool_id"] for row in rows})
        self.assertEqual(60, len(rows))
        self.assertEqual(58, sum(1 for row in rows if row["external_network"]))
        self.assertEqual(2, sum(1 for row in rows if not row["external_network"]))
        for row in rows:
            with self.subTest(tool_id=row["tool_id"]):
                self.assertIn(row["availability"], {"READY", "DISABLED", "UNAVAILABLE", "CONFIG_REQUIRED"})
                self.assertIn(row["credential"], {"none", "optional", "required", "Configured"})
                self.assertIn(row["execution_type"], {"HTTP_GET", "LOCAL_TOOL"})

    def test_lfm_contract_accepts_each_registered_tool_from_natural_language(self):
        requests = {
            "weather.open_meteo": "東京の明日の天気を調べて",
            "currency.frankfurter": "100ドルはユーロでいくら？",
            "research.openalex": "RAGの最近の論文を調べて",
            "knowledge.wikimedia": "AnthropicについてWikipediaで調べて",
            "knowledge.wikidata": "日本の歴代首相を調べて",
            "environment.air_quality": "東京のPM2.5を調べて",
            "geo.poi_search": "東京駅から半径1kmの病院を調べて",
            "geo.routing": "東京駅から渋谷駅までの距離を調べて",
            "country.profile": "日本の首都と通貨を調べて",
            "statistics.world_bank": "日本のGDP統計を調べて",
            "statistics.oecd": "OECDの失業率データを調べて",
            "earth.earthquake": "昨日の日本周辺の地震を調べて",
            "earth.natural_event": "現在の自然災害を調べて",
            "marine.tides_currents": "東京の満潮時刻を調べて",
            "astronomy.sun_times": "東京の日の出時刻を調べて",
            "books.search": "Rustの本を検索して",
            "web.archive_search": "example.comの過去のアーカイブを調べて",
            "chemistry.compound": "aspirinの分子式を調べて",
            "health.clinical_trials": "糖尿病の募集中の臨床試験を調べて",
            "health.fda_data": "aspirinのFDA情報を調べて",
            "food.product_lookup": "この商品のバーコードを調べて",
            "news.hacker_news": "Hacker Newsの最新記事を調べて",
            "software.github": "ReactのGitHubリポジトリを調べて",
            "aviation.live_state": "現在飛行中の航空機を調べて",
            "language.dictionary": "serendipityの意味を英語辞書で調べて",
            "language.translation": "この英文を日本語に翻訳して",
            "visualization.chart": "このデータを棒グラフにして",
            "government.us_federal_register": "Federal RegisterのAI規則を調べて",
            "math.symbolic": "x^2 - 1を因数分解して",
            "math.numeric": "x^2を数値積分して",
        }
        self.assertTrue(set(requests) <= set(external_tools.REGISTRY))
        for tool_id, message in requests.items():
            class FakeRouter:
                def generate(self, *args, **kwargs):
                    return {"text": '{"decision":"tool","tool_id":' + repr(tool_id).replace("'", '"') + ',"arguments":{"query":"example"}}'}
            with self.subTest(tool_id=tool_id), patch.object(app.settings, "router_model", DEFAULT_ROUTER_MODEL), patch.object(app.runtime, "model", FakeRouter()):
                decision = app.router_decision(message)
            self.assertEqual((tool_id, {"query": "example"}), decision)

    def test_router_rejects_extra_fields_and_no_tool_is_safe(self):
        class FakeRouter:
            def __init__(self, payload): self.payload = payload
            def generate(self, *args, **kwargs): return {"text": self.payload}
        with patch.object(app.settings, "router_model", DEFAULT_ROUTER_MODEL), patch.object(app.runtime, "model", FakeRouter('{"decision":"tool","tool_id":"currency.frankfurter","arguments":{},"second_tool":"weather.open_meteo"}')):
            self.assertIsNone(app.router_decision("Japanについて調べて"))
        with patch.object(app.settings, "router_model", DEFAULT_ROUTER_MODEL), patch.object(app.runtime, "model", FakeRouter('{"decision":"no_tool"}')):
            self.assertIsNone(app.router_decision("こんにちは"))
        self.assertFalse(app._external_router_candidate("こんにちは"))
        self.assertFalse(app._external_router_candidate("Pythonのfor文を説明して"))
