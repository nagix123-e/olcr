"""Canonical API live-E2E acceptance fixture derived from the OLCR implementation contract.

This fixture is intentionally data-only: it never turns auth/network skips into PASS.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

ACCEPTANCE_CASES: tuple[dict[str, str], ...] = [
    {
        "tool_id": "calendar.public_holidays",
        "user_prompt": "2026年の日本の祝日を一覧で教えて",
        "expected_response_kind": "list"
    },
    {
        "tool_id": "space.launches",
        "user_prompt": "次に予定されているロケット打ち上げを3件教えて",
        "expected_response_kind": "event_list"
    },
    {
        "tool_id": "space.space_weather",
        "user_prompt": "現在の宇宙天気はどう？Kp指数と磁気嵐の状況を教えて",
        "expected_response_kind": "status"
    },
    {
        "tool_id": "space.satellite_orbit",
        "user_prompt": "ISSの最新のTLE軌道要素を見せて",
        "expected_response_kind": "record"
    },
    {
        "tool_id": "space.ephemeris",
        "user_prompt": "2026年10月1日0時UTCの火星の地球からの距離を教えて",
        "expected_response_kind": "fact"
    },
    {
        "tool_id": "space.iss_position",
        "user_prompt": "今ISSは地球のどのあたりを飛んでる？",
        "expected_response_kind": "geo_point"
    },
    {
        "tool_id": "space.exoplanets",
        "user_prompt": "TRAPPIST-1 eの半径と公転周期を教えて",
        "expected_response_kind": "record"
    },
    {
        "tool_id": "news.global_search",
        "user_prompt": "日本の半導体政策について直近の海外報道を5件探して",
        "expected_response_kind": "search_results"
    },
    {
        "tool_id": "finance.sec_filings",
        "user_prompt": "Appleの最新の10-Qを探して、提出日と主要な数値を教えて",
        "expected_response_kind": "document_list"
    },
    {
        "tool_id": "japan.diet_transcript",
        "user_prompt": "国会で生成AIについて最近どんな発言があったか探して",
        "expected_response_kind": "search_results"
    },
    {
        "tool_id": "japan.law",
        "user_prompt": "日本の個人情報保護法の目的に関する条文を見せて",
        "expected_response_kind": "legal_text"
    },
    {
        "tool_id": "geo.elevation",
        "user_prompt": "東京駅付近の標高は何メートル？",
        "expected_response_kind": "fact"
    },
    {
        "tool_id": "earth.streamflow",
        "user_prompt": "ワシントンD.C.周辺のポトマック川の最新の流量か水位を教えて",
        "expected_response_kind": "status"
    },
    {
        "tool_id": "biology.occurrence",
        "user_prompt": "東京で最近記録されたカワセミの観察記録を探して",
        "expected_response_kind": "event_list"
    },
    {
        "tool_id": "biology.phylogeny",
        "user_prompt": "犬と猫の系統上の共通祖先に近いノードを教えて",
        "expected_response_kind": "relationship"
    },
    {
        "tool_id": "biology.structure",
        "user_prompt": "ヒトのヘモグロビンのPDB構造を3件探して",
        "expected_response_kind": "record_list"
    },
    {
        "tool_id": "biology.protein",
        "user_prompt": "ヒトTP53のUniProt IDと主な機能を教えて",
        "expected_response_kind": "record"
    },
    {
        "tool_id": "security.cve",
        "user_prompt": "CVE-2021-44228の概要と影響製品を教えて",
        "expected_response_kind": "security_record"
    },
    {
        "tool_id": "security.exploit_probability",
        "user_prompt": "CVE-2021-44228の現在のEPSSスコアを教えて",
        "expected_response_kind": "fact"
    },
    {
        "tool_id": "crypto.bitcoin_network",
        "user_prompt": "今のBitcoinの推奨送金手数料とmempoolの混雑状況を教えて",
        "expected_response_kind": "status"
    },
    {
        "tool_id": "internet.domain_registration",
        "user_prompt": "openai.comの登録情報とネームサーバーを教えて",
        "expected_response_kind": "record"
    },
    {
        "tool_id": "internet.ip_info",
        "user_prompt": "8.8.8.8はどこのIP？ASNと事業者も教えて",
        "expected_response_kind": "record"
    },
    {
        "tool_id": "media.tv",
        "user_prompt": "Breaking Badのシーズン1第1話のタイトルと放送日を教えて",
        "expected_response_kind": "record"
    },
    {
        "tool_id": "games.pokemon",
        "user_prompt": "ピカチュウのタイプと基本ステータスを教えて",
        "expected_response_kind": "record"
    },
    {
        "tool_id": "games.trivia",
        "user_prompt": "科学カテゴリでmediumの4択クイズを1問出して",
        "expected_response_kind": "quiz"
    },
    {
        "tool_id": "food.recipe",
        "user_prompt": "鶏肉を使った料理を3つ提案して、材料と作り方も教えて",
        "expected_response_kind": "record_list"
    },
    {
        "tool_id": "art.met_collection",
        "user_prompt": "メトロポリタン美術館所蔵のゴッホ作品を5件探して",
        "expected_response_kind": "record_list"
    },
    {
        "tool_id": "games.pc_deals",
        "user_prompt": "Cyberpunk 2077のPC版で今安いストアを探して",
        "expected_response_kind": "deal_list"
    },
    {
        "tool_id": "games.chess",
        "user_prompt": "Hikaru Nakamuraの最近の公開対局を5局見せて",
        "expected_response_kind": "event_list"
    },
    {
        "tool_id": "sports.formula1",
        "user_prompt": "2025年F1ドライバーズランキングの最終順位を教えて",
        "expected_response_kind": "table"
    },
    {
        "tool_id": "knowledge.wikidata",
        "user_prompt": "人口1000万人以上のEU加盟国を人口順で出して",
        "expected_response_kind": "table"
    },
    {
        "tool_id": "environment.air_quality",
        "user_prompt": "東京の今日のPM2.5とAQIはどれくらい？",
        "expected_response_kind": "status"
    },
    {
        "tool_id": "geo.poi",
        "user_prompt": "東京駅から1km以内の病院を5件探して",
        "expected_response_kind": "geo_points"
    },
    {
        "tool_id": "geo.routing",
        "user_prompt": "東京駅から皇居まで車だと何kmくらい？ルートも教えて",
        "expected_response_kind": "route"
    },
    {
        "tool_id": "geo.country_facts",
        "user_prompt": "フランスの首都、通貨、主要言語、人口を教えて",
        "expected_response_kind": "record"
    },
    {
        "tool_id": "econ.worldbank",
        "user_prompt": "日本のGDPを2015年から最新年まで年ごとに見せて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "econ.oecd",
        "user_prompt": "日本の失業率の最近の推移をOECDデータで見せて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "earth.earthquakes",
        "user_prompt": "過去24時間に世界で起きたM6以上の地震を教えて",
        "expected_response_kind": "event_list"
    },
    {
        "tool_id": "earth.disasters",
        "user_prompt": "現在進行中の大規模な山火事を世界から5件教えて",
        "expected_response_kind": "event_list"
    },
    {
        "tool_id": "ocean.tides",
        "user_prompt": "サンフランシスコの次の満潮と干潮の時刻を教えて",
        "expected_response_kind": "forecast"
    },
    {
        "tool_id": "astronomy.sunrise",
        "user_prompt": "東京の明日の日の出と日の入りは何時？",
        "expected_response_kind": "status"
    },
    {
        "tool_id": "books.openlibrary",
        "user_prompt": "ISBN 9780140328721の本のタイトルと著者を教えて",
        "expected_response_kind": "record"
    },
    {
        "tool_id": "web.archive",
        "user_prompt": "openai.comの2023年のWayback Machineスナップショットをいくつか探して",
        "expected_response_kind": "snapshot_list"
    },
    {
        "tool_id": "chemistry.pubchem",
        "user_prompt": "カフェインの分子式、分子量、主な性質を教えて",
        "expected_response_kind": "record"
    },
    {
        "tool_id": "health.clinical_trials",
        "user_prompt": "2型糖尿病について日本で募集中の臨床試験を探して",
        "expected_response_kind": "record_list"
    },
    {
        "tool_id": "health.openfda",
        "user_prompt": "ibuprofenに関するFDAの副作用報告を最近のものから見せて",
        "expected_response_kind": "event_list"
    },
    {
        "tool_id": "food.product",
        "user_prompt": "バーコード3017620422003の商品名と原材料、栄養情報を教えて",
        "expected_response_kind": "record"
    },
    {
        "tool_id": "news.hackernews",
        "user_prompt": "Hacker Newsの今のトップ記事を10件教えて",
        "expected_response_kind": "search_results"
    },
    {
        "tool_id": "knowledge.devto",
        "user_prompt": "Find recent DEV Community articles about Python async patterns and show title, author, tags, publication date, reactions, and links",
        "expected_response_kind": "search_results"
    },
    {
        "tool_id": "dev.github",
        "user_prompt": "FastAPIのGitHubで最新リリースと公開日を教えて",
        "expected_response_kind": "record"
    },
    {
        "tool_id": "aviation.opensky",
        "user_prompt": "OpenSkyで今の羽田空港周辺の航空機を見せて",
        "expected_response_kind": "geo_points"
    },
    {
        "tool_id": "language.dictionary",
        "user_prompt": "serendipityの意味、品詞、発音を教えて",
        "expected_response_kind": "record"
    },
    {
        "tool_id": "language.translation",
        "user_prompt": "Good morning, how are you? を自然な日本語に訳して",
        "expected_response_kind": "text"
    },
    {
        "tool_id": "render.quickchart",
        "user_prompt": "A=10、B=20、C=15の棒グラフを画像にして",
        "expected_response_kind": "media"
    },
    {
        "tool_id": "policy.us_federal_register",
        "user_prompt": "米政府のAIに関する最近のFederal Register文書を5件探して",
        "expected_response_kind": "document_list"
    },
    {
        "tool_id": "conflict.acled",
        "user_prompt": "ACLEDで直近7日間のウクライナの戦闘イベントを地域別に見せて",
        "expected_response_kind": "event_list"
    },
    {
        "tool_id": "conflict.ucdp",
        "user_prompt": "UCDPでウクライナの最近の紛争イベントを5件見せて",
        "expected_response_kind": "event_list"
    },
    {
        "tool_id": "humanitarian.reliefweb",
        "user_prompt": "スーダン情勢について最新の人道支援レポートを5件探して",
        "expected_response_kind": "document_list"
    },
    {
        "tool_id": "humanitarian.hdx",
        "user_prompt": "スーダンの食料安全保障や避難民に関する最新指標を見せて",
        "expected_response_kind": "table"
    },
    {
        "tool_id": "humanitarian.unhcr",
        "user_prompt": "トルコにいるシリア難民の最新公表人数を教えて",
        "expected_response_kind": "table"
    },
    {
        "tool_id": "sanctions.unsc",
        "user_prompt": "国連安保理の制裁リストで北朝鮮関連の指定対象を探して",
        "expected_response_kind": "record_list"
    },
    {
        "tool_id": "sanctions.opensanctions",
        "user_prompt": "OpenSanctionsでRosneftに関連する制裁情報を確認して",
        "expected_response_kind": "record_list"
    },
    {
        "tool_id": "econ.imf",
        "user_prompt": "IMFデータで日本の消費者物価上昇率の最近の推移を見せて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "trade.wto",
        "user_prompt": "WTOデータで日本の商品輸出額の最近の推移を見せて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "trade.un_comtrade",
        "user_prompt": "2024年の日本から米国への乗用車輸出額をUN Comtradeで教えて",
        "expected_response_kind": "table"
    },
    {
        "tool_id": "finance.bis",
        "user_prompt": "BISデータで日本の銀行信用の最近の推移を見せて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "development.un_sdg",
        "user_prompt": "日本のSDG 8.5.2の最新値を教えて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "aid.iati",
        "user_prompt": "ケニアの教育分野で日本が関わるIATI登録プロジェクトを探して",
        "expected_response_kind": "record_list"
    },
    {
        "tool_id": "maritime.ais_live",
        "user_prompt": "AISstreamで今の東京湾周辺の船を5隻見せて",
        "expected_response_kind": "geo_points"
    },
    {
        "tool_id": "maritime.vessel_intelligence",
        "user_prompt": "Global Fishing Watchでガラパゴス周辺の最近の漁業活動を確認して",
        "expected_response_kind": "geo_points"
    },
    {
        "tool_id": "aviation.adsb_live",
        "user_prompt": "ADSB.lolで今の羽田空港周辺の航空機を見せて",
        "expected_response_kind": "geo_points"
    },
    {
        "tool_id": "econ.adb",
        "user_prompt": "ADBデータでフィリピンのGDP成長率の最近の推移を見せて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "econ.afdb",
        "user_prompt": "AfDBのデータでケニアのGDP成長率の最近の推移を見せて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "econ.cepalstat",
        "user_prompt": "CEPALSTATでアルゼンチンの消費者物価の最近の推移を見せて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "development.idb",
        "user_prompt": "ブラジルで現在進行中のIDBプロジェクトを5件探して",
        "expected_response_kind": "record_list"
    },
    {
        "tool_id": "econ.eurostat",
        "user_prompt": "Eurostatでドイツのインフレ率の最近の推移を見せて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "finance.ecb",
        "user_prompt": "ECBの最新のユーロ対ドル参考為替レートを教えて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "politics.eu_parliament",
        "user_prompt": "欧州議会でAI Actに関連する最近の議会文書を探して",
        "expected_response_kind": "document_list"
    },
    {
        "tool_id": "law.eu_cellar",
        "user_prompt": "EU AI Actの公式法令文書を探して、文書番号と公開日を教えて",
        "expected_response_kind": "document_list"
    },
    {
        "tool_id": "politics.uk_parliament",
        "user_prompt": "英国議会でAIに関係する最近の法案や審議を探して",
        "expected_response_kind": "document_list"
    },
    {
        "tool_id": "politics.de_bundestag",
        "user_prompt": "ドイツ連邦議会でAIに関係する最近の議案や文書を探して",
        "expected_response_kind": "document_list"
    },
    {
        "tool_id": "gov.in_open_data",
        "user_prompt": "インドの最新の発電量に関する政府オープンデータを探して",
        "expected_response_kind": "table"
    },
    {
        "tool_id": "politics.br_chamber",
        "user_prompt": "ブラジル下院でAIに関係する最近の法案を探して",
        "expected_response_kind": "document_list"
    },
    {
        "tool_id": "politics.br_senate",
        "user_prompt": "ブラジル上院でAIに関係する最近の法案や審議を探して",
        "expected_response_kind": "document_list"
    },
    {
        "tool_id": "politics.ca_openparliament",
        "user_prompt": "カナダ議会でAIに関係する最近の法案を探して",
        "expected_response_kind": "document_list"
    },
    {
        "tool_id": "politics.us_congress",
        "user_prompt": "米議会でAIに関係する最近の法案を5件探して",
        "expected_response_kind": "document_list"
    },
    {
        "tool_id": "policy.us_rulemaking",
        "user_prompt": "Regulations.govでAIに関係する最近の規則案やコメント募集を探して",
        "expected_response_kind": "document_list"
    },
    {
        "tool_id": "politics.us_campaign_finance",
        "user_prompt": "2024年米大統領選の主要候補の選挙資金収入を比較して",
        "expected_response_kind": "table"
    },
    {
        "tool_id": "gov.us_spending",
        "user_prompt": "2024年度にNASAからSpaceXへ出た主な契約支出を探して",
        "expected_response_kind": "table"
    },
    {
        "tool_id": "law.us_courts",
        "user_prompt": "CourtListenerでSection 230に言及する最近の判例を探して",
        "expected_response_kind": "document_list"
    },
    {
        "tool_id": "defense.us_contracts",
        "user_prompt": "2025年の米国防総省からLockheed Martinへの主な契約を探して",
        "expected_response_kind": "table"
    },
    {
        "tool_id": "sanctions.us_ofac",
        "user_prompt": "OFACの制裁リストでIran Airに関する登録を確認して",
        "expected_response_kind": "record_list"
    },
    {
        "tool_id": "econ.fred",
        "user_prompt": "FREDで米政策金利の最近の推移を見せて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "econ.us_labor",
        "user_prompt": "米国の最新の失業率をBLSデータで教えて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "econ.us_bea",
        "user_prompt": "BEAデータで米国の2025年実質GDP成長率を教えて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "econ.us_census",
        "user_prompt": "米国の最新の人口推計をCensusデータで教えて",
        "expected_response_kind": "fact"
    },
    {
        "tool_id": "energy.us_eia",
        "user_prompt": "EIAで米国の原油在庫の最新値と最近の推移を見せて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "econ.jp_estat",
        "user_prompt": "e-Statで日本の最新の消費者物価指数を教えて",
        "expected_response_kind": "time_series"
    },
    {
        "tool_id": "maritime.us_vessel_traffic",
        "user_prompt": "MarineCadastreで2023年のニューヨーク港周辺の船舶交通を確認して",
        "expected_response_kind": "geo_points"
    },
    {
        "tool_id": "aviation.operational_weather",
        "user_prompt": "羽田空港RJTTの最新METARとTAFを教えて",
        "expected_response_kind": "status"
    },
    {
        "tool_id": "maritime.norway_ais",
        "user_prompt": "BarentsWatchで今のトロムソ周辺の船舶を5隻見せて",
        "expected_response_kind": "geo_points"
    },
    {
        "tool_id": "knowledge.qiita",
        "user_prompt": "QiitaからPythonやReactの実装・使い方に関する技術記事を探して、タイトル、投稿者、タグ、更新日、本文の要点、記事リンクを示して",
        "expected_response_kind": "search_results"
    }
]

MANIFEST_SOURCE_COUNT = 102

# These provider cases remain in the historical 102-source fixture above so
# prior benchmark evidence and fixture hashes stay reproducible. They are not
# part of the shipped product surface until their provider/account/network
# gates are resolved and a fresh acceptance pass is recorded.
PRODUCTION_EXCLUDED_API_IDS = frozenset({
    "conflict.acled",             # provider-side access permission denied
    "trade.wto",                  # user-side credential issuance pending
    "aid.iati",                   # user-side credential issuance pending
    "politics.de_bundestag",      # user-side credential issuance pending
    "maritime.norway_ais",        # user-side credentials pending
    "gov.in_open_data",           # provider portal maintenance
    "econ.us_census",             # submitted credential rejected upstream
    "econ.us_bea",                # submitted User ID inactive upstream
    "humanitarian.reliefweb",     # required appname not configured
    "maritime.ais_live",          # no live records observed in retest
    "maritime.vessel_intelligence", # provider/network timeout
    "news.global_search",         # provider rate limiting
    "finance.sec_filings",        # SEC endpoint returned HTTP 403
})


PRODUCTION_ACCEPTANCE_CASES: tuple[dict[str, str], ...] = tuple(
    case for case in ACCEPTANCE_CASES
    if case["tool_id"] not in PRODUCTION_EXCLUDED_API_IDS
)


def production_tool_ids() -> tuple[str, ...]:
    """Tool IDs intentionally exposed by the current product build."""
    return tuple(item["tool_id"] for item in PRODUCTION_ACCEPTANCE_CASES)


def acceptance_tool_ids() -> tuple[str, ...]:
    return tuple(item["tool_id"] for item in ACCEPTANCE_CASES)


def acceptance_fixture_hash() -> str:
    payload = json.dumps(ACCEPTANCE_CASES, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_acceptance_fixture() -> dict[str, Any]:
    ids = acceptance_tool_ids()
    duplicates = sorted({tool_id for tool_id in ids if ids.count(tool_id) > 1})
    malformed = [item.get("tool_id", "") for item in ACCEPTANCE_CASES
                 if not item.get("tool_id") or not item.get("user_prompt") or not item.get("expected_response_kind")]
    return {
        "source_count": MANIFEST_SOURCE_COUNT,
        "case_count": len(ACCEPTANCE_CASES),
        "unique_tool_ids": len(set(ids)),
        "duplicate_tool_ids": duplicates,
        "malformed_cases": malformed,
        "skip_is_pass": False,
        "auth_blocked_is_pass": False,
        "fixture_hash": acceptance_fixture_hash(),
        "valid": len(ACCEPTANCE_CASES) == MANIFEST_SOURCE_COUNT and len(set(ids)) == MANIFEST_SOURCE_COUNT and not duplicates and not malformed,
    }


def manifest_registry_diff(registry_ids: set[str] | list[str] | tuple[str, ...]) -> dict[str, Any]:
    expected = set(production_tool_ids())
    actual = set(registry_ids)
    return {
        "expected_count": len(expected),
        "actual_count": len(actual),
        "missing": sorted(expected - actual),
        "extra": sorted(actual - expected),
        "exact_match": expected == actual,
    }
