import json
import unittest
from unittest.mock import MagicMock, patch

from olcr_api import external_tools
from olcr_api.api_acceptance import PRODUCTION_EXCLUDED_API_IDS, production_tool_ids


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
    def test_congress_list_records_survive_optional_detail_provider_failure(self):
        def response(_host, path, _params, *_args):
            if path == "/v3/bill/119":
                return {"bills": [{"title": "Artificial Intelligence Act", "type": "HR",
                                   "number": 100, "congress": 119}]}
            raise external_tools.ExternalToolError("PROVIDER_TIMEOUT")
        with patch.object(external_tools, "_provider_credential", return_value=("fixture-key", None)), \
             patch.object(external_tools, "_request", side_effect=response):
            result = external_tools.execute("politics.us_congress", {"query": "artificial intelligence", "limit": 1})
        self.assertEqual("DATA", result["data"]["semantic_status"])
        row = result["data"]["items"][0]
        self.assertEqual("Artificial Intelligence Act", row["title"])
        self.assertEqual({"details", "actions", "cosponsors"}, {entry["field"] for entry in row["enrichment_failures"]})
        self.assertTrue(result["sources"])

    def test_diet_transcript_compiler_extracts_topics_from_natural_questions(self):
        cases = {
            "国会で生成AIの規制について、政府側はどんな答弁をしている？": "生成AI",
            "最近の国会質疑で、少子化対策をめぐって与野党の論点がどう違ったか調べて。": "少子化対策",
            "防災に関する大臣答弁を、発言者と会議日が分かる形で探して。": "防災",
            "国会でマイナンバー制度の課題が取り上げられた最近のやり取りを見たい。": "マイナンバー制度 課題",
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                compiled = external_tools.compile_provider_arguments("japan.diet_transcript", prompt)
                self.assertEqual(expected, compiled["query"])
        self.assertEqual("大臣", external_tools.compile_provider_arguments(
            "japan.diet_transcript", "国会で生成AIの規制について、政府側はどんな答弁をしている？"
        )["speaker_position"])
        self.assertEqual("大臣", external_tools.compile_provider_arguments(
            "japan.diet_transcript", "防災に関する大臣答弁を、発言者と会議日が分かる形で探して。"
        )["speaker_position"])

    def test_diet_transcript_adapter_preserves_speech_records_and_renders_evidence(self):
        from olcr_api.app import _new_provider_summary

        payload = {"numberOfRecords": 21, "speechRecord": [{
            "date": "2026-07-16", "nameOfHouse": "参議院", "nameOfMeeting": "経済産業委員会",
            "issue": "第14号", "speaker": "竹詰仁", "speakerGroup": "国民民主党・新緑風会",
            "speech": "○竹詰仁君　生成ＡＩを含む新たなデジタル技術の導入について質問します。",
            "speechURL": "https://kokkai.ndl.go.jp/txt/example", "speechID": "example-1",
        }]}
        with patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools._simple_http("japan.diet_transcript", {"query": "生成AI 規制", "limit": 5})
        self.assertEqual("本文", request.call_args.args[2]["searchRange"])
        self.assertEqual(1, result["data"]["item_count"])
        self.assertIn("生成ＡＩ", result["data"]["items"][0]["speech_excerpt"])
        rendered = _new_provider_summary(result)
        self.assertIn("2026-07-16", rendered)
        self.assertIn("竹詰仁", rendered)
        self.assertIn("生成ＡＩ", rendered)
        self.assertIn("https://kokkai.ndl.go.jp/txt/example", rendered)

    def test_diet_transcript_prioritizes_actual_ministers_over_ministry_officials(self):
        payload = {"numberOfRecords": 21, "speechRecord": [
            {"date": "2026-07-24", "speaker": "宮下匡之", "speakerPosition": "外務省大臣官房儀典長",
             "speech": "○政府参考人（宮下匡之君）　防災施策を説明します。"},
            {"date": "2026-07-22", "speaker": "貫名功二", "speakerPosition": "内閣府大臣官房審議官",
             "speech": "○政府参考人（貫名功二君）　防災計画を説明します。"},
            {"date": "2026-07-22", "speaker": "金子恭之", "speakerPosition": "国土交通大臣",
             "speech": "○金子国務大臣　防災・減災に取り組みます。", "speechURL": "https://example.test/minister"},
        ]}
        with patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools._simple_http("japan.diet_transcript", {
                "query": "防災", "limit": 5, "speaker_position": "大臣",
            })
        self.assertEqual(20, request.call_args.args[2]["maximumRecords"])
        self.assertEqual(["金子恭之"], [row["speaker"] for row in result["data"]["items"]])
        self.assertEqual("国土交通大臣", result["data"]["items"][0]["speaker_position"])

    def test_iss_fuzzy_angles_route_to_position_api(self):
        prompts = (
            "ISSは今、陸地の上と海の上のどちらを飛んでいる？",
            "今の位置から、ISSが日本の近くへ来る方向を知りたい。",
            "ISSの現在地の緯度・経度を地図に載せられる形で教えて。",
            "今ISSがいる場所を、近い都市や地域の目安と一緒に説明して。",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual("space.iss_position", external_tools.route(prompt)[0])

    def test_iss_renderer_formats_epoch_and_map_coordinates_without_overclaiming(self):
        from olcr_api.app import _new_provider_summary

        base = {"tool_id": "space.iss_position", "provider": "Where The ISS At",
                "query": "ISSは今、陸地の上と海の上のどちらを飛んでいる？",
                "data": {"latitude": 26.1443, "longitude": 44.4039, "altitude": 421.6,
                         "velocity": 27582, "timestamp": 1791549355}}
        rendered = _new_provider_summary(base)
        self.assertIn("陸地／海上の判定", rendered)
        self.assertIn("2026-10-09", rendered)
        self.assertIn("地図用座標：26.144300, 44.403900", rendered)
        self.assertIn("openstreetmap.org", rendered)

        direction = dict(base, query="今の位置から、ISSが日本の近くへ来る方向を知りたい。")
        self.assertIn("単一時点の位置", _new_provider_summary(direction))

        locality = dict(base, query="今ISSがいる場所を、近い都市や地域の目安と一緒に説明して。")
        self.assertIn("逆ジオコーディング", _new_provider_summary(locality))

    def test_iss_gui_composer_passes_user_intent_to_typed_renderer(self):
        from olcr_api.app import compose_external_result

        prompt = "ISSは今、陸地の上と海の上のどちらを飛んでいる？"
        result = {
            "tool_id": "space.iss_position",
            "provider": "Where The ISS At",
            "response_kind": "geo_point",
            "sources": [{"provider": "Where The ISS At", "url": "https://example.test/iss"}],
            "data": {
                "semantic_status": "DATA",
                "latitude": 26.1443,
                "longitude": 44.4039,
                "altitude": 421.6,
                "velocity": 27582,
                "timestamp": 1791549355,
            },
        }

        _, rendered = compose_external_result(prompt, result)

        self.assertIn("地図用座標：26.144300, 44.403900", rendered)
        self.assertIn("陸地／海上の判定", rendered)
        self.assertNotIn("query", result)

    def test_space_weather_compiler_preserves_impact_intent_for_renderer(self):
        prompt = "今の太陽活動で、無線通信やGPSに影響が出そうな兆候はある？"

        compiled = external_tools.compile_provider_arguments("space.space_weather", prompt)

        self.assertIn("GPS", compiled["query"])
        self.assertIn("影響", compiled["query"])

    def test_launch_schedule_fuzzy_angles_route_and_compile(self):
        prompts = (
            "次に飛ぶ日本のロケットはいつで、どこから上がる予定？",
            "今月予定されている打ち上げを、運ぶもの別に見比べたい。",
            "直近の打ち上げ予定で、延期や変更が出ているものはある？",
            "火星探査に関係する次の打ち上げ計画があれば知りたい。",
            "When is the next rocket launch and where will it lift off?",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual("space.launches", external_tools.route(prompt)[0])
                compiled = external_tools.compile_provider_arguments("space.launches", prompt)
                self.assertTrue(compiled["upcoming"])
                self.assertGreaterEqual(compiled["limit"], 1)
        japan = external_tools.compile_provider_arguments("space.launches", prompts[0])
        self.assertEqual("JPN", japan["country_code"])
        monthly_payloads = external_tools.compile_provider_arguments("space.launches", prompts[1])
        self.assertTrue(monthly_payloads["group_by_payload"])
        self.assertEqual(10, len(monthly_payloads["date_from"]))
        self.assertEqual(10, len(monthly_payloads["date_to"]))
        changes = external_tools.compile_provider_arguments("space.launches", prompts[2])
        self.assertTrue(changes["explain_schedule_changes"])
        mars = external_tools.compile_provider_arguments("space.launches", prompts[3])
        self.assertEqual("Mars", mars["search"])

    def test_monthly_launch_query_normalizes_payloads_filters_month_and_groups_rendering(self):
        from olcr_api.app import external_tool_summary

        def launch(name, day, mission_name, mission_type, rocket):
            return {"name": name, "net": f"2026-10-{day:02d}T12:00:00Z", "url": f"https://ll.thespacedevs.com/{name}",
                    "status": {"name": "Go"}, "rocket": {"configuration": {"full_name": rocket}},
                    "mission": {"name": mission_name, "type": mission_type, "orbit": {"name": "LEO"}},
                    "launch_service_provider": {"name": "Example Agency"},
                    "pad": {"name": "Pad 1", "location": {"name": "Example Site", "country_code": "USA"}}}

        rows = [launch("Mission A", 10, "A payload", "Communications", "Rocket A"),
                launch("Mission B", 20, "B payload", "Earth observation", "Rocket B"),
                launch("Mission C", 31, "Out of month", "Unknown", "Rocket C")]
        rows[2]["net"] = "2026-11-01T12:00:00Z"
        with patch.object(external_tools, "_request", return_value={"results": rows}) as request:
            result = external_tools.execute("space.launches", {
                "limit": 10, "date_from": "2026-10-01", "date_to": "2026-10-31", "group_by_payload": True,
            })
        self.assertEqual(1, request.call_count)
        self.assertEqual(10, request.call_args.args[2]["limit"])
        self.assertEqual(2, result["data"]["item_count"])
        self.assertEqual("Communications", result["data"]["items"][0]["payload_type"])
        self.assertEqual("Rocket A", result["data"]["items"][0]["rocket"])
        rendered = external_tool_summary(result)
        self.assertIn("運ぶもの：Communications", rendered)
        self.assertIn("運ぶもの：Earth observation", rendered)
        self.assertNotIn("{'configuration'", rendered)
        self.assertNotIn("Out of month", rendered)

    def test_launch_schedule_change_question_does_not_claim_history_without_evidence(self):
        from olcr_api.app import external_tool_summary

        prompt = "直近の打ち上げ予定で、延期や変更が出ているものはある？"
        arguments = external_tools.compile_provider_arguments("space.launches", prompt)
        payload = {"tool_id": "space.launches", "data": {
            "items": [{"name": "Example Launch", "net": "2026-10-10T00:00:00Z",
                       "status": "Go for Launch", "last_updated": "2026-10-09T12:00:00Z",
                       "url": "https://example.test/launch"}],
            "explain_schedule_changes": arguments["explain_schedule_changes"],
        }}
        rendered = external_tool_summary(payload)
        self.assertIn("公開ステータス", rendered)
        self.assertIn("現在の予定とAPIに含まれる公表更新", rendered)
        self.assertIn("延期の証明ではありません", rendered)
        self.assertIn("情報更新：2026-10-09T12:00:00Z", rendered)

    def test_mars_launch_query_uses_provider_search(self):
        arguments = external_tools.compile_provider_arguments(
            "space.launches", "火星探査に関係する次の打ち上げ計画があれば知りたい。")
        def launch(name, mission_name):
            return {"name": name, "net": "2026-12-31T00:00:00Z", "url": "https://example.test/launch",
                    "status": {"name": "TBD"}, "mission": {"name": mission_name},
                    "rocket": {"configuration": {"full_name": "Rocket"}}}
        with patch.object(external_tools, "_request", return_value={"results": [
                launch("Falcon 9 | unrelated", "Inmarsat-7 F1"),
                launch("Terran R | Mars Lander", "Mars Lander"),
            ]}) as request:
            result = external_tools.execute("space.launches", arguments)
        self.assertEqual("Mars", request.call_args.args[2]["search"])
        self.assertEqual(["Mars Lander"], [row["mission"] for row in result["data"]["items"]])

    def test_launch_schedule_country_filter_is_provider_and_evidence_checked(self):
        from olcr_api.app import external_tool_summary

        launch_rows = [
            {"name": "H3 | Test Payload", "net": "2026-10-20T00:00:00Z",
             "status": {"name": "Go"}, "url": "https://ll.thespacedevs.com/launch/jp",
             "pad": {"name": "Yoshinobu Launch Complex", "location": {"name": "Tanegashima Space Center", "country_code": "JPN"}},
             "rocket": {"configuration": {"full_name": "H3"}}, "mission": {"name": "Test Payload"},
             "launch_service_provider": {"name": "JAXA"}},
            {"name": "Falcon 9 | Unrelated", "net": "2026-10-21T00:00:00Z",
             "pad": {"location": {"name": "Cape Canaveral", "country_code": "USA"}}},
        ]
        with patch.object(external_tools, "_request", side_effect=[
            {"results": [{"id": 34, "country_code": "JPN"}]},
            {"count": 2, "results": launch_rows},
        ]) as request:
            result = external_tools.execute("space.launches", {"limit": 3, "upcoming": True, "country_code": "JPN"})

        self.assertEqual(2, request.call_count)
        self.assertEqual("/2.2.0/location/", request.call_args_list[0].args[1])
        self.assertEqual("/2.2.0/launch/upcoming/", request.call_args_list[1].args[1])
        self.assertEqual("34", request.call_args_list[1].args[2]["location__ids"])
        self.assertEqual(["H3 | Test Payload"], [row["name"] for row in result["data"]["items"]])
        rendered = external_tool_summary(result)
        for expected in ("Tanegashima Space Center", "H3", "Test Payload", "JAXA"):
            self.assertIn(expected, rendered)

    def test_rdap_preserves_nested_registration_fields_and_renders_them(self):
        from olcr_api.app import external_tool_summary

        payload = {
            "objectClassName": "domain", "ldhName": "EXAMPLE.COM",
            "status": ["client transfer prohibited"],
            "nameservers": [{"ldhName": "A.IANA-SERVERS.NET"}, {"ldhName": "B.IANA-SERVERS.NET"}],
            "events": [
                {"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"},
                {"eventAction": "expiration", "eventDate": "2028-08-13T04:00:00Z"},
            ],
            "entities": [{"handle": "EXAMPLE-REG", "roles": ["registrar"],
                          "vcardArray": ["vcard", [["fn", {}, "text", "Example Registrar"]]]}],
        }

        result = external_tools._external_result(
            "internet.domain_registration", "example.com", payload, "https://rdap.verisign.com/com/v1/domain/EXAMPLE.COM")
        rendered = external_tool_summary(result)

        self.assertEqual("registered", result["data"]["registration_status"])
        self.assertEqual(["a.iana-servers.net", "b.iana-servers.net"], result["data"]["nameservers"])
        self.assertEqual("Example Registrar", result["data"]["registrar"])
        for expected in ("登録状況：registered", "a.iana-servers.net", "b.iana-servers.net",
                         "レジストラ（registrarロール）：Example Registrar", "expiration：2028-08-13T04:00:00Z"):
            self.assertIn(expected, rendered)

    def test_worldwide_holiday_query_uses_global_endpoint_and_filters_target_date(self):
        holidays = [
            {"date": "2026-10-08", "name": "Sample Day", "localName": "Sample Day", "countryCode": "AA", "types": ["Public"]},
            {"date": "2026-10-09", "name": "Tomorrow Day", "localName": "Tomorrow Day", "countryCode": "BB", "types": ["Public"]},
        ]
        with patch.object(external_tools, "_request", return_value=holidays) as request:
            result = external_tools.execute("calendar.public_holidays", {
                "scope": "worldwide", "date": "2026-10-08", "year": 2026,
            })
        request.assert_called_once()
        self.assertEqual("/api/v3/NextPublicHolidaysWorldwide", request.call_args.args[1])
        self.assertEqual(["AA"], result["data"]["countries"])
        self.assertEqual(["2026-10-08"], [item["date"] for item in result["data"]["items"]])

    def test_hacker_news_summary_shows_authors_points_and_links(self):
        from olcr_api.app import external_tool_summary

        result = {
            "tool_id": "news.hackernews", "provider": "Hacker News Firebase",
            "sources": [{"canonical_url": "https://news.ycombinator.com/"}],
            "data": {"items": [{
                "title": "Rust async error handling", "author": "reader42",
                "points": 123, "num_comments": 45,
                "url": "https://example.com/rust-async",
            }]},
        }
        rendered = external_tool_summary(result)
        for expected in ("Hacker News · stories", "Rust async error handling", "reader42", "score 123", "comments 45", "https://example.com/rust-async"):
            self.assertIn(expected, rendered)

    def test_idempotent_get_retries_one_transient_timeout(self):
        response = MagicMock(status_code=200, headers={"content-type": "application/json"}, content=b"{}")
        response.url = "https://api.worldbank.org/v2/country/jpn/indicator/EN.ATM.CO2E.PC"
        response.json.return_value = {"ok": True}
        first = MagicMock()
        first.__enter__.return_value = first
        first.get.side_effect = external_tools.httpx.ReadTimeout("temporary timeout")
        second = MagicMock()
        second.__enter__.return_value = second
        second.get.return_value = response
        with patch.object(external_tools.httpx, "Client", side_effect=[first, second]) as client_factory:
            payload = external_tools._request("api.worldbank.org", "/v2/country/jpn/indicator/EN.ATM.CO2E.PC", {})
        self.assertEqual({"ok": True}, payload)
        self.assertEqual(2, client_factory.call_count)
        first.get.assert_called_once()
        second.get.assert_called_once()

    def test_ucdp_summary_renders_event_fields_count_version_and_source(self):
        from olcr_api.app import external_tool_summary

        result = {
            "tool_id": "conflict.ucdp", "provider": "UCDP Georeferenced Event Dataset",
            "sources": [{"canonical_url": "https://ucdpapi.pcr.uu.se/api/gedevents/26.1", "provider": "UCDP Georeferenced Event Dataset"}],
            "data": {"items": [{
                "id": 559709, "date_start": "2025-01-03", "date_end": "2025-01-03",
                "country": "Ukraine", "adm_1": "Donetsk", "where_description": "Avdiivka",
                "type_of_violence": 1, "best": 12,
            }], "total_count": 24, "dataset_version": "26.1"},
        }

        rendered = external_tool_summary(result)

        for expected in ("2025-01-03", "Avdiivka", "国家間・国家内紛争", "12人", "表示件数：1件", "該当総数：24件", "26.1", "559709"):
            self.assertIn(expected, rendered)
        self.assertNotIn("1. 559709\n", rendered)

    def test_noaa_space_weather_renders_latest_case_sensitive_kp_and_storm_level(self):
        from olcr_api.app import external_tool_summary

        result = {
            "tool_id": "space.space_weather", "provider": "NOAA SWPC",
            "data": {"items": [
                {"time_tag": "2026-09-30T00:00:00", "Kp": 1.0, "a_running": 4, "station_count": 8},
                {"time_tag": "2026-09-30T03:00:00", "Kp": 5.0, "a_running": 48, "station_count": 8},
            ]},
        }

        rendered = external_tool_summary(result)

        self.assertIn("Kp指数 5", rendered)
        self.assertIn("地磁気嵐レベル G1", rendered)
        self.assertIn("2026-09-30T03:00:00 UTC", rendered)
        self.assertNotIn("Kp指数 1", rendered)

    def test_noaa_space_weather_answers_requested_gps_and_radio_impact_with_limits(self):
        from olcr_api.app import external_tool_summary

        result = {
            "tool_id": "space.space_weather", "provider": "NOAA SWPC",
            "query_summary": "今の太陽活動で、無線通信やGPSに影響が出そうな兆候はある？",
            "data": {
                "query": "今の太陽活動で、無線通信やGPSに影響が出そうな兆候はある？",
                "items": [{"time_tag": "2026-10-09T06:00:00", "Kp": 2.0}],
            },
        }

        rendered = external_tool_summary(result)

        self.assertIn("Kp指数 2", rendered)
        self.assertIn("GPS・無線への影響を示す兆候は見られません", rendered)
        self.assertIn("短波通信", rendered)
        self.assertIn("Kp値だけでは", rendered)

    def test_noaa_space_weather_does_not_add_impact_analysis_to_plain_status_query(self):
        from olcr_api.app import external_tool_summary

        result = {
            "tool_id": "space.space_weather", "provider": "NOAA SWPC",
            "query_summary": "現在の宇宙天気を調べて",
            "data": {"items": [{"time_tag": "2026-10-09T06:00:00", "Kp": 2.0}]},
        }

        rendered = external_tool_summary(result)

        self.assertIn("Kp指数 2", rendered)
        self.assertNotIn("影響の目安", rendered)

    def test_noaa_space_weather_explains_aurora_scope_without_overclaiming(self):
        from olcr_api.app import external_tool_summary

        result = {
            "tool_id": "space.space_weather", "provider": "NOAA SWPC",
            "query_summary": "オーロラを見られる可能性が高まりそうな宇宙天気か、今の状況から教えて。",
            "data": {"query": "オーロラを見られる可能性が高まりそうな宇宙天気か、今の状況から教えて。",
                     "items": [{"time_tag": "2026-10-09T06:00:00", "Kp": 2.0}]},
        }

        rendered = external_tool_summary(result)

        self.assertIn("オーロラの目安", rendered)
        self.assertIn("可能性の上昇は確認できません", rendered)
        self.assertIn("地域別のオーロラ予報ではありません", rendered)

    def test_noaa_space_weather_compares_requested_recent_kp_trend_to_previous_sample(self):
        from olcr_api.app import external_tool_summary

        result = {
            "tool_id": "space.space_weather", "provider": "NOAA SWPC",
            "query_summary": "Kp指数が普段より上がっているか、現在値を基準と一緒に知りたい。",
            "data": {"query": "Kp指数が普段より上がっているか、現在値を基準と一緒に知りたい。",
                     "items": [
                         {"time_tag": "2026-10-09T06:00:00", "Kp": 2.0},
                         {"time_tag": "2026-10-09T09:00:00", "Kp": 4.0},
                     ]},
        }

        rendered = external_tool_summary(result)

        self.assertIn("直前の観測値 Kp 2 から上昇", rendered)
        self.assertIn("長期的な平常値との比較ではありません", rendered)
        self.assertIn("Kp 5以上がG1", rendered)

    def test_noaa_space_weather_explains_satellite_impact_data_limits(self):
        from olcr_api.app import external_tool_summary

        result = {
            "tool_id": "space.space_weather", "provider": "NOAA SWPC",
            "query_summary": "宇宙天気の今の状態を、衛星運用への影響という観点で短く説明して。",
            "data": {"query": "宇宙天気の今の状態を、衛星運用への影響という観点で短く説明して。",
                     "items": [{"time_tag": "2026-10-09T06:00:00", "Kp": 2.0}]},
        }

        rendered = external_tool_summary(result)

        self.assertIn("衛星運用の目安", rendered)
        self.assertIn("地磁気嵐の基準未満", rendered)
        self.assertIn("放射線、帯電、大気抵抗", rendered)

    def test_satellite_orbit_compiler_keeps_both_comparison_targets_and_full_request(self):
        comparison = "ISSとハッブル宇宙望遠鏡の軌道要素を並べて比べたい。"
        arguments = external_tools.compile_provider_arguments("space.satellite_orbit", comparison)
        self.assertEqual("ISS,Hubble", arguments["name"])
        self.assertEqual(comparison.rstrip("。"), arguments["request_text"])

    def test_satellite_orbit_fetches_exact_catalog_ids_and_normalizes_tle(self):
        iss = "ISS (ZARYA)\n1 25544U 98067A   26281.98258931  .00005946  00000+0  11685-3 0  9991\n2 25544  51.6313  96.9458 0006821 239.5445 120.4870 15.48782794589392"
        hubble = "HST\n1 20580U 90037B   26281.67127014  .00005552  00000+0  16789-3 0  9991\n2 20580  28.4734  24.2725 0001576 348.7905  11.2656 15.31864315805980"
        arguments = external_tools.compile_provider_arguments(
            "space.satellite_orbit", "ISSとハッブル宇宙望遠鏡の軌道要素を並べて比べたい。")
        with patch.object(external_tools, "_text_request", side_effect=[iss, hubble]) as request:
            result = external_tools.execute("space.satellite_orbit", arguments)
        self.assertEqual(["25544", "20580"], [call.args[2]["CATNR"] for call in request.call_args_list])
        self.assertEqual(["ISS (ZARYA)", "HST"], [item["name"] for item in result["data"]["items"]])
        self.assertEqual("2026-10-08T23:34:55.716384+00:00", result["data"]["items"][0]["epoch"])
        self.assertTrue(all(item["tle_line1"].startswith("1 ") and item["tle_line2"].startswith("2 ")
                            for item in result["data"]["items"]))

    def test_satellite_orbit_renderer_shows_tle_and_honest_query_limits(self):
        from olcr_api.app import external_tool_summary

        rendered = external_tool_summary({
            "tool_id": "space.satellite_orbit", "provider": "CelesTrak GP Data",
            "sources": [{"provider": "CelesTrak GP Data", "canonical_url": "https://celestrak.org/NORAD/elements/gp.php"}],
            "data": {"request_text": "ISSの軌道が最近変わったか確認できる最新の要素を出して。",
                     "items": [{"name": "ISS (ZARYA)", "norad_id": "25544", "epoch": "2026-10-08T23:34:55+00:00",
                                "tle_line1": "1 25544U 98067A   26281.98258931  .00005946  00000+0  11685-3 0  9991",
                                "tle_line2": "2 25544  51.6313  96.9458 0006821 239.5445 120.4870 15.48782794589392"}]},
        })
        self.assertIn("ISS (ZARYA)（NORAD 25544）", rendered)
        self.assertIn("要素エポック", rendered)
        self.assertIn("軌道傾斜角：51.6313°", rendered)
        self.assertIn("1 25544U", rendered)
        self.assertIn("2 25544", rendered)
        self.assertIn("過去TLEとの時系列比較は含まれない", rendered)
        self.assertIn("出典：https://celestrak.org/NORAD/elements/gp.php", rendered)

    def test_ephemeris_compiles_relative_end_of_year_and_multiple_bodies(self):
        from datetime import datetime, timezone

        prompt = "今年の年末ごろ、地球から火星までどれくらい離れている？"
        compiled = external_tools.compile_provider_arguments("space.ephemeris", prompt)
        self.assertEqual(["Mars"], compiled["targets"])
        self.assertEqual(f"{datetime.now(timezone.utc).year}-12-31", compiled["epoch"])

        comparison = external_tools.compile_provider_arguments(
            "space.ephemeris", "来月の同じ時刻で、金星と火星のどちらが地球に近いか比べて。")
        self.assertEqual(["Venus", "Mars"], comparison["targets"])
        self.assertRegex(comparison["epoch"], r"^20\d\d-\d\d-\d\d$")

        # Natural-language dates and all named bodies outrank incomplete
        # arguments proposed by the fuzzy router.
        explicit = external_tools.compile_provider_arguments(
            "space.ephemeris", "2027年1月15日の木星の地心距離を計算して。",
            {"target": "Mars", "targets": ["Mars"], "epoch": "2026-10-09"})
        self.assertEqual(["Jupiter"], explicit["targets"])
        self.assertEqual("2027-01-15", explicit["epoch"])
        relative = external_tools.compile_provider_arguments(
            "space.ephemeris", "今年の年末ごろ、地球から火星までどれくらい離れている？",
            {"target": "Venus", "epoch": "2026-10-09"})
        self.assertEqual(["Mars"], relative["targets"])
        self.assertEqual(f"{datetime.now(timezone.utc).year}-12-31", relative["epoch"])

    def test_ephemeris_quarterly_request_expands_four_dates_and_renders_each(self):
        from datetime import date
        from olcr_api.app import external_tool_summary

        prompt = "火星までの距離が一年の中で大きく変わる時期を調べるため、四半期ごとの値を出して。"
        arguments = external_tools.compile_provider_arguments("space.ephemeris", prompt)
        year = int(arguments["epoch"][:4])
        expected = [f"{year}-{month:02d}-01" for month in (1, 4, 7, 10)]
        self.assertEqual(expected, arguments["epochs"])

        def horizons(_host, _path, params, _headers=None):
            target = params["COMMAND"].strip("'")
            epoch = params["START_TIME"].strip("'")
            day = date.fromisoformat(epoch).strftime("%Y-%b-%d")
            return {"result": f"$$SOE\\n2460000.5 = A.D. {day} 00:00:00.0000 TDB\\n"
                               " X =1.0E+08 Y =2.0E+07 Z =1.0E+06\\n$$EOE"}

        with patch.object(external_tools, "_request", side_effect=horizons) as request:
            result = external_tools.execute("space.ephemeris", arguments)
        self.assertEqual(4, request.call_count)
        self.assertEqual(expected, result["data"]["epochs"])
        self.assertEqual(expected, [row["epoch_date"] for row in result["data"]["items"]])
        rendered = external_tool_summary(result)
        for quarter_date in expected:
            self.assertIn(quarter_date, rendered)
        self.assertIn("四半期の各月初をサンプル日", rendered)

    def test_ephemeris_fetches_and_renders_typed_earth_centered_distance(self):
        from olcr_api.app import external_tool_summary

        raw = {"result": "$$SOE\n2461405.500000000 = A.D. 2026-Dec-31 00:00:00.0000 TDB\n"
                          " X =-1.288548384260275E+08 Y = 4.850673201062699E+07 Z = 7.789775903505091E+06\n"
                          " VX=1 VY=2 VZ=3\n$$EOE"}
        prompt = "今年の年末ごろ、地球から火星までどれくらい離れている？"
        arguments = external_tools.compile_provider_arguments("space.ephemeris", prompt)
        with patch.object(external_tools, "_request", return_value=raw) as request:
            result = external_tools.execute("space.ephemeris", arguments)
        self.assertEqual("'499'", request.call_args.args[2]["COMMAND"])
        self.assertEqual("'2026-12-31'", request.call_args.args[2]["START_TIME"])
        item = result["data"]["items"][0]
        self.assertEqual("Mars", item["name"])
        self.assertEqual("2026-Dec-31 00:00 TDB", item["epoch"])
        self.assertGreater(item["distance_km"], 100_000_000)

        rendered = external_tool_summary(result)
        self.assertIn("JPL Horizons（2026-12-31）", rendered)
        self.assertIn("JPL Horizons · 地球中心からの天体距離", rendered)
        self.assertIn("Mars：", rendered)
        self.assertIn("km", rendered)
        self.assertIn("JPL HorizonsのTDB", rendered)

    def test_reliefweb_uses_current_v2_reports_api_and_latest_preset(self):
        with patch.object(external_tools, "_request") as request:
            with self.assertRaisesRegex(external_tools.ExternalToolError, "PROVIDER_NOT_IN_PRODUCTION"):
                external_tools.execute("humanitarian.reliefweb", {"query": "Sudan humanitarian reports"})
        request.assert_not_called()
        self.assertIsNone(external_tools.route("ReliefWebでスーダンの最新人道支援レポートを探して"))

    def test_reliefweb_missing_approved_appname_stops_before_provider_request(self):
        from olcr_api.external_tools import ExternalToolError

        with patch.object(external_tools, "_provider_credential", return_value=("", None)), \
             patch.object(external_tools, "_request") as request:
            with self.assertRaises(ExternalToolError) as raised:
                external_tools.execute("humanitarian.reliefweb", {"query": "Sudan reports"})

        self.assertEqual("PROVIDER_NOT_IN_PRODUCTION", str(raised.exception))
        request.assert_not_called()
        self.assertIsNone(external_tools.credential_setup("humanitarian.reliefweb"))

    def test_european_parliament_searches_documents_and_never_renders_mep_records(self):
        from olcr_api.app import external_tool_summary

        prompt = "欧州議会でAI Actに関連する最近の議会文書を探して"
        payload = {"data": [
            {"type": "Work", "id": "eli/dl/doc/TA-9-2023-0236", "document_date": "2023-06-14",
             "is_realized_by": [
                 {"type": "Expression", "id": "eli/dl/doc/TA-9-2023-0236/fr", "title": {"fr": "Législation sur l’intelligence artificielle"}},
                 {"type": "Expression", "id": "eli/dl/doc/TA-9-2023-0236/en", "title": {"en": "Artificial Intelligence Act"}},
             ]},
            {"type": "Work", "id": "eli/dl/doc/TA-9-2024-0138", "document_date": "2024-03-13",
             "is_realized_by": [{"type": "Expression", "id": "eli/dl/doc/TA-9-2024-0138/en",
                                  "title": {"en": "Artificial Intelligence Act"}}]},
            {"type": "Person", "id": "person/96834", "title": {"en": "MEP record"}},
        ]}
        with patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("politics.eu_parliament", {"query": prompt, "limit": 5})

        self.assertEqual("/api/v2/adopted-texts", request.call_args.args[1])
        self.assertEqual("Artificial Intelligence Act", request.call_args.args[2]["title"])
        self.assertEqual("en", request.call_args.args[2]["search-language"])
        self.assertEqual("application/ld+json", request.call_args.args[2]["format"])
        self.assertEqual("application/ld+json", request.call_args.args[3]["Accept"])
        self.assertEqual(5, request.call_args.args[2]["limit"])
        self.assertEqual(2, len(result["data"]["items"]))
        self.assertEqual("TA-9-2024-0138", result["data"]["items"][0]["identifier"])
        self.assertEqual("2024-03-13", result["data"]["items"][0]["date"])
        rendered = external_tool_summary(result)
        self.assertIn("Artificial Intelligence Act", rendered)
        self.assertIn("TA-9-2024-0138", rendered)
        self.assertIn("2024-03-13", rendered)
        self.assertNotIn("person/96834", rendered)
        self.assertNotIn("MEP record", rendered)

    def test_uk_parliament_uses_bills_api_for_ai_legislation_search(self):
        from olcr_api.app import external_tool_summary

        prompt = "英国議会でAIに関係する最近の法案や審議を探して"
        payload = {"items": [{
            "billId": 123, "shortTitle": "Artificial Intelligence (Regulation) Bill",
            "lastUpdate": "2026-09-25T12:00:00Z",
            "currentStage": {"description": "House of Commons, 2nd reading"},
        }], "totalResults": 1}
        with patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("politics.uk_parliament", {"query": prompt, "limit": 5})

        self.assertEqual("bills-api.parliament.uk", request.call_args.args[0])
        self.assertEqual("/api/v1/Bills", request.call_args.args[1])
        self.assertEqual("artificial intelligence", request.call_args.args[2]["SearchTerm"])
        self.assertEqual(5, request.call_args.args[2]["Take"])
        rendered = external_tool_summary(result)
        self.assertIn("Artificial Intelligence (Regulation) Bill", rendered)
        self.assertIn("House of Commons, 2nd reading", rendered)
        self.assertIn("https://bills.parliament.uk/bills/123", rendered)

    def test_courtlistener_uses_v4_and_normalizes_recent_opinions(self):
        from olcr_api.app import external_tool_summary

        prompt = "CourtListenerでSection 230に言及する最近の判例を探して"
        payload = {"count": 1, "results": [{
            "caseName": "Example v. Platform", "dateFiled": "2026-09-29",
            "court": "U.S. Court of Appeals", "citation": ["123 F.4th 456"],
            "absolute_url": "/opinion/123/example-v-platform/",
        }]}
        with patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("law.us_courts", {"query": prompt, "limit": 5})

        self.assertEqual("www.courtlistener.com", request.call_args.args[0])
        self.assertEqual("/api/rest/v4/search/", request.call_args.args[1])
        self.assertEqual('"Section 230"', request.call_args.args[2]["q"])
        self.assertEqual("o", request.call_args.args[2]["type"])
        self.assertEqual(5, request.call_args.args[2]["page_size"])
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual(1, len(result["data"]["items"]))
        rendered = external_tool_summary(result)
        self.assertIn("Example v. Platform", rendered)
        self.assertIn("2026-09-29", rendered)
        self.assertIn("https://www.courtlistener.com/opinion/123/example-v-platform/", rendered)

    def test_wayback_respects_requested_year_and_filters_unrelated_snapshots(self):
        from olcr_api.app import external_tool_summary

        prompt = "openai.comの2023年のWayback Machineスナップショットをいくつか探して"
        compiled = external_tools.compile_provider_arguments(
            "web.archive_search", prompt, {"url": "https://openai.com/"}
        )
        self.assertEqual("2023", compiled["year"])
        payload = [
            ["timestamp", "original", "statuscode", "digest"],
            ["20231201010203", "https://openai.com/", "200", "new"],
            ["20010706030108", "http://www.openai.com:80/", "200", "old"],
        ]
        with patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute(
                "web.archive_search", {"url": "https://openai.com/", "year": "2023", "limit": 5}
            )

        self.assertEqual("2023", request.call_args.args[2]["from"])
        self.assertEqual("2023", request.call_args.args[2]["to"])
        self.assertEqual("reverse", request.call_args.args[2]["sort"])
        self.assertEqual(["20231201010203"], [row["timestamp"] for row in result["data"]["items"]])
        rendered = external_tool_summary(result)
        self.assertIn("Wayback Machine", rendered)
        self.assertIn("https://web.archive.org/web/20231201010203/openai.com/", rendered)
        self.assertEqual(2, len(rendered.splitlines()))
        self.assertNotIn(" · https://openai.com/", rendered)

    def test_all_thirty_are_registered_and_have_status_rows(self):
        production_ids = set(production_tool_ids())
        rows = {row["tool_id"]: row for row in external_tools.status(True)}
        self.assertEqual(production_ids, rows.keys())
        self.assertFalse(PRODUCTION_EXCLUDED_API_IDS & rows.keys())

    def test_sec_edgar_does_not_require_an_api_key(self):
        self.assertNotIn("finance.sec_filings", {row["tool_id"] for row in external_tools.status(True)})
        self.assertIsNone(external_tools.route(CASES["finance.sec_filings"]))
        self.assertIsNone(external_tools._CREDENTIAL_ENV.get("finance.sec_filings"))

    def test_all_thirty_route_and_compile(self):
        for tool_id, text in CASES.items():
            with self.subTest(tool_id=tool_id):
                route = external_tools.route(text)
                if tool_id in PRODUCTION_EXCLUDED_API_IDS:
                    self.assertIsNone(route)
                    continue
                self.assertIsNotNone(route)
                self.assertEqual(tool_id, route[0])
                if tool_id == "biology.phylogeny":
                    # The provider name "Open Tree" is not a pair of taxa.
                    with self.assertRaises(external_tools.ExternalToolError):
                        external_tools.compile_provider_arguments(tool_id, text, route[1])
                    continue
                compiled = external_tools.compile_provider_arguments(tool_id, text, route[1])
                self.assertIsInstance(compiled, dict)

    def test_human_tp53_question_compiles_to_fielded_uniprot_search(self):
        prompt = "ヒトTP53のUniProt IDと主な機能を教えて"
        arguments = external_tools.compile_provider_arguments("biology.protein", prompt)
        self.assertEqual("gene_exact:TP53 AND organism_id:9606 AND reviewed:true", arguments["query"])
        self.assertEqual("ヒトTP53のUniProt IDと主な機能を教えて", arguments["request_text"])

    def test_tvmaze_episode_question_compiles_show_and_episode_numbers(self):
        prompt = "Breaking Badのシーズン1第1話のタイトルと放送日を教えて"
        arguments = external_tools.compile_provider_arguments("media.tv", prompt)
        self.assertEqual({"query": "Breaking Bad", "limit": 5, "season": 1, "episode": 1},
                         {key: value for key, value in arguments.items() if key != "request_text"})

    def test_tvmaze_fetches_and_renders_requested_episode_by_number(self):
        from olcr_api.app import external_tool_summary

        prompt = "Breaking Badのシーズン1第1話のタイトルと放送日を教えて"
        arguments = external_tools.compile_provider_arguments("media.tv", prompt)
        show = {"id": 169, "name": "Breaking Bad"}
        episode = {"id": 1, "name": "Pilot", "season": 1, "number": 1,
                   "airdate": "2008-01-20", "url": "https://www.tvmaze.com/episodes/1/breaking-bad-1x01-pilot"}
        with patch.object(external_tools, "_request", side_effect=[show, episode]) as request:
            result = external_tools.execute("media.tv", arguments)
        self.assertEqual("/singlesearch/shows", request.call_args_list[0].args[1])
        self.assertEqual("/shows/169/episodebynumber", request.call_args_list[1].args[1])
        self.assertEqual("DATA", result["data"]["semantic_status"])
        rendered = external_tool_summary({
            "tool_id": "media.tv", "provider": "TVmaze", "data": result["data"],
            "sources": result["sources"],
        })
        self.assertIn("Pilot", rendered)
        self.assertIn("2008-01-20", rendered)
        self.assertIn("https://www.tvmaze.com/episodes/1/breaking-bad-1x01-pilot", rendered)

    def test_tvmaze_compiles_natural_season_question_and_returns_every_episode(self):
        from olcr_api.app import external_tool_summary

        prompt = "Breaking Badのシーズン1は全何話？各話のタイトルを放送順に教えて。"
        arguments = external_tools.compile_provider_arguments("media.tv", prompt)
        self.assertEqual({"query": "Breaking Bad", "limit": 5, "season": 1},
                         {key: value for key, value in arguments.items() if key != "request_text"})
        original_angle = "Breaking Badの最初のシーズンは何話構成で、各話の公開順は？"
        self.assertEqual({"query": "Breaking Bad", "limit": 5, "season": 1},
                         {key: value for key, value in external_tools.compile_provider_arguments(
                             "media.tv", original_angle).items() if key != "request_text"})
        show = {"id": 169, "name": "Breaking Bad", "url": "https://www.tvmaze.com/shows/169/breaking-bad"}
        episodes = [
            {"name": f"Episode {number}", "season": 1, "number": number,
             "airdate": f"2008-01-{19 + number:02d}", "url": f"https://www.tvmaze.com/episodes/{number}"}
            for number in range(1, 8)
        ] + [{"name": "Special", "season": 0, "number": 1, "airdate": "2008-01-01"}]
        with patch.object(external_tools, "_request", side_effect=[show, episodes]) as request:
            result = external_tools.execute("media.tv", arguments)

        self.assertEqual("/singlesearch/shows", request.call_args_list[0].args[1])
        self.assertEqual("/shows/169/episodes", request.call_args_list[1].args[1])
        self.assertEqual({"specials": 1}, request.call_args_list[1].args[2])
        self.assertEqual(7, result["data"]["total_episodes"])
        self.assertEqual(list(range(1, 8)), [row["episode"] for row in result["data"]["items"]])
        rendered = external_tool_summary({"tool_id": "media.tv", "provider": "TVmaze",
                                          "data": result["data"], "sources": result["sources"]})
        self.assertIn("シーズン1：全7話", rendered)
        self.assertIn("シーズン1 第7話：Episode 7", rendered)
        self.assertNotIn("Special", rendered)

    def test_nasa_exoplanet_renderer_includes_radius_period_and_source(self):
        from olcr_api.app import external_tool_summary

        rendered = external_tool_summary({
            "tool_id": "space.exoplanets", "provider": "NASA Exoplanet Archive",
            "data": {"target": "TRAPPIST-1 e", "items": [{
                "pl_name": "TRAPPIST-1 e", "hostname": "TRAPPIST-1",
                "pl_rade": 0.92, "pl_orbper": 6.099615,
                "pl_rade_reflink": '<a refstr="AGOL_ET_AL__2021" href= target=ref>Agol et al. 2021</a>',
                "pl_orbper_reflink": '<a refstr="AGOL_ET_AL__2021" href= target=ref>Agol et al. 2021</a>',
            }]},
            "sources": [{"provider": "NASA Exoplanet Archive",
                         "canonical_url": "https://exoplanetarchive.ipac.caltech.edu/"}],
        })
        self.assertIn("TRAPPIST-1 e", rendered)
        self.assertIn("半径：0.92 地球半径", rendered)
        self.assertIn("公転周期：6.099615 日", rendered)
        self.assertIn("Agol et al. 2021", rendered)
        self.assertNotIn("<a", rendered)
        self.assertNotIn("href=", rendered)
        self.assertIn("https://exoplanetarchive.ipac.caltech.edu/", rendered)

    def test_nasa_exoplanet_query_uses_one_row_composite_parameters(self):
        prompt = "TRAPPIST-1 eの半径と公転周期を教えて"
        arguments = external_tools.compile_provider_arguments("space.exoplanets", prompt)
        payload = [{"pl_name": "TRAPPIST-1 e", "hostname": "TRAPPIST-1",
                    "pl_rade": 0.92, "pl_orbper": 6.099615,
                    "pl_rade_reflink": "radius-ref", "pl_orbper_reflink": "period-ref"}]
        with patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("space.exoplanets", arguments)
        query = request.call_args.args[2]["query"]
        self.assertIn("from pscomppars", query)
        self.assertNotIn(" from ps where", query)
        self.assertIn("pl_rade", query)
        self.assertNotIn("_reflink", query)
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual("TRAPPIST-1 e", result["data"]["items"][0]["pl_name"])

    def test_nasa_exoplanet_system_prompt_compiles_to_host_filter(self):
        prompt = "TRAPPIST-1の惑星たちは、地球と比べると大きさや一年の長さがどう違う？"
        arguments = external_tools.compile_provider_arguments("space.exoplanets", prompt)
        self.assertEqual("TRAPPIST-1", arguments["target"])
        self.assertEqual("host", arguments["target_kind"])
        self.assertEqual(100, arguments["limit"])
        with patch.object(external_tools, "_request", return_value=[]) as request:
            external_tools.execute("space.exoplanets", arguments)
        query = request.call_args.args[2]["query"]
        self.assertIn("where hostname = 'TRAPPIST-1'", query)
        self.assertNotIn("where pl_name like", query)

    def test_nasa_exoplanet_host_system_request_keeps_all_rows_not_default_five(self):
        prompt = "TRAPPIST-1 eと同じ恒星を回る惑星をまとめて、基本的な違いを知りたい。"
        arguments = external_tools.compile_provider_arguments("space.exoplanets", prompt)
        payload = [{"pl_name": f"TRAPPIST-1 {letter}", "hostname": "TRAPPIST-1",
                    "pl_rade": 1.0, "pl_orbper": float(index + 1)}
                   for index, letter in enumerate("bcdefgh")]
        with patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("space.exoplanets", arguments)
        self.assertIn("select top 100", request.call_args.args[2]["query"])
        self.assertEqual(7, len(result["data"]["items"]))
        self.assertEqual("TRAPPIST-1 h", result["data"]["items"][-1]["pl_name"])

    def test_nasa_exoplanet_short_period_prompt_sorts_period_ascending(self):
        prompt = "公転周期が短い既知の系外惑星をいくつか見せて。"
        arguments = external_tools.compile_provider_arguments("space.exoplanets", prompt)
        self.assertTrue(arguments["sort_by_period"])
        with patch.object(external_tools, "_request", return_value=[]) as request:
            external_tools.execute("space.exoplanets", arguments)
        query = request.call_args.args[2]["query"]
        self.assertIn("pl_orbper is not null", query)
        self.assertIn("order by pl_orbper asc", query)

    def test_nasa_exoplanet_kepler_earth_sized_prompt_compiles_catalog_and_radius(self):
        prompt = "ケプラーで見つかった惑星のうち、半径が地球に近いものを探して。"
        arguments = external_tools.compile_provider_arguments("space.exoplanets", prompt)
        self.assertTrue(arguments["kepler_catalog"])
        self.assertTrue(arguments["earth_like_radius"])
        with patch.object(external_tools, "_request", return_value=[]) as request:
            external_tools.execute("space.exoplanets", arguments)
        query = request.call_args.args[2]["query"]
        self.assertIn("disc_facility like '%Kepler%'", query)
        self.assertIn("pl_rade between 0.8 and 1.2", query)
        self.assertIn("order by pl_rade asc", query)
        self.assertNotIn("abs(", query)

    def test_nasa_exoplanet_short_period_results_are_sorted_and_limited_locally(self):
        prompt = "公転周期が短い既知の系外惑星をいくつか見せて。"
        arguments = external_tools.compile_provider_arguments("space.exoplanets", prompt)
        payload = [
            {"pl_name": "Long", "pl_orbper": 20.0},
            {"pl_name": "Short B", "pl_orbper": 0.8},
            {"pl_name": "Short A", "pl_orbper": 0.2},
        ]
        with patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("space.exoplanets", arguments)
        query = request.call_args.args[2]["query"]
        self.assertIn("select top 100", query)
        self.assertEqual(["Short A", "Short B", "Long"],
                         [row["pl_name"] for row in result["data"]["items"]])

    def test_nasa_exoplanet_kepler_results_are_sorted_by_distance_to_earth(self):
        prompt = "ケプラーで見つかった惑星のうち、半径が地球に近いものを探して。"
        arguments = external_tools.compile_provider_arguments("space.exoplanets", prompt)
        payload = [
            {"pl_name": "Kepler-1", "pl_rade": 0.82},
            {"pl_name": "Kepler-2", "pl_rade": 1.14},
            {"pl_name": "Kepler-3", "pl_rade": 1.02},
        ]
        with patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("space.exoplanets", arguments)
        query = request.call_args.args[2]["query"]
        self.assertIn("select top 100", query)
        self.assertNotIn("_reflink", query)
        self.assertEqual(["Kepler-3", "Kepler-2", "Kepler-1"],
                         [row["pl_name"] for row in result["data"]["items"]])

    def test_nasa_exoplanet_same_host_prompt_returns_trappist_system(self):
        prompt = "TRAPPIST-1 eと同じ恒星を回る惑星をまとめて、基本的な違いを知りたい。"
        arguments = external_tools.compile_provider_arguments("space.exoplanets", prompt)
        self.assertEqual("TRAPPIST-1", arguments["target"])
        self.assertEqual("host", arguments["target_kind"])
        with patch.object(external_tools, "_request", return_value=[]) as request:
            external_tools.execute("space.exoplanets", arguments)
        query = request.call_args.args[2]["query"]
        self.assertIn("where hostname = 'TRAPPIST-1'", query)
        self.assertNotIn("pl_name like", query)

    def test_nasa_exoplanet_system_renderer_states_earth_comparison_baseline(self):
        from olcr_api.app import external_tool_summary

        rendered = external_tool_summary({
            "tool_id": "space.exoplanets", "provider": "NASA Exoplanet Archive",
            "query": "TRAPPIST-1の惑星たちは、地球と比べると大きさや一年の長さがどう違う？",
            "data": {"target": "TRAPPIST-1", "items": [{
                "pl_name": "TRAPPIST-1 e", "hostname": "TRAPPIST-1",
                "pl_rade": 0.92, "pl_orbper": 6.1,
            }]}, "sources": [],
        })
        self.assertIn("地球半径単位", rendered)
        self.assertIn("地球の1年は約365.26日", rendered)

    def test_nasa_exoplanet_success_uses_deterministic_renderer(self):
        from olcr_api import app as app_module

        result = external_tools._result("space.exoplanets", "TRAPPIST-1 e radius and period", {
            "target": "TRAPPIST-1 e", "items": [{"pl_name": "TRAPPIST-1 e",
                "hostname": "TRAPPIST-1", "pl_rade": 0.92, "pl_orbper": 6.099615}],
            "result_kind": "items", "has_payload": True, "item_count": 1,
            "evidence_field_count": 2, "semantic_status": "DATA",
        }, [{"title": "NASA Exoplanet Archive", "provider": "NASA Exoplanet Archive",
             "canonical_url": "https://exoplanetarchive.ipac.caltech.edu/"}])
        with patch.object(app_module.runtime, "compose_tool_result",
                          side_effect=AssertionError("provider evidence must use the deterministic renderer")):
            task, answer = app_module.compose_external_result("TRAPPIST-1 eの半径と公転周期を教えて", result)
        self.assertEqual("completed", task.state.value)
        self.assertIn("半径：0.92 地球半径", answer)
        self.assertIn("公転周期：6.099615 日", answer)

    def test_un_comtrade_compiles_export_countries_year_and_passenger_car_heading(self):
        prompt = "2024年の日本から米国への乗用車輸出額をUN Comtradeで教えて"
        arguments = external_tools.compile_provider_arguments("trade.un_comtrade", prompt)
        self.assertEqual(2024, arguments["year"])
        self.assertEqual("392", arguments["reporter_code"])
        self.assertEqual("842", arguments["partner_code"])
        self.assertEqual("X", arguments["flow_code"])
        self.assertEqual("8703", arguments["commodity_code"])

    def test_un_comtrade_normalizes_actual_primary_value_and_renders_deterministically(self):
        from olcr_api.app import external_tool_summary

        prompt = "2024年の日本から米国への乗用車輸出額をUN Comtradeで教えて"
        arguments = external_tools.compile_provider_arguments("trade.un_comtrade", prompt)
        payload = {"elapsedTime": "0.08 secs", "count": 1, "error": "", "data": [{
            "period": "2024", "refYear": 2024, "reporterCode": 392, "reporterDesc": None,
            "flowCode": "X", "partnerCode": 842, "partnerDesc": None, "cmdCode": "8703",
            "cmdDesc": None, "primaryValue": 39637679032.467, "isReported": False,
        }]}
        with patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("trade.un_comtrade", arguments)
        requested_params = request.call_args.args[2]
        self.assertEqual("8703", requested_params["cmdCode"])
        self.assertEqual("X", requested_params["flowCode"])
        self.assertEqual("2024", requested_params["period"])
        self.assertEqual("392", requested_params["reporterCode"])
        self.assertEqual("842", requested_params["partnerCode"])
        self.assertEqual(39637679032.467, result["data"]["items"][0]["trade_value_usd"])
        summary = external_tool_summary(result)
        self.assertIn("2024年", summary)
        self.assertIn("日本 → 米国", summary)
        self.assertIn("US$ 39,637,679,032", summary)
        self.assertIn("推計・集計値", summary)

    def test_un_comtrade_empty_result_is_truthful_and_bypasses_brain_composition(self):
        from olcr_api import app as app_module

        result = external_tools._result("trade.un_comtrade", "2024 Japan US passenger cars exports", {
            "query": "2024 Japan US passenger cars exports", "items": [], "result_kind": "items",
            "has_payload": False, "item_count": 0, "evidence_field_count": 0, "semantic_status": "EMPTY",
        }, [{"title": "UN Comtrade", "provider": "UN Comtrade",
             "canonical_url": "https://comtradeapi.un.org/"}])
        with patch.object(app_module.runtime, "compose_tool_result", side_effect=AssertionError("empty provider result must not be sent to the model")):
            task, answer = app_module.compose_external_result("UN Comtrade query", result)
        self.assertEqual("completed", task.state.value)
        self.assertIn("データが見つかりません", answer)

    def test_tokyo_kingfisher_question_compiles_to_gbif_filters(self):
        prompt = "東京で最近記録されたカワセミの観察記録を探して"
        arguments = external_tools.compile_provider_arguments("biology.occurrence", prompt)
        self.assertEqual("Alcedo atthis", arguments["scientificName"])
        self.assertEqual("Tokyo", arguments["stateProvince"])
        self.assertRegex(arguments["eventDate"], r"^\d{4}-\d{2}-\d{2},\d{4}-\d{2}-\d{2}$")

    def test_gbif_relevance_filter_matches_japanese_common_name_via_scientific_name(self):
        query = "東京で最近記録されたカワセミの観察記録を探して"
        payload = {"results": [{"scientificName": "Alcedo atthis (Linnaeus, 1758)",
                                "eventDate": "2026-01-24", "stateProvince": "Tokyo", "key": 6129882243}]}
        result = external_tools._new_relevance_result("biology.occurrence", query, payload, "https://www.gbif.org/")
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual(1, result["data"]["item_count"])

    def test_compiled_gbif_arguments_keep_records_when_original_query_is_not_forwarded(self):
        query = "東京で最近記録されたカワセミの観察記録を探して"
        arguments = external_tools.compile_provider_arguments("biology.occurrence", query)
        payload = {"count": 1, "results": [{
            "scientificName": "Alcedo atthis (Linnaeus, 1758)",
            "eventDate": "2026-01-24", "stateProvince": "Tokyo", "key": 6129882243,
        }]}
        with patch.object(external_tools, "_request", return_value=payload):
            result = external_tools.execute("biology.occurrence", arguments)
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual(1, result["data"]["item_count"])

    def test_gbif_renderer_shows_observation_date_location_and_source(self):
        from olcr_api.app import external_tool_summary

        rendered = external_tool_summary({
            "tool_id": "biology.occurrence", "provider": "GBIF",
            "data": {"items": [{"scientificName": "Alcedo atthis", "eventDate": "2026-01-24",
                                  "stateProvince": "Tokyo", "decimalLatitude": 35.68,
                                  "decimalLongitude": 139.51, "key": 6129882243}]},
            "sources": [],
        })
        self.assertIn("2026-01-24", rendered)
        self.assertIn("Tokyo", rendered)
        self.assertIn("35.68, 139.51", rendered)
        self.assertIn("https://www.gbif.org/occurrence/6129882243", rendered)

    def test_uniprot_renderer_shows_accession_and_function(self):
        from olcr_api.app import external_tool_summary

        rendered = external_tool_summary({
            "tool_id": "biology.protein", "provider": "UniProt",
            "data": {"items": [{
                "primaryAccession": "P04637",
                "genes": [{"geneName": {"value": "TP53"}}],
                "proteinDescription": {"recommendedName": {"fullName": {"value": "Cellular tumor antigen p53"}}},
                "comments": [{"commentType": "FUNCTION", "texts": [{"value": "Multifunctional transcription factor that induces cell cycle arrest, DNA repair or apoptosis."}]}],
            }]},
            "sources": [],
        })
        self.assertIn("P04637", rendered)
        self.assertIn("TP53", rendered)
        self.assertIn("主な機能：Multifunctional transcription factor", rendered)
        self.assertIn("主な機能：Multifunctional transcription factor that induces cell cycle arrest, DNA repair or apoptosis.\n", rendered)
        self.assertIn("https://www.uniprot.org/uniprotkb/P04637/entry", rendered)

    def test_launch_library_summary_includes_schedule_and_status(self):
        from olcr_api.app import external_tool_summary

        result = {
            "tool_id": "space.launches",
            "provider": "Launch Library 2",
            "data": {"items": [{
                "name": "Falcon 9 | Test Mission",
                "net": "2026-10-02T12:30:00Z",
                "status": {"name": "Go for Launch"},
                "url": "https://ll.thespacedevs.com/2.2.0/launch/example/",
            }]},
            "sources": [],
        }
        rendered = external_tool_summary(result)
        self.assertIn("Falcon 9 | Test Mission", rendered)
        self.assertIn("予定日時：2026-10-02T12:30:00Z", rendered)
        self.assertIn("状況：Go for Launch", rendered)
        self.assertIn("https://ll.thespacedevs.com/2.2.0/launch/example/", rendered)

    def test_fec_campaign_finance_paraphrases_route_to_the_live_provider(self):
        prompts = (
            "2024年米大統領選の主要候補の選挙資金収入を比較して",
            "2024年米大統領選の主要候補の選挙資金収入を比較して。各候補の収入額、対象期間、データの出典も示して。",
            "Compare 2024 presidential campaign finance receipts from FEC.",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual("politics.us_campaign_finance", external_tools.route(prompt)[0])

    def test_fred_and_eia_explicit_queries_route_to_live_providers(self):
        prompts = (
            ("FREDで米政策金利（Federal Funds Rate）の直近12か月の推移を見せて。", "econ.fred"),
            ("米国の政策金利FFRを過去24か月、月ごとにFREDから取得して", "econ.fred"),
            ("EIAで米国の原油在庫の最新値と最近の推移を見せて", "energy.us_eia"),
        )
        for prompt, expected_tool in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(expected_tool, external_tools.route(prompt)[0])
        compiled = external_tools.compile_provider_arguments("econ.fred", prompts[1][0])
        self.assertEqual("FEDFUNDS", compiled["series_id"])
        self.assertEqual(24, compiled["limit"])
        self.assertEqual(12, external_tools.compile_provider_arguments(
            "econ.fred", "FREDで米政策金利の最近の推移を見せて")["limit"])

    def test_opensanctions_compiles_entity_name_not_entire_japanese_prompt(self):
        prompt = "OpenSanctionsでRosneftに関連する制裁情報を確認して"
        for arguments in (None, {"query": prompt}):
            with self.subTest(arguments=arguments):
                compiled = external_tools.compile_provider_arguments(
                    "sanctions.opensanctions", prompt, arguments)
                self.assertEqual("Rosneft", compiled["query"])

    def test_opensanctions_uses_documented_auth_and_normalizes_search_results(self):
        from olcr_api import app as app_module
        from olcr_api.app import external_tool_summary

        key = "fixture-key-should-not-leak"
        payload = {"limit": 5, "total": 1, "results": [{
            "id": "NK-rosneft", "caption": "Open Joint-Stock Company Rosneft Oil Company", "schema": "Company",
            "properties": {"name": ["PJSC Rosneft"], "alias": ["Rosneft"], "country": ["ru"]},
            "topics": ["sanction", "export.risk"], "datasets": ["us_ofac_sdn"],
        }]}
        with patch.object(external_tools, "_provider_credential", return_value=(key, None)), \
             patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("sanctions.opensanctions", {"query": "Rosneft", "limit": 5})
        self.assertEqual("ApiKey " + key, request.call_args.args[3]["Authorization"])
        self.assertEqual(1, result["data"]["item_count"])
        self.assertEqual("Open Joint-Stock Company Rosneft Oil Company", result["data"]["items"][0]["name"])
        self.assertIn("sanction", json.dumps(result["data"]))
        summary = external_tool_summary(result)
        self.assertIn("Rosneft", summary)
        self.assertIn("us_ofac_sdn", summary)
        with patch.object(app_module.runtime, "compose_tool_result", side_effect=AssertionError("must not ask model to reinterpret provider records")):
            task, answer = app_module.compose_external_result("OpenSanctionsでRosneftに関連する制裁情報を確認して", result)
        self.assertEqual("completed", task.state.value)
        self.assertIn("Open Joint-Stock Company Rosneft Oil Company", answer)
        self.assertIn("us_ofac_sdn", answer)
        self.assertNotIn(key, json.dumps(result))

    def test_congress_compiler_honors_requested_five_not_internal_batch_size(self):
        compiled = external_tools.compile_provider_arguments(
            "politics.us_congress", "米議会でAIに関係する最近の法案を5件探して")
        self.assertEqual(5, compiled["limit"])

    @unittest.skip("BEA is intentionally excluded from the production surface until provider activation succeeds")
    def test_bea_compiles_year_and_normalizes_annual_real_gdp_rows(self):
        from olcr_api.app import external_tool_summary

        payload = {"BEAAPI": {"Results": {"Data": [
            {"LineDescription": "Percent change in real gross domestic product", "TimePeriod": "2025",
             "DataValue": "2.1", "UNIT": "Percent change from preceding year"},
            {"LineDescription": "Percent change in real gross domestic product", "TimePeriod": "2024",
             "DataValue": "2.8", "UNIT": "Percent change from preceding year"},
            {"LineDescription": "Gross domestic product", "TimePeriod": "2025", "DataValue": "30,000.0",
             "UNIT": "Billions of dollars"},
        ]}}}
        prompt = "BEAデータで米国の2025年実質GDP成長率を教えて"
        arguments = external_tools.compile_provider_arguments("econ.us_bea", prompt)
        self.assertEqual(2025, arguments["year"])
        with patch.object(external_tools, "_provider_credential", return_value=("fixture-key", None)), \
             patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("econ.us_bea", {"query": prompt, **arguments})
        params = request.call_args.args[2]
        self.assertEqual("NIPA", params["DataSetName"])
        self.assertEqual("T10101", params["TableName"])
        self.assertEqual("A", params["Frequency"])
        self.assertEqual("2025", params["Year"])
        self.assertEqual(1, result["data"]["item_count"])
        self.assertEqual("2.1", result["data"]["items"][0]["value"])
        self.assertIn("2.1", external_tool_summary(result))

    @unittest.skip("BEA is intentionally excluded from the production surface until provider activation succeeds")
    def test_bea_inactive_user_id_is_not_misreported_as_no_matching_data(self):
        payload = {"BEAAPI": {"Results": {"Error": {
            "APIErrorCode": "4", "APIErrorDescription": "This UserId is not active. Please activate it and try again.",
        }}}}
        with patch.object(external_tools, "_provider_credential", return_value=("fixture-key", None)), \
             patch.object(external_tools, "_request", return_value=payload):
            with self.assertRaises(external_tools.ExternalToolError) as caught:
                external_tools.execute("econ.us_bea", {"query": "BEA 2025 real GDP growth", "year": 2025})
        self.assertEqual("PROVIDER_CREDENTIAL_NOT_ACTIVE", str(caught.exception))
        from olcr_api.app import _external_tool_user_message
        message = _external_tool_user_message(str(caught.exception))
        self.assertIn("未有効化", message)
        self.assertIn("https://apps.bea.gov/api/signup/", message)

    def test_provider_rate_limit_has_specific_user_message(self):
        from olcr_api.app import _external_tool_user_message

        message = _external_tool_user_message("PROVIDER_RATE_LIMITED: retry after 12s")
        self.assertIn("リクエスト数を制限", message)
        self.assertIn("約12秒後", message)

    def test_provider_timeout_and_sec_access_denial_have_actionable_non_secret_messages(self):
        from olcr_api.app import _external_tool_user_message

        timeout = _external_tool_user_message("PROVIDER_TIMEOUT")
        self.assertIn("認証情報が無効という意味ではありません", timeout)
        denied = _external_tool_user_message("PROVIDER_ACCESS_DENIED", tool_id="finance.sec_filings")
        self.assertIn("HTTP 403", denied)
        self.assertIn("APIキー不要", denied)
        self.assertIn("https://www.sec.gov/about/webmaster-frequently-asked-questions", denied)

    def test_sec_edgar_403_is_classified_as_provider_access_denial(self):
        response = MagicMock(status_code=403, headers={"content-type": "text/html"})
        response.url = "https://data.sec.gov/submissions/CIK0000320193.json"
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.return_value = response
        with patch.object(external_tools.httpx, "Client", return_value=client):
            with self.assertRaises(external_tools.ExternalToolError) as caught:
                external_tools._request("data.sec.gov", "/submissions/CIK0000320193.json", {})
        self.assertEqual("PROVIDER_ACCESS_DENIED", str(caught.exception))

    def test_census_html_diagnostics_are_allowlisted_and_never_echo_response_body(self):
        from contextlib import redirect_stderr
        from io import StringIO

        cases = (
            ("<html>Invalid API key</html>", "KEY_REJECTED", "PROVIDER_AUTHENTICATION_FAILED"),
            ("<html>Cloudflare checking your browser</html>", "INTERSTITIAL_OR_WAF", "PROVIDER_BAD_RESPONSE"),
            ("<html>unknown error</html>", "UNCLASSIFIED_HTML", "PROVIDER_BAD_RESPONSE"),
        )
        for body, category, expected_error in cases:
            with self.subTest(category=category):
                response = MagicMock(
                    status_code=200,
                    headers={"content-type": "text/html"},
                    content=body.encode(),
                    text=body,
                )
                response.url = "https://api.census.gov/data/2024/acs/acs1"
                response.json.side_effect = ValueError("not JSON")
                client = MagicMock()
                client.__enter__.return_value = client
                client.get.return_value = response
                diagnostics = StringIO()
                with patch.object(external_tools.httpx, "Client", return_value=client), \
                     redirect_stderr(diagnostics):
                    with self.assertRaisesRegex(external_tools.ExternalToolError, expected_error):
                        external_tools._request("api.census.gov", "/data/2024/acs/acs1", {"key": "do-not-log"})
                self.assertIn(f"CENSUS_RESPONSE_DIAGNOSTIC={category}", diagnostics.getvalue())
                self.assertNotIn(body, diagnostics.getvalue())
                self.assertNotIn("do-not-log", diagnostics.getvalue())

    def test_census_rejected_key_has_actionable_non_secret_message(self):
        from olcr_api.app import _external_tool_user_message

        message = _external_tool_user_message("PROVIDER_AUTHENTICATION_FAILED")
        self.assertIn("Census", message)
        self.assertIn("無効または未登録", message)
        self.assertIn("https://www.census.gov/data/developers.html", message)

    def test_http_504_is_classified_as_provider_timeout(self):
        response = MagicMock(status_code=504, headers={"content-type": "text/html"})
        response.url = "https://overpass-api.de/api/interpreter"
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.return_value = response
        with patch.object(external_tools.httpx, "Client", return_value=client):
            with self.assertRaises(external_tools.ExternalToolError) as caught:
                external_tools._request("overpass-api.de", "/api/interpreter", {"data": "query"})
        self.assertEqual("PROVIDER_TIMEOUT", str(caught.exception))

    def test_overpass_elements_are_normalized_and_rendered_as_facilities(self):
        from olcr_api.app import external_tool_summary

        payload = {"elements": [
            {"type": "node", "id": 101, "lat": 35.68, "lon": 139.76,
             "tags": {"name": "中央総合病院", "amenity": "hospital", "addr:city": "千代田区"}},
            {"type": "way", "id": 202, "center": {"lat": 35.69, "lon": 139.77},
             "tags": {"amenity": "hospital", "name:en": "Tokyo Medical Center"}},
        ]}
        with patch.object(external_tools, "_geocode_place", return_value=(35.681236, 139.767125)), \
             patch.object(external_tools, "_overpass_request", return_value=payload):
            result = external_tools.execute("geo.poi", {"center_name": "東京駅", "radius_m": 1000,
                                                        "poi_type": "hospital", "limit": 5})

        self.assertEqual(2, result["data"]["item_count"])
        rendered = external_tool_summary(result)
        self.assertIn("中央総合病院", rendered)
        self.assertIn("Tokyo Medical Center", rendered)
        self.assertIn("35.69, 139.77", rendered)
        self.assertIn("openstreetmap.org/way/202", rendered)

    @unittest.skip("Census is intentionally excluded from production until the provider accepts its key")
    def test_census_keyed_json_array_is_normalized_and_rendered(self):
        from olcr_api.app import external_tool_summary

        payload = [["NAME", "B01001_001E"], ["United States", "340110988"]]
        with patch.object(external_tools, "_provider_credential", return_value=("fixture-key", None)), \
             patch.dict("os.environ", {"OLCR_CENSUS_API_KEY": "fixture-key"}), \
             patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("econ.us_census", {"query": "latest U.S. population estimate"})

        self.assertEqual("2024 ACS 1-year estimate", result["data"]["vintage"])
        self.assertEqual("340110988", result["data"]["items"][0]["population"])
        self.assertEqual("fixture-key", request.call_args.args[2]["key"])
        self.assertEqual("us:1", request.call_args.args[2]["for"])
        rendered = external_tool_summary(result)
        self.assertIn("340,110,988人", rendered)
        self.assertIn("2024年", rendered)

    def test_uniprot_relevance_filter_checks_provider_specific_gene_fields(self):
        query = "(gene:TP53) AND (organism_id:9606)"
        payload = {"results": [{
            "primaryAccession": "P04637",
            "genes": [{"geneName": {"value": "TP53"}}],
            "proteinDescription": {"recommendedName": {"fullName": {"value": "Cellular tumor antigen p53"}}},
            "organism": {"scientificName": "Homo sapiens"},
        }]}
        result = external_tools._new_relevance_result("biology.protein", query, payload, "https://www.uniprot.org/")
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual(1, result["data"]["item_count"])
        self.assertEqual("P04637", result["data"]["items"][0]["primaryAccession"])

    def test_successful_common_envelope_has_provenance_and_not_raw_json(self):
        for tool_id in CASES:
            with self.subTest(tool_id=tool_id):
                payload = ({"ldhName": "fixture.example", "nameservers": [{"ldhName": "ns1.example"}]}
                           if tool_id == "internet.domain_registration"
                           else {"items": [{"title": "Fixture", "value": 1}]})
                result = external_tools._external_result(tool_id, "fixture", payload, "https://example.invalid/")
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
                payload = ({"ldhName": "fixture.example", "nameservers": [{"ldhName": "ns1.example"}]}
                           if tool_id == "internet.domain_registration"
                           else {"items": [{"title": "Fixture", "name": "Fixture", "value": 1}]})
                result = external_tools._external_result(tool_id, "fixture", payload, "https://example.invalid/")
                rendered = external_tool_summary(result)
                self.assertTrue(rendered.strip())
                self.assertNotEqual(json.dumps(result["data"], ensure_ascii=False), rendered)

    def test_estat_cpi_normalizes_latest_rows_and_renders_values_without_leaking_key(self):
        from olcr_api.app import external_tool_summary

        payload = {"GET_STATS_DATA": {
            "RESULT": {"STATUS": 0},
            "STATISTICAL_DATA": {
                "CLASS_INF": {"CLASS_OBJ": [
                    {"@id": "time", "CLASS": [
                        {"@code": "202607", "@name": "2026年7月"},
                        {"@code": "202608", "@name": "2026年8月"},
                    ]},
                    {"@id": "cat01", "CLASS": {"@code": "0001", "@name": "総合"}},
                    {"@id": "area", "CLASS": {"@code": "00000", "@name": "全国"}},
                ]},
                "DATA_INF": {"VALUE": [
                    {"@time": "202607", "@cat01": "0001", "@area": "00000", "$": "110.8"},
                    {"@time": "202608", "@cat01": "0001", "@area": "00000", "$": "111.2"},
                ]},
            },
        }}
        with patch.object(external_tools, "_provider_credential", return_value=("fixture-app-id", None)), \
             patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("econ.jp_estat", {"query": "日本の最新の消費者物価指数"})

        request.assert_called_once()
        params = request.call_args.args[2]
        self.assertEqual("0003427113", params["statsDataId"])
        self.assertEqual("1", params["cdTab"])
        self.assertEqual("0001", params["cdCat01"])
        self.assertEqual("00000", params["cdArea"])
        self.assertEqual("fixture-app-id", params["appId"])
        self.assertEqual(1, params["limit"])
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual(1, result["data"]["item_count"])
        self.assertEqual("2026年8月", result["data"]["items"][0]["period"])
        self.assertEqual("111.2", result["data"]["items"][0]["value"])
        rendered = external_tool_summary(result)
        self.assertIn("2026年8月：111.2", rendered)
        self.assertIn("e-stat.go.jp/stat-search/database", rendered)
        self.assertNotIn("fixture-app-id", json.dumps(result, ensure_ascii=False))

    def test_estat_history_obeys_requested_month_count_but_latest_defaults_to_one(self):
        latest = external_tools.compile_provider_arguments(
            "econ.jp_estat", "e-Statで日本の最新の消費者物価指数を教えて")
        history = external_tools.compile_provider_arguments(
            "econ.jp_estat", "e-Statで消費者物価指数の直近24か月の推移を見せて")
        self.assertEqual(1, latest["limit"])
        self.assertEqual(24, history["limit"])

    def test_estat_monthly_trend_drops_annual_fiscal_year_rows(self):
        payload = {"GET_STATS_DATA": {
            "RESULT": {"STATUS": 0},
            "STATISTICAL_DATA": {
                "CLASS_INF": {"CLASS_OBJ": [{"@id": "time", "CLASS": [
                    {"@code": "202608", "@name": "2026年8月"},
                    {"@code": "202607", "@name": "2026年7月"},
                    {"@code": "2025FY", "@name": "2025年度"},
                ]}]},
                "DATA_INF": {"VALUE": [
                    {"@time": "202608", "$": "114.3"},
                    {"@time": "202607", "$": "114.2"},
                    {"@time": "2025FY", "$": "112.3"},
                ]},
            },
        }}
        with patch.object(external_tools, "_provider_credential", return_value=("fixture-app-id", None)), \
             patch.object(external_tools, "_request", return_value=payload):
            result = external_tools.execute("econ.jp_estat", {
                "query": "最近の日本の物価の動き", "limit": 12,
            })
        self.assertEqual(["2026年8月", "2026年7月"],
                         [row["period"] for row in result["data"]["items"]])

    def test_estat_trend_renderer_adds_index_point_summary(self):
        from olcr_api.app import external_tool_summary
        result = external_tools._result("econ.jp_estat", "CPI", {
            "items": [{"period": "2026年8月", "value": "114.3", "unit": "指数"},
                      {"period": "2026年7月", "value": "114.2", "unit": "指数"},
                      {"period": "2025年10月", "value": "112.8", "unit": "指数"}],
            "semantic_status": "DATA",
        }, [{"title": "e-Stat", "canonical_url": "https://www.e-stat.go.jp/"}])
        rendered = external_tool_summary(result)
        self.assertIn("112.8→114.3、+1.5指数ポイント", rendered)
        self.assertIn("最新月の前月差：+0.1指数ポイント", rendered)
        self.assertIn("物価上昇率ではなく指数の差", rendered)

    def test_un_sdg_renewable_rows_are_japan_scoped_and_sorted_latest_first(self):
        payload = [
            {"indicator": "7.2.1", "geoAreaCode": "392", "geoAreaName": "Japan",
             "timePeriodStart": 2022, "value": "22.0", "units": "%"},
            {"indicator": "7.2.1", "geoAreaCode": "392", "geoAreaName": "Japan",
             "timePeriodStart": 2024, "value": "24.0", "units": "%"},
            {"indicator": "7.2.1", "geoAreaCode": "840", "geoAreaName": "United States",
             "timePeriodStart": 2025, "value": "10.0", "units": "%"},
        ]
        with patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("development.un_sdg", {
                "indicator": "7.2.1", "area_codes": [392], "limit": 2,
            })
        params = request.call_args.args[2]
        self.assertEqual("7.2.1", params["indicator"])
        self.assertEqual(100, params["pageSize"])
        self.assertEqual([392], params["areaCode"])
        self.assertEqual(["2024", "2022"], [row["year"] for row in result["data"]["items"]])
        self.assertTrue(all(row["geo_area"] == "Japan" for row in result["data"]["items"]))

    def test_estat_error_or_malformed_envelope_is_not_false_data(self):
        for payload in (
            {"GET_STATS_DATA": {"RESULT": {"STATUS": 100, "ERROR_MSG": "denied"}}},
            {"GET_STATS_DATA": {"RESULT": "malformed"}},
            {"GET_STATS_DATA": {"RESULT": {"STATUS": 0}, "STATISTICAL_DATA": {"DATA_INF": {"VALUE": []}}}},
        ):
            with self.subTest(payload=payload):
                result = external_tools._result(
                    "econ.jp_estat", "CPI", {"items": external_tools._estat_rows(payload),
                     "has_payload": bool(external_tools._estat_rows(payload)),
                     "semantic_status": "DATA" if external_tools._estat_rows(payload) else "EMPTY"}, [],
                )
                self.assertEqual("EMPTY", result["data"]["semantic_status"])
                self.assertEqual("no_data", result["status"])

    def test_failed_credential_gate_arguments_are_compiled_to_provider_fields(self):
        cases = (
            ("econ.fred", "FREDで米政策金利の最近の推移を見せて", "series_id", "FEDFUNDS"),
            ("energy.us_eia", "EIAで米国の原油在庫の最新値と最近の推移を見せて", "series_id", "PET.WCESTUS1.W"),
            ("politics.us_congress", "米議会でAIに関係する最近の法案を5件探して", "query", "artificial intelligence"),
            ("policy.us_rulemaking", "Regulations.govでAIに関係する最近の規則案を探して", "query", "artificial intelligence"),
        )
        for tool_id, prompt, field, expected in cases:
            with self.subTest(tool_id=tool_id):
                self.assertEqual(expected, external_tools.compile_provider_arguments(tool_id, prompt)[field])

    def test_fred_and_eia_adapters_use_correct_series_parse_rows_and_render(self):
        from olcr_api.app import external_tool_summary

        fake_key = "fixture-key-should-not-leak"
        fred_payload = {"observations": [
            {"date": "2026-08-01", "value": "4.2"}, {"date": "2026-09-01", "value": "4.1"},
        ]}
        with patch.object(external_tools, "_provider_credential", return_value=(fake_key, None)), \
             patch.object(external_tools, "_request", return_value=fred_payload) as request:
            fred = external_tools.execute("econ.fred", {"query": "FRED policy rate", "series_id": "FEDFUNDS", "limit": 2})
        self.assertEqual("/fred/series/observations", request.call_args.args[1])
        self.assertEqual("FEDFUNDS", request.call_args.args[2]["series_id"])
        self.assertEqual("desc", request.call_args.args[2]["sort_order"])
        self.assertEqual("2026-09-01", fred["data"]["items"][0]["period"])
        self.assertIn("2026-09-01：4.1", external_tool_summary(fred))
        self.assertIn("https://fred.stlouisfed.org/series/FEDFUNDS", external_tool_summary(fred))
        self.assertNotIn(fake_key, json.dumps(fred))

        eia_payload = {"response": {"data": [
            {"period": "2026-09-18", "value": "426398", "value-units": "thousand barrels"},
            {"period": "2026-09-11", "value": "423429", "value-units": "thousand barrels"},
        ]}}
        with patch.object(external_tools, "_provider_credential", return_value=(fake_key, None)), \
             patch.object(external_tools, "_request", return_value=eia_payload) as request:
            eia = external_tools.execute("energy.us_eia", {"query": "EIA crude stocks", "limit": 2})
        self.assertEqual("/v2/petroleum/sum/sndw/data/", request.call_args.args[1])
        self.assertEqual(fake_key, request.call_args.args[2]["api_key"])
        self.assertEqual("value", request.call_args.args[2]["data[0]"])
        self.assertEqual("WCESTUS1", request.call_args.args[2]["facets[series][]"])
        self.assertEqual("2026-09-18", eia["data"]["items"][0]["period"])
        self.assertIn("426398", external_tool_summary(eia))
        self.assertIn("https://www.eia.gov/dnav/pet/PET_STOC_WSTK_A_EPC0_SAX_MBBL_W.htm", external_tool_summary(eia))
        self.assertNotIn(fake_key, json.dumps(eia))

    def test_usaspending_contract_results_render_as_typed_evidence_without_brain(self):
        from olcr_api.app import external_tool_summary

        payload = {"results": [
            {
                "Award ID": "80NSSC24CA001",
                "Transaction Amount": 125000000,
                "Awarding Agency": "National Aeronautics and Space Administration",
                "Recipient Name": "Space Exploration Technologies Corp.",
                "Action Date": "2024-02-01",
                "Mod": "P0001",
                "Award Type": "DEFINITIVE CONTRACT",
            },
            {
                "Award ID": "80NSSC24CB002",
                "Transaction Amount": 75000000,
                "Awarding Agency": "National Aeronautics and Space Administration",
                "Recipient Name": "SpaceX",
                "Action Date": "2024-06-01",
            },
        ]}
        with patch.object(external_tools, "_post_request", return_value=payload):
            result = external_tools.execute("gov.us_spending", {
                "fiscal_year": 2024, "agency": "NASA", "recipient": "SpaceX", "limit": 5,
            })

        rendered = external_tool_summary(result)
        self.assertIn("80NSSC24CA001", rendered)
        self.assertIn("Space Exploration Technologies Corp.", rendered)
        self.assertIn("USD 125,000,000", rendered)
        self.assertIn("取引日：2024-02-01", rendered)
        self.assertIn("USD 75,000,000", rendered)
        self.assertIn("支払済み出金（outlay）額でもありません", rendered)
        self.assertIn("https://api.usaspending.gov/docs/endpoints", rendered)
        self.assertTrue(result["data"]["items"])

        result["data"]["search_resolution"] = {"requested": "SpaceX", "submitted": "SPACE EXPLORATION TECHNOLOGIES CORP."}
        self.assertIn("SpaceX → SPACE EXPLORATION TECHNOLOGIES CORP.", external_tool_summary(result))

        result["data"]["recipient_filter_name"] = "LOCKHEED MARTIN CORPORATION"
        result["data"]["excluded_recipient_count"] = 1
        self.assertIn("別の登録先名の取引1件を除外", external_tool_summary(result))

    def test_fred_honors_twelve_month_request_in_provider_query_and_rendering(self):
        from olcr_api.app import external_tool_summary

        prompt = "FREDで米国の政策金利の最新値と直近12か月の推移を教えて"
        arguments = external_tools.compile_provider_arguments("econ.fred", prompt)
        self.assertEqual(12, arguments["limit"])
        periods = [
            "2026-08-01", "2026-07-01", "2026-06-01", "2026-05-01",
            "2026-04-01", "2026-03-01", "2026-02-01", "2026-01-01",
            "2025-12-01", "2025-11-01", "2025-10-01", "2025-09-01",
        ]
        payload = {"observations": [
            {"date": period, "value": f"{4.0 + index / 100:.2f}"}
            for index, period in enumerate(periods)
        ]}
        with patch.object(external_tools, "_provider_credential", return_value=("fixture-key", None)), \
             patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("econ.fred", {"query": prompt, **arguments})

        self.assertEqual(12, request.call_args.args[2]["limit"])
        self.assertEqual(12, result["data"]["item_count"])
        rendered = external_tool_summary(result)
        self.assertIn("2026-08-01", rendered)
        self.assertIn("2025-09-01", rendered)
        self.assertNotIn("fixture-key", json.dumps(result))

    def test_fred_honors_requested_period_longer_than_benchmark(self):
        prompt = "FREDで米国の政策金利の直近24か月の推移を教えて"
        arguments = external_tools.compile_provider_arguments("econ.fred", prompt)
        self.assertEqual(24, arguments["limit"])
        periods = [f"2026-{month:02d}-01" for month in range(9, 0, -1)] + [
            f"2025-{month:02d}-01" for month in range(12, 0, -1)] + [
            f"2024-{month:02d}-01" for month in range(12, 9, -1)]
        payload = {"observations": [{"date": period, "value": "4.00"} for period in periods]}
        with patch.object(external_tools, "_provider_credential", return_value=("fixture-key", None)), \
             patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("econ.fred", {"query": prompt, **arguments})
        self.assertEqual(24, request.call_args.args[2]["limit"])
        self.assertEqual(24, result["data"]["item_count"])
        self.assertEqual("2024-10-01", result["data"]["items"][-1]["period"])

    def test_fred_pages_beyond_internal_page_size_without_total_cap(self):
        newest = [{"date": f"2026-{(index % 12) + 1:02d}-{(index % 27) + 1:02d}", "value": str(index)}
                  for index in range(1000)]
        older = [{"date": "2023-01-01", "value": "1000"}]
        request_params = []
        def request_page(_host, _path, params, *_args):
            request_params.append(dict(params))
            return {"observations": newest if len(request_params) == 1 else older}
        with patch.object(external_tools, "_provider_credential", return_value=("fixture-key", None)), \
             patch.object(external_tools, "_request", side_effect=request_page) as request:
            result = external_tools.execute("econ.fred", {"query": "FRED history", "limit": 1001})
        self.assertEqual(2, request.call_count)
        self.assertEqual([0, 1000], [params["offset"] for params in request_params])
        self.assertEqual([1000, 1], [params["limit"] for params in request_params])
        self.assertEqual(1001, result["data"]["item_count"])

    def test_openalex_follows_cursor_for_explicit_count_over_one_page(self):
        def work(index):
            return {"id": f"https://openalex.org/W{index}", "title": f"Research result {index}",
                    "publication_year": 2025, "publication_date": "2025-01-01",
                    "authorships": [], "primary_location": {}, "doi": None,
                    "open_access": {}, "relevance_score": 1.0}
        pages = [
            {"results": [work(i) for i in range(100)], "meta": {"next_cursor": "cursor-2"}},
            {"results": [work(i) for i in range(100, 120)], "meta": {"next_cursor": None}},
        ]
        with patch.object(external_tools, "_request", side_effect=pages) as request:
            result = external_tools.research(query="research", limit=120)
        self.assertEqual(2, request.call_count)
        self.assertEqual(100, request.call_args_list[0].args[2]["per-page"])
        self.assertEqual("*", request.call_args_list[0].args[2]["cursor"])
        self.assertEqual(20, request.call_args_list[1].args[2]["per-page"])
        self.assertEqual("cursor-2", request.call_args_list[1].args[2]["cursor"])
        self.assertEqual(120, len(result["data"]["works"]))

    def test_fec_and_congress_adapters_use_searchable_envelopes_and_render(self):
        from olcr_api.app import external_tool_summary

        fake_key = "fixture-key-should-not-leak"
        fec_payload = {"results": [
            {"candidate_id": "P00000001", "candidate_name": "ALL CANDIDATES", "net_receipts": 9999999.0},
            {"candidate_id": "P80001571", "candidate_name": "TRUMP, DONALD J.", "candidate_party_affiliation": "REP", "net_receipts": 1234567.0},
            {"candidate_id": "P00009423", "candidate_name": "HARRIS, KAMALA", "candidate_party_affiliation": "DEM", "net_receipts": 2345678.0},
        ]}
        with patch.object(external_tools, "_provider_credential", return_value=(fake_key, None)), \
             patch.object(external_tools, "_request", return_value=fec_payload) as request:
            fec = external_tools.execute("politics.us_campaign_finance", {"query": "2024 presidential receipts", "cycle": 2024, "office": "P"})
        self.assertEqual("/v1/presidential/financial_summary/", request.call_args_list[0].args[1])
        self.assertEqual("-net_receipts", request.call_args_list[0].args[2]["sort"])
        self.assertEqual(2024, request.call_args_list[0].args[2]["election_year"])
        self.assertEqual(["Kamala Harris", "Donald J. Trump"], [row["candidate_name"] for row in fec["data"]["items"]])
        rendered = external_tool_summary(fec)
        self.assertIn("Kamala Harris", rendered)
        self.assertIn("USD 2,345,678", rendered)
        self.assertNotIn("$", rendered)
        self.assertIn("net receipts（純収入）", rendered)
        self.assertIn("https://www.fec.gov/data/candidates/president/", rendered)
        self.assertNotIn(fake_key, json.dumps(fec))

        congress_payload = {"bills": [
            {"congress": 119, "type": "hr", "number": 100, "title": "Artificial Intelligence Safety Act", "latestAction": {"actionDate": "2026-08-01", "text": "Introduced"}},
            {"congress": 119, "type": "hr", "number": 101, "title": "Aviation Safety Act", "latestAction": {"actionDate": "2026-08-02", "text": "Introduced"}},
        ]}
        with patch.object(external_tools, "_provider_credential", return_value=(fake_key, None)), \
             patch.object(external_tools, "_request", return_value=congress_payload) as request:
            congress = external_tools.execute("politics.us_congress", {"query": "artificial intelligence", "congress": 119, "limit": 5})
        self.assertTrue(any(call.args[1] == "/v3/bill/119" for call in request.call_args_list))
        self.assertNotIn("query", request.call_args.args[2])
        self.assertEqual(1, congress["data"]["item_count"])
        congress_summary = external_tool_summary(congress)
        self.assertIn("Artificial Intelligence Safety Act", congress_summary)
        self.assertIn("https://www.congress.gov/bill/119th-congress/house-bill/100", congress_summary)

    def test_fec_presidential_financial_summary_uses_consistent_candidate_net_receipts(self):
        fake_key = "fixture-key-should-not-leak"
        payload = {"results": [
            {"candidate_id": "P00000001", "candidate_name": "ALL CANDIDATES", "net_receipts": 2000},
            {"candidate_id": "P80001571", "candidate_name": "TRUMP, DONALD J.",
             "candidate_party_affiliation": "REP", "net_receipts": 1100,
             "cash_on_hand_end": 200, "transfers_from_affiliated_committees": 900},
            {"candidate_id": "P00009423", "candidate_name": "HARRIS, KAMALA",
             "candidate_party_affiliation": "DEM", "net_receipts": 1000},
        ], "pagination": {"pages": 1}}
        with patch.object(external_tools, "_provider_credential", return_value=(fake_key, None)), \
             patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("politics.us_campaign_finance", {
                "query": "2024 presidential candidate receipts", "limit": 3,
            })
        self.assertEqual("/v1/presidential/financial_summary/", request.call_args.args[1])
        self.assertEqual(["P80001571", "P00009423"],
                         [row["candidate_id"] for row in result["data"]["items"]])
        self.assertEqual(["Donald J. Trump", "Kamala Harris"],
                         [row["candidate_name"] for row in result["data"]["items"]])
        self.assertEqual([1100, 1000], [row["receipts"] for row in result["data"]["items"]])
        self.assertEqual("Republican Party", result["data"]["items"][0]["party"])
        self.assertEqual("DATA", result["data"]["semantic_status"])
        from olcr_api.app import external_tool_summary
        rendered = external_tool_summary(result)
        self.assertIn("USD 1,100", rendered)
        self.assertNotIn("not directly comparable", rendered)

    def test_fec_and_congress_continue_past_vendor_page_sizes(self):
        fec_rows = [[{"candidate_id": f"P{i:08d}", "candidate_name": f"Candidate {i}",
                      "party_full": "Democratic Party", "receipts": i} for i in range(100)],
                    [{"candidate_id": "P00000100", "candidate_name": "Candidate 100",
                      "party_full": "Republican Party", "receipts": 100}]]
        fec_params = []
        def fec_page(_host, _path, params, *_args):
            if "page" not in params:
                return {"results": [{"candidate_id": "P80001571", "candidate_name": "Trump, Donald J.",
                                     "party_full": "Republican Party", "receipts": 500, "cycle": 2024}]}
            fec_params.append(dict(params))
            return {"results": fec_rows[params["page"] - 1], "pagination": {"pages": 2}}
        with patch.object(external_tools, "_provider_credential", return_value=("fixture-key", None)), \
             patch.object(external_tools, "_request", side_effect=fec_page):
            fec = external_tools.execute("politics.us_campaign_finance", {"query": "presidential receipts", "limit": 101})
        self.assertEqual([1, 2], [params["page"] for params in fec_params])
        self.assertEqual(101, fec["data"]["item_count"])

        bill_pages = [[{"title": f"Artificial Intelligence Act {i}", "type": "HR", "number": i,
                        "congress": 119} for i in range(250)],
                      [{"title": "Artificial Intelligence Act 250", "type": "HR", "number": 250,
                        "congress": 119}]]
        congress_params = []
        def congress_page(_host, _path, params, *_args):
            if _path != "/v3/bill/119":
                return {}
            congress_params.append(dict(params))
            return {"bills": bill_pages[len(congress_params) - 1]}
        with patch.object(external_tools, "_provider_credential", return_value=("fixture-key", None)), \
             patch.object(external_tools, "_request", side_effect=congress_page):
            congress = external_tools.execute("politics.us_congress", {"query": "artificial intelligence", "limit": 251})
        self.assertEqual([0, 250], [params["offset"] for params in congress_params])
        self.assertEqual(251, congress["data"]["item_count"])

    def test_regulations_normalizes_jsonapi_documents_and_expands_ai_query(self):
        from olcr_api.app import external_tool_summary

        payload = {"data": [
            {"id": "SEC-2026-0001-0001", "attributes": {
                "title": "Securities Relief for Public Companies", "documentType": "Proposed Rule",
                "postedDate": "2026-09-02", "agencyId": "SEC",
            }},
            {"id": "EPA-HQ-2026-0001-0001", "attributes": {
                "title": "Artificial Intelligence Reporting Requirements", "documentType": "Proposed Rule",
                "postedDate": "2026-09-01", "agencyId": "EPA",
            }},
            {"id": "EPA-HQ-2026-0001-0002", "attributes": {
                "title": "Artificial Intelligence Supporting Material", "documentType": "Supporting & Related Material",
                "postedDate": "2026-09-03", "agencyId": "EPA",
            }}]}
        with patch.object(external_tools, "_provider_credential", return_value=("fixture-key", None)), \
             patch.object(external_tools, "_request", return_value=payload) as request:
            result = external_tools.execute("policy.us_rulemaking", {"query": "artificial intelligence", "limit": 5})
        search_call = next(call for call in request.call_args_list if "filter[searchTerm]" in call.args[2])
        self.assertEqual("artificial intelligence", search_call.args[2]["filter[searchTerm]"])
        self.assertEqual("Artificial Intelligence Reporting Requirements", result["data"]["items"][0]["title"])
        self.assertEqual(["EPA-HQ-2026-0001-0001"], [row["id"] for row in result["data"]["items"]])
        regulation_summary = external_tool_summary(result)
        self.assertIn("EPA-HQ-2026-0001-0001", regulation_summary)
        self.assertIn("https://www.regulations.gov/document/EPA-HQ-2026-0001-0001", regulation_summary)
        self.assertNotIn("Supporting Material", regulation_summary)

    @unittest.skip("Global Fishing Watch is intentionally excluded from production after provider timeouts")
    def test_gfw_posts_bounded_galapagos_polygon_and_reads_entries_envelope(self):
        from olcr_api.app import external_tool_summary

        payload = {"entries": [{"id": "event-1", "type": "FISHING", "start": "2026-09-20",
                                "vessel": {"name": "Pacific vessel", "flag": "ECU"},
                                "position": {"lat": "-0.5", "lon": "-90.2"}}]}
        token = "fixture-token-should-not-leak"
        with patch.object(external_tools, "_provider_credential", return_value=(token, None)), \
             patch.object(external_tools, "_post_request", return_value=payload) as request:
            result = external_tools.execute("maritime.vessel_intelligence", {"query": "Galapagos", "region": "Galapagos", "limit": 1})
        self.assertIn("/v3/events?offset=0&limit=1", request.call_args.args[1])
        self.assertEqual("Polygon", request.call_args.args[2]["geometry"]["type"])
        self.assertEqual("Pacific vessel", result["data"]["items"][0]["vessel"])
        self.assertIn("Pacific vessel", external_tool_summary(result))
        self.assertNotIn(token, json.dumps(result))

    @unittest.skip("AISStream is intentionally excluded from production pending live-record acceptance")
    def test_aisstream_reads_bounded_live_messages_and_reports_bad_key(self):
        from olcr_api.app import external_tool_summary

        token = "fixture-ais-key-should-not-leak"
        socket = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = socket
        socket.recv.side_effect = [
            json.dumps({"MessageType": "SubscriptionConfirmation"}),
            json.dumps({"MessageType": "PositionReport", "MetaData": {"MMSI": 123456789, "ShipName": "TEST SHIP", "Latitude": 35.4, "Longitude": 139.8}, "Message": {"PositionReport": {}}}),
        ]
        with patch.object(external_tools, "_provider_credential", return_value=(token, None)), \
             patch("websockets.sync.client.connect", return_value=context) as connect:
            result = external_tools.execute("maritime.ais_live", {"query": "Tokyo Bay", "limit": 1})
        self.assertEqual(1, result["data"]["item_count"])
        self.assertIn("TEST SHIP", external_tool_summary(result))
        self.assertEqual("wss://stream.aisstream.io/v0/stream", connect.call_args.args[0])
        subscription = json.loads(socket.send.call_args.args[0])
        self.assertEqual([[[35.8, 139.3], [35.0, 140.3]]], subscription["BoundingBoxes"])
        self.assertNotIn(token, json.dumps(result))

        error_socket = MagicMock()
        error_context = MagicMock()
        error_context.__enter__.return_value = error_socket
        error_socket.recv.return_value = json.dumps({"MessageType": "Error", "error": "Invalid API key"})
        with patch.object(external_tools, "_provider_credential", return_value=(token, None)), \
             patch("websockets.sync.client.connect", return_value=error_context):
            with self.assertRaises(external_tools.ExternalToolError) as caught:
                external_tools.execute("maritime.ais_live", {"query": "Tokyo Bay", "limit": 1})
        self.assertEqual("PROVIDER_AUTHENTICATION_FAILED", str(caught.exception))

    @unittest.skip("AISStream is intentionally excluded from production pending live-record acceptance")
    def test_aisstream_bounds_stream_of_non_position_messages(self):
        token = "fixture-ais-key-should-not-leak"
        socket = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = socket
        socket.recv.return_value = json.dumps({"MessageType": "SubscriptionConfirmation"})
        with patch.object(external_tools, "_provider_credential", return_value=(token, None)), \
             patch("websockets.sync.client.connect", return_value=context):
            with self.assertRaises(external_tools.ExternalToolError) as caught:
                external_tools.execute("maritime.ais_live", {"query": "Tokyo Bay", "limit": 1})
        self.assertEqual("PROVIDER_NO_LIVE_DATA", str(caught.exception))
        self.assertEqual(20, socket.recv.call_count)
        from olcr_api.app import _external_tool_user_message
        message = _external_tool_user_message(str(caught.exception))
        self.assertIn("接続はできました", message)
        self.assertIn("位置情報", message)

    def test_aisstream_compiles_documented_northwest_to_southeast_bounds(self):
        arguments = external_tools.compile_provider_arguments(
            "maritime.ais_live", "AISstreamで今の東京湾周辺の船を5隻見せて"
        )
        self.assertEqual([[[35.8, 139.3], [35.0, 140.3]]], arguments["bounding_boxes"])
        self.assertEqual(5, arguments["limit"])


if __name__ == "__main__":
    unittest.main()
