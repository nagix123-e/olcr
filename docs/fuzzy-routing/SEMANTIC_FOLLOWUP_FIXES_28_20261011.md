# PARTIAL・FAIL 28件の修正記録（2026-10-11）

47件のGUI E2Eの意味判定から、PARTIAL 18件・FAIL 10件を対象に修正。全28件へ修正を対応づけました。過去の応答・判定は保存し、合格に変更していません。

既存のAPI adapter、型付きrenderer、grounded composer、共有chat routerの責務内で修正し、新しいAPI処理基盤は追加していません。RDAP・経路・Triviaの旧表示分岐は、新しい既存renderer内の表示へ集約し、二重実装を残していません。

## ケース別対応

| 元番号 | ケース | 元判定 | 修正 |
|---:|---|---|---|
| 7 | `space.launches#3` | PARTIAL | 現在の予定・状態と公表更新を既存rendererで表示。新規登録・GOへの変更を延期と断定しない。 |
| 24 | `space.iss_position#4` | PARTIAL | 海上で逆ジオコーディングが失敗した場合、検証済み海岸線と限定された座標範囲から概略地域を表示し、その推定方法を明記。 |
| 27 | `space.exoplanets#3` | FAIL | NASA TAPの待ち時間を20秒へ。元のAPI失敗理由を代替Web設定エラーで隠さない。 |
| 34 | `japan.law#2` | PARTIAL | 取得した条番号・見出し・本文に基づく目的／義務比較へ。対象条文の収集範囲を拡張し、未取得条文の役割を断定しない。 |
| 42 | `earth.streamflow#2` | FAIL | USGSの待ち時間20秒・有限の応答上限4 MiBへ。元のタイムアウトとWeb未設定を区別。 |
| 47 | `biology.occurrence#3` | PARTIAL | 対象期間と返却された最新日を表示。日付・種名・座標がない匿名レコードを除外し、観察日降順へ。 |
| 53 | `biology.structure#1` | FAIL | 酸素分子OXYを正式な検索属性で照会。取得したリガンド・原題により区別し、deoxyと酸素結合を同時に分類しない。 |
| 56 | `biology.structure#4` | PARTIAL | 酸素化／脱酸素化検索の流用をやめ、hemoglobin-haptoglobin複合体とhemoglobinを検索。構成タンパク質と同種比較候補を表示。 |
| 58 | `biology.protein#2` | PARTIAL | Reviewedタンパク質を優先。FUNCTION本文を前方へ正規化し、入れ子の文字数・深さ制限でコメント本文が空にならないよう改善。 |
| 59 | `biology.protein#3` | PARTIAL | Signal／Propeptide／Chain／Peptideの切断位置を優先保持。前駆体配列長と成熟鎖の残基位置を混同しないための指示を追加。 |
| 75 | `internet.domain_registration#3` | FAIL | RDAP registrarとregistrantを役割別に表示。空の登録者欄を非公開の証明とせず、別主体の連絡先で補わない。 |
| 76 | `internet.domain_registration#4` | PARTIAL | RDAP fanoutを既存型付き表示で展開し、両方のネームサーバーと共通設定を比較。欠落は引き続き明示。 |
| 90 | `games.trivia#2` | PARTIAL | クイズの正解は明示要求時だけ渡す。少し難しめはmediumへ。表示のfallbackも四択と正解非表示を維持。 |
| 97 | `art.met_collection#1` | FAIL | Met APIの待ち時間20秒へ。接続エラーを保持したまま代替取得の成否を説明。 |
| 102 | `games.pc_deals#2` | PARTIAL | 協力カテゴリ確認候補を有限の32件へ拡張。価格・割引を既存rendererで並べ、1件を比較完了とせず、正確性の根拠なき保証を除去。 |
| 107 | `games.chess#3` | FAIL | Chess.com月別棋譜応答の有限上限8 MiB・待ち時間20秒へ。過大パケットをスキーマ異常と誤判定する条件を緩和。 |
| 108 | `games.chess#4` | PARTIAL | 同じ会話の直前のユーザー指定からプレイヤーを解決し、前問の取得失敗とは別に新規取得。アシスタントの推測からは対象を決めない。stream経路も共有routerへ。 |
| 121 | `geo.routing#1` | FAIL | 徒歩・自転車の取得済み数値から距離と時間の差を直接計算。直接回答・自律分析の双方で時系列専用fallbackを回避。 |
| 124 | `geo.routing#4` | PARTIAL | OSRMの返却候補を取得し、短い道順では候補中の距離最小を選ぶ。移動手段と範囲を明記し、全道路の最短と断定しない。 |
| 142 | `earth.disasters#2` | FAIL | EONETへ地域bboxを正式な順序で送信し、世界の結果上限を適用してから地域を絞る欠落を防止。 |
| 148 | `ocean.tides#4` | PARTIAL | オークランドの自動選択を気象用9414776から、潮汐予報対応9414777へ。比較時刻と現地日付の処理を維持。 |
| 165 | `health.clinical_trials#1` | FAIL | ClinicalTrials.govの使用フィールドに照会を限定し、有限上限8 MiB・待ち時間20秒へ。元の応答エラーと代替検索の状態を区別。 |
| 169 | `health.openfda#1` | PARTIAL | 取得した報告日・反応別報告数を型付き表示。serious=1を重篤、2を非重篤と区別し、古い最新返却日を現在直近と表現しない。 |
| 174 | `food.product#2` | FAIL | 引数エラーにも食品専用の確認質問を適用し、場所・期間を求める汎用エラーを回避。stream経路にもtool IDを渡す。 |
| 192 | `aviation.opensky#4` | PARTIAL | 上空／飛行中の依頼はon_ground=falseの観測だけを表示。高度m・速度m/sを型付き表示し、地上状態から離着陸を推測しない。 |
| 316 | `law.us_courts#4` | PARTIAL | 判決本文の追加HTML形式html_lawbox／html_columbiaにも対応。API本文取得が未完了なら引き続き判決内容を補わない。 |
| 345 | `aviation.operational_weather#1` | PARTIAL | CAVOKと観測風から視程・風の意味を説明。横風や着陸可否は滑走路・機体条件が必要と明記。 |
| 346 | `aviation.operational_weather#2` | PARTIAL | TAF区間のepochをUTC日時へ変換し、観測からの風速差・風向差を算出。視程の比較は予報区間ごとの値で行う。 |

## 確認範囲

- 隔離したheadlessプロファイルのオフライン検証：37件合格（既存23件＋今回の回帰14件）。HTTP・モデル呼び出しはfixture/mock。ユーザーのアプリ・DBには接続していません。
- 変更した3つのPythonソースのAST解析と対象ファイルの `git diff --check` は通過。
- ログ：`.live/semantic28-final-offline.log`。
- 後続の実GUI E2Eは[別記録](SEMANTIC_FOLLOWUP_GUI_E2E_28_20261011.md)に保存。28件すべてをGUIから試行。23件がterminal（20完了・3失敗）、5件は応答未完了。全ケース別記録は[GUI E2E記録](SEMANTIC_FOLLOWUP_GUI_E2E_28_20261011.md)を参照。
- Keychain復元・資格情報の状態は今回変更・検査していません。28件の意味PASSは再実行後に別途判定する必要があります。

FDAの元応答は `serious: 2` を重篤と説明していました。今回の修正ではこの取り違えも修正対象へ含めています。

## 確認した外部仕様

- [NOAAのOakland Middle Harbor潮汐予報](https://tidesandcurrents.noaa.gov/noaatidepredictions.html?id=9414777&legacy=1)：9414777は潮汐予報対応。以前の9414776は気象観測所。
- [RCSB公式検索属性](https://www.rcsb.org/docs/search-and-browse/advanced-search/attribute-details)：非高分子リガンドのcomponent IDを用いてOXYを検索。
- [NASA EONET v3](https://eonet.gsfc.nasa.gov/docs/v3)：bboxはmin longitude, max latitude, max longitude, min latitudeの順。

## 保存と復元

- 修正前ソース：`.live/semantic28-fixes-20261011-before/backend/olcr_api/`。
- 今回だけのソース差分：`.live/semantic28-source.diff`。前後SHA256とケース別状態は[JSON台帳](SEMANTIC_FOLLOWUP_FIXES_28_20261011.json)に保存。
- 既存の大量の変更を含む `git diff` や `git reset` を復元に使わず、今回の差分だけを確認して戻す。
- [元の47件ログ](SEMANTIC_BATCH_E2E_47_20261011.json)には今回の修正概要・再GUI検証未実施の状態を追記。元の応答と意味判定は保存。
