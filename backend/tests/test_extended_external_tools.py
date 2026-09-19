import unittest
from unittest.mock import patch
from pathlib import Path

from olcr_api import external_tools as tools


class ExtendedProviderTests(unittest.TestCase):
    IDS = {
        "knowledge.wikidata", "environment.air_quality", "geo.poi_search", "geo.routing", "country.profile",
        "statistics.world_bank", "statistics.oecd", "earth.earthquake", "earth.natural_event",
        "marine.tides_currents", "astronomy.sun_times", "books.search", "web.archive_search",
        "chemistry.compound", "health.clinical_trials", "health.fda_data", "food.product_lookup",
        "news.hacker_news", "software.github", "aviation.live_state", "language.dictionary",
        "language.translation", "visualization.chart", "government.us_federal_register", "math.symbolic", "math.numeric",
    }

    def test_registry_contains_all_structured_and_local_tools(self):
        self.assertTrue(self.IDS.issubset(tools.REGISTRY))
        rows = {row["tool_id"]: row for row in tools.status(True)}
        self.assertEqual("LOCAL_TOOL", rows["math.symbolic"]["execution_type"])
        self.assertEqual("LOCAL_TOOL", rows["math.numeric"]["execution_type"])
        self.assertEqual("knowledge.structured_query", rows["knowledge.wikidata"]["capabilities"])
        self.assertEqual("HTTP_GET", rows["news.hacker_news"]["execution_type"])
        self.assertIn("provider_id", rows["country.profile"])

    def test_natural_language_routing_is_specific_and_negative_cases_stay_general(self):
        cases = {
            "歴代首相で最年少は？": "knowledge.wikidata",
            "今日のPM2.5は？": "environment.air_quality",
            "半径1kmの病院": "geo.poi_search",
            "A地点からB地点までの距離": "geo.routing",
            "日本とドイツのGDP推移": "statistics.world_bank",
            "最近のM5以上の地震": "earth.earthquake",
            "最近の自然災害": "earth.natural_event",
            "このISBNの本 978-0131103627": "books.search",
            "aspirinの分子量": "chemistry.compound",
            "この病気の募集中の臨床試験": "health.clinical_trials",
            "このGitHub repoの最新版 https://github.com/org/repo": "software.github",
            "この英単語の意味 hello": "language.dictionary",
            "x^2 - 1を因数分解": "math.symbolic",
            "数値積分して": "math.numeric",
            "「今日はとても良い天気です」を英語に翻訳してください": "language.translation",
            "英単語 \"ephemeral\" の意味、品詞、発音、例文": "language.dictionary",
            "1月10、2月20、3月15、4月30を棒グラフにしてください": "visualization.chart",
            "現在進行中として登録されている山火事や火山": "earth.natural_event",
            "サンフランシスコ周辺の今日の満潮・干潮時刻": "marine.tides_currents",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expected, tools.route(text)[0])
        self.assertIsNone(tools.route("今日の一般ニュース"))
        self.assertIsNone(tools.route("薬を飲んだ方がいい？"))
        self.assertIsNone(tools.route("Pythonで積分コードを書いて"))
        self.assertIsNone(tools.route("Wikidataとは何？"))

    def test_http_adapters_return_normalized_provenance(self):
        payload = {"results": [{"id": 1}, {"id": 2}]}
        args = {
            "knowledge.wikidata": {"query": "SELECT * WHERE {?s ?p ?o} LIMIT 2"},
            "environment.air_quality": {"latitude": 35.0, "longitude": 139.0},
            "geo.poi_search": {"query": "[out:json];node(35,139,35.1,139.1);out 2;"},
            "geo.routing": {"start": "139.0,35.0", "end": "139.1,35.1"},
            "country.profile": {"country": "JP"},
            "statistics.world_bank": {"country": "JP", "indicator": "NY.GDP.MKTP.CD"},
            "statistics.oecd": {"query": "DP_LIVE"},
            "earth.earthquake": {"query": "Japan", "minmagnitude": 5},
            "earth.natural_event": {"status": "open"},
            "marine.tides_currents": {"station": "8518750"},
            "astronomy.sun_times": {"latitude": 35.0, "longitude": 139.0},
            "books.search": {"query": "Rust"},
            "web.archive_search": {"url": "https://example.com"},
            "chemistry.compound": {"compound": "aspirin"},
            "health.clinical_trials": {"query": "diabetes"},
            "health.fda_data": {"query": "aspirin"},
            "food.product_lookup": {"barcode": "04963406"},
            "news.hacker_news": {"query": "Rust"},
            "software.github": {"repo": "org/repo"},
            "aviation.live_state": {},
            "language.dictionary": {"word": "hello"},
            "language.translation": {"text": "hello", "source": "en", "target": "ja"},
            "government.us_federal_register": {"query": "climate"},
        }
        with patch.object(tools, "_request", return_value=payload):
            for tool_id, arguments in args.items():
                with self.subTest(tool_id=tool_id):
                    result = tools.execute(tool_id, arguments)
                    self.assertEqual(tool_id, result["tool_id"])
                    self.assertEqual(tools.REGISTRY[tool_id].provider, result["provider"])
                    self.assertTrue(result["sources"])
                    self.assertIn("data", result)
                    self.assertIn("query", result["data"])

    def test_local_tools_are_typed_and_do_not_execute_python(self):
        result = tools.execute("math.symbolic", {"operation": "factor", "expression": "x^2 - 1"})
        self.assertIn("x - 1", result["data"]["result"])
        with self.assertRaisesRegex(tools.ExternalToolError, "INVALID_EXPRESSION"):
            tools.execute("math.symbolic", {"operation": "factor", "expression": "__import__('os').system('id')"})
        with self.assertRaisesRegex(tools.ExternalToolError, "INVALID_OPERATION"):
            tools.execute("math.numeric", {"operation": "run_python", "expression": "x"})

    def test_scipy_adapter_reports_missing_optional_dependency_or_normalizes_result(self):
        try:
            result = tools.execute("math.numeric", {"operation": "integrate", "expression": "x**2", "lower": 0, "upper": 1})
        except tools.ExternalToolError as exc:
            self.assertEqual("LOCAL_TOOL_UNAVAILABLE", str(exc))
        else:
            self.assertEqual("math.numeric", result["tool_id"])
            self.assertAlmostEqual(1 / 3, result["data"]["result"], places=5)

    def test_http_timeout_is_normalized(self):
        import httpx
        with patch("httpx.Client", side_effect=httpx.TimeoutException("timeout")):
            with self.assertRaisesRegex(tools.ExternalToolError, "PROVIDER_TIMEOUT"):
                tools._request("example.com", "/", {})

    def test_open_library_and_pubchem_semantic_normalization(self):
        with patch.object(tools, "_request", return_value={"numFound": 1, "docs": [{"title": "Structure and Interpretation of Computer Programs", "author_name": ["Harold Abelson"], "isbn": ["9780262033848"], "first_publish_year": 1996}]}):
            result = tools.execute("books.search", {"isbn": "9780262033848"})
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual("Structure and Interpretation of Computer Programs", result["data"]["items"][0]["title"])
        with patch.object(tools, "_request", return_value={"PropertyTable": {"Properties": [{"CID": 528, "MolecularFormula": "C9H8O4", "MolecularWeight": "180.16", "IUPACName": "2-acetyloxybenzoic acid"}]}}):
            result = tools.execute("chemistry.compound", {"compound": "aspirin"})
        self.assertEqual(528, result["data"]["cid"])
        self.assertEqual("C9H8O4", result["data"]["molecular_formula"])
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual([], result["data"]["missing_fields"])
        self.assertTrue(result["data"]["request_complete"])

    def test_pubchem_missing_requested_field_is_explicit(self):
        with patch.object(tools, "_request", return_value={"PropertyTable": {"Properties": [{"CID": 528, "MolecularWeight": "180.16"}]}}):
            result = tools.execute("chemistry.compound", {"compound": "aspirin"})
        self.assertEqual(["molecular_formula"], result["data"]["missing_fields"])
        self.assertFalse(result["data"]["request_complete"])

    def test_pubchem_request_includes_formula_property(self):
        with patch.object(tools, "_request", return_value={"PropertyTable": {"Properties": [{"CID": 528, "MolecularFormula": "C9H8O4", "MolecularWeight": "180.16"}]}}) as request:
            tools.execute("chemistry.compound", {"compound": "aspirin"})
        self.assertIn("MolecularFormula", request.call_args.args[1])

    def test_hacker_news_top_stories_fetches_item_details(self):
        def request(host, path, params, headers=None):
            if path.endswith("topstories.json"): return [101, 102]
            return {"id": int(path.split("/")[-1].split(".")[0]), "title": "Story"}
        with patch.object(tools, "_request", side_effect=request):
            result = tools.execute("news.hacker_news", {"query": "top stories", "limit": 2})
        self.assertEqual(2, len(result["data"]["items"]))

    def test_world_bank_preserves_native_values_and_reports_entity_coverage(self):
        payloads = [
            [{"page": 1}, [{"countryiso3code": "JPN", "date": str(year), "value": 4200000000000 + year} for year in range(2015, 2024)]],
            [{"page": 1}, [{"countryiso3code": "DEU", "date": str(year), "value": 4500000000000 + year} for year in range(2015, 2024)]],
        ]
        with patch.object(tools, "_request", side_effect=payloads):
            result = tools.execute("statistics.world_bank", {"countries": ["JP", "DE"], "indicator": "NY.GDP.MKTP.CD", "start_year": 2015, "end_year": 2023})
        self.assertEqual(["JP", "DE"], result["data"]["requested_entities"])
        self.assertEqual([], result["data"]["missing_entities"])
        self.assertEqual({"start": 2015, "end": 2023}, result["data"]["requested_time_range"])
        self.assertEqual([], result["data"]["missing_time_points"])
        self.assertTrue(result["data"]["request_complete"])
        self.assertEqual(18, len(result["data"]["items"]))

    def test_world_bank_partial_result_reports_missing_entity_without_latest_five_substitution(self):
        payloads = [[{"page": 1}, [{"countryiso3code": "JPN", "date": str(year), "value": year} for year in range(2015, 2024)]], [{"page": 1}, []]]
        with patch.object(tools, "_request", side_effect=payloads):
            result = tools.execute("statistics.world_bank", {"countries": ["JP", "DE"], "indicator": "NY.GDP.MKTP.CD", "start_year": 2015, "end_year": 2023})
        self.assertEqual(["DE"], result["data"]["missing_entities"])
        self.assertFalse(result["data"]["request_complete"])
        self.assertEqual(9, len(result["data"]["items"]))

    def test_usgs_worldwide_query_uses_typed_window_without_free_text_q(self):
        captured = {}

        def request(host, path, params, headers=None):
            captured.update(params)
            return {"features": [{"id": "eq-1"}]}

        with patch.object(tools, "_request", side_effect=request):
            result = tools.execute("earth.earthquake", {"query": "worldwide", "minmagnitude": 5, "starttime": "2026-09-01", "endtime": "2026-09-07"})
        self.assertNotIn("q", captured)
        self.assertEqual("2026-09-01", captured["starttime"])
        self.assertEqual("2026-09-07", captured["endtime"])
        self.assertEqual("DATA", result["data"]["semantic_status"])

    def test_wayback_cdx_rows_are_normalized_and_mark_execution(self):
        cdx = [["timestamp", "original", "statuscode", "digest"], ["20260907000000", "https://example.com/", "200", "abc"]]
        with patch.object(tools, "_request", return_value=cdx):
            result = tools.execute("web.archive_search", {"url": "https://example.com/"})
        self.assertEqual("20260907000000", result["data"]["items"][0]["timestamp"])
        self.assertEqual("DATA", result["data"]["semantic_status"])

    def test_current_event_and_federal_register_empty_payloads_are_semantically_empty(self):
        with patch.object(tools, "_request", return_value={"events": []}):
            eonet = tools.execute("earth.natural_event", {"status": "open"})
        self.assertEqual("EMPTY", eonet["data"]["semantic_status"])
        with patch.object(tools, "_request", return_value={"results": []}):
            register = tools.execute("government.us_federal_register", {"query": "no-such-topic"})
        self.assertEqual("EMPTY", register["data"]["semantic_status"])

    def test_current_event_without_usable_identity_is_empty(self):
        with patch.object(tools, "_request", return_value={"events": [{"geometry": {"type": "Point"}}]}):
            result = tools.execute("earth.natural_event", {"status": "open"})
        self.assertEqual([], result["data"]["items"])
        self.assertEqual("EMPTY", result["data"]["semantic_status"])

    def test_federal_register_explicit_documents_collection_wins_over_metadata_results(self):
        payload = {"results": [{"total_pages": 1}], "documents": []}
        with patch.object(tools, "_request", return_value=payload):
            result = tools.execute("government.us_federal_register", {"query": "artificial intelligence"})
        self.assertEqual(0, result["data"]["item_count"])
        self.assertEqual("EMPTY", result["data"]["semantic_status"])

    def test_opensky_requires_documented_states_schema(self):
        with patch.object(tools, "_request", return_value={"unexpected": []}):
            with self.assertRaisesRegex(tools.ExternalToolError, "PROVIDER_BAD_RESPONSE"):
                tools.execute("aviation.live_state", {})

    def test_clinical_trials_recruiting_filter_is_applied(self):
        studies = [{"protocolSection": {"statusModule": {"overallStatus": status}}} for status in ("RECRUITING", "WITHDRAWN", "NOT_YET_RECRUITING")]
        with patch.object(tools, "_request", return_value={"studies": studies}):
            result = tools.execute("health.clinical_trials", {"query": "Alzheimer", "recruitment_status": "recruiting"})
        self.assertEqual(1, len(result["data"]["items"]))
        self.assertEqual("DATA", result["data"]["semantic_status"])

    def test_osrm_landmarks_are_geocoded_independently_after_fallback(self):
        calls = []

        def request(host, path, params, headers=None):
            calls.append((host, path, params))
            if host == "geocoding-api.open-meteo.com":
                return {"results": []}
            if host == "nominatim.openstreetmap.org":
                if "東京駅" in params.get("q", ""):
                    return [{"lat": "35.6812", "lon": "139.7671"}]
                return [{"lat": "35.6586", "lon": "139.7454"}]
            return {"routes": [{"distance": 1000, "duration": 300}]}

        with patch.object(tools, "_request", side_effect=request):
            result = tools.execute("geo.routing", {"origin": "東京駅", "destination": "東京タワー", "profile": "driving"})

        self.assertEqual("geo.routing", result["tool_id"])
        nominatim_queries = [entry[2]["q"] for entry in calls if entry[0] == "nominatim.openstreetmap.org"]
        self.assertEqual(["東京駅", "東京タワー"], nominatim_queries)
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual(1000, result["data"]["items"][0]["distance_m"])
        self.assertEqual(300, result["data"]["items"][0]["duration_s"])

    def test_query_and_path_bounds_reject_unsafe_values(self):
        with self.assertRaisesRegex(tools.ExternalToolError, "QUERY_COMPLEXITY_LIMIT"):
            tools.execute("knowledge.wikidata", {"query": "SELECT * WHERE { SERVICE <https://evil.example> {?s ?p ?o} }"})
        with self.assertRaisesRegex(tools.ExternalToolError, "INVALID_TOOL_ARGUMENTS"):
            tools.execute("statistics.world_bank", {"country": "JP", "indicator": "../secret"})
        with self.assertRaisesRegex(tools.ExternalToolError, "INVALID_TOOL_ARGUMENTS"):
            tools.execute("web.archive_search", {"url": "file:///etc/passwd"})

    def test_scipy_dependency_is_in_runtime_bootstrap_source(self):
        root = Path(__file__).resolve().parents[2]
        requirements = (root / "backend" / "requirements.txt").read_text()
        build_script = (root / "scripts" / "build_release.py").read_text()
        self.assertRegex(requirements, r"(?m)^scipy>=1\.11,<2\.0$")
        self.assertIn('"backend"/"requirements.txt"', build_script)


if __name__ == "__main__":
    unittest.main()
