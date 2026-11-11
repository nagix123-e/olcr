import unittest
from unittest.mock import patch

from olcr_api import external_tools as tools


class ExternalToolsTests(unittest.TestCase):
    def test_qiita_fuzzy_agent_query_returns_multiple_relevant_articles(self):
        request = {"query": "AIエージェント", "page": 1, "per_page": 10}
        payload = [
            {"id": f"agent-{index}", "title": f"AIエージェント実装パターン {index}",
             "url": f"https://qiita.com/example/items/{index}",
             "user": {"id": "author"}, "tags": [{"name": "AIエージェント"}],
             "body": "ツール呼び出しと状態管理を説明する記事です。", "likes_count": index}
            for index in range(1, 4)
        ]
        with patch.object(tools, "_request", return_value=payload) as provider:
            result = tools._qiita_search({
                "query": "最近の日本語技術記事から、AIエージェントの実装でよく使われる構成と課題を複数記事をもとに整理して。"
            })
        self.assertEqual("AIエージェント", provider.call_args.args[2]["query"])
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertGreaterEqual(result["data"]["item_count"], 3)
        self.assertEqual(3, len(result["data"]["items"]))

    def test_qiita_pagination_keeps_compact_page_size_constant(self):
        calls = []

        def response(_host, _path, params, _headers=None):
            calls.append(dict(params))
            count = 10 if params["page"] == 1 else 2
            return [
                {"id": f"agent-{params['page']}-{index}",
                 "title": f"AIエージェント設計 {params['page']}-{index}",
                 "url": f"https://qiita.com/example/items/{params['page']}-{index}",
                 "user": {"id": "author"}, "tags": [{"name": "AIエージェント"}],
                 "body": "AIエージェントの構成と課題を説明する。", "likes_count": index}
                for index in range(count)
            ]

        with patch.object(tools, "_request", side_effect=response):
            result = tools._qiita_search({"query": "AIエージェント", "limit": 12})

        self.assertEqual([(1, 10), (2, 10)], [(call["page"], call["per_page"]) for call in calls])
        self.assertEqual(12, result["data"]["item_count"])

    def test_weather_uses_only_location_and_normalizes_response(self):
        calls=[]
        def response(host, path, params, headers=None):
            calls.append((host, params))
            if host == "geocoding-api.open-meteo.com":
                return {"results":[{"name":"Tokyo","country":"Japan","latitude":35.68,"longitude":139.76,"timezone":"Asia/Tokyo"}]}
            return {"timezone":"Asia/Tokyo","current":{"time":"2026-09-06T10:00","temperature_2m":25,"wind_speed_10m":4}}
        with patch.object(tools, "_request", side_effect=response):
            result=tools.weather("Tokyo")
        self.assertEqual("weather.open_meteo", result["tool_id"])
        self.assertEqual(25, result["data"]["current"]["temperature_2m"])
        self.assertEqual({"name","count","language","format"}, set(calls[0][1]))

    def test_weather_japanese_temporal_prefix_is_not_sent_to_geocoder(self):
        calls=[]
        def response(host, path, params, headers=None):
            calls.append((host, params))
            return {"results":[{"name":"東京都","country":"Japan","latitude":35.68,"longitude":139.76,"timezone":"Asia/Tokyo"}]} if "geocoding" in host else {"timezone":"Asia/Tokyo","current":{"time":"2026-09-06","temperature_2m":25}}
        with patch.object(tools, "_request", side_effect=response):
            result=tools.weather("今日の東京の天気は？", "today")
        self.assertEqual("東京都", result["data"]["location"]["name"])
        self.assertEqual("東京", calls[0][1]["name"])

    def test_weather_route_keeps_date_out_of_location(self):
        cases = {
            "今日の東京の天気は？": ("東京", "today"),
            "明日の大阪の天気": ("大阪", "tomorrow"),
            "札幌は今日雨？": ("札幌", "today"),
            "Weather in Paris tomorrow": ("Paris", "tomorrow"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                tool_id, args = tools.route(text)
                self.assertEqual("weather.open_meteo", tool_id)
                self.assertEqual(expected, (args["location"], args["date"]))

    def test_currency_converts_and_router_handles_japanese(self):
        with patch.object(tools, "_request", return_value={"base":"USD","quote":"JPY","rate":150.0,"date":"2026-09-06"}):
            result=tools.currency("USD", "JPY", 100)
        self.assertEqual("15000", result["data"]["converted_amount"])
        self.assertEqual(("currency.frankfurter", {"base":"USD","quote":"JPY","amount":100.0}), tools.route("100ドルは今何円？"))

    def test_currency_historical_and_malformed_v2_response(self):
        with patch.object(tools, "_request", return_value={"base":"USD","quote":"JPY","rate":"149.125","date":"2025-01-15"}) as request:
            result=tools.currency("USD", "JPY", date="2025-01-15")
        self.assertEqual("2025-01-15", result["data"]["rate_date"])
        self.assertEqual({"date":"2025-01-15"}, request.call_args.args[2])
        with patch.object(tools, "_request", return_value={"rates":{"JPY":150}}):
            with self.assertRaisesRegex(tools.ExternalToolError, "PROVIDER_BAD_RESPONSE"):
                tools.currency("USD", "JPY")

    def test_currency_v2_normalizes_provider_rate_for_multiple_directions_and_amounts(self):
        cases = [(100, "USD", "EUR", "0.8721", "87.21"),
                 (100, "EUR", "USD", "1.1467", "114.67"),
                 (1, "USD", "EUR", "0.8721", "0.8721"),
                 (123.45, "USD", "EUR", "0.8721", "107.660745")]
        for amount, base, quote, rate, expected in cases:
            with self.subTest(amount=amount, base=base, quote=quote), patch.object(tools, "_request", return_value={"base": base, "quote": quote, "rate": rate, "date": "2026-09-18"}):
                result = tools.currency(base, quote, amount)
            self.assertEqual(base, result["data"]["base"])
            self.assertEqual(quote, result["data"]["quote"])
            self.assertEqual(expected, result["data"]["converted_amount"])

    def test_currency_rejects_missing_rate_and_wrong_direction_metadata(self):
        with patch.object(tools, "_request", return_value={"base": "USD", "quote": "EUR", "date": "2026-09-18"}):
            with self.assertRaisesRegex(tools.ExternalToolError, "PROVIDER_BAD_RESPONSE"):
                tools.currency("USD", "EUR", 100)
        with patch.object(tools, "_request", return_value={"base": "EUR", "quote": "USD", "rate": "1.1467"}):
            with self.assertRaisesRegex(tools.ExternalToolError, "PROVIDER_BAD_RESPONSE"):
                tools.currency("USD", "EUR", 100)

    def test_openalex_key_is_never_returned(self):
        work={"results":[{"id":"https://openalex.org/W1","title":"Paper","publication_year":2025,"authorships":[],"primary_location":{},"open_access":{}}]}
        with patch.dict("os.environ", {"OLCR_OPENALEX_API_KEY":"never-display"}), patch.object(tools, "_request", return_value=work) as request:
            result=tools.research(query="RAG")
        self.assertNotIn("never-display", str(result))
        self.assertEqual("key_configured", result["data"]["authentication"])
        self.assertEqual("never-display", request.call_args.args[2]["api_key"])

    def test_external_provider_text_is_plain_data(self):
        self.assertEqual("knowledge.wikimedia", tools.route("WikipediaでRustを調べて")[0])
        self.assertIsNone(tools.route("今日のニュース"))

    def test_natural_research_and_wiki_keep_only_the_subject(self):
        research_tool, research_args = tools.route("RAGについて最近の論文を3件探して")
        self.assertEqual("research.openalex", research_tool)
        self.assertEqual("retrieval augmented generation", research_args["query"])
        self.assertEqual(3, research_args["limit"])
        self.assertEqual("recent", research_args["recency"])
        wiki_tool, wiki_args = tools.route("Wikipediaを使って量子コンピュータを簡単に説明して")
        self.assertEqual("knowledge.wikimedia", wiki_tool)
        self.assertEqual({"query": "量子コンピュータ", "language": "ja"}, wiki_args)

    def test_recent_research_uses_a_bounded_newest_first_provider_query(self):
        with patch.object(tools, "_request", return_value={"results":[{"id":"W1","title":"Recent RAG","publication_year":2026,"publication_date":"2026-01-01","type":"article","authorships":[],"primary_location":{},"open_access":{}}]}) as request:
            tools.research(query="retrieval augmented generation", recency="recent")
        params = request.call_args.args[2]
        self.assertNotIn("sort", params)
        self.assertIn("from_publication_date:", params["filter"])
        self.assertIn("to_publication_date:", params["filter"])
        self.assertGreater(params["per-page"], 3)
        self.assertIn("title", params["select"])
        self.assertNotIn("abstract_inverted_index", params["select"])

    def test_llm_environmental_research_fetches_compact_abstract_evidence(self):
        row = {"id": "https://openalex.org/W1", "title": "Energy Consumption Evaluation for Large Language Models",
               "publication_year": 2025, "publication_date": "2025-05-01", "type": "article",
               "authorships": [], "primary_location": {}, "open_access": {}, "relevance_score": 10,
               "abstract_inverted_index": {"We": [0], "measure": [1], "energy": [2], "use.": [3]}}
        with patch.object(tools, "_request", return_value={"results": [row]}) as request:
            result = tools.research(
                query="large language model inference training energy consumption water consumption measurement",
                limit=5, recency="recent",
            )
        params = request.call_args.args[2]
        self.assertIn("abstract_inverted_index", params["select"])
        self.assertEqual("We measure energy use.", result["data"]["works"][0]["abstract"])

    def test_llm_resource_research_rejects_code_generation_energy_papers(self):
        def abstract(words):
            return {word: [index] for index, word in enumerate(words.split())}
        rows = [
            {"id": "Wcode", "title": "Energy-Efficient Code Generation Using Large Language Models",
             "publication_year": 2025, "publication_date": "2025-05-01", "type": "article",
             "authorships": [], "primary_location": {}, "open_access": {}, "relevance_score": 100,
             "abstract_inverted_index": abstract("We evaluate energy efficiency of generated code from LLMs.")},
            {"id": "Wdirect", "title": "Measuring Energy and Water Consumption of Large Language Model Inference",
             "publication_year": 2025, "publication_date": "2025-06-01", "type": "article",
             "authorships": [], "primary_location": {}, "open_access": {}, "relevance_score": 10,
             "abstract_inverted_index": abstract("We measure energy consumption and water usage during LLM inference.")},
        ]
        with patch.object(tools, "_request", return_value={"results": rows}):
            result = tools.research(
                query="large language model inference training energy consumption water consumption measurement",
                limit=5,
            )
        self.assertEqual(["Wdirect"], [work["openalex_id"] for work in result["data"]["works"]])

    def test_recent_research_rejects_future_unrelated_and_duplicate_candidates(self):
        rows = [
            {"id":"Wfuture","title":"Journal of GIS based Historical Studies","publication_date":"2029-01-01","type":"article","authorships":[],"primary_location":{},"open_access":{}},
            {"id":"Wbad","title":"MRAG-HC-System release","publication_date":"2026-01-01","type":"software","authorships":[],"primary_location":{},"open_access":{}},
            {"id":"W1","title":"Retrieval augmented generation for medical QA","publication_date":"2025-06-01","type":"article","authorships":[],"primary_location":{},"open_access":{}},
            {"id":"W2","title":"Retrieval augmented generation for medical QA","publication_date":"2025-06-02","type":"article","authorships":[],"primary_location":{},"open_access":{}},
        ]
        with patch.object(tools, "_request", return_value={"results": rows}), patch.object(tools, "datetime") as clock:
            clock.now.return_value = __import__("datetime").datetime(2026, 9, 6, tzinfo=__import__("datetime").timezone.utc)
            result = tools.research(query="retrieval augmented generation", recency="recent")
        self.assertEqual(["W1"], [work["openalex_id"] for work in result["data"]["works"]])
