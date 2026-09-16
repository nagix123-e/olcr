import unittest

from olcr_api.external_tools import compile_provider_arguments


class ProviderArgumentCompilerTests(unittest.TestCase):
    def test_structured_examples_are_compact_and_typed(self):
        text = "人口1000万人以上のEU加盟国を人口の多い順に5か国挙げてください。構造化データを使って確認してください。"
        args = compile_provider_arguments("knowledge.wikidata", text, {"query": text})
        self.assertEqual("EU member country", args["subject"])
        self.assertEqual(10_000_000, args["population_min"])
        self.assertEqual("population_desc", args["ordering"])
        self.assertEqual(5, args["limit"])

        args = compile_provider_arguments("environment.air_quality", "東京都心の現在のPM2.5と大気質を調べて", {"location": text})
        self.assertEqual("東京", args["location"])

        args = compile_provider_arguments("geo.poi_search", "東京駅から半径1km以内にある病院を探して", {})
        self.assertEqual({"center_name": "東京駅", "radius_m": 1000, "poi_type": "hospital", "limit": 1}, args)

    def test_semantic_identifiers_never_use_the_full_prompt(self):
        text = "ISBN 9780262033848 の本について詳しく調べて"
        args = compile_provider_arguments("books.search", text, {"query": text})
        self.assertEqual("9780262033848", args["isbn"])
        self.assertNotEqual(text, args["isbn"])
        self.assertEqual("aspirin", compile_provider_arguments("chemistry.compound", "アスピリンの分子式", {})["compound"])
        self.assertEqual(["JP", "DE"], compile_provider_arguments("statistics.world_bank", "日本とドイツのGDPを2015年から2023年まで比較", {})["countries"])

    def test_common_gui_intents_compile_without_geographic_fallback(self):
        translation = compile_provider_arguments(
            "language.translation",
            "「今日はとても良い天気です」を英語に翻訳してください。",
            {"text": "prompt", "source": "ja", "target": "en"},
        )
        self.assertEqual({"text": "今日はとても良い天気です", "source": "ja", "target": "en"}, translation)
        dictionary = compile_provider_arguments(
            "language.dictionary", '英単語 "ephemeral" の意味、品詞、発音、例文を調べてください。', {"word": "prompt"}
        )
        self.assertEqual("ephemeral", dictionary["word"])
        chart = compile_provider_arguments(
            "visualization.chart", "1月10、2月20、3月15、4月30というデータを棒グラフにしてください。", {}
        )
        self.assertEqual(["1月", "2月", "3月", "4月"], chart["config"]["data"]["labels"])
        self.assertEqual([10, 20, 15, 30], chart["config"]["data"]["datasets"][0]["data"])
        symbolic = compile_provider_arguments(
            "math.symbolic", "x^4 - 1 を因数分解してください。", {"operation": "factor", "expression": "x**2-1"}
        )
        self.assertEqual("x^4 - 1", symbolic["expression"])

    def test_provider_queries_are_compact_and_typed(self):
        earthquake = compile_provider_arguments("earth.earthquake", "過去7日間の世界中のM6以上の地震を調べて", {})
        self.assertEqual("worldwide", earthquake["query"])
        self.assertEqual(6.0, earthquake["minmagnitude"])
        self.assertNotIn("過去", str(earthquake))
        register = compile_provider_arguments("government.us_federal_register", "最近の人工知能に関するruleを5件", {})
        self.assertEqual("人工知能に関する", register["query"])
        self.assertEqual("RULE", register["type"])
        self.assertNotIn("最近", register["query"])


if __name__ == "__main__":
    unittest.main()
