import unittest
from unittest.mock import patch
from pathlib import Path

from olcr_api import external_tools as tools
from olcr_api.app import external_tool_summary, _new_provider_summary, _deterministic_eia_analysis


class ExtendedProviderTests(unittest.TestCase):
    IDS = {
        "knowledge.wikidata", "environment.air_quality", "geo.routing", "geo.country_facts",
        "econ.worldbank", "econ.oecd", "earth.earthquakes", "earth.disasters",
        "ocean.tides", "astronomy.sunrise", "books.openlibrary", "web.archive",
        "chemistry.pubchem", "health.clinical_trials", "health.openfda", "food.product",
        "news.hackernews", "dev.github", "aviation.opensky", "language.dictionary",
        "language.translation", "render.quickchart", "policy.us_federal_register",
    }

    def test_registry_contains_all_structured_and_local_tools(self):
        self.assertTrue(self.IDS.issubset(tools.REGISTRY))
        rows = {row["tool_id"]: row for row in tools.status(True)}
        self.assertFalse(tools.REGISTRY["math.symbolic"].external)
        self.assertFalse(tools.REGISTRY["math.numeric"].external)
        self.assertEqual("knowledge.structured_query", rows["knowledge.wikidata"]["capabilities"])
        self.assertEqual("HTTP_GET", rows["news.hackernews"]["execution_type"])
        self.assertIn("provider_id", rows["geo.country_facts"])

    def test_natural_language_routing_is_specific_and_negative_cases_stay_general(self):
        cases = {
            "歴代首相で最年少は？": "knowledge.wikidata",
            "今日のPM2.5は？": "environment.air_quality",
            "A地点からB地点までの距離": "geo.routing",
            "最近のM5以上の地震": "earth.earthquakes",
            "最近の自然災害": "earth.disasters",
            "このISBNの本 978-0131103627": "books.openlibrary",
            "aspirinの分子量": "chemistry.pubchem",
            "この病気の募集中の臨床試験": "health.clinical_trials",
            "このGitHub repoの最新版 https://github.com/org/repo": "dev.github",
            "この英単語の意味 hello": "language.dictionary",
            "x^2 - 1を因数分解": "math.symbolic",
            "数値積分して": "math.numeric",
            "「今日はとても良い天気です」を英語に翻訳してください": "language.translation",
            "英単語 \"ephemeral\" の意味、品詞、発音、例文": "language.dictionary",
            "1月10、2月20、3月15、4月30を棒グラフにしてください": "render.quickchart",
            "現在進行中として登録されている山火事や火山": "earth.disasters",
            "サンフランシスコ周辺の今日の満潮・干潮時刻": "ocean.tides",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expected, tools.route(text)[0])
        self.assertEqual('geo.poi', tools.route("半径1kmの病院")[0])
        self.assertEqual('geo.poi', tools.route("Find POI near Tokyo Station")[0])
        self.assertIsNone(tools.route("今日の一般ニュース"))
        self.assertIsNone(tools.route("日本とドイツのGDP推移"))
        self.assertEqual("econ.worldbank", tools.route("World Bankで日本とドイツのGDP推移を比較")[0])
        self.assertIsNone(tools.route(
            "This is not a coding task. What is Japan's latest reported annual GDP in current US dollars?"
        ))
        self.assertEqual("ocean.tides", tools.route("Ocean currents near San Francisco")[0])
        self.assertIsNone(tools.route("薬を飲んだ方がいい？"))
        self.assertIsNone(tools.route("Pythonで積分コードを書いて"))
        self.assertIsNone(tools.route("Wikidataとは何？"))

    def test_checkpoint_word_does_not_trigger_point_of_interest_routing(self):
        self.assertIsNone(tools.route("Bonsai diagnosis checkpoint followed by Ornith implementation"))

    def test_new_provider_renderer_keeps_measurements_not_just_provider_title(self):
        result = {
            "tool_id": "econ.eurostat",
            "provider": "Eurostat",
            "data": {"items": [{"dataset": "nama_10_gdp", "geo": "DE", "time": "2025",
                                  "unit": "EUR_HAB", "value": 51234.5}]},
        }
        rendered = _new_provider_summary(result)
        self.assertIn("Eurostat", rendered)
        self.assertIn("51234.5", rendered)
        self.assertIn("2025", rendered)

    def test_new_provider_renderer_renders_direct_typed_data_when_items_empty(self):
        result = {
            "tool_id": "ocean.tides",
            "provider": "Ocean",
            "data": {"items": [], "semantic_status": "DATA", "station": "San Francisco",
                     "next_high_tide": "2026-10-10T08:30:00Z", "height_m": 1.72},
        }
        rendered = _new_provider_summary(result)
        self.assertIn("San Francisco", rendered)
        self.assertIn("2026-10-10T08:30:00Z", rendered)
        self.assertIn("1.72", rendered)

    def test_cve_record_renders_summary_attack_vector_and_affected_versions(self):
        payload = {
            "cveMetadata": {"cveId": "CVE-2021-44228", "state": "PUBLISHED", "datePublished": "2021-12-10"},
            "containers": {"cna": {
                "descriptions": [{"lang": "en", "value": "Apache Log4j2 JNDI lookup can load code from LDAP when an attacker controls log messages."}],
                "affected": [{"vendor": "Apache", "product": "Log4j", "versions": [
                    {"version": "2.0-beta9", "lessThan": "2.15.0", "status": "affected"},
                    {"version": "2.0-beta9", "lessThan": "log4j-core*", "status": "affected"},
                ]}],
                "metrics": [{"cvssV3_1": {"baseScore": 10.0, "baseSeverity": "CRITICAL",
                                            "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H"}}],
                "references": [{"url": "https://logging.apache.org/log4j/2.x/security.html"}],
            }},
        }
        with patch.object(tools, "_request", return_value=payload):
            result = tools.execute("security.cve", {"cve": "CVE-2021-44228"})
        answer = external_tool_summary(result)
        self.assertIn("CVE-2021-44228", answer)
        self.assertIn("Apache Log4j2 JNDI lookup", answer)
        self.assertIn("攻撃条件（CVE本文の記載）", answer)
        self.assertIn("CVSS：10.0（CRITICAL）", answer)
        self.assertIn("攻撃ベクトル：Network", answer)
        self.assertIn("2.0-beta9 < 2.15.0", answer)
        self.assertNotIn("< log4j-core*", answer)
        self.assertIn("logging.apache.org", answer)

    def test_http_adapters_return_normalized_provenance(self):
        payload = {"results": [{"id": 1}, {"id": 2}]}
        args = {
            "knowledge.wikidata": {"query": "SELECT * WHERE {?s ?p ?o} LIMIT 2"},
            "environment.air_quality": {"latitude": 35.0, "longitude": 139.0},
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

    def test_poi_uses_current_public_endpoint_with_bounded_query(self):
        with patch.object(tools, '_geocode_place', return_value=(35.68, 139.76)), \
             patch.object(tools, '_overpass_request', return_value={'elements': []}) as provider:
            result = tools.execute('geo.poi', {'center_name': '東京駅', 'radius_m': 1000,
                                               'poi_type': 'hospital', 'limit': 5})
        self.assertEqual('geo.poi', result['tool_id'])
        self.assertEqual(('maps.mail.ru', '/osm/tools/overpass/api/interpreter'), provider.call_args.args[:2])
        self.assertIn('around:1000,35.680000,139.760000', provider.call_args.args[2])

    def test_uk_bills_http_boundary_allows_official_search_response(self):
        response = type("Response", (), {
            "status_code": 200, "url": "https://bills-api.parliament.uk/api/v1/Bills",
            "headers": {"content-type": "application/json"}, "content": b"{}",
            "json": lambda self: {},
        })()
        client = type("Client", (), {
            "__enter__": lambda self: self, "__exit__": lambda self, *args: None,
            "get": lambda self, *args, **kwargs: response,
        })()
        with patch.object(tools.httpx, "Client", return_value=client) as make_client:
            tools._request("bills-api.parliament.uk", "/api/v1/Bills", {"SearchTerm": "AI"})

        self.assertEqual(20.0, make_client.call_args.kwargs["timeout"])

    def test_every_executed_adapter_emits_valid_normalized_contract(self):
        with patch.object(tools, "_request", return_value={"results": [{"id": 1}]}):
            result = tools.execute("statistics.world_bank", {"country": "JP", "indicator": "NY.GDP.MKTP.CD"})
        self.assertEqual({"valid": True, "errors": []}, tools.validate_normalized_result(result))

    def test_cepal_country_name_does_not_trigger_unrelated_iso_validator(self):
        # _simple_http builds a legacy endpoint table eagerly.  A non-ISO
        # country name belongs to CEPAL and must not be validated as the
        # unrelated country.profile argument while that table is constructed.
        with patch.object(tools, "_request", return_value={"body": {"data": [], "dimensions": [], "metadata": {}}}):
            result = tools.execute("econ.cepalstat", {"indicator_id": "365", "country": "Argentina", "limit": 5})
        self.assertEqual("econ.cepalstat", result["tool_id"])
        self.assertEqual("time_series", result["response_kind"])

    def test_cepalstat_normalization_filters_requested_country_and_national_only(self):
        payload = {"body": {
            "metadata": {"indicator_name": "Gini income concentration index", "unit": "Index"},
            "dimensions": [
                {"id": 208, "name": "Country__ESTANDAR", "members": [
                    {"id": 222, "name": "Brazil"}, {"id": 216, "name": "Argentina"}]},
                {"id": 29117, "name": "Years__ESTANDAR", "members": [
                    {"id": 29193, "name": "2023"}, {"id": 29194, "name": "2024"}]},
                {"id": 326, "name": "Geographical area", "members": [
                    {"id": 327, "name": "National"}, {"id": 330, "name": "Urban"}]},
            ],
            "data": [
                {"iso3": "BRA", "value": "0.50", "dim_208": 222, "dim_29117": 29193, "dim_326": 327},
                {"iso3": "BRA", "value": "0.45", "dim_208": 222, "dim_29117": 29194, "dim_326": 327},
                {"iso3": "BRA", "value": "0.40", "dim_208": 222, "dim_29117": 29194, "dim_326": 330},
                {"iso3": "ARG", "value": "0.42", "dim_208": 216, "dim_29117": 29194, "dim_326": 327},
            ],
        }}
        rows = tools._cepalstat_rows(payload, "3289", "Brazil", "ブラジルの所得格差", 20)
        self.assertEqual([("2024", "0.45"), ("2023", "0.50")],
                         [(row["year"], row["value"]) for row in rows])
        self.assertEqual({"BRA"}, {row["country_code"] for row in rows})

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

    def test_world_bank_open_ended_range_fetches_latest_years_after_start(self):
        payload = [{"page": 1}, [
            {"countryiso3code": "JPN", "date": str(year), "value": year}
            for year in range(2026, 2009, -1)
        ]]
        with patch.object(tools, "_request", return_value=payload) as request:
            result = tools.execute("statistics.world_bank", {
                "countries": ["JP"], "indicator": "NY.GDP.MKTP.CD", "start_year": 2015, "end_year": None
            })
        self.assertEqual(100, request.call_args.args[2]["per_page"])
        self.assertNotIn("date", request.call_args.args[2])
        self.assertEqual(list(range(2015, 2027)), sorted(int(item["date"]) for item in result["data"]["items"]))
        self.assertEqual({"start": 2015, "end": None}, result["data"]["requested_time_range"])
        self.assertEqual({"start": 2015, "end": 2026}, result["data"]["result_time_range"])
        self.assertTrue(result["data"]["request_complete"])

    def test_world_bank_all_country_mode_collects_paginated_rows_and_keeps_years(self):
        rows = [
            {"date": "2000", "value": 10.0, "countryiso3code": "JPN",
             "country": {"id": "JP", "value": "Japan"}, "indicator": {"id": "EN.ATM.CO2E.KT"}},
            {"date": "2000", "value": 20.0, "countryiso3code": "CHN",
             "country": {"id": "CN", "value": "China"}, "indicator": {"id": "EN.ATM.CO2E.KT"}},
        ]
        def provider(host, path, params):
            if path == '/v2/country/all':
                return [{'pages': 1}, [{'iso2Code': code, 'region': {'id': 'EAS'}} for code in ('JP', 'CN')]]
            return [{'pages': 2}, [rows[params['page'] - 1]]]
        tools._worldbank_country_members.cache_clear()
        try:
            with patch.object(tools, "_request", side_effect=provider) as request:
                result = tools.execute("statistics.world_bank", {
                    "countries": ["all"], "indicator": "EN.ATM.CO2E.KT",
                    "indicators": ["EN.ATM.CO2E.KT"], "start_year": 2000, "end_year": 2000,
                })
            self.assertEqual(2, sum('/country/all/indicator/EN.ATM.CO2E.KT' in call.args[1] for call in request.call_args_list))
        finally:
            tools._worldbank_country_members.cache_clear()
        self.assertEqual(2, len(result["data"]["items"]))
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertTrue(result["data"]["request_complete"])

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

    def test_ucdp_normalizes_events_and_uses_api_endpoint_as_provenance(self):
        with patch.dict("os.environ", {"OLCR_UCDP_API_TOKEN": "ucdp-test"}):
            with patch.object(tools, "_request", return_value={"TotalCount": 1, "Result": [{"id": 1, "date_start": "2024-01-01"}]}):
                ucdp = tools.execute("conflict.ucdp", {"country_id": "369", "limit": 5})
        self.assertEqual("DATA", ucdp["data"]["semantic_status"])
        self.assertEqual(1, ucdp["data"]["items"][0]["id"])
        self.assertEqual("https://ucdpapi.pcr.uu.se/api/gedevents/26.1", ucdp["sources"][0]["canonical_url"])
        self.assertEqual(1, ucdp["data"]["total_count"])
        self.assertEqual("26.1", ucdp["data"]["dataset_version"])

    def test_ucdp_grouped_analysis_fetches_all_pages_before_month_and_region_aggregation(self):
        first = {"TotalCount": 3, "TotalPages": 2, "Result": [
            {"id": 1, "date_end": "2025-01-02", "adm_1": "Kyiv", "best": 2},
            {"id": 2, "date_end": "2025-01-05", "adm_1": "Kyiv", "best": 1},
        ]}
        second = {"TotalCount": 3, "TotalPages": 2, "Result": [
            {"id": 3, "date_end": "2025-02-01", "adm_1": "Kharkiv", "best": 4},
        ]}
        with patch.dict("os.environ", {"OLCR_UCDP_API_TOKEN": "ucdp-test"}), \
             patch.object(tools, "_request", side_effect=[first, second]) as request:
            result = tools.execute("conflict.ucdp", {
                "country_id": "369", "start_date": "2025-01-01", "end_date": "2025-12-31",
                "limit": 10_000, "aggregate_by_time_region": True,
            })
        self.assertEqual(2, request.call_count)
        self.assertEqual("2", str(request.call_args_list[1].args[2]["page"]))
        self.assertEqual(3, result["data"]["aggregated_row_count"])
        self.assertEqual(2, result["data"]["analysis_summary"]["monthly"][0]["events"])
        self.assertEqual(3.0, result["data"]["analysis_summary"]["monthly"][0]["best_deaths"])
        self.assertEqual("Kyiv", result["data"]["analysis_summary"]["top_regions"][0]["region"])

    def test_usaspending_adapters_use_bounded_post_contract(self):
        with patch.object(tools, "_post_request", side_effect=[
            {"results": [{"recipient_name": "SPACE EXPLORATION TECHNOLOGIES CORP."}]},
            {"results": [{"Award ID": "A-1", "Transaction Amount": 10}]},
        ]) as post:
            result = tools.execute("gov.us_spending", {"fiscal_year": 2024, "agency": "National Aeronautics and Space Administration", "recipient": "SpaceX", "limit": 5})
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual("/api/v2/autocomplete/recipient/", post.call_args_list[0].args[1])
        self.assertEqual({"search_text": "Space Exploration Technologies"}, post.call_args_list[0].args[2])
        body = post.call_args_list[1].args[2]
        self.assertEqual("/api/v2/search/spending_by_transaction/", post.call_args_list[1].args[1])
        self.assertEqual("2023-10-01", body["filters"]["time_period"][0]["start_date"])
        self.assertEqual("2024-09-30", body["filters"]["time_period"][0]["end_date"])
        self.assertEqual("action_date", body["filters"]["time_period"][0]["date_type"])
        self.assertEqual(["SPACE EXPLORATION TECHNOLOGIES CORP."], body["filters"]["recipient_search_text"])
        self.assertEqual(4, len(body["filters"]["agencies"]))
        self.assertEqual("Transaction Amount", body["sort"])
        self.assertEqual("desc", body["order"])
        self.assertEqual({"requested": "SpaceX", "submitted": "SPACE EXPLORATION TECHNOLOGIES CORP."}, result["data"]["search_resolution"])

    def test_usaspending_keeps_original_when_autocomplete_is_ambiguous(self):
        ambiguous = {"results": [
            {"recipient_name": "ACME HEALTH SYSTEMS INC"},
            {"recipient_name": "ACME HEALTH SYSTEM LLC"},
        ]}
        award_results = {"results": [{"Award ID": "A-2", "Award Amount": 20}]}
        with patch.object(tools, "_post_request", side_effect=[ambiguous, award_results]) as post:
            result = tools.execute("gov.us_spending", {
                "fiscal_year": 2024, "agency": "NASA", "recipient": "Acme Health System", "limit": 5,
            })
        self.assertEqual(["Acme Health System"], post.call_args_list[1].args[2]["filters"]["recipient_search_text"])
        self.assertNotIn("search_resolution", result["data"])

    def test_usaspending_filters_fuzzy_recipient_rows_after_entity_resolution(self):
        self.assertEqual("cisco", tools._recipient_entity_key("Cisco"))
        self.assertEqual("lockheedmartin", tools._recipient_entity_key("Lockheed Martin Corp."))
        self.assertEqual("lockheedmartin", tools._recipient_entity_key("LOCKHEED MARTIN CORPORATION"))
        self.assertEqual("spaceexplorationtechnologies", tools._recipient_entity_key("SpaceX"))
        self.assertEqual("spaceexplorationtechnologies", tools._recipient_entity_key("Space Exploration Technologies Corp."))
        with patch.object(tools, "_post_request", side_effect=[
            {"results": [{"recipient_name": "LOCKHEED MARTIN CORPORATION"}]},
            {"results": [
                {"Award ID": "LM-1", "Recipient Name": "LOCKHEED MARTIN CORP.", "Transaction Amount": 20},
                {"Award ID": "SIK-1", "Recipient Name": "SIKORSKY AIRCRAFT CORPORATION", "Transaction Amount": 10},
            ]},
        ]):
            result = tools.execute("defense.us_contracts", {
                "fiscal_year": 2025, "agency": "Department of Defense",
                "recipient": "Lockheed Martin Corporation", "limit": 5,
            })
        self.assertEqual(["LM-1"], [row["Award ID"] for row in result["data"]["items"]])
        self.assertEqual(1, result["data"]["excluded_recipient_count"])

    def test_usaspending_compiler_preserves_user_entities(self):
        compiled = tools.compile_provider_arguments(
            "gov.us_spending", "NASAからSpaceXへ出た契約を探して",
            {"recipient": "Acme Research", "agency": "National Aeronautics and Space Administration"},
        )
        self.assertEqual("Acme Research", compiled["recipient"])

    def test_xml_sanctions_adapters_search_bounded_entries(self):
        xml = b'''<root><INDIVIDUAL><FIRST_NAME>Kim</FIRST_NAME><SECOND_NAME>North Korea</SECOND_NAME><REFERENCE_NUMBER>KP-1</REFERENCE_NUMBER></INDIVIDUAL><sdnEntry><lastName>Iran Air</lastName><uid>OFAC-1</uid><sdnType>Entity</sdnType></sdnEntry></root>'''
        with patch.object(tools, "_xml_request", return_value=xml):
            unsc = tools.execute("sanctions.unsc", {"query": "DPRK", "limit": 5})
            ofac = tools.execute("sanctions.us_ofac", {"query": "Iran Air", "limit": 5})
        self.assertEqual("DATA", unsc["data"]["semantic_status"])
        self.assertEqual("KP-1", unsc["data"]["items"][0]["reference_number"])
        self.assertEqual("DATA", ofac["data"]["semantic_status"])
        self.assertEqual("Iran Air", ofac["data"]["items"][0]["name"])

    def test_idb_ckan_adapter_normalizes_records(self):
        payload = {"success": True, "result": {"total": 1, "records": [{"Project Number": "BR-L1", "Country": "Brazil"}]}}
        with patch.object(tools, "_request", return_value=payload) as request:
            result = tools.execute("development.idb", {"country": "Brazil", "limit": 5})
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual("BR-L1", result["data"]["items"][0]["Project Number"])
        self.assertEqual("Brazil", request.call_args.args[2]["q"])

    def test_marinecadastre_identify_adapter_uses_bounded_web_mercator(self):
        with patch.object(tools, "_request", return_value={"results": [{"value": 12, "geometry": {"x": -8238310, "y": 4970071}}]}) as request:
            result = tools.execute("maritime.us_vessel_traffic", {"year": 2023, "latitude": 40.7128, "longitude": -74.006, "limit": 5})
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual("coast.noaa.gov", request.call_args.args[0])
        self.assertIn("AISVesselTransitCounts2023", request.call_args.args[1])
        self.assertIn("geometry", request.call_args.args[2])

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
        studies = [{"protocolSection": {
            "identificationModule": {"nctId": f"NCT-{index}", "briefTitle": title},
            "conditionsModule": {"conditions": conditions},
            "statusModule": {"overallStatus": status},
        }} for index, (title, conditions, status) in enumerate((
            ("Alzheimer study", ["Alzheimer Disease"], "RECRUITING"),
            ("Infant motor screening", ["Motor Skills"], "RECRUITING"),
            ("Alzheimer study closed", ["Alzheimer Disease"], "WITHDRAWN"),
        ))]
        with patch.object(tools, "_request", return_value={"studies": studies}):
            result = tools.execute("health.clinical_trials", {"query": "Alzheimer", "recruitment_status": "recruiting"})
        self.assertEqual(1, len(result["data"]["items"]))
        self.assertEqual("NCT-0", result["data"]["items"][0]["nct_id"])
        self.assertEqual("DATA", result["data"]["semantic_status"])

    def test_clinical_trials_drops_condition_false_positives_before_applying_limit(self):
        studies = [
            {"protocolSection": {"identificationModule": {"nctId": "NCT-BAD", "briefTitle": "Infant motor screening"},
                                  "conditionsModule": {"conditions": ["Infant Development"]}}},
            {"protocolSection": {"identificationModule": {"nctId": "NCT-GOOD", "briefTitle": "Alzheimer treatment"},
                                  "conditionsModule": {"conditions": ["Alzheimer Disease"]}}},
        ]
        with patch.object(tools, "_request", return_value={"studies": studies}):
            result = tools.execute("health.clinical_trials", {"query": "Alzheimer", "limit": 1})
        self.assertEqual(["NCT-GOOD"], [row["nct_id"] for row in result["data"]["items"]])

    def test_open_sky_location_questions_use_a_bounding_box_and_render_fields(self):
        captured = {}
        states = {"states": [
            ["abc001", "JA1", "Japan", 1, 1, 139.78, 35.55, 800, False, 120, 90, -1],
            ["def002", "FAR", "United States", 1, 1, -85.75, 27.73, 12000, False, 220, 151, 0],
        ]}
        def fake_request(host, path, params, headers=None):
            captured.update(host=host, path=path, params=params)
            return states
        with patch.object(tools, "_request", side_effect=fake_request):
            result = tools.execute("aviation.live_state", {"query": "今、羽田周辺の航空機を見せて"})
        self.assertEqual("opensky-network.org", captured["host"])
        self.assertEqual("/api/states/all", captured["path"])
        self.assertLess(captured["params"]["lamin"], 35.55)
        self.assertGreater(captured["params"]["lamax"], 35.55)
        self.assertEqual(["abc001"], [row["icao24"] for row in result["data"]["items"]])
        # Exercise the same canonical-ID dispatch used by live chat.  Calling
        # _new_provider_summary directly would miss regressions where the
        # dispatcher forgets the historical adapter alias.
        rendered = external_tool_summary({
            "tool_id": "aviation.opensky", "provider": "OpenSky", "data": result["data"]
        })
        self.assertIn("JA1", rendered)
        self.assertIn("高度 800 m", rendered)
        self.assertIn("OpenSky Network", rendered)
        self.assertNotIn("1. Result", rendered)
        self.assertNotIn("FAR", rendered)

    def test_oecd_senior_employment_fetches_comparable_latest_country_rates(self):
        args = tools.compile_provider_arguments(
            "statistics.oecd", "OECD諸国で高齢者の就業率がどう違うか、直近年で見たい。", {})
        captured = {}
        payload = {"data": {
            "dataSets": [{"observations": {"0:0:0:0:0:0:0": [54.52]}}],
            "structures": [{"dimensions": {"observation": [
                {"id": "REF_AREA", "values": [{"id": "JPN"}]},
                {"id": "MEASURE", "values": [{"id": "EMP_RATIO"}]},
                {"id": "UNIT_MEASURE", "values": [{"id": "PT_POP_SUB"}]},
                {"id": "SEX", "values": [{"id": "_T"}]},
                {"id": "AGE", "values": [{"id": "Y65T69"}]},
                {"id": "LABOUR_FORCE_STATUS", "values": [{"id": "EMP"}]},
                {"id": "TIME_PERIOD", "values": [{"id": "2025"}]},
            ]}}],
        }}
        def fake_request(host, path, params, headers=None):
            captured.update(host=host, path=path, params=params)
            return payload
        with patch.object(tools, "_request", side_effect=fake_request):
            result = tools.execute("statistics.oecd", args)
        self.assertEqual("sdmx.oecd.org", captured["host"])
        self.assertIn("DSD_LFS@DF_LFS_INDIC", captured["path"])
        self.assertIn(".EMP_RATIO.PT_POP_SUB._T.Y65T69.EMP", captured["path"])
        self.assertIn("JPN", captured["path"])
        self.assertEqual("54.52", str(result["data"]["items"][0]["value"]))
        self.assertEqual("Japan", result["data"]["items"][0]["country"])
        self.assertEqual("%", result["data"]["items"][0]["unit"])
        rendered = external_tool_summary(result)
        self.assertIn("65〜69歳", rendered)
        self.assertIn("Japan · 2025：54.52%", rendered)

    def test_eia_state_gasoline_query_fetches_and_keeps_latest_state_series(self):
        payload = {"response": {"data": [
            {"period": "2026-09-28", "value": "3.25", "units": "$/GAL",
             "duoarea": "SCA", "area-name": "CALIFORNIA", "series": "EMM_EPM0_PTE_SCA_DPG",
             "series-description": "California retail gasoline prices"},
            {"period": "2026-09-21", "value": "3.20", "units": "$/GAL",
             "duoarea": "SCA", "area-name": "CALIFORNIA", "series": "EMM_EPM0_PTE_SCA_DPG",
             "series-description": "California retail gasoline prices"},
            {"period": "2026-09-28", "value": "2.80", "units": "$/GAL",
             "duoarea": "STX", "area-name": "TEXAS", "series": "EMM_EPM0_PTE_STX_DPG",
             "series-description": "Texas retail gasoline prices"},
        ]}}
        captured = {}
        with patch.dict("os.environ", {"OLCR_EIA_API_KEY": "test-key"}), \
             patch.object(tools, "_request", side_effect=lambda host, path, params, headers=None: captured.update(
                 host=host, path=path, params=params) or payload):
            result = tools.execute("energy.us_eia", {
                "dataset": "gasoline_retail", "state_level": True, "limit": 50,
            })
        self.assertEqual("api.eia.gov", captured["host"])
        self.assertIn("EMM_EPM0_PTE_SCA_DPG", captured["params"]["facets[series][]"])
        self.assertEqual(["CALIFORNIA", "TEXAS"], [row["area"] for row in result["data"]["items"]])
        self.assertEqual(["2026-09-28", "2026-09-28"], [row["period"] for row in result["data"]["items"]])
        self.assertEqual("3.25", result["data"]["items"][0]["value"])

    def test_country_profile_compiles_and_fans_out_two_countries(self):
        args = tools.compile_provider_arguments(
            "geo.country_facts", "フランスと日本で、通貨・公用語・人口規模を比べて。", {})
        self.assertEqual(["FR", "JP"], [item["country"] for item in args["requests"]])
        calls = []
        def fake_http(adapter, child_args):
            calls.append(child_args["country"])
            country = child_args["country"]
            data = {"name": "France" if country == "FR" else "Japan",
                    "capital": "Paris" if country == "FR" else "Tokyo",
                    "population": 68000000 if country == "FR" else 125000000,
                    "currencies": [{"name": "Euro" if country == "FR" else "Japanese yen"}],
                    "languages": [{"name": "French" if country == "FR" else "Japanese"}]}
            return tools._result(adapter, country, data, [{"title": "fixture", "provider": "fixture",
                "canonical_url": "https://example.test/", "retrieved_at": "2026-01-01T00:00:00Z"}])
        with patch.object(tools, "_simple_http", side_effect=fake_http):
            result = tools.execute("geo.country_facts", args)
        self.assertEqual(["FR", "JP"], calls)
        rendered = external_tool_summary(result)
        self.assertIn("France", rendered)
        self.assertIn("Japan", rendered)
        self.assertIn("French", rendered)
        self.assertIn("Japanese", rendered)

    def test_provider_envelopes_without_measurements_are_not_reported_as_data(self):
        imf = tools._external_result("econ.imf", "inflation", {"values": {"PCPIPCH": {"JPN": None, "USA": None}}}, "https://example.test/")
        ecb = tools._external_result("finance.ecb", "FX", {"header": {"structure": {"dimensions": {}}}}, "https://example.test/")
        no_data = tools._external_result("maritime.us_vessel_traffic", "AIS", {
            "results": [{"attributes": {"UniqueValue.Pixel Value": "NoData"}}]}, "https://example.test/")
        self.assertEqual("EMPTY", imf["data"]["semantic_status"])
        self.assertEqual("no_data", imf["status"])
        self.assertEqual("EMPTY", ecb["data"]["semantic_status"])
        self.assertEqual("no_data", ecb["status"])
        self.assertEqual("EMPTY", no_data["data"]["semantic_status"])
        self.assertFalse(no_data["data"]["has_payload"])

    def test_eia_natural_gas_production_and_consumption_are_two_distinct_gets(self):
        args = tools.compile_provider_arguments("energy.us_eia", "今年の米国の天然ガスの生産と消費の推移を比べて", {})
        self.assertEqual("natural_gas_production_and_consumption", args["dataset"])
        calls = []
        def fake_request(host, path, params, headers=None):
            calls.append((path, dict(params)))
            return {"response": {"data": [{"period": "2026-06", "value": "123", "units": "MMCF"}]}}
        with patch.object(tools, "_request", side_effect=fake_request), patch.object(tools, "_provider_credential", return_value=("test-key", None)):
            result = tools.execute("energy.us_eia", args)
        self.assertEqual(2, len(calls))
        self.assertIn("natural-gas/prod/sum/data", calls[0][0])
        self.assertIn("natural-gas/cons/sum/data", calls[1][0])
        now = __import__("datetime").datetime.now().astimezone()
        self.assertEqual(f"{now.year}-01", calls[0][1].get("start"))
        self.assertEqual(now.strftime("%Y-%m"), calls[0][1].get("end"))
        self.assertEqual(["N9070US2", "VGT"], [call[1].get("facets[series][]", call[1].get("facets[process][]")) for call in calls])
        rendered = external_tool_summary(result)
        self.assertIn("【生産】", rendered)
        self.assertIn("【消費】", rendered)

    def test_eia_year_to_date_analysis_uses_only_requested_year_observations(self):
        current_year = __import__("datetime").datetime.now().astimezone().year
        prior_year = current_year - 1
        analysis = _deterministic_eia_analysis([{
            "tool_id": "energy.us_eia", "sources": [{"source_id": "S1"}],
            "data": {"items": [
                {"measure": "natural_gas_production", "period": f"{prior_year}-12", "value": 99, "unit": "MMCF"},
                {"measure": "natural_gas_production", "period": f"{current_year}-01", "value": 100, "unit": "MMCF"},
                {"measure": "natural_gas_production", "period": f"{current_year}-02", "value": 110, "unit": "MMCF"},
                {"measure": "natural_gas_consumption", "period": f"{current_year}-01", "value": 90, "unit": "MMCF"},
                {"measure": "natural_gas_consumption", "period": f"{current_year}-02", "value": 95, "unit": "MMCF"},
            ]},
        }], "今年の米国の天然ガス生産と消費")
        self.assertIn(f"{current_year}-01", analysis)
        self.assertIn(f"{current_year}-02", analysis)
        self.assertNotIn(f"{prior_year}-12", analysis)
        self.assertIn("出典 [S1]", analysis)

    def test_eia_petroleum_imports_and_exports_are_monthly_separate_gets(self):
        prompt = "エネルギー輸入依存度を考えるため、米国の石油輸出入の月次変化を調べて。"
        args = tools.compile_provider_arguments("energy.us_eia", prompt, {})
        self.assertEqual("petroleum_imports_and_exports", args["dataset"])
        calls = []

        def fake_request(host, path, params, headers=None):
            calls.append((host, path, dict(params)))
            return {"response": {"data": [{"period": "2026-06", "value": "123", "units": "thousand barrels"}]}}

        with patch.object(tools, "_request", side_effect=fake_request), \
             patch.object(tools, "_provider_credential", return_value=("test-key", None)):
            result = tools.execute("energy.us_eia", args)
        self.assertEqual(["/v2/petroleum/move/imp/data/", "/v2/petroleum/move/exp/data/"],
                         [call[1] for call in calls])
        self.assertTrue(all(call[2].get("frequency") == "monthly" for call in calls))
        self.assertEqual(["輸入", "輸出"], [row["direction"] for row in result["data"]["items"]])
        rendered = external_tool_summary(result)
        self.assertIn("【輸入】", rendered)
        self.assertIn("【輸出】", rendered)

    def test_typed_renderers_keep_multiple_observations_and_suppress_unscoped_unhcr_rows(self):
        elevation = _new_provider_summary({"tool_id": "geo.elevation", "provider": "GSI", "data": {"items": [
            {"location": "京都駅", "elevation_m": 30.08}, {"location": "大阪駅", "elevation_m": 0.74}]}})
        sunrise = _new_provider_summary({"tool_id": "astronomy.sunrise", "provider": "Sunrise", "data": {"items": [
            {"location": "Tokyo", "sunrise": "2026-10-10T05:41:40+09:00", "sunset": "2026-10-10T17:14:26+09:00", "day_length": 41566},
            {"location": "Sapporo", "sunrise": "2026-10-10T05:40:27+09:00", "sunset": "2026-10-10T17:02:55+09:00", "day_length": 40948}]}})
        unhcr = _new_provider_summary({"tool_id": "humanitarian.unhcr", "provider": "UNHCR", "data": {"items": [
            {"year": 1951, "coo_name": "-", "coa_name": "-", "refugees": 2000}]}})
        self.assertIn("京都駅", elevation)
        self.assertIn("大阪駅", elevation)
        self.assertIn("標高差", elevation)
        self.assertIn("Tokyo", sunrise)
        self.assertIn("Sapporo", sunrise)
        self.assertIn("日照時間の差", sunrise)
        self.assertIn("内訳がなく", unhcr)
        self.assertNotIn("1951", unhcr)

    def test_qiita_uses_small_pages_under_shared_response_byte_limit(self):
        with patch.object(tools, "_request", return_value=[]) as request:
            result = tools.execute("knowledge.qiita", {"query": "AI agent", "limit": 3})
        self.assertEqual("EMPTY", result["data"]["semantic_status"])
        self.assertEqual(10, request.call_args.args[2]["per_page"])

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
