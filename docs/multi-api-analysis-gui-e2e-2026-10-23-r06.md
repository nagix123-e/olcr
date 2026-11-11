# Multi-API Analysis GUI E2E — r06

## 2026-10-23: active test list revised

The active matrix contains **32 cases**. Eighteen underspecified prompts were removed by user request. Original IDs are retained; no replacement prompts or renumbering were introduced. Regression, live API, and GUI E2E batches must read only `cases` from `backend/tests/fixtures/multi_api_analysis_cases.json`.

The excluded prompts and reasons are archived in `backend/tests/fixtures/multi_api_analysis_excluded_cases.json`; this archive is not an execution list. Missing mandatory subject identifiers and the resulting valid clarification responses must not count as application failures. Existing run totals below are historical, from before this revision.

Active IDs: 01, 02, 03, 04, 05, 06, 07, 08, 09, 11, 12, 13, 14, 15, 18, 19, 21, 22, 23, 24, 25, 26, 28, 29, 34, 37, 38, 39, 40, 41, 42, 48

- Date: 2026-10-23 (Asia/Tokyo)
- Run/build: `analysis-r06-20261023` / `olcr-analysis-r06-20261023`
- Target: isolated OLCR bundle and profile `/private/tmp/olcr-e2e-analysis-r06-20261023-profile`
- Execution: all fixture cases 1–50 were entered once, sequentially, in **one GUI conversation**. Database confirms 50 user + 50 assistant messages in a single conversation (`1175669a-1ce2-4d2e-b801-262e1e35f27e`). No retries.
- Fixture: `backend/tests/fixtures/multi_api_analysis_cases.json`
- Evidence: isolated SQLite run DB plus launcher log. UI/DB results are observations, not a claim that each analysis succeeded.

## Aggregate

The execution counts below are historical DB records, not complete network-attempt counts. Investigation confirmed that API failures before an all-source fallback were omitted from those records; this has been repaired and is documented in `multi-api-analysis-r06-repair-2026-10-23.md`.

Current-scope r06 aggregate: **32 valid cases** (18 excluded), recorded API executions **30 success / 27 failure**. All 5 clarification-only prompts were excluded; clarification for missing mandatory targets is correct behavior. These are API execution counts, not case pass rates. Historical totals below remain unchanged.

- 50 prompts submitted; 50 assistant turns recorded.
- Task states: `completed / autonomous_multi_api_analysis: 45, waiting_for_confirmation / API_ARGUMENT_CLARIFICATION: 5`
- Recorded external API tool executions: `failed: 44, success: 45` (89 total).
- The 5 confirmation-wait cases (10, 16, 17, 47, 49) were underspecified prompts, now excluded. Asking for their missing mandatory targets is valid behavior and is not counted as an application failure.
- The main recurring classes were argument compilation failures, unsupported provider capabilities, empty/partial data, retrieval instead of structured API routing, and analysis/synthesis that stopped at evidence cards.

## Per-case results

`API success/failure` counts are execution records, not whether the user’s requested cross-source analysis was achieved. Providers are those visible in the assistant turn. Cause column is a likely explanation based on task state, tool errors, and displayed result; it is a hypothesis to guide a code-level audit.

| # | Task state / route | API success/failure | Failed calls | Visible sources | UI result | Predicted cause |
|---:|---|---:|---|---|---|---|
| 01 | completed / EXTERNAL_API | 1/0 | — | World Bank | 取得できた公開データの範囲では、次の数値が確認できます。 World Bankの実質GDP成長率では日本の値が2021年の3.56%から2025年の1.19%へ低下しています（−2.37ポイント） 。 World Bankの失業率（ILOモデル推計）では日本の値が2021年の2.81%から2025年の2.45%へ低下しています（−0.36ポイント） 。… | 取得・定量比較とも完了し、API失敗もなし。 |
| 02 | completed / EXTERNAL_API | 3/0 | — | World Bank, Federal Reserve FRED, U.S. Bureau of Labor Statistics | 取得できた公開データの範囲では、次の数値が確認できます。 World Bankの実質GDP成長率では米国の値が2021年の6.15%から2025年の2.16%へ低下しています（−3.99ポイント） 。 World Bankの失業率（ILOモデル推計）では米国の値が2021年の5.35%から2025年の4.2%へ低下しています（−1.15ポイント） 。 … | 取得・定量比較とも完了し、API失敗もなし。 |
| 03 | completed / EXTERNAL_API | 1/1 | trade.un_comtrade: ARGUMENT_COMPILATION_FAILED | World Bank | 取得できた公開データの範囲では、次の数値が確認できます。 World Bankの実質GDP成長率ではBrunei Darussalamの値が2021年の-1.59%から2025年の0.67%へ上昇しています（+2.26ポイント） 。 World Bankの実質GDP成長率ではIndonesiaの値が2021年の3.7%から2025年の5.11%へ上昇し… | API引数コンパイラが必須フィールドを生成できず要求失敗。 |
| 04 | completed / EXTERNAL_API | 1/3 | econ.worldbank: PROVIDER_PARTIAL_COVERAGE；econ.imf: PROVIDER_UNAVAILABLE；development.un_sdg: PROVIDER_CAPABILITY_UNSUPPORTED | World Bank | 取得できた公開データの範囲では、次の数値が確認できます。 World Bankの実質GDP成長率ではAngolaの値が2021年の1.09%から2025年の3.13%へ上昇しています（+2.04ポイント） 。 World Bankの実質GDP成長率ではBurkina Fasoの値が2021年の6.94%から2025年の5.29%へ低下しています（−1.… | 要求した指標/粒度をAPIアダプターが提供できない。能力照合と代替API選定不足。 |
| 05 | completed / EXTERNAL_API | 1/1 | econ.worldbank: PROVIDER_PARTIAL_COVERAGE | World Bank | 取得できた公開データの範囲では、次の数値が確認できます。 World Bankの実質GDP成長率ではArgentinaの値が2021年の10.44%から2025年の4.37%へ低下しています（−6.07ポイント） 。 World Bankの消費者物価上昇率ではArgentinaの値が2021年の48.41%から2024年の219.88%へ上昇しています… | 地域・指標・期間のカバレッジが部分的。計画時の能力照合が不足。 |
| 06 | completed / EXTERNAL_API | 2/0 | — | Eurostat, World Bank | 取得できた公開データの範囲では、次の数値が確認できます。 World Bankの実質GDP成長率ではAustriaの値が2021年の4.92%から2025年の0.62%へ低下しています（−4.31ポイント） 。 World Bankの消費者物価上昇率ではAustriaの値が2021年の2.77%から2025年の3.53%へ上昇しています（+0.76ポイ… | 定量分析は生成したが、未取得項目が残り要求全体では部分成功。 |
| 07 | completed / RETRIEVAL | 0/0 | — | なし（確認要求/検索資料のみ） | 検索資料: **財務省貿易統計 Trade Statistics of Japan** | API全件失敗後の検索フォールバック。ログにAPI実行計画・通信があり、失敗試行のDB記録漏れによって0件に見えていた。 |
| 08 | completed / RETRIEVAL | 0/0 | — | なし（確認要求/検索資料のみ） | 検索資料: **国際線就航状況、コロナ前比「93%」に回復：国別1位は「韓国」【航空便動向まとめ】 ／ 訪日ラボ** | API全件失敗後の検索フォールバック。ログにAPI実行計画・通信があり、失敗試行のDB記録漏れによって0件に見えていた。 |
| 09 | completed / EXTERNAL_API | 2/1 | gov.us_spending: PROVIDER_CAPABILITY_UNSUPPORTED | Federal Reserve FRED, U.S. Bureau of Labor Statistics | 取得できた公開データの範囲では、次の数値が確認できます。 Federal Reserve FREDの米国消費者物価上昇率（前年比）では米国の値が2025-09-01の3.02%から2026-09-01の3.72%へ上昇しています（+0.69ポイント） 。 U.S. Bureau of Labor Statisticsの米国失業率では米国の値が2021-… | 要求した指標/粒度をAPIアダプターが提供できない。能力照合と代替API選定不足。 |
| 10 | waiting_for_confirmation / EXTERNAL_API | 0/0 | — | なし（確認要求/検索資料のみ） | 、次の情報が必要です。 - 対象を特定するため、国・地域、名称、または識別子をもう少し具体的に指定してください。 | **除外済み（課題不備）** 国・地域がなく、住宅費・物価・家計指標の対象を決定できない。 過去の実行記録としてのみ保持。 |
| 11 | completed / EXTERNAL_API | 1/1 | econ.worldbank: ARGUMENT_COMPILATION_FAILED | Federal Reserve FRED | 取得できた公開データの範囲では、次の数値が確認できます。 Federal Reserve FREDの米国政策金利では米国の値が2025-10-01の4.09%から2026-09-01の3.75%へ低下しています（−0.34ポイント） 。 Federal Reserve FREDの米国消費者物価上昇率（前年比）では米国の値が2025-09-01の3.02… | API引数コンパイラが必須フィールドを生成できず要求失敗。 |
| 12 | completed / RETRIEVAL | 0/0 | — | なし（確認要求/検索資料のみ） | 検索資料: **図2 地域別人口の推移｜早わかり グラフでみる長期労働統計｜労働政策研究・研修機構（JILPT）** | API全件失敗後の検索フォールバック。ログにAPI実行計画・通信があり、失敗試行のDB記録漏れによって0件に見えていた。 |
| 13 | completed / EXTERNAL_API | 1/1 | econ.worldbank: PROVIDER_PARTIAL_COVERAGE | World Bank | 取得できた公開データの範囲では、次の数値が確認できます。 World Bankの最終エネルギー消費に占める再生可能エネルギーの割合ではAfghanistanの値が2021年の20%から2022年の20%へ横ばいしています（0ポイント） 。 World Bankの最終エネルギー消費に占める再生可能エネルギーの割合の2023, 2024, 2025年はAP… | 地域・指標・期間のカバレッジが部分的。計画時の能力照合が不足。 |
| 14 | completed / RETRIEVAL | 0/0 | — | なし（確認要求/検索資料のみ） | 検索資料: **アメリカの発電割合は？ 再エネ発電率向上の理由や今後の展望を解説** | API全件失敗後の検索フォールバック。ログにAPI実行計画・通信があり、失敗試行のDB記録漏れによって0件に見えていた。 |
| 15 | completed / EXTERNAL_API | 1/2 | earth.disasters: PROVIDER_CAPABILITY_UNSUPPORTED；humanitarian.hdx: PROVIDER_EMPTY | Earth | USGSが指定条件で返した5件を、イベント座標から概略地域に分類しました。 地域別件数: - 東・東南アジア: 3件 - インド太平洋（東南アジア〜メラネシア）: 1件 - 北米・中米・カリブ海: 1件 返却イベントのマグニチュードは5.0〜7.0（平均5.44）。 これはAPIが返した対象・期間・件数上限内のイベント集計で、地域境界は概略です。取得上… | 要求した指標/粒度をAPIアダプターが提供できない。能力照合と代替API選定不足。 |
| 16 | waiting_for_confirmation / EXTERNAL_API | 0/0 | — | なし（確認要求/検索資料のみ） | 近隣の河川観測所を特定するため、調べたい地域・河川名、またはUSGS観測所番号を指定してください。 | **除外済み（課題不備）** 河川・流域・観測所・座標がなく、水位と標高の照合対象を決定できない。 過去の実行記録としてのみ保持。 |
| 17 | waiting_for_confirmation / EXTERNAL_API | 0/0 | — | なし（確認要求/検索資料のみ） | 潮汐を調べる沿岸地域または観測所番号を指定してください。 | **除外済み（課題不備）** 沿岸地域・観測所がなく、潮汐と災害の照合対象を決定できない。 過去の実行記録としてのみ保持。 |
| 18 | completed / EXTERNAL_API | 1/0 | — | World Bank | 取得できた公開データの範囲では、次の数値が確認できます。 World Bankの電力発電量に占める再生可能電力の割合の最新の数値観測はAfghanistan・2021年の78.23%です 。 World Bankの電力発電量に占める再生可能電力の割合の2022, 2023, 2024, 2025年はAPI応答に数値がありません。空欄の理由（未発表・未更… | 取得・定量比較とも完了し、API失敗もなし。 |
| 19 | completed / RETRIEVAL | 0/0 | — | なし（確認要求/検索資料のみ） | 検索資料: **EU、GHG排出量削減を牽引も、注視される域内産業の競争力強化との両立(EU) ／ ビジネス短信 ―ジェトロの海外ニュース - ジェトロ** | API全件失敗後の検索フォールバック。ログにAPI実行計画・通信があり、失敗試行のDB記録漏れによって0件に見えていた。 |
| 20 | completed / EXTERNAL_API | 1/2 | econ.worldbank: ARGUMENT_COMPILATION_FAILED；humanitarian.hdx: PROVIDER_EMPTY | Earth | 取得したAPI出力を根拠として示します。統合できる数値系列がないため、分析は限定されます。 | **除外済み（課題不備）** 地域・災害イベントがなく、災害、人口、人道支援を結びつける共通対象がない。 過去の実行記録としてのみ保持。 |
| 21 | completed / EXTERNAL_API | 1/1 | development.un_sdg: PROVIDER_PARTIAL_COVERAGE | UN SDG API | 取得できた公開データの範囲では、次の数値が確認できます。 UN SDG APIのLevel of water stress: freshwater withdrawal as a proportion of available freshwater resources (%)ではAfghanistanの値が2000年の0.42%から2001年の54.7… | 地域・指標・期間のカバレッジが部分的。計画時の能力照合が不足。 |
| 22 | completed / EXTERNAL_API | 1/1 | weather.open_meteo: LOCATION_NOT_FOUND | NOAA SWPC | 取得したAPI出力を根拠として示します。統合できる数値系列がないため、分析は限定されます。 | APIは呼ばれたが数値系列/共通軸を統合できず、証拠の列挙に留まった。 |
| 23 | completed / EXTERNAL_API | 1/1 | earth.disasters: PROVIDER_CAPABILITY_UNSUPPORTED | Earth | USGSが指定条件で返した32件を、イベント座標から概略地域に分類しました。 地域別件数: - インド太平洋（東南アジア〜メラネシア）: 14件 - 東・東南アジア: 7件 - 北米・中米・カリブ海: 3件 - アフリカ周辺: 2件 - 南米・南大西洋: 2件 - 欧州・地中海・西アジア: 2件 - アラスカ・アリューシャン列島: 1件 - 地域特定不… | 要求した指標/粒度をAPIアダプターが提供できない。能力照合と代替API選定不足。 |
| 24 | completed / RETRIEVAL | 0/0 | — | なし（確認要求/検索資料のみ） | 検索資料: **世界の電力 ／ 統計・マップ ／ 自然エネルギー財団** | API全件失敗後の検索フォールバック。ログにAPI実行計画・通信があり、失敗試行のDB記録漏れによって0件に見えていた。 |
| 25 | completed / EXTERNAL_API | 2/1 | humanitarian.hdx: PROVIDER_EMPTY | UCDP Georeferenced Event Dataset, UNHCR Refugee Data Finder | 取得できた公開データの範囲では、次の数値が確認できます。 UCDP Georeferenced Event Datasetは2025年についてAPI応答に数値がありません。空欄の理由（未発表・未更新など）は、この応答だけでは判断できません 。 UNHCR Refugee Data FinderのrefugeesではUkraineの値が2021年の275… | プロバイダー応答が空。クエリ条件/対象名/期間を確認する必要。 |
| 26 | completed / EXTERNAL_API | 2/2 | conflict.ucdp: PROVIDER_PARTIAL_COVERAGE；humanitarian.hdx: PROVIDER_EMPTY | UNHCR Refugee Data Finder, UCDP Georeferenced Event Dataset | 取得できた公開データの範囲では、次の数値が確認できます。 UNHCR Refugee Data FinderのrefugeesではSudanの値が2021年の825290人から2025年の2820737人へ上昇しています（+1995447人） 。 UNHCR Refugee Data Finderのasylum_seekersではSudanの値が202… | プロバイダー応答が空。クエリ条件/対象名/期間を確認する必要。 |
| 27 | completed / EXTERNAL_API | 1/1 | sanctions.opensanctions: PROVIDER_EMPTY | UCDP Georeferenced Event Dataset | 取得できた公開データの範囲では、次の数値が確認できます。 UCDP Georeferenced Event Datasetは2025年についてAPI応答に数値がありません。空欄の理由（未発表・未更新など）は、この応答だけでは判断できません 。 UCDP Georeferenced Event Datasetは2025年についてAPI応答に数値がありませ… | **除外済み（課題不備）** 制裁対象の組織・地域がなく、紛争イベントとの地理照合を評価できない。 過去の実行記録としてのみ保持。 |
| 28 | completed / RETRIEVAL | 0/0 | — | なし（確認要求/検索資料のみ） | 検索資料: **経済制裁措置及び対象者リスト : 財務省** | API全件失敗後の検索フォールバック。ログにAPI実行計画・通信があり、失敗試行のDB記録漏れによって0件に見えていた。 |
| 29 | completed / EXTERNAL_API | 2/1 | humanitarian.unhcr: PROVIDER_PARTIAL_COVERAGE | UNHCR Refugee Data Finder, World Bank | 取得できた公開データの範囲では、次の数値が確認できます。 UNHCR Refugee Data Finderのrefugeesの最新の数値観測はSudan・2025年の809816人です 。 UNHCR Refugee Data Finderのasylum_seekersの最新の数値観測はSudan・2025年の50147人です 。 UNHCR Ref… | 地域・指標・期間のカバレッジが部分的。計画時の能力照合が不足。 |
| 30 | completed / EXTERNAL_API | 1/1 | humanitarian.hdx: PROVIDER_EMPTY | Earth | 取得したAPI出力を根拠として示します。統合できる数値系列がないため、分析は限定されます。 | **除外済み（課題不備）** 災害名・被災地がなく、支援要請と災害記録を結びつけられない。 過去の実行記録としてのみ保持。 |
| 31 | completed / EXTERNAL_API | 1/2 | aviation.opensky: ARGUMENT_COMPILATION_FAILED；aviation.operational_weather: ARGUMENT_COMPILATION_FAILED | UCDP Georeferenced Event Dataset | 取得できた公開データの範囲では、次の数値が確認できます。 UCDP Georeferenced Event Datasetは2025年についてAPI応答に数値がありません。空欄の理由（未発表・未更新など）は、この応答だけでは判断できません 。 UCDP Georeferenced Event Datasetは2025年についてAPI応答に数値がありませ… | **除外済み（課題不備）** 紛争地域・空域・空港がなく、運航と航空気象の観測範囲を決定できない。 過去の実行記録としてのみ保持。 |
| 32 | completed / EXTERNAL_API | 3/2 | humanitarian.hdx: PROVIDER_PARTIAL_COVERAGE；humanitarian.unhcr: PROVIDER_PARTIAL_COVERAGE | Humanitarian Data Exchange, UCDP Georeferenced Event Dataset, UNHCR Refugee Data Finder | 取得できた公開データの範囲では、次の数値が確認できます。 UCDP Georeferenced Event Datasetは2025年についてAPI応答に数値がありません。空欄の理由（未発表・未更新など）は、この応答だけでは判断できません 。 UCDP Georeferenced Event Datasetは2025年についてAPI応答に数値がありませ… | **除外済み（課題不備）** 紛争地域・国がなく、食料危機・避難民・支援ニーズの比較対象を決定できない。 過去の実行記録としてのみ保持。 |
| 33 | completed / RETRIEVAL | 0/0 | — | なし（確認要求/検索資料のみ） | 検索資料: **【2026年最新】経済制裁とは？対象国・目的・企業の自主規制との違いをわかりやすく解説** | **除外済み（課題不備）** 企業名・国・品目がなく、制裁情報と貿易フローを結びつける対象がない。 過去の実行記録としてのみ保持。 |
| 34 | completed / EXTERNAL_API | 1/2 | conflict.ucdp: PROVIDER_EMPTY；humanitarian.hdx: PROVIDER_EMPTY | UNHCR Refugee Data Finder | 取得できた公開データの範囲では、次の数値が確認できます。 UNHCR Refugee Data FinderのrefugeesではColombiaの値が2021年の115792人から2025年の79669人へ低下しています（−36123人） 。 UNHCR Refugee Data Finderのasylum_seekersではColombiaの値が2… | プロバイダー応答が空。クエリ条件/対象名/期間を確認する必要。 |
| 35 | completed / RETRIEVAL | 0/0 | — | なし（確認要求/検索資料のみ） | 検索資料: **持続可能な開発目標（SDGs）ー 事実と数字 ／ 国連広報センター** | **除外済み（課題不備）** 危機・対象地域・SDG指標がなく、何を比較するか評価できない。 過去の実行記録としてのみ保持。 |
| 36 | completed / EXTERNAL_API | 1/1 | sanctions.us_ofac: PROVIDER_EMPTY | USAspending.gov | 取得したAPI出力を根拠として示します。統合できる数値系列がないため、分析は限定されます。 | **除外済み（課題不備）** 制裁対象企業・団体・措置がなく、米国契約との関連を照合できない。 過去の実行記録としてのみ保持。 |
| 37 | completed / EXTERNAL_API | 1/2 | gov.us_spending: PROVIDER_CAPABILITY_UNSUPPORTED；politics.us_congress: PROVIDER_EMPTY | Policy | 取得したAPI出力を根拠として示します。統合できる数値系列がないため、分析は限定されます。 | 要求した指標/粒度をAPIアダプターが提供できない。能力照合と代替API選定不足。 |
| 38 | completed / RETRIEVAL | 0/0 | — | なし（確認要求/検索資料のみ） | 検索資料: **日本の「AI新法」とは？概要から施行後の変化、今後の展望まで徹底解説 ／ NOB DATA株式会社 ／ 福岡 データサイエンティスト** | API全件失敗後の検索フォールバック。ログにAPI実行計画・通信があり、失敗試行のDB記録漏れによって0件に見えていた。 |
| 39 | completed / EXTERNAL_API | 2/2 | econ.worldbank: PROVIDER_EMPTY；law.eu_cellar: PROVIDER_PARTIAL_COVERAGE | European Parliament Open Data, EU Cellar | 取得できた公開データの範囲では、次の数値が確認できます。 European Parliament Open Dataは2021-04-29, 2021-05-20, 2021-10-05, 2023-05-10, 2024-04-11年についてAPI応答に数値がありません。空欄の理由（未発表・未更新など）は、この応答だけでは判断できません 。 | プロバイダー応答が空。クエリ条件/対象名/期間を確認する必要。 |
| 40 | completed / RETRIEVAL | 0/0 | — | なし（確認要求/検索資料のみ） | 検索資料: **【ヘルスケア】イギリスの医薬品・医療機器規制の概要とBrexit後の最新動向 ／ ブログ ／ Our Eyes ／ TMI総合法律事務所** | API全件失敗後の検索フォールバック。ログにAPI実行計画・通信があり、失敗試行のDB記録漏れによって0件に見えていた。 |
| 41 | completed / RETRIEVAL | 0/0 | — | なし（確認要求/検索資料のみ） | 検索資料: **2025年のカナダを振り返る（1）カナダ経済を強く築くカーニー政権 ／ 地域・分析レポート - 海外ビジネス情報 - ジェトロ** | API全件失敗後の検索フォールバック。ログにAPI実行計画・通信があり、失敗試行のDB記録漏れによって0件に見えていた。 |
| 42 | completed / EXTERNAL_API | 1/2 | econ.worldbank: PROVIDER_CAPABILITY_UNSUPPORTED；politics.br_chamber: PROVIDER_EMPTY | Senado Federal Dados Abertos | # 分析結果：エビデンスに基づかない比較の要求について | 要求した指標/粒度をAPIアダプターが提供できない。能力照合と代替API選定不足。 |
| 43 | completed / EXTERNAL_API | 2/1 | politics.us_congress: PROVIDER_EMPTY | FEC API, USAspending.gov | 取得したAPI出力を根拠として示します。統合できる数値系列がないため、分析は限定されます。 | **除外済み（課題不備）** 選挙・候補者・企業・法案の指定がなく、選挙資金、法案、政府契約の関連を評価できない。 過去の実行記録としてのみ保持。 |
| 44 | completed / RETRIEVAL | 0/0 | — | なし（確認要求/検索資料のみ） | 検索資料: **アメリカ合衆国-連邦法の調べ方 ／ リサーチ・ナビ ／ 国立国会図書館** | **除外済み（課題不備）** 規則・政策分野・法案・裁判例の指定がなく、3種の文書を結びつける争点がない。 過去の実行記録としてのみ保持。 |
| 45 | completed / EXTERNAL_API | 2/3 | health.clinical_trials: PROVIDER_PARTIAL_COVERAGE；health.openfda: ARGUMENT_COMPILATION_FAILED；biology.protein: ARGUMENT_COMPILATION_FAILED | ClinicalTrials.gov, OpenAlex | 取得したAPI出力を根拠として示します。統合できる数値系列がないため、分析は限定されます。 | **除外済み（課題不備）** 疾患・薬剤・標的タンパク質がなく、臨床試験と有害事象・分子研究の対応を決定できない。 過去の実行記録としてのみ保持。 |
| 46 | completed / EXTERNAL_API | 2/1 | health.clinical_trials: PROVIDER_PARTIAL_COVERAGE | OpenAlex, ClinicalTrials.gov | 取得したAPI出力を根拠として示します。統合できる数値系列がないため、分析は限定されます。 | **除外済み（課題不備）** 感染症・研究課題・対象国がなく、論文、試験、健康指標の比較軸を決定できない。 過去の実行記録としてのみ保持。 |
| 47 | waiting_for_confirmation / EXTERNAL_API | 0/0 | — | なし（確認要求/検索資料のみ） | 、次の情報が必要です。 - 悪用可能性を確認するCVE番号を指定してください。 - 比較する脆弱性のCVE番号、または対象製品名を指定してください。 | **除外済み（課題不備）** CVE番号・製品名がなく、深刻度、悪用可能性、エクスプロイトの対象を特定できない。 過去の実行記録としてのみ保持。 |
| 48 | completed / EXTERNAL_API | 1/2 | knowledge.qiita: PROVIDER_PARTIAL_COVERAGE；dev.github: ARGUMENT_COMPILATION_FAILED | Qiita API v2 | Qiitaの検索結果4件から、実装内容を本文抜粋で確認できた記事を対象に整理します。 - 権限・実行範囲の制御: 抜粋で確認できた記事 — 。 これは検索上位の抜粋3件に現れた例の整理で、Qiita全体での採用頻度や代表性を示すものではありません。記事本文全体・実装コードを確認していないため、各方式の有効性も比較していません。 検索出典 。 | API引数コンパイラが必須フィールドを生成できず要求失敗。 |
| 49 | waiting_for_confirmation / EXTERNAL_API | 0/0 | — | なし（確認要求/検索資料のみ） | 栄養成分を調べる商品を特定できません。商品名またはバーコード番号を指定してください。 | **除外済み（課題不備）** 食品名・商品名・バーコードがなく、栄養成分、表示、レシピの対象を特定できない。 過去の実行記録としてのみ保持。 |
| 50 | completed / EXTERNAL_API | 1/3 | biology.occurrence: ARGUMENT_COMPILATION_FAILED；biology.protein: ARGUMENT_COMPILATION_FAILED；biology.phylogeny: ARGUMENT_COMPILATION_FAILED | OpenAlex | 取得したAPI出力を根拠として示します。統合できる数値系列がないため、分析は限定されます。 | **除外済み（課題不備）** 種名・学名・遺伝子/タンパク質がなく、観察・系統・分子研究を結びつけられない。 過去の実行記録としてのみ保持。 |

## Universal causes indicated by this run

1. **Argument compilation and provider contracts:** `ARGUMENT_COMPILATION_FAILED` blocked several tools (including trade, World Bank, and the biology APIs). Validate required/optional argument schemas before dispatch; compile target entities, time ranges, identifiers, and pagination from the plan; return field-level failures rather than silently dropping calls.
2. **Capability-aware planning:** unsupported operations are still selected (`PROVIDER_CAPABILITY_UNSUPPORTED`). The planner should compare requested metrics/granularity to provider capabilities and choose compatible alternatives or explicitly mark gaps.
3. **Test validity:** the five clarification-only prompts are excluded because mandatory target information was absent. Their clarification responses are correct behavior, not evidence that arbitrary defaults should be added. The other excluded cases similarly lack a shared analysis target. Remaining valid cases must still reject irrelevant provider sample rows.
4. **Relevance and temporal quality:** several cards contained weakly matched or stale results (e.g. unrelated events/indicators; old or generic search pages). Add entity, geography, period, and metric relevance gates before surfacing results; prefer exact data sources and recent coverage.
5. **Route/audit preservation (confirmed after repair investigation):** the logs show API plans and HTTP calls before RETRIEVAL fallbacks. The all-API-failure return path failed to attach API executions to the recovery task. Zero DB executions therefore did not prove that APIs were skipped. See `multi-api-analysis-r06-repair-2026-10-23.md` for the fix and real-call verification.
6. **Analysis completion:** successful API calls frequently ended with only cards or “no integrated numeric series.” A deterministic finalization path should summarize successful and failed calls, align overlapping entities/time periods, and clearly state where comparisons are impossible instead of returning a bare evidence panel.
7. **Context/RAG volume:** prior run logs already showed very large web context payloads (over 100k characters). This run continued to surface irrelevant web results. Relevance-first retrieval and configurable context budgets should happen before synthesis, not after the prompt has been assembled.

## Reproduction artifacts

- GUI launcher log: `/private/tmp/olcr-e2e-analysis-r06-20261023.log`
- Run DB: `/private/tmp/olcr-e2e-analysis-r06-20261023-profile/olcr.db`
- Profile-local model progress: `/private/tmp/olcr-e2e-analysis-r06-20261023-profile/model-progress.jsonl`
