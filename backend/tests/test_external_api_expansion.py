import json
import unittest
from unittest.mock import patch

from olcr_api import external_tools


CASES = {
    "calendar.public_holidays": "日本の2027年の祝日を調べて",
    "space.launches": "次のロケット打ち上げは？",
    "space.space_weather": "現在の宇宙天気を調べて",
    "space.satellite_orbit": "ISSの衛星軌道を調べて",
    "space.ephemeris": "JPL Horizonsで火星の位置",
    "space.iss_position": "今のISSの位置は？",
    "space.exoplanets": "系外惑星を調べて",
    "news.global_search": "最近の世界のニュース",
    "finance.sec_filings": "Appleの最新の10-Qを調べて",
    "japan.diet_transcript": "国会会議録で発言を調べて",
    "japan.law": "民法の条文を調べて",
    "geo.elevation": "富士山の標高 35.36,138.73",
    "earth.streamflow": "河川の流量 12345678",
    "biology.occurrence": "GBIFの生物出現記録",
    "biology.phylogeny": "Open Treeの系統樹",
    "biology.structure": "RCSB PDB 1CRN",
    "biology.protein": "UniProt P69905",
    "security.cve": "CVE-2024-1234の脆弱性",
    "security.exploit_probability": "CVE-2024-1234のEPSS",
    "crypto.bitcoin_network": "Bitcoinの手数料",
    "internet.domain_registration": "example.comのRDAP情報",
    "internet.ip_info": "8.8.8.8のIP情報",
    "media.tv": "TVmazeでBreaking Bad",
    "games.pokemon": "ピカチュウのタイプと能力値",
    "games.trivia": "トリビアクイズ",
    "food.recipe": "鶏肉レシピ",
    "art.met_collection": "メトロポリタン美術館の作品",
    "games.pc_deals": "PCゲームのセール",
    "games.chess": "Chess.comのMagnusレーティング",
    "sports.formula1": "F1の今季順位",
}


class ExternalApiExpansionTests(unittest.TestCase):
    def test_all_thirty_are_registered_and_have_status_rows(self):
        self.assertEqual(set(CASES), set(external_tools.NEW_TOOL_IDS))
        rows = {row["tool_id"]: row for row in external_tools.status(True)}
        self.assertTrue(set(CASES) <= rows.keys())

    def test_all_thirty_route_and_compile(self):
        for tool_id, text in CASES.items():
            with self.subTest(tool_id=tool_id):
                route = external_tools.route(text)
                self.assertIsNotNone(route)
                self.assertEqual(tool_id, route[0])
                compiled = external_tools.compile_provider_arguments(tool_id, text, route[1])
                self.assertIsInstance(compiled, dict)

    def test_successful_common_envelope_has_provenance_and_not_raw_json(self):
        for tool_id in CASES:
            with self.subTest(tool_id=tool_id):
                result = external_tools._external_result(tool_id, "fixture", {"items": [{"title": "Fixture", "value": 1}]}, "https://example.invalid/")
                self.assertEqual("DATA", result["data"]["semantic_status"])
                self.assertTrue(result["sources"])
                self.assertNotEqual(json.dumps(result["data"], ensure_ascii=False), result["data"].get("render_text"))

    def test_empty_list_is_not_semantic_data(self):
        for tool_id in CASES:
            with self.subTest(tool_id=tool_id):
                result = external_tools._external_result(tool_id, "fixture", {"items": []}, "https://example.invalid/")
                self.assertEqual("EMPTY", result["data"]["semantic_status"])

    def test_all_thirty_have_non_empty_deterministic_rendering(self):
        from olcr_api.app import external_tool_summary
        for tool_id in CASES:
            with self.subTest(tool_id=tool_id):
                result = external_tools._external_result(tool_id, "fixture", {"items": [{"title": "Fixture", "name": "Fixture", "value": 1}]}, "https://example.invalid/")
                rendered = external_tool_summary(result)
                self.assertTrue(rendered.strip())
                self.assertNotEqual(json.dumps(result["data"], ensure_ascii=False), rendered)


if __name__ == "__main__":
    unittest.main()
