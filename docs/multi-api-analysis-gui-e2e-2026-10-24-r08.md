# 修正後の実GUI E2E — 32課題（r08）

実行日: 2026-10-24 (JST)  
対象: 現行の選定済み32課題（`multi_api_analysis_cases.json`）。曖昧すぎる18課題はアーカイブ済みリストにあり、実行していない。  
方式: 最新ソースを専用の署名済みOLCR GUIビルドで起動し、**32件を同じ会話に順番に1回ずつ送信**。実APIルーティングを通し、成功したアダプターは公開APIから実データを取得。未対応/引数エラーの一部は送信前に拒否された可能性がある。  
Build ID: `olcr-analysis-r08-20261023`  
Bundle: `/private/tmp/olcr-e2e-analysis-r08-target/debug/bundle/macos/OLCR.app`  
専用プロファイル: `/private/tmp/olcr-e2e-analysis-r08-profile`  
起動確認: `READY_FOR_COMPUTER_USE=YES`; optional Ollama inventory diagnostic was `MODEL_RUNTIME_BINDINGS_STATUS=FAIL` (GUI/API E2Eは続行可能とランチャーが表示)。  
Conversation ID: `498931ae-e2e2-4eb0-8b4a-20e22dfc9500`  

## 実行結果

- ユーザー発話32件、アシスタント回答32件、交互に64メッセージ保存。fixtureの並び順と完全一致し、すべてのタスクは `completed`。重複送信・未送信は確認されなかった。
- 外部API実行レコードは **80件**: 成功39、失敗41。ここでの件数はアプリのAPIツール実行レコード数で、プロバイダ内部のHTTPリトライ回数ではない。
- 25課題で少なくとも一つのAPIが成功応答。6課題は全APIが失敗または未対応となり、推論モデルの留保付きフォールバックへ進んだ。1課題はAPI全失敗後にWeb資料へ補完した。
- 全API実行が成功扱いでも、課題で要求した指標をすべて満たしたことを意味しない。`PROVIDER_PARTIAL_COVERAGE` などは一部項目の不足として数えた。

### 課題別記録

「OK」はAPIアダプターが成功応答を返した記録であり、全指標の完全性を保証しない。失敗コードは実行ログの値を記録した。

| ID | API実行結果 | 回答の状態 |
|---:|---|---|
| 01 | Econ=OK | API証拠を使って回答 |
| 02 | Econ=OK; Federal Reserve FRED=OK; U.S. Bureau of Labor Statistics=OK | API証拠を使って回答 |
| 03 | Econ=OK; UN Comtrade=OK; trade.un_comtrade=PROVIDER_PARTIAL_COVERAGE | 部分取得／一部API失敗 |
| 04 | Econ=OK; IMF DataMapper=OK; econ.worldbank=PROVIDER_PARTIAL_COVERAGE; econ.imf=PROVIDER_PARTIAL_COVERAGE; development.un_sdg=PROVIDER_CAPABILITY_UNSUPPORTED | 部分取得／一部API失敗 |
| 05 | Econ=OK; econ.worldbank=PROVIDER_PARTIAL_COVERAGE | 部分取得／一部API失敗 |
| 06 | Eurostat=OK; Econ=OK | API証拠を使って回答 |
| 07 | UN Comtrade=OK; currency.frankfurter=INVALID_TOOL_ARGUMENTS | 部分取得／一部API失敗 |
| 08 | econ.worldbank=PROVIDER_EMPTY; aviation.opensky=PROVIDER_CAPABILITY_UNSUPPORTED | API証拠なし・推論フォールバック（留保表示） |
| 09 | Federal Reserve FRED=OK; U.S. Bureau of Labor Statistics=OK; gov.us_spending=PROVIDER_CAPABILITY_UNSUPPORTED | 部分取得／一部API失敗 |
| 11 | Econ=OK; Federal Reserve FRED=OK | API証拠を使って回答 |
| 12 | econ.oecd=PROVIDER_CAPABILITY_UNSUPPORTED; econ.worldbank=PROVIDER_CAPABILITY_UNSUPPORTED | API証拠なし・推論フォールバック（留保表示） |
| 13 | Econ=OK; econ.worldbank=PROVIDER_PARTIAL_COVERAGE | 部分取得／一部API失敗 |
| 14 | econ.worldbank=PROVIDER_EMPTY; environment.air_quality=ARGUMENT_COMPILATION_FAILED | API証拠なし・推論フォールバック（留保表示） |
| 15 | Earth=OK; earth.disasters=PROVIDER_CAPABILITY_UNSUPPORTED; humanitarian.hdx=PROVIDER_EMPTY | 部分取得／一部API失敗 |
| 18 | Econ=OK | API証拠を使って回答 |
| 19 | weather.open_meteo=PROVIDER_CAPABILITY_UNSUPPORTED; econ.worldbank=PROVIDER_CAPABILITY_UNSUPPORTED; environment.air_quality=ARGUMENT_COMPILATION_FAILED | API証拠なし・推論フォールバック（留保表示） |
| 21 | Econ=OK; UN SDG API=OK; development.un_sdg=PROVIDER_PARTIAL_COVERAGE | 部分取得／一部API失敗 |
| 22 | NOAA SWPC=OK | 取得データを限定して説明 |
| 23 | Earth=OK; earth.disasters=PROVIDER_CAPABILITY_UNSUPPORTED | 部分取得／一部API失敗 |
| 24 | econ.worldbank=PROVIDER_EMPTY; trade.un_comtrade=PROVIDER_UNAVAILABLE | 部分取得／一部API失敗 |
| 25 | UCDP Georeferenced Event Dataset=OK; UNHCR Refugee Data Finder=OK; humanitarian.hdx=PROVIDER_EMPTY | 部分取得／一部API失敗 |
| 26 | UNHCR Refugee Data Finder=OK; UCDP Georeferenced Event Dataset=OK; conflict.ucdp=PROVIDER_PARTIAL_COVERAGE; humanitarian.hdx=PROVIDER_EMPTY | 部分取得／一部API失敗 |
| 28 | sanctions.unsc=PROVIDER_EMPTY; conflict.ucdp=PROVIDER_EMPTY; sanctions.us_ofac=PROVIDER_EMPTY | API証拠なし・推論フォールバック（留保表示） |
| 29 | UNHCR Refugee Data Finder=OK; Econ=OK; humanitarian.unhcr=PROVIDER_PARTIAL_COVERAGE | 部分取得／一部API失敗 |
| 34 | UNHCR Refugee Data Finder=OK; conflict.ucdp=PROVIDER_EMPTY; humanitarian.hdx=PROVIDER_EMPTY | 部分取得／一部API失敗 |
| 37 | Policy=OK; gov.us_spending=PROVIDER_CAPABILITY_UNSUPPORTED; politics.us_congress=PROVIDER_EMPTY | 部分取得／一部API失敗 |
| 38 | 国会会議録検索API=OK; e-Gov 法令API=OK | API証拠を使って回答 |
| 39 | European Parliament Open Data=OK; EU Cellar=OK; econ.worldbank=PROVIDER_EMPTY; law.eu_cellar=PROVIDER_PARTIAL_COVERAGE | 部分取得／一部API失敗 |
| 40 | politics.uk_parliament=PROVIDER_CAPABILITY_UNSUPPORTED; health.clinical_trials=PROVIDER_UNAVAILABLE | API証拠なし・推論フォールバック（留保表示） |
| 41 | Econ=OK; politics.ca_openparliament=PROVIDER_CAPABILITY_UNSUPPORTED | 部分取得／一部API失敗 |
| 42 | Econ=OK; Senado Federal Dados Abertos=OK; politics.br_chamber=PROVIDER_EMPTY | 部分取得／一部API失敗 |
| 48 | Qiita API v2=OK; dev.github=PROVIDER_UNAVAILABLE | 部分取得／一部API失敗 |
## 今回確認した主な問題と原因候補

| 観測 | 原因候補 |
|---|---|
| Frankfurter は日本向けの為替照会で `INVALID_TOOL_ARGUMENTS` | 為替ペア/基準通貨を依頼文から特定する引数コンパイルが不足。国名「日本」からJPYを設定できていない可能性が高い。 |
| USAspending.gov、OpenParliament.ca、OpenSky、英国議会、複数の人道/制裁ソースが `PROVIDER_CAPABILITY_UNSUPPORTED` / `PROVIDER_UNAVAILABLE` / `PROVIDER_EMPTY` | 一律の通信障害ではなく、アダプターが未対応の機能・要求粒度、またはAPI応答を該当対象へ絞れない問題が残る。 |
| 米国/欧州の大気質照会が `ARGUMENT_COMPILATION_FAILED` | 任意の地域・期間を地点/系列/年月のAPI引数に変換できていない。 |
| 全APIが失敗した6課題で、留保付き一般知識回答に116〜316秒かかった | 最終推論フォールバック自体は発動したが、ローカル推論の遅延が大きい。APIを捏造せず回答した点は期待どおり。 |
| 成功と部分失敗の混在が多い（41件の失敗レコード） | ルーティング/記録の経路は残るが、対象APIのカバレッジや引数生成が依然として個別に不足。成功した兄弟APIの証拠は同じ回答に保持された。 |

失敗コードの内訳: `PROVIDER_EMPTY` 14件, `PROVIDER_CAPABILITY_UNSUPPORTED` 12件, `PROVIDER_PARTIAL_COVERAGE` 9件, `PROVIDER_UNAVAILABLE` 3件, `ARGUMENT_COMPILATION_FAILED` 2件, `INVALID_TOOL_ARGUMENTS` 1件.

## 同一会話での混在・コンテクスト確認

- DB上ではユーザー発話の `task_id` はNULL。直後の回答が持つ固有のタスクIDから参照した `tasks.raw_request` が隣接するユーザー発話と一致し、全32件がfixture順に並んだ。実行結果で以前のケースのAPI出力が後続ケースの回答として保存された例は見つからなかった。
- 推論フォールバック6件の記録上の入力は414〜422 prompt tokensでほぼ一定だった。会話が長くなっても全履歴が推論入力へ伸び続けた形跡はない。これはログに基づく推定であり、意味内容の混入を完全に否定するテストではない。
- 一方、アシスタントのAPI証拠メッセージがケースID 13: 73,733文字, ケースID 21: 115,723文字, ケースID 29: 51,304文字と大きくなった。これはUI/DB上の会話履歴を肥大化させるため、取得結果の証拠表示量にも上限を設け、全文データを会話文脈に残し続けない改善が必要。

## 結論

実GUI・実APIの32件はすべて完了し、同一会話内のケース順序とタスク分離は維持された。今回の主な残課題は、失敗APIの引数コンパイル/機能対応、人道・制裁・議会・環境データのカバレッジ、API証拠本文の大きさ、全滅時のローカル推論待ち時間である。とくに巨大な証拠本文は、今回の「混ざり込み」対策として次に直すべき共通原因候補。

この実行では新しいコード変更を加えていない。結果は次の詳細修正に使えるよう記録した。

後続の修正内容・修正後の回帰テスト・実API検証は [r08修正記録](multi-api-analysis-r08-repair-2026-10-24.md) を参照。このページの32件GUI結果は修正前の履歴として保存する。
