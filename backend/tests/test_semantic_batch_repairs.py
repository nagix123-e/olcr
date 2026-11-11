"""Offline counterexamples from the 269-case semantic review."""
import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch, Mock

from olcr_api import external_tools as tools
from olcr_api.app import _new_provider_summary, external_tool_summary
from olcr_api.runtime import Runtime


class SemanticBatchRepairs(unittest.TestCase):
    def test_composer_preserves_date_and_both_long_comparison_records(self):
        generate = Mock(return_value={"text": "fixture answer"})
        runtime = SimpleNamespace(settings=SimpleNamespace(main_model="fixture"), _generate_brain=generate,
                                  context_budget=lambda *_args: 4096,
                                  _finish=lambda task, answer, started: (task, answer))
        rows = [{"name": name, "text": "x" * 20000} for name in ("first", "second")]
        Runtime.compose_tool_result(runtime, "比較して", {"tool_id": "fixture", "provider": "fixture",
                                    "data": {"semantic_status": "DATA", "items": rows}, "sources": []})
        messages = generate.call_args.args[0]
        packet = json.loads(messages[-1]["content"].split("[TOOL_RESULT_UNTRUSTED]\n", 1)[1])
        self.assertIn("current_time_utc", packet["execution_context"])
        self.assertEqual(["first", "second"], [row["name"] for row in packet["data"]["items"]])
        self.assertTrue(packet["evidence_truncated"])

    def test_land_surface_uses_geometry_not_longitude_guess(self):
        polygon = [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]
        with patch.object(tools, "_land_polygons", return_value=[polygon]):
            self.assertEqual("land", tools._land_surface(5, 5))
            self.assertEqual("sea", tools._land_surface(5, 20))
            self.assertEqual("coastal_uncertain", tools._land_surface(5, .05))

    def test_walking_route_uses_foot_dataset_and_steps(self):
        with patch.object(tools, "_request", return_value={"routes": [{"distance": 100, "duration": 80}]}) as request:
            tools._osrm({"start": "139,35", "end": "139.1,35.1", "profile": "walking", "steps": True})
        self.assertEqual("routing.openstreetmap.de", request.call_args.args[0])
        self.assertIn("/routed-foot/", request.call_args.args[1])
        self.assertEqual("true", request.call_args.args[2]["steps"])

    def test_null_evidence_is_not_false_truncation(self):
        value = {"items": [{"name": "a", "optional": None}], "next": None}
        self.assertEqual(value, tools._bounded_provider_value(value))

    def test_empty_exact_fact_provider_is_still_empty(self):
        result = tools._result("space.exoplanets", "q", {"items": []}, [])
        self.assertEqual("EMPTY", result["data"]["semantic_status"])

    def test_trivia_initializes_its_own_payload(self):
        fixture = {"response_code": 0, "results": [{"question": "Question?", "type": "multiple",
                    "difficulty": "easy", "category": "Science", "correct_answer": "A",
                    "incorrect_answers": ["B", "C", "D"]}]}
        with patch.object(tools, "_request", return_value=fixture):
            result = tools._simple_http("games.trivia", {"limit": 1, "hide_answer": True})
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertIn("Question?", str(result["data"]))
        self.assertNotIn("correct_answer", result["data"]["items"][0])

    def test_exoplanet_radius_ranking_reachable(self):
        fixture = [{"pl_name": "large", "pl_rade": 1.2, "pl_orbper": 3},
                   {"pl_name": "small", "pl_rade": .5, "pl_orbper": 4}]
        with patch.object(tools, "_request", return_value=fixture):
            result = tools._simple_http("space.exoplanets", {"limit": 2, "earth_like_radius": True})
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual("large", result["data"]["items"][0]["pl_name"])

    def test_pikachu_uses_outgoing_evolution_edge(self):
        pichu = {"species": {"name": "pichu"}, "evolves_to": [{"species": {"name": "pikachu"},
            "evolution_details": [{"min_happiness": 220}], "evolves_to": [{"species": {"name": "raichu"},
                "evolution_details": [{"item": {"name": "thunder-stone"}, "min_happiness": None}], "evolves_to": []}]}]}
        fixtures = [{"name": "pikachu", "species": {"name": "pikachu", "url": "https://pokeapi.co/api/v2/pokemon-species/25/"},
                     "stats": [{"base_stat": 90, "stat": {"name": "speed"}}]},
                    {"evolution_chain": {"url": "https://pokeapi.co/api/v2/evolution-chain/10/"}}, {"chain": pichu}]
        with patch.object(tools, "_request", side_effect=fixtures):
            result = tools._provider_details("games.pokemon", {"name": "pikachu", "include_evolution": True}, "pikachu", 1)
        edges = result["data"]["items"][0]["evolutions"]
        self.assertEqual("pikachu", edges[0]["from"])
        self.assertEqual("raichu", edges[0]["to"])
        answer = external_tool_summary(result)
        self.assertIn("かみなりのいし", answer)
        self.assertNotIn("220", answer)
        self.assertIn("speed 90", answer)

    def test_ecb_quote_direction_and_inverse(self):
        result = {"tool_id": "finance.ecb", "provider": "ECB", "data": {"base": "EUR", "quote": "USD",
            "items": [{"time_period": "2026-10-01", "value": 1.1}, {"time_period": "2026-10-02", "value": 1.2}]}}
        answer = _new_provider_summary(result)
        self.assertIn("1 EUR = 1.2 USD", answer)
        self.assertIn("ユーロ高", answer)
        self.assertIn("1 USD = 0.833333 EUR", answer)

    def test_ecb_bad_number_does_not_crash(self):
        result = {"tool_id": "finance.ecb", "provider": "ECB", "data": {"quote": "JPY",
            "items": [{"value": "not-a-number"}, {"value": float("nan")}, {"value": 160}]}}
        answer = _new_provider_summary(result)
        self.assertIn("1 EUR = 160 JPY", answer)
        self.assertNotIn("nan JPY", answer)

    def test_ofac_only_renders_official_alias_and_program(self):
        result = {"tool_id": "sanctions.us_ofac", "provider": "OFAC", "data": {"items": [{"name": "IRAN AIR",
            "aliases": ["IRANAIR"], "programs": ["IRAN"]}]}}
        answer = _new_provider_summary(result)
        self.assertIn("IRANAIR", answer)
        self.assertIn("IRAN", answer)
        self.assertNotIn("SDGT", answer)

    def test_ofac_named_entity_does_not_match_unrelated_remarks(self):
        xml = b'<sdnList><sdnEntry><lastName>OTHER ENTITY</lastName><remarks>Iran air operations</remarks></sdnEntry><sdnEntry><lastName>IRAN AIR</lastName><programList><program>IRAN</program></programList><akaList><aka><lastName>IRANAIR</lastName></aka></akaList></sdnEntry></sdnList>'
        rows = tools._search_sanctions_xml(xml, "Iran Air", {"sdnentry"}, {"last_name", "lastname", "name"}, 5)
        self.assertEqual(["IRAN AIR"], [row["name"] for row in rows])
        self.assertEqual(["IRANAIR"], rows[0]["aliases"])

    def test_bad_iss_coordinates_become_provider_error(self):
        with patch.object(tools, "_request", return_value={"latitude": "bad"}):
            with self.assertRaisesRegex(tools.ExternalToolError, "PROVIDER_BAD_RESPONSE"):
                tools._provider_details("space.iss_position", {}, "ISS", 1)

    def test_metar_wind_units_and_tl_are_not_guessed(self):
        result = {"tool_id": "aviation.operational_weather", "provider": "AWC", "data": {"items": [{
            "rawOb": "RJTT 101200Z 04014KT 9999 TEMPO TL1000", "wdir": 40, "wspd": 14}]}}
        answer = _new_provider_summary(result)
        self.assertIn("14 kt", answer)
        self.assertIn("終了時刻", answer)
        self.assertIn("視程10 km以上", answer)
        self.assertNotIn("北西", answer)

    def test_gbif_keeps_scientific_label(self):
        result = {"tool_id": "biology.occurrence", "provider": "GBIF", "data": {"items": [{
            "scientificName": "Parus minor", "eventDate": "2026-10-01", "decimalLatitude": 35.7, "decimalLongitude": 139.77}]}}
        self.assertIn("Parus minor", _new_provider_summary(result))

    def test_tides_use_selected_day_and_station_timezone(self):
        captured = {}
        def request(host, path, params):
            captured.update(params)
            return {"predictions": [{"t": "2026-10-11 12:00", "v": "1.1", "type": "H"}]}
        with patch.object(tools, "_request", side_effect=request):
            tools._simple_http("marine.tides_currents", {"station": "9414290", "date": "2026-10-11"})
        self.assertEqual("20261011", captured["begin_date"])
        self.assertEqual("20261011", captured["end_date"])
        self.assertNotIn("date", captured)
        self.assertEqual("lst_ldt", captured["time_zone"])

    def test_law_extracts_article_not_table_of_contents(self):
        listing = '<DataRoot><LawNameListInfo><LawName>個人情報保護法</LawName><LawId>id</LawId></LawNameListInfo></DataRoot>'
        detail = '<Law><TOC><TOCArticle><ArticleCaption>利用目的</ArticleCaption><ArticleTitle>目次</ArticleTitle></TOCArticle></TOC><LawBody><MainProvision><Article Num="17"><ArticleCaption>利用目的</ArticleCaption><ArticleTitle>第十七条</ArticleTitle><Paragraph><ParagraphSentence><Sentence>実際の条文本体。</Sentence></ParagraphSentence></Paragraph></Article></MainProvision></LawBody></Law>'
        with patch.object(tools, "_xml_request", side_effect=[listing, detail]):
            result = tools._simple_http("japan.law", {"query": "個人情報保護法", "request_text": "利用目的の条文"})
        articles = result["data"]["items"][0]["articles"]
        self.assertEqual("実際の条文本体。", articles[0]["text"])
        self.assertEqual("17", articles[0]["article_number"])
        self.assertTrue(result["data"]["request_complete"])

    def test_uniprot_length_does_not_remove_other_features(self):
        record = {"primaryAccession": "P00533", "sequence": {"length": 1210, "value": "M" * 1210},
            "comments": [{"commentType": "SUBCELLULAR LOCATION"}, {"commentType": "FUNCTION"}],
            "features": [{"type": "Active site"}]}
        with patch.object(tools, "_request", return_value={"results": [record]}):
            result = tools._provider_details("biology.protein", {"accession": "P00533", "request_text": "配列長"}, "EGFR", 1)
        row = result["data"]["items"][0]
        self.assertEqual(1210, row["sequence_length"])
        self.assertEqual("FUNCTION", row["comments"][0]["commentType"])
        self.assertEqual("Active site", row["features"][0]["type"])
        self.assertNotIn("value", row["sequence"])

    def test_japan_trial_requires_actual_japan_location(self):
        def study(identifier, country):
            return {"protocolSection": {"identificationModule": {"nctId": identifier, "briefTitle": "Type 2 Diabetes Trial"},
                "conditionsModule": {"conditions": ["Type 2 Diabetes"]}, "statusModule": {"overallStatus": "RECRUITING"},
                "contactsLocationsModule": {"locations": [{"country": country, "status": "RECRUITING"}]}}}
        with patch.object(tools, "_request", return_value={"studies": [study("wrong", "United States"), study("right", "Japan")]}) as request:
            result = tools._simple_http("health.clinical_trials", {"condition": "type 2 diabetes", "location": "Japan", "recruitment_status": "RECRUITING"})
        self.assertEqual(["right"], [row["nct_id"] for row in result["data"]["items"]])
        self.assertIn("AREA[LocationCountry]Japan", request.call_args.args[2]["filter.advanced"])

    def test_fda_reports_are_matched_to_requested_drug(self):
        fixture = {"results": [{"safetyreportid": "unrelated", "patient": {"drug": [{"medicinalproduct": "acetaminophen"}]}},
            {"safetyreportid": "matched", "patient": {"drug": [{"medicinalproduct": "IBUPROFEN"}], "reaction": [{"reactionmeddrapt": "Headache"}]}}]}
        with patch.object(tools, "_request", return_value=fixture):
            result = tools._simple_http("health.fda_data", {"drug": "ibuprofen", "event_type": "adverse_event"})
        self.assertEqual(["matched"], [row["report_id"] for row in result["data"]["items"]])
        self.assertEqual(["Headache"], result["data"]["items"][0]["reactions"])

    def test_ueno_does_not_assume_chiyoda_or_species(self):
        args = tools._refine_provider_arguments("biology.occurrence", "上野公園周辺の鳥の観測記録", {"stateProvince": "千代田区", "scientificName": "bird"})
        self.assertNotIn("stateProvince", args)
        self.assertNotIn("scientificName", args)
        self.assertEqual(212, args["taxonKey"])

    def test_recent_disaster_dates_are_visible(self):
        result = {"tool_id": "earth.natural_event", "provider": "NASA EONET", "data": {"items": [{
            "title": "Past event", "categories": [{"title": "Floods"}], "geometry": [{"date": "2025-11-01", "coordinates": [100, 10]}]}]}}
        answer = external_tool_summary(result)
        self.assertIn("2025-11-01", answer)
        self.assertIn("保証しません", answer)

    def test_southeast_asia_filters_event_coordinates(self):
        args = tools._refine_provider_arguments("earth.natural_event", "東南アジアで進行中の災害の開始日", {})
        from datetime import datetime, timezone
        observed = datetime.now(timezone.utc).date().isoformat()
        fixture = {"events": [
            {"id": "sea", "title": "regional", "geometry": [{"type": "Point", "coordinates": [100, 10], "date": observed}]},
            {"id": "usa", "title": "unrelated", "geometry": [{"type": "Point", "coordinates": [-100, 30], "date": "2026-10-11"}]}]}
        with patch.object(tools, "_request", return_value=fixture):
            result = tools._simple_http("earth.natural_event", args)
        self.assertEqual(["sea"], [row["id"] for row in result["data"]["items"]])
        self.assertEqual(observed, result["data"]["items"][0]["date"])


if __name__ == "__main__":
    unittest.main()
