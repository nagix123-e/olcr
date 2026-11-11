import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from olcr_api import external_tools
from olcr_api.external_tools import ExternalToolError, compile_provider_arguments, normalize_qiita_arguments, normalize_research_arguments, research, route


class ProviderArgumentCompilerTests(unittest.TestCase):
    def test_translation_keeps_long_source_and_instruction_like_dialogue(self):
        passage = ('京都の丸善で画集を眺めた。\n\n' * 300) + '行き方を教えてください'
        compiled = compile_provider_arguments(
            'language.translation', f'{passage}\n\n##スペイン語に全部訳して##')
        self.assertEqual(passage, compiled['text'])
        self.assertEqual('es', compiled['target'])
        with self.assertRaises(ExternalToolError):
            compile_provider_arguments('language.translation',
                                       'あ' * 12001 + '\n\nスペイン語に訳して')

    def test_whole_passage_translation_instruction_owns_provider_keywords(self):
        passage = '檸檬\n梶井基次郎\n\n京都の街を歩いた。\n丸善で画集を眺めた。'
        for scope in ('全部', '全文', 'すべて', '全て'):
            for prompt in (f'{passage}\n\n##スペイン語に{scope}訳して##',
                           f'スペイン語に{scope}翻訳してください：\n{passage}'):
                with self.subTest(prompt=prompt):
                    selected = route(prompt)
                    self.assertIsNotNone(selected)
                    tool_id, arguments = selected
                    self.assertEqual('language.translation', tool_id)
                    compiled = compile_provider_arguments(tool_id, prompt, arguments)
                    self.assertEqual(passage, compiled['text'])
                    self.assertEqual('ja', compiled['source'])
                    self.assertEqual('es', compiled['target'])

    def test_last_month_earthquake_prompt_uses_previous_calendar_month(self):
        today = datetime.now(timezone.utc).date()
        current_month_start = today.replace(day=1)
        expected_end = current_month_start - timedelta(days=1)
        prompt = "先月に日本で起きたM4以上の地震を地域別に分析して"
        compiled = compile_provider_arguments("earth.earthquakes", prompt)
        self.assertEqual(expected_end.replace(day=1).isoformat(), compiled["starttime"])
        self.assertEqual(expected_end.isoformat(), compiled["endtime"])
        self.assertEqual(4.0, compiled["minmagnitude"])

    def test_structured_examples_are_compact_and_typed(self):
        text = "人口1000万人以上のEU加盟国を人口の多い順に5か国挙げてください。構造化データを使って確認してください。"
        args = compile_provider_arguments("knowledge.wikidata", text, {"query": text})
        self.assertEqual("EU member country", args["subject"])
        self.assertEqual(10_000_000, args["population_min"])
        self.assertEqual("population_desc", args["ordering"])
        self.assertEqual(5, args["limit"])

        args = compile_provider_arguments("environment.air_quality", "東京都心の現在のPM2.5と大気質を調べて", {"location": text})
        self.assertEqual("東京", args["location"])

    def test_semantic_identifiers_never_use_the_full_prompt(self):
        text = "ISBN 9780262033848 の本について詳しく調べて"
        args = compile_provider_arguments("books.search", text, {"query": text})
        self.assertEqual("9780262033848", args["isbn"])
        self.assertNotEqual(text, args["isbn"])
        self.assertEqual("aspirin", compile_provider_arguments("chemistry.compound", "アスピリンの分子式", {})["compound"])
        self.assertEqual(["JP", "DE"], compile_provider_arguments("statistics.world_bank", "日本とドイツのGDPを2015年から2023年まで比較", {})["countries"])
        self.assertEqual(["JP"], compile_provider_arguments("statistics.world_bank", "Show Japan's GDP in 2023", {})["countries"])
        self.assertEqual(["JP"], compile_provider_arguments(
            "statistics.world_bank",
            "Use the World Bank provider explicitly to retrieve Japan's annual GDP (current US$) from 2015 through the latest available year.",
            {},
        )["countries"])
        self.assertEqual(["JP"], compile_provider_arguments(
            "statistics.world_bank",
            "World Bankで、日本の最新GDP（current US$）、指標ID NY.GDP.MKTP.CDを調べて",
            {},
        )["countries"])
        self.assertEqual(["JP"], compile_provider_arguments(
            "statistics.world_bank",
            "Japan's latest annual GDP in current US dollars",
            {},
        )["countries"])

    def test_common_gui_intents_compile_without_geographic_fallback(self):
        translation = compile_provider_arguments(
            "language.translation",
            "「今日はとても良い天気です」を英語に翻訳してください。",
            {"text": "prompt", "source": "ja", "target": "en"},
        )
        self.assertEqual({"text": "今日はとても良い天気です", "source": "ja", "target": "en"},
                         {key: value for key, value in translation.items() if key != "request_text"})
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

    def test_fuzzy_entities_compile_to_provider_identifiers(self):
        self.assertEqual("CVE-2021-44228", compile_provider_arguments(
            "security.cve", "Log4Shellの影響製品と修正方法を教えて", {}
        )["cve"])
        self.assertEqual("CVE-2021-44228", compile_provider_arguments(
            "security.exploit_probability", "Log4Shellの悪用可能性を知りたい", {}
        )["cve"])
        for name, expected in (("塩化ナトリウム", "sodium chloride"), ("果糖", "fructose"), ("ショ糖", "sucrose")):
            with self.subTest(compound=name):
                self.assertEqual(expected, compile_provider_arguments(
                    "chemistry.compound", f"{name}の分子量を知りたい", {}
                )["compound"])
        self.assertEqual("pydantic/pydantic", compile_provider_arguments(
            "software.github", "Pydanticの最近のリリース内容を教えて", {}
        )["repo"])
        self.assertEqual("kubernetes/kubernetes", compile_provider_arguments(
            "software.github", "Kubernetesの最新バージョンは？", {}
        )["repo"])
        translated = compile_provider_arguments(
            "language.translation", "“The model found the right route.”を日本語にして", {}
        )
        self.assertEqual("The model found the right route.", translated["text"])
        self.assertEqual("ja", translated["target"])
        translated_after_colon = compile_provider_arguments(
            "language.translation",
            "この案内文を、旅行者に分かりやすい日本語にして：Please proceed to the designated boarding area.",
            {},
        )
        self.assertEqual("Please proceed to the designated boarding area.", translated_after_colon["text"])
        self.assertEqual("serendipity", compile_provider_arguments(
            "language.dictionary", "「思いがけない幸運」に近い英単語の意味と使い方を調べて", {}
        )["word"])
        eia = compile_provider_arguments(
            "energy.us_eia", "ガソリン価格の地域差を、州別データで確認して", {}
        )
        self.assertEqual("gasoline_retail", eia["dataset"])
        self.assertGreaterEqual(eia["limit"], 50)
        petroleum = compile_provider_arguments(
            "energy.us_eia", "エネルギー輸入依存度を考えるため、米国の石油輸出入の月次変化を調べて。", {}
        )
        self.assertEqual("petroleum_imports_and_exports", petroleum["dataset"])
        current_year = __import__("datetime").datetime.now().astimezone().year
        year_to_date = compile_provider_arguments(
            "energy.us_eia", "米国の天然ガス生産と消費が、今年どう推移しているか見たい。", {})
        self.assertEqual(f"{current_year}-01", year_to_date["start"])
        self.assertEqual(__import__("datetime").datetime.now().astimezone().strftime("%Y-%m"), year_to_date["end"])
        self.assertGreaterEqual(year_to_date["limit"], 12)
        oecd_senior = compile_provider_arguments(
            "statistics.oecd", "OECD諸国で高齢者の就業率がどう違うか、直近年で見たい。", {})
        self.assertTrue(oecd_senior["senior_employment_rate"])
        self.assertEqual(["OECD"], oecd_senior["countries"])
        self.assertEqual("5097", compile_provider_arguments(
            "econ.cepalstat", "ラテンアメリカで若年失業率が高い国を、同じ指標で見たい。", {})["indicator_id"])
        self.assertEqual("3289", compile_provider_arguments(
            "econ.cepalstat", "ブラジルの所得格差が近年どう動いているか、統計系列を探して。", {})["indicator_id"])
        self.assertEqual("2470", compile_provider_arguments(
            "econ.cepalstat", "中南米の女性労働参加率を国別・年別に比較して。", {})["indicator_id"])

    def test_provider_queries_are_compact_and_typed(self):
        earthquake = compile_provider_arguments("earth.earthquake", "過去7日間の世界中のM6以上の地震を調べて", {})
        self.assertEqual("worldwide", earthquake["query"])
        self.assertEqual(6.0, earthquake["minmagnitude"])
        self.assertNotIn("過去", str({key: value for key, value in earthquake.items() if key != "request_text"}))
        register = compile_provider_arguments("government.us_federal_register", "最近の人工知能に関するruleを5件", {})
        self.assertEqual("artificial intelligence", register["query"])
        self.assertEqual("RULE", register["type"])
        self.assertNotIn("最近", register["query"])

    def test_required_wayback_url_and_streamflow_station_are_never_sent_empty(self):
        for tool_id, prompt in (
            ("web.archive", "2018年から2024年まで、同じURLの保存スナップショットがどれくらいある？"),
            ("earth.streamflow", "近隣の複数観測地点で流量に差があるか、最新データを並べて。"),
        ):
            with self.subTest(tool_id=tool_id):
                with self.assertRaises(ExternalToolError) as raised:
                    compile_provider_arguments(tool_id, prompt, {})
                self.assertEqual("ARGUMENT_COMPILATION_FAILED", str(raised.exception))
        self.assertEqual(
            "https://openai.com/",
            compile_provider_arguments(
                "web.archive", "openai.comの過去版を見せて", {}
            )["url"],
        )
        self.assertEqual(
            "01646500",
            compile_provider_arguments(
                "earth.streamflow", "ポトマック川の直近の水位を見せて", {}
            )["station"],
        )

    def test_analysis_batch_prompts_compile_domain_specific_arguments(self):
        cases = [
            ("statistics.world_bank", "日本と韓国の経済成長を比較して", "indicator", "NY.GDP.MKTP.KD.ZG"),
            ("econ.fred", "米国の失業率と雇用者数を5年間で見る", "series_id", "UNRATE"),
            ("econ.us_labor", "米国の失業率と雇用者数を5年間で見る", "series_id", "CES0000000001"),
            ("environment.air_quality", "東京の空気質は季節でどう違う？PM2.5とNO2", "location", "東京"),
            ("health.clinical_trials", "GLP-1薬による肥満治療の臨床試験を分析して", "condition", "obesity"),
        ]
        for tool_id, prompt, key, expected in cases:
            with self.subTest(tool_id=tool_id):
                compiled = compile_provider_arguments(tool_id, prompt, {"query": prompt})
                self.assertEqual(expected, compiled[key])

        air = compile_provider_arguments("environment.air_quality", "東京の空気質は季節でどう違う？PM2.5とNO2", {})
        self.assertEqual(["pm2_5", "nitrogen_dioxide"], air["requested_metrics"])
        quake = compile_provider_arguments("earth.earthquake", "日本で過去30日間にM4以上の地震を調べて", {})
        self.assertEqual("Japan", quake["query"])
        self.assertEqual((24.0, 46.0, 122.0, 146.0), (quake["minlatitude"], quake["maxlatitude"], quake["minlongitude"], quake["maxlongitude"]))
        broad_exoplanets = compile_provider_arguments("space.exoplanets", "系外惑星の発見年と公転周期の分布を分析して", {})
        self.assertIsNone(broad_exoplanets["target"])
        self.assertTrue(broad_exoplanets["trend_analysis"])
        trade = compile_provider_arguments("trade.un_comtrade", "日本の貿易収支について分析して。2019年以降の輸出額・輸入額を同じ年ごとに並べて", {})
        self.assertEqual("392", trade["reporter_code"])
        self.assertTrue(trade["include_both_flows"])
        self.assertIn("2019", trade["periods"])
        air = compile_provider_arguments("environment.air_quality", "東京のPM2.5とNO2を季節ごとに分析して", {})
        self.assertTrue(air["seasonal"])
        fred_cpi = compile_provider_arguments("econ.fred", "米国の賃金と消費者物価を比較", {})
        self.assertEqual("CPIAUCSL", fred_cpi["series_id"])
        fred_wages = compile_provider_arguments("econ.us_labor", "米国の平均時給の推移", {})
        self.assertEqual("CES0500000003", fred_wages["series_id"])
        self.assertEqual(["JP", "KR"], compile_provider_arguments(
            "statistics.world_bank", "日本と韓国の実質GDP成長率を比較", {})["countries"])
        self.assertEqual("lithium-ion battery recycling", compile_provider_arguments(
            "research.openalex", "リチウムイオン電池のリサイクル研究を2020年以降で分析して", {}
        )["query"])
        self.assertEqual(100, compile_provider_arguments(
            "research.openalex", "リチウムイオン電池のリサイクル研究について分析して。2020年以降の論文の年別件数を示して", {}
        )["limit"])

    def test_fuzzy_emissions_and_annual_pm25_prompts_choose_correct_series(self):
        per_capita = compile_provider_arguments(
            "econ.worldbank", "日本とドイツの一人あたりCO2排出量は2000年以降でどう変化した？", {}
        )
        self.assertEqual(["EN.GHG.CO2.PC.CE.AR5"], per_capita["indicators"])
        self.assertEqual(["JP", "DE"], per_capita["countries"])
        annual_pm = compile_provider_arguments(
            "econ.worldbank", "日本の大気汚染は近年改善している？PM2.5濃度の年次推移を見せて", {}
        )
        self.assertEqual(["EN.ATM.PM25.MC.M3"], annual_pm["indicators"])
        self.assertEqual(["JP"], annual_pm["countries"])

    def test_recent_openalex_analysis_is_normalized_as_recent_and_bounded_multi_result(self):
        prompt = "近年の論文を複数参照して、大規模言語モデルの電力や水の使用量を整理して"
        compiled = compile_provider_arguments("research.openalex", prompt, {})
        self.assertEqual("recent", compiled["recency"])
        self.assertEqual(5, compiled["limit"])

    def test_fuzzy_analysis_metrics_compile_to_typed_public_provider_arguments(self):
        population = compile_provider_arguments(
            "econ.worldbank", "日本と韓国、少子高齢化の進み方ってこの20年でどう違う？出生率と高齢人口の数字で比較", {}
        )
        self.assertEqual(["JP", "KR"], population["countries"])
        self.assertEqual(["SP.DYN.TFRT.IN", "SP.POP.65UP.TO.ZS"], population["indicators"])
        self.assertEqual(20, datetime.now(timezone.utc).year - population["start_year"])

        tourism = compile_provider_arguments(
            "econ.worldbank", "コロナ後、日本に来る旅行者はどれくらい戻った？2019年と比べて最近の年次推移", {}
        )
        self.assertEqual(["ST.INT.ARVL"], tourism["indicators"])
        self.assertEqual(2019, tourism["start_year"])

        energy = compile_provider_arguments(
            "econ.worldbank", "日本と韓国はエネルギーをどれくらい海外に頼っている？輸入依存と燃料の変化を長期で比較", {}
        )
        self.assertEqual(["EG.IMP.CONS.ZS"], energy["indicators"])
        self.assertEqual(20, datetime.now(timezone.utc).year - energy["start_year"])

        emissions = compile_provider_arguments(
            "econ.worldbank", "世界のCO2排出はどの国が多くて、一人あたりで見ると順位が変わる？2000年ごろから", {}
        )
        self.assertEqual(["ALL"], [value.upper() for value in emissions["countries"]])
        self.assertEqual(["EN.GHG.CO2.MT.CE.AR5", "EN.GHG.CO2.PC.CE.AR5"], emissions["indicators"])
        self.assertEqual(2000, emissions["start_year"])

        earthquake = compile_provider_arguments(
            "earth.earthquake", "この1か月、日本のどのあたりでマグニチュード4以上の地震が目立った？地域ごとに", {}
        )
        self.assertEqual("Japan", earthquake["query"])
        self.assertEqual(4.0, earthquake["minmagnitude"])
        self.assertGreaterEqual(earthquake["limit"], 100)

        studies = compile_provider_arguments(
            "research.openalex", "大規模言語モデルの電力や水の消費について、2022年以降の研究をまとめて", {}
        )
        self.assertIn("large language model", studies["query"])
        self.assertEqual(2022, studies["from_year"])

    def test_research_since_year_is_normalized_from_natural_language(self):
        self.assertEqual(2022, normalize_research_arguments({
            "query": "大規模言語モデルの電力や水の消費について、2022年以降の研究をまとめて",
            "from_year": 2022,
        })["from_year"])

    def test_openalex_battery_recycling_search_drops_off_topic_hits(self):
        payload = {"results": [
            {"id": "W1", "title": "Net Zero Japan 2050", "publication_year": 2022,
             "abstract_inverted_index": {"climate": [0], "energy": [1]}},
            {"id": "W2", "title": "Recycling lithium-ion batteries for a circular economy",
             "publication_year": 2023,
             "abstract_inverted_index": {"battery": [0], "recycling": [1], "recovery": [2]},
             "authorships": [], "primary_location": {"source": {"display_name": "Journal"}}},
        ], "meta": {"next_cursor": None}}
        with patch("olcr_api.external_tools._request", return_value=payload):
            result = research(query="lithium-ion battery recycling", limit=5)
        self.assertEqual(["Recycling lithium-ion batteries for a circular economy"],
                         [work["title"] for work in result["data"]["works"]])

    def test_temporal_and_source_wrappers_are_compiled_at_common_boundary(self):
        tool, routed = route("2026年9月19日の東京の天気を教えて")
        self.assertEqual("weather.open_meteo", tool)
        self.assertEqual({"location": "東京", "date": "2026-09-19"},
                         {key: value for key, value in compile_provider_arguments(tool, "2026年9月19日の東京の天気を教えて", routed).items() if key != "request_text"})

        tool, routed = route("論文「Attention Is All You Need」をOpenAlexで検索して")
        self.assertEqual("research.openalex", tool)
        self.assertEqual("Attention Is All You Need", compile_provider_arguments(tool, "論文「Attention Is All You Need」をOpenAlexで検索して", routed)["query"])

        self.assertEqual("日本の首都", compile_provider_arguments(
            "knowledge.wikidata", "Wikidataで日本の首都を確認してください", {"query": "Wikidataで日本の首都を確認してください"}
        )["subject"])
        self.assertEqual("SP.POP.TOTL", compile_provider_arguments(
            "econ.worldbank", "World Bankで日本の2023年人口を調べて", {"indicator": "NY.GDP.MKTP.CD"}
        )["indicator"])
        latest_gdp = compile_provider_arguments(
            "econ.worldbank", "Show Japan's GDP year by year from 2015 through the latest available year.", {}
        )
        self.assertEqual(["JP"], latest_gdp["countries"])
        self.assertEqual((2015, None), (latest_gdp["start_year"], latest_gdp["end_year"]))
        self.assertEqual("9414290", compile_provider_arguments(
            "ocean.tides", "NOAAでサンフランシスコの潮汐を調べて", {"query": "NOAAでサンフランシスコの潮汐を調べて"}
        )["station"])
        self.assertEqual("9414290", compile_provider_arguments(
            "ocean.tides", "サンフランシスコで明日、海岸を歩くなら潮が一番低いのは何時？", {}
        )["station"])
        self.assertEqual("https://example.com", compile_provider_arguments(
            "web.archive", "Wayback Machineでhttps://example.comの履歴を調べて", {"url": "https://example.comの履歴を調べて"}
        )["url"])

    def test_f1_imf_and_ecb_keep_user_requested_season_indicator_and_currency(self):
        f1 = compile_provider_arguments(
            "sports.formula1", "2025年のコンストラクターズ順位を見せて", {}
        )
        self.assertEqual((2025, "constructors"), (f1["season"], f1["standing_type"]))

        imf = compile_provider_arguments(
            "econ.imf", "ここ10年の日本の経常収支をGDP比で見たい", {}
        )
        self.assertEqual("BCA_NGDPD", imf["indicator"])
        self.assertEqual(["JPN"], imf["countries"])
        self.assertEqual(10, datetime.now(timezone.utc).year - imf["start_year"])

        ecb = compile_provider_arguments(
            "finance.ecb", "ECBのユーロと英ポンドの参考レートを教えて", {}
        )
        self.assertEqual(("EUR", "GBP"), (ecb["base"], ecb["quote"]))

    def test_f1_imf_and_ecb_execute_the_compiled_provider_series(self):
        requests = []

        def record_request(host, path, params=None, headers=None):
            requests.append((host, path, params or {}))
            return {"MRData": {"StandingsTable": {}, "StandingsLists": []}, "data": {}}

        with patch.object(external_tools, "_request", side_effect=record_request):
            external_tools.execute("sports.formula1", {"season": 2025, "standing_type": "constructors", "limit": 10})
            external_tools.execute("econ.imf", {"indicator": "BCA_NGDPD", "countries": ["JPN"], "start_year": 2016, "limit": 10})
            external_tools.execute("finance.ecb", {"base": "EUR", "quote": "GBP", "start": "2026-01-01", "limit": 10})

        self.assertEqual("/ergast/f1/2025/constructorsStandings.json", requests[0][1])
        self.assertEqual("/external/datamapper/api/v1/BCA_NGDPD/JPN", requests[1][1])
        self.assertEqual("/service/data/EXR/D.GBP.EUR.SP00.A", requests[2][1])

    def test_provider_specific_validation_does_not_run_for_other_adapters(self):
        with patch.object(external_tools, "_request", return_value={"results": []}) as request:
            external_tools.execute("media.tv", {
                "query": "The Expanse", "indicator": "bad indicator",
            })
        self.assertEqual("api.tvmaze.com", request.call_args.args[0])

    def test_live_batch_fuzzy_misses_route_by_domain_and_user_intent(self):
        cases = {
            "フランスと日本で、通貨・公用語・人口規模を比べて。": "geo.country_facts",
            "個人データの開示請求をする人に関係する条文を、条番号付きで教えて。": "japan.law",
            "日本と米国の消費者物価上昇率は、最近どちらの方が速い？": "econ.imf",
            "近年、ウガンダで受け入れている難民数はどう変化した？": "humanitarian.unhcr",
            "取引先の名前が制裁・PEPデータに一致するか、候補と出典を確認して：Rosneft。": "sanctions.opensanctions",
            "国連安保理の制裁対象について、別名や指定日がどう登録されているか確認したい。": "sanctions.unsc",
            "“resilient” は仕事の文脈でどんな意味？例文と品詞も知りたい。": "language.dictionary",
            "米国の天然ガス生産と消費が、今年どう推移しているか見たい。": "energy.us_eia",
            "EUの議会で、データ保護に関する決議がどの段階まで進んでいるか知りたい。": "politics.eu_parliament",
            "英国政府に提出された住宅政策についての議会質問を探して。": "politics.uk_parliament",
            "カナダの気候政策に関する法案の提出者と、現在の審議状況を調べて。": "politics.ca_openparliament",
            "米議会で最近提出された医療費関連法案の審議段階と提出議員を知りたい。": "politics.us_congress",
            "連邦控訴裁判所と最高裁でSection 230の判例を比べたい。": "law.us_courts",
            "Gonzalez v. Googleの裁判所、判決日、引用関係を調べて。": "law.us_courts",
            "アフリカ開発銀行の公開データで、ケニアとタンザニアのGDP成長率を最近の年で比べて。": "econ.afdb",
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(expected, route(prompt)[0])

    def test_explicit_provider_names_outrank_incidental_lexical_matches(self):
        cases = {
            "World Bankで日本のGDPを調べて": "econ.worldbank",
            "Wikidataで歴代首相の在任期間を調べて": "knowledge.wikidata",
        }
        for prompt, expected_tool in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(expected_tool, route(prompt)[0])
        for excluded_prompt in (
            "AISStreamで東京湾の現在位置を5隻見せて",
            "Global Fishing Watchでガラパゴスの漁業活動を調べて",
            "ReliefWebでスーダンの最新人道支援レポートを5件探して",
            "Use the ReliefWeb API to find reports and return title and source",
        ):
            with self.subTest(excluded_prompt=excluded_prompt):
                self.assertIsNone(route(excluded_prompt))
        self.assertEqual("space.satellite_orbit", route("NORAD ID 25544のTLEを見せて")[0])
        self.assertIsNone(route("Return the title and source for my local note"))

    def test_fuzzy_qiita_prompt_is_compiled_to_topic_terms(self):
        prompts = [
            "Reactで状態管理をするときの最近の実装例や使い方を、役に立ちそうな技術記事から3つ探して。",
            "Reactで状態管理の最近の実装例を3つ拾って、何が違うかざっくり知りたい",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                tool, args = route(prompt)
                self.assertEqual("knowledge.qiita", tool)
                self.assertEqual("tag:react 状態管理", args["query"])
                self.assertEqual(3, args["limit"])

        fuzzy_briefing = "最近の日本語技術記事から、AIエージェントの実装でよく使われる構成と課題を複数記事をもとに整理して。"
        args = normalize_qiita_arguments({"query": fuzzy_briefing})
        self.assertEqual("AIエージェント", args["query"])
        self.assertEqual(5, args["limit"])

    def test_japanese_inflation_paraphrase_routes_to_estat(self):
        for prompt in ("最近の日本の物価の動き、ざっくり教えて。",
                       "日本のインフレ率の推移を見せて"):
            with self.subTest(prompt=prompt):
                self.assertEqual("econ.jp_estat", route(prompt)[0])

    def test_estat_trend_defaults_to_twelve_periods_and_explicit_range_wins(self):
        trend = "最近の日本の物価の動き、ざっくり教えて。"
        self.assertEqual(12, compile_provider_arguments(
            "econ.jp_estat", trend, {"limit": 1},
        )["limit"])
        explicit = "日本の物価の直近3か月の推移を見せて"
        self.assertEqual(3, compile_provider_arguments(
            "econ.jp_estat", explicit, {"limit": 1},
        )["limit"])

    def test_renewable_energy_fuzzy_topic_selects_relevant_world_bank_series(self):
        prompt = "日本の再生可能エネルギーの普及状況をブリーフィングして。最近の進展と課題を、関連する公開データを複数探して突き合わせて。"
        tool, args = route(prompt)
        self.assertEqual("econ.worldbank", tool)
        self.assertEqual("EG.FEC.RNEW.ZS", args["indicator"])
        compiled = compile_provider_arguments(tool, prompt, args)
        self.assertEqual(["JP"], compiled["countries"])
        self.assertEqual("EG.FEC.RNEW.ZS", compiled["indicator"])

        electricity_prompt = "日本の再生可能電力の割合の推移を教えて"
        self.assertEqual("EG.ELC.RNEW.ZS", route(electricity_prompt)[1]["indicator"])

        sdg = compile_provider_arguments("development.un_sdg", prompt, {})
        self.assertEqual("7.2.1", sdg["indicator"])
        self.assertEqual([392], sdg["area_codes"])


if __name__ == "__main__":
    unittest.main()
