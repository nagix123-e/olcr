"""Offline fuzzy-routing coverage for every enabled production knowledge API.

These cases exercise candidate retrieval and final provider selection only;
they never execute a provider or make network requests.
"""
from __future__ import annotations

import re
from pathlib import Path
import unittest
from unittest.mock import PropertyMock, patch
import olcr_api.app as app
import olcr_api.external_tools as external_tools
from olcr_api.api_acceptance import PRODUCTION_ACCEPTANCE_CASES
from olcr_api.api_capability_cards import API_CAPABILITY_CARDS
from olcr_api.external_tools import ExternalToolError, REGISTRY, compile_provider_arguments


_SEMANTIC_EQUIVALENTS = {
    # Once the user removes the source name, these request shapes genuinely do
    # not distinguish between overlapping public datasets. Either result is a
    # valid nearest-API selection; unrelated providers remain rejected.
    "aviation.opensky": {"aviation.opensky", "aviation.adsb_live"},
    "aviation.adsb_live": {"aviation.opensky", "aviation.adsb_live"},
    "econ.imf": {"econ.imf", "econ.jp_estat", "econ.worldbank"},
    "econ.jp_estat": {"econ.jp_estat", "econ.imf", "econ.worldbank"},
    # Country GDP and broad sanctions lookups are genuinely served by multiple
    # enabled sources when the source name/jurisdiction is omitted.
    "econ.adb": {"econ.adb", "econ.worldbank"},
    "econ.afdb": {"econ.afdb", "econ.worldbank"},
    "sanctions.opensanctions": {"sanctions.opensanctions", "sanctions.us_ofac"},
    "sanctions.us_ofac": {"sanctions.us_ofac", "sanctions.opensanctions"},
    "sanctions.unsc": {"sanctions.unsc", "sanctions.opensanctions"},
    "geo.country_facts": {"geo.country_facts", "knowledge.wikidata"},
    "policy.us_federal_register": {"policy.us_federal_register", "policy.us_rulemaking"},
}


def _without_source_brand(prompt: str, tool_id: str) -> str:
    value = prompt
    brands = {REGISTRY[tool_id].provider, tool_id}
    # A few acceptance examples mention a product/API name rather than the
    # registry's display provider. Strip source-like proper-name phrases when
    # they directly modify "data", "で", or "から", keeping subject entities.
    for brand in sorted((x for x in brands if x), key=len, reverse=True):
        value = re.sub(re.escape(brand), " ", value, flags=re.I)
    value = re.sub(
        r"(?<![A-Za-z])(?:[A-Z][A-Za-z0-9.-]*(?:\s+[A-Z][A-Za-z0-9.-]*){0,3})"
        r"(?=(?:で|から|データで|データから|の(?:公開)?データ|\s+data\b))",
        " ", value,
    )
    value = re.sub(r"\b(?:API|dataset)\b|\bAPI\b|データセット(?:で|から|の)?", " ", value, flags=re.I)
    value = re.sub(r"(?:GitHub|IMF|OFAC|OpenSky|ADSB\.lol|UCDP|Wikidata|Federal Register)(?=で|から|の)", " ", value, flags=re.I)
    value = re.sub(r"^(?:データで|データから|で)", "", value)
    value = re.sub(r"\s+", " ", value).strip(" 、。！？!?：: でから")
    return value


def _fuzzy_variants(case: dict[str, str]) -> tuple[str, str]:
    subject = _without_source_brand(case["user_prompt"], case["tool_id"])
    # Keep the domain clue, but remove the source name and wrap the request in
    # ordinary conversational language rather than API-oriented phrasing.
    if re.search(r"[ぁ-んァ-ン一-龯]", subject):
        natural = f"ちょっと知りたいんだけど、{subject}。"
        vague = f"このことをもう少し知りたい。{subject}"
    else:
        natural = f"I'm trying to understand this: {subject}."
        vague = f"I'm curious to know more about this. {subject}"
    return natural, vague


class FuzzyApiRoutingMatrixTests(unittest.TestCase):
    def test_all_enabled_apis_route_from_source_free_natural_prompts(self):
        from olcr_api.external_tools import RUNTIME_REGISTRY
        self.assertEqual(set(RUNTIME_REGISTRY), {case["tool_id"] for case in PRODUCTION_ACCEPTANCE_CASES})
        self.assertEqual(len(RUNTIME_REGISTRY), len(PRODUCTION_ACCEPTANCE_CASES))

        with patch.object(app, "vectors") as vectors, \
             patch.object(app, "_api_embedding_provider") as embedding, \
             patch.object(type(app.laya_provider), "loaded", new_callable=PropertyMock, return_value=False):
            vectors.state = "disabled"
            embedding.embed.side_effect = AssertionError("disabled embedding index must not be called")
            for case in PRODUCTION_ACCEPTANCE_CASES:
                expected = case["tool_id"]
                for variant_index, prompt in enumerate(_fuzzy_variants(case), 1):
                    with self.subTest(tool_id=expected, variant=variant_index, prompt=prompt):
                        decision = app.route_external_tool(prompt)
                        if decision is None:
                            self.assertTrue(app._external_router_candidate(prompt), prompt)
                            decision = app.router_decision(prompt)
                        self.assertIsNotNone(decision, f"no provider selected for {expected}: {prompt}")
                        self.assertIn(decision[0], _SEMANTIC_EQUIVALENTS.get(expected, {expected}), prompt)

    def test_embedding_provider_failure_uses_the_bounded_offline_router(self):
        prompt = next(
            candidate
            for case in PRODUCTION_ACCEPTANCE_CASES
            for candidate in _fuzzy_variants(case)
            if app.route_external_tool(candidate) is None
        )
        with patch.object(app, "vectors") as vectors, \
             patch.object(app._api_embedding_provider, "embed", side_effect=RuntimeError("offline")), \
             patch.object(type(app.laya_provider), "loaded", new_callable=PropertyMock, return_value=False):
            vectors.state = "configured"
            decision = app.router_decision(prompt)
        self.assertIsNotNone(decision, f"embedding failure blocked the bounded lexical route for: {prompt}")

    @staticmethod
    def _planned_angle_prompts() -> list[tuple[str, str]]:
        """Load the reviewed 4-per-API probes from the human-readable plan."""
        plan = Path(__file__).resolve().parents[2] / "docs/fuzzy-routing/plan-and-352-prompts.md"
        text = plan.read_text(encoding="utf-8")
        blocks = re.split(r"^### (\d+)\. `([^`]+)`[^\n]*$", text, flags=re.M)
        cases: list[tuple[str, str]] = []
        counts: dict[str, int] = {}
        for offset in range(1, len(blocks), 3):
            _number, tool_id, body = blocks[offset:offset + 3]
            prompts = re.findall(r"^\d+\. (.+)$", body, flags=re.M)
            counts[tool_id] = len(prompts)
            cases.extend((tool_id, prompt) for prompt in prompts)
        expected_ids = {case["tool_id"] for case in PRODUCTION_ACCEPTANCE_CASES}
        if set(counts) != expected_ids:
            raise AssertionError(
                f"routing plan API coverage differs from production fixture: "
                f"missing={sorted(expected_ids - set(counts))}, extra={sorted(set(counts) - expected_ids)}"
            )
        if any(count != 4 for count in counts.values()):
            raise AssertionError(f"expected four angle prompts per API, got {counts}")
        return cases

    def test_angle_prompt_plan_covers_all_enabled_api_cases(self):
        """Keep the complete active repair queue present and auditable."""
        cases = self._planned_angle_prompts()
        expected_ids = {case["tool_id"] for case in PRODUCTION_ACCEPTANCE_CASES}
        self.assertEqual(4 * len(expected_ids), len(cases))
        self.assertEqual(expected_ids, {tool_id for tool_id, _ in cases})
        self.assertEqual(
            expected_ids,
            set(API_CAPABILITY_CARDS),
        )

    def test_all_angle_prompts_route_or_ask_a_valid_clarification(self):
        """Batch-check every active angle, accepting explicit safe clarifications."""
        # Angle coverage is a deterministic capability test. Do not let a
        # developer's installed embedding model or running Laya service change
        # which branch this offline fixture exercises.
        with patch.object(app, "vectors") as vectors, \
             patch.object(type(app.laya_provider), "loaded", new_callable=PropertyMock, return_value=False):
            vectors.state = "disabled"
            for case_number, (expected, prompt) in enumerate(self._planned_angle_prompts(), 1):
                with self.subTest(case=case_number, expected=expected, prompt=prompt):
                    guard = app._api_preflight_clarification_guard(prompt)
                    if guard:
                        self.assertTrue(app._api_router_clarification(None, prompt, guard))
                        continue
                    decision = app.route_external_tool(prompt) or app.router_decision(prompt)
                    self.assertIsNotNone(decision, f"no route or clarification for case {case_number}: {prompt}")
                    selected, arguments = decision
                    allowed = _SEMANTIC_EQUIVALENTS.get(expected, {expected})
                    self.assertIn(selected, allowed, f"case {case_number}: {prompt}")
                    try:
                        compile_provider_arguments(selected, prompt, arguments)
                    except ExternalToolError:
                        self.assertTrue(
                            app._api_router_clarification(selected, prompt),
                            f"case {case_number} has invalid arguments without a safe clarification: {prompt}",
                        )

    def test_unspecified_ais_target_area_asks_for_location_instead_of_using_default_harbor(self):
        prompt = "船種ごとに航行パターンが違うか、対象海域のAISデータで確認して。"
        guard = app._api_preflight_clarification_guard(prompt)
        self.assertEqual("AIS_WATER_AREA_REQUIRED", guard)
        self.assertIn("海域", app._api_router_clarification(None, prompt, guard))

    def test_global_holiday_request_routes_worldwide_without_country_clarification(self):
        prompts = (
            "今日、世界のどこかで祝日になっている国を知りたい。国ごとに教えて。",
            "今日、世界各国のどこで祝日になっている？祝日名と国コードを一覧で教えて",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                decision = app.route_external_tool(prompt)
                self.assertIsNotNone(decision, "global scope must route directly, without requiring a country code")
                self.assertEqual("calendar.public_holidays", decision[0])
                args = compile_provider_arguments(decision[0], prompt, decision[1])
                self.assertEqual("worldwide", args["scope"])
                self.assertEqual("today", args["date"])
                self.assertNotIn("country_code", args)

    def test_independent_ambiguous_data_angles_select_their_domain_api(self):
        cases = {
            "自社ログに出たこのIPのネットワーク運営者と国を確認したい：1.1.1.1。": "internet.ip_info",
            "2025年はチームメイト同士で、ドライバーズポイントにどんな差が出た？": "sports.formula1",
            "2025年のコンストラクターズ順位とドライバーズ順位を照らし合わせて。": "sports.formula1",
            "あるドライバーが年間タイトルを取った年の最終ポイントを確認したい。2025年で。": "sports.formula1",
            "東京と札幌では、明日の日照時間にどのくらい差がある？": "astronomy.sunrise",
            "openai.comのトップページが2020年ごろどう変わったか、保存版を探したい。": "web.archive",
            "ここ10年の日本の経常収支を、GDP比で見たい。": "econ.imf",
            "日本の家計向け信用と企業向け信用で、伸び方に差がある？": "finance.bis",
            "ユーロと英ポンド、スイスフランの参考レートを同じ日付で比べて。": "finance.ecb",
            "国籍別と受入国別で難民数を見比べたい。アフガニスタン出身者について。": "humanitarian.unhcr",
            "2024年選挙で、候補者本人の選挙委員会と外部PACの収入を比べたい。": "politics.us_campaign_finance",
            "Section 230を引用した判決の中で、プラットフォーム責任を最近どう判断している？": "law.us_courts",
            "ケニアとタンザニアのGDP成長率を、最近の年で比べて。": "econ.worldbank",
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                decision = app.route_external_tool(prompt)
                self.assertIsNotNone(decision, f"no deterministic provider route for {prompt}")
                self.assertIn(decision[0], _SEMANTIC_EQUIVALENTS.get(expected, {expected}))

    def test_multi_location_angles_compile_for_one_batched_provider_execution(self):
        cases = (
            ("astronomy.sunrise", "東京と札幌では、明日の日照時間にどのくらい差がある？", 2),
            ("geo.elevation", "京都駅周辺と大阪駅周辺では、標高にどのくらい差がある？", 2),
            ("geo.elevation", "富士山五合目と山頂の標高差を比べて。", 2),
            ("aviation.operational_weather", "成田と羽田の風向・視程を比べたい。", 2),
        )
        for tool_id, prompt, expected_count in cases:
            with self.subTest(tool_id=tool_id, prompt=prompt):
                compiled = compile_provider_arguments(tool_id, prompt, {})
                self.assertEqual(expected_count, len(compiled["locations"]))

    def test_multi_location_execution_fans_out_and_returns_one_typed_result(self):
        cases = (
            ("astronomy.sunrise", "東京と札幌では、明日の日照時間にどのくらい差がある？"),
            ("geo.elevation", "京都駅周辺と大阪駅周辺では、標高にどのくらい差がある？"),
            ("aviation.operational_weather", "成田と羽田の風向・視程を比べたい。"),
        )
        for tool_id, prompt in cases:
            with self.subTest(tool_id=tool_id):
                args = compile_provider_arguments(tool_id, prompt, {})
                calls = []

                def fake_provider(adapter_id, child_args):
                    calls.append((adapter_id, child_args))
                    payload = ({"items": [{"icao": child_args.get("icao"), "wind": "VRB"}]}
                               if adapter_id == "aviation.operational_weather" else
                               {"results": {"sunrise": "06:00", "sunset": "18:00", "day_length": 43200}}
                               if adapter_id == "astronomy.sun_times" else
                               {"latitude": child_args["latitude"], "longitude": child_args["longitude"], "elevation_m": 30})
                    return external_tools._result(
                        adapter_id, prompt,
                        {**payload, "has_payload": True, "semantic_status": "DATA",
                         "result_kind": "items" if "items" in payload else "object",
                         "item_count": len(payload.get("items", [])), "evidence_field_count": 1},
                        [{"title": "fixture", "provider": "fixture", "canonical_url": "https://example.test/", "retrieved_at": "2026-01-01T00:00:00Z"}],
                    )

                with patch.object(external_tools, "_simple_http", side_effect=fake_provider):
                    result = external_tools.execute(tool_id, args)
                self.assertEqual(2, len(calls))
                self.assertEqual("ok", result["status"])
                self.assertEqual(2, result["data"]["item_count"])

    def test_uncomtrade_can_rank_major_semiconductor_equipment_exporters(self):
        compiled = compile_provider_arguments(
            "trade.un_comtrade", "半導体製造装置の主要な輸出国を、相手国別の金額で調べて。", {}
        )
        self.assertEqual("all", compiled["reporter_code"])
        self.assertEqual("8486", compiled["commodity_code"])
        self.assertEqual("X", compiled["flow_code"])

    def test_standalone_external_questions_isolate_old_evidence_but_followups_keep_context(self):
        self.assertTrue(app._standalone_external_context_isolation(
            "ニューヨーク港で貨物船の交通が多い水域を、年別に見たい。"
        ))
        self.assertFalse(app._standalone_external_context_isolation(
            "さっきの結果と比較して、ニューヨーク港の貨物船交通を見たい。"
        ))

    def test_worldwide_holiday_year_request_is_not_misrepresented_as_next_seven_days(self):
        with self.assertRaises(ExternalToolError):
            compile_provider_arguments("calendar.public_holidays", "世界各国の2026年の祝日を教えて")
        self.assertIn("年単位", app._api_router_clarification(
            "calendar.public_holidays", "世界各国の2026年の祝日を教えて"
        ))

    def test_argument_clarifications_name_the_missing_discriminator(self):
        expected = {
            "geo.elevation": "地点を1か所",
            "security.exploit_probability": "CVE番号",
            "chemistry.pubchem": "化合物名",
            "health.openfda": "有効成分名",
            "food.product": "商品名",
            "trade.un_comtrade": "輸出国",
            "econ.adb": "対象国",
        }
        for tool_id, phrase in expected.items():
            with self.subTest(tool_id=tool_id):
                self.assertIn(phrase, app._api_router_clarification(tool_id, "曖昧な質問"))

    def test_missing_jurisdiction_and_current_location_are_clarification_guards(self):
        self.assertEqual(
            "POLITICAL_JURISDICTION_REQUIRED",
            app._api_preflight_clarification_guard("最近提出された法案の審議状況を教えて"),
        )
        self.assertEqual(
            "LOCATION_SUBJECT_REQUIRED",
            app._api_preflight_clarification_guard("現在地の緯度・経度を地図に載せられる形で教えて"),
        )
        self.assertIsNone(app._api_preflight_clarification_guard("ISSの現在地の緯度と経度を教えて"))

    def test_us_congress_context_and_bill_identifiers_satisfy_jurisdiction_guard(self):
        for prompt in (
            "米議会でAI規制に関する最近の法案を探して",
            "S. 1234 の審議状況を教えて",
            "H.R. 987 の提出者と最新の審議状況を確認して",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(app._api_preflight_clarification_guard(prompt))

    def test_underspecified_angle_prompts_clarify_instead_of_silently_misrouting(self):
        prompts = {
            "今のネットワーク状況だと、手数料を抑えた送金はどれくらい待ちそう？": "CRYPTO_NETWORK_REQUIRED",
            "彼の最近の対局から、短手数で決着したものと長いものを比べたい。": "CHESS_PLAYER_REQUIRED",
            "近隣の複数観測地点で流量に差があるか、最新データを並べて。": "STREAMFLOW_LOCATION_REQUIRED",
            "2018年から2024年まで、同じURLの保存スナップショットがどれくらいある？": "WEB_ARCHIVE_URL_REQUIRED",
            "同じ食材で、調理時間が短いものと作り置き向きのものを比べて。": "FOOD_RECIPE_INGREDIENTS_REQUIRED",
            "同じ法的論点について、連邦控訴裁判所と最高裁の判例を比べたい。": "COURT_ISSUE_REQUIRED",
        }
        for prompt, guard in prompts.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(guard, app._api_preflight_clarification_guard(prompt))
                response = app._api_router_clarification(None, prompt, guard)
                self.assertTrue(response)

    def test_missing_discriminators_do_not_block_sufficient_neighboring_prompts(self):
        prompts = (
            "ポトマック川の直近の水位・流量を見せて。",
            "ある企業サイトのリニューアル前後を比較したい。openai.comの過去版を見せて。",
            "冷蔵庫に鶏肉とキャベツがある。調理時間が短いレシピを教えて。",
            "著作権とオンラインサービスに関する最近の連邦判決を探して。",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertIsNone(app._api_preflight_clarification_guard(prompt))

    def test_explicit_cve_attackability_routes_to_cve_details_not_epss(self):
        prompts = (
            "このCVEはリモートから悪用できるのか、攻撃条件と影響範囲を確認したい：CVE-2021-44228。",
            "CVE-2021-44228は、外部から悪用できる？必要な条件と影響を短く説明して。",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                decision = app.route_external_tool(prompt)
                self.assertIsNotNone(decision)
                self.assertEqual("security.cve", decision[0])

    def test_african_economic_indicators_and_f1_standings_route_by_domain(self):
        self.assertEqual(
            "econ.afdb",
            app.route_external_tool("ケニアの一人当たり所得や雇用の推移を見せて。")[0],
        )
        self.assertEqual(
            "sports.formula1",
            app.route_external_tool("シーズン終盤に順位が上がったドライバーを、最終順位と一緒に見たい。")[0],
        )

    def test_holiday_argument_compiler_does_not_default_to_japan(self):
        with self.assertRaises(ExternalToolError):
            compile_provider_arguments("calendar.public_holidays", "今日の祝日は？")
        self.assertEqual(
            "JP",
            compile_provider_arguments("calendar.public_holidays", "日本の2026年の祝日") ["country_code"],
        )

    def test_unspecified_holiday_scope_still_clarifies_instead_of_defaulting_to_japan(self):
        prompt = "今日の祝日は？"
        self.assertIsNone(app.router_decision(prompt))
        trace = app._API_ROUTING_TRACE.get() or {}
        self.assertEqual("HOLIDAY_COUNTRY_REQUIRED", trace.get("selection_guard"))

    def test_annual_holiday_question_about_countries_is_not_misread_as_missing_country(self):
        prompt = "祝日が月曜に移動する制度がある国について、今年の日付を調べて。"
        self.assertIsNone(app._api_preflight_clarification_guard(prompt))
        self.assertEqual("calendar.public_holidays", app.route_external_tool(prompt)[0])
        with self.assertRaises(ExternalToolError):
            compile_provider_arguments("calendar.public_holidays", prompt, {})


if __name__ == "__main__":
    unittest.main()
