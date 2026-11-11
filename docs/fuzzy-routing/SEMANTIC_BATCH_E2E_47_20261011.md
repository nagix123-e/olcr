# 47件 GUI E2E 実行記録（2026-10-11）

## 実行概要

- 対象: 269件の修正記録のうち `specific_changes` を持つ47件（[元の修正記録](SEMANTIC_BATCH_FIXES_269_20261011.md)）
- 方法: 隔離したOLCRデスクトップアプリのGUIから、既存のprojectless会話1つに質問を1件ずつ送信
- ビルドID: `olcr-semantic47-r47c-20261011`
- 実行会話: `743c2f20-54a2-414e-8d8a-7cf889f3488f`
- 検証結果: ユーザー発話47件、アシスタント応答47件が隔離DBに保存。全47件の応答を取得
- タスク行: 71（1回の会話送信に外部API実行・回答生成の複数タスクが記録される場合を含む）

## 意味的な判定

| 判定 | 件数 |
|---|---:|
| PASS | 19 |
| PARTIAL | 18 |
| FAIL | 10 |

判定対象は、保存された質問に対して応答が内容面で答えているか、取得した証拠を回答へまとめているか、制約や不確実性を適切に説明しているかです。全ての外部データを独立に再検証する事実監査ではありません。API実行が成功しても回答が質問を満たさなければ合格にしていません。

FAIL 10件の内訳は、プロバイダー／Web検索の依存失敗6件（元番号27, 42, 97, 107, 142, 165）と、ツール成功・実行記録の有無とは別の回答上の問題4件（元番号53, 75, 121, 174）です。特に元番号121は徒歩・自転車双方の数値が取得済みなのに回答へ統合されず、元番号75はRDAPの登録者・レジストラ項目を誤解させる説明を含みます。

### 全47件の判定

| 元番号 | ケース | 判定 | 根拠の要約 |
|---:|---|---|---|
| 7 | `space.launches#3` | **PARTIAL** | 変更履歴のないUSSF-481まで「3件すべて繰り返し変更」と総括している。最初の2件の変更情報は回答しているが、冒頭と結論が証拠を超えている。 |
| 21 | `space.iss_position#1` | **PASS** | ISSの座標から海上と回答し、観測時刻・判定精度の限界も示している。 |
| 22 | `space.iss_position#2` | **PASS** | 進行方位と日本の基準点への距離変化を示し、通過予報とは区別している。 |
| 24 | `space.iss_position#4` | **PARTIAL** | 座標は出したが、依頼された近隣都市・地域を提示できていない。取得失敗を明示しており、捏造はない。 |
| 27 | `space.exoplanets#3` | **FAIL** | NASA Exoplanet Archive取得と代替検索が失敗し、該当惑星を一件も提示できていない。 |
| 34 | `japan.law#2` | **PARTIAL** | 目的条文と代表的義務を比べているが、義務の範囲を17〜27条と断定しながら、条文の説明・範囲に誤りと省略があり、監督規定との接続も不正確。 |
| 42 | `earth.streamflow#2` | **FAIL** | USGSの観測所・流量を提示できず、Web検索設定案内だけで依頼を満たしていない。 |
| 47 | `biology.occurrence#3` | **PARTIAL** | 上野周辺の鳥類記録と出典は示すが、最新記録が実行日より約3か月前で、「最近」の要件に合うか明示評価していない。 |
| 53 | `biology.structure#1` | **FAIL** | 酸素結合状態の表で2HBSと1GLIを酸素結合と脱酸素の両方に分類する矛盾があり、依頼された比較も完了していない。 |
| 54 | `biology.structure#2` | **PASS** | ヒト以外の代表例を複数提示し、構造ID・生物種・解像度を含めて質問に答えている。 |
| 55 | `biology.structure#3` | **PASS** | 解像度・登録日・実験法・論文情報を含む引用候補を複数提示している。 |
| 56 | `biology.structure#4` | **PARTIAL** | 比較可能な複合体がないという制約は説明しているが、複合体と単独構造の例や構造差は提示できていない。 |
| 57 | `biology.protein#1` | **PASS** | BRCA1の機能と関連経路をUniProtの識別子付きで具体的に説明している。 |
| 58 | `biology.protein#2` | **PARTIAL** | ヒト・マウスの識別子と配列長は比較したが、依頼の中心である基本機能を説明できていない。 |
| 59 | `biology.protein#3` | **PARTIAL** | 前駆体と成熟A/B鎖の関係は説明するが、成熟鎖の長さ・切断位置を回答できず、110残基の前駆体を主に扱っている。 |
| 60 | `biology.protein#4` | **PASS** | ヒトEGFRの別名と1210残基の配列長をUniProt識別子付きで示している。 |
| 75 | `internet.domain_registration#3` | **FAIL** | 登録者情報の中心的回答にレジストラをICANNとする混同があり、レジストラ連絡先にも疑わしい値をそのまま断定している。登録状況の説明として信頼できない。 |
| 76 | `internet.domain_registration#4` | **PARTIAL** | openai.comのネームサーバーは列挙したが、wikipedia.orgを取得できず比較を完了していない。 |
| 85 | `games.pokemon#1` | **PASS** | ピカチュウからの通常・アローラの進化先と、かみなりのいしという条件を回答している。 |
| 89 | `games.trivia#1` | **PASS** | 小学生向けの簡単な宇宙四択クイズを1問提示している。答えも表示する形式だが、非表示指定はない。 |
| 90 | `games.trivia#2` | **PARTIAL** | 映画クイズを提示したが、考えるための間を装いながら同じ回答内で正解を公開している。クイズとしての使い勝手を損ねる。 |
| 92 | `games.trivia#4` | **PASS** | 科学の四択問題だけを提示し、正解を表示していない。 |
| 97 | `art.met_collection#1` | **FAIL** | Met Collection APIの取得と代替検索が失敗し、風景画・自画像の所蔵情報を一件も提示していない。 |
| 102 | `games.pc_deals#2` | **PARTIAL** | 1作品の価格・割引率は示すが、比較を求める質問に対して1件しかなく比較不能。加えて正確性を担保したとの記述がデータ不審の注意書きと矛盾する。 |
| 107 | `games.chess#3` | **FAIL** | Chess.com取得失敗後のWeb設定案内のみで、棋譜や対局情報を提示できていない。 |
| 108 | `games.chess#4` | **PARTIAL** | 前問の失敗を引き継いで比較不能と説明している点は誠実だが、独立した依頼への再取得・比較は行われていない。 |
| 119 | `environment.air_quality#3` | **PASS** | 東京で最も高い構成AQI指標をPM2.5と特定し、異なる汚染物質を同じ尺度で比べると説明している。 |
| 121 | `geo.routing#1` | **FAIL** | 徒歩・自転車の両ルート数値自体は取得済みなのに、合成せずAPIの生データを列挙し、「統合できない」として距離・時間差を回答していない。 |
| 124 | `geo.routing#4` | **PARTIAL** | 車ルート1件の距離・経路は出すが、候補1件だけで「最短」とは証明できず、質問に指定のない車プロファイルを採用している。 |
| 142 | `earth.disasters#2` | **FAIL** | 災害データ取得が失敗し、発生地域・開始日を一件も回答できていない。 |
| 146 | `ocean.tides#2` | **PASS** | サンフランシスコの当日満潮・干潮と最高満潮から最低干潮までの差を示している。 |
| 148 | `ocean.tides#4` | **PARTIAL** | サンフランシスコ分はあるが、オークランドを取得できず、尋ねられた両港の時刻差を計算できていない。制約は明示している。 |
| 155 | `books.openlibrary#3` | **PASS** | 指定ISBNの作品を特定し、複数の翻訳版・英語別版を列挙し、サンプルであることも明示している。 |
| 165 | `health.clinical_trials#1` | **FAIL** | ClinicalTrials.govの取得と代替検索が失敗し、国内募集中試験や介入比較を示せていない。 |
| 169 | `health.openfda#1` | **PARTIAL** | 報告された事象と因果・発生率を区別している点は適切。一方、2026-06-30の5件を「最近／直近」と扱う根拠や抽出条件が弱く、母集団傾向として読むのは不適切。 |
| 174 | `food.product#2` | **FAIL** | 商品名・商品識別子が必要なのに、引数コンパイル失敗後も何の商品かを尋ねず、場所・期間など不適切な追加条件を求めている。 |
| 192 | `aviation.opensky#4` | **PARTIAL** | 座標付き一覧は提示したが、地上にいる機体を「東京上空」の一覧へ含め、地上状態から離着陸中と推測している。取得件数の網羅性に関する説明も一部不整合。 |
| 270 | `finance.ecb#2` | **PASS** | 営業日ごとのEUR/USDを列挙し、期間変化と方向を数値に整合する形で示している。 |
| 272 | `finance.ecb#4` | **PASS** | 直近営業日のEUR/JPYと直近5営業日の推移、参考レートである注意を示している。 |
| 286 | `politics.br_chamber#2` | **PASS** | 教育予算関連の法案と委員会状況を2件示し、撤回済みと審議中を区別し、検索範囲の限界も明記している。 |
| 287 | `politics.br_chamber#3` | **PASS** | 環境関連提案を提出者・日付付きで列挙し、上院提出の1件を下院議員案と区別している。 |
| 288 | `politics.br_chamber#4` | **PASS** | CLT改正案と提出日・審議状況を示し、検索結果がサンプルであることや履歴切り詰めを明記している。 |
| 309 | `gov.us_spending#1` | **PASS** | NASA発注のSpaceX契約を額の大きい順に目的付きで示し、累積契約額であることを明示している。 |
| 316 | `law.us_courts#4` | **PARTIAL** | 最近の判決候補を裁判所・日付・URL付きで示すが、判決理由や著作権論点との関係は取得できず、関連性を読者が確認する必要がある。 |
| 321 | `sanctions.us_ofac#1` | **PASS** | OFAC SDNの検索結果中でIran Airを明示し、別名と制裁プログラムを列挙している。 |
| 345 | `aviation.operational_weather#1` | **PARTIAL** | METARと風・視程を提示するが、着陸への影響がありそうかという判断を示さず、滑走路・機体任せで結論を保留している。 |
| 346 | `aviation.operational_weather#2` | **PARTIAL** | METARとTAFの値は示すが、数時間後に何がどう変わるかを平易に要約していない。風向変化などの比較を利用者に委ねている。 |

## 実行時の制約

- Keychain資格情報ブリッジの復元は `credential_bundle_bridge_timeout` で終了。資格情報/APIキーは入力していない。したがってWeb検索プロバイダー未設定の結果は、この隔離プロファイルの制約を受ける。
- Webプロバイダーを設定していない状態で、最初に試した一括CLI経路は46件ともHTTP 401となった。これらは会話履歴・タスクを作っていないため、GUI E2Eの47件には含めず、セッション資格情報の抽出・再利用も行っていない。
- 一部の回答で、DB上の完了・保存よりGUIのローダー消失が遅れる挙動を観測。実行結果はDB保存を照合して記録した。

## プロバイダー実行で失敗を記録した7件

いずれもアシスタント応答自体は保存されていますが、期待する外部データ取得の成功を示しません。

| 元番号 | ケース | 失敗内容 |
|---:|---|---|
| 27 | `space.exoplanets#3` | `space.exoplanets`: PROVIDER_UNAVAILABLE, `external_api`: PROVIDER_UNAVAILABLE; `direct_provider_failure_fallback`: WEB_PROVIDER_NOT_CONFIGURED |
| 42 | `earth.streamflow#2` | `earth.streamflow`: PROVIDER_TIMEOUT, `external_api`: PROVIDER_TIMEOUT; `direct_provider_failure_fallback`: WEB_PROVIDER_NOT_CONFIGURED |
| 97 | `art.met_collection#1` | `art.met_collection`: PROVIDER_UNAVAILABLE, `external_api`: PROVIDER_UNAVAILABLE; `direct_provider_failure_fallback`: WEB_PROVIDER_NOT_CONFIGURED |
| 107 | `games.chess#3` | `games.chess`: PROVIDER_BAD_RESPONSE, `external_api`: PROVIDER_BAD_RESPONSE; `direct_provider_failure_fallback`: WEB_PROVIDER_NOT_CONFIGURED |
| 142 | `earth.disasters#2` | `earth.disasters`: PROVIDER_EMPTY, `external_api`: PROVIDER_EMPTY; `direct_provider_failure_fallback`: WEB_PROVIDER_NOT_CONFIGURED |
| 165 | `health.clinical_trials#1` | `health.clinical_trials`: PROVIDER_BAD_RESPONSE, `external_api`: PROVIDER_BAD_RESPONSE; `direct_provider_failure_fallback`: WEB_PROVIDER_NOT_CONFIGURED |
| 174 | `food.product#2` | `external_api`: ARGUMENT_COMPILATION_FAILED; `failed`: ARGUMENT_COMPILATION_FAILED |

元番号174は外部プロバイダーの停止とは別に、商品を特定するための質問をせず、引数コンパイル失敗後に場所・期間を求める不適切な案内を返したため、意味判定でもFAILです。

## 実行状態の内訳

- 39ケース: `external_api` 成功を記録
- 1ケース: 外部実行の失敗記録なし（Chess.com PubAPIの2件目）
- 7ケース: 外部データ取得の失敗記録あり（上表）

## 詳細ログ

全47件について、入力、期待ツール、完全なアシスタント回答、タスク状態/所要時間、ツール名/状態、モデル呼び出し状態、意味判定と根拠をJSONに保存しました: [SEMANTIC_BATCH_E2E_47_20261011.json](SEMANTIC_BATCH_E2E_47_20261011.json)

ログ内のケース順は修正記録の元番号順です。GUI会話DBと隔離起動ログもJSONの実行メタデータに記載しています。
